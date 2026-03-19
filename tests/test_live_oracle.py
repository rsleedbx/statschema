"""
Live Oracle XE integration tests.

Connects to a real Oracle instance (via Lima QEMU VM) and verifies:
  1. DDL emitted by emit_ddl(dialect="oracle") executes without errors.
  2. The schema read back from ALL_TAB_COLUMNS matches the canonical model.
  3. A round-trip parse → emit → execute → introspect is lossless for key types.

Prerequisites
-------------
Oracle XE running inside a Lima QEMU VM (see docs/local-databases.md):

    limactl start --name=oracle ~/github/zerobus/config/lima/oracle.yaml
    # First boot takes 3-5 minutes; watch progress with:
    limactl shell oracle -- podman logs -f oracle-xe | grep -i "ready"

Environment variables (defaults match the oracle.yaml ORACLE_PWD value):

    ORACLE_HOST     default: 127.0.0.1
    ORACLE_PORT     default: 1521
    ORACLE_USER     default: system
    ORACLE_PASS     default: oracle
    ORACLE_SERVICE  default: XE
    ORACLE_SCHEMA   default: ZEROBUS_TEST   (test schema/user created by fixture)

Skip behaviour
--------------
All tests are automatically skipped when:
- oracledb is not installed, OR
- ORACLE_PASS is not set, OR
- the connection to Oracle fails (e.g. VM not running)
"""

import os
import textwrap

import pytest

from src.schema_parser import parse_ddl, emit_ddl

# ---------------------------------------------------------------------------
# Connection defaults
# ---------------------------------------------------------------------------

_HOST    = os.environ.get("ORACLE_HOST",    "127.0.0.1")
_PORT    = int(os.environ.get("ORACLE_PORT",    "1521"))
_USER    = os.environ.get("ORACLE_USER",    "system")
_PASS    = os.environ.get("ORACLE_PASS",    "oracle")
_SERVICE = os.environ.get("ORACLE_SERVICE", "XE")
_SCHEMA  = os.environ.get("ORACLE_SCHEMA",  "ZEROBUS_TEST").upper()
_SCHEMA_PASS = "ZerobusTest1"   # password for the test schema user


def _get_connection(user: str = _USER, password: str = _PASS, service: str = _SERVICE):
    oracledb = pytest.importorskip("oracledb")
    try:
        conn = oracledb.connect(
            user=user,
            password=password,
            dsn=f"{_HOST}:{_PORT}/{service}",
        )
        return conn
    except Exception as exc:
        pytest.skip(f"Cannot connect to Oracle ({_HOST}:{_PORT}/{service}): {exc}")


def _execute(conn, sql: str):
    """Execute a single SQL statement, ignoring trailing semicolons."""
    cur = conn.cursor()
    cur.execute(sql.rstrip(";").strip())
    conn.commit()
    return cur


def _execute_ignore(conn, sql: str, *ignore_codes: int):
    """Execute SQL, silently swallowing specific Oracle error codes."""
    try:
        _execute(conn, sql)
    except Exception as exc:
        msg = str(exc)
        if any(f"ORA-{code:05d}" in msg for code in ignore_codes):
            return
        raise


def _fetchall(conn, sql: str) -> list[tuple]:
    cur = conn.cursor()
    cur.execute(sql.rstrip(";").strip())
    return cur.fetchall()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def conn():
    """
    Module-scoped connection.

    Creates a dedicated test user (ZEROBUS_TEST) so tests don't pollute the
    SYSTEM schema.  Drops and recreates the user on every run for a clean slate.
    Requires oracledb and a reachable Oracle instance.
    """
    oracledb = pytest.importorskip("oracledb")
    if not _PASS:
        pytest.skip("ORACLE_PASS not set – skipping live Oracle tests")

    sys_conn = _get_connection()

    # Drop and recreate the test user for a clean slate
    _execute_ignore(sys_conn,
        f"DROP USER {_SCHEMA} CASCADE",
        1918,  # ORA-01918: user does not exist
    )
    _execute(sys_conn,
        f"CREATE USER {_SCHEMA} IDENTIFIED BY \"{_SCHEMA_PASS}\" "
        f"DEFAULT TABLESPACE USERS QUOTA UNLIMITED ON USERS"
    )
    _execute(sys_conn, f"GRANT CREATE SESSION, CREATE TABLE, CREATE SEQUENCE TO {_SCHEMA}")
    sys_conn.close()

    # Connect as the test user
    test_conn = _get_connection(user=_SCHEMA, password=_SCHEMA_PASS)
    yield test_conn

    test_conn.close()

    # Teardown: drop the test user
    cleanup = _get_connection()
    _execute_ignore(cleanup, f"DROP USER {_SCHEMA} CASCADE", 1918)
    cleanup.close()


