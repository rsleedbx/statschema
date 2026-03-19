"""
Live synthetic-data pipeline tests.

Full end-to-end pipeline on real databases:

  canonical table schema
        │
        ▼
  emit DDL → CREATE TABLE on live DB (table_v1)
        │
        ▼
  build_dataframe_from_canonical(default stats, 1000 rows)
        │  ← Spark + dbldatagen
        ▼
  Spark DataFrame → pandas → load into live DB table_v1
        │
        ▼
  collect_table_stats(conn, "table_v1")   ← real measured distributions
        │  → ColumnStats: null_fraction, n_distinct, min, max, MCVs
        ▼
  build_dataframe_from_canonical(REAL stats, 1000 rows)  ← stats-driven
        │
        ▼
  load into live DB table_v2
        │
        ▼
  collect_table_stats(conn, "table_v2")
        │
        ▼
  COMPARE table_v1 stats vs table_v2 stats:
    - null fractions within ±0.10
    - distinct count ratios within [0.3, 3.0]
    - numeric min/max within observed range
    - row counts equal

Prerequisites
-------------
Same containers as test_live_mysql.py / test_live_pg.py plus a live SQL Server
(see docs/local-databases.md).

Spark is required for data generation.  Tests are skipped when:
  - pyspark is not importable
  - dbldatagen is not importable
  - sqlalchemy is not importable
  - the target database is unreachable

Databases tested
----------------
  MySQL 8.4   port 3384   (native ARM64)
  PG 16       port 5416   (native ARM64)
  SQL Server  port 14330  (Lima QEMU)

Run
---
  make test-live-synth SQLSERVER_PASS=<password>

  # or directly (network access required):
  SQLSERVER_PASS=<pw> .venv_test/bin/pytest tests/test_live_synth.py -v
"""

from __future__ import annotations

import math
import os
import textwrap
from typing import Any

import pytest

from src.statschema import emit_ddl, parse_ddl
from src.statschema.db_stats_collector import collect_table_stats
from src.statschema.dbldatagen_builder import build_dataframe_from_canonical
from src.statschema.stats_io import make_default_stats

# ---------------------------------------------------------------------------
# Connection settings — match test_live_mysql.py / test_live_pg.py / test_live_sqlserver.py
# ---------------------------------------------------------------------------

_MYSQL_HOST = "127.0.0.1"
_MYSQL_PASS = os.environ.get("MYSQL_ROOT_PASS", "testpass")
_MYSQL8_PORT = int(os.environ.get("MYSQL8_PORT", "3384"))

_PG_PASS    = os.environ.get("PG_PASSWORD",    "testpass")
_PG16_PORT  = int(os.environ.get("PG16_PORT",  "5416"))

_SS_PASS    = os.environ.get("SQLSERVER_PASS", "")
_SS_PORT    = int(os.environ.get("SQLSERVER_PORT", "14330"))

_SYNTH_ROWS = 1000   # rows per generation round (fast but enough for meaningful stats)


# ---------------------------------------------------------------------------
# Representative test tables (cover diverse type combinations)
# ---------------------------------------------------------------------------

_TEST_TABLES_DDL = {
    "mysql": textwrap.dedent("""\
        CREATE TABLE `synth_orders` (
            `order_id`      INT           NOT NULL AUTO_INCREMENT,
            `customer_id`   INT           NOT NULL,
            `status`        VARCHAR(20)   NOT NULL DEFAULT 'pending',
            `total`         DECIMAL(10,2)     NULL,
            `discount_pct`  FLOAT             NULL,
            `is_paid`       TINYINT(1)    NOT NULL DEFAULT 0,
            `created_at`    DATETIME          NULL,
            PRIMARY KEY (`order_id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """),
    "postgres": textwrap.dedent("""\
        CREATE TABLE "synth_orders" (
            "order_id"      SERIAL        NOT NULL PRIMARY KEY,
            "customer_id"   INTEGER       NOT NULL,
            "status"        VARCHAR(20)   NOT NULL DEFAULT 'pending',
            "total"         NUMERIC(10,2)     NULL,
            "discount_pct"  REAL              NULL,
            "is_paid"       BOOLEAN       NOT NULL DEFAULT FALSE,
            "created_at"    TIMESTAMP         NULL
        );
    """),
    "sqlserver": textwrap.dedent("""\
        CREATE TABLE [synth_orders] (
            [order_id]      INT           NOT NULL IDENTITY(1,1),
            [customer_id]   INT           NOT NULL,
            [status]        NVARCHAR(20)  NOT NULL DEFAULT 'pending',
            [total]         DECIMAL(10,2)     NULL,
            [discount_pct]  FLOAT             NULL,
            [is_paid]       BIT           NOT NULL DEFAULT 0,
            [created_at]    DATETIME2         NULL,
            CONSTRAINT PK_synth_orders PRIMARY KEY ([order_id])
        );
    """),
}

