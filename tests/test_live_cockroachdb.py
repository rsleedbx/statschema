"""
Live CockroachDB integration tests — Postgres wire protocol, two topologies.

Topologies
----------
single-node   cockroachdb/cockroach start-single-node --insecure
              Default port: 26257.  Use when you need a quick dev target.

multi-region  3-node cluster with locality regions us-east1 / us-central1 / us-west1.
              Tests connect to node-1 (default port 26267), which exposes the full cluster.
              Multi-region-specific tests exercise LOCALITY GLOBAL, LOCALITY REGIONAL BY TABLE,
              and LOCALITY REGIONAL BY ROW — these are skipped on single-node.

Both topologies use psycopg2 (Postgres wire protocol) with sslmode=disable (insecure mode).
CockroachDB 22+ has native ARM64 images; both Podman containers run natively on Apple Silicon.

Prerequisites
-------------
See docs/local-databases.md → "CockroachDB" for Podman setup commands.

Environment variables (defaults)
---------------------------------
    CRDB_HOST         default: 127.0.0.1
    CRDB_USER         default: root
    CRDB_SINGLE_PORT  default: 26257
    CRDB_MULTI_PORT   default: 26267   (node-1 of the multi-region cluster)

Skip behaviour
--------------
Each topology auto-skips when its port is unreachable, so make test always completes cleanly.

CockroachDB / PostgreSQL type differences (information_schema)
--------------------------------------------------------------
  INTEGER / INT          → udt_name: "int8"   (CockroachDB INT is 64-bit; Postgres INT is 32-bit)
  BIGINT                 → udt_name: "int8"
  BYTEA                  → udt_name: "bytes" (old CRDB) / "bytea" (CRDB 23+; matches Postgres)
  TEXT                   → udt_name: "text"
  VARCHAR / CHARACTER VARYING → udt_name: "text" (old CRDB) / "varchar" (CRDB 23+; preserves declared type)
  BOOLEAN                → udt_name: "bool"
  FLOAT / DOUBLE PRECISION → udt_name: "float8"
  DECIMAL / NUMERIC      → udt_name: "numeric"
  UUID                   → udt_name: "uuid"
  TIMESTAMP              → udt_name: "timestamp"
  TIMESTAMPTZ / TIMESTAMP WITH TIME ZONE → udt_name: "timestamptz"
  DATE                   → udt_name: "date"
"""

from __future__ import annotations

import os
import textwrap
from typing import Any

import pytest

from src.statschema import emit_ddl, parse_ddl

# ---------------------------------------------------------------------------
# Connection configuration
# ---------------------------------------------------------------------------

_HOST        = os.environ.get("CRDB_HOST",        "127.0.0.1")
_USER        = os.environ.get("CRDB_USER",        "root")
_SINGLE_PORT = int(os.environ.get("CRDB_SINGLE_PORT", "26257"))
_MULTI_PORT  = int(os.environ.get("CRDB_MULTI_PORT",  "26267"))

# Database created during setup (created by the Podman init commands in docs/local-databases.md)
_DB = "testdb"

_TOPOLOGIES = [
    pytest.param(("single", _SINGLE_PORT), id="single-node"),
    pytest.param(("multi",  _MULTI_PORT),  id="multi-region-node1"),
]


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def _get_connection(port: int):
    psycopg2 = pytest.importorskip("psycopg2")
    try:
        conn = psycopg2.connect(
            host=_HOST,
            port=port,
            user=_USER,
            dbname=_DB,
            sslmode="disable",
            connect_timeout=5,
        )
        conn.autocommit = True
        return conn
    except Exception as exc:
        pytest.skip(
            f"Cannot connect to CockroachDB on {_HOST}:{port}: {exc}\n"
            "See docs/local-databases.md → 'CockroachDB' for container setup."
        )


def _execute(conn: Any, sql: str):
    cur = conn.cursor()
    cur.execute(sql)
    return cur


