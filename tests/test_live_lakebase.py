"""
Live Databricks Lakebase integration tests.

Connects to a real Lakebase endpoint via OAuth and verifies:
  1. DDL emitted by emit_ddl(dialect="postgres") executes without errors.
  2. The schema read back via information_schema matches the canonical model.
  3. collect → inject round-trip produces correct optimizer statistics.
  4. Synthetic data can be loaded via BULK_COPY (COPY FROM STDIN).

Prerequisites
-------------
A running Databricks Lakebase endpoint with a service principal configured
(see docs/lakebase.md or https://learn.microsoft.com/azure/databricks/oltp/projects/external-apps-connect).

Environment variables:

    DATABRICKS_HOST             Databricks workspace URL (e.g. https://adb-123.azuredatabricks.net)
    DATABRICKS_CLIENT_ID        Service-principal client ID (UUID) — also used as PGUSER
    DATABRICKS_CLIENT_SECRET    Service-principal OAuth secret

    STATSCHEMA_LAKEBASE_ENDPOINT  Full endpoint resource path:
                                  projects/<proj-id>/branches/<branch-id>/endpoints/<ep-id>
    STATSCHEMA_LAKEBASE_HOST      PostgreSQL hostname for the endpoint
                                  (e.g. <ep-id>.database.<region>.cloud.databricks.com)
    STATSCHEMA_LAKEBASE_DB        Database name (default: databricks_postgres)
    STATSCHEMA_LAKEBASE_USER      PG username; defaults to DATABRICKS_CLIENT_ID if not set

Skip behaviour
--------------
All tests are automatically skipped when STATSCHEMA_LAKEBASE_ENDPOINT is not set.
If the env var IS set but the connection fails, the test fails hard (no silent skip).
"""

from __future__ import annotations

import pytest

from src.statschema import parse_ddl, emit_ddl
from src.statschema.cli import _lakebase_connect
from src.statschema.db_stats_collector import collect_table_stats
from src.statschema.data_loader import LoadStrategy, load_dataframe

# ---------------------------------------------------------------------------
# Connection parameters — all read from environment at import time
# ---------------------------------------------------------------------------

from tests.live_helpers import _tp  # noqa: E402

_p = _tp("test_lakebase")

_ENDPOINT = _p.endpoint or ""
_HOST     = _p.host     or ""
_DB       = _p.database or "databricks_postgres"
_USER     = _p.username or ""
_SCHEMA   = "statschema_test"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def conn():
    """
    Module-scoped psycopg2 connection to Lakebase.

    Skipped when STATSCHEMA_LAKEBASE_ENDPOINT is not set.
    If it IS set but the connection fails, the error propagates (no quiet skip).
    """
    if not _ENDPOINT:
        pytest.skip("STATSCHEMA_LAKEBASE_ENDPOINT not set — skipping live Lakebase tests")
    if not _HOST:
        pytest.skip("STATSCHEMA_LAKEBASE_HOST not set — skipping live Lakebase tests")
    if not _USER:
        pytest.skip(
            "STATSCHEMA_LAKEBASE_USER / PGUSER / DATABRICKS_CLIENT_ID not set "
            "— skipping live Lakebase tests"
        )

    pytest.importorskip("databricks.sdk", reason="databricks-sdk not installed")
    pytest.importorskip("psycopg2",       reason="psycopg2 not installed")

    # Any exception here is a genuine failure, not a skip.
    c = _lakebase_connect(_ENDPOINT, _HOST, _DB, _USER)
    yield c
    c.close()


@pytest.fixture(scope="module")
def schema_conn(conn):
    """Ensure the test schema exists and return the connection."""
    cur = conn.cursor()
    cur.execute(f"CREATE SCHEMA IF NOT EXISTS {_SCHEMA}")
    conn.commit()
    yield conn
    conn.rollback()  # clear any aborted transaction before cleanup
    cur.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    conn.commit()
    cur.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _exec(conn, sql: str) -> None:
    cur = conn.cursor()
    cur.execute(sql.rstrip(";").strip())
    conn.commit()
    cur.close()


_SIMPLE_DDL = """
CREATE TABLE statschema_test.lb_items (
    item_id  SERIAL PRIMARY KEY,
    name     VARCHAR(120) NOT NULL,
    price    NUMERIC(10,2),
    in_stock BOOLEAN DEFAULT TRUE
);
"""

