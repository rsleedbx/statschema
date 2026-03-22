"""
Live MariaDB integration tests – runs against real MariaDB 10.11 (LTS) and 11.4 instances.

Tests verify that DDL emitted by emit_ddl(dialect="mysql") executes without errors on a real
MariaDB server and that types, nullability, and constraints survive the parse → emit → execute →
introspect cycle.  Also confirms that the "mariadb" dialect alias and schema_source tag resolve
correctly through the dialect registry.

Prerequisites
-------------
Two MariaDB containers via Podman (see docs/local-databases.md):

    podman run -d --name mariadb1011 \\
        -e MARIADB_ROOT_PASSWORD=testpass -e MARIADB_DATABASE=testdb \\
        -p 3310:3306 docker.io/library/mariadb:10.11

    podman run -d --name mariadb114 \\
        -e MARIADB_ROOT_PASSWORD=testpass -e MARIADB_DATABASE=testdb \\
        -p 3311:3306 docker.io/library/mariadb:11.4

Environment variables (defaults match the Podman commands above):

    MARIADB_HOST      default: 127.0.0.1
    MARIADB_ROOT_PASS default: testpass
    MARIADB_LTS_PORT  default: 3310   (MariaDB 10.11 LTS)
    MARIADB_NEW_PORT  default: 3311   (MariaDB 11.4)

Skip behaviour
--------------
Each parametrised version skips automatically when its port is unreachable or pymysql is not
installed — so ``make test`` always completes cleanly.

Known type normalizations (MariaDB vs MySQL)
--------------------------------------------
MariaDB uses ``longtext`` / ``longblob`` for the unbounded text/blob types in the canonical model;
``information_schema`` reports them the same way MySQL does.  The main divergence is:

- ``TINYINT(1)`` is MariaDB's boolean idiom (same as MySQL).
- MariaDB 10.7+ has a native ``UUID`` type; ``information_schema`` reports ``DATA_TYPE = 'uuid'``.
  On earlier versions, UUID is typically stored as ``CHAR(36)`` or ``VARCHAR(36)``.
- ``BIGINT`` in MariaDB information_schema is reported as ``bigint`` (same as MySQL).
- ``INT UNSIGNED`` widening follows MySQL semantics.
"""

import os
import textwrap

import pytest

from src.statschema import parse_ddl, emit_ddl, load_schema

# ---------------------------------------------------------------------------
# Connection config
# ---------------------------------------------------------------------------

_HOST      = os.environ.get("MARIADB_HOST",      "127.0.0.1")
_ROOT_PASS = os.environ.get("MARIADB_ROOT_PASS", "testpass")
_PORT_LTS  = int(os.environ.get("MARIADB_LTS_PORT", "3310"))
_PORT_NEW  = int(os.environ.get("MARIADB_NEW_PORT",  "3311"))

_VERSIONS = [
    pytest.param(("10.11", _PORT_LTS), id="mariadb1011"),
    pytest.param(("11.4",  _PORT_NEW), id="mariadb114"),
]


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

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
        pytest.skip(f"Cannot connect to MariaDB on {_HOST}:{port}: {exc}")


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
    """One connection per MariaDB version; auto-skips if unreachable."""
    _version, port = request.param
    connection = _get_connection(port)
    _execute(connection, "DROP DATABASE IF EXISTS live_test")
    _execute(connection, "CREATE DATABASE live_test")
    connection.select_db("live_test")
    yield connection
    _execute(connection, "DROP DATABASE IF EXISTS live_test")
    connection.close()


# ---------------------------------------------------------------------------
# Helpers
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

class TestMariaDBConnection:
    def test_version(self, conn):
        rows = _fetchall(conn, "SELECT VERSION()")
        version = rows[0][0]
        assert version, "Empty version string"
        assert "MariaDB" in version, f"VERSION() does not contain 'MariaDB': {version!r}"


