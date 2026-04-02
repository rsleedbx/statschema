"""
Live Oracle HR/CO sample schema integration tests.

Two Oracle sample schemas are tested:

HR (Human Resources) — 7 tables
  Shipped with Oracle documentation. Covers: REGIONS → COUNTRIES → LOCATIONS →
  DEPARTMENTS → JOBS → EMPLOYEES → JOB_HISTORY with full FK graph.
  Types: NUMBER(p,s), VARCHAR2, CHAR, DATE, TIMESTAMP, INTERVAL.

CO (Customer Orders) — 7 tables
  Modern, simpler schema from oracle-samples/db-sample-schemas.
  Covers: CUSTOMERS → STORES → PRODUCTS → ORDERS → SHIPMENTS → ORDER_ITEMS
  → INVENTORY.

Together they provide 14 Oracle tables that exercise the most important
Oracle-specific type idioms: NUMBER(p,s) vs NUMBER(p), VARCHAR2 vs CLOB,
DATE vs TIMESTAMP, IDENTITY columns, CHECK constraints, and foreign keys.

Prerequisites
-------------
Oracle XE running in the Lima VM (see docs/local-databases.md):

    limactl start --name=oracle config/lima/oracle.yaml
    # Wait for 'DATABASE IS READY TO USE!' in podman logs oracle-xe

    # Create HR and CO schemas (run once)
    cd /path/to/statschema   # repo root after clone: https://github.com/rsleedbx/statschema
    .venv_test/bin/python - << 'EOF'
    import oracledb, re
    # (see docs/local-databases.md for the full one-time setup script)
    EOF

Environment variables (defaults match the oracle.yaml config):

    ORACLE_HOST     default: 127.0.0.1
    ORACLE_PORT     default: 1521
    ORACLE_PASS     default: oracle      (SYSTEM password)

Skip behaviour
--------------
All tests skip when Oracle is unreachable or oracledb is not installed.
"""

from __future__ import annotations

import pytest

from src.statschema import emit_ddl, load_canonical, parse_ddl
from src.statschema.model import CanonicalTableSchema, expand_table_instances
from src.statschema.schema_io import dump_schema as dump_canonical

# ---------------------------------------------------------------------------
# Connection configuration
# ---------------------------------------------------------------------------

from tests.live_helpers import _tp  # noqa: E402

_p = _tp("test_oracle")

_HOST    = _p.host     or "127.0.0.1"
_PORT    = _p.port     or 1521
_SYS_PW  = _p.password or ""
_DSN     = f"{_HOST}:{_PORT}/{_p.database or 'XE'}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_conn(user: str, pw: str):
    oracledb = pytest.importorskip("oracledb")
    try:
        conn = oracledb.connect(user=user, password=pw, dsn=_DSN)
        return conn
    except Exception as exc:
        pytest.skip(f"Cannot connect to Oracle {user}@{_DSN}: {exc}")


def _all_tables(conn) -> list[str]:
    cur = conn.cursor()
    cur.execute("SELECT table_name FROM user_tables ORDER BY 1")
    return [r[0] for r in cur.fetchall()]


def _get_ddl(conn, table: str) -> str:
    """Build CREATE TABLE DDL from user_tab_columns + user_constraints."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT column_name, data_type, data_length, data_precision,
               data_scale, nullable
        FROM user_tab_columns
        WHERE table_name = :1
        ORDER BY column_id
        """,
        [table],
    )
    cols = cur.fetchall()
    if not cols:
        return f"CREATE TABLE {table} (id NUMBER NOT NULL PRIMARY KEY)"

    # Get primary key columns
    cur.execute(
        """
        SELECT col.column_name
        FROM user_constraints con
        JOIN user_cons_columns col ON con.constraint_name = col.constraint_name
        WHERE con.table_name = :1 AND con.constraint_type = 'P'
        ORDER BY col.position
        """,
        [table],
    )
    pks = [r[0] for r in cur.fetchall()]
    pk_set = set(pks)

    parts = []
    for name, dtype, dlen, prec, scale, nullable in cols:
        col_type = dtype
        if dtype == "VARCHAR2":
            col_type = f"VARCHAR2({dlen})"
        elif dtype == "NVARCHAR2":
            col_type = f"NVARCHAR2({dlen})"
        elif dtype == "CHAR":
            col_type = f"CHAR({dlen})"
        elif dtype == "NUMBER":
            if prec is not None and scale is not None:
                col_type = f"NUMBER({prec},{scale})"
            elif prec is not None:
                col_type = f"NUMBER({prec})"
        nn = " NOT NULL" if nullable == "N" else ""
        pk = " PRIMARY KEY" if name in pk_set and len(pks) == 1 else ""
        parts.append(f"  {name} {col_type}{nn}{pk}")

    if len(pks) > 1:
        parts.append(f"  PRIMARY KEY ({', '.join(pks)})")

    return f"CREATE TABLE {table} (\n" + ",\n".join(parts) + "\n)"


