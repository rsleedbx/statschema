"""
Live IBM Db2 CE integration tests – runs against Db2 Community Edition 11.5 inside a Lima VM.

Tests verify that DDL emitted by emit_ddl(dialect="db2") executes without errors on a real
Db2 instance and that types, nullability, and constraints survive the parse → emit → execute →
introspect cycle.  Also confirms that Db2 dialect aliases and schema_source tags resolve
correctly through the dialect registry.

Prerequisites
-------------
IBM Db2 CE running inside a Lima x86_64 VM (see docs/local-databases.md):

    limactl start --name=db2 config/lima/db2.yaml
    # First boot takes 5-10 min. Wait for: "Setup has completed"
    # Watch: limactl shell db2 -- podman logs -f db2ce

Environment variables (defaults match the Lima config):

    DB2_HOST     default: 127.0.0.1
    DB2_PORT     default: 50000
    DB2_USER     default: db2inst1
    DB2_PASS     default: testpass
    DB2_DATABASE default: statschema

Skip behaviour
--------------
All tests are automatically skipped when:
- ibm_db is not installed, OR
- the connection to Db2 fails (e.g. VM not running)

Known type normalizations (Db2 CE 11.5)
-----------------------------------------
Db2 differs from other SQL databases in several ways relevant to the canonical model:

- No AUTO_INCREMENT: identity columns use ``GENERATED ALWAYS AS IDENTITY``.
- No IF NOT EXISTS before Db2 11.1: emit_ddl uses IF NOT EXISTS (supported in 11.1+).
- CLOB: unbounded strings (canonical "string" with no length) emit as CLOB, not TEXT.
- BLOB: binary data; no VARBINARY type in standard Db2 — use BLOB(n) for fixed size.
- BOOLEAN: supported from Db2 11.1; stored as BOOLEAN in SYSCAT.COLUMNS.
- REAL vs FLOAT: canonical "float" emits as REAL (32-bit); "double" emits as DOUBLE (64-bit).
- CHAR(36): UUID stored as CHAR(36) since Db2 has no native UUID type.
- DATE: date only (unlike Oracle where DATE includes time component).
- TIME: no timezone-aware TIME type; ``timetz`` degrades to TIME.
- SYSCAT.COLUMNS: use for introspection rather than INFORMATION_SCHEMA; type names from
  TYPENAME column are uppercase (e.g. 'VARCHAR', 'INTEGER', 'CLOB', 'BOOLEAN').
- sqlglot: no Db2 dialect — parse_ddl(dialect="db2") uses ANSI SQL parsing.
- ibm_db: Python driver has ARM64 macOS wheels; no Lima requirement for the driver itself.
"""

import textwrap

import pytest

from benchmarks.bench_config import DEFAULT_CATALOG
from src.statschema import parse_ddl, emit_ddl, load_schema
from tests.live_helpers import _tp

# ---------------------------------------------------------------------------
# Connection config
# ---------------------------------------------------------------------------

_p = _tp("test_db2")

_HOST     = _p.host     or "127.0.0.1"
_PORT     = _p.port     or 50000
_USER     = _p.username or "db2inst1"
_PASS     = _p.password or "testpass"
_DATABASE = _p.database or DEFAULT_CATALOG
_SCHEMA   = _USER.upper()   # Db2 default schema is the connected user (uppercase)


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def _get_connection():
    ibm_db_dbi = pytest.importorskip("ibm_db_dbi")
    conn_str = (
        f"DATABASE={_DATABASE};HOSTNAME={_HOST};PORT={_PORT};"
        f"PROTOCOL=TCPIP;UID={_USER};PWD={_PASS};"
    )
    try:
        conn = ibm_db_dbi.connect(conn_str, "", "")
        return conn
    except Exception as exc:
        pytest.skip(f"Cannot connect to Db2 on {_HOST}:{_PORT}: {exc}")


def _execute(conn, sql: str):
    cur = conn.cursor()
    cur.execute(sql.rstrip(";").strip())
    conn.commit()
    return cur