def _fetchall(conn: Any, sql: str, params=None) -> list[tuple]:
    cur = conn.cursor()
    cur.execute(sql, params or ())
    return cur.fetchall() or []


def _introspect(conn: Any, schema: str, table: str) -> dict[str, dict]:
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

@pytest.fixture(scope="module", params=_TOPOLOGIES)
def conn(request):
    """One connection per topology; auto-skips if unreachable."""
    _topo, port = request.param
    connection = _get_connection(port)
    _execute(connection, "DROP SCHEMA IF EXISTS statschema_test CASCADE")
    _execute(connection, "CREATE SCHEMA statschema_test")
    yield connection
    _execute(connection, "DROP SCHEMA IF EXISTS statschema_test CASCADE")
    connection.close()


@pytest.fixture(scope="module")
def multi_conn():
    """Connection to the multi-region node; skips if unreachable."""
    return _get_connection(_MULTI_PORT)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(conn: Any, ddl: str, table: str) -> dict[str, dict]:
    """Parse DDL → emit postgres → execute on CRDB → introspect information_schema."""
    _execute(conn, f'DROP TABLE IF EXISTS statschema_test."{table}"')
    tables = parse_ddl(ddl, dialect="postgres")
    assert tables, f"parse_ddl returned no tables for: {ddl[:80]}"
    sql = emit_ddl(tables[0], dialect="postgres", if_not_exists=False)
    sql = sql.replace(f'"{table}"', f'statschema_test."{table}"', 1)
    _execute(conn, sql)
    return _introspect(conn, "statschema_test", table)


# ---------------------------------------------------------------------------
# Tests: common to both single-node and multi-region
# ---------------------------------------------------------------------------

class TestCRDBConnection:
    def test_version(self, conn):
        rows = _fetchall(conn, "SELECT version()")
        ver = rows[0][0]
        assert "CockroachDB" in ver, f"Unexpected version string: {ver}"