# ---------------------------------------------------------------------------
# Introspection helper
# ---------------------------------------------------------------------------

def _introspect(conn, table: str) -> dict[str, dict]:
    """
    Return {column_name: {data_type, data_length, data_precision, data_scale,
                          nullable, char_used}} from ALL_TAB_COLUMNS.
    Column names are lowercased for consistent comparison.
    """
    rows = _fetchall(conn, f"""
        SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH,
               DATA_PRECISION, DATA_SCALE, NULLABLE, CHAR_USED
        FROM ALL_TAB_COLUMNS
        WHERE OWNER = '{_SCHEMA}'
          AND TABLE_NAME = '{table.upper()}'
        ORDER BY COLUMN_ID
    """)
    return {
        row[0].lower(): {
            "data_type":      row[1],
            "data_length":    row[2],
            "data_precision": row[3],
            "data_scale":     row[4],
            "nullable":       row[5] == "Y",
            "char_used":      row[6],
        }
        for row in rows
    }


def _drop_table(conn, table: str):
    _execute_ignore(conn,
        f"DROP TABLE {_SCHEMA}.\"{table.upper()}\" CASCADE CONSTRAINTS",
        942,   # ORA-00942: table or view does not exist
    )


def _run(conn, ddl: str, table: str) -> dict[str, dict]:
    """Parse DDL → emit Oracle DDL → execute → introspect."""
    _drop_table(conn, table)
    tables = parse_ddl(ddl, dialect="oracle")
    assert tables, f"parse_ddl returned no tables for: {ddl[:80]}"
    oracle_ddl = emit_ddl(tables[0], dialect="oracle", if_not_exists=False)
    _execute(conn, oracle_ddl)
    return _introspect(conn, table)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestOracleConnection:
    """Smoke-test: can we connect and run a trivial query?"""

    def test_version(self, conn):
        rows = _fetchall(conn, "SELECT * FROM V$VERSION WHERE BANNER LIKE 'Oracle%'")
        assert rows, "No version row returned"
        banner = rows[0][0]
        assert "Oracle" in banner
        print(f"\nOracle version: {banner}")

    def test_test_schema_accessible(self, conn):
        rows = _fetchall(conn, f"SELECT user FROM dual")
        assert rows[0][0].upper() == _SCHEMA


class TestEmitAndExecute:
    """Emit canonical DDL as Oracle SQL and verify it executes cleanly."""

    _SOURCE_DDL = textwrap.dedent("""\
        CREATE TABLE customers (
            customer_id   NUMBER(10)    NOT NULL,
            email         VARCHAR2(255) NOT NULL,
            balance       NUMBER(18,4)      NULL,
            is_active     NUMBER(1)     NOT NULL,
            created_at    TIMESTAMP         NULL,
            notes         CLOB              NULL,
            CONSTRAINT pk_customers PRIMARY KEY (customer_id)
        )
    """)

    def test_emit_executes_without_error(self, conn):
        _drop_table(conn, "customers")
        tables = parse_ddl(self._SOURCE_DDL, dialect="oracle")
        assert tables, "parse_ddl returned no tables"
        oracle_ddl = emit_ddl(tables[0], dialect="oracle", if_not_exists=False)
        _execute(conn, oracle_ddl)
        cols = _introspect(conn, "customers")
        assert "customer_id" in cols
        assert "email"        in cols

    def test_column_types_survive_round_trip(self, conn):
        cols = _introspect(conn, "customers")

        assert cols["customer_id"]["data_type"] == "NUMBER"
        # NUMBER(10) without scale → canonical "long" → emits NUMBER(19).
        # Precision widening is intentional: canonical "integer" = NUMBER(10),
        # "long" = NUMBER(19); input NUMBER(10) falls in the "long" bucket.
        assert cols["customer_id"]["data_precision"] in (10, 19)
        assert cols["customer_id"]["nullable"] is False

        assert cols["email"]["data_type"]   == "VARCHAR2"
        assert cols["email"]["data_length"] == 255
        assert cols["email"]["nullable"]    is False

        assert cols["balance"]["data_type"]      == "NUMBER"
        assert cols["balance"]["data_precision"] == 18
        assert cols["balance"]["data_scale"]     == 4
        assert cols["balance"]["nullable"]       is True

        assert cols["is_active"]["data_type"] == "NUMBER"
        assert cols["notes"]["data_type"]      == "CLOB"

    def test_not_null_preserved(self, conn):
        cols = _introspect(conn, "customers")
        assert cols["customer_id"]["nullable"] is False
        assert cols["email"]["nullable"]       is False
        assert cols["is_active"]["nullable"]   is False

    def test_nullable_preserved(self, conn):
        cols = _introspect(conn, "customers")
        assert cols["balance"]["nullable"]    is True
        assert cols["created_at"]["nullable"] is True
        assert cols["notes"]["nullable"]      is True