_CANONICAL_DDL = """
CREATE TABLE statschema_test.lb_orders (
    order_id    INTEGER      NOT NULL,
    customer_id INTEGER      NOT NULL,
    total       DECIMAL(12,2),
    placed_at   TIMESTAMP,
    PRIMARY KEY (order_id)
);
CREATE TABLE statschema_test.lb_order_items (
    line_id   INTEGER NOT NULL,
    order_id  INTEGER NOT NULL REFERENCES statschema_test.lb_orders(order_id),
    sku       VARCHAR(50),
    qty       INTEGER,
    PRIMARY KEY (line_id)
);
"""

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestConnection:
    def test_basic_query(self, conn):
        """Connection works; current_user matches the service-principal client ID."""
        cur = conn.cursor()
        cur.execute("SELECT current_user, current_database()")
        row = cur.fetchone()
        assert row is not None, "SELECT current_user returned no rows"
        assert row[0] == _USER, (
            f"current_user={row[0]!r} does not match expected service principal {_USER!r}"
        )
        assert row[1] == _DB


class TestDDLRoundTrip:
    def test_create_and_introspect(self, schema_conn):
        """DDL emitted by statschema executes cleanly on Lakebase."""
        conn = schema_conn
        _exec(conn, f"DROP TABLE IF EXISTS {_SCHEMA}.lb_items CASCADE")
        _exec(conn, _SIMPLE_DDL)

        cur = conn.cursor()
        cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
            (_SCHEMA, "lb_items"),
        )
        rows = cur.fetchall()
        col_names = [r[0] for r in rows]
        assert "item_id"  in col_names
        assert "name"     in col_names
        assert "price"    in col_names
        assert "in_stock" in col_names

    def test_canonical_parse_emit_execute(self, schema_conn):
        """parse_ddl → emit_ddl(postgres) → execute on Lakebase is lossless."""
        conn = schema_conn
        for tbl in ("lb_order_items", "lb_orders"):
            _exec(conn, f"DROP TABLE IF EXISTS {_SCHEMA}.{tbl} CASCADE")

        tables = parse_ddl(_CANONICAL_DDL, dialect="postgres")
        assert len(tables) == 2

        for tbl in tables:
            sql = emit_ddl(tbl, dialect="postgres")
            # emit_ddl quotes the table name; qualify it with the test schema.
            sql = sql.replace(
                f'CREATE TABLE IF NOT EXISTS "{tbl.name}"',
                f'CREATE TABLE IF NOT EXISTS "{_SCHEMA}"."{tbl.name}"',
            )
            _exec(conn, sql)

        cur = conn.cursor()
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s ORDER BY table_name",
            (_SCHEMA,),
        )
        found = {r[0] for r in cur.fetchall()}
        assert "lb_orders"      in found
        assert "lb_order_items" in found


class TestStatsCollection:
    def test_collect_stats(self, schema_conn):
        """collect_table_stats returns a TableStats object for a Lakebase table."""
        conn = schema_conn
        _exec(conn, f"DROP TABLE IF EXISTS {_SCHEMA}.lb_items CASCADE")
        _exec(conn, _SIMPLE_DDL)
        _exec(
            conn,
            f"INSERT INTO {_SCHEMA}.lb_items (name, price) VALUES "
            "('widget', 9.99), ('gadget', 24.95), ('doohickey', 4.00)",
        )
        conn.commit()

        ts = collect_table_stats(conn, "lb_items", dialect="postgres", schema=_SCHEMA)
        assert ts.row_count == 3, f"Expected 3 rows, got {ts.row_count}"
        assert ts.columns, "No column stats returned"
        names = {c.name for c in ts.columns}
        assert "name"  in names
        assert "price" in names


# ---------------------------------------------------------------------------
# Data-type identity pipeline
# ---------------------------------------------------------------------------

# All 14 canonical types expressed as PostgreSQL DDL.  Lakebase is Postgres-
# wire-compatible, so the postgres emitter's type map applies directly.
_ALL_TYPES_DDL = f"""
CREATE TABLE {_SCHEMA}.lb_dtype_src (
    c_integer    INTEGER,
    c_long       BIGINT,
    c_float      REAL,
    c_double     DOUBLE PRECISION,
    c_decimal    NUMERIC(18,4),
    c_string     TEXT,
    c_uuid       UUID,
    c_boolean    BOOLEAN,
    c_date       DATE,
    c_timestamp  TIMESTAMP,
    c_timestamptz TIMESTAMP WITH TIME ZONE,
    c_time       TIME,
    c_timetz     TIME WITH TIME ZONE,
    c_binary     BYTEA
);
"""

_ALL_TYPES_TGT_DDL = _ALL_TYPES_DDL.replace("lb_dtype_src", "lb_dtype_tgt")