def _execute_ignore(conn, sql: str, *sqlstates: str):
    """Execute SQL, silently swallowing specific SQLSTATE codes."""
    try:
        _execute(conn, sql)
    except Exception as exc:
        msg = str(exc)
        if any(state in msg for state in sqlstates):
            return
        raise


def _fetchall(conn, sql: str) -> list[tuple]:
    cur = conn.cursor()
    cur.execute(sql.rstrip(";").strip())
    return cur.fetchall()


def _introspect(conn, table: str) -> dict[str, dict]:
    """
    Return {col_name: {data_type, length, scale, nullable}} from SYSCAT.COLUMNS.
    Column names lowercased.  Type names are uppercase (e.g. 'VARCHAR', 'INTEGER').
    """
    rows = _fetchall(conn, f"""
        SELECT COLNAME, TYPENAME, LENGTH, SCALE, NULLS
        FROM SYSCAT.COLUMNS
        WHERE TABSCHEMA = '{_SCHEMA}'
          AND TABNAME   = '{table.upper()}'
        ORDER BY COLNO
    """)
    return {
        row[0].lower(): {
            "data_type": row[1],
            "length":    row[2],
            "scale":     row[3],
            "nullable":  row[4] == "Y",
        }
        for row in rows
    }


def _drop_table(conn, table: str):
    _execute_ignore(conn, f'DROP TABLE "{_SCHEMA}"."{table.upper()}"',
                    "42704",  # SQLSTATE for undefined object
                    "42S02")  # ODBC equivalent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def conn():
    """Module-scoped Db2 connection; auto-skips if ibm_db is absent or VM is down."""
    connection = _get_connection()
    yield connection
    connection.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(conn, ddl: str, table: str) -> dict[str, dict]:
    """Parse DDL → emit Db2 DDL → execute → introspect."""
    _drop_table(conn, table)
    tables = parse_ddl(ddl, dialect="db2")
    assert tables, f"parse_ddl returned no tables for: {ddl[:80]}"
    db2_ddl = emit_ddl(tables[0], dialect="db2", if_not_exists=False)
    _execute(conn, db2_ddl)
    return _introspect(conn, table)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDb2Connection:
    def test_version(self, conn):
        rows = _fetchall(conn, "SELECT SERVICE_LEVEL FROM SYSIBMADM.ENV_INST_INFO")
        version = rows[0][0]
        assert version, "Empty version string"
        assert "DB2" in version.upper(), f"Unexpected version string: {version!r}"


class TestDb2DialectAlias:
    """Dialect aliases must resolve to 'db2' through the registry."""

    def test_dialect_alias_normalize(self):
        from src.statschema import normalize_dialect
        assert normalize_dialect("db2")       == "db2"
        assert normalize_dialect("ibmdb2")    == "db2"
        assert normalize_dialect("ibm_db2")   == "db2"
        assert normalize_dialect("db2luw")    == "db2"
        assert normalize_dialect("dashdb")    == "db2"
        assert normalize_dialect("DB2LUW")    == "db2"

    def test_schema_source_db2_resolves(self):
        from src.statschema.loader import get_schema_source_from_data, SchemaSource
        assert get_schema_source_from_data({"schema_source": "db2"})    == SchemaSource.DB2
        assert get_schema_source_from_data({"schema_source": "ibmdb2"}) == SchemaSource.DB2
        assert get_schema_source_from_data({"schema_source": "db2luw"}) == SchemaSource.DB2

    def test_emit_ddl_dialect_alias(self, conn):
        """emit_ddl with dialect='ibmdb2' must produce valid Db2 DDL."""
        ddl = textwrap.dedent("""\
            CREATE TABLE "alias_test" (
                id   INTEGER NOT NULL,
                name VARCHAR(50)
            )
        """)
        tables = parse_ddl(ddl, dialect="db2")
        assert tables
        sql = emit_ddl(tables[0], dialect="ibmdb2", if_not_exists=False)
        _drop_table(conn, "alias_test")
        _execute(conn, sql)
        cols = _introspect(conn, "alias_test")
        assert "id"   in cols
        assert "name" in cols