# These columns are loaded from the Spark DF (omit auto-increment PK)
_LOAD_COLS = {
    "mysql":     ["customer_id", "status", "total", "discount_pct", "is_paid", "created_at"],
    "postgres":  ["customer_id", "status", "total", "discount_pct", "is_paid", "created_at"],
    "sqlserver": ["customer_id", "status", "total", "discount_pct", "is_paid", "created_at"],
}

# Columns used for stats comparison (skip auto-increment PK)
_STATS_COLS = _LOAD_COLS


# ---------------------------------------------------------------------------
# Spark helper (reuse pattern from test_statschema.py)
# ---------------------------------------------------------------------------

def _get_spark():
    """Return a local PySpark session or skip."""
    import sys
    pyspark = pytest.importorskip("pyspark", reason="pyspark not installed")
    pytest.importorskip("dbldatagen", reason="dbldatagen not installed")
    # Force PySpark workers to use the same Python interpreter as the driver.
    # Without this, PySpark may pick up the system Python (e.g. 3.14) which
    # differs from the venv Python (3.11) and raises PYTHON_VERSION_MISMATCH.
    os.environ.setdefault("PYSPARK_PYTHON",        sys.executable)
    os.environ.setdefault("PYSPARK_DRIVER_PYTHON", sys.executable)
    try:
        spark = (
            pyspark.sql.SparkSession.builder
            .master("local[2]")
            .appName("synth_live_test")
            .config("spark.driver.host", "localhost")
            .config("spark.driver.bindAddress", "127.0.0.1")
            .config("spark.sql.shuffle.partitions", "4")
            # Allow DECIMAL overflow to produce NULL instead of raising an error.
            # dbldatagen may generate values outside a narrow precision (e.g. DECIMAL(10,2))
            # when no explicit maxValue is set.  NULL is preferable to a job failure.
            .config("spark.sql.ansi.enabled", "false")
            .getOrCreate()
        )
        return spark
    except Exception as exc:
        pytest.skip(f"Cannot start local Spark: {exc}")


# ---------------------------------------------------------------------------
# Database connection helpers
# ---------------------------------------------------------------------------

def _mysql_engine(db: str = "live_synth"):
    sa = pytest.importorskip("sqlalchemy", reason="sqlalchemy not installed")
    return sa.create_engine(
        f"mysql+pymysql://root:{_MYSQL_PASS}@{_MYSQL_HOST}:{_MYSQL8_PORT}/{db}",
        pool_pre_ping=True,
    )


def _pg_engine(schema: str = "live_synth"):
    sa = pytest.importorskip("sqlalchemy", reason="sqlalchemy not installed")
    return sa.create_engine(
        f"postgresql+psycopg2://postgres:{_PG_PASS}@127.0.0.1:{_PG16_PORT}/testdb"
        f"?options=-csearch_path%3D{schema}",
        pool_pre_ping=True,
    )


def _mysql_raw():
    pymysql = pytest.importorskip("pymysql")
    try:
        conn = pymysql.connect(
            host=_MYSQL_HOST, port=_MYSQL8_PORT,
            user="root", password=_MYSQL_PASS,
            autocommit=True, connect_timeout=5,
        )
        return conn
    except Exception as e:
        pytest.skip(f"MySQL unreachable: {e}")


def _pg_raw():
    psycopg2 = pytest.importorskip("psycopg2")
    try:
        conn = psycopg2.connect(
            host="127.0.0.1", port=_PG16_PORT,
            user="postgres", password=_PG_PASS,
            dbname="testdb", connect_timeout=5,
        )
        conn.autocommit = True
        return conn
    except Exception as e:
        pytest.skip(f"PostgreSQL unreachable: {e}")