class TestCRDBBasicTypes:
    """Core type round-trips: parse Postgres DDL → emit → execute on CockroachDB."""

    def test_integer_and_bigint(self, conn):
        cols = _run(conn, textwrap.dedent("""
            CREATE TABLE crdb_types_int (
                id      BIGINT      NOT NULL PRIMARY KEY,
                qty     INTEGER     NOT NULL,
                rank    BIGINT
            )
        """), "crdb_types_int")
        # CockroachDB stores both INT and BIGINT as int8
        assert cols["id"]["udt_name"]  == "int8"
        assert cols["qty"]["udt_name"] == "int8"
        assert cols["id"]["nullable"]  is False
        assert cols["rank"]["nullable"] is True

    def test_string_types(self, conn):
        cols = _run(conn, textwrap.dedent("""
            CREATE TABLE crdb_types_str (
                a TEXT          NOT NULL,
                b VARCHAR(200)  NOT NULL,
                c CHARACTER VARYING(50)
            )
        """), "crdb_types_str")
        # TEXT always maps to "text". VARCHAR/CHARACTER VARYING mapped to "text" in older CRDB
        # but newer versions (23+) preserve the declared type and return "varchar".
        assert cols["a"]["udt_name"] == "text"
        for col in ("b", "c"):
            assert cols[col]["udt_name"] in {"text", "varchar"}, (
                f"{col}: expected text or varchar, got {cols[col]['udt_name']}"
            )

    def test_uuid(self, conn):
        cols = _run(conn, textwrap.dedent("""
            CREATE TABLE crdb_types_uuid (
                id UUID NOT NULL PRIMARY KEY DEFAULT gen_random_uuid(),
                ref UUID
            )
        """), "crdb_types_uuid")
        assert cols["id"]["udt_name"] == "uuid"
        assert cols["ref"]["udt_name"] == "uuid"

    def test_boolean(self, conn):
        cols = _run(conn, textwrap.dedent("""
            CREATE TABLE crdb_types_bool (
                active BOOLEAN NOT NULL DEFAULT false
            )
        """), "crdb_types_bool")
        assert cols["active"]["udt_name"] == "bool"

    def test_decimal(self, conn):
        cols = _run(conn, textwrap.dedent("""
            CREATE TABLE crdb_types_decimal (
                price   DECIMAL(18,4) NOT NULL,
                pct     NUMERIC(5,2)
            )
        """), "crdb_types_decimal")
        assert cols["price"]["udt_name"] == "numeric"
        assert cols["price"]["num_prec"]  == 18
        assert cols["price"]["num_scale"] == 4

    def test_float(self, conn):
        cols = _run(conn, textwrap.dedent("""
            CREATE TABLE crdb_types_float (
                score   FLOAT            NOT NULL,
                measure DOUBLE PRECISION NOT NULL
            )
        """), "crdb_types_float")
        assert cols["score"]["udt_name"]   == "float8"
        assert cols["measure"]["udt_name"] == "float8"

    def test_temporal(self, conn):
        cols = _run(conn, textwrap.dedent("""
            CREATE TABLE crdb_types_temporal (
                created_at  TIMESTAMP               NOT NULL,
                updated_at  TIMESTAMP WITH TIME ZONE NOT NULL,
                birth_date  DATE                    NOT NULL
            )
        """), "crdb_types_temporal")
        assert cols["created_at"]["udt_name"] == "timestamp"
        assert cols["updated_at"]["udt_name"] == "timestamptz"
        assert cols["birth_date"]["udt_name"] == "date"

    def test_bytes(self, conn):
        """BYTEA alias: older CRDB reports 'bytes', newer CRDB 23+ reports 'bytea' (Postgres-compatible)."""
        cols = _run(conn, textwrap.dedent("""
            CREATE TABLE crdb_types_bytes (
                data BYTEA
            )
        """), "crdb_types_bytes")
        assert cols["data"]["udt_name"] in {"bytes", "bytea"}

    def test_primary_key_and_not_null(self, conn):
        cols = _run(conn, textwrap.dedent("""
            CREATE TABLE crdb_pk_test (
                id      BIGINT      NOT NULL PRIMARY KEY,
                label   VARCHAR(80) NOT NULL,
                notes   TEXT
            )
        """), "crdb_pk_test")
        assert cols["id"]["nullable"]    is False
        assert cols["label"]["nullable"] is False
        assert cols["notes"]["nullable"] is True

    def test_dialect_alias_cockroach_parses(self, conn):
        """dialect='cockroach' normalises to 'postgres' via dialect_registry — DDL executes on CRDB."""
        ddl = "CREATE TABLE crdb_alias_test (id BIGINT PRIMARY KEY, val TEXT)"
        tables = parse_ddl(ddl, dialect="cockroach")
        assert len(tables) == 1
        sql = emit_ddl(tables[0], dialect="cockroach", if_not_exists=False)
        sql = sql.replace('"crdb_alias_test"', 'statschema_test."crdb_alias_test"', 1)
        _execute(conn, "DROP TABLE IF EXISTS statschema_test.\"crdb_alias_test\"")
        _execute(conn, sql)
        cols = _introspect(conn, "statschema_test", "crdb_alias_test")
        assert set(cols.keys()) == {"id", "val"}

    def test_composite_primary_key(self, conn):
        cols = _run(conn, textwrap.dedent("""
            CREATE TABLE crdb_composite_pk (
                tenant_id BIGINT  NOT NULL,
                order_id  BIGINT  NOT NULL,
                amount    DECIMAL(18,4),
                PRIMARY KEY (tenant_id, order_id)
            )
        """), "crdb_composite_pk")
        assert cols["tenant_id"]["nullable"] is False
        assert cols["order_id"]["nullable"]  is False

    def test_default_value(self, conn):
        cols = _run(conn, textwrap.dedent("""
            CREATE TABLE crdb_defaults (
                id       BIGINT  NOT NULL DEFAULT 1 PRIMARY KEY,
                active   BOOLEAN NOT NULL DEFAULT false,
                label    TEXT    NOT NULL DEFAULT 'x'
            )
        """), "crdb_defaults")
        assert cols["active"]["udt_name"] == "bool"
        assert cols["label"]["udt_name"]  == "text"