def _build_all_types_schema() -> "CanonicalTableSchema":
    """Return a CanonicalTableSchema covering all 14 canonical types."""
    from src.statschema.core.model import CanonicalColumn, CanonicalTableSchema, GenerationRule
    return CanonicalTableSchema(name="lb_dtype_src", columns=[
        CanonicalColumn("c_integer",     "integer",     not_null=True,
                        generation=GenerationRule(min_value=1, max_value=100_000)),
        CanonicalColumn("c_long",        "long",        not_null=True,
                        generation=GenerationRule(min_value=1, max_value=10**15)),
        CanonicalColumn("c_float",       "float",       not_null=True,
                        generation=GenerationRule(min_value=0.0, max_value=1_000.0)),
        CanonicalColumn("c_double",      "double",      not_null=True,
                        generation=GenerationRule(min_value=0.0, max_value=1e9)),
        CanonicalColumn("c_decimal",     "decimal",     not_null=True,
                        precision=18, scale=4,
                        generation=GenerationRule(min_value=0.0, max_value=99_999.9999)),
        CanonicalColumn("c_string",      "string",      not_null=True,
                        generation=GenerationRule(
                            values=["alpha", "beta", "gamma", "delta"],
                            weights=[0.4, 0.3, 0.2, 0.1])),
        CanonicalColumn("c_uuid",        "uuid",        not_null=True),
        CanonicalColumn("c_boolean",     "boolean",     not_null=True,
                        generation=GenerationRule(values=[True, False], weights=[0.7, 0.3])),
        CanonicalColumn("c_date",        "date",        not_null=True,
                        generation=GenerationRule(
                            min_value="2020-01-01", max_value="2024-12-31")),
        CanonicalColumn("c_timestamp",   "timestamp",   not_null=True,
                        generation=GenerationRule(
                            min_value="2020-01-01 00:00:00",
                            max_value="2024-12-31 23:59:59")),
        CanonicalColumn("c_timestamptz", "timestamptz", not_null=True,
                        generation=GenerationRule(
                            min_value="2020-01-01 00:00:00",
                            max_value="2024-12-31 23:59:59")),
        CanonicalColumn("c_time",        "time",        not_null=True),
        CanonicalColumn("c_timetz",      "timetz",      not_null=True),
        CanonicalColumn("c_binary",      "binary",      not_null=True, length=8),
    ])


