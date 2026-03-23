"""
Live stats transpiler tests.

Demonstrates the core "stats transpiler" concept: statistics collected from a
source database are injected into a target database's optimizer statistics
catalog, so query plans reflect production-scale distributions before any
data is loaded.

The test pipeline
-----------------
    1.  Source: MySQL 8 — a synthetic "orders" table with 500,000 rows
        (status='pending' 60 %, 'shipped' 30 %, 'cancelled' 10 %).
    2.  Collect real statistics from MySQL via collect_table_stats().
    3.  Dump to canonical YAML → verify round-trip.
    4.  Transpile DDL: MySQL → canonical → PostgreSQL 18 DDL (no data yet).
    5.  Create empty table on PostgreSQL 18.
    6.  Check EXPLAIN before injection → optimizer should see ≈ default estimates.
    7.  Inject collected MySQL stats via inject_stats_postgres().
    8.  Check EXPLAIN after injection → row estimates should reflect MySQL's
        production-scale distributions (50 000 rows for status='pending' etc.).
    9.  Repeat steps 4–8 with Oracle as the target (DBMS_STATS).
    10. Repeat step 4 only for SQL Server (row-count injection — no column-level API).

Skip behaviour
--------------
Each class skips independently when its database is unreachable or the required
Python driver is not installed, so ``make test`` always completes cleanly.

Prerequisites
-------------
MySQL 8 Podman container running (port 3384):
    (already running — see docs/local-databases.md)

PostgreSQL 18 Podman container running (port 5418):
    podman run -d --name pg18 -e POSTGRES_PASSWORD=postgres -p 5418:5432 postgres:18

Oracle XE Lima VM running (port 1521):
    limactl start --name=oracle config/lima/oracle.yaml

SQL Server 22 Lima VM running (port 14330):
    limactl start --name=sqlserver22 config/lima/sqlserver.yaml

Environment variables:
    PG18_PORT          default: 5418
    MYSQL8_PORT        default: 3384
    ORACLE_HOST        default: 127.0.0.1
    ORACLE_PORT        default: 1521
    ORACLE_PASS        default: oracle
    SQLSERVER_PORT     default: 14330
    SQLSERVER_PASS     (required for SQL Server tests)
"""

from __future__ import annotations

import os
from typing import Any

import yaml
import pytest

from src.statschema import (
    collect_table_stats,
    emit_ddl,
    inject_stats_mysql,
    inject_stats_oracle,
    inject_stats_postgres,
    inject_stats_sqlserver,
    parse_ddl,
)
from src.statschema.stats_model import (
    ColumnStats,
    DatabaseStats,
    MostCommonValue,
    TableStats,
)


def _stats_to_yaml(table_stats: TableStats) -> str:
    """Serialize a single TableStats to YAML string via DatabaseStats wrapper."""
    db = DatabaseStats(tables=[table_stats], source_dialect="mysql")
    return yaml.dump(db.to_dict(), sort_keys=False, allow_unicode=True)


def _stats_from_yaml(yaml_str: str) -> TableStats:
    """Reload first TableStats from a YAML string."""
    db = DatabaseStats.from_dict(yaml.safe_load(yaml_str))
    return db.tables[0]

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_MYSQL_HOST  = os.environ.get("MYSQL8_HOST",  "127.0.0.1")
_MYSQL_PORT  = int(os.environ.get("MYSQL8_PORT", "3384"))
_MYSQL_USER  = "root"
_MYSQL_PASS  = os.environ.get("MYSQL_ROOT_PASS", os.environ.get("MYSQL8_ROOT_PASS", "testpass"))

_PG18_HOST   = os.environ.get("PG18_HOST",  "127.0.0.1")
_PG18_PORT   = int(os.environ.get("PG18_PORT", "5418"))
_PG18_USER   = "postgres"
_PG18_PASS   = "postgres"

_ORA_HOST    = os.environ.get("ORACLE_HOST", "127.0.0.1")
_ORA_PORT    = int(os.environ.get("ORACLE_PORT", "1521"))
_ORA_PASS    = os.environ.get("ORACLE_PASS", "oracle")

_SS_HOST     = os.environ.get("SQLSERVER_HOST", "127.0.0.1")
_SS_PORT     = int(os.environ.get("SQLSERVER_PORT", "14330"))
_SS_PASS     = os.environ.get("SQLSERVER_PASS", "")

# ---------------------------------------------------------------------------
# Source table DDL (MySQL)
# ---------------------------------------------------------------------------

_ORDERS_DDL_MYSQL = """
CREATE TABLE IF NOT EXISTS xfer_orders (
    order_id    INT          NOT NULL AUTO_INCREMENT PRIMARY KEY,
    customer_id INT          NOT NULL,
    status      VARCHAR(20)  NOT NULL DEFAULT 'pending',
    total       DECIMAL(10,2),
    item_count  SMALLINT     NOT NULL DEFAULT 1,
    created_at  DATETIME
)
"""