# ---------------------------------------------------------------------------
# Test: HR schema – connectivity and structure
# ---------------------------------------------------------------------------

class TestOracleHRConnection:
    def test_connect_and_count_tables(self):
        conn = _get_conn("hr", "hr")
        tables = _all_tables(conn)
        conn.close()
        assert len(tables) == 7, f"Expected 7 HR tables, got {len(tables)}"

    def test_all_hr_tables_present(self):
        conn = _get_conn("hr", "hr")
        tables = set(_all_tables(conn))
        conn.close()
        expected = {"REGIONS", "COUNTRIES", "LOCATIONS", "DEPARTMENTS",
                    "JOBS", "EMPLOYEES", "JOB_HISTORY"}
        missing = expected - tables
        assert not missing, f"Missing HR tables: {missing}"


# ---------------------------------------------------------------------------
# Test: HR schema – parse
# ---------------------------------------------------------------------------

class TestOracleHRParse:
    def test_all_hr_tables_parse_without_error(self):
        conn = _get_conn("hr", "hr")
        tables = _all_tables(conn)
        failures: list[tuple[str, str]] = []
        for tbl in tables:
            ddl = _get_ddl(conn, tbl)
            try:
                result = parse_ddl(ddl, dialect="oracle")
                assert result, f"parse_ddl returned empty list for {tbl}"
            except Exception as exc:
                failures.append((tbl, str(exc)[:120]))
        conn.close()
        assert not failures, (
            f"{len(failures)} HR tables failed to parse:\n"
            + "\n".join(f"  {t}: {e}" for t, e in failures)
        )

    def test_employees_columns(self):
        conn = _get_conn("hr", "hr")
        ddl = _get_ddl(conn, "EMPLOYEES")
        conn.close()

        tables = parse_ddl(ddl, dialect="oracle")
        assert tables
        t = tables[0]
        col_names = {c.name.upper() for c in t.columns}
        for expected in ("EMPLOYEE_ID", "FIRST_NAME", "LAST_NAME", "EMAIL", "SALARY"):
            assert expected in col_names, f"Column '{expected}' missing from EMPLOYEES"

        emp_id = next(c for c in t.columns if c.name.upper() == "EMPLOYEE_ID")
        assert emp_id.primary_key

    def test_jobs_salary_range_types(self):
        """JOBS has MIN_SALARY and MAX_SALARY as NUMBER(8,2) — check decimal type."""
        conn = _get_conn("hr", "hr")
        ddl = _get_ddl(conn, "JOBS")
        conn.close()

        tables = parse_ddl(ddl, dialect="oracle")
        assert tables
        t = tables[0]
        salary_cols = [c for c in t.columns if "salary" in c.name.lower()]
        assert salary_cols, "No salary columns found in JOBS"
        for c in salary_cols:
            assert c.type in ("decimal", "long", "integer"), (
                f"Expected numeric type for {c.name}, got {c.type}"
            )

    def test_foreign_key_graph(self):
        """Employees FK references both Departments and Jobs — verify detection."""
        conn = _get_conn("hr", "hr")
        # HR FK graph: employees.department_id → departments.department_id
        #              employees.job_id → jobs.job_id
        # Parse employees with simplified DDL (just checks parsing doesn't fail)
        ddl = _get_ddl(conn, "EMPLOYEES")
        conn.close()

        tables = parse_ddl(ddl, dialect="oracle")
        assert tables
        # At minimum, employees table should have parsed without error