class TestCRDBRoundTrip:
    """Full parse → canonical → emit → execute → re-parse cycle."""

    def test_round_trip_postgres_ddl(self, conn):
        ddl = textwrap.dedent("""
            CREATE TABLE "orders" (
                order_id    BIGSERIAL               NOT NULL PRIMARY KEY,
                customer_id BIGINT                  NOT NULL,
                total       NUMERIC(18,4)           NOT NULL DEFAULT 0.0,
                status      CHARACTER VARYING(20)   NOT NULL DEFAULT 'pending',
                created_at  TIMESTAMP WITH TIME ZONE NOT NULL,
                notes       TEXT
            )
        """)
        _execute(conn, 'DROP TABLE IF EXISTS statschema_test."orders"')
        tables = parse_ddl(ddl, dialect="postgres")
        assert len(tables) == 1
        sql = emit_ddl(tables[0], dialect="postgres", if_not_exists=False)
        sql = sql.replace('"orders"', 'statschema_test."orders"', 1)
        _execute(conn, sql)
        cols = _introspect(conn, "statschema_test", "orders")
        assert set(cols.keys()) == {"order_id", "customer_id", "total", "status", "created_at", "notes"}
        assert cols["total"]["udt_name"]      == "numeric"
        assert cols["created_at"]["udt_name"] == "timestamptz"

    def test_round_trip_crdb_schema_source(self, conn):
        """load_schema with schema_source='cockroachdb' resolves to Postgres DDL path."""
        from src.statschema import load_schema
        ddl = "CREATE TABLE crdb_src (id BIGINT PRIMARY KEY, name TEXT NOT NULL);"
        tables = load_schema({"schema_source": "cockroachdb", "ddl": ddl})
        assert len(tables) == 1
        assert tables[0].name == "crdb_src"
        assert tables[0].columns[0].type == "long"
        assert tables[0].columns[1].type == "string"


# ---------------------------------------------------------------------------
# Tests: multi-region only
# ---------------------------------------------------------------------------