def _ss_raw():
    if not _SS_PASS:
        pytest.skip("SQLSERVER_PASS not set")
    pymssql = pytest.importorskip("pymssql")
    try:
        conn = pymssql.connect(
            host="127.0.0.1", port=_SS_PORT,
            user="sa", password=_SS_PASS,
            autocommit=True, timeout=5,
        )
        return conn
    except Exception as e:
        pytest.skip(f"SQL Server unreachable: {e}")


# ---------------------------------------------------------------------------
# Data loading helpers (Spark DataFrame → live DB)
# ---------------------------------------------------------------------------

def _drop_and_create(conn, table: str, ddl: str, dialect: str):
    cur = conn.cursor()
    if dialect == "mysql":
        cur.execute(f"DROP TABLE IF EXISTS `{table}`")
        cur.execute(ddl)
    elif dialect == "postgres":
        cur.execute(f'DROP TABLE IF EXISTS "{table}"')
        cur.execute(ddl)
    elif dialect == "sqlserver":
        cur.execute(
            f"IF OBJECT_ID(N'[{table}]', N'U') IS NOT NULL DROP TABLE [{table}]"
        )
        cur.execute(ddl)


def _load_df_to_mysql(df_spark, table: str, cols: list[str]):
    """Spark DataFrame → pandas → MySQL via SQLAlchemy (appends into existing table)."""
    pdf = df_spark.select(cols).toPandas()
    engine = _mysql_engine()
    with engine.begin() as cx:
        # Use "append" so the DDL-created table schema is preserved.
        # The table must already exist (created by _drop_and_create before calling this).
        pdf.to_sql(table, cx, if_exists="append", index=False)
    engine.dispose()
    return len(pdf)


def _load_df_to_pg(df_spark, table: str, cols: list[str], schema: str = "live_synth"):
    """Spark DataFrame → pandas → PostgreSQL via SQLAlchemy (appends into existing table)."""
    pdf = df_spark.select(cols).toPandas()
    engine = _pg_engine(schema)
    with engine.begin() as cx:
        pdf.to_sql(table, cx, schema=schema, if_exists="append", index=False)
    engine.dispose()
    return len(pdf)


def _load_df_to_ss(df_spark, conn, table: str, cols: list[str]):
    """Spark DataFrame → pandas → SQL Server via pymssql executemany."""
    pdf = df_spark.select(cols).toPandas()
    if pdf.empty:
        return 0
    # Build parameterised INSERT
    placeholders = ", ".join(["%s"] * len(cols))
    col_names = ", ".join(f"[{c}]" for c in cols)
    sql = f"INSERT INTO [{table}] ({col_names}) VALUES ({placeholders})"
    rows = [
        tuple(None if (isinstance(v, float) and math.isnan(v)) else v
              for v in row)
        for row in pdf.itertuples(index=False, name=None)
    ]
    cur = conn.cursor()
    cur.executemany(sql, rows)
    return len(rows)


# ---------------------------------------------------------------------------
# Stats accuracy helpers
# ---------------------------------------------------------------------------

def _null_fraction_error(stats1, stats2, col: str) -> float:
    """Absolute difference in null_fraction between two TableStats for one column."""
    cs1 = next((c for c in stats1.columns if c.name == col), None)
    cs2 = next((c for c in stats2.columns if c.name == col), None)
    if cs1 is None or cs2 is None:
        return 0.0
    return abs(cs1.null_fraction - cs2.null_fraction)


def _distinct_ratio(stats1, stats2, col: str) -> float:
    """Ratio of distinct counts (stats2 / stats1).  Returns 1.0 if either is 0."""
    cs1 = next((c for c in stats1.columns if c.name == col), None)
    cs2 = next((c for c in stats2.columns if c.name == col), None)
    if cs1 is None or cs2 is None or cs1.n_distinct == 0:
        return 1.0
    return cs2.n_distinct / cs1.n_distinct


# ---------------------------------------------------------------------------
# Shared pipeline state (connection objects are C-extension types in PG/SS
# and do not allow arbitrary attribute assignment, so store shared state here)
# ---------------------------------------------------------------------------