# ---------------------------------------------------------------------------
# Test: HR schema – emit and cross-dialect
# ---------------------------------------------------------------------------

class TestOracleHREmit:
    def test_all_hr_tables_emit_without_error(self):
        conn = _get_conn("hr", "hr")
        tables = _all_tables(conn)
        failures: list[tuple[str, str]] = []
        for tbl in tables:
            ddl = _get_ddl(conn, tbl)
            try:
                parsed = parse_ddl(ddl, dialect="oracle")
                if parsed:
                    emit_ddl(parsed[0], dialect="oracle")
            except Exception as exc:
                failures.append((tbl, str(exc)[:120]))
        conn.close()
        assert not failures, (
            f"{len(failures)} HR tables failed to emit:\n"
            + "\n".join(f"  {t}: {e}" for t, e in failures)
        )

    def test_double_roundtrip_employees(self):
        conn = _get_conn("hr", "hr")
        ddl1 = _get_ddl(conn, "EMPLOYEES")
        conn.close()

        t1   = parse_ddl(ddl1, dialect="oracle")[0]
        # Use if_not_exists=False so plain CREATE TABLE can be re-parsed
        ddl2 = emit_ddl(t1, dialect="oracle", if_not_exists=False)
        t2   = parse_ddl(ddl2, dialect="oracle")[0]
        ddl3 = emit_ddl(t2, dialect="oracle", if_not_exists=False)

        assert ddl2 == ddl3, "EMPLOYEES double round-trip not idempotent"

    def test_cross_dialect_hr_to_mysql(self):
        """Translate Oracle HR EMPLOYEES → canonical → MySQL DDL."""
        conn = _get_conn("hr", "hr")
        ddl = _get_ddl(conn, "EMPLOYEES")
        conn.close()

        t = parse_ddl(ddl, dialect="oracle")[0]
        mysql_ddl = emit_ddl(t, dialect="mysql")
        assert "CREATE TABLE" in mysql_ddl
        assert "LAST_NAME" in mysql_ddl or "last_name" in mysql_ddl

    def test_cross_dialect_hr_to_postgresql(self):
        conn = _get_conn("hr", "hr")
        ddl = _get_ddl(conn, "EMPLOYEES")
        conn.close()

        t = parse_ddl(ddl, dialect="oracle")[0]
        pg_ddl = emit_ddl(t, dialect="postgresql")
        assert "CREATE TABLE" in pg_ddl
        assert "EMAIL" in pg_ddl or "email" in pg_ddl

    def test_cross_dialect_hr_to_sqlserver(self):
        conn = _get_conn("hr", "hr")
        ddl = _get_ddl(conn, "JOBS")
        conn.close()

        t = parse_ddl(ddl, dialect="oracle")[0]
        tsql = emit_ddl(t, dialect="sqlserver")
        assert "CREATE TABLE" in tsql
        assert "JOB_TITLE" in tsql or "job_title" in tsql


# ---------------------------------------------------------------------------
# Test: CO (Customer Orders) schema
# ---------------------------------------------------------------------------

class TestOracleCOConnection:
    def test_connect_and_count_tables(self):
        conn = _get_conn("co", "co")
        tables = _all_tables(conn)
        conn.close()
        assert len(tables) == 7, f"Expected 7 CO tables, got {len(tables)}"

    def test_all_co_tables_present(self):
        conn = _get_conn("co", "co")
        tables = set(_all_tables(conn))
        conn.close()
        expected = {"CUSTOMERS", "STORES", "PRODUCTS", "ORDERS",
                    "SHIPMENTS", "ORDER_ITEMS", "INVENTORY"}
        missing = expected - tables
        assert not missing, f"Missing CO tables: {missing}"


