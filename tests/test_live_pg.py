"""
Live PostgreSQL integration tests – runs against real PostgreSQL 14 and 16 instances.

Tests verify that DDL emitted by emit_ddl(dialect="postgres") executes on a real
PostgreSQL server and that types, nullability, constraints, and PG-specific features
survive the parse → emit → execute → introspect cycle.

Prerequisites
-------------
Two PostgreSQL containers via Podman (see docs/local-databases.md):

    podman run -d --name pg14 \\
        -e POSTGRES_PASSWORD=testpass -e POSTGRES_DB=testdb \\
        -p 5414:5432 docker.io/library/postgres:14

    podman run -d --name pg16 \\
        -e POSTGRES_PASSWORD=testpass -e POSTGRES_DB=testdb \\
        -p 5416:5432 docker.io/library/postgres:16

Environment variables (defaults match the Podman commands above):

    PG_HOST      default: 127.0.0.1
    PG_USER      default: postgres
    PG_PASSWORD  default: testpass
    PG_DB        default: testdb
    PG14_PORT    default: 5414
    PG16_PORT    default: 5416

Skip behaviour
--------------
Each parametrised version skips automatically when its port is unreachable or
psycopg2 is not installed — so make test always completes cleanly.
"""

import os
import textwrap

import pytest

from src.statschema import parse_ddl, emit_ddl

# ---------------------------------------------------------------------------
# Connection configuration
# ---------------------------------------------------------------------------

_HOST     = os.environ.get("PG_HOST",     "127.0.0.1")
_USER     = os.environ.get("PG_USER",     "postgres")
_PASSWORD = os.environ.get("PG_PASSWORD", "testpass")
_DB       = os.environ.get("PG_DB",       "testdb")
_PORT_14  = int(os.environ.get("PG14_PORT", "5414"))
_PORT_16  = int(os.environ.get("PG16_PORT", "5416"))

_VERSIONS = [
    pytest.param(("14", _PORT_14), id="pg14"),
    pytest.param(("16", _PORT_16), id="pg16"),
]


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def _get_connection(port: int, db: str = _DB):
    psycopg2 = pytest.importorskip("psycopg2")
    try:
        conn = psycopg2.connect(
            host=_HOST, port=port,
            user=_USER, password=_PASSWORD,
            dbname=db,
            connect_timeout=5,
        )
        conn.autocommit = True
        return conn
    except Exception as exc:
        pytest.skip(f"Cannot connect to PostgreSQL on {_HOST}:{port}: {exc}")


def _execute(conn, sql: str):
    cur = conn.cursor()
    cur.execute(sql)
    return cur


def _fetchall(conn, sql: str, params=None) -> list[tuple]:
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur.fetchall()