class TestTypeRoundTrips:
    """One table per canonical type group — emit, execute, introspect."""

    def test_integer_types(self, conn):
        """
        Oracle NUMBER(p) without scale widens to canonical integer/long:
          precision ≤ 9  → "integer" → NUMBER(10)
          precision ≤ 18 → "long"    → NUMBER(19)
          precision > 18 → "decimal" → NUMBER(p,0)
        """
        cols = _run(conn, """
            CREATE TABLE t_ints (
                c_int    NUMBER(10),
                c_bigint NUMBER(19)
            )
        """, "t_ints")
        assert cols["c_int"]["data_type"]    == "NUMBER"
        # NUMBER(10) → long → NUMBER(19)
        assert cols["c_int"]["data_precision"] == 19
        assert cols["c_bigint"]["data_type"]  == "NUMBER"
        # NUMBER(19) → decimal(19,0) → NUMBER(19,0) — precision preserved
        assert cols["c_bigint"]["data_precision"] == 19

    def test_string_types(self, conn):
        """VARCHAR2 with length → VARCHAR2; CLOB (no length) → canonical string → CLOB."""
        cols = _run(conn, """
            CREATE TABLE t_strings (
                c_varchar  VARCHAR2(200),
                c_char     CHAR(10),
                c_clob     CLOB
            )
        """, "t_strings")
        assert cols["c_varchar"]["data_type"]   == "VARCHAR2"
        assert cols["c_varchar"]["data_length"] == 200
        # CLOB → canonical string (no length) → emits CLOB for Oracle
        assert cols["c_clob"]["data_type"] == "CLOB"

    def test_decimal_and_float(self, conn):
        cols = _run(conn, """
            CREATE TABLE t_numeric (
                c_number   NUMBER(12, 3),
                c_float    FLOAT,
                c_float53  FLOAT(53)
            )
        """, "t_numeric")
        assert cols["c_number"]["data_type"]      == "NUMBER"
        assert cols["c_number"]["data_precision"] == 12
        assert cols["c_number"]["data_scale"]     == 3
        assert cols["c_float"]["data_type"]       == "FLOAT"

    def test_boolean_as_number1(self, conn):
        """
        NUMBER(1) → canonical integer (precision ≤ 9) → emits NUMBER(10).
        The canonical model widens single-digit NUMBER to INTEGER = NUMBER(10).
        """
        cols = _run(conn, """
            CREATE TABLE t_bool (
                c_bool NUMBER(1) NOT NULL
            )
        """, "t_bool")
        assert cols["c_bool"]["data_type"] == "NUMBER"
        # NUMBER(1) widens to canonical integer → NUMBER(10)
        assert cols["c_bool"]["data_precision"] == 10

    def test_date_and_timestamp(self, conn):
        """DATE and TIMESTAMP survive round-trip."""
        cols = _run(conn, """
            CREATE TABLE t_datetime (
                c_date      DATE,
                c_ts        TIMESTAMP,
                c_tstz      TIMESTAMP WITH TIME ZONE
            )
        """, "t_datetime")
        assert cols["c_date"]["data_type"] == "DATE"
        assert cols["c_ts"]["data_type"]   in ("TIMESTAMP(6)", "TIMESTAMP")
        assert "TIME ZONE" in cols["c_tstz"]["data_type"]

    def test_binary_types(self, conn):
        """RAW(N) for short binary, BLOB for large binary."""
        cols = _run(conn, """
            CREATE TABLE t_binary (
                c_raw   RAW(16),
                c_blob  BLOB
            )
        """, "t_binary")
        assert cols["c_raw"]["data_type"]  == "RAW"
        assert cols["c_raw"]["data_length"] == 16
        assert cols["c_blob"]["data_type"] == "BLOB"

    def test_uuid_as_char36(self, conn):
        """
        CHAR(36) → canonical string(length=36) → emits VARCHAR2(36) in Oracle.
        The canonical model normalises CHAR → string; Oracle emits VARCHAR2 for
        string-with-length.  CHAR(36) and VARCHAR2(36) are both acceptable.
        """
        cols = _run(conn, """
            CREATE TABLE t_uuid (
                c_uuid CHAR(36) NOT NULL
            )
        """, "t_uuid")
        assert cols["c_uuid"]["data_type"]   in ("CHAR", "VARCHAR2")
        assert cols["c_uuid"]["data_length"] == 36

    def test_not_null_and_nullable(self, conn):
        cols = _run(conn, """
            CREATE TABLE t_null (
                c_notnull  NUMBER(10)    NOT NULL,
                c_nullable VARCHAR2(50)
            )
        """, "t_null")
        assert cols["c_notnull"]["nullable"]  is False
        assert cols["c_nullable"]["nullable"] is True

    def test_primary_key(self, conn):
        """PRIMARY KEY constraint emits correctly and table executes."""
        _drop_table(conn, "t_pk")
        tables = parse_ddl("""
            CREATE TABLE t_pk (
                id   NUMBER(10)    NOT NULL,
                name VARCHAR2(100),
                CONSTRAINT pk_t PRIMARY KEY (id)
            )
        """, dialect="oracle")
        assert tables
        oracle_ddl = emit_ddl(tables[0], dialect="oracle", if_not_exists=False)
        _execute(conn, oracle_ddl)
        cols = _introspect(conn, "t_pk")
        assert cols["id"]["nullable"] is False

    def test_identity_column(self, conn):
        """Oracle 12c+ IDENTITY / GENERATED BY DEFAULT AS IDENTITY."""
        # We emit NUMBER(10) for auto_increment; Oracle 12c+ uses GENERATED AS IDENTITY.
        _drop_table(conn, "t_identity")
        tables = parse_ddl("""
            CREATE TABLE t_identity (
                id   NUMBER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                name VARCHAR2(100)
            )
        """, dialect="oracle")
        assert tables
        oracle_ddl = emit_ddl(tables[0], dialect="oracle", if_not_exists=False)
        _execute(conn, oracle_ddl)
        cols = _introspect(conn, "t_identity")
        assert "id" in cols
        assert cols["id"]["nullable"] is False

    def test_foreign_key(self, conn):
        """FK constraints emit and execute without error."""
        _drop_table(conn, "fk_child")
        _drop_table(conn, "fk_parent")
        _execute(conn, """
            CREATE TABLE fk_parent (
                id NUMBER(10) NOT NULL,
                CONSTRAINT pk_fk_parent PRIMARY KEY (id)
            )
        """)
        tables = parse_ddl("""
            CREATE TABLE fk_child (
                id        NUMBER(10) NOT NULL,
                parent_id NUMBER(10) NOT NULL,
                CONSTRAINT pk_fk_child PRIMARY KEY (id),
                CONSTRAINT fk_cp FOREIGN KEY (parent_id)
                    REFERENCES fk_parent (id)
            )
        """, dialect="oracle")
        assert tables
        oracle_ddl = emit_ddl(tables[0], dialect="oracle", if_not_exists=False)
        _execute(conn, oracle_ddl)
        cols = _introspect(conn, "fk_child")
        assert "parent_id" in cols