class TestMariaDBDialectAlias:
    """Dialect alias 'mariadb' must resolve to 'mysql' in parse/emit round-trips."""

    def test_parse_with_mariadb_alias(self, conn):
        ddl = textwrap.dedent("""\
            CREATE TABLE `alias_test` (
                `id`   INT NOT NULL AUTO_INCREMENT,
                `name` VARCHAR(100) NOT NULL,
                PRIMARY KEY (`id`)
            ) ENGINE=InnoDB;
        """)
        # parse with the 'mariadb' alias — must not raise
        tables = parse_ddl(ddl, dialect="mariadb")
        assert tables, "parse_ddl returned no tables when dialect='mariadb'"
        # emit back using alias — must produce executable SQL
        sql = emit_ddl(tables[0], dialect="mariadb", if_not_exists=False)
        _execute(conn, "DROP TABLE IF EXISTS `alias_test`")
        _execute(conn, sql)
        cols = _introspect(conn, "alias_test")
        assert "id"   in cols
        assert "name" in cols

    def test_schema_source_mariadb_resolves_to_mysql(self):
        """schema_source: mariadb in YAML/dict must map to SchemaSource.MYSQL."""
        from src.statschema.loader import get_schema_source_from_data, SchemaSource
        for tag in ("mariadb", "maria", "mariadb_columnstore"):
            result = get_schema_source_from_data({"schema_source": tag})
            assert result == SchemaSource.MYSQL, (
                f"schema_source={tag!r} resolved to {result!r}, expected SchemaSource.MYSQL"
            )

    def test_dialect_alias_normalize(self):
        """normalize_dialect must map mariadb variants to 'mysql'."""
        from src.statschema import normalize_dialect
        assert normalize_dialect("mariadb")           == "mysql"
        assert normalize_dialect("maria")             == "mysql"
        assert normalize_dialect("mariadb_columnstore") == "mysql"
        assert normalize_dialect("MARIADB")           == "mysql"