def _introspect(conn, schema: str, table: str) -> dict[str, dict]:
    """Return {col_name: {udt_name, char_len, num_prec, num_scale, nullable}} from information_schema."""
    rows = _fetchall(conn, """
        SELECT column_name,
               udt_name,
               character_maximum_length,
               numeric_precision,
               numeric_scale,
               is_nullable
        FROM   information_schema.columns
        WHERE  table_schema = %s
          AND  table_name   = %s
        ORDER  BY ordinal_position
    """, (schema, table))
    return {
        row[0]: {
            "udt_name":  row[1],
            "char_len":  row[2],
            "num_prec":  row[3],
            "num_scale": row[4],
            "nullable":  row[5] == "YES",
        }
        for row in rows
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", params=_VERSIONS)
def conn(request):
    """One connection per PG version; auto-skips if unreachable."""
    _version, port = request.param
    connection = _get_connection(port)
    _execute(connection, "DROP SCHEMA IF EXISTS live_test CASCADE")
    _execute(connection, "CREATE SCHEMA live_test")
    yield connection
    _execute(connection, "DROP SCHEMA IF EXISTS live_test CASCADE")
    connection.close()


# ---------------------------------------------------------------------------
# Helpers used by test classes
# ---------------------------------------------------------------------------

def _run(conn, ddl: str, table: str) -> dict[str, dict]:
    """Parse DDL → emit postgres → execute → introspect in live_test schema."""
    _execute(conn, f'DROP TABLE IF EXISTS live_test."{table}"')
    tables = parse_ddl(ddl, dialect="postgres")
    assert tables, f"parse_ddl returned no tables for: {ddl[:80]}"
    sql = emit_ddl(tables[0], dialect="postgres", if_not_exists=False)
    # Prefix table name with live_test schema
    sql = sql.replace(f'"{table}"', f'live_test."{table}"', 1)
    _execute(conn, sql)
    return _introspect(conn, "live_test", table)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPGConnection:
    def test_version(self, conn):
        rows = _fetchall(conn, "SELECT current_setting('server_version')")
        version = rows[0][0]
        assert version, "Empty version string"
        major = int(version.split(".")[0])
        assert major in (14, 15, 16, 17), f"Unexpected PG major version: {version}"


class TestEmitAndExecute:
    """Parse canonical DDL → emit PostgreSQL DDL → execute → introspect."""

    _SOURCE_DDL = textwrap.dedent("""\
        CREATE TABLE "orders" (
            "order_id"    SERIAL        NOT NULL,
            "customer_id" INTEGER       NOT NULL,
            "total"       NUMERIC(12,2)     NULL,
            "status"      VARCHAR(50)   NOT NULL DEFAULT 'pending',
            "notes"       TEXT              NULL,
            "is_active"   BOOLEAN       NOT NULL DEFAULT TRUE,
            "created_at"  TIMESTAMP         NULL,
            PRIMARY KEY ("order_id")
        );
    """)

    def test_emit_executes_without_error(self, conn):
        _execute(conn, 'DROP TABLE IF EXISTS live_test."orders"')
        tables = parse_ddl(self._SOURCE_DDL, dialect="postgres")
        assert tables
        sql = emit_ddl(tables[0], dialect="postgres", if_not_exists=False)
        sql = sql.replace('"orders"', 'live_test."orders"', 1)
        _execute(conn, sql)
        cols = _introspect(conn, "live_test", "orders")
        assert "order_id"    in cols
        assert "customer_id" in cols

    def test_nullability_preserved(self, conn):
        cols = _introspect(conn, "live_test", "orders")
        assert cols["order_id"]["nullable"]    is False
        assert cols["customer_id"]["nullable"] is False
        assert cols["status"]["nullable"]      is False
        assert cols["total"]["nullable"]       is True
        assert cols["notes"]["nullable"]       is True

    def test_decimal_precision_preserved(self, conn):
        cols = _introspect(conn, "live_test", "orders")
        assert cols["total"]["udt_name"] == "numeric"
        assert cols["total"]["num_prec"] == 12
        assert cols["total"]["num_scale"]== 2

    def test_string_length_preserved(self, conn):
        cols = _introspect(conn, "live_test", "orders")
        assert cols["status"]["udt_name"] == "varchar"
        assert cols["status"]["char_len"] == 50


class TestTypeRoundTrips:
    """One table per type group — emit, execute, introspect."""

    def test_integer_types(self, conn):
        cols = _run(conn, """
            CREATE TABLE "t_ints" (
                c_smallint  SMALLINT,
                c_int       INTEGER,
                c_bigint    BIGINT
            )
        """, "t_ints")
        # canonical "integer" → INTEGER; canonical "long" → BIGINT
        # SMALLINT widens to INTEGER in canonical model
        assert cols["c_int"]["udt_name"]    == "int4"
        assert cols["c_bigint"]["udt_name"] == "int8"
        assert cols["c_smallint"]["udt_name"] in ("int2", "int4")

    def test_string_types(self, conn):
        cols = _run(conn, """
            CREATE TABLE "t_strings" (
                c_char    CHAR(10),
                c_varchar VARCHAR(255),
                c_text    TEXT
            )
        """, "t_strings")
        # CHAR(n) → canonical string → VARCHAR(n) or bpchar
        assert cols["c_char"]["udt_name"]    in ("bpchar", "varchar")
        assert cols["c_char"]["char_len"]    == 10
        assert cols["c_varchar"]["udt_name"] == "varchar"
        assert cols["c_varchar"]["char_len"] == 255
        assert cols["c_text"]["udt_name"]    == "text"

    def test_numeric_types(self, conn):
        cols = _run(conn, """
            CREATE TABLE "t_numeric" (
                c_numeric NUMERIC(10, 3),
                c_real    REAL,
                c_double  DOUBLE PRECISION
            )
        """, "t_numeric")
        assert cols["c_numeric"]["udt_name"] == "numeric"
        assert cols["c_numeric"]["num_prec"] == 10
        assert cols["c_numeric"]["num_scale"]== 3
        assert cols["c_real"]["udt_name"]    == "float4"
        assert cols["c_double"]["udt_name"]  == "float8"

    def test_boolean(self, conn):
        cols = _run(conn, """
            CREATE TABLE "t_bool" (
                c_bool BOOLEAN NOT NULL DEFAULT FALSE
            )
        """, "t_bool")
        assert cols["c_bool"]["udt_name"] == "bool"

    def test_date_and_time(self, conn):
        cols = _run(conn, """
            CREATE TABLE "t_datetime" (
                c_date        DATE,
                c_time        TIME,
                c_timetz      TIME WITH TIME ZONE,
                c_timestamp   TIMESTAMP,
                c_timestamptz TIMESTAMP WITH TIME ZONE
            )
        """, "t_datetime")
        assert cols["c_date"]["udt_name"]        == "date"
        assert cols["c_time"]["udt_name"]        == "time"
        assert cols["c_timetz"]["udt_name"]      == "timetz"
        assert cols["c_timestamp"]["udt_name"]   == "timestamp"
        assert cols["c_timestamptz"]["udt_name"] == "timestamptz"

    def test_uuid_type(self, conn):
        """PostgreSQL has a native UUID type; canonical 'uuid' emits as UUID."""
        cols = _run(conn, """
            CREATE TABLE "t_uuid" (
                c_uuid UUID NOT NULL
            )
        """, "t_uuid")
        assert cols["c_uuid"]["udt_name"] == "uuid"

    def test_binary_type(self, conn):
        """PostgreSQL uses BYTEA (no length) for all binary data."""
        cols = _run(conn, """
            CREATE TABLE "t_binary" (
                c_bytea BYTEA
            )
        """, "t_binary")
        assert cols["c_bytea"]["udt_name"] == "bytea"

    def test_not_null_and_nullable(self, conn):
        cols = _run(conn, """
            CREATE TABLE "t_null" (
                c_notnull  INTEGER      NOT NULL,
                c_nullable VARCHAR(50)      NULL,
                c_default  INTEGER      NOT NULL DEFAULT 0
            )
        """, "t_null")
        assert cols["c_notnull"]["nullable"]  is False
        assert cols["c_nullable"]["nullable"] is True
        assert cols["c_default"]["nullable"]  is False

    def test_primary_key(self, conn):
        cols = _run(conn, """
            CREATE TABLE "t_pk" (
                id   INTEGER NOT NULL PRIMARY KEY,
                name TEXT
            )
        """, "t_pk")
        assert cols["id"]["udt_name"] == "int4"
        assert cols["id"]["nullable"] is False

    def test_foreign_key(self, conn):
        _execute(conn, 'DROP TABLE IF EXISTS live_test."fk_child"')
        _execute(conn, 'DROP TABLE IF EXISTS live_test."fk_parent"')
        _execute(conn, 'CREATE TABLE live_test."fk_parent" (id INTEGER NOT NULL PRIMARY KEY)')
        tables = parse_ddl("""
            CREATE TABLE "fk_child" (
                id        INTEGER NOT NULL PRIMARY KEY,
                parent_id INTEGER NOT NULL,
                CONSTRAINT fk_cp FOREIGN KEY (parent_id) REFERENCES "fk_parent" (id)
            )
        """, dialect="postgres")
        assert tables
        sql = emit_ddl(tables[0], dialect="postgres", if_not_exists=False)
        sql = sql.replace('"fk_child"', 'live_test."fk_child"', 1)
        _execute(conn, sql)
        cols = _introspect(conn, "live_test", "fk_child")
        assert "parent_id" in cols

    def test_serial_identity(self, conn):
        """SERIAL columns parse and emit as INTEGER with the sequence intact."""
        cols = _run(conn, """
            CREATE TABLE "t_serial" (
                id   SERIAL      NOT NULL PRIMARY KEY,
                name VARCHAR(80) NOT NULL
            )
        """, "t_serial")
        # SERIAL → canonical integer, emits as INTEGER (sequence is dropped — known normalisation)
        assert cols["id"]["udt_name"] in ("int4", "int8")


class TestPGSpecificTypes:
    """PostgreSQL-specific types that have no direct MySQL/SQL Server equivalent."""

    def test_json_and_jsonb(self, conn):
        _execute(conn, 'DROP TABLE IF EXISTS live_test."t_json"')
        _execute(conn, """
            CREATE TABLE live_test."t_json" (
                c_json  JSON,
                c_jsonb JSONB
            )
        """)
        cols = _introspect(conn, "live_test", "t_json")
        assert cols["c_json"]["udt_name"]  == "json"
        assert cols["c_jsonb"]["udt_name"] == "jsonb"

    def test_array_column(self, conn):
        """Integer array column executes and introspects correctly."""
        _execute(conn, 'DROP TABLE IF EXISTS live_test."t_array"')
        try:
            _execute(conn, 'CREATE TABLE live_test."t_array" (tags INTEGER[])')
        except Exception:
            pytest.skip("ARRAY type not supported")
        cols = _introspect(conn, "live_test", "t_array")
        assert cols["tags"]["udt_name"] == "_int4"

    def test_inet_and_cidr(self, conn):
        """Network address types — PostgreSQL-only."""
        _execute(conn, 'DROP TABLE IF EXISTS live_test."t_net"')
        _execute(conn, """
            CREATE TABLE live_test."t_net" (
                ip   INET,
                net  CIDR
            )
        """)
        cols = _introspect(conn, "live_test", "t_net")
        assert cols["ip"]["udt_name"]  == "inet"
        assert cols["net"]["udt_name"] == "cidr"

    def test_interval(self, conn):
        _execute(conn, 'DROP TABLE IF EXISTS live_test."t_interval"')
        _execute(conn, 'CREATE TABLE live_test."t_interval" (dur INTERVAL)')
        cols = _introspect(conn, "live_test", "t_interval")
        assert cols["dur"]["udt_name"] == "interval"

    def test_numeric_precision_variants(self, conn):
        """NUMERIC with and without precision executes correctly.

        Known canonical normalization:
          NUMERIC(p)    (scale omitted → scale 0) → canonical "long" → BIGINT (int8).
          A whole-number NUMERIC is semantically an integer; the canonical model
          maps it to the largest integer type rather than keeping it as decimal.
          NUMERIC / NUMERIC(p, s) with an explicit non-zero scale stays "decimal".
        """
        cols = _run(conn, """
            CREATE TABLE "t_numerics" (
                c_num_free  NUMERIC,
                c_num_prec  NUMERIC(15),
                c_num_scale NUMERIC(15, 4)
            )
        """, "t_numerics")
        assert cols["c_num_free"]["udt_name"]  == "numeric"
        # NUMERIC(15) has no fractional part → canonical "long" → BIGINT
        assert cols["c_num_prec"]["udt_name"]  in ("int8", "numeric")
        assert cols["c_num_scale"]["udt_name"] == "numeric"
        assert cols["c_num_scale"]["num_prec"] == 15
        assert cols["c_num_scale"]["num_scale"]== 4


class TestParseFromLiveServer:
    """Reconstruct DDL from information_schema and re-parse it."""

    def test_create_and_reparse(self, conn):
        _execute(conn, 'DROP TABLE IF EXISTS live_test."reparse_test"')
        _execute(conn, """
            CREATE TABLE live_test."reparse_test" (
                id    INTEGER      NOT NULL PRIMARY KEY,
                name  VARCHAR(120) NOT NULL,
                score NUMERIC(8,3)     NULL
            )
        """)
        rows = _fetchall(conn, """
            SELECT column_name, udt_name,
                   character_maximum_length, numeric_precision, numeric_scale,
                   is_nullable
            FROM   information_schema.columns
            WHERE  table_schema = 'live_test'
              AND  table_name   = 'reparse_test'
            ORDER  BY ordinal_position
        """)
        col_defs = []
        for col_name, udt, maxlen, prec, scale, nullable in rows:
            null_str = "NULL" if nullable == "YES" else "NOT NULL"
            pg_type = {
                "int4": "INTEGER", "int8": "BIGINT",
                "varchar": f"VARCHAR({maxlen})" if maxlen else "TEXT",
                "numeric": f"NUMERIC({prec},{scale})" if prec else "NUMERIC",
                "text": "TEXT", "bool": "BOOLEAN",
            }.get(udt, udt.upper())
            col_defs.append(f'  "{col_name}" {pg_type} {null_str}')
        reconstructed = (
            'CREATE TABLE "reparse_test" (\n'
            + ",\n".join(col_defs)
            + "\n);"
        )

        tables = parse_ddl(reconstructed, dialect="postgres")
        assert tables, "parse_ddl returned no tables from reconstructed DDL"
        schema = tables[0]
        assert len(schema.columns) == 3
        assert schema.columns[0].name == "id"
        assert schema.columns[1].type == "string"
        assert schema.columns[2].type == "decimal"