class TestCrossDialectRoundTrip:
    """Parse MySQL/PG DDL → emit Oracle DDL → execute on live Oracle."""

    def test_mysql_to_oracle(self, conn):
        """Parse a MySQL table and emit it as Oracle DDL."""
        _drop_table(conn, "mysql_orders")
        tables = parse_ddl("""
            CREATE TABLE `mysql_orders` (
                `order_id`    INT           NOT NULL AUTO_INCREMENT,
                `customer_id` INT           NOT NULL,
                `status`      VARCHAR(50)   NOT NULL DEFAULT 'pending',
                `total`       DECIMAL(10,2)     NULL,
                `is_paid`     TINYINT(1)    NOT NULL DEFAULT 0,
                `created_at`  DATETIME          NULL,
                PRIMARY KEY (`order_id`)
            ) ENGINE=InnoDB
        """, dialect="mysql")
        assert tables
        oracle_ddl = emit_ddl(tables[0], dialect="oracle", if_not_exists=False)
        _execute(conn, oracle_ddl)
        cols = _introspect(conn, "mysql_orders")
        assert "order_id"    in cols
        assert "customer_id" in cols
        assert "status"      in cols
        assert "total"       in cols
        assert "is_paid"     in cols
        # DATETIME → TIMESTAMP in Oracle
        assert cols["created_at"]["data_type"] in ("TIMESTAMP(6)", "TIMESTAMP", "DATE")
        # DECIMAL(10,2) → NUMBER(10,2)
        assert cols["total"]["data_type"] == "NUMBER"
        assert cols["total"]["data_precision"] == 10
        assert cols["total"]["data_scale"]     == 2

    def test_postgres_to_oracle(self, conn):
        """Parse a PostgreSQL table and emit it as Oracle DDL."""
        _drop_table(conn, "pg_products")
        tables = parse_ddl("""
            CREATE TABLE pg_products (
                product_id  SERIAL        NOT NULL,
                name        VARCHAR(200)  NOT NULL,
                price       NUMERIC(8,2)      NULL,
                is_active   BOOLEAN       NOT NULL DEFAULT TRUE,
                created_at  TIMESTAMPTZ       NULL,
                PRIMARY KEY (product_id)
            )
        """, dialect="postgres")
        assert tables
        oracle_ddl = emit_ddl(tables[0], dialect="oracle", if_not_exists=False)
        _execute(conn, oracle_ddl)
        cols = _introspect(conn, "pg_products")
        assert "product_id" in cols
        assert "name"       in cols
        # NUMERIC(8,2) → NUMBER(8,2)
        assert cols["price"]["data_type"]      == "NUMBER"
        assert cols["price"]["data_precision"] == 8
        assert cols["price"]["data_scale"]     == 2
        # BOOLEAN → NUMBER(1)
        assert cols["is_active"]["data_type"]      == "NUMBER"
        assert cols["is_active"]["data_precision"] == 1
        # TIMESTAMPTZ → TIMESTAMP WITH TIME ZONE
        assert "TIME ZONE" in cols["created_at"]["data_type"]