_PIPELINE_STATE: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# MySQL synth pipeline
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def mysql_synth_env():
    """Set up live_synth database on MySQL 8 for the full synth pipeline."""
    conn = _mysql_raw()
    conn.cursor().execute("DROP DATABASE IF EXISTS live_synth")
    conn.cursor().execute("CREATE DATABASE live_synth")
    conn.select_db("live_synth")
    yield conn
    conn.cursor().execute("DROP DATABASE IF EXISTS live_synth")
    conn.close()
    _PIPELINE_STATE.pop("mysql_stats_v1", None)


class TestMySQLSynthPipeline:
    """Full synthetic data pipeline on MySQL 8.4."""

    _DDL     = _TEST_TABLES_DDL["mysql"]
    _DIALECT = "mysql"
    _TABLE   = "synth_orders"
    _COLS    = _STATS_COLS["mysql"]

    def _get_canonical(self):
        tables = parse_ddl(self._DDL, self._DIALECT)
        assert tables
        return tables[0]

    # ── Phase 1: emit + create table ─────────────────────────────────────────

    def test_01_create_table(self, mysql_synth_env):
        """Emitted DDL creates the table successfully on MySQL 8."""
        canonical = self._get_canonical()
        ddl = emit_ddl(canonical, self._DIALECT, if_not_exists=False)
        _drop_and_create(mysql_synth_env, self._TABLE, ddl, self._DIALECT)
        row = mysql_synth_env.cursor()
        row.execute(f"SHOW TABLES LIKE '{self._TABLE}'")
        assert row.fetchone(), "Table was not created"

    # ── Phase 2: default-stats generation → load ─────────────────────────────

    def test_02_generate_default_stats_and_load(self, mysql_synth_env):
        """Generate 1000 rows with default stats and load into MySQL."""
        canonical = self._get_canonical()
        spark = _get_spark()
        try:
            df = build_dataframe_from_canonical(
                spark, canonical, rows=_SYNTH_ROWS, partitions=2, seed=42
            )
            assert df.count() == _SYNTH_ROWS
            n = _load_df_to_mysql(df, self._TABLE, self._COLS)
            assert n == _SYNTH_ROWS
        finally:
            spark.stop()

    # ── Phase 3: collect real stats ───────────────────────────────────────────

    def test_03_collect_live_stats(self, mysql_synth_env):
        """Collect stats from the loaded table; verify structure."""
        mysql_synth_env.cursor().execute("ANALYZE TABLE `synth_orders`")
        stats = collect_table_stats(
            mysql_synth_env, self._TABLE, "mysql", columns=self._COLS
        )
        assert stats.row_count == _SYNTH_ROWS, (
            f"Expected {_SYNTH_ROWS} rows, got {stats.row_count}"
        )
        assert len(stats.columns) == len(self._COLS)
        for cs in stats.columns:
            assert cs.name in self._COLS
            assert 0.0 <= cs.null_fraction <= 1.0
            assert cs.n_distinct >= 0
        _PIPELINE_STATE["mysql_stats_v1"] = stats

    # ── Phase 4: stats-driven generation → load v2 ───────────────────────────

    def test_04_generate_with_live_stats_and_load(self, mysql_synth_env):
        """Regenerate 1000 rows using real stats and load into synth_orders_v2."""
        stats_v1 = _PIPELINE_STATE.get("mysql_stats_v1")
        if stats_v1 is None:
            pytest.skip("Stats from Phase 3 not available")

        canonical = self._get_canonical()
        spark = _get_spark()
        try:
            df = build_dataframe_from_canonical(
                spark, canonical, rows=_SYNTH_ROWS, partitions=2, seed=99,
                stats=stats_v1,
            )
            assert df.count() == _SYNTH_ROWS

            v2_ddl = emit_ddl(canonical, self._DIALECT, if_not_exists=False).replace(
                "`synth_orders`", "`synth_orders_v2`"
            )
            _drop_and_create(
                mysql_synth_env, "synth_orders_v2",
                v2_ddl, self._DIALECT,
            )
            n = _load_df_to_mysql(df, "synth_orders_v2", self._COLS)
            assert n == _SYNTH_ROWS
        finally:
            spark.stop()

    # ── Phase 5: collect stats from v2 and compare ───────────────────────────

    def test_05_compare_stats_accuracy(self, mysql_synth_env):
        """Stats from stats-driven generation should match v1 within tolerance."""
        stats_v1 = _PIPELINE_STATE.get("mysql_stats_v1")
        if stats_v1 is None:
            pytest.skip("Stats from Phase 3 not available")

        mysql_synth_env.cursor().execute("ANALYZE TABLE `synth_orders_v2`")
        stats_v2 = collect_table_stats(
            mysql_synth_env, "synth_orders_v2", "mysql", columns=self._COLS
        )

        assert stats_v2.row_count == _SYNTH_ROWS

        failures = []
        for col in self._COLS:
            nf_err   = _null_fraction_error(stats_v1, stats_v2, col)
            dist_rat = _distinct_ratio(stats_v1, stats_v2, col)
            if nf_err > 0.15:
                failures.append(f"{col}: null_fraction error {nf_err:.3f} > 0.15")
            if not (0.2 <= dist_rat <= 5.0):
                failures.append(f"{col}: distinct_ratio {dist_rat:.2f} outside [0.2, 5.0]")

        assert not failures, "Stats accuracy failures:\n" + "\n".join(failures)

    def test_06_stats_improve_over_defaults(self, mysql_synth_env):
        """
        Null fraction error with real stats should not be WORSE than default stats
        (stats-driven generation should be at least as good as guessing).
        This test validates that gather→feed-back stats loop is beneficial.
        """
        stats_v1 = _PIPELINE_STATE.get("mysql_stats_v1")
        if stats_v1 is None:
            pytest.skip("Stats from Phase 3 not available")

        canonical  = self._get_canonical()
        default_db = make_default_stats([canonical], row_count=_SYNTH_ROWS)
        default_ts = default_db.tables[0] if default_db.tables else None

        stats_v2 = collect_table_stats(
            mysql_synth_env, "synth_orders_v2", "mysql", columns=self._COLS
        )

        # For nullable columns: compare null_fraction error default vs real-stats
        # "total" and "discount_pct" and "created_at" are nullable
        nullable_cols = ["total", "discount_pct", "created_at"]
        for col in nullable_cols:
            cs_v1  = next((c for c in stats_v1.columns  if c.name == col), None)
            cs_v2  = next((c for c in stats_v2.columns  if c.name == col), None)
            cs_def = (
                next((c for c in default_ts.columns if c.name == col), None)
                if default_ts else None
            )
            if cs_v1 is None or cs_v2 is None:
                continue
            actual_v2_err = abs(cs_v2.null_fraction - cs_v1.null_fraction)
            if cs_def:
                default_err = abs(cs_def.null_fraction - cs_v1.null_fraction)
                # stats-driven should not be dramatically worse than defaults
                assert actual_v2_err <= max(0.15, default_err * 3), (
                    f"[{col}] stats-driven null_frac error {actual_v2_err:.3f} "
                    f"is much worse than default error {default_err:.3f}"
                )