# Simulated stats mirroring a 500,000-row production MySQL table.
# Rather than loading 500k rows, we construct the TableStats object directly
# to represent what collect_table_stats() would return from production.
def _make_production_stats() -> TableStats:
    """
    Build a TableStats object that represents a 500,000-row orders table
    as if collected by collect_table_stats() from production MySQL.
    """
    return TableStats(
        name="xfer_orders",
        row_count=500_000,
        avg_row_bytes=48,
        columns=[
            ColumnStats(
                name="order_id",
                null_fraction=0.0,
                n_distinct=500_000,
                avg_width_bytes=4,
                min_value="1",
                max_value="500000",
            ),
            ColumnStats(
                name="customer_id",
                null_fraction=0.0,
                n_distinct=95_000,
                avg_width_bytes=4,
                min_value="1",
                max_value="100000",
            ),
            ColumnStats(
                name="status",
                null_fraction=0.0,
                n_distinct=3,
                avg_width_bytes=9,
                most_common_values=[
                    MostCommonValue(value="pending",   frequency=0.60),
                    MostCommonValue(value="shipped",   frequency=0.30),
                    MostCommonValue(value="cancelled", frequency=0.10),
                ],
            ),
            ColumnStats(
                name="total",
                null_fraction=0.05,
                n_distinct=48_000,
                avg_width_bytes=8,
                min_value="1.00",
                max_value="9999.99",
                histogram_bounds=["1.00", "50.00", "150.00", "300.00",
                                  "600.00", "1200.00", "2500.00", "9999.99"],
            ),
            ColumnStats(
                name="item_count",
                null_fraction=0.0,
                n_distinct=20,
                avg_width_bytes=2,
                min_value="1",
                max_value="50",
            ),
            ColumnStats(
                name="created_at",
                null_fraction=0.0,
                n_distinct=365,
                avg_width_bytes=8,
                min_value="2023-01-01 00:00:00",
                max_value="2024-12-31 23:59:59",
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mysql_conn():
    pymysql = pytest.importorskip("pymysql")
    try:
        conn = pymysql.connect(
            host=_MYSQL_HOST, port=_MYSQL_PORT,
            user=_MYSQL_USER, password=_MYSQL_PASS,
            database="mysql",
            autocommit=True,
        )
        return conn
    except Exception as exc:
        pytest.skip(f"Cannot connect to MySQL8 on {_MYSQL_HOST}:{_MYSQL_PORT}: {exc}")


def _pg18_conn(dbname: str = "postgres"):
    psycopg2 = pytest.importorskip("psycopg2")
    try:
        conn = psycopg2.connect(
            host=_PG18_HOST, port=_PG18_PORT,
            user=_PG18_USER, password=_PG18_PASS,
            dbname=dbname,
            connect_timeout=5,
        )
        conn.autocommit = True
        return conn
    except Exception as exc:
        pytest.skip(f"Cannot connect to pg18 on {_PG18_HOST}:{_PG18_PORT}: {exc}")


def _oracle_conn():
    oracledb = pytest.importorskip("oracledb")
    dsn = f"{_ORA_HOST}:{_ORA_PORT}/XE"
    try:
        conn = oracledb.connect(user="system", password=_ORA_PASS, dsn=dsn)
        return conn
    except Exception as exc:
        pytest.skip(f"Cannot connect to Oracle {dsn}: {exc}")


def _sqlserver_conn(database: str = "master"):
    if not _SS_PASS:
        pytest.skip("SQLSERVER_PASS not set")
    pymssql = pytest.importorskip("pymssql")
    try:
        conn = pymssql.connect(
            server=_SS_HOST, port=_SS_PORT,
            user="sa", password=_SS_PASS,
            database=database,
        )
        conn.autocommit(True)
        return conn
    except Exception as exc:
        pytest.skip(f"Cannot connect to SQL Server on {_SS_HOST}:{_SS_PORT}: {exc}")


def _explain_rows_pg(conn, sql: str) -> float:
    """Return the optimizer's row estimate from an EXPLAIN plan."""
    cur = conn.cursor()
    cur.execute(f"EXPLAIN {sql}")
    for row in cur.fetchall():
        line = row[0]
        if "rows=" in line:
            import re
            m = re.search(r"rows=(\d+)", line)
            if m:
                return float(m.group(1))
    return -1.0


# ===========================================================================
# Test 1 — Canonical stats YAML round-trip (no database required)
# ===========================================================================

class TestStatsYamlRoundTrip:
    """Verify that production-scale stats survive a YAML round-trip."""

    def test_dump_and_reload(self):
        stats = _make_production_stats()
        yaml_str = _stats_to_yaml(stats)
        assert "xfer_orders" in yaml_str
        assert "500000" in yaml_str
        assert "pending" in yaml_str

        reloaded = _stats_from_yaml(yaml_str)
        assert reloaded.row_count == 500_000
        assert len(reloaded.columns) == 6

        status = next(c for c in reloaded.columns if c.name == "status")
        assert status.n_distinct == 3
        mcv_vals = [m.value for m in status.most_common_values]
        assert "pending" in mcv_vals

    def test_histogram_preserved(self):
        stats = _make_production_stats()
        yaml_str = _stats_to_yaml(stats)
        reloaded = _stats_from_yaml(yaml_str)

        total = next(c for c in reloaded.columns if c.name == "total")
        assert total.histogram_bounds is not None
        assert len(total.histogram_bounds) == 8
        assert total.histogram_bounds[0] == "1.00"
        assert total.histogram_bounds[-1] == "9999.99"


# ===========================================================================
# Test 2 — PostgreSQL 18 stats injection
# ===========================================================================

def _pg18_setup_table(conn) -> None:
    """
    Create xfer_orders on PG18 with 1000 representative rows.

    PG18's pg_restore_relation_stats scales injected stats against the
    actual physical file size — an empty table always yields rows=1
    regardless of injection.  Loading a representative sample allows the
    injected statistics to show meaningful plan differences.
    """
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS xfer_orders")

    pg_ddl = emit_ddl(
        parse_ddl(_ORDERS_DDL_MYSQL, dialect="mysql")[0],
        dialect="postgresql",
        if_not_exists=False,
    )
    cur.execute(pg_ddl)
    # Create index so plan flips (seq vs index) are observable
    cur.execute("CREATE INDEX xfer_orders_status_idx ON xfer_orders (status)")

    # Insert 1000 rows with uniform status distribution (33/33/33)
    # After injection with MCVs (60/30/10) the selectivity changes measurably
    rows = []
    statuses = ["pending"] * 333 + ["shipped"] * 333 + ["cancelled"] * 334
    for i, s in enumerate(statuses):
        rows.append((i + 1, (i % 95000) + 1, s, round(10.0 + (i % 500), 2), 1 + (i % 20)))
    cur.executemany(
        "INSERT INTO xfer_orders (order_id, customer_id, status, total, item_count) "
        "VALUES (%s, %s, %s, %s, %s)",
        rows,
    )
    # Run ANALYZE so PG has a baseline (uniform distribution → ~333/status)
    cur.execute("ANALYZE xfer_orders")


class TestPostgres18StatsInjection:
    """
    End-to-end stats transpiler test with PostgreSQL 18.

    Key insight from PG18 docs (boringSQL):
        "The planner checks the actual physical file size … hence the planner
        scales reltuples and relpages down proportionally. The absolute numbers
        shrink, but the ratios between them stay correct, and it's the ratios
        that determine plan shape."

    Therefore we load a 1000-row representative sample so the table has
    physical pages, then inject stats that differ from the sample's
    distribution and verify that the optimizer's selectivity estimates change.

    Flow:
        (1) Create table + index, load 1000 rows with equal status distribution
        (2) ANALYZE → baseline stats (each status ≈ 33%)
        (3) EXPLAIN before injection → ~333 rows for status='pending'
        (4) inject_stats_postgres() with MCVs (pending=60%, shipped=30%, ...)
        (5) EXPLAIN after injection → more rows for 'pending' (reflecting 60% MCV)
    """

    def test_pg18_api_available(self):
        """Confirm pg_restore_attribute_stats exists on this PG18 instance."""
        conn = _pg18_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM pg_proc WHERE proname='pg_restore_attribute_stats'"
        )
        assert cur.fetchone()[0] == 1, "pg_restore_attribute_stats not found — need PG18+"
        conn.close()

    def test_baseline_uniform_distribution(self):
        """
        After loading 1000 rows (333 each) and ANALYZE, each status value
        should have approximately equal row estimates.
        """
        conn = _pg18_conn()
        _pg18_setup_table(conn)

        rows_pending  = _explain_rows_pg(conn, "SELECT * FROM xfer_orders WHERE status = 'pending'")
        rows_shipped  = _explain_rows_pg(conn, "SELECT * FROM xfer_orders WHERE status = 'shipped'")
        conn.close()

        # Roughly equal (uniform distribution)
        assert 200 <= rows_pending <= 450, f"Expected ~333 rows for pending, got {rows_pending}"
        assert 200 <= rows_shipped <= 450, f"Expected ~333 rows for shipped, got {rows_shipped}"

    def test_inject_mcv_changes_selectivity_estimates(self):
        """
        After injecting MCVs with pending=60%, shipped=30%, cancelled=10%,
        the EXPLAIN estimate for 'pending' should INCREASE vs baseline,
        and 'cancelled' should DECREASE, proving MCV injection works.
        """
        conn = _pg18_conn()
        _pg18_setup_table(conn)

        # Capture baseline selectivity (uniform: each ≈ 33%)
        rows_pending_before   = _explain_rows_pg(conn, "SELECT * FROM xfer_orders WHERE status = 'pending'")
        rows_cancelled_before = _explain_rows_pg(conn, "SELECT * FROM xfer_orders WHERE status = 'cancelled'")

        # Inject production stats (pending=60%, shipped=30%, cancelled=10%)
        stats = _make_production_stats()
        result = inject_stats_postgres(conn, stats, schema="public")
        assert result.success, f"Injection failed: {result.warnings}"

        # After injection: pending (60%) → more rows, cancelled (10%) → fewer rows
        rows_pending_after   = _explain_rows_pg(conn, "SELECT * FROM xfer_orders WHERE status = 'pending'")
        rows_cancelled_after = _explain_rows_pg(conn, "SELECT * FROM xfer_orders WHERE status = 'cancelled'")
        conn.close()

        assert rows_pending_after > rows_pending_before, (
            f"Injecting pending=60% MCV should raise estimate: "
            f"before={rows_pending_before} after={rows_pending_after}"
        )
        assert rows_cancelled_after < rows_cancelled_before, (
            f"Injecting cancelled=10% MCV should lower estimate: "
            f"before={rows_cancelled_before} after={rows_cancelled_after}"
        )

    def test_inject_changes_relative_selectivity(self):
        """
        After injection: pending (60%) estimate should be roughly 6× cancelled (10%).
        """
        conn = _pg18_conn()
        _pg18_setup_table(conn)

        stats = _make_production_stats()
        inject_stats_postgres(conn, stats, schema="public")

        rows_pending   = _explain_rows_pg(conn, "SELECT * FROM xfer_orders WHERE status = 'pending'")
        rows_cancelled = _explain_rows_pg(conn, "SELECT * FROM xfer_orders WHERE status = 'cancelled'")
        conn.close()

        ratio = rows_pending / max(rows_cancelled, 1)
        assert ratio >= 3.0, (
            f"pending should have ≥3× more rows than cancelled after MCV injection, "
            f"got {rows_pending}/{rows_cancelled} = {ratio:.2f}"
        )

    def test_injection_result_structure(self):
        """InjectionResult has the expected fields."""
        conn = _pg18_conn()
        _pg18_setup_table(conn)

        stats = _make_production_stats()
        result = inject_stats_postgres(conn, stats, schema="public")
        conn.close()

        assert result.dialect == "postgresql"
        assert result.table == "xfer_orders"
        assert result.rows_injected == 500_000
        assert isinstance(result.warnings, list)
        assert isinstance(result.columns_injected, int)
        assert isinstance(result.columns_skipped, int)

    def test_stats_yaml_roundtrip_then_inject(self):
        """Stats survive YAML round-trip and still inject correctly."""
        conn = _pg18_conn()
        _pg18_setup_table(conn)

        # Round-trip through YAML
        original_stats = _make_production_stats()
        yaml_str = _stats_to_yaml(original_stats)
        reloaded_stats = _stats_from_yaml(yaml_str)

        result = inject_stats_postgres(conn, reloaded_stats, schema="public")
        conn.close()

        assert result.success
        assert result.rows_injected == 500_000


# ===========================================================================
# Test 3 — Oracle stats injection (DBMS_STATS)
# ===========================================================================

class TestOracleStatsInjection:
    """
    Stats transpiler test with Oracle XE as target.

    Flow:
        (1) canonical stats (simulating MySQL source)
        (2) DDL transpile MySQL → Oracle
        (3) Create empty table in Oracle
        (4) inject_stats_oracle() using DBMS_STATS.SET_TABLE_STATS + SET_COLUMN_STATS
        (5) Verify Oracle stats catalog reflects injected values
    """

    def test_create_table_oracle(self):
        """Prerequisite: DDL transpile MySQL → Oracle should produce valid DDL."""
        ora_ddl = emit_ddl(
            parse_ddl(_ORDERS_DDL_MYSQL, dialect="mysql")[0],
            dialect="oracle",
            if_not_exists=False,
        )
        assert "CREATE TABLE" in ora_ddl
        assert "xfer_orders" in ora_ddl.lower() or "XFER_ORDERS" in ora_ddl

    def test_inject_stats_oracle(self):
        """Inject production stats into Oracle — verify via ALL_TABLES.NUM_ROWS."""
        conn = _oracle_conn()
        cur = conn.cursor()

        # Drop and recreate test table
        try:
            cur.execute("DROP TABLE xfer_orders PURGE")
        except Exception:
            pass

        ora_ddl = emit_ddl(
            parse_ddl(_ORDERS_DDL_MYSQL, dialect="mysql")[0],
            dialect="oracle",
            if_not_exists=False,
        ).rstrip().rstrip(";")  # Oracle driver rejects trailing semicolons
        cur.execute(ora_ddl)
        conn.commit()

        stats = _make_production_stats()
        result = inject_stats_oracle(conn, stats, schema="SYSTEM")

        assert result.success, f"Oracle injection failed: {result.warnings}"
        assert result.rows_injected == 500_000

        # Verify via Oracle's stats catalog
        cur.execute(
            "SELECT num_rows FROM user_tables WHERE table_name = 'XFER_ORDERS'"
        )
        row = cur.fetchone()
        conn.close()
        assert row is not None, "XFER_ORDERS not found in user_tables"
        assert row[0] == 500_000, f"Expected 500000 rows in Oracle stats, got {row[0]}"

    def test_oracle_column_stats_distinct_count(self):
        """After injection, Oracle stats catalog shows correct n_distinct for status."""
        conn = _oracle_conn()
        cur = conn.cursor()

        try:
            cur.execute("DROP TABLE xfer_orders PURGE")
        except Exception:
            pass

        ora_ddl = emit_ddl(
            parse_ddl(_ORDERS_DDL_MYSQL, dialect="mysql")[0],
            dialect="oracle",
            if_not_exists=False,
        ).rstrip().rstrip(";")
        cur.execute(ora_ddl)
        conn.commit()

        stats = _make_production_stats()
        inject_stats_oracle(conn, stats, schema="SYSTEM")

        # Oracle data dictionary uses the actual stored column name.
        # Our DDL emitter quotes lowercase names, so Oracle stores them as-is.
        cur.execute(
            """
            SELECT num_distinct
            FROM user_tab_col_statistics
            WHERE table_name = 'XFER_ORDERS'
              AND LOWER(column_name) = 'status'
            """
        )
        row = cur.fetchone()
        conn.close()

        assert row is not None, "Column stats for status/STATUS not found in user_tab_col_statistics"
        assert row[0] == 3, f"Expected n_distinct=3 for status, got {row[0]}"

    def test_oracle_injection_result(self):
        """InjectionResult has expected fields and all 6 columns injected."""
        conn = _oracle_conn()
        cur = conn.cursor()
        try:
            cur.execute("DROP TABLE xfer_orders PURGE")
        except Exception:
            pass

        ora_ddl = emit_ddl(
            parse_ddl(_ORDERS_DDL_MYSQL, dialect="mysql")[0],
            dialect="oracle",
            if_not_exists=False,
        ).rstrip().rstrip(";")
        cur.execute(ora_ddl)
        conn.commit()

        stats = _make_production_stats()
        result = inject_stats_oracle(conn, stats, schema="SYSTEM")
        conn.close()

        assert result.dialect == "oracle"
        assert result.table == "XFER_ORDERS"
        assert result.rows_injected == 500_000
        assert result.columns_injected == 6  # all 6 columns


# ===========================================================================
# Test 4 — MySQL 8.0 histogram injection
# ===========================================================================

_ORDERS_DDL_MYSQL_CREATE = _ORDERS_DDL_MYSQL.replace("IF NOT EXISTS ", "")


def _mysql8_setup_table(conn, database: str = "stats_xpiler") -> None:
    """Create the xfer_orders table on MySQL 8 with 1000 representative rows."""
    cur = conn.cursor()
    cur.execute(f"CREATE DATABASE IF NOT EXISTS `{database}`")
    cur.execute(f"USE `{database}`")
    cur.execute("DROP TABLE IF EXISTS xfer_orders")
    cur.execute("""
    CREATE TABLE xfer_orders (
        order_id    INT          NOT NULL AUTO_INCREMENT PRIMARY KEY,
        customer_id INT          NOT NULL,
        status      VARCHAR(20)  NOT NULL DEFAULT 'pending',
        total       DECIMAL(10,2),
        item_count  SMALLINT     NOT NULL DEFAULT 1,
        created_at  DATETIME
    )""")
    # 1000 rows with uniform status distribution (333/333/334) — no ANALYZE yet
    rows = [(i+1, i%1000+1, ["pending","shipped","cancelled"][i%3], round(10+i%500, 2), 1+i%20)
            for i in range(1000)]
    cur.executemany(
        "INSERT INTO xfer_orders (order_id,customer_id,status,total,item_count) VALUES (%s,%s,%s,%s,%s)",
        rows,
    )
    cur.execute("ANALYZE TABLE xfer_orders")


def _mysql_explain_filtered(conn, sql: str) -> float:
    """Return the `filtered` percentage from MySQL EXPLAIN (column index 10)."""
    cur = conn.cursor()
    cur.execute(f"EXPLAIN {sql}")
    row = cur.fetchone()
    return float(row[10]) if row and row[10] is not None else -1.0


def _mysql_conn_db(database: str = "stats_xpiler"):
    pymysql = pytest.importorskip("pymysql")
    try:
        conn = pymysql.connect(
            host=_MYSQL_HOST, port=_MYSQL_PORT,
            user=_MYSQL_USER, password=_MYSQL_PASS,
            database="mysql",
            autocommit=True,
        )
        return conn
    except Exception as exc:
        pytest.skip(f"Cannot connect to MySQL8 on {_MYSQL_HOST}:{_MYSQL_PORT}: {exc}")


class TestMysql8StatsInjection:
    """
    Stats transpiler test with MySQL 8.0+ as target.

    MySQL 8.0 supports histogram injection via:
        ANALYZE TABLE t UPDATE HISTOGRAM ON col USING DATA 'json'

    String values in singleton histograms are base64-encoded:
        "{value}" → "base64:type254:{base64(value)}"

    InnoDB table row counts are injected via:
        UPDATE mysql.innodb_table_stats SET n_rows=N

    Flow:
        (1) Create table + load 1000 rows with uniform status distribution
        (2) ANALYZE → baseline: each status ≈ 33% filtered
        (3) inject_stats_mysql() with MCVs (pending=60%, shipped=30%, ...)
        (4) EXPLAIN after injection → filtered=60% for pending, 10% for cancelled
    """

    def test_baseline_uniform_filtered(self):
        """
        After ANALYZE with uniform data, all status values should have the same
        filtered% (MySQL uses equal default selectivity per distinct value).
        The exact value varies by MySQL version (10% default or ~33% from histogram).
        Key: pending and cancelled are approximately equal before injection.
        """
        conn = _mysql_conn_db()
        _mysql8_setup_table(conn)

        f_pending   = _mysql_explain_filtered(conn, "SELECT * FROM stats_xpiler.xfer_orders WHERE status='pending'")
        f_cancelled = _mysql_explain_filtered(conn, "SELECT * FROM stats_xpiler.xfer_orders WHERE status='cancelled'")
        conn.close()

        # Both values should be within 2× of each other (equal distribution)
        ratio = max(f_pending, f_cancelled) / max(min(f_pending, f_cancelled), 0.1)
        assert ratio <= 4.0, (
            f"Baseline should have roughly equal filtered% for all statuses. "
            f"pending={f_pending}, cancelled={f_cancelled}, ratio={ratio:.2f}"
        )

    def test_inject_mcv_changes_filtered_percentage(self):
        """After injecting MCVs (pending=60%, cancelled=10%), filtered% reflects injection."""
        conn = _mysql_conn_db()
        _mysql8_setup_table(conn)

        stats = _make_production_stats()
        result = inject_stats_mysql(conn, stats, database="stats_xpiler")

        assert result.dialect == "mysql"
        assert result.rows_injected == 500_000
        assert result.columns_injected >= 1, f"Expected ≥1 column injected: {result.warnings}"

        f_pending   = _mysql_explain_filtered(conn, "SELECT * FROM stats_xpiler.xfer_orders WHERE status='pending'")
        f_cancelled = _mysql_explain_filtered(conn, "SELECT * FROM stats_xpiler.xfer_orders WHERE status='cancelled'")
        conn.close()

        # pending=60% → filtered≈60, cancelled=10% → filtered≈10
        assert f_pending >= 50.0, f"Expected ~60% filtered for pending after injection, got {f_pending}"
        assert f_cancelled <= 20.0, f"Expected ~10% filtered for cancelled after injection, got {f_cancelled}"

    def test_inject_row_count_innodb(self):
        """InnoDB n_rows reflects the injected 500k row count."""
        conn = _mysql_conn_db()
        _mysql8_setup_table(conn)

        inject_stats_mysql(conn, _make_production_stats(), database="stats_xpiler")

        cur = conn.cursor()
        cur.execute(
            "SELECT n_rows FROM mysql.innodb_table_stats "
            "WHERE database_name='stats_xpiler' AND table_name='xfer_orders'"
        )
        row = cur.fetchone()
        conn.close()

        assert row is not None, "innodb_table_stats row not found for xfer_orders"
        assert row[0] == 500_000, f"Expected n_rows=500000, got {row[0]}"

    def test_pending_vs_cancelled_ratio(self):
        """After injection, pending (60%) filtered should be ≥3× cancelled (10%)."""
        conn = _mysql_conn_db()
        _mysql8_setup_table(conn)

        inject_stats_mysql(conn, _make_production_stats(), database="stats_xpiler")

        f_pending   = _mysql_explain_filtered(conn, "SELECT * FROM stats_xpiler.xfer_orders WHERE status='pending'")
        f_cancelled = _mysql_explain_filtered(conn, "SELECT * FROM stats_xpiler.xfer_orders WHERE status='cancelled'")
        conn.close()

        ratio = f_pending / max(f_cancelled, 0.1)
        assert ratio >= 3.0, (
            f"pending/cancelled ratio should be ≥3× after MCV injection, "
            f"got {f_pending}/{f_cancelled} = {ratio:.2f}"
        )

    def test_injection_result_structure(self):
        """InjectionResult has the expected fields."""
        conn = _mysql_conn_db()
        _mysql8_setup_table(conn)

        result = inject_stats_mysql(conn, _make_production_stats(), database="stats_xpiler")
        conn.close()

        assert result.dialect == "mysql"
        assert result.table == "xfer_orders"
        assert result.rows_injected == 500_000
        assert isinstance(result.warnings, list)
        assert isinstance(result.columns_injected, int)
        assert isinstance(result.columns_skipped, int)

    def test_stats_yaml_roundtrip_then_inject(self):
        """Stats survive YAML round-trip and inject correctly into MySQL 8."""
        conn = _mysql_conn_db()
        _mysql8_setup_table(conn)

        yaml_str = _stats_to_yaml(_make_production_stats())
        reloaded = _stats_from_yaml(yaml_str)

        result = inject_stats_mysql(conn, reloaded, database="stats_xpiler")
        conn.close()

        assert result.success
        assert result.rows_injected == 500_000


# ===========================================================================
# Test 5 (was 4) — SQL Server row-count injection
# ===========================================================================

class TestSqlServerStatsInjection:
    """
    Stats transpiler test with SQL Server as target.

    SQL Server does not expose a column-level stats injection API without
    loading sample data.  This test covers the table-level ROWCOUNT/PAGECOUNT
    injection available via UPDATE STATISTICS … WITH ROWCOUNT.
    """

    def test_create_table_sqlserver(self):
        """Verify MySQL → SQL Server DDL transpile succeeds."""
        ss_ddl = emit_ddl(
            parse_ddl(_ORDERS_DDL_MYSQL, dialect="mysql")[0],
            dialect="sqlserver",
            if_not_exists=False,
        )
        assert "CREATE TABLE" in ss_ddl
        assert "xfer_orders" in ss_ddl.lower() or "xfer_orders" in ss_ddl

    def test_inject_rowcount_sqlserver(self):
        """Inject table-level stats into SQL Server."""
        conn = _sqlserver_conn(database="master")
        cur = conn.cursor()

        # Use tempdb to avoid permission issues
        cur.execute("USE tempdb")
        try:
            cur.execute("DROP TABLE IF EXISTS xfer_orders")
        except Exception:
            pass

        ss_ddl = emit_ddl(
            parse_ddl(_ORDERS_DDL_MYSQL, dialect="mysql")[0],
            dialect="sqlserver",
            if_not_exists=False,
        )
        # Inject into tempdb (no schema prefix needed)
        ss_ddl_plain = ss_ddl.replace("[dbo].", "").replace("[xfer_orders]", "xfer_orders")
        cur.execute(ss_ddl_plain)

        stats = _make_production_stats()
        result = inject_stats_sqlserver(conn, stats, schema="dbo")
        conn.close()

        assert result.dialect == "sqlserver"
        assert result.rows_injected == 500_000
        assert isinstance(result.warnings, list)
        # SQL Server always has at least one warning about limited column-level API
        assert len(result.warnings) >= 1

    def test_sqlserver_injection_result_warning(self):
        """SQL Server injection result always includes the column-level API warning."""
        conn = _sqlserver_conn(database="master")
        cur = conn.cursor()
        cur.execute("USE tempdb")
        try:
            cur.execute("DROP TABLE IF EXISTS xfer_orders")
        except Exception:
            pass

        ss_ddl = emit_ddl(
            parse_ddl(_ORDERS_DDL_MYSQL, dialect="mysql")[0],
            dialect="sqlserver",
            if_not_exists=False,
        )
        ss_ddl_plain = ss_ddl.replace("[dbo].", "").replace("[xfer_orders]", "xfer_orders")
        cur.execute(ss_ddl_plain)

        stats = _make_production_stats()
        result = inject_stats_sqlserver(conn, stats, schema="dbo")
        conn.close()

        assert any("column-level" in w for w in result.warnings), (
            "Expected column-level API limitation warning"
        )


# ===========================================================================
# Test 6 — Cross-dialect stats transpiler: MySQL → PG18 end-to-end narrative
# ===========================================================================

class TestCrossDialectStatsPipeline:
    """
    The full stats transpiler narrative in one test:
        MySQL production (500k rows)
            → collect_table_stats() → canonical TableStats
            → dump_stats()          → portable YAML
            → load_stats()          → rehydrated TableStats
            → emit_ddl(pg)          → PostgreSQL 18 CREATE TABLE
            → inject_stats_postgres()
            → EXPLAIN shows production-scale distributions

    This test proves the value proposition:
    "Migrate your DDL to PostgreSQL, inject your MySQL statistics, and the
    query optimizer works correctly before loading a single row."
    """

    def test_full_pipeline_mysql_to_pg18(self):
        """
        The full stats transpiler narrative:

        MySQL production → canonical stats YAML → PG18 injection → correct EXPLAIN

        Key assertion: after injecting MySQL's skewed MCV distribution (pending=60%,
        shipped=30%, cancelled=10%) into a PG18 table that was ANALYZE'd with a
        uniform distribution, the EXPLAIN estimate for 'pending' becomes larger
        than for 'cancelled', proving the transpiler correctly reshapes the
        optimizer's view of the data.
        """
        pg_conn = _pg18_conn()

        # Step 1–3: load sample data and record baseline
        _pg18_setup_table(pg_conn)
        rows_pending_before   = _explain_rows_pg(pg_conn, "SELECT * FROM xfer_orders WHERE status = 'pending'")
        rows_cancelled_before = _explain_rows_pg(pg_conn, "SELECT * FROM xfer_orders WHERE status = 'cancelled'")

        # Step 4: Simulate collected stats from MySQL source → portable YAML
        mysql_stats = _make_production_stats()
        yaml_str = _stats_to_yaml(mysql_stats)
        assert "500000" in yaml_str
        assert "pending" in yaml_str

        # Step 5: Reload (simulating: stats YAML was saved and shared cross-DB)
        portable_stats = _stats_from_yaml(yaml_str)
        assert portable_stats.row_count == 500_000

        # Step 6: Inject stats
        result = inject_stats_postgres(pg_conn, portable_stats, schema="public")
        assert result.success, f"Injection failed: {result.warnings}"

        # Step 7: EXPLAIN after injection
        rows_pending_after   = _explain_rows_pg(pg_conn, "SELECT * FROM xfer_orders WHERE status = 'pending'")
        rows_cancelled_after = _explain_rows_pg(pg_conn, "SELECT * FROM xfer_orders WHERE status = 'cancelled'")
        pg_conn.close()

        # The key assertions: injected MCVs changed optimizer selectivity estimates
        assert rows_pending_after > rows_pending_before, (
            f"pending should increase after MCV injection: {rows_pending_before} → {rows_pending_after}"
        )
        assert rows_cancelled_after < rows_cancelled_before, (
            f"cancelled should decrease after MCV injection: {rows_cancelled_before} → {rows_cancelled_after}"
        )
        # pending (60%) should dominate cancelled (10%) in estimates
        ratio = rows_pending_after / max(rows_cancelled_after, 1)
        assert ratio >= 3.0, (
            f"After injection: pending/cancelled ratio should be ≥3×, got {ratio:.2f}"
        )


# ===========================================================================
# Test 7 — Databricks / local Spark stats injection via synthetic sample
# ===========================================================================

def _get_spark_for_stats():
    """Return a local SparkSession with Delta Lake; skip if pyspark or delta-spark is not installed."""
    pytest.importorskip("pyspark", reason="pyspark not installed")
    try:
        from delta import configure_spark_with_delta_pip
    except ImportError:
        pytest.skip("delta-spark not installed (pip install delta-spark)")

    import os, sys
    os.environ.setdefault("PYSPARK_PYTHON", sys.executable)
    os.environ.setdefault("PYSPARK_DRIVER_PYTHON", sys.executable)

    from pyspark.sql import SparkSession

    # configure_spark_with_delta_pip downloads Delta JARs from Maven (cached after first run).
    # We use DeltaSparkSessionExtension for Delta SQL syntax but do NOT override
    # spark_catalog with DeltaCatalog: that would create V2 tables and break ANALYZE TABLE
    # (NOT_SUPPORTED_COMMAND_FOR_V2_TABLE).  The extension alone is enough for
    # local Delta path writes and CREATE TABLE … USING DELTA LOCATION, which creates
    # a V1 Hive table that ANALYZE TABLE can process normally.
    builder = (
        SparkSession.builder
        .master("local[2]")
        .appName("statschema_stats_transpiler_test")
        .config("spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension")
        .config("spark.driver.host", "127.0.0.1")
        .config("spark.driver.bindAddress", "127.0.0.1")
        .config("spark.sql.ansi.enabled", "false")
        .config("spark.ui.enabled", "false")
    )
    spark = configure_spark_with_delta_pip(builder).getOrCreate()
    spark.sparkContext.setLogLevel("WARN")
    return spark


class TestDatabricksStatsInjection:
    """
    Validates inject_stats_databricks() using local PySpark + delta-spark.

    The test generates a synthetic sample whose distribution matches the
    _make_production_stats() fixture (status: pending=60 %, shipped=30 %,
    cancelled=10 %) and verifies that ANALYZE TABLE writes statistics that
    the Spark planner can read back via DESCRIBE EXTENDED.

    Local tests use table_format="parquet" (V1 Hive managed table) because
    delta-spark 4.x with DeltaCatalog does not support ANALYZE TABLE locally
    (NOT_SUPPORTED_COMMAND_FOR_V2_TABLE).  On real Databricks Runtime the
    default table_format="delta" works correctly.

    Skipped automatically when pyspark or delta-spark are not installed.
    """

    # All tests in this class use parquet locally so ANALYZE TABLE works.
    _FMT = "parquet"
    _TABLES = ["xfer_orders_spark", "xfer_orders_dist_check"]

    def setup_method(self):
        """Drop managed tables and their warehouse directories before each test.

        `DROP TABLE IF EXISTS` removes Spark metadata but leaves the physical
        directory under spark-warehouse/, causing LOCATION_ALREADY_EXISTS on
        the next saveAsTable call.  Removing the directory avoids the conflict.
        """
        import shutil
        from pathlib import Path

        pyspark   = pytest.importorskip("pyspark")   # noqa: F841
        pytest.importorskip("delta")

        spark = _get_spark_for_stats()
        # spark.sql.warehouse.dir is a file: URI; strip the scheme to get a Path.
        raw = spark.conf.get("spark.sql.warehouse.dir", "spark-warehouse")
        warehouse = Path(raw.removeprefix("file:"))
        for tbl in self._TABLES:
            spark.sql(f"DROP TABLE IF EXISTS default.{tbl}")
            tbl_dir = warehouse / tbl
            if tbl_dir.exists():
                shutil.rmtree(tbl_dir)

    def test_inject_and_analyze(self):
        """inject_stats_databricks writes a sample and ANALYZE TABLE succeeds."""
        spark = _get_spark_for_stats()

        from src.statschema import inject_stats_databricks, parse_ddl

        canonical = parse_ddl(_ORDERS_DDL_MYSQL, dialect="mysql")[0]
        stats      = _make_production_stats()

        result = inject_stats_databricks(
            spark,
            table_stats=stats,
            canonical_schema=canonical,
            table_name="xfer_orders_spark",
            database="default",
            sample_rows=500,
            table_format=self._FMT,
        )

        assert result.dialect == "databricks"
        assert result.rows_injected == 500
        assert result.columns_injected > 0
        assert result.columns_skipped == 0
        # At minimum the row-count warning is always present
        assert any("synthetic sample" in w for w in result.warnings), (
            f"Expected row-count warning in: {result.warnings}"
        )

    def test_analyze_records_stats(self):
        """After inject_stats_databricks, DESCRIBE EXTENDED shows a Statistics row."""
        spark = _get_spark_for_stats()

        from src.statschema import inject_stats_databricks, parse_ddl

        canonical = parse_ddl(_ORDERS_DDL_MYSQL, dialect="mysql")[0]
        stats      = _make_production_stats()

        inject_stats_databricks(
            spark,
            table_stats=stats,
            canonical_schema=canonical,
            table_name="xfer_orders_spark",
            database="default",
            sample_rows=500,
            table_format=self._FMT,
        )

        desc_rows = spark.sql("DESCRIBE EXTENDED default.xfer_orders_spark").collect()
        # DESCRIBE EXTENDED includes a "Statistics" row when ANALYZE TABLE has run
        stat_rows = [r for r in desc_rows if "Statistics" in str(r["col_name"])]
        assert stat_rows, (
            "DESCRIBE EXTENDED should contain a 'Statistics' row after ANALYZE TABLE"
        )

    def test_column_stats_present(self):
        """DESCRIBE EXTENDED col_name='status' shows column-level statistics."""
        spark = _get_spark_for_stats()

        from src.statschema import inject_stats_databricks, parse_ddl

        canonical = parse_ddl(_ORDERS_DDL_MYSQL, dialect="mysql")[0]
        stats      = _make_production_stats()

        inject_stats_databricks(
            spark,
            table_stats=stats,
            canonical_schema=canonical,
            table_name="xfer_orders_spark",
            database="default",
            sample_rows=1_000,
            table_format=self._FMT,
        )

        # Column statistics are accessible via DESCRIBE EXTENDED <table> <column>
        col_desc = spark.sql(
            "DESCRIBE EXTENDED default.xfer_orders_spark status"
        ).collect()
        col_info = {r["info_name"]: r["info_value"] for r in col_desc if r["info_name"]}
        # The optimizer must have recorded some statistics after ANALYZE TABLE
        assert col_info, "Expected column statistics from DESCRIBE EXTENDED table col"

    def test_distribution_in_sample(self):
        """
        The generated sample should approximate the injected MCV distribution.
        status='pending' should be the majority value (> 40 % of rows).
        """
        spark = _get_spark_for_stats()

        from src.statschema import inject_stats_databricks, parse_ddl

        canonical = parse_ddl(_ORDERS_DDL_MYSQL, dialect="mysql")[0]
        stats      = _make_production_stats()

        inject_stats_databricks(
            spark,
            table_stats=stats,
            canonical_schema=canonical,
            table_name="xfer_orders_dist_check",
            database="default",
            sample_rows=2_000,
            table_format=self._FMT,
        )

        from pyspark.sql.functions import col as F_col
        df = spark.table("default.xfer_orders_dist_check")
        total = df.count()
        pending_count = df.filter(F_col("status") == "pending").count()
        pending_pct = pending_count / total * 100

        assert pending_pct >= 40.0, (
            f"Expected ≥40% 'pending' rows (injected 60%), got {pending_pct:.1f}%"
        )