class TestParseFromLiveOracle:
    """Reconstruct DDL from ALL_TAB_COLUMNS and re-parse it."""

    def test_create_and_reparse(self, conn):
        """Create a table, build DDL from catalog, parse it back."""
        _drop_table(conn, "reparse_test")
        _execute(conn, """
            CREATE TABLE reparse_test (
                id     NUMBER(10)    NOT NULL,
                name   VARCHAR2(120) NOT NULL,
                score  NUMBER(8,3)
            )
        """)
        rows = _fetchall(conn, f"""
            SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH,
                   DATA_PRECISION, DATA_SCALE, NULLABLE
            FROM ALL_TAB_COLUMNS
            WHERE OWNER = '{_SCHEMA}'
              AND TABLE_NAME = 'REPARSE_TEST'
            ORDER BY COLUMN_ID
        """)
        col_defs = []
        for col_name, dtype, dlen, prec, scale, nullable in rows:
            null_str = "" if nullable == "Y" else " NOT NULL"
            if dtype == "NUMBER" and prec is not None and scale is not None:
                col_defs.append(f'  "{col_name}" NUMBER({prec},{scale}){null_str}')
            elif dtype == "NUMBER" and prec is not None:
                col_defs.append(f'  "{col_name}" NUMBER({prec}){null_str}')
            elif dtype in ("VARCHAR2", "CHAR"):
                col_defs.append(f'  "{col_name}" {dtype}({dlen}){null_str}')
            else:
                col_defs.append(f'  "{col_name}" {dtype}{null_str}')
        reconstructed = "CREATE TABLE reparse_test (\n" + ",\n".join(col_defs) + "\n)"

        tables = parse_ddl(reconstructed, dialect="oracle")
        assert tables, "parse_ddl returned no tables from reconstructed DDL"
        schema = tables[0]
        assert len(schema.columns) == 3
        assert schema.columns[0].name.lower() == "id"
        assert schema.columns[1].type == "string"
        assert schema.columns[2].type == "decimal"