class TestEmitAndExecute:
    """Parse canonical DDL → emit Db2 DDL → execute → introspect."""

    _SOURCE_DDL = textwrap.dedent("""\
        CREATE TABLE orders (
            order_id    INTEGER NOT NULL,
            customer_id INTEGER NOT NULL,
            total       DECIMAL(12,2),
            status      VARCHAR(50)  NOT NULL,
            notes       CLOB,
            created_at  TIMESTAMP
        )
    """)

    def test_emit_executes_without_error(self, conn):
        _drop_table(conn, "orders")
        tables = parse_ddl(self._SOURCE_DDL, dialect="db2")
        assert tables, "parse_ddl returned no tables"
        sql = emit_ddl(tables[0], dialect="db2", if_not_exists=False)
        _execute(conn, sql)
        cols = _introspect(conn, "orders")
        assert "order_id"    in cols
        assert "customer_id" in cols

    def test_nullability_preserved(self, conn):
        cols = _introspect(conn, "orders")
        assert cols["order_id"]["nullable"]    is False
        assert cols["customer_id"]["nullable"] is False
        assert cols["status"]["nullable"]      is False
        assert cols["total"]["nullable"]       is True
        assert cols["notes"]["nullable"]       is True

    def test_decimal_precision_preserved(self, conn):
        cols = _introspect(conn, "orders")
        assert cols["total"]["data_type"] == "DECIMAL"
        assert cols["total"]["length"]    == 12
        assert cols["total"]["scale"]     == 2

    def test_string_length_preserved(self, conn):
        cols = _introspect(conn, "orders")
        assert cols["status"]["data_type"] == "VARCHAR"
        assert cols["status"]["length"]    == 50


class TestTypeRoundTrips:
    """One table per type group — emit, execute on Db2, introspect via SYSCAT.COLUMNS."""

    def test_integer_types(self, conn):
        cols = _run(conn, """
            CREATE TABLE t_ints (
                c_smallint  SMALLINT,
                c_integer   INTEGER,
                c_bigint    BIGINT
            )
        """, "t_ints")
        # SMALLINT maps to canonical "integer" which re-emits as INTEGER in Db2.
        assert cols["c_smallint"]["data_type"]  in ("SMALLINT", "INTEGER")
        assert cols["c_integer"]["data_type"]   == "INTEGER"
        assert cols["c_bigint"]["data_type"]    == "BIGINT"

    def test_string_types(self, conn):
        cols = _run(conn, """
            CREATE TABLE t_strings (
                c_char    CHAR(10),
                c_varchar VARCHAR(255),
                c_clob    CLOB
            )
        """, "t_strings")
        # CHAR(n) maps to canonical "string" with length, which re-emits as VARCHAR(n) in Db2.
        assert cols["c_char"]["data_type"]    in ("CHARACTER", "VARCHAR")
        assert cols["c_char"]["length"]       == 10
        assert cols["c_varchar"]["data_type"] == "VARCHAR"
        assert cols["c_varchar"]["length"]    == 255
        # CLOB: canonical unbounded string round-trips to CLOB
        assert cols["c_clob"]["data_type"]    == "CLOB"

    def test_decimal_and_float(self, conn):
        cols = _run(conn, """
            CREATE TABLE t_numeric (
                c_decimal  DECIMAL(10,3),
                c_real     REAL,
                c_double   DOUBLE
            )
        """, "t_numeric")
        assert cols["c_decimal"]["data_type"] == "DECIMAL"
        assert cols["c_decimal"]["length"]    == 10
        assert cols["c_decimal"]["scale"]     == 3
        assert cols["c_real"]["data_type"]    == "REAL"
        assert cols["c_double"]["data_type"]  == "DOUBLE"

    def test_boolean(self, conn):
        """Db2 11.1+ has a native BOOLEAN type."""
        cols = _run(conn, """
            CREATE TABLE t_bool (
                c_bool   BOOLEAN NOT NULL
            )
        """, "t_bool")
        assert cols["c_bool"]["data_type"] == "BOOLEAN"

    def test_date_and_time(self, conn):
        cols = _run(conn, """
            CREATE TABLE t_datetime (
                c_date      DATE,
                c_time      TIME,
                c_timestamp TIMESTAMP
            )
        """, "t_datetime")
        assert cols["c_date"]["data_type"]      == "DATE"
        assert cols["c_time"]["data_type"]      == "TIME"
        assert cols["c_timestamp"]["data_type"] == "TIMESTAMP"

    def test_binary_type(self, conn):
        cols = _run(conn, """
            CREATE TABLE t_binary (
                c_blob BLOB
            )
        """, "t_binary")
        assert cols["c_blob"]["data_type"] == "BLOB"


