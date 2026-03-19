"""
Live SQL Server integration tests.

These tests connect to a real SQL Server instance (via Lima VM) and verify that:
  1. DDL emitted by emit_ddl(dialect="tsql") executes without errors.
  2. The schema read back from INFORMATION_SCHEMA matches the canonical model.
  3. A round-trip parse → emit → execute → introspect is lossless for key types.

Prerequisites
-------------
A running SQL Server instance (see docs/local-databases.md):

    limactl start sqlserver22
    # SQL Server listens on 127.0.0.1:14330 by default

Environment variables (or edit _CONN_DEFAULTS below):

    SQLSERVER_HOST    default: 127.0.0.1
    SQLSERVER_PORT    default: 14330
    SQLSERVER_USER    default: sa
    SQLSERVER_PASS    required (no default – set from cloud-init-output.log)
    SQLSERVER_DB      default: testdb

Find the password:
    grep "SQL Server sa password is" ~/.lima/sqlserver22/serial*.log | tail -1
    # or:
    limactl shell sqlserver22 -- grep "SQL Server sa password is" /var/log/cloud-init-output.log | tail -1

Skip behaviour
--------------
All tests are automatically skipped when:
- pymssql is not installed, OR
- SQLSERVER_PASS is not set in the environment, OR
- the connection to SQL Server fails (e.g. VM not running)
"""

import os
import textwrap

import pytest

from src.statschema import parse_ddl, emit_ddl
from src.statschema.model import CanonicalTableSchema

# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

_CONN_DEFAULTS = {
    "host":     os.environ.get("SQLSERVER_HOST", "127.0.0.1"),
    "port":     int(os.environ.get("SQLSERVER_PORT", "14330")),
    "user":     os.environ.get("SQLSERVER_USER", "sa"),
    "password": os.environ.get("SQLSERVER_PASS", ""),
    "database": os.environ.get("SQLSERVER_DB",   "master"),
}


def _get_connection():
    """Return a pymssql connection or raise RuntimeError."""
    pymssql = pytest.importorskip("pymssql")
    p = _CONN_DEFAULTS
    if not p["password"]:
        pytest.skip("SQLSERVER_PASS not set – skipping live SQL Server tests")
    try:
        conn = pymssql.connect(
            server=p["host"],
            port=p["port"],
            user=p["user"],
            password=p["password"],
            database=p["database"],
            login_timeout=5,
            tds_version="7.4",
        )
        return conn
    except Exception as exc:
        pytest.skip(f"Cannot connect to SQL Server ({p['host']}:{p['port']}): {exc}")


def _execute(conn, sql: str):
    """Execute one or more SQL statements separated by GO or semicolons.

    T-SQL IF...BEGIN...END blocks must be sent as a single batch; splitting
    naively on ';' breaks them.  We therefore send the whole string at once
    if it contains a BEGIN...END block, and fall back to semicolon-splitting
    for simple single-statement strings.
    """
    cur = conn.cursor()
    if "BEGIN" in sql.upper() and "END" in sql.upper():
        # Send as a single batch to preserve IF...BEGIN...END structure
        cur.execute(sql)
    else:
        for statement in sql.split(";"):
            stmt = statement.strip()
            if stmt:
                cur.execute(stmt)
    try:
        conn.commit()
    except Exception:
        pass
    return cur


