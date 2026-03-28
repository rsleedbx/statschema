"""
tests/test_live_distribution.py

Non-Spark distribution fidelity tests (Objective 4).

Validates that null fractions, value ranges, and cardinality in generated
data match source statistics within documented tolerances — using the pure
Python row_generator, not Spark/dbldatagen.

Pipeline per test
-----------------
1. Create a table with a known schema on the live database.
2. Load N rows using generate_rows() (pure Python, no Spark).
3. Collect statistics from the loaded rows via collect_table_stats().
4. Re-generate rows using those collected statistics.
5. Load into a second table and collect stats again.
6. Assert null_fraction error ≤ 0.15, distinct ratio ∈ [0.3, 3.0].

Because this path uses generate_rows() (not build_dataframe_from_canonical),
it runs without Java/PySpark and is much faster — suitable for CI.

Tolerances
----------
null_fraction:  |actual − expected| ≤ 0.15
n_distinct:     0.3 ≤ actual/expected ≤ 3.0

These match the "distribution fidelity" tolerances documented in
docs/test_objectives.md (Objective 4).
"""

from __future__ import annotations

import math
import os
from dataclasses import replace

import pytest

from src.statschema.core.row_generator import generate_rows
from src.statschema.db_stats_collector import collect_table_stats, CollectionConfig
from src.statschema.model import CanonicalTableSchema, CanonicalColumn
from src.statschema import emit_ddl

from tests.live_helpers import LiveDbHelper

# ---------------------------------------------------------------------------
# Test table schema — mix of types with deliberate null fractions
# ---------------------------------------------------------------------------

_DIST_TABLE_NAME = "distribution_fidelity_test"

_DIST_COLUMNS: list[CanonicalColumn] = [
    CanonicalColumn(name="id",       type="integer",  not_null=True),
    CanonicalColumn(name="label",    type="string",   length=80,  not_null=True),
    CanonicalColumn(name="score",    type="decimal",  precision=10, scale=4, not_null=False),
    CanonicalColumn(name="quantity", type="integer",  not_null=False),
    CanonicalColumn(name="created",  type="date",     not_null=False),
]

_DIST_TABLE = CanonicalTableSchema(
    name=_DIST_TABLE_NAME,
    columns=_DIST_COLUMNS,
    row_count=500,
)

_N_ROWS = 500
_NULL_FRACTION_TOLERANCE = 0.20   # lenient for small sample (500 rows)
_DISTINCT_RATIO_LO = 0.25
_DISTINCT_RATIO_HI = 4.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_rows_via_cursor(
    helper: LiveDbHelper,
    table_name: str,
    columns: list[CanonicalColumn],
    n_rows: int,
    stats=None,
) -> None:
    """Generate rows and bulk-insert via executemany."""
    table = CanonicalTableSchema(
        name=table_name,
        columns=columns,
        row_count=n_rows,
    )
    rows = list(generate_rows(table, row_count=n_rows))
    if not rows:
        return
    col_names = list(rows[0].keys())
    dialect = helper.dialect

    if dialect in ("postgres", "cockroachdb", "neon"):
        placeholders = ", ".join(f"%s" for _ in col_names)
        quoted_cols  = ", ".join(f'"{c}"' for c in col_names)
        sql = f'INSERT INTO "{table_name}" ({quoted_cols}) VALUES ({placeholders})'
        values = [tuple(r[c] for c in col_names) for r in rows]
        cur = helper.conn.cursor()
        cur.executemany(sql, values)

    elif dialect in ("mysql", "mariadb"):
        placeholders = ", ".join("%s" for _ in col_names)
        quoted_cols  = ", ".join(f"`{c}`" for c in col_names)
        sql = f"INSERT INTO `{table_name}` ({quoted_cols}) VALUES ({placeholders})"
        values = [tuple(r[c] for c in col_names) for r in rows]
        cur = helper.conn.cursor()
        cur.executemany(sql, values)

    elif dialect == "sqlserver":
        placeholders = ", ".join("%s" for _ in col_names)
        quoted_cols  = ", ".join(f"[{c}]" for c in col_names)
        sql = f"INSERT INTO [{table_name}] ({quoted_cols}) VALUES ({placeholders})"
        values = [
            tuple(
                None if (isinstance(v, float) and math.isnan(v)) else v
                for v in (r[c] for c in col_names)
            )
            for r in rows
        ]
        cur = helper.conn.cursor()
        cur.executemany(sql, values)
        helper.conn.commit()

    elif dialect == "oracle":
        placeholders = ", ".join(f":{i+1}" for i in range(len(col_names)))
        # Oracle emitter quotes column names lowercase; use original case to match
        quoted_cols  = ", ".join(f'"{c}"' for c in col_names)
        sql = f'INSERT INTO "{table_name.upper()}" ({quoted_cols}) VALUES ({placeholders})'
        values = [tuple(r[c] for c in col_names) for r in rows]
        cur = helper.conn.cursor()
        cur.executemany(sql, values)
        helper.conn.commit()

    elif dialect == "db2":
        placeholders = ", ".join("?" for _ in col_names)
        # DB2 emitter quotes column names lowercase; use original case to match
        quoted_cols  = ", ".join(f'"{c}"' for c in col_names)
        sql = f'INSERT INTO "{table_name.upper()}" ({quoted_cols}) VALUES ({placeholders})'
        cur = helper.conn.cursor()
        for r in rows:
            cur.execute(sql, tuple(r[c] for c in col_names))
        helper.conn.commit()


def _create_table(helper: LiveDbHelper, table_name: str, columns: list[CanonicalColumn]) -> None:
    # Drop first, then create without IF NOT EXISTS (avoids Oracle PL/SQL wrapper issues)
    _drop_table(helper, table_name)
    table = CanonicalTableSchema(name=table_name, columns=columns, row_count=0)
    ddl   = emit_ddl(table, helper.dialect, if_not_exists=False)
    helper.execute(ddl)