class TestDb2RoundTrip:
    """Full parse → canonical → emit → execute → introspect cycle."""

    def test_full_round_trip(self, conn):
        ddl = textwrap.dedent("""\
            CREATE TABLE products (
                product_id  INTEGER     NOT NULL,
                sku         VARCHAR(100) NOT NULL,
                price       DECIMAL(10,2) NOT NULL,
                description CLOB,
                PRIMARY KEY (product_id)
            )
        """)
        tables = parse_ddl(ddl, dialect="db2")
        assert tables
        emitted = emit_ddl(tables[0], dialect="db2", if_not_exists=False)
        _drop_table(conn, "products")
        _execute(conn, emitted)
        cols = _introspect(conn, "products")
        assert cols["product_id"]["nullable"] is False
        assert cols["sku"]["data_type"]       == "VARCHAR"
        assert cols["sku"]["length"]          == 100
        assert cols["price"]["data_type"]     == "DECIMAL"
        assert cols["price"]["length"]        == 10
        assert cols["price"]["scale"]         == 2
        assert cols["description"]["data_type"] == "CLOB"

    def test_identity_column(self, conn):
        """GENERATED ALWAYS AS IDENTITY round-trips correctly on Db2."""
        ddl = textwrap.dedent("""\
            CREATE TABLE seq_test (
                id    INTEGER NOT NULL GENERATED ALWAYS AS IDENTITY,
                label VARCHAR(50)
            )
        """)
        tables = parse_ddl(ddl, dialect="db2")
        assert tables
        emitted = emit_ddl(tables[0], dialect="db2", if_not_exists=False)
        _drop_table(conn, "seq_test")
        _execute(conn, emitted)
        cols = _introspect(conn, "seq_test")
        assert cols["id"]["data_type"]    == "INTEGER"
        assert cols["id"]["nullable"]     is False

    def test_schema_source_db2_load(self, conn):
        """load_schema with schema_source: db2 routes to Db2/ANSI DDL parsing."""
        data = {
            "schema_source": "db2",
            "ddl": textwrap.dedent("""\
                CREATE TABLE events (
                    event_id  BIGINT    NOT NULL,
                    name      VARCHAR(200) NOT NULL,
                    payload   CLOB
                )
            """),
        }
        tables = load_schema(data)
        assert tables, "load_schema with schema_source: db2 returned no tables"
        t = tables[0]
        col_names = [c.name for c in t.columns]
        assert "event_id" in col_names
        assert "name"     in col_names

    def test_if_not_exists(self, conn):
        """CREATE TABLE IF NOT EXISTS must not fail when table already exists."""
        ddl = textwrap.dedent("""\
            CREATE TABLE ine_test (
                id    INTEGER NOT NULL,
                value VARCHAR(50)
            )
        """)
        tables = parse_ddl(ddl, dialect="db2")
        assert tables
        # First create — must succeed
        _drop_table(conn, "ine_test")
        sql = emit_ddl(tables[0], dialect="db2", if_not_exists=True)
        _execute(conn, sql)
        # Second create with IF NOT EXISTS — must not raise
        _execute(conn, sql)
        cols = _introspect(conn, "ine_test")
        assert "id" in cols