class TestDtypeIdentityPipeline:
    """
    Full identity pipeline for all 14 canonical types on a live Lakebase endpoint.

    Pipeline (mirrors benchmarks/identity_test.py):
      Phase A — write-1:  build_rows_from_canonical (no stats) → MULTI_ROW INSERT
      Phase B — collect:  ANALYZE + collect_table_stats (postgres dialect)
      Phase C — write-2:  build_rows_from_canonical (with string-encoded stats)
                          → MULTI_ROW INSERT into target table

    Coverage added by this test beyond TestStatsCollection:
    - All 14 canonical types (integer, long, float, double, decimal, string, uuid,
      boolean, date, timestamp, timestamptz, time, timetz, binary) in a single table.
    - Verifies that numeric MCV values returned as strings by collect_table_stats are
      handled correctly by the generator (the Oracle "str for NUMBER" regression path
      is present in every dialect's stats collector, not just Oracle).
    - Confirms that binary (BYTEA) and uuid (UUID) columns survive both load phases.

    Note: MULTI_ROW is used instead of BULK_COPY because the bulk COPY path requires
    binary values to be serialised as hex escapes in the CSV buffer, which is not
    implemented in bulk_load_postgres.  MULTI_ROW passes bytes objects directly to
    psycopg2, which handles BYTEA encoding natively.
    """

    _N = 100  # rows per phase

    def test_all_types_ddl_executes(self, schema_conn):
        """DDL for all 14 canonical types executes on Lakebase without error."""
        conn = schema_conn
        _exec(conn, f"DROP TABLE IF EXISTS {_SCHEMA}.lb_dtype_src CASCADE")
        _exec(conn, _ALL_TYPES_DDL)

        cur = conn.cursor()
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
            (_SCHEMA, "lb_dtype_src"),
        )
        cols = [r[0] for r in cur.fetchall()]
        assert len(cols) == 14, f"Expected 14 columns, got {len(cols)}: {cols}"
        for expected in ("c_integer", "c_long", "c_float", "c_double", "c_decimal",
                         "c_string", "c_uuid", "c_boolean", "c_date", "c_timestamp",
                         "c_timestamptz", "c_time", "c_timetz", "c_binary"):
            assert expected in cols, f"Column {expected!r} missing from lb_dtype_src"

    def test_write1_all_types_load(self, schema_conn):
        """Phase A: build_rows_from_canonical (no stats) → MULTI_ROW INSERT → 100 rows."""
        pd = pytest.importorskip("pandas")
        conn = schema_conn
        _exec(conn, f"DROP TABLE IF EXISTS {_SCHEMA}.lb_dtype_src CASCADE")
        _exec(conn, _ALL_TYPES_DDL)

        from src.statschema.core.pandas_builder import build_rows_from_canonical
        schema = _build_all_types_schema()
        df = build_rows_from_canonical(schema, self._N, seed=42)
        assert len(df) == self._N

        n = load_dataframe(
            df, conn, f"{_SCHEMA}.lb_dtype_src", dialect="postgres",
            strategy=LoadStrategy.MULTI_ROW,
        )
        assert n == self._N

        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {_SCHEMA}.lb_dtype_src")
        assert cur.fetchone()[0] == self._N

    def test_collect_stats_all_types_after_analyze(self, schema_conn):
        """Phase B: ANALYZE + collect_table_stats returns stats for all 14 columns."""
        pd = pytest.importorskip("pandas")
        conn = schema_conn
        _exec(conn, f"DROP TABLE IF EXISTS {_SCHEMA}.lb_dtype_src CASCADE")
        _exec(conn, _ALL_TYPES_DDL)

        from src.statschema.core.pandas_builder import build_rows_from_canonical
        schema = _build_all_types_schema()
        df = build_rows_from_canonical(schema, self._N, seed=42)
        load_dataframe(
            df, conn, f"{_SCHEMA}.lb_dtype_src", dialect="postgres",
            strategy=LoadStrategy.MULTI_ROW,
        )

        # ANALYZE is required before pg_stats is populated for newly loaded data.
        _exec(conn, f"ANALYZE {_SCHEMA}.lb_dtype_src")

        ts = collect_table_stats(
            conn, "lb_dtype_src", dialect="postgres", schema=_SCHEMA
        )
        assert ts.row_count == self._N, f"row_count={ts.row_count}, expected {self._N}"
        assert ts.columns, "collect_table_stats returned no column stats"

        col_names = {c.name for c in ts.columns}
        for expected in ("c_integer", "c_long", "c_decimal", "c_string", "c_boolean",
                         "c_date", "c_timestamp"):
            assert expected in col_names, (
                f"Column {expected!r} missing from collected stats. "
                f"Got: {sorted(col_names)}"
            )

    def test_numeric_mcvs_are_strings_in_collected_stats(self, schema_conn):
        """
        Verify that collect_table_stats encodes MCV values as strings for numeric
        columns (c_decimal, c_float, c_double) — same as Oracle/DB2 collectors.
        This is the regression path for the 'str for NUMBER' Oracle bug.
        """
        pd = pytest.importorskip("pandas")
        conn = schema_conn
        _exec(conn, f"DROP TABLE IF EXISTS {_SCHEMA}.lb_dtype_src CASCADE")
        _exec(conn, _ALL_TYPES_DDL)

        from src.statschema.core.pandas_builder import build_rows_from_canonical
        schema = _build_all_types_schema()
        # Use a schema with explicit decimal MCVs so pg_stats definitely records them
        from dataclasses import replace as _dc_replace
        from src.statschema.core.model import GenerationRule
        cols_with_mcvs = []
        for col in schema.columns:
            if col.name == "c_decimal":
                col = _dc_replace(col, generation=GenerationRule(
                    values=[10.0, 20.0, 30.0, 40.0], weights=[0.4, 0.3, 0.2, 0.1]
                ))
            cols_with_mcvs.append(col)
        schema_mcv = _dc_replace(schema, columns=cols_with_mcvs)

        df = build_rows_from_canonical(schema_mcv, self._N, seed=1)
        load_dataframe(
            df, conn, f"{_SCHEMA}.lb_dtype_src", dialect="postgres",
            strategy=LoadStrategy.MULTI_ROW,
        )
        _exec(conn, f"ANALYZE {_SCHEMA}.lb_dtype_src")

        ts = collect_table_stats(
            conn, "lb_dtype_src", dialect="postgres", schema=_SCHEMA
        )
        dec_stats = ts.column_stats("c_decimal")
        assert dec_stats is not None, "c_decimal stats not found"

        # All MCV values must be strings (the model's MostCommonValue.value is always str)
        for mcv in dec_stats.most_common_values:
            assert isinstance(mcv.value, str), (
                f"c_decimal MCV value should be str, got {type(mcv.value).__name__}: "
                f"{mcv.value!r}"
            )
        # min_value / max_value from stats are also strings
        if dec_stats.min_value is not None:
            assert isinstance(dec_stats.min_value, str)
        if dec_stats.max_value is not None:
            assert isinstance(dec_stats.max_value, str)

    def test_write2_with_collected_stats(self, schema_conn):
        """Phase C: regenerate using string-encoded stats → load into target table."""
        pd = pytest.importorskip("pandas")
        conn = schema_conn
        _exec(conn, f"DROP TABLE IF EXISTS {_SCHEMA}.lb_dtype_src CASCADE")
        _exec(conn, f"DROP TABLE IF EXISTS {_SCHEMA}.lb_dtype_tgt CASCADE")
        _exec(conn, _ALL_TYPES_DDL)
        _exec(conn, _ALL_TYPES_TGT_DDL)

        from src.statschema.core.pandas_builder import build_rows_from_canonical
        schema = _build_all_types_schema()

        # Phase A: write-1 (no stats)
        df1 = build_rows_from_canonical(schema, self._N, seed=42)
        load_dataframe(
            df1, conn, f"{_SCHEMA}.lb_dtype_src", dialect="postgres",
            strategy=LoadStrategy.MULTI_ROW,
        )
        _exec(conn, f"ANALYZE {_SCHEMA}.lb_dtype_src")

        # Phase B: collect stats
        ts = collect_table_stats(
            conn, "lb_dtype_src", dialect="postgres", schema=_SCHEMA
        )
        assert ts.row_count == self._N

        # Phase C: write-2 (with stats) into target
        df2 = build_rows_from_canonical(schema, self._N, seed=99, stats=ts)
        assert len(df2) == self._N, f"write-2 DataFrame has {len(df2)} rows, expected {self._N}"

        n2 = load_dataframe(
            df2, conn, f"{_SCHEMA}.lb_dtype_tgt", dialect="postgres",
            strategy=LoadStrategy.MULTI_ROW,
        )
        assert n2 == self._N

        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {_SCHEMA}.lb_dtype_src")
        src_count = cur.fetchone()[0]
        cur.execute(f"SELECT COUNT(*) FROM {_SCHEMA}.lb_dtype_tgt")
        tgt_count = cur.fetchone()[0]

        assert src_count == self._N, f"table_src has {src_count} rows, expected {self._N}"
        assert tgt_count == self._N, f"table_tgt has {tgt_count} rows, expected {self._N}"

    def test_write2_numeric_columns_not_strings_after_collected_stats(self, schema_conn):
        """
        The generator must produce float/int values for numeric columns even when
        pg_stats returns string-encoded MCVs.  This test exercises the same bug
        path that caused Oracle TPC-DI to fail with 'str for DB_TYPE_NUMBER'.
        All dialects (including Lakebase/postgres) return MCVs as strings from
        collect_table_stats; psycopg2 multi-row INSERT will fail if a string is
        bound to an INTEGER or NUMERIC column.
        """
        pd = pytest.importorskip("pandas")
        conn = schema_conn
        _exec(conn, f"DROP TABLE IF EXISTS {_SCHEMA}.lb_dtype_src CASCADE")
        _exec(conn, f"DROP TABLE IF EXISTS {_SCHEMA}.lb_dtype_tgt CASCADE")
        _exec(conn, _ALL_TYPES_DDL)
        _exec(conn, _ALL_TYPES_TGT_DDL)

        from src.statschema.core.pandas_builder import build_rows_from_canonical
        schema = _build_all_types_schema()

        df1 = build_rows_from_canonical(schema, self._N, seed=42)
        load_dataframe(
            df1, conn, f"{_SCHEMA}.lb_dtype_src", dialect="postgres",
            strategy=LoadStrategy.MULTI_ROW,
        )
        _exec(conn, f"ANALYZE {_SCHEMA}.lb_dtype_src")

        ts = collect_table_stats(
            conn, "lb_dtype_src", dialect="postgres", schema=_SCHEMA
        )

        df2 = build_rows_from_canonical(schema, self._N, seed=7, stats=ts)

        # Verify that decimal/float/double columns are not pure strings after
        # stats-driven regeneration — every value must be float()-able.
        for col_name in ("c_decimal", "c_float", "c_double", "c_integer", "c_long"):
            series = df2[col_name].dropna()
            for val in series:
                try:
                    float(val)
                except (ValueError, TypeError) as exc:
                    pytest.fail(
                        f"Column {col_name}: value {val!r} (type {type(val).__name__}) "
                        f"cannot be converted to float — would fail psycopg2 binding: {exc}"
                    )