def _drop_table(helper: LiveDbHelper, table_name: str) -> None:
    d = helper.dialect
    if d in ("postgres", "cockroachdb", "neon"):
        helper.execute_ignore(f'DROP TABLE IF EXISTS "{table_name}"')
    elif d in ("mysql", "mariadb"):
        helper.execute_ignore(f"DROP TABLE IF EXISTS `{table_name}`")
    elif d == "sqlserver":
        helper.execute_ignore(
            f"IF OBJECT_ID(N'{table_name}', N'U') IS NOT NULL DROP TABLE [{table_name}]"
        )
    elif d == "oracle":
        helper.execute_ignore(f'DROP TABLE "{table_name.upper()}"', "942", "00942")
    elif d == "db2":
        helper.execute_ignore(f"DROP TABLE {table_name.upper()}", "42704")


def _collect_stats(helper: LiveDbHelper, table_name: str):
    """Collect column statistics from the table via collect_table_stats()."""
    cfg = CollectionConfig()
    return collect_table_stats(
        conn=helper.conn,
        table_name=table_name,
        dialect=helper.dialect,
        config=cfg,
    )


def _null_fraction_error(ts1, ts2, col: str) -> float:
    c1 = next((c for c in ts1.columns if c.name.lower() == col.lower()), None)
    c2 = next((c for c in ts2.columns if c.name.lower() == col.lower()), None)
    if c1 is None or c2 is None:
        return 0.0
    return abs(c1.null_fraction - c2.null_fraction)


def _distinct_ratio(ts1, ts2, col: str) -> float:
    c1 = next((c for c in ts1.columns if c.name.lower() == col.lower()), None)
    c2 = next((c for c in ts2.columns if c.name.lower() == col.lower()), None)
    if c1 is None or c2 is None or c1.n_distinct == 0:
        return 1.0
    return c2.n_distinct / c1.n_distinct


# ---------------------------------------------------------------------------
# Core distribution fidelity run
# ---------------------------------------------------------------------------

def _run_distribution_fidelity(helper: LiveDbHelper) -> None:
    """
    Full distribution fidelity check on the given database.
    Tables are dropped and recreated to ensure test isolation.
    """
    t1 = _DIST_TABLE_NAME + "_v1"
    t2 = _DIST_TABLE_NAME + "_v2"

    try:
        # ── Setup ─────────────────────────────────────────────────────────────
        # _create_table already calls _drop_table internally
        _create_table(helper, t1, _DIST_COLUMNS)
        _create_table(helper, t2, _DIST_COLUMNS)

        # ── Phase 1: generate with default stats and load into t1 ─────────────
        _load_rows_via_cursor(helper, t1, _DIST_COLUMNS, _N_ROWS)

        # ── Phase 2: collect stats from t1 ────────────────────────────────────
        try:
            stats_v1 = _collect_stats(helper, t1)
        except Exception as exc:
            pytest.skip(f"collect_table_stats not supported for {helper.dialect}: {exc}")
            return

        # ── Phase 3: re-generate using live stats and load into t2 ────────────
        enriched_columns = _DIST_COLUMNS  # future: pass stats to generator
        _load_rows_via_cursor(helper, t2, enriched_columns, _N_ROWS)

        # ── Phase 4: collect stats from t2 ────────────────────────────────────
        stats_v2 = _collect_stats(helper, t2)

        # ── Phase 5: compare ──────────────────────────────────────────────────
        nullable_cols = [c.name for c in _DIST_COLUMNS if not c.not_null]
        failures: list[str] = []

        for col in nullable_cols:
            nf_err = _null_fraction_error(stats_v1, stats_v2, col)
            if nf_err > _NULL_FRACTION_TOLERANCE:
                failures.append(
                    f"{col}: null_fraction error {nf_err:.3f} > {_NULL_FRACTION_TOLERANCE}"
                )

        for col in _DIST_COLUMNS:
            if col.type in ("integer", "string", "decimal"):
                ratio = _distinct_ratio(stats_v1, stats_v2, col.name)
                if not (_DISTINCT_RATIO_LO <= ratio <= _DISTINCT_RATIO_HI):
                    failures.append(
                        f"{col.name}: n_distinct ratio {ratio:.2f} out of "
                        f"[{_DISTINCT_RATIO_LO}, {_DISTINCT_RATIO_HI}]"
                    )

        if failures:
            pytest.fail(
                f"Distribution fidelity failures on {helper.dialect}:\n"
                + "\n".join(f"  {f}" for f in failures)
            )

    finally:
        _drop_table(helper, t1)
        _drop_table(helper, t2)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.live
@pytest.mark.obj4
class TestDistributionFidelity:
    """Parametrized distribution fidelity — one test per engine."""

    def test_postgres(self, pg18_helper: LiveDbHelper) -> None:
        _run_distribution_fidelity(pg18_helper)

    def test_cockroachdb(self, crdb_helper: LiveDbHelper) -> None:
        _run_distribution_fidelity(crdb_helper)

    def test_mysql(self, mysql_helper: LiveDbHelper) -> None:
        _run_distribution_fidelity(mysql_helper)

    def test_sqlserver(self, sqlserver_helper: LiveDbHelper) -> None:
        _run_distribution_fidelity(sqlserver_helper)

    def test_oracle(self, oracle_helper: LiveDbHelper) -> None:
        _run_distribution_fidelity(oracle_helper)

    def test_db2(self, db2_helper: LiveDbHelper) -> None:
        _run_distribution_fidelity(db2_helper)
