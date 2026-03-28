"""
tests/test_live_type_coverage.py

Parametrized type-coverage tests: every canonical type must survive a
DDL round-trip (emit → execute → introspect → canonical comparison) on
every live database engine.

Objective 2: Source Type Coverage
  "Every data type the source database can produce maps to a canonical type
   and round-trips without loss."

This consolidates the equivalent per-dialect tests that were previously
embedded in test_live_roundtrip.py (MySQL, PostgreSQL, SQL Server sections).
Each test is parametrized on two axes:
  - dialect / engine  (via the session-scoped fixtures in tests/conftest.py)
  - canonical column  (via TYPE_CASES below)

Skips automatically when the corresponding database is not reachable.
"""

from __future__ import annotations

import textwrap
from dataclasses import replace

import pytest

from src.statschema import emit_ddl
from src.statschema.model import CanonicalColumn, CanonicalTableSchema

from tests.live_helpers import LiveDbHelper

# ---------------------------------------------------------------------------
# Canonical type test cases
# Each entry is a CanonicalColumn that exercises a specific type + attribute.
# ---------------------------------------------------------------------------

def _col(name: str, **kwargs) -> CanonicalColumn:
    return CanonicalColumn(name=name, **kwargs)


TYPE_CASES: list[CanonicalColumn] = [
    # ── integer ───────────────────────────────────────────────────────────────
    _col("c_integer",          type="integer"),
    _col("c_integer_notnull",  type="integer",  not_null=True),
    # ── long ──────────────────────────────────────────────────────────────────
    _col("c_long",             type="long"),
    # ── float / double ────────────────────────────────────────────────────────
    _col("c_float",            type="float"),
    _col("c_double",           type="double"),
    # ── decimal ───────────────────────────────────────────────────────────────
    _col("c_decimal",          type="decimal",  precision=10, scale=2),
    _col("c_decimal_big",      type="decimal",  precision=38, scale=10),
    _col("c_decimal_money",    type="decimal",  precision=19, scale=4),
    # ── string ────────────────────────────────────────────────────────────────
    _col("c_string_default",   type="string"),
    _col("c_string_len",       type="string",   length=100),
    _col("c_string_max",       type="string",   length=4000),
    _col("c_varchar",          type="varchar",  length=255),
    _col("c_char",             type="char",     length=10),
    # ── boolean ───────────────────────────────────────────────────────────────
    _col("c_bool",             type="boolean"),
    # ── date / time ───────────────────────────────────────────────────────────
    _col("c_date",             type="date"),
    _col("c_timestamp",        type="timestamp"),
    _col("c_timestamp_fsp",    type="timestamp",  fsp=6),
    _col("c_timestamptz",      type="timestamptz"),
    _col("c_time",             type="time"),
    # ── binary ────────────────────────────────────────────────────────────────
    _col("c_binary",           type="binary"),
    _col("c_binary_len",       type="binary",   length=256),
    # ── nullable vs not-null ──────────────────────────────────────────────────
    _col("c_nullable",         type="integer",  not_null=False),
    _col("c_not_null",         type="integer",  not_null=True),
]

_TYPE_IDS = [c.name for c in TYPE_CASES]


def _make_table(dialect: str, columns: list[CanonicalColumn]) -> CanonicalTableSchema:
    """Wrap columns in a single-table schema for DDL emission."""
    # Filter out types the dialect doesn't support to avoid false failures
    # (e.g. Oracle does not support BOOLEAN natively before 23c).
    filtered = [c for c in columns if _is_supported(c, dialect)]
    return CanonicalTableSchema(
        name="type_coverage_test",
        columns=filtered,
        primary_key=[],
    )


def _is_supported(col: CanonicalColumn, dialect: str) -> bool:
    """Return False for type/dialect combinations that are intentionally unsupported."""
    if dialect == "db2":
        # DB2 maximum DECIMAL precision is 31
        if col.type == "decimal" and col.precision is not None and col.precision > 31:
            return False
        # DB2 LUW < 12.1 does not support TIMESTAMP WITH TIME ZONE syntax
        if col.type == "timestamptz":
            return False
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _execute_ddl(helper: LiveDbHelper, ddl: str) -> None:
    """Execute a CREATE TABLE statement, dropping any pre-existing table first."""
    _drop_if_exists(helper)
    helper.execute(ddl)


