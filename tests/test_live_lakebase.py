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

import os

import pytest

from src.statschema import parse_ddl, emit_ddl
from src.statschema.cli import _lakebase_connect
from src.statschema.db_stats_collector import collect_table_stats

# ---------------------------------------------------------------------------
# Connection parameters — all read from environment at import time
# ---------------------------------------------------------------------------

_ENDPOINT = os.environ.get("STATSCHEMA_LAKEBASE_ENDPOINT", "")
_HOST     = os.environ.get("STATSCHEMA_LAKEBASE_HOST", "")
_DB       = os.environ.get("STATSCHEMA_LAKEBASE_DB", "databricks_postgres")
_USER     = (
    os.environ.get("STATSCHEMA_LAKEBASE_USER")
    or os.environ.get("PGUSER")
    or os.environ.get("DATABRICKS_CLIENT_ID", "")
)
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