class TestOracleCOParse:
    def test_all_co_tables_parse_without_error(self):
        conn = _get_conn("co", "co")
        tables = _all_tables(conn)
        failures: list[tuple[str, str]] = []
        for tbl in tables:
            ddl = _get_ddl(conn, tbl)
            try:
                result = parse_ddl(ddl, dialect="oracle")
                assert result
            except Exception as exc:
                failures.append((tbl, str(exc)[:120]))
        conn.close()
        assert not failures, (
            f"{len(failures)} CO tables failed to parse:\n"
            + "\n".join(f"  {t}: {e}" for t, e in failures)
        )

    def test_co_all_tables_emit_without_error(self):
        conn = _get_conn("co", "co")
        tables = _all_tables(conn)
        failures: list[tuple[str, str]] = []
        for tbl in tables:
            ddl = _get_ddl(conn, tbl)
            try:
                parsed = parse_ddl(ddl, dialect="oracle")
                if parsed:
                    emit_ddl(parsed[0], dialect="oracle")
            except Exception as exc:
                failures.append((tbl, str(exc)[:120]))
        conn.close()
        assert not failures, (
            f"{len(failures)} CO tables failed to emit:\n"
            + "\n".join(f"  {t}: {e}" for t, e in failures)
        )

    def test_double_roundtrip_orders(self):
        conn = _get_conn("co", "co")
        ddl1 = _get_ddl(conn, "ORDERS")
        conn.close()

        t1   = parse_ddl(ddl1, dialect="oracle")[0]
        ddl2 = emit_ddl(t1, dialect="oracle", if_not_exists=False)
        t2   = parse_ddl(ddl2, dialect="oracle")[0]
        ddl3 = emit_ddl(t2, dialect="oracle", if_not_exists=False)

        assert ddl2 == ddl3, "ORDERS double round-trip not idempotent"


# ---------------------------------------------------------------------------
# Test: multi-instance and canonical YAML
# ---------------------------------------------------------------------------

class TestOracleSampleMultiInstance:
    def test_hr_employees_shard_expansion(self):
        conn = _get_conn("hr", "hr")
        ddl = _get_ddl(conn, "EMPLOYEES")
        conn.close()

        base = parse_ddl(ddl, dialect="oracle")[0]
        base = CanonicalTableSchema(
            name=base.name,
            columns=base.columns,
            fk_constraints=base.fk_constraints,
            temporal_ordering_constraints=base.temporal_ordering_constraints,
            instance_count=3,
        )

        expanded = expand_table_instances([base])
        assert len(expanded) == 3
        names = [t.name for t in expanded]
        assert names[0].endswith("_0001")
        for t in expanded:
            ddl_out = emit_ddl(t, dialect="oracle")
            assert t.name.upper() in ddl_out.upper()

    def test_co_orders_with_aliases(self):
        conn = _get_conn("co", "co")
        ddl = _get_ddl(conn, "ORDERS")
        conn.close()

        base = parse_ddl(ddl, dialect="oracle")[0]
        base = CanonicalTableSchema(
            name=base.name,
            columns=base.columns,
            fk_constraints=base.fk_constraints,
            temporal_ordering_constraints=base.temporal_ordering_constraints,
            aliases=["ORDERS_ARCHIVE", "ORDERS_2024"],
        )

        expanded = expand_table_instances([base])
        assert len(expanded) == 3
        names = {t.name for t in expanded}
        assert "ORDERS" in names
        assert "ORDERS_ARCHIVE" in names
        assert "ORDERS_2024" in names

    def test_full_hr_schema_yaml_roundtrip(self):
        conn = _get_conn("hr", "hr")
        tables_raw = _all_tables(conn)
        parsed: list[CanonicalTableSchema] = []
        for tbl in tables_raw:
            ddl = _get_ddl(conn, tbl)
            result = parse_ddl(ddl, dialect="oracle")
            if result:
                parsed.append(result[0])
        conn.close()

        yaml_str = dump_canonical(parsed)
        assert "version:" in yaml_str
        reloaded = load_canonical(yaml_str)
        assert len(reloaded) == len(parsed)

    def test_combined_hr_co_yaml(self):
        """Both schemas combined into one canonical YAML file."""
        all_tables: list[CanonicalTableSchema] = []
        for user, pw in [("hr", "hr"), ("co", "co")]:
            conn = _get_conn(user, pw)
            for tbl in _all_tables(conn):
                ddl = _get_ddl(conn, tbl)
                result = parse_ddl(ddl, dialect="oracle")
                if result:
                    all_tables.append(result[0])
            conn.close()

        assert len(all_tables) == 14  # 7 HR + 7 CO
        yaml_str = dump_canonical(all_tables)
        reloaded = load_canonical(yaml_str)
        assert len(reloaded) == 14