def _fetchall(conn, sql: str) -> list[tuple]:
    cur = conn.cursor()
    cur.execute(sql)
    return cur.fetchall()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def conn():
    pymssql = pytest.importorskip("pymssql")
    p = _CONN_DEFAULTS
    if not p["password"]:
        pytest.skip("SQLSERVER_PASS not set – skipping live SQL Server tests")

    # Connect to master first to create the test database
    try:
        master_conn = pymssql.connect(
            server=p["host"], port=p["port"],
            user=p["user"], password=p["password"],
            database="master", login_timeout=5, tds_version="7.4",
            autocommit=True,
        )
    except Exception as exc:
        pytest.skip(f"Cannot connect to SQL Server ({p['host']}:{p['port']}): {exc}")

    cur = master_conn.cursor()
    cur.execute("IF DB_ID('zerobus_test') IS NULL CREATE DATABASE zerobus_test")
    master_conn.close()

    # Re-connect directly into the test database
    connection = pymssql.connect(
        server=p["host"], port=p["port"],
        user=p["user"], password=p["password"],
        database="zerobus_test", login_timeout=5, tds_version="7.4",
    )
    yield connection

    # Teardown: drop the test database
    connection.close()
    cleanup = pymssql.connect(
        server=p["host"], port=p["port"],
        user=p["user"], password=p["password"],
        database="master", login_timeout=5, tds_version="7.4",
        autocommit=True,
    )
    cleanup.cursor().execute("DROP DATABASE IF EXISTS zerobus_test")
    cleanup.close()


# ---------------------------------------------------------------------------
# Helper: parse DDL → emit for tsql → execute → read back column info
# ---------------------------------------------------------------------------

def _introspect_columns(conn, table_name: str) -> dict[str, dict]:
    """Return {column_name: {data_type, max_length, precision, scale, is_nullable}} from INFORMATION_SCHEMA."""
    rows = _fetchall(conn, f"""
        SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH,
               NUMERIC_PRECISION, NUMERIC_SCALE, IS_NULLABLE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = '{table_name}'
        ORDER BY ORDINAL_POSITION
    """)
    return {
        row[0]: {
            "data_type":  row[1],
            "max_length": row[2],
            "precision":  row[3],
            "scale":      row[4],
            "nullable":   row[5] == "YES",
        }
        for row in rows
    }


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------

class TestSqlServerConnection:
    """Smoke-test: can we connect and run a trivial query?"""

    def test_version(self, conn):
        rows = _fetchall(conn, "SELECT @@VERSION")
        version = rows[0][0]
        assert "Microsoft SQL Server" in version
        assert "2022" in version or "2019" in version


class TestEmitAndExecute:
    """Emit canonical DDL as T-SQL and verify it executes cleanly."""

    _SOURCE_DDL = textwrap.dedent("""\
        CREATE TABLE dbo.customers (
            customer_id   INT           NOT NULL,
            email         NVARCHAR(255) NOT NULL,
            balance       DECIMAL(18,4)     NULL,
            is_active     BIT           NOT NULL DEFAULT 1,
            created_at    DATETIME2(3)      NULL,
            notes         NVARCHAR(MAX)     NULL,
            CONSTRAINT PK_customers PRIMARY KEY (customer_id)
        );
    """)

    def test_emit_executes_without_error(self, conn):
        _execute(conn, "DROP TABLE IF EXISTS dbo.customers")
        tables = parse_ddl(self._SOURCE_DDL, dialect="sqlserver")
        assert tables, "parse_ddl returned no tables"
        tsql   = emit_ddl(tables[0], dialect="tsql")
        _execute(conn, "DROP TABLE IF EXISTS dbo.customers")
        _execute(conn, tsql)
        cols = _introspect_columns(conn, "customers")
        assert "customer_id" in cols
        assert "email"       in cols

    def test_column_types_survive_round_trip(self, conn):
        """Parse → emit → execute → introspect: key types match expectations."""
        cols = _introspect_columns(conn, "customers")

        assert cols["customer_id"]["data_type"] == "int"
        assert cols["customer_id"]["nullable"]  is False

        assert cols["email"]["data_type"]  == "nvarchar"
        assert cols["email"]["max_length"] == 255
        assert cols["email"]["nullable"]   is False

        assert cols["balance"]["data_type"] == "decimal"
        assert cols["balance"]["precision"] == 18
        assert cols["balance"]["scale"]     == 4
        assert cols["balance"]["nullable"]  is True

        assert cols["is_active"]["data_type"] == "bit"
        assert cols["notes"]["data_type"]     == "nvarchar"
        assert cols["notes"]["max_length"]    == -1  # MAX

    def test_not_null_preserved(self, conn):
        cols = _introspect_columns(conn, "customers")
        assert cols["customer_id"]["nullable"] is False
        assert cols["email"]["nullable"]       is False
        assert cols["is_active"]["nullable"]   is False

    def test_nullable_preserved(self, conn):
        cols = _introspect_columns(conn, "customers")
        assert cols["balance"]["nullable"]    is True
        assert cols["created_at"]["nullable"] is True
        assert cols["notes"]["nullable"]      is True


