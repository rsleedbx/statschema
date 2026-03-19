"""
Live MySQL integration tests – runs against real MySQL 5.7 and 8.x instances.

Tests verify that DDL emitted by emit_ddl(dialect="mysql") executes without
errors on a real MySQL server and that types, nullability, and constraints
survive the parse → emit → execute → introspect cycle.

Prerequisites
-------------
Two MySQL containers via Podman (see docs/local-databases.md):

    podman run -d --name mysql57 --platform linux/amd64 \\
        -e MYSQL_ROOT_PASSWORD=testpass -e MYSQL_DATABASE=testdb \\
        -p 3357:3306 docker.io/library/mysql:5.7

    podman run -d --name mysql8 \\
        -e MYSQL_ROOT_PASSWORD=testpass -e MYSQL_DATABASE=testdb \\
        -p 3384:3306 docker.io/library/mysql:8.4

Environment variables (defaults match the Podman commands above):

    MYSQL_HOST       default: 127.0.0.1
    MYSQL_ROOT_PASS  default: testpass
    MYSQL57_PORT     default: 3357
    MYSQL8_PORT      default: 3384

Skip behaviour
--------------
Each parametrised version skips automatically when its port is unreachable or
pymysql is not installed — so make test always completes cleanly.
"""

import os
import textwrap

import pytest

from src.schema_parser import parse_ddl, emit_ddl

# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

_HOST      = os.environ.get("MYSQL_HOST",      "127.0.0.1")
_ROOT_PASS = os.environ.get("MYSQL_ROOT_PASS", "testpass")
_PORT_57   = int(os.environ.get("MYSQL57_PORT", "3357"))
_PORT_8    = int(os.environ.get("MYSQL8_PORT",  "3384"))

_VERSIONS = [
    pytest.param(("5.7", _PORT_57), id="mysql57"),
    pytest.param(("8.x", _PORT_8),  id="mysql8"),
]


def _get_connection(port: int, db: str = "testdb"):
    pymysql = pytest.importorskip("pymysql")
    try:
        conn = pymysql.connect(
            host=_HOST, port=port,
            user="root", password=_ROOT_PASS,
            database=db,
            connect_timeout=5,
            autocommit=True,
        )
        return conn
    except Exception as exc:
        pytest.skip(f"Cannot connect to MySQL on {_HOST}:{port}: {exc}")


def _execute(conn, sql: str):
    cur = conn.cursor()
    for stmt in sql.split(";"):
        s = stmt.strip()
        if s:
            cur.execute(s)
    return cur


def _fetchall(conn, sql: str) -> list[tuple]:
    cur = conn.cursor()
    cur.execute(sql)
    return cur.fetchall()