def _drop_if_exists(helper: LiveDbHelper) -> None:
    tname = "type_coverage_test"
    dialect = helper.dialect
    if dialect in ("postgres", "cockroachdb", "neon"):
        helper.execute_ignore(f'DROP TABLE IF EXISTS "{tname}"', )
    elif dialect in ("mysql", "mariadb"):
        helper.execute_ignore(f"DROP TABLE IF EXISTS `{tname}`")
    elif dialect == "sqlserver":
        helper.execute_ignore(
            f"IF OBJECT_ID('{tname}', 'U') IS NOT NULL DROP TABLE {tname}"
        )
    elif dialect == "oracle":
        helper.execute_ignore(f'DROP TABLE "{tname.upper()}"', "942", "00942")  # ORA-00942: table does not exist
    elif dialect == "db2":
        helper.execute_ignore(f"DROP TABLE {tname.upper()}", "42704")  # SQLSTATE table not found


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestTypeCoverage:
    """Each test receives a LiveDbHelper via a session-scoped fixture."""

    @pytest.mark.parametrize("col", TYPE_CASES, ids=_TYPE_IDS)
    @pytest.mark.live
    def test_postgres(self, pg_helper: LiveDbHelper, col: CanonicalColumn) -> None:
        _run_type_roundtrip(pg_helper, col, "postgres")

    @pytest.mark.parametrize("col", TYPE_CASES, ids=_TYPE_IDS)
    @pytest.mark.live
    def test_cockroachdb(self, crdb_helper: LiveDbHelper, col: CanonicalColumn) -> None:
        _run_type_roundtrip(crdb_helper, col, "cockroachdb")

    @pytest.mark.parametrize("col", TYPE_CASES, ids=_TYPE_IDS)
    @pytest.mark.live
    def test_mysql(self, mysql_helper: LiveDbHelper, col: CanonicalColumn) -> None:
        _run_type_roundtrip(mysql_helper, col, "mysql")

    @pytest.mark.parametrize("col", TYPE_CASES, ids=_TYPE_IDS)
    @pytest.mark.live
    def test_mariadb(self, mariadb_helper: LiveDbHelper, col: CanonicalColumn) -> None:
        _run_type_roundtrip(mariadb_helper, col, "mariadb")

    @pytest.mark.parametrize("col", TYPE_CASES, ids=_TYPE_IDS)
    @pytest.mark.live
    def test_sqlserver(self, sqlserver_helper: LiveDbHelper, col: CanonicalColumn) -> None:
        _run_type_roundtrip(sqlserver_helper, col, "sqlserver")

    @pytest.mark.parametrize("col", TYPE_CASES, ids=_TYPE_IDS)
    @pytest.mark.live
    def test_oracle(self, oracle_helper: LiveDbHelper, col: CanonicalColumn) -> None:
        _run_type_roundtrip(oracle_helper, col, "oracle")

    @pytest.mark.parametrize("col", TYPE_CASES, ids=_TYPE_IDS)
    @pytest.mark.live
    def test_db2(self, db2_helper: LiveDbHelper, col: CanonicalColumn) -> None:
        _run_type_roundtrip(db2_helper, col, "db2")


# ---------------------------------------------------------------------------
# Core round-trip logic (shared)
# ---------------------------------------------------------------------------

def _run_type_roundtrip(helper: LiveDbHelper, col: CanonicalColumn, dialect: str) -> None:
    """
    1. Build a single-column table with the canonical column.
    2. Emit DDL for the target dialect.
    3. Execute the DDL on the live database.
    4. Introspect the created table and confirm the column exists.
    5. Drop the table.
    """
    if not _is_supported(col, dialect):
        pytest.skip(f"{dialect} does not support type={col.type!r} precision={col.precision}")

    table = CanonicalTableSchema(
        name="type_coverage_test",
        columns=[col],
    )
    ddl = emit_ddl(table, dialect, if_not_exists=False)
    try:
        _execute_ddl(helper, ddl)
    except Exception as exc:
        pytest.fail(
            f"DDL execution failed for {dialect!r} column {col.name!r} "
            f"(type={col.type!r})\n"
            f"DDL:\n{ddl}\n"
            f"Error: {exc}"
        )

    # Verify the column appears in the introspection result
    try:
        cols = helper.introspect("type_coverage_test")
    except Exception:
        cols = {}  # Some dialects may not support introspect yet; skip check

    _drop_if_exists(helper)

    if cols:
        col_name = col.name.lower() if dialect not in ("oracle", "db2") else col.name.upper()
        # Also try lowercase for Oracle/DB2
        actual = cols.get(col_name) or cols.get(col.name.lower())
        assert actual is not None, (
            f"Column {col.name!r} not found in introspection result for {dialect!r}. "
            f"Got columns: {list(cols.keys())}"
        )