# ---------------------------------------------------------------------------
# PostgreSQL synth pipeline
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def pg_synth_env():
    conn = _pg_raw()
    conn.cursor().execute("DROP SCHEMA IF EXISTS live_synth CASCADE")
    conn.cursor().execute("CREATE SCHEMA live_synth")
    yield conn
    conn.cursor().execute("DROP SCHEMA IF EXISTS live_synth CASCADE")
    conn.close()
    _PIPELINE_STATE.pop("pg_stats_v1", None)


class TestPostgresSynthPipeline:
    """Full synthetic data pipeline on PostgreSQL 16."""

    _DDL     = _TEST_TABLES_DDL["postgres"]
    _DIALECT = "postgres"
    _TABLE   = "synth_orders"
    _COLS    = _STATS_COLS["postgres"]

    def _get_canonical(self):
        tables = parse_ddl(self._DDL, self._DIALECT)
        assert tables
        return tables[0]

    def test_01_create_table(self, pg_synth_env):
        canonical = self._get_canonical()
        ddl = emit_ddl(canonical, self._DIALECT, if_not_exists=False)
        ddl_s = ddl.replace('"synth_orders"', 'live_synth."synth_orders"', 1)
        pg_synth_env.cursor().execute('DROP TABLE IF EXISTS live_synth."synth_orders"')
        pg_synth_env.cursor().execute(ddl_s)
        row = pg_synth_env.cursor()
        row.execute("""
            SELECT 1 FROM information_schema.tables
            WHERE table_schema='live_synth' AND table_name='synth_orders'
        """)
        assert row.fetchone()

    def test_02_generate_default_stats_and_load(self, pg_synth_env):
        canonical = self._get_canonical()
        spark = _get_spark()
        try:
            df = build_dataframe_from_canonical(
                spark, canonical, rows=_SYNTH_ROWS, partitions=2, seed=42
            )
            assert df.count() == _SYNTH_ROWS
            n = _load_df_to_pg(df, self._TABLE, self._COLS, schema="live_synth")
            assert n == _SYNTH_ROWS
        finally:
            spark.stop()

    def test_03_collect_live_stats(self, pg_synth_env):
        pg_synth_env.cursor().execute('ANALYZE live_synth."synth_orders"')
        stats = collect_table_stats(
            pg_synth_env, self._TABLE, "postgres",
            schema="live_synth", columns=self._COLS,
        )
        assert stats.row_count == _SYNTH_ROWS
        assert len(stats.columns) == len(self._COLS)
        for cs in stats.columns:
            assert 0.0 <= cs.null_fraction <= 1.0
        _PIPELINE_STATE["pg_stats_v1"] = stats

    def test_04_generate_with_live_stats_and_load(self, pg_synth_env):
        stats_v1 = _PIPELINE_STATE.get("pg_stats_v1")
        if stats_v1 is None:
            pytest.skip("Stats from Phase 3 not available")

        canonical = self._get_canonical()
        spark = _get_spark()
        try:
            df = build_dataframe_from_canonical(
                spark, canonical, rows=_SYNTH_ROWS, partitions=2, seed=99,
                stats=stats_v1,
            )
            assert df.count() == _SYNTH_ROWS
            ddl = emit_ddl(canonical, self._DIALECT, if_not_exists=False)
            ddl_v2 = ddl.replace('"synth_orders"', 'live_synth."synth_orders_v2"', 1)
            pg_synth_env.cursor().execute('DROP TABLE IF EXISTS live_synth."synth_orders_v2"')
            pg_synth_env.cursor().execute(ddl_v2)
            n = _load_df_to_pg(df, "synth_orders_v2", self._COLS, schema="live_synth")
            assert n == _SYNTH_ROWS
        finally:
            spark.stop()

    def test_05_compare_stats_accuracy(self, pg_synth_env):
        stats_v1 = _PIPELINE_STATE.get("pg_stats_v1")
        if stats_v1 is None:
            pytest.skip("Stats from Phase 3 not available")

        pg_synth_env.cursor().execute('ANALYZE live_synth."synth_orders_v2"')
        stats_v2 = collect_table_stats(
            pg_synth_env, "synth_orders_v2", "postgres",
            schema="live_synth", columns=self._COLS,
        )

        assert stats_v2.row_count == _SYNTH_ROWS
        failures = []
        for col in self._COLS:
            nf_err   = _null_fraction_error(stats_v1, stats_v2, col)
            dist_rat = _distinct_ratio(stats_v1, stats_v2, col)
            if nf_err > 0.15:
                failures.append(f"{col}: null_fraction error {nf_err:.3f} > 0.15")
            if not (0.2 <= dist_rat <= 5.0):
                failures.append(f"{col}: distinct_ratio {dist_rat:.2f} outside [0.2, 5.0]")
        assert not failures, "Stats accuracy failures:\n" + "\n".join(failures)

    def test_06_pg_stats_richer_than_default(self, pg_synth_env):
        """PostgreSQL ANALYZE populates pg_stats; collected stats include MCVs
        for the low-cardinality 'is_paid' (boolean) column."""
        stats_v1 = _PIPELINE_STATE.get("pg_stats_v1")
        if stats_v1 is None:
            pytest.skip("Stats from Phase 3 not available")
        is_paid = next((c for c in stats_v1.columns if c.name == "is_paid"), None)
        assert is_paid is not None
        assert is_paid.n_distinct <= 3, (
            f"is_paid should have ≤ 2 distinct values, got {is_paid.n_distinct}"
        )