class TestEmitAndExecute:
    """Parse canonical DDL → emit MySQL DDL → execute on MariaDB → introspect."""

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
    """One table per type group — emit, execute on MariaDB, introspect."""

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
        assert cols["c_int"]["data_type"]    == "int"
        assert cols["c_bigint"]["data_type"] == "bigint"
        assert cols["c_tinyint"]["data_type"]   in ("tinyint", "int")
        assert cols["c_smallint"]["data_type"]  in ("smallint", "int")
        assert cols["c_mediumint"]["data_type"] in ("mediumint", "int")

    def test_string_types(self, conn):
        cols = _run(conn, """
            CREATE TABLE t_strings (
                c_char       CHAR(10),
                c_varchar    VARCHAR(255),
                c_text       TEXT,
                c_longtext   LONGTEXT
            )
        """, "t_strings")
        assert cols["c_char"]["data_type"]     in ("char", "varchar")
        assert cols["c_char"]["max_length"]    == 10
        assert cols["c_varchar"]["data_type"]  == "varchar"
        assert cols["c_varchar"]["max_length"] == 255
        # Both TEXT and LONGTEXT round-trip to canonical "string", which emits as TEXT.
        # information_schema therefore reports 'text' for both.
        assert cols["c_text"]["data_type"]     in ("text", "mediumtext", "longtext")
        assert cols["c_longtext"]["data_type"] in ("text", "longtext")

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
        cols = _run(conn, """
            CREATE TABLE t_bool (
                c_bool  TINYINT(1) NOT NULL DEFAULT 0,
                c_bool2 BOOLEAN
            )
        """, "t_bool")
        assert cols["c_bool"]["data_type"]  == "tinyint"
        assert cols["c_bool2"]["data_type"] == "tinyint"

    def test_date_and_time(self, conn):
        cols = _run(conn, """
            CREATE TABLE t_datetime (
                c_date      DATE,
                c_time      TIME,
                c_datetime  DATETIME,
                c_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
        assert cols["c_varbinary"]["data_type"] == "varbinary"
        assert cols["c_blob"]["data_type"]      in ("blob", "longblob")
        assert cols["c_longblob"]["data_type"]  == "longblob"

    def test_json_type(self, conn):
        """MariaDB supports JSON (stored as LONGTEXT with a JSON_VALID constraint internally)."""
        cols = _run(conn, """
            CREATE TABLE t_json (
                c_json JSON
            )
        """, "t_json")
        # MariaDB implements JSON as LONGTEXT with a JSON_VALID CHECK constraint.
        # After the parse→canonical→emit round-trip, emit_ddl outputs TEXT (the canonical
        # unbounded string), so information_schema reports 'text'.  Allow all observed values.
        assert cols["c_json"]["data_type"] in ("json", "longtext", "text"), (
            f"Unexpected JSON type: {cols['c_json']['data_type']!r}"
        )


class TestMariaDBRoundTrip:
    """Full parse → emit → execute → re-parse cycle on MariaDB."""

    def test_full_round_trip(self, conn):
        ddl = textwrap.dedent("""\
            CREATE TABLE `products` (
                `product_id`  INT           NOT NULL AUTO_INCREMENT,
                `sku`         VARCHAR(100)  NOT NULL,
                `price`       DECIMAL(10,2) NOT NULL DEFAULT 0.00,
                `description` TEXT              NULL,
                `in_stock`    TINYINT(1)    NOT NULL DEFAULT 1,
                `created_at`  DATETIME          NULL,
                PRIMARY KEY (`product_id`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)
        tables = parse_ddl(ddl, dialect="mysql")
        assert tables
        emitted = emit_ddl(tables[0], dialect="mysql", if_not_exists=False)
        _execute(conn, "DROP TABLE IF EXISTS `products`")
        _execute(conn, emitted)
        cols = _introspect(conn, "products")
        assert cols["product_id"]["nullable"] is False
        assert cols["sku"]["data_type"]       == "varchar"
        assert cols["sku"]["max_length"]      == 100
        assert cols["price"]["data_type"]     == "decimal"
        assert cols["price"]["precision"]     == 10
        assert cols["price"]["scale"]         == 2
        assert cols["in_stock"]["data_type"]  == "tinyint"

    def test_schema_source_mariadb_load(self, conn):
        """load_schema with schema_source: mariadb uses MySQL DDL parsing."""
        data = {
            "schema_source": "mariadb",
            "ddl": textwrap.dedent("""\
                CREATE TABLE `events` (
                    `event_id`  BIGINT       NOT NULL AUTO_INCREMENT,
                    `name`      VARCHAR(200) NOT NULL,
                    `payload`   LONGTEXT         NULL,
                    PRIMARY KEY (`event_id`)
                ) ENGINE=InnoDB;
            """),
        }
        tables = load_schema(data)
        assert tables, "load_schema with schema_source: mariadb returned no tables"
        t = tables[0]
        col_names = [c.name for c in t.columns]
        assert "event_id" in col_names
        assert "name"     in col_names


class TestMariaDBSpecific:
    """Features that differ between MariaDB and MySQL."""

    def test_mariadb_sequence_via_autoincrement(self, conn):
        """AUTO_INCREMENT primary key survives the round-trip unchanged."""
        cols = _run(conn, """
            CREATE TABLE t_seq (
                id   BIGINT NOT NULL AUTO_INCREMENT,
                val  VARCHAR(50),
                PRIMARY KEY (id)
            )
        """, "t_seq")
        assert cols["id"]["data_type"]    == "bigint"
        assert cols["id"]["nullable"]     is False

    def test_mariadb_column_default_expressions(self, conn):
        """MariaDB 10.2+ supports DEFAULT (expr) for most types."""
        cols = _run(conn, """
            CREATE TABLE t_defaults (
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
        """, "t_defaults")
        assert cols["created_at"]["data_type"] == "datetime"
        assert cols["updated_at"]["data_type"] == "datetime"

    def test_charset_and_collation(self, conn):
        """utf8mb4 charset tables execute cleanly on MariaDB."""
        ddl = textwrap.dedent("""\
            CREATE TABLE `t_charset` (
                `id`   INT NOT NULL AUTO_INCREMENT,
                `name` VARCHAR(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci,
                PRIMARY KEY (`id`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """)
        _execute(conn, "DROP TABLE IF EXISTS `t_charset`")
        tables = parse_ddl(ddl, dialect="mysql")
        sql = emit_ddl(tables[0], dialect="mysql", if_not_exists=False)
        _execute(conn, sql)
        cols = _introspect(conn, "t_charset")
        assert cols["name"]["data_type"] == "varchar"