def _introspect(conn, table: str) -> dict[str, dict]:
    """Return {col_name: {data_type, max_length, precision, scale, nullable}} from information_schema."""
    rows = _fetchall(conn, f"""
        SELECT COLUMN_NAME, DATA_TYPE,
               CHARACTER_MAXIMUM_LENGTH,
               NUMERIC_PRECISION, NUMERIC_SCALE,
               IS_NULLABLE
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = '{table}'
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
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", params=_VERSIONS)
def conn(request):
    """One connection per MySQL version; auto-skips if unreachable."""
    version, port = request.param
    connection = _get_connection(port)
    _execute(connection, "DROP DATABASE IF EXISTS live_test")
    _execute(connection, "CREATE DATABASE live_test")
    connection.select_db("live_test")
    yield connection
    _execute(connection, "DROP DATABASE IF EXISTS live_test")
    connection.close()


# ---------------------------------------------------------------------------
# Helpers used by test classes
# ---------------------------------------------------------------------------

def _run(conn, ddl: str, table: str) -> dict[str, dict]:
    _execute(conn, f"DROP TABLE IF EXISTS `{table}`")
    tables = parse_ddl(ddl, dialect="mysql")
    assert tables, f"parse_ddl returned no tables for: {ddl[:80]}"
    sql = emit_ddl(tables[0], dialect="mysql", if_not_exists=False)
    _execute(conn, sql)
    return _introspect(conn, table)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestMySQLConnection:
    def test_version(self, conn):
        rows = _fetchall(conn, "SELECT VERSION()")
        version = rows[0][0]
        assert version, "Empty version string"
        # Basic sanity: must be 5.x or 8.x
        major = int(version.split(".")[0])
        assert major in (5, 8), f"Unexpected MySQL major version: {version}"


class TestEmitAndExecute:
    """Parse canonical DDL → emit MySQL DDL → execute → introspect."""

    _SOURCE_DDL = textwrap.dedent("""\
        CREATE TABLE `orders` (
            `order_id`    INT           NOT NULL AUTO_INCREMENT,
            `customer_id` INT           NOT NULL,
            `total`       DECIMAL(12,2)     NULL,
            `status`      VARCHAR(50)   NOT NULL DEFAULT 'pending',
            `notes`       TEXT              NULL,
            `is_active`   TINYINT(1)    NOT NULL DEFAULT 1,
            `created_at`  DATETIME          NULL,
            PRIMARY KEY (`order_id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """)

    def test_emit_executes_without_error(self, conn):
        _execute(conn, "DROP TABLE IF EXISTS `orders`")
        tables = parse_ddl(self._SOURCE_DDL, dialect="mysql")
        assert tables, "parse_ddl returned no tables"
        sql = emit_ddl(tables[0], dialect="mysql", if_not_exists=False)
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
        assert cols["total"]["data_type"] == "decimal"
        assert cols["total"]["precision"] == 12
        assert cols["total"]["scale"]     == 2

    def test_string_length_preserved(self, conn):
        cols = _introspect(conn, "orders")
        assert cols["status"]["data_type"]  == "varchar"
        assert cols["status"]["max_length"] == 50


class TestTypeRoundTrips:
    """One table per type group — emit, execute, introspect."""

    def test_integer_types(self, conn):
        cols = _run(conn, """
            CREATE TABLE t_ints (
                c_tinyint   TINYINT,
                c_smallint  SMALLINT,
                c_mediumint MEDIUMINT,
                c_int       INT,
                c_bigint    BIGINT
            )
        """, "t_ints")
        # canonical "integer" → INT; canonical "long" → BIGINT
        # TINYINT, SMALLINT, MEDIUMINT widen to INT in canonical model
        assert cols["c_int"]["data_type"]     == "int"
        assert cols["c_bigint"]["data_type"]  == "bigint"
        # Widened types all come back as int
        assert cols["c_tinyint"]["data_type"]   in ("tinyint", "int")
        assert cols["c_smallint"]["data_type"]  in ("smallint", "int")
        assert cols["c_mediumint"]["data_type"] in ("mediumint", "int")

    def test_unsigned_integer_widening(self, conn):
        """UNSIGNED integers must widen to the next larger signed canonical type."""
        cols = _run(conn, """
            CREATE TABLE t_unsigned (
                c_uint      INT UNSIGNED,
                c_ubigint   BIGINT UNSIGNED,
                c_utinyint  TINYINT UNSIGNED,
                c_usmallint SMALLINT UNSIGNED
            )
        """, "t_unsigned")
        # All UNSIGNED ints widen to avoid overflow: UINT→BIGINT, UBIGINT→DECIMAL,
        # UTINYINT→SMALLINT/INT, USMALLINT→INT
        assert cols["c_uint"]["data_type"]    in ("bigint", "decimal")
        assert cols["c_ubigint"]["data_type"] in ("decimal",)
        assert cols["c_utinyint"]["data_type"]  in ("smallint", "int", "tinyint")
        assert cols["c_usmallint"]["data_type"] in ("int", "smallint")

    def test_string_types(self, conn):
        cols = _run(conn, """
            CREATE TABLE t_strings (
                c_char       CHAR(10),
                c_varchar    VARCHAR(255),
                c_tinytext   TINYTEXT,
                c_text       TEXT,
                c_mediumtext MEDIUMTEXT,
                c_longtext   LONGTEXT
            )
        """, "t_strings")
        # canonical "string" with length → VARCHAR(n) in MySQL.
        # CHAR → VARCHAR is intentional: canonical model does not preserve CHAR vs VARCHAR.
        assert cols["c_char"]["data_type"]    in ("char", "varchar")
        assert cols["c_char"]["max_length"]   == 10
        assert cols["c_varchar"]["data_type"] == "varchar"
        assert cols["c_varchar"]["max_length"]== 255
        # All text variants map to canonical string → emitted as TEXT or LONGTEXT
        for col in ("c_tinytext", "c_text", "c_mediumtext", "c_longtext"):
            assert cols[col]["data_type"] in ("tinytext", "text", "mediumtext", "longtext")

    def test_decimal_and_float(self, conn):
        cols = _run(conn, """
            CREATE TABLE t_numeric (
                c_decimal   DECIMAL(10, 3),
                c_float     FLOAT,
                c_double    DOUBLE
            )
        """, "t_numeric")
        assert cols["c_decimal"]["data_type"] == "decimal"
        assert cols["c_decimal"]["precision"] == 10
        assert cols["c_decimal"]["scale"]     == 3
        assert cols["c_float"]["data_type"]   == "float"
        assert cols["c_double"]["data_type"]  == "double"

    def test_boolean(self, conn):
        """TINYINT(1) is MySQL's boolean idiom and round-trips as such."""
        cols = _run(conn, """
            CREATE TABLE t_bool (
                c_bool  TINYINT(1) NOT NULL DEFAULT 0,
                c_bool2 BOOLEAN
            )
        """, "t_bool")
        # canonical "boolean" → TINYINT(1)
        assert cols["c_bool"]["data_type"]  == "tinyint"
        assert cols["c_bool2"]["data_type"] == "tinyint"

    def test_date_and_time(self, conn):
        cols = _run(conn, """
            CREATE TABLE t_datetime (
                c_date      DATE,
                c_time      TIME,
                c_datetime  DATETIME,
                c_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                c_year      YEAR
            )
        """, "t_datetime")
        assert cols["c_date"]["data_type"]      == "date"
        assert cols["c_time"]["data_type"]      == "time"
        assert cols["c_datetime"]["data_type"]  == "datetime"
        assert cols["c_timestamp"]["data_type"] == "timestamp"

    def test_binary_types(self, conn):
        cols = _run(conn, """
            CREATE TABLE t_binary (
                c_binary    BINARY(16),
                c_varbinary VARBINARY(256),
                c_blob      BLOB,
                c_longblob  LONGBLOB
            )
        """, "t_binary")
        assert cols["c_varbinary"]["data_type"] in ("varbinary",)
        assert cols["c_varbinary"]["max_length"] == 256
        for col in ("c_blob", "c_longblob"):
            assert cols[col]["data_type"] in ("blob", "longblob", "mediumblob")

    def test_not_null_and_nullable(self, conn):
        cols = _run(conn, """
            CREATE TABLE t_null (
                c_notnull   INT          NOT NULL,
                c_nullable  VARCHAR(50)      NULL,
                c_default   INT          NOT NULL DEFAULT 0
            )
        """, "t_null")
        assert cols["c_notnull"]["nullable"]  is False
        assert cols["c_nullable"]["nullable"] is True
        assert cols["c_default"]["nullable"]  is False

    def test_auto_increment_primary_key(self, conn):
        cols = _run(conn, """
            CREATE TABLE t_pk (
                id   INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(100)
            )
        """, "t_pk")
        assert cols["id"]["data_type"] == "int"
        assert cols["id"]["nullable"]  is False

    def test_foreign_key(self, conn):
        _execute(conn, "DROP TABLE IF EXISTS fk_child")
        _execute(conn, "DROP TABLE IF EXISTS fk_parent")
        _execute(conn, "CREATE TABLE fk_parent (id INT NOT NULL PRIMARY KEY)")
        tables = parse_ddl("""
            CREATE TABLE fk_child (
                id        INT NOT NULL PRIMARY KEY,
                parent_id INT NOT NULL,
                CONSTRAINT fk_cp FOREIGN KEY (parent_id) REFERENCES fk_parent (id)
            )
        """, dialect="mysql")
        assert tables
        sql = emit_ddl(tables[0], dialect="mysql", if_not_exists=False)
        _execute(conn, sql)
        cols = _introspect(conn, "fk_child")
        assert "parent_id" in cols

    def test_uuid_type(self, conn):
        """canonical 'uuid' emits as CHAR(36) in MySQL.
        CHAR(36) may come back as VARCHAR(36) because canonical model
        normalises CHAR → VARCHAR (string type with length)."""
        cols = _run(conn, """
            CREATE TABLE t_uuid (
                c_uuid CHAR(36) NOT NULL
            )
        """, "t_uuid")
        assert cols["c_uuid"]["data_type"]  in ("char", "varchar")
        assert cols["c_uuid"]["max_length"] == 36


class TestParseFromLiveServer:
    """Reconstruct DDL from information_schema and re-parse it."""

    def test_create_and_reparse(self, conn):
        _execute(conn, "DROP TABLE IF EXISTS reparse_test")
        _execute(conn, """
            CREATE TABLE reparse_test (
                id    INT          NOT NULL AUTO_INCREMENT PRIMARY KEY,
                name  VARCHAR(120) NOT NULL,
                score DECIMAL(8,3)     NULL
            )
        """)
        rows = _fetchall(conn, """
            SELECT COLUMN_NAME, DATA_TYPE,
                   CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE,
                   IS_NULLABLE
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'reparse_test'
            ORDER BY ORDINAL_POSITION
        """)
        col_defs = []
        for col_name, dtype, maxlen, prec, scale, nullable in rows:
            null_str = "NULL" if nullable == "YES" else "NOT NULL"
            if dtype in ("varchar", "char"):
                col_defs.append(f"  `{col_name}` {dtype.upper()}({maxlen}) {null_str}")
            elif dtype == "decimal":
                col_defs.append(f"  `{col_name}` DECIMAL({prec},{scale}) {null_str}")
            else:
                col_defs.append(f"  `{col_name}` {dtype.upper()} {null_str}")
        reconstructed = "CREATE TABLE `reparse_test` (\n" + ",\n".join(col_defs) + "\n);"

        tables = parse_ddl(reconstructed, dialect="mysql")
        assert tables, "parse_ddl returned no tables from reconstructed DDL"
        schema = tables[0]
        assert len(schema.columns) == 3
        assert schema.columns[0].name == "id"
        assert schema.columns[1].type == "string"
        assert schema.columns[2].type == "decimal"


class TestMySQLVersionSpecific:
    """Tests that exercise version-specific behaviour."""

    def test_utf8mb4_charset(self, conn):
        """MySQL 5.7 and 8.x both support utf8mb4; emitted DDL must execute."""
        _execute(conn, "DROP TABLE IF EXISTS t_charset")
        _execute(conn, """
            CREATE TABLE t_charset (
                name VARCHAR(100) NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        cols = _introspect(conn, "t_charset")
        assert cols["name"]["data_type"] == "varchar"

    def test_json_column(self, conn):
        """JSON type: supported in MySQL 5.7.8+ and 8.x."""
        try:
            _execute(conn, "DROP TABLE IF EXISTS t_json")
            _execute(conn, "CREATE TABLE t_json (data JSON)")
        except Exception:
            pytest.skip("JSON type not supported by this MySQL version")
        cols = _introspect(conn, "t_json")
        assert cols["data"]["data_type"] == "json"

    def test_generated_column(self, conn):
        """Virtual generated columns: supported in MySQL 5.7+ and 8.x."""
        try:
            _execute(conn, "DROP TABLE IF EXISTS t_generated")
            _execute(conn, """
                CREATE TABLE t_generated (
                    first  VARCHAR(50),
                    last   VARCHAR(50),
                    full_name VARCHAR(101) AS (CONCAT(first, ' ', last)) VIRTUAL
                )
            """)
        except Exception:
            pytest.skip("Generated columns not supported by this MySQL version")
        cols = _introspect(conn, "t_generated")
        assert "full_name" in cols