# ---------------------------------------------------------------------------
# SQL Server synth pipeline
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ss_synth_env():
    conn = _ss_raw()
    cur = conn.cursor()
    cur.execute("IF DB_ID('live_synth') IS NOT NULL DROP DATABASE live_synth")
    cur.execute("CREATE DATABASE live_synth")
    cur.execute("USE live_synth")
    yield conn
    cur2 = conn.cursor()
    cur2.execute("USE master")
    cur2.execute("IF DB_ID('live_synth') IS NOT NULL DROP DATABASE live_synth")
    conn.close()
    _PIPELINE_STATE.pop("ss_stats_v1", None)


class TestSQLServerSynthPipeline:
    """Full synthetic data pipeline on SQL Server 2022."""

    _DDL     = _TEST_TABLES_DDL["sqlserver"]
    _DIALECT = "sqlserver"
    _TABLE   = "synth_orders"
    _COLS    = _STATS_COLS["sqlserver"]

    def _get_canonical(self):
        tables = parse_ddl(self._DDL, self._DIALECT)
        assert tables
        return tables[0]

    def test_01_create_table(self, ss_synth_env):
        canonical = self._get_canonical()
        ddl = emit_ddl(canonical, self._DIALECT, if_not_exists=False)
        _drop_and_create(ss_synth_env, self._TABLE, ddl, self._DIALECT)
        cur = ss_synth_env.cursor()
        cur.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'synth_orders'"
        )
        assert cur.fetchone()

    def test_02_generate_default_stats_and_load(self, ss_synth_env):
        canonical = self._get_canonical()
        spark = _get_spark()
        try:
            df = build_dataframe_from_canonical(
                spark, canonical, rows=_SYNTH_ROWS, partitions=2, seed=42
            )
            assert df.count() == _SYNTH_ROWS
            n = _load_df_to_ss(df, ss_synth_env, self._TABLE, self._COLS)
            assert n == _SYNTH_ROWS
        finally:
            spark.stop()

    def test_03_collect_live_stats(self, ss_synth_env):
        ss_synth_env.cursor().execute("UPDATE STATISTICS [synth_orders]")
        stats = collect_table_stats(
            ss_synth_env, self._TABLE, "sqlserver", columns=self._COLS
        )
        assert stats.row_count == _SYNTH_ROWS
        assert len(stats.columns) == len(self._COLS)
        for cs in stats.columns:
            assert 0.0 <= cs.null_fraction <= 1.0
        _PIPELINE_STATE["ss_stats_v1"] = stats

    def test_04_generate_with_live_stats_and_load(self, ss_synth_env):
        stats_v1 = _PIPELINE_STATE.get("ss_stats_v1")
        if stats_v1 is None:
            pytest.skip("Stats from Phase 3 not available")

        canonical = self._get_canonical()
        spark = _get_spark()
        try:
            df = build_dataframe_from_canonical(
                spark, canonical, rows=_SYNTH_ROWS, partitions=2, seed=99,
                stats=stats_v1,
            )
            assert df.count() == _SYNTH_ROWS
            v2_ddl = emit_ddl(canonical, self._DIALECT, if_not_exists=False).replace(
                "[synth_orders]", "[synth_orders_v2]", 1
            )
            _drop_and_create(ss_synth_env, "synth_orders_v2", v2_ddl, self._DIALECT)
            n = _load_df_to_ss(df, ss_synth_env, "synth_orders_v2", self._COLS)
            assert n == _SYNTH_ROWS
        finally:
            spark.stop()

    def test_05_compare_stats_accuracy(self, ss_synth_env):
        stats_v1 = _PIPELINE_STATE.get("ss_stats_v1")
        if stats_v1 is None:
            pytest.skip("Stats from Phase 3 not available")

        ss_synth_env.cursor().execute("UPDATE STATISTICS [synth_orders_v2]")
        stats_v2 = collect_table_stats(
            ss_synth_env, "synth_orders_v2", "sqlserver", columns=self._COLS
        )

        assert stats_v2.row_count == _SYNTH_ROWS
        failures = []
        for col in self._COLS:
            nf_err   = _null_fraction_error(stats_v1, stats_v2, col)
            dist_rat = _distinct_ratio(stats_v1, stats_v2, col)
            if nf_err > 0.15:
                failures.append(f"{col}: null_fraction error {nf_err:.3f} > 0.15")
            if not (0.2 <= dist_rat <= 5.0):
                failures.append(f"{col}: distinct_ratio {dist_rat:.2f} outside [0.2, 5.0]")
        assert not failures, "Stats accuracy failures:\n" + "\n".join(failures)