class TestTypeRoundTrips:
    """One table per canonical type — emit T-SQL, execute, introspect."""

    def _run(self, conn, ddl: str, table: str) -> dict[str, dict]:
        _execute(conn, f"DROP TABLE IF EXISTS dbo.{table}")
        tables = parse_ddl(ddl, dialect="sqlserver")
        assert tables, f"parse_ddl returned no tables for: {ddl[:80]}"
        tsql = emit_ddl(tables[0], dialect="tsql")
        _execute(conn, tsql)
        return _introspect_columns(conn, table)

    def test_integer_types(self, conn):
        cols = self._run(conn, """
            CREATE TABLE dbo.t_ints (
                c_tinyint   TINYINT, c_smallint SMALLINT,
                c_int       INT,     c_bigint   BIGINT
            )
        """, "t_ints")
        # canonical "integer" → INT, canonical "long" → BIGINT.
        # TINYINT and SMALLINT are widened to INT by the canonical model.
        assert cols["c_tinyint"]["data_type"]  == "int"
        assert cols["c_smallint"]["data_type"] == "int"
        assert cols["c_int"]["data_type"]      == "int"
        assert cols["c_bigint"]["data_type"]   == "bigint"

    def test_string_types(self, conn):
        cols = self._run(conn, """
            CREATE TABLE dbo.t_strings (
                c_char      CHAR(10),
                c_varchar   VARCHAR(200),
                c_nvarchar  NVARCHAR(400),
                c_text      NVARCHAR(MAX)
            )
        """, "t_strings")
        assert cols["c_char"]["max_length"]    == 10
        assert cols["c_varchar"]["max_length"] == 200
        assert cols["c_nvarchar"]["max_length"]== 400
        assert cols["c_text"]["max_length"]    == -1

    def test_decimal_and_float(self, conn):
        cols = self._run(conn, """
            CREATE TABLE dbo.t_numeric (
                c_decimal   DECIMAL(10, 2),
                c_money     MONEY,
                c_float     FLOAT,
                c_real      REAL
            )
        """, "t_numeric")
        assert cols["c_decimal"]["data_type"] == "decimal"
        assert cols["c_decimal"]["precision"] == 10
        assert cols["c_decimal"]["scale"]     == 2
        # MONEY → canonical decimal(19,4) → DECIMAL(19,4) on re-emit (intentional widening)
        assert cols["c_money"]["data_type"]   == "decimal"
        assert cols["c_money"]["precision"]   == 19
        assert cols["c_money"]["scale"]       == 4
        assert cols["c_float"]["data_type"]   in ("float", "real")

    def test_date_and_time(self, conn):
        cols = self._run(conn, """
            CREATE TABLE dbo.t_datetime (
                c_date          DATE,
                c_time          TIME(3),
                c_datetime2     DATETIME2(6),
                c_datetimeoffset DATETIMEOFFSET(3)
            )
        """, "t_datetime")
        assert cols["c_date"]["data_type"]         == "date"
        assert cols["c_time"]["data_type"]         == "time"
        assert cols["c_datetime2"]["data_type"]    == "datetime2"
        assert cols["c_datetimeoffset"]["data_type"]== "datetimeoffset"

    def test_binary_and_bool(self, conn):
        cols = self._run(conn, """
            CREATE TABLE dbo.t_binary (
                c_binary    BINARY(16),
                c_varbinary VARBINARY(256),
                c_bit       BIT
            )
        """, "t_binary")
        # canonical "binary" always emits as VARBINARY in SQL Server
        assert cols["c_binary"]["data_type"]    == "varbinary"
        assert cols["c_binary"]["max_length"]   == 16
        assert cols["c_varbinary"]["data_type"] == "varbinary"
        assert cols["c_bit"]["data_type"]       == "bit"

    def test_uniqueidentifier(self, conn):
        cols = self._run(conn, """
            CREATE TABLE dbo.t_guid (
                c_guid UNIQUEIDENTIFIER NOT NULL DEFAULT NEWID()
            )
        """, "t_guid")
        # canonical "uuid" → UNIQUEIDENTIFIER in SQL Server
        assert cols["c_guid"]["data_type"] == "uniqueidentifier"

    def test_identity_column(self, conn):
        cols = self._run(conn, """
            CREATE TABLE dbo.t_identity (
                id   INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
                name NVARCHAR(100)
            )
        """, "t_identity")
        assert cols["id"]["data_type"]  == "int"
        assert cols["id"]["nullable"]   is False

    def test_foreign_key(self, conn):
        _execute(conn, "DROP TABLE IF EXISTS dbo.fk_child")
        _execute(conn, "DROP TABLE IF EXISTS dbo.fk_parent")
        _execute(conn, """
            CREATE TABLE dbo.fk_parent (
                id INT NOT NULL PRIMARY KEY
            )
        """)
        tables = parse_ddl("""
            CREATE TABLE dbo.fk_child (
                id         INT NOT NULL PRIMARY KEY,
                parent_id  INT NOT NULL,
                CONSTRAINT fk_pc FOREIGN KEY (parent_id)
                    REFERENCES dbo.fk_parent (id)
            )
        """, dialect="sqlserver")
        assert tables, "parse_ddl returned no tables"
        tsql = emit_ddl(tables[0], dialect="tsql")
        _execute(conn, tsql)
        cols = _introspect_columns(conn, "fk_child")
        assert "parent_id" in cols