class TestCRDBMultiRegion:
    """
    Multi-region DDL features.  All tests skip when the multi-region cluster
    is not running (port CRDB_MULTI_PORT unreachable).
    """

    @pytest.fixture(autouse=True)
    def setup(self, multi_conn):
        self.conn = multi_conn
        self.conn.autocommit = True
        _execute(self.conn, "DROP SCHEMA IF EXISTS statschema_mr CASCADE")
        _execute(self.conn, "CREATE SCHEMA statschema_mr")
        yield
        _execute(self.conn, "DROP SCHEMA IF EXISTS statschema_mr CASCADE")

    def test_database_has_regions(self, multi_conn):
        """The multi-region database has at least two regions configured."""
        rows = _fetchall(multi_conn, "SHOW REGIONS FROM DATABASE testdb")
        region_names = [row[0] for row in rows]
        assert len(region_names) >= 2, (
            f"Expected ≥ 2 regions, got: {region_names}. "
            "Run the multi-region init SQL from docs/local-databases.md."
        )

    def test_locality_global_table(self, multi_conn):
        """LOCALITY GLOBAL pins a table's data to all regions — reads are fast everywhere."""
        _execute(multi_conn, "DROP TABLE IF EXISTS statschema_mr.config")
        _execute(multi_conn, textwrap.dedent("""
            CREATE TABLE statschema_mr.config (
                key   TEXT NOT NULL PRIMARY KEY,
                value TEXT NOT NULL
            ) LOCALITY GLOBAL
        """))
        # CockroachDB 'SHOW CREATE TABLE' returns the DDL text in the second column
        rows = _fetchall(multi_conn, "SHOW CREATE TABLE statschema_mr.config")
        ddl_text = rows[0][1] if rows else ""
        assert "LOCALITY GLOBAL" in ddl_text.upper()

    def test_locality_regional_by_table(self, multi_conn):
        """LOCALITY REGIONAL BY TABLE pins a table to one region."""
        rows = _fetchall(multi_conn, "SHOW REGIONS FROM DATABASE testdb")
        # rows schema: (database, region, primary, secondary, zones); find the primary region
        primary_region = next(r[1] for r in rows if r[2])

        _execute(multi_conn, "DROP TABLE IF EXISTS statschema_mr.events")
        _execute(multi_conn, textwrap.dedent(f"""
            CREATE TABLE statschema_mr.events (
                id         BIGINT NOT NULL PRIMARY KEY,
                event_time TIMESTAMPTZ NOT NULL,
                payload    TEXT
            ) LOCALITY REGIONAL BY TABLE IN "{primary_region}"
        """))
        rows = _fetchall(multi_conn, "SHOW CREATE TABLE statschema_mr.events")
        ddl_text = rows[0][1] if rows else ""
        assert "LOCALITY REGIONAL BY TABLE" in ddl_text.upper()

    def test_locality_regional_by_row(self, multi_conn):
        """
        LOCALITY REGIONAL BY ROW assigns each row to a region via the
        crdb_region column (auto-injected as crdb_internal_region NOT NULL).
        Each row's region is set by DEFAULT gateway_region() or explicit value.
        """
        _execute(multi_conn, "DROP TABLE IF EXISTS statschema_mr.orders")
        _execute(multi_conn, textwrap.dedent("""
            CREATE TABLE statschema_mr.orders (
                id          UUID        NOT NULL DEFAULT gen_random_uuid(),
                customer_id BIGINT      NOT NULL,
                amount      DECIMAL(18,4),
                PRIMARY KEY (crdb_region, id)
            ) LOCALITY REGIONAL BY ROW
        """))
        rows = _fetchall(multi_conn, "SHOW CREATE TABLE statschema_mr.orders")
        ddl_text = rows[0][1] if rows else ""
        assert "LOCALITY REGIONAL BY ROW" in ddl_text.upper()

        # crdb_region column is auto-added by CRDB
        cols = _introspect(multi_conn, "statschema_mr", "orders")
        assert "crdb_region" in cols, (
            "crdb_region column not found — CockroachDB should inject it automatically "
            "for LOCALITY REGIONAL BY ROW tables."
        )

    def test_canonical_ddl_executes_on_multi_region(self, multi_conn):
        """Standard emit_ddl(dialect='postgres') output executes unchanged on multi-region CRDB."""
        ddl = textwrap.dedent("""
            CREATE TABLE "products" (
                product_id  BIGSERIAL               NOT NULL PRIMARY KEY,
                name        CHARACTER VARYING(200)  NOT NULL,
                price       NUMERIC(18,4)           NOT NULL,
                created_at  TIMESTAMP WITH TIME ZONE NOT NULL
            )
        """)
        _execute(multi_conn, 'DROP TABLE IF EXISTS statschema_mr."products"')
        tables = parse_ddl(ddl, dialect="postgres")
        sql = emit_ddl(tables[0], dialect="postgres", if_not_exists=False)
        sql = sql.replace('"products"', 'statschema_mr."products"', 1)
        _execute(multi_conn, sql)
        cols = _introspect(multi_conn, "statschema_mr", "products")
        assert cols["name"]["udt_name"]       in {"text", "varchar"}
        assert cols["price"]["udt_name"]      == "numeric"
        assert cols["created_at"]["udt_name"] == "timestamptz"