class TestParseFromLiveServer:
    """Extract DDL from INFORMATION_SCHEMA and re-parse it."""

    def test_create_and_reparse(self, conn):
        """Create a table, reconstruct its DDL from catalog, parse it."""
        _execute(conn, "DROP TABLE IF EXISTS dbo.reparse_test")
        _execute(conn, """
            CREATE TABLE dbo.reparse_test (
                id     INT           NOT NULL PRIMARY KEY,
                name   NVARCHAR(100) NOT NULL,
                score  DECIMAL(8,3)      NULL
            )
        """)
        # Build CREATE TABLE DDL from INFORMATION_SCHEMA
        rows = _fetchall(conn, """
            SELECT COLUMN_NAME, DATA_TYPE,
                   CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE,
                   IS_NULLABLE
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = 'reparse_test'
            ORDER BY ORDINAL_POSITION
        """)
        col_defs = []
        for col_name, dtype, maxlen, prec, scale, nullable in rows:
            null_str = "NULL" if nullable == "YES" else "NOT NULL"
            if dtype in ("nvarchar", "varchar", "char", "nchar"):
                length = "MAX" if maxlen == -1 else str(maxlen)
                col_defs.append(f"    {col_name} {dtype.upper()}({length}) {null_str}")
            elif dtype == "decimal":
                col_defs.append(f"    {col_name} DECIMAL({prec},{scale}) {null_str}")
            else:
                col_defs.append(f"    {col_name} {dtype.upper()} {null_str}")
        reconstructed_ddl = "CREATE TABLE reparse_test (\n" + ",\n".join(col_defs) + "\n);"

        tables = parse_ddl(reconstructed_ddl, dialect="sqlserver")
        assert tables, "parse_ddl returned no tables from reconstructed DDL"
        schema = tables[0]
        assert len(schema.columns) == 3
        assert schema.columns[0].name == "id"
        assert schema.columns[1].type == "string"
        assert schema.columns[2].type == "decimal"
