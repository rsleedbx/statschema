"""
TPC-x Identity Test — validates statschema's query-plan fidelity.

The identity test answers the core question:

    "If I use statschema to collect statistics from a source database and
     inject them into a new database with synthetic data, will query plans
     on the copy match plans on the original?"

This is a *control experiment*: we start with a known dataset (TPC-H, loaded
from the canonical YAML generator), run queries against it, then use
statschema's full collect → generate → inject pipeline to create a copy.
Matching query plans on the copy prove that statschema's statistics are
accurate enough to drive the optimizer to the same decisions.

Pipeline
--------
Phase A    LOAD SOURCE    Load TPC-x into <source_schema> from the YAML generator.
                          Run ANALYZE so the planner sees fresh stats.
Phase A.5  EXTENDED STATS (Layer 3 — optional, enabled by default)
                          Parse the benchmark query YAML to find column pairs that
                          appear together in JOIN ON / WHERE / GROUP BY on the same
                          table.  Issue CREATE STATISTICS for each pair on the
                          source and re-ANALYZE so functional-dependency and
                          combined n_distinct information is available.
Phase B    BASELINE       Run EXPLAIN (FORMAT JSON) on each benchmark query.
                          Record plan trees and row-count estimates.
Phase C    COLLECT        Run collect_table_stats() on every table in <source_schema>.
                          Persist to an in-memory dict (no YAML file required for
                          programmatic use; --save-yaml writes files).
Phase D    BUILD COPY     Create <target_schema>; emit DDL; generate synthetic rows
                          using build_rows_from_canonical() driven by the collected
                          statistics; load rows; run inject_stats_postgres().
Phase D.5  EXTENDED STATS Mirror the CREATE STATISTICS objects from Phase A.5 onto
                          <target_schema>.  ANALYZE to compute multi-column stats
                          from the synthetic data, then re-inject source single-column
                          stats so pg_restore_attribute_stats values take precedence.
Phase E    REPLAY         Run the same EXPLAIN queries against <target_schema>.
Phase F    SCORE          Compare plans:
                          • node_jaccard    — Jaccard of plan node-type multisets
                          • within_2x       — fraction of nodes where estimated
                                             row ratio ≤ 2× vs source estimate
                          • within_10x      — same threshold at 10×

Pass criteria (configurable via --threshold-*):
    node_jaccard  ≥ 0.70   (same join methods chosen)
    within_2x     ≥ 0.50   (majority of estimates within 2×)

Usage
-----
    # Full identity test at SF=1 on PostgreSQL (pg16 container)
    python benchmarks/identity_test.py \\
        --schema tpch --sf 1 --dialect postgres \\
        --dsn "host=127.0.0.1 port=5416 dbname=testdb user=postgres password=testpass"

    # Quick smoke test at SF=0.1 (fast, good for CI)
    python benchmarks/identity_test.py \\
        --schema tpch --sf 0.1 --dialect postgres --dsn "..."

    # Skip Phase A (reuse data already in source_schema):
    python benchmarks/identity_test.py --skip-load ...

    # Save collected YAML artifacts for inspection:
    python benchmarks/identity_test.py --save-yaml /tmp/identity_artifacts ...
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.statschema.schema_io import load_canonical, resolve_load_order, resolve_row_counts
from src.statschema.ddl_emitter import emit_ddl, emit_ddl_all
from src.statschema.row_generator import generate_rows
from src.statschema.data_loader import BatchConfig, LoadStrategy, load_dataframe
from src.statschema.db_stats_collector import (
    CollectionConfig,
    collect_table_stats,
    predicate_col_map_from_yaml,
    predicate_col_map_from_db,
)
from src.statschema.stats_model import TableStats
from src.statschema.stats_io import dump_stats, load_stats

logger = logging.getLogger(__name__)

from benchmarks.dialects import get as _get_dialect


def _strip_constraints(table):
    """Return a constraint-free copy of table for bulk benchmark loads."""
    stripped = [
        dataclasses.replace(col, primary_key=False, unique=False)
        for col in table.columns
    ]
    return dataclasses.replace(table, columns=stripped, fk_constraints=None, foreign_keys=None)


SCHEMAS_DIR   = Path(__file__).parent / "schemas"
QUERIES_DIR   = Path(__file__).parent / "queries"
RESULTS_DIR   = Path(__file__).parent / "results"

_SOURCE_SCHEMA = None  # derived from --schema at runtime: <schema>_src
_TARGET_SCHEMA = None  # derived from --schema at runtime: <schema>_tgt

# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class PlanMetrics:
    """Comparison metrics for a single query's EXPLAIN plan."""
    query_id: str
    source_node_types: list[str]
    target_node_types: list[str]
    node_jaccard: float          # Jaccard similarity of node-type multisets
    row_estimate_ratios: list[float]  # per-node: max(s/t, t/s), so ≥1
    within_2x: float             # fraction of nodes with ratio ≤ 2
    within_10x: float            # fraction of nodes with ratio ≤ 10
    source_plan_depth: int
    target_plan_depth: int


@dataclass
class IdentityTestResult:
    """Full identity test outcome for one (schema, dialect, sf) combination."""
    schema: str
    dialect: str
    sf: float
    source_schema: str
    target_schema: str
    # Row counts
    source_row_counts: dict[str, int] = field(default_factory=dict)
    target_row_counts: dict[str, int] = field(default_factory=dict)
    # Per-query plan metrics
    query_metrics: dict[str, PlanMetrics] = field(default_factory=dict)
    # Aggregate scores
    mean_node_jaccard: float = 0.0
    mean_within_2x: float = 0.0
    mean_within_10x: float = 0.0
    # Timing
    phase_times: dict[str, float] = field(default_factory=dict)
    # Pass / fail
    threshold_node_jaccard: float = 0.70
    threshold_within_2x: float = 0.50
    passed: bool = False
    failure_reason: str = ""
    # Stats injection mode:
    #   "injected"       — source histograms transplanted directly via
    #                      pg_restore_attribute_stats (PG 18+, full injection)
    #   "stats_analyze"  — stats-driven synthetic data loaded, then native
    #                      ANALYZE run on it; used by all non-PG engines and
    #                      by PG-wire engines that lack pg_restore_attribute_stats
    #   "none"           — no stats update attempted
    stats_injection_mode: str = "none"
    # When stats are collected from a different database engine than the target
    # (cross-database test), this records the source engine's dialect name.
    stats_source_dialect: str = ""
    # Git commit hash of the code that produced this result — set at save time
    git_commit: str = ""
    # Stats fidelity (Phase C.5) — how well did build_rows_from_canonical
    # reproduce the source statistics on the target schema?
    # Structure:
    #   mean_ndistinct_within_2x  float  — fraction of (table,col) pairs where
    #                                       target n_distinct is within 2× of source
    #   mean_range_covered        float  — fraction of numeric/date columns where
    #                                       target [min,max] stays within 5% of the
    #                                       source range (string columns excluded)
    #   mean_null_match           float  — fraction where null_fraction within 0.02
    #   per_table                 dict   — per-table breakdown (row_count_ok,
    #                                       ndistinct_within_2x, columns: {...})
    stats_fidelity: dict = field(default_factory=dict)

    def summary(self) -> str:
        lines = [
            f"\n{'='*70}",
            f"  Identity Test Result",
            f"  schema={self.schema}  dialect={self.dialect}  sf={self.sf}",
            f"  source={self.source_schema}  target={self.target_schema}",
            f"{'='*70}",
        ]
        # ── Stats fidelity ───────────────────────────────────────────────
        if self.stats_fidelity:
            nd   = self.stats_fidelity.get("mean_ndistinct_within_2x")
            rng  = self.stats_fidelity.get("mean_range_covered")
            null = self.stats_fidelity.get("mean_null_match")
            lines.append(
                f"  stats_fidelity: ndistinct_w2x={nd:.3f}  "
                f"range_covered={rng:.3f}  null_match={null:.3f}"
                if (nd is not None and rng is not None and null is not None)
                else "  stats_fidelity: (not collected)"
            )
            # Warn on any table with poor n_distinct fidelity
            for tname, td in self.stats_fidelity.get("per_table", {}).items():
                nd_t = td.get("ndistinct_within_2x", 1.0)
                if nd_t is not None and nd_t < 0.75:
                    lines.append(
                        f"    ⚠  {tname}: only {nd_t:.0%} of columns have "
                        f"matching n_distinct (target stats may not reflect source)"
                    )
        lines += [
            f"",
            f"  node_jaccard  : {self.mean_node_jaccard:.3f}  "
            f"(threshold ≥ {self.threshold_node_jaccard})",
            f"  within_2x     : {self.mean_within_2x:.3f}  "
            f"(threshold ≥ {self.threshold_within_2x})",
            f"  within_10x    : {self.mean_within_10x:.3f}",
            f"",
        ]
        for qid, m in self.query_metrics.items():
            lines.append(
                f"  {qid:<20}  jaccard={m.node_jaccard:.2f}  "
                f"within_2x={m.within_2x:.2f}  "
                f"nodes: {len(m.source_node_types)}→{len(m.target_node_types)}"
            )
        lines.append("")
        verdict = "PASS ✓" if self.passed else f"FAIL ✗  ({self.failure_reason})"
        lines.append(f"  Overall: {verdict}")
        for phase, t in self.phase_times.items():
            lines.append(f"    {phase:<25} {t:.2f}s")
        lines.append(f"{'='*70}\n")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Dialect dispatch — thin wrappers delegating to benchmarks/dialects/
# ---------------------------------------------------------------------------

def _connect(dialect: str, dsn: str):
    return _get_dialect(dialect).connect(dsn)


def _create_schema(conn, schema_name: str, dialect: str) -> None:
    _get_dialect(dialect).create_schema(conn, schema_name)


def _set_namespace(conn, schema_name: str, dialect: str):
    """Set namespace and return the (possibly new) connection.

    Always assign the return value:  conn = _set_namespace(conn, ...)
    For SQL Server this opens a fresh connection to the target database
    instead of issuing USE [db] (unsupported on Azure SQL Database).
    """
    return _get_dialect(dialect).set_namespace(conn, schema_name)


def _analyze_tables(
    conn,
    tables: list,
    schema: str,
    dialect: str,
    pred_col_map: dict | None = None,
    full_stats: bool = False,
    db2_tablesample_pct: float | None = None,
) -> None:
    """Run the engine's native ANALYZE / RUNSTATS / GATHER_TABLE_STATS.

    *pred_col_map* — ``{table_name: [col, ...]}`` from the workload YAML.
      When provided (and *full_stats* is False), DB2 and Oracle restrict
      expensive per-column distribution stats to predicate columns only,
      which is significantly faster for wide schemas.
    *full_stats* — if True, always gather all-column statistics regardless
      of the predicate map.  Useful for major-release validation runs.
    *db2_tablesample_pct* — DB2 only: ``TABLESAMPLE SYSTEM(n)`` page-sampling
      percentage (0 < n ≤ 100).  Reduces RUNSTATS scan time proportionally.
    """
    _get_dialect(dialect).analyze(
        conn, tables, schema,
        pred_col_map=pred_col_map, full_stats=full_stats,
        tablesample_pct=db2_tablesample_pct,
    )


def _strip_oracle_quotes(text: str) -> str:
    """Remove double-quote delimiters from Oracle identifiers.

    Used for both DDL (column names) and query SQL (identifier quoting),
    since Oracle stores unquoted identifiers as uppercase.
    """
    import re
    return re.sub(r'"([^"]+)"', r'\1', text)


# ---------------------------------------------------------------------------
# Phase A: load source data
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# SQL query transpilation and schema qualification
# ---------------------------------------------------------------------------

def _transpile_and_qualify(
    sql: str,
    schema_name: str,
    table_names: set[str],
    source_dialect: str,
    target_dialect: str,
) -> str:
    """Transpile SQL to target dialect and qualify unqualified table references."""
    import sqlglot
    import sqlglot.expressions as exp

    # Map identity-test dialect names to sqlglot dialect names.
    # "ansi" is not a valid sqlglot dialect; use None (default) so LIMIT/TIMESTAMP
    # are parsed correctly and then transpiled to the target dialect.
    _SQLGLOT_DIALECT: dict[str, str | None] = {
        "postgres": "postgres", "neon": "postgres", "cockroachdb": "postgres",
        "lakebase": "postgres", "mysql": "mysql", "mariadb": "mysql",
        "sqlserver": "tsql", "oracle": "oracle", "db2": "db2", "ansi": None,
    }
    read_d  = _SQLGLOT_DIALECT.get(source_dialect, None)
    write_d = _SQLGLOT_DIALECT.get(target_dialect, None)

    try:
        tree = sqlglot.parse_one(sql, read=read_d or None,
                                  error_level=sqlglot.ErrorLevel.WARN)
    except Exception:
        return sql  # parse failure — return as-is

    # Add schema qualifier to all unqualified table references
    tnames_lower = {t.lower() for t in table_names}
    for tbl in tree.find_all(exp.Table):
        if tbl.name.lower() in tnames_lower and not tbl.db:
            tbl.set("db", exp.Identifier(this=schema_name, quoted=True))

    try:
        return tree.sql(dialect=write_d or None)
    except Exception:
        return sql  # generation failure — return original


def _get_explain(conn, sql: str, schema: str, dialect: str) -> dict:
    """Dispatch to the correct EXPLAIN parser for the given dialect."""
    return _get_dialect(dialect).explain(conn, sql, schema)


def _explain_pg(conn, sql: str, schema: str) -> dict:
    """Convenience wrapper: run EXPLAIN on a PostgreSQL connection."""
    return _get_explain(conn, sql, schema, "postgres")


def _schema_table_ref(table_name: str, schema_name: str, dialect: str) -> str:
    """Return a fully-qualified, properly-quoted table reference for the dialect."""
    return _get_dialect(dialect).table_ref(table_name, schema_name)


def load_source(
    conn,
    schema: str,
    sf: float,
    source_schema: str,
    dialect: str,
    seed: int = 42,
    ctx: "Any | None" = None,
) -> dict[str, int]:
    """
    Phase A — create source_schema, load TPC-x data from the YAML generator,
    run ANALYZE, return row counts.
    """
    yaml_path = SCHEMAS_DIR / f"{schema}_schema.yaml"
    if not yaml_path.exists():
        raise FileNotFoundError(f"Schema file not found: {yaml_path}")

    tables        = load_canonical(yaml_path)
    ordered       = resolve_load_order(tables)
    row_counts    = resolve_row_counts(tables, scale_factor=sf)

    _create_schema(conn, source_schema, dialect)
    conn = _set_namespace(conn, source_schema, dialect)

    print(f"  [A] Creating tables in schema {source_schema!r}…", end=" ", flush=True)
    _if_not_exists = dialect in ("postgres", "neon", "cockroachdb", "lakebase")
    ddl_text = emit_ddl_all([_strip_constraints(t) for t in ordered], dialect=dialect,
                            if_not_exists=_if_not_exists)
    if dialect == "oracle":
        # Strip double-quote delimiters so column names resolve case-insensitively
        # (Oracle's default uppercase convention) rather than as lowercase-quoted identifiers.
        ddl_text = _strip_oracle_quotes(ddl_text)
    with conn.cursor() as cur:
        for stmt in ddl_text.split(";"):
            stmt = stmt.strip()
            if stmt:
                try:
                    cur.execute(stmt)
                except Exception as e:
                    logger.warning("DDL statement failed (%s): %s", dialect, e)
                    try:
                        conn.rollback()
                    except Exception:
                        pass
    try:
        conn.commit()
    except Exception:
        pass  # autocommit connections don't need explicit commit
    print("done")

    # Oracle uses oracledb direct_path_load, SQL Server uses mssql-python bulkcopy — both intercepted in data_loader before strategy dispatch.
    from src.statschema.data_loader import LoadStrategy as _LS
    _strat = _LS.BULK_COPY

    for table in ordered:
        n = row_counts[table.name]
        # Oracle DDL strips quotes → columns and tables become uppercase unquoted identifiers.
        _tname = table.name.upper() if dialect == "oracle" else table.name
        _cols  = ([c.name.upper() for c in table.columns] if dialect == "oracle"
                  else [c.name for c in table.columns])
        # DB2: ibm_db_dbi cannot auto-coerce int/float → CLOB/VARCHAR; pass column
        # types so bulk_load_db2 can str-ify non-string values for text columns.
        _ctypes = [c.type for c in table.columns] if dialect == "db2" else None
        print(f"  [A]   loading {table.name:<25} {n:>10,} rows…", end=" ", flush=True)
        t0 = time.perf_counter()
        load_dataframe(
            generate_rows(table, n, seed=seed, parent_row_counts=row_counts),
            conn, _tname, dialect,
            strategy=_strat,
            cols=_cols,
            col_types=_ctypes,
            commit=False,
            ctx=ctx,
        )
        try:
            conn.commit()
        except Exception:
            pass
        elapsed = time.perf_counter() - t0
        print(f"{elapsed:.2f}s")

    print(f"  [A] Running ANALYZE on {source_schema!r}…", end=" ", flush=True)
    # Phase A must always gather full distribution stats — Phase C reads from this schema.
    _analyze_tables(conn, ordered, source_schema, dialect, full_stats=True)
    print("done")

    actual = {}
    with conn.cursor() as cur:
        for t in ordered:
            tref = _schema_table_ref(t.name, source_schema, dialect)
            cur.execute(f"SELECT count(*) FROM {tref}")
            actual[t.name] = cur.fetchone()[0]
    return actual


# ---------------------------------------------------------------------------
# Phase B: baseline EXPLAIN plans
# ---------------------------------------------------------------------------

def collect_baseline_plans(
    conn,
    queries_yaml: Path,
    schema: str,
    dialect: str,
    verbose_plans: bool = False,
    table_names: set[str] | None = None,
) -> dict[str, dict]:
    """Phase B — run EXPLAIN for each query; return query_id → plan tree."""
    from src.statschema.query_model import load_queries
    workload = load_queries(queries_yaml)
    source_dialect = getattr(workload, "source_dialect", "ansi") or "ansi"
    plans = {}
    for entry in workload.queries:
        print(f"  [B] EXPLAIN {entry.id}…", end=" ", flush=True)
        try:
            # Transpile and qualify for non-PG dialects.
            sql = entry.sql
            if dialect in ("mysql", "mariadb", "sqlserver"):
                tnames = table_names or set()
                _q_schema = "dbo" if dialect == "sqlserver" else schema
                sql = _transpile_and_qualify(sql, _q_schema, tnames, source_dialect, dialect)
            elif dialect == "oracle":
                # Oracle DDL strips double-quotes (via _strip_oracle_quotes) so all
                # identifiers are stored uppercase. Apply the same stripping to query SQL
                # so "DimBroker" → DimBroker → Oracle auto-uppercases → DIMBROKER.
                import re as _re
                sql = _strip_oracle_quotes(sql)
                sql = _re.sub(r'\bLIMIT\s+(\d+)', r'FETCH FIRST \1 ROWS ONLY', sql,
                              flags=_re.IGNORECASE)
            elif dialect == "db2":
                # DB2 uses FETCH FIRST N ROWS ONLY; avoid sqlglot identifier quoting issues.
                import re as _re
                sql = _re.sub(r'\bLIMIT\s+(\d+)', r'FETCH FIRST \1 ROWS ONLY', sql,
                              flags=_re.IGNORECASE)
            plan = _get_explain(conn, sql, schema, dialect)
            plans[entry.id] = plan
            print("ok")
            if verbose_plans:
                nodes = _extract_plan_nodes(plan)
                for n in nodes:
                    print(f"       {n.get('Node Type','?'):<22} rows={n.get('Plan Rows','?')}")
        except Exception as exc:
            print(f"ERROR: {exc}")
            logger.warning("EXPLAIN failed for %s: %s", entry.id, exc)
            try:
                conn.rollback()
            except Exception:
                pass
    return plans


# ---------------------------------------------------------------------------
# Phase C: collect statistics
# ---------------------------------------------------------------------------

def collect_stats(
    conn,
    tables,
    source_schema: str,
    dialect: str,
    save_dir: Path | None = None,
    config: CollectionConfig | None = None,
) -> dict[str, TableStats]:
    """Phase C — collect column statistics from every table in source_schema.

    Parameters
    ----------
    config  Optional CollectionConfig enabling enrichment techniques.
            None = baseline collection only (existing behaviour).
    """
    def _safe_rb() -> None:
        try:
            conn.rollback()
        except Exception:
            pass

    # For SQL Server, tables sit in the 'dbo' schema within the named database.
    # After _set_namespace_sqlserver (USE [db]), pass 'dbo' as the schema to the collector.
    _stats_schema = "dbo" if dialect == "sqlserver" else source_schema

    all_stats: dict[str, TableStats] = {}
    for table in tables:
        print(f"  [C] collecting stats {table.name}…", end=" ", flush=True)
        try:
            _safe_rb()  # ensure clean state before each table
            # DB2 stores identifiers as uppercase in the system catalog (SYSCAT).
            # Pass uppercase names so catalog lookups match the quoted-uppercase DDL.
            _tbl_name  = table.name.upper()          if dialect == "db2" else table.name
            _sch_name  = _stats_schema.upper()       if dialect == "db2" else _stats_schema
            ts = collect_table_stats(
                conn, _tbl_name, dialect=dialect, schema=_sch_name, config=config,
            )
            _safe_rb()  # stats queries are read-only; release any open txn
            all_stats[table.name] = ts
            print(f"ok  (row_count={ts.row_count:,})")
        except Exception as exc:
            print(f"WARNING: {exc}")
            logger.warning("collect_table_stats failed for %s: %s", table.name, exc)
            try:
                conn.rollback()
            except Exception:
                pass
    if save_dir:
        save_dir.mkdir(parents=True, exist_ok=True)
        for name, ts in all_stats.items():
            out = save_dir / f"{name}_stats.yaml"
            dump_stats(ts, out)
            logger.info("Saved stats → %s", out)
    return all_stats


def _compare_stats(
    source: dict[str, "TableStats"],
    target: dict[str, "TableStats"],
) -> dict:
    """Compare source vs target statistics and return a fidelity summary.

    Per-column metrics:
      ndistinct_within_2x — target n_distinct in [src/2, src*2]
      range_covered       — for numeric/date columns: target [min,max] covers at
                            least 95% of the source range AND does not exceed the
                            source range by more than 5%.  String columns are
                            excluded (random extreme values are meaningless).
      null_match          — |src - tgt| ≤ 0.02
    """
    import datetime

    def _norm_str(v) -> str | None:
        if v is None:
            return None
        s = str(v)
        if len(s) > 10 and s[4] == "-" and s[7] == "-" and s[10] == " ":
            s = s[:10]
        return s

    def _to_numeric(v) -> float | None:
        """Try to parse a min/max value as a float or date ordinal."""
        if v is None:
            return None
        s = str(v).strip()
        # Trim datetime → date
        if len(s) > 10 and s[4] == "-" and s[7] == "-" and s[10] == " ":
            s = s[:10]
        # Try plain float
        try:
            return float(s)
        except ValueError:
            pass
        # Try ISO date
        for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
            try:
                d = datetime.datetime.strptime(s, fmt).date()
                return float(d.toordinal())
            except ValueError:
                pass
        return None  # string — not comparable numerically

    def _range_covered(s_min, s_max, t_min, t_max) -> bool | None:
        """Return True/False for numeric/date ranges, None for strings."""
        sv_min = _to_numeric(s_min)
        sv_max = _to_numeric(s_max)
        tv_min = _to_numeric(t_min)
        tv_max = _to_numeric(t_max)
        if any(x is None for x in (sv_min, sv_max, tv_min, tv_max)):
            return None  # string column — skip
        span = sv_max - sv_min
        if span == 0:
            # Constant column: target must also be constant at the same value
            return tv_min == sv_min and tv_max == sv_max
        tol = max(abs(span) * 0.05, 1.0)
        # Target must not undershoot source min by more than tol, and must not
        # overshoot source max by more than tol.
        return (tv_min >= sv_min - tol) and (tv_max <= sv_max + tol)

    nd_hits = nd_total = 0
    rng_hits = rng_total = 0
    null_hits = null_total = 0
    per_table: dict[str, dict] = {}

    for tname, src_ts in source.items():
        tgt_ts = target.get(tname)
        if tgt_ts is None:
            continue

        row_ok = (src_ts.row_count == tgt_ts.row_count)
        t_nd = t_nd_hit = 0
        t_rng = t_rng_hit = 0
        t_null = t_null_hit = 0
        col_detail: dict[str, dict] = {}

        # Normalize to lowercase so Oracle (uppercase) and PG (lowercase) match.
        src_cols = {c.name.lower(): c for c in (src_ts.columns or [])}
        tgt_cols = {c.name.lower(): c for c in (tgt_ts.columns or [])}

        for cname, sc in src_cols.items():
            tc = tgt_cols.get(cname)
            if tc is None:
                continue

            # n_distinct
            s_nd = sc.n_distinct or 0.0
            t_nd_val = tc.n_distinct or 0.0
            t_nd += 1
            nd_ok = False
            if s_nd == 0:
                nd_ok = (t_nd_val == 0)
            elif s_nd < 0 and t_nd_val < 0:
                nd_ok = (0.5 <= t_nd_val / s_nd <= 2.0)
            elif s_nd > 0 and t_nd_val > 0:
                nd_ok = (0.5 * s_nd <= t_nd_val <= 2.0 * s_nd)
            if nd_ok:
                t_nd_hit += 1

            # range coverage (numeric/date only)
            has_src_range = (sc.min_value is not None and sc.max_value is not None)
            has_tgt_range = (tc.min_value is not None and tc.max_value is not None)
            rng_ok: bool | None = None
            if has_src_range and has_tgt_range:
                rng_ok = _range_covered(sc.min_value, sc.max_value,
                                        tc.min_value, tc.max_value)
                if rng_ok is not None:   # numeric/date — count it
                    t_rng += 1
                    if rng_ok:
                        t_rng_hit += 1

            # null fraction
            s_nf = sc.null_fraction or 0.0
            t_nf = tc.null_fraction or 0.0
            t_null += 1
            null_ok = abs(s_nf - t_nf) <= 0.02
            if null_ok:
                t_null_hit += 1

            col_detail[cname] = {
                "src_n_distinct":  s_nd,
                "tgt_n_distinct":  t_nd_val,
                "nd_ok":           nd_ok,
                "src_min":         _norm_str(sc.min_value),
                "tgt_min":         _norm_str(tc.min_value),
                "src_max":         _norm_str(sc.max_value),
                "tgt_max":         _norm_str(tc.max_value),
                "range_ok":        rng_ok,
                "src_null_frac":   round(s_nf, 4),
                "tgt_null_frac":   round(t_nf, 4),
                "null_ok":         null_ok,
            }

        nd_hits   += t_nd_hit;   nd_total   += t_nd
        rng_hits  += t_rng_hit;  rng_total  += t_rng
        null_hits += t_null_hit; null_total += t_null

        per_table[tname] = {
            "row_count_ok":        row_ok,
            "src_row_count":       src_ts.row_count,
            "tgt_row_count":       tgt_ts.row_count,
            "ndistinct_within_2x": round(t_nd_hit / t_nd, 4) if t_nd   else None,
            "range_covered":       round(t_rng_hit / t_rng, 4) if t_rng else None,
            "null_match":          round(t_null_hit / t_null, 4) if t_null else None,
            "columns":             col_detail,
        }

    return {
        "mean_ndistinct_within_2x": round(nd_hits  / nd_total,   4) if nd_total   else None,
        "mean_range_covered":       round(rng_hits  / rng_total,  4) if rng_total  else None,
        "mean_null_match":          round(null_hits / null_total, 4) if null_total else None,
        "per_table": per_table,
    }


# ---------------------------------------------------------------------------
# Phases A.5 / D.5: extended statistics (query-driven, Layer 3)
# ---------------------------------------------------------------------------

def create_source_extended_stats(
    conn,
    queries_yaml: Path,
    tables,
    source_schema: str,
    dialect: str,
) -> dict[str, list[tuple[str, str]]]:
    """
    Phase A.5 — derive extended statistics from query patterns on the source.

    Parses every SQL query in *queries_yaml* to find column pairs from the same
    table that appear together in JOIN ON, WHERE, GROUP BY, or HAVING.  For each
    pair, ``CREATE STATISTICS`` is issued on the source schema so the subsequent
    ``ANALYZE`` computes functional-dependency coefficients and combined
    n_distinct values.

    This step is independent of predicate *values*: bind-variable placeholders
    (``:b1``, ``@p1``, ``?``) are transparent because the analysis only looks at
    which columns appear together structurally, not what values they carry.

    Returns
    -------
    stat_defs : dict[table_name → [(col_a, col_b)]]
        The column pairs that were registered.  Passed verbatim to
        ``create_target_extended_stats`` in Phase D.5 so the same objects are
        created on the target without re-parsing the queries.
    """
    # CockroachDB uses its own single-column statistics engine; pg_statistic_ext
    # functional-dependency stats are a PostgreSQL-specific concept.
    if dialect not in ("postgres", "lakebase", "neon"):
        return {}

    from src.statschema.query_analyzer import extract_column_pairs, make_stat_name

    table_names = {t.name.lower() for t in tables}
    stat_defs = extract_column_pairs(queries_yaml, table_names)

    if not stat_defs:
        print("  [A.5] No intra-table column pairs found — skipping extended stats")
        return {}

    total_pairs = sum(len(p) for p in stat_defs.values())
    print(f"  [A.5] Found {total_pairs} column pairs across "
          f"{len(stat_defs)} tables; creating statistics…", end=" ", flush=True)

    created = 0
    with conn.cursor() as cur:
        for table_name, pairs in stat_defs.items():
            for col_a, col_b in pairs:
                sname = make_stat_name(table_name, col_a, col_b)
                try:
                    cur.execute(
                        f'CREATE STATISTICS IF NOT EXISTS "{source_schema}"."{sname}" '
                        f'ON "{col_a}", "{col_b}" '
                        f'FROM "{source_schema}"."{table_name}"'
                    )
                    created += 1
                except Exception as exc:
                    logger.warning("CREATE STATISTICS source %s(%s,%s): %s",
                                   table_name, col_a, col_b, exc)
                    conn.rollback()
    conn.commit()

    print(f"done ({created} created)")
    print(f"  [A.5] Running ANALYZE for extended stats…", end=" ", flush=True)
    with conn.cursor() as cur:
        for table_name in stat_defs:
            try:
                cur.execute(f'ANALYZE "{source_schema}"."{table_name}"')
            except Exception as exc:
                logger.warning("ANALYZE source %s: %s", table_name, exc)
                conn.rollback()
    conn.commit()
    print("done")

    return stat_defs


def create_target_extended_stats(
    conn,
    stat_defs: dict[str, list[tuple[str, str]]],
    tables,
    collected_stats: dict[str, TableStats],
    target_schema: str,
    dialect: str,
) -> None:
    """
    Phase D.5 — mirror extended statistics onto the target schema.

    Creates the same ``CREATE STATISTICS`` objects that Phase A.5 registered on
    the source, runs ``ANALYZE`` to compute them from the synthetic data, then
    re-injects the source's single-column statistics via
    ``pg_restore_attribute_stats`` so the target ends up with:

    * ``pg_statistic``      — source distributions (accurate single-column stats)
    * ``pg_statistic_ext``  — target ANALYZE output (approximate multi-column
                              stats; structurally correct for functional
                              dependencies when FK ranges are declared)
    """
    if dialect not in ("postgres", "lakebase", "neon"):
        return
    if not stat_defs:
        return

    from src.statschema.stats_injector import inject_stats_postgres
    from src.statschema.query_analyzer import make_stat_name

    ordered = resolve_load_order(tables)

    print(f"  [D.5] Creating extended statistics in {target_schema!r}…",
          end=" ", flush=True)
    created = 0
    with conn.cursor() as cur:
        for table_name, pairs in stat_defs.items():
            for col_a, col_b in pairs:
                sname = make_stat_name(table_name, col_a, col_b)
                try:
                    cur.execute(
                        f'CREATE STATISTICS IF NOT EXISTS "{target_schema}"."{sname}" '
                        f'ON "{col_a}", "{col_b}" '
                        f'FROM "{target_schema}"."{table_name}"'
                    )
                    created += 1
                except Exception as exc:
                    logger.warning("CREATE STATISTICS target %s(%s,%s): %s",
                                   table_name, col_a, col_b, exc)
                    conn.rollback()
    conn.commit()
    print(f"done ({created} created)")

    print(f"  [D.5] Analyzing target tables for extended stats…",
          end=" ", flush=True)
    with conn.cursor() as cur:
        for table_name in stat_defs:
            try:
                cur.execute(f'ANALYZE "{target_schema}"."{table_name}"')
            except Exception as exc:
                logger.warning("ANALYZE target %s: %s", table_name, exc)
                conn.rollback()
    conn.commit()
    print("done")

    print(f"  [D.5] Re-injecting single-column stats after ANALYZE…",
          end=" ", flush=True)
    for table in ordered:
        if table.name in stat_defs:
            ts = collected_stats.get(table.name)
            if ts:
                try:
                    inj = inject_stats_postgres(conn, ts, schema=target_schema)
                    if inj.warnings:
                        for w in inj.warnings:
                            logger.warning("re-inject %s: %s", table.name, w)
                except Exception as exc:
                    logger.warning("re-inject stats failed %s: %s", table.name, exc)
                    try:
                        conn.rollback()
                    except Exception:
                        pass
    conn.commit()
    print("done")


# ---------------------------------------------------------------------------
# Phase D: build target copy
# ---------------------------------------------------------------------------

def build_target(
    conn,
    tables,
    collected_stats: dict[str, TableStats],
    target_schema: str,
    dialect: str,
    sf: float,
    seed: int = 99,
    fk_range_overrides: dict[str, dict[str, tuple[int, int]]] | None = None,
    pred_col_map: dict | None = None,
    full_stats: bool = False,
) -> tuple[dict[str, int], str]:
    """
    Phase D — create target_schema, load synthetic rows driven by collected
    statistics, then run inject_stats_postgres() to install the stats into
    the optimizer catalog.

    Parameters
    ----------
    pred_col_map
        ``{table_name: [col, ...]}`` from the workload YAML.  Passed to
        ``_analyze_tables`` so DB2 and Oracle only gather deep distribution
        stats for workload-predicate columns.  Pass ``None`` (default) for
        all-columns stats (same as pre-existing behaviour).
    full_stats
        If True, always run the most expensive ANALYZE variant regardless of
        *pred_col_map*.  Intended for major-release validation.
    fk_range_overrides
        ``{table_name: {col_name: (min, max)}}`` derived from query predicate
        analysis (Phase A.6).  Constrains FK column generation ranges so that
        synthetic fact rows are concentrated in the same data window as the
        real workload, improving join-order accuracy.
    """
    from src.statschema.stats_injector import inject_stats_postgres
    from src.statschema.pandas_builder import build_rows_from_canonical

    _create_schema(conn, target_schema, dialect)
    conn = _set_namespace(conn, target_schema, dialect)

    _is_pg_wire = dialect in ("postgres", "neon", "cockroachdb", "lakebase")

    print(f"  [D] Creating tables in schema {target_schema!r}…", end=" ", flush=True)
    _if_not_exists = dialect in ("postgres", "neon", "cockroachdb", "lakebase")
    ddl_text = emit_ddl_all([_strip_constraints(t) for t in tables], dialect=dialect,
                            if_not_exists=_if_not_exists)
    # Note: SQL Server uses USE [db] to switch context; no schema prefix needed in DDL.
    if dialect == "oracle":
        ddl_text = _strip_oracle_quotes(ddl_text)
    with conn.cursor() as cur:
        for stmt in ddl_text.split(";"):
            stmt = stmt.strip()
            if stmt:
                try:
                    cur.execute(stmt)
                except Exception as e:
                    logger.warning("DDL failed (%s): %s", dialect, e)
                    try:
                        conn.rollback()
                    except Exception:
                        pass
        if _is_pg_wire:
            # Suppress autovacuum so it cannot overwrite injected stats between D and E.
            _autovac_sql = (
                'SET (autovacuum_enabled = false)'
                if dialect == "cockroachdb"
                else 'SET (autovacuum_enabled = false, toast.autovacuum_enabled = false)'
            )
            for t in tables:
                try:
                    cur.execute(f'ALTER TABLE "{target_schema}"."{t.name}" {_autovac_sql}')
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
    conn.commit()
    print("done")

    # Disable CockroachDB background auto-stats for the duration of Phase D → Phase E.
    # Without this, auto-stats jobs triggered by data inserts can overwrite the ANALYZE
    # results (or our injected stats) before Phase E reads them, causing non-deterministic
    # within_2x scores.  Re-enabled after Phase E in the caller (run_identity_test).
    if dialect == "cockroachdb":
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "SET CLUSTER SETTING sql.stats.automatic_collection.enabled = false"
                )
            conn.autocommit = False
        except Exception:
            pass  # non-admin or older CRDB — proceed; may still have flaky stats

    ordered = resolve_load_order(tables)
    row_counts = resolve_row_counts(tables, scale_factor=sf)

    from src.statschema.data_loader import LoadStrategy as _LS
    _strat = _LS.BULK_COPY

    for table in ordered:
        n = row_counts[table.name]
        stats = collected_stats.get(table.name)
        _tname = table.name.upper() if dialect == "oracle" else table.name
        print(f"  [D]   synthetic {table.name:<25} {n:>10,} rows…", end=" ", flush=True)
        t0 = time.perf_counter()
        _cols = ([c.name.upper() for c in table.columns] if dialect == "oracle"
                 else [c.name for c in table.columns])
        if table.builtin_generator:
            # Tables with a deterministic built-in generator (e.g. date_dim, time_dim)
            # must use generate_rows — their rows are fully specified by the schema and
            # have unique constraints that stats-driven synthetic data cannot satisfy.
            df = generate_rows(table, n, seed=seed, parent_row_counts=row_counts)
        else:
            try:
                tbl_fk_overrides = (fk_range_overrides or {}).get(table.name)
                df = build_rows_from_canonical(
                    table, n, stats=stats, seed=seed,
                    parent_row_counts=row_counts,
                    fk_range_overrides=tbl_fk_overrides,
                )
                _cols = ([c.upper() for c in df.columns] if dialect == "oracle"
                         else None)
            except Exception as _gen_exc:
                logger.warning(
                    "build_rows_from_canonical failed for %s (%s); "
                    "falling back to simple generate_rows — stats-driven "
                    "generation was NOT used for this table",
                    table.name, _gen_exc,
                )
                print(f"\n    WARN {table.name}: stats-driven generator failed, "
                      f"using simple generator ({type(_gen_exc).__name__})",
                      end="", flush=True)
                df = generate_rows(table, n, seed=seed, parent_row_counts=row_counts)
        load_dataframe(
            df, conn, _tname, dialect,
            strategy=_strat,
            cols=_cols,
            commit=False,
        )
        try:
            conn.commit()
        except Exception:
            pass
        elapsed = time.perf_counter() - t0
        print(f"{elapsed:.2f}s")

    print(f"  [D] Updating statistics in {target_schema!r}…", end=" ", flush=True)
    injection_mode = "none"

    if _is_pg_wire:
        try:
            for table in ordered:
                ts = collected_stats.get(table.name)
                if ts:
                    inj = inject_stats_postgres(conn, ts, schema=target_schema)
                    if inj.warnings:
                        for w in inj.warnings:
                            logger.warning("inject_stats_postgres %s: %s", table.name, w)
                            print(f"\n    WARN {table.name}: {w.strip()}", end="", flush=True)
                    logger.info("injected %s: ok=%d skip=%d", table.name, inj.columns_injected, inj.columns_skipped)
            conn.commit()
            print("done")
            injection_mode = "injected"
        except RuntimeError as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"\n  [D] pg_restore_attribute_stats unavailable ({exc}); running ANALYZE…",
                  end=" ", flush=True)
            with conn.cursor() as cur:
                for t in ordered:
                    try:
                        cur.execute(f'ANALYZE "{target_schema}"."{t.name}"')
                    except Exception as ae:
                        logger.warning("ANALYZE failed for %s: %s", t.name, ae)
                        conn.rollback()
            conn.commit()
            print("done")
            injection_mode = "stats_analyze"
    else:
        # Non-PG dialects: run the engine's native ANALYZE equivalent on the
        # stats-driven synthetic data loaded above.
        _analyze_tables(conn, ordered, target_schema, dialect,
                        pred_col_map=pred_col_map, full_stats=full_stats)
        print("done (native ANALYZE)")
        injection_mode = "stats_analyze"

    actual = {}
    with conn.cursor() as cur:
        for t in ordered:
            tref = _schema_table_ref(t.name, target_schema, dialect)
            cur.execute(f"SELECT count(*) FROM {tref}")
            actual[t.name] = cur.fetchone()[0]
    return actual, injection_mode


# ---------------------------------------------------------------------------
# Phase F: score plan comparison
# ---------------------------------------------------------------------------

def _extract_plan_nodes(plan: dict) -> list[dict]:
    """Recursively collect all plan nodes from a PostgreSQL EXPLAIN JSON tree."""
    nodes = [plan]
    for child in plan.get("Plans", []):
        nodes.extend(_extract_plan_nodes(child))
    return nodes


def _plan_depth(plan: dict, depth: int = 0) -> int:
    children = plan.get("Plans", [])
    if not children:
        return depth
    return max(_plan_depth(c, depth + 1) for c in children)


def score_plans(
    query_id: str,
    source_plan: dict,
    target_plan: dict,
) -> PlanMetrics:
    """
    Compare two PostgreSQL EXPLAIN JSON plan trees and return PlanMetrics.

    node_jaccard:
        Jaccard similarity of node-type multisets.
        1.0 = identical set of operators; 0.0 = nothing in common.

    within_Nx:
        At each position in the flattened plan tree, compare the "Plan Rows"
        estimate from source vs target.  The ratio is always ≥ 1 (how many
        times off in either direction).  within_2x is the fraction of nodes
        where the ratio ≤ 2.

    Interpretation:
        node_jaccard ≥ 0.70  →  same join methods selected
        within_2x   ≥ 0.50  →  majority of row estimates within 2×
        → PASS: optimizer would make similar decisions on both databases
    """
    s_nodes = _extract_plan_nodes(source_plan)
    t_nodes = _extract_plan_nodes(target_plan)

    # Jaccard of node-type multisets
    s_types = Counter(n.get("Node Type", "?") for n in s_nodes)
    t_types = Counter(n.get("Node Type", "?") for n in t_nodes)
    all_types = set(s_types) | set(t_types)
    intersection = sum(min(s_types[tp], t_types[tp]) for tp in all_types)
    union        = sum(max(s_types[tp], t_types[tp]) for tp in all_types)
    jaccard = intersection / union if union else 1.0

    # Row-estimate ratios (zip aligned nodes)
    ratios: list[float] = []
    for s_node, t_node in zip(s_nodes, t_nodes):
        s_rows = s_node.get("Plan Rows", 1)
        t_rows = t_node.get("Plan Rows", 1)
        if s_rows > 0 and t_rows > 0:
            ratio = max(s_rows / t_rows, t_rows / s_rows)
            ratios.append(ratio)

    within_2x  = sum(1 for r in ratios if r <= 2.0)  / len(ratios) if ratios else 1.0
    within_10x = sum(1 for r in ratios if r <= 10.0) / len(ratios) if ratios else 1.0

    return PlanMetrics(
        query_id=query_id,
        source_node_types=[n.get("Node Type", "?") for n in s_nodes],
        target_node_types=[n.get("Node Type", "?") for n in t_nodes],
        node_jaccard=jaccard,
        row_estimate_ratios=ratios,
        within_2x=within_2x,
        within_10x=within_10x,
        source_plan_depth=_plan_depth(source_plan),
        target_plan_depth=_plan_depth(target_plan),
    )


# ---------------------------------------------------------------------------
# Full orchestrator
# ---------------------------------------------------------------------------

def run_identity_test(
    schema: str,
    sf: float,
    dialect: str,
    dsn: str,
    queries_yaml: Path | None = None,
    source_schema: str | None = None,
    target_schema: str | None = None,
    skip_load: bool = False,
    save_yaml: Path | None = None,
    seed: int = 42,
    threshold_node_jaccard: float = 0.70,
    threshold_within_2x: float = 0.50,
    use_extended_stats: bool = True,
    stats_source_dsn: str | None = None,
    stats_source_dialect: str | None = None,
    stats_source_schema: str | None = None,
    collection_config: CollectionConfig | None = None,
    phases: list[str] | None = None,
    full_stats: bool = False,
    **kwargs,
) -> IdentityTestResult:
    """
    Run the full identity test pipeline and return a scored IdentityTestResult.

    Parameters
    ----------
    schema          TPC schema name: "tpch", "tpcc", "tpcb", etc.
    sf              Scale factor for both source load and synthetic generation.
    dialect         Target dialect, e.g. "postgres".
    dsn             DB-API2 connection string.
    queries_yaml    Path to queries YAML file; defaults to
                    benchmarks/queries/<schema>.yaml.
    source_schema   Schema for the real TPC data on the target DB.
    target_schema   Schema for the statschema synthetic copy on the target DB.
    skip_load            If True, skip Phase A (reuse existing source_schema data).
    save_yaml            If set, write collected stats and schema artifacts here.
    use_extended_stats   If True (default), run Phases A.5 and D.5 — parse the
                         query workload to create multi-column CREATE STATISTICS
                         objects on both source and target (Layer 3 feature).
    stats_source_dsn      If set, collect Phase C statistics from this separate
                          database instead of from ``source_schema`` on the main
                          connection.  Enables cross-database tests where data
                          is loaded into the target DB (e.g. Lakebase) but stats
                          come from a different engine (e.g. MySQL).
    stats_source_dialect  Dialect of the stats-source DB (required when
                          ``stats_source_dsn`` is set).
    stats_source_schema   Schema in the stats-source DB to collect from.
                          Defaults to ``source_schema`` when not specified.
                          If the schema does not already contain data it will
                          be loaded fresh (Phase A on the stats-source connection).
    """
    if queries_yaml is None:
        queries_yaml = QUERIES_DIR / f"{schema}.yaml"
    if not queries_yaml.exists():
        raise FileNotFoundError(
            f"No queries file for schema {schema!r}: {queries_yaml}\n"
            f"Create benchmarks/queries/{schema}.yaml to enable query comparison."
        )

    if source_schema is None:
        source_schema = f"{schema}_src"
    if target_schema is None:
        target_schema = f"{schema}_tgt"

    yaml_path = SCHEMAS_DIR / f"{schema}_schema.yaml"
    tables    = load_canonical(yaml_path)
    ordered   = resolve_load_order(tables)

    result = IdentityTestResult(
        schema=schema,
        dialect=dialect,
        sf=sf,
        source_schema=source_schema,
        target_schema=target_schema,
        threshold_node_jaccard=threshold_node_jaccard,
        threshold_within_2x=threshold_within_2x,
        stats_source_dialect=stats_source_dialect or "",
    )

    conn = _connect(dialect, dsn)
    if dialect != "sqlserver":
        # mssql-python (SQL Server) requires autocommit=True for DDL (CREATE DATABASE,
        # DROP DATABASE, UPDATE STATISTICS) and sets it in _connect_sqlserver.
        # Do not override it here.
        try:
            conn.autocommit = False
        except (AttributeError, TypeError):
            pass  # ibm_db_dbi doesn't support setting autocommit post-connect

    _is_pg_wire = dialect in ("postgres", "neon", "cockroachdb", "lakebase")

    # Resolve active phases.  --skip-load is kept for backwards compat and
    # takes precedence over phases when both are given.
    _active_phases: set[str] = set(phases) if phases else {
        "load_source", "explain_source", "collect_stats",
        "load_target", "explain_target", "score",
    }
    if "load_source" not in _active_phases:
        skip_load = True

    # ── Phase A: Load source ─────────────────────────────────────────────────
    if skip_load:
        print("  [A] --skip-load: reusing existing source data")
        with conn.cursor() as cur:
            for t in ordered:
                try:
                    tref = _schema_table_ref(t.name, source_schema, dialect)
                    cur.execute(f"SELECT count(*) FROM {tref}")
                    result.source_row_counts[t.name] = cur.fetchone()[0]
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    result.source_row_counts[t.name] = 0
    else:
        t0 = time.perf_counter()
        result.source_row_counts = load_source(
            conn, schema, sf, source_schema, dialect, seed=seed
        )
        result.phase_times["A_load_source"] = time.perf_counter() - t0

    _verbose_plans = kwargs.get("verbose_plans", False)

    # ── Phase A.5: Extended stats from query patterns (Layer 3) ──────────────
    # Extended stats via CREATE STATISTICS are PostgreSQL-only.
    stat_defs: dict[str, list[tuple[str, str]]] = {}
    if use_extended_stats and _is_pg_wire:
        t0 = time.perf_counter()
        stat_defs = create_source_extended_stats(
            conn, queries_yaml, ordered, source_schema, dialect
        )
        result.phase_times["A5_source_extended_stats"] = time.perf_counter() - t0

    # ── Phase A.6: FK range inference from query predicates ──────────────────
    fk_range_overrides: dict[str, dict[str, tuple[int, int]]] = {}
    if use_extended_stats and queries_yaml:
        t0 = time.perf_counter()
        try:
            from src.statschema.query_analyzer import (
                extract_column_predicates,
                classify_tables,
                resolve_fk_generation_ranges,
            )
            from src.statschema.schema_io import resolve_row_counts as _resolve_row_counts
            _table_names = {t.name.lower() for t in ordered}
            _src_row_counts = result.source_row_counts or _resolve_row_counts(ordered, scale_factor=sf)
            _roles = classify_tables(ordered, _src_row_counts)
            _dim_tables = {tn for tn, role in _roles.items() if role in ("dimension", "lookup")}
            _predicates = extract_column_predicates(queries_yaml, _dim_tables)
            if _predicates:
                print(
                    f"  [A.6] {len(_predicates)} dimension predicates extracted; "
                    f"resolving FK ranges…",
                    end=" ", flush=True,
                )
                fk_range_overrides = resolve_fk_generation_ranges(
                    conn, source_schema, _predicates, ordered
                )
                n_overrides = sum(len(v) for v in fk_range_overrides.values())
                print(f"done ({n_overrides} FK column overrides)")
            else:
                print("  [A.6] No literal predicates on dimension columns found — skipping FK range inference")
        except Exception as exc:
            logger.warning("Phase A.6 FK range inference failed (non-fatal): %s", exc)
            print(f"  [A.6] FK range inference skipped ({exc})")
        result.phase_times["A6_fk_range_inference"] = time.perf_counter() - t0

    # ── Phase B: Baseline EXPLAIN plans ──────────────────────────────────────
    t0 = time.perf_counter()
    _table_names = {t.name for t in tables}
    source_plans = collect_baseline_plans(conn, queries_yaml, source_schema, dialect,
                                          verbose_plans=_verbose_plans,
                                          table_names=_table_names)
    result.phase_times["B_baseline_explain"] = time.perf_counter() - t0

    # ── Phase C prep: auto-build predicate_col_map for pred_cols technique ──
    # Priority: explicit --queries YAML > live query store > all-columns fallback.
    if collection_config and collection_config.pred_cols and not collection_config.predicate_col_map:
        _tbl_names = [t.name for t in ordered]
        if queries_yaml:
            # YAML provided explicitly (e.g. benchmark workload or exported queries).
            try:
                pred_map = predicate_col_map_from_yaml(str(queries_yaml), _tbl_names)
                collection_config.predicate_col_map = pred_map
                _n_pred = sum(len(v) for v in pred_map.values())
                print(f"  [C] pred_cols: {_n_pred} predicate columns from workload YAML"
                      f" across {len(pred_map)} tables")
            except Exception as exc:
                logger.warning("predicate_col_map_from_yaml failed: %s", exc)
        else:
            # No YAML: try the live source-DB query store (pg_stat_statements,
            # performance_schema, v$sql, sys.dm_exec_query_stats, …).
            _pred_conn    = _ss_conn    if stats_source_dsn else conn
            _pred_dialect = _ss_dialect if stats_source_dsn else dialect
            _pred_src_schema = stats_source_schema if stats_source_dsn else source_schema
            _pred_catalog = (_pred_src_schema if _pred_dialect in ("mysql", "mariadb") else None)
            _pred_schema  = (_pred_src_schema if _pred_dialect == "oracle" else None)
            try:
                pred_map = predicate_col_map_from_db(
                    _pred_conn, _pred_dialect, _tbl_names,
                    catalog=_pred_catalog, schema=_pred_schema,
                )
                if pred_map and any(pred_map.values()):
                    collection_config.predicate_col_map = pred_map
                    _n_pred = sum(len(v) for v in pred_map.values())
                    print(f"  [C] pred_cols: {_n_pred} predicate columns from live query"
                          f" store ({_pred_dialect}) across {len(pred_map)} tables")
                else:
                    print(f"  [C] pred_cols: query store empty or unavailable"
                          f" ({_pred_dialect}); collecting stats for all columns")
            except Exception as exc:
                logger.warning("predicate_col_map_from_db failed: %s", exc)

    # ── Phase C: Collect statistics ───────────────────────────────────────────
    t0 = time.perf_counter()
    if stats_source_dsn:
        # Cross-database mode: collect stats from a separate engine.
        # The stats-source schema must already contain data (loaded by the
        # caller or a prior same-database identity run on that engine).
        _ss_dialect = stats_source_dialect or dialect
        _ss_schema  = stats_source_schema or source_schema
        print(f"  [C] Cross-DB stats: collecting from {_ss_dialect} schema {_ss_schema!r}…")
        _ss_conn = _connect(_ss_dialect, stats_source_dsn)
        # SQL Server requires autocommit=True for DDL (CREATE DATABASE etc.);
        # _connect_sqlserver sets it — don't override it here.
        if _ss_dialect != "sqlserver":
            try:
                _ss_conn.autocommit = False
            except (AttributeError, TypeError):
                pass
        # For SQL Server and MySQL the "schema" is a DATABASE.  Switch context so
        # that subsequent catalog queries and table refs work without the 3-part
        # name.  (load_source calls _create_schema / _set_namespace internally.)
        if _ss_dialect in ("sqlserver", "mysql", "mariadb"):
            try:
                _ss_conn = _set_namespace(_ss_conn, _ss_schema, _ss_dialect)
            except Exception:
                pass  # schema may not exist yet; will be created below

        # Ensure the stats-source schema exists; if empty, load data there first.
        _ss_empty = False
        try:
            with _ss_conn.cursor() as _cur:
                _first = ordered[0]
                _tref = _schema_table_ref(_first.name, _ss_schema, _ss_dialect)
                _cur.execute(f"SELECT COUNT(*) FROM {_tref}")
                _ss_empty = (_cur.fetchone()[0] == 0)
        except Exception:
            try:
                _ss_conn.rollback()
            except Exception:
                pass
            _ss_empty = True
        if _ss_empty:
            print(f"  [C] Stats-source schema {_ss_schema!r} is empty — loading data…")
            load_source(_ss_conn, schema, sf, _ss_schema, _ss_dialect, seed=seed)
        collected_stats = collect_stats(
            _ss_conn, ordered, _ss_schema, _ss_dialect, save_dir=save_yaml,
            config=collection_config,
        )
        _ss_conn.close()
        # Cross-DB Phase C can be long (loading + collecting from source DB).
        # The target connection may have been closed by an application-level idle
        # timeout on the server side.  Reconnect before Phase D to avoid a stale
        # connection error on the first Lakebase DDL statement.
        try:
            conn.cursor().execute("SELECT 1")
        except Exception:
            conn.close()
            conn = _connect(dialect, dsn)
            conn.autocommit = False
    else:
        collected_stats = collect_stats(
            conn, ordered, source_schema, dialect, save_dir=save_yaml,
            config=collection_config,
        )
    result.phase_times["C_collect_stats"] = time.perf_counter() - t0

    # ── Phase D: Build copy ────────────────────────────────────────────────────
    t0 = time.perf_counter()
    _pred_map = (collection_config.predicate_col_map
                 if collection_config and not full_stats else None)
    result.target_row_counts, result.stats_injection_mode = build_target(
        conn, ordered, collected_stats, target_schema, dialect,
        sf=sf, seed=seed + 1000,
        fk_range_overrides=fk_range_overrides or None,
        pred_col_map=_pred_map,
        full_stats=full_stats,
    )
    result.phase_times["D_build_target"] = time.perf_counter() - t0

    # ── Phase D.5: Mirror extended stats onto target ───────────────────────────
    if use_extended_stats and stat_defs:
        t0 = time.perf_counter()
        create_target_extended_stats(
            conn, stat_defs, ordered, collected_stats, target_schema, dialect
        )
        result.phase_times["D5_target_extended_stats"] = time.perf_counter() - t0

    # ── Phase C.5: Collect target stats and compare vs source ─────────────────
    # Validates that build_rows_from_canonical actually reproduced the source
    # statistics — if it didn't, plan matches in Phase E are coincidental.
    # Uses the same collection_config as Phase C so pred_cols short-circuiting
    # and samp_nd apply here too, keeping C.5 at a similar cost as Phase C.
    t0 = time.perf_counter()
    try:
        print("  [C.5] Collecting target stats for fidelity check…")
        target_stats = collect_stats(conn, ordered, target_schema, dialect,
                                     config=collection_config)
        result.stats_fidelity = _compare_stats(collected_stats, target_stats)
        nd   = result.stats_fidelity.get("mean_ndistinct_within_2x")
        rng  = result.stats_fidelity.get("mean_range_covered")
        null = result.stats_fidelity.get("mean_null_match")
        print(
            f"  [C.5] stats fidelity: "
            f"ndistinct_w2x={nd:.3f}  range_covered={rng:.3f}  null_match={null:.3f}"
            if (nd is not None and rng is not None and null is not None)
            else "  [C.5] stats fidelity: (insufficient data)"
        )
    except Exception as _c5_exc:
        logger.warning("Phase C.5 stats comparison failed: %s", _c5_exc)
        try:
            conn.rollback()
        except Exception:
            pass
    result.phase_times["C5_target_stats_fidelity"] = time.perf_counter() - t0

    # Reconnect before Phase E so the new session starts with a fresh catalog
    # cache.  pg_restore_attribute_stats commits stats to pg_statistic, but the
    # local session's syscache may not process the invalidation within the same
    # connection, causing EXPLAIN to read stale (zero) statistics.
    conn.close()
    conn = _connect(dialect, dsn)
    try:
        conn.autocommit = False
    except (AttributeError, TypeError):
        pass

    # ── Phase E: Replay EXPLAIN on target ─────────────────────────────────────
    # Debug: verify that stats were committed and are visible before Phase E
    if _verbose_plans:
        try:
            with conn.cursor() as _cur:
                # Check pg_statistic directly (not the view) — bypasses privilege filtering
                _cur.execute("""
                    SELECT a.attname, s.stanullfrac, s.stadistinct
                    FROM pg_statistic s
                    JOIN pg_class c ON c.oid = s.starelid
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = s.staattnum
                    WHERE c.relname = 'lineitem' AND n.nspname = %s
                    AND a.attname IN ('l_returnflag','l_linestatus','l_shipdate')
                """, (target_schema,))
                rows = _cur.fetchall()
                print(f"  [E-debug] pg_statistic for {target_schema}.lineitem ({len(rows)} rows):")
                for r in rows:
                    print(f"    {r[0]}: null_frac={r[1]} stadistinct={r[2]}")
        except Exception as _e:
            print(f"  [E-debug] pg_statistic check failed: {_e}")
            try:
                conn.rollback()
            except Exception:
                pass

    t0 = time.perf_counter()
    target_plans = collect_baseline_plans(conn, queries_yaml, target_schema, dialect,
                                          verbose_plans=_verbose_plans,
                                          table_names=_table_names)
    result.phase_times["E_replay_explain"] = time.perf_counter() - t0

    # Re-enable CockroachDB auto-stats after Phase E EXPLAIN is done.
    if dialect == "cockroachdb":
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "SET CLUSTER SETTING sql.stats.automatic_collection.enabled = true"
                )
            conn.autocommit = False
        except Exception:
            pass

    # ── Phase F: Score ─────────────────────────────────────────────────────────
    for qid in source_plans:
        if qid in target_plans:
            metrics = score_plans(qid, source_plans[qid], target_plans[qid])
            result.query_metrics[qid] = metrics

    if result.query_metrics:
        result.mean_node_jaccard = sum(
            m.node_jaccard for m in result.query_metrics.values()
        ) / len(result.query_metrics)
        result.mean_within_2x = sum(
            m.within_2x for m in result.query_metrics.values()
        ) / len(result.query_metrics)
        result.mean_within_10x = sum(
            m.within_10x for m in result.query_metrics.values()
        ) / len(result.query_metrics)

    # Determine pass/fail
    if result.mean_node_jaccard < threshold_node_jaccard:
        result.failure_reason = (
            f"mean_node_jaccard {result.mean_node_jaccard:.3f} "
            f"< threshold {threshold_node_jaccard}"
        )
    elif result.mean_within_2x < threshold_within_2x:
        result.failure_reason = (
            f"mean_within_2x {result.mean_within_2x:.3f} "
            f"< threshold {threshold_within_2x}"
        )
    else:
        result.passed = True

    conn.close()
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="TPC-x identity test — validates statschema query-plan fidelity",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--schema",  default="tpch",
                   choices=["tpch", "tpcb", "tpcc", "tpcds", "tpcdi", "tpce"],
                   help="TPC schema to test (default: tpch)")
    p.add_argument("--sf",      type=float, default=1.0,
                   help="Scale factor for both source load and synthetic generation (default: 1)")
    p.add_argument("--dialect", default="postgres",
                   choices=["postgres", "lakebase", "neon", "cockroachdb",
                            "mysql", "mariadb", "sqlserver", "oracle", "db2"],
                   help="Database dialect (default: postgres)")
    p.add_argument("--dsn",     required=True,
                   help='DB-API2 connection string, e.g. "host=127.0.0.1 port=5416 '
                        'dbname=testdb user=postgres password=testpass"')
    p.add_argument("--queries", metavar="FILE",
                   help="Path to queries YAML (default: benchmarks/queries/<schema>.yaml)")
    p.add_argument("--source-schema", default=None,
                   help="Schema name for real TPC data (default: <schema>_src)")
    p.add_argument("--target-schema", default=None,
                   help="Schema name for statschema copy (default: <schema>_tgt)")
    p.add_argument("--skip-load", action="store_true",
                   help="Skip Phase A — reuse existing source_schema data")
    p.add_argument(
        "--phases", metavar="LIST",
        default="load_source,explain_source,collect_stats,load_target,explain_target,score",
        help=(
            "Comma-separated pipeline phases to run. "
            "Values: load_source (A), explain_source (B), collect_stats (C), "
            "load_target (D), explain_target (E), score (F). "
            "Default: all. "
            "Example: --phases explain_source,score (re-score from saved data)"
        ),
    )
    p.add_argument("--save-yaml", metavar="DIR",
                   help="Save collected stats + schema YAML artifacts to DIR")
    p.add_argument("--threshold-jaccard", type=float, default=0.70, metavar="FLOAT",
                   help="Minimum mean node_jaccard to pass (default: 0.70)")
    p.add_argument("--threshold-within-2x", type=float, default=0.50, metavar="FLOAT",
                   help="Minimum fraction of estimates within 2× to pass (default: 0.50)")
    p.add_argument("--seed",    type=int, default=42)
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--no-extended-stats", action="store_true",
                   help="Disable Phase A.5/D.5 extended statistics (Layer 3 query feature)")
    p.add_argument("--full-stats-db2-ora", action="store_true",
                   help=(
                       "DB2 and Oracle only.  Phase D: gather full per-column distribution "
                       "stats on the target schema (DB2: WITH DISTRIBUTION AND DETAILED "
                       "INDEXES ALL; Oracle: FOR ALL COLUMNS SIZE AUTO).  Default is "
                       "predicate-column-only stats, which is 5–10× faster.  "
                       "No effect on Postgres, MySQL, SQL Server, or CockroachDB.  "
                       "Use for major-release or audit runs."
                   ))
    p.add_argument(
        "--enrich", metavar="TECHNIQUES",
        help=(
            "Comma-separated list of enrichment techniques to apply during Phase C "
            "stats collection.  Available: full_mcv, ntile_hist, pred_cols, samp_nd, "
            "rank_corr, all.  Overrides --profile.  "
            "Example: --enrich full_mcv,ntile_hist,samp_nd"
        ),
    )
    p.add_argument(
        "--profile", metavar="NAME",
        help=(
            "Named collection profile from config/collection_profiles.yaml "
            "(off | light | standard | thorough).  "
            "When omitted, the profile is auto-selected from the source engine dialect. "
            "--enrich takes precedence over --profile when both are given."
        ),
    )
    p.add_argument(
        "--no-auto-profile", action="store_true",
        help="Disable automatic profile selection; collect only baseline stats.",
    )

    cross = p.add_argument_group(
        "Cross-database mode",
        "Collect Phase C statistics from a separate engine while keeping the\n"
        "target (Phases A, B, D, E) on the main --dsn.  Useful for N×Lakebase\n"
        "tests where stats come from MySQL/Oracle/etc. but EXPLAIN plans are\n"
        "compared on Lakebase.",
    )
    cross.add_argument(
        "--stats-source-dsn", metavar="DSN",
        help="DSN for the stats-collection source DB (e.g. MySQL, Oracle).",
    )
    cross.add_argument(
        "--stats-source-dialect",
        choices=["postgres", "mysql", "mariadb", "sqlserver", "oracle", "db2",
                 "lakebase", "neon", "cockroachdb"],
        help="Dialect of the stats-source DB (required with --stats-source-dsn).",
    )
    cross.add_argument(
        "--stats-source-schema", metavar="SCHEMA",
        help="Existing schema in the stats-source DB to collect from. "
             "If omitted, data is loaded fresh using the source_schema name.",
    )
    return p


def _parse_enrich(enrich_str: str | None) -> CollectionConfig | None:
    """Parse --enrich CSV into a CollectionConfig.  Returns None for baseline."""
    if not enrich_str:
        return None
    techniques = {t.strip().lower() for t in enrich_str.split(",")}
    if "all" in techniques:
        return CollectionConfig.all()
    return CollectionConfig(
        full_mcv="full_mcv" in techniques,
        ntile_hist="ntile_hist" in techniques,
        pred_cols="pred_cols" in techniques,
        samp_nd="samp_nd" in techniques,
        rank_corr="rank_corr" in techniques,
    )


# ---------------------------------------------------------------------------
# Collection profiles
# ---------------------------------------------------------------------------

_PROFILES_YAML = _REPO_ROOT / "config" / "collection_profiles.yaml"


def _load_profiles() -> dict:
    """Load config/collection_profiles.yaml.  Returns empty dict on failure."""
    try:
        import yaml  # PyYAML — already a dependency via ydata-sdk or similar
        return yaml.safe_load(_PROFILES_YAML.read_text())
    except Exception as exc:
        logger.warning("Could not load collection profiles from %s: %s", _PROFILES_YAML, exc)
        return {}


def _config_for_profile(profile_name: str, profiles_data: dict) -> CollectionConfig | None:
    """Return a CollectionConfig for a named profile, or None if not found."""
    entry = profiles_data.get("profiles", {}).get(profile_name)
    if entry is None:
        logger.warning("Profile %r not found in %s", profile_name, _PROFILES_YAML)
        return None
    return CollectionConfig.from_profile_dict(entry)


def _auto_profile(source_dialect: str | None, profiles_data: dict) -> CollectionConfig | None:
    """Select the default profile for a source engine dialect.

    Walks the `defaults` list in the YAML and returns the first matching
    CollectionConfig.  Returns None if the YAML is unavailable or the
    matched profile is 'off'.
    """
    if not source_dialect or not profiles_data:
        return None
    for rule in profiles_data.get("defaults", []):
        src = rule.get("source", "*")
        if src == "*" or src == source_dialect:
            pname = rule.get("profile", "standard")
            if pname == "off":
                return None
            return _config_for_profile(pname, profiles_data)
    return None


def main() -> None:
    args = _build_parser().parse_args()

    # Derive schema-name defaults from --schema so parallel runs don't collide.
    if args.source_schema is None:
        args.source_schema = f"{args.schema}_src"
    if args.target_schema is None:
        args.target_schema = f"{args.schema}_tgt"

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
    )

    # ── Resolve collection config (precedence: --enrich > --profile > auto) ──
    collection_config: CollectionConfig | None
    if args.enrich:
        # Explicit technique list always wins.
        collection_config = _parse_enrich(args.enrich)
    elif args.no_auto_profile:
        collection_config = None
    elif args.profile:
        profiles_data = _load_profiles()
        collection_config = _config_for_profile(args.profile, profiles_data)
        if collection_config is not None:
            print(f"  [C] Profile '{args.profile}' loaded from config/collection_profiles.yaml")
    else:
        # Auto-select based on the source engine dialect.
        src_dialect = args.stats_source_dialect or args.dialect
        profiles_data = _load_profiles()
        collection_config = _auto_profile(src_dialect, profiles_data)
        if collection_config is not None:
            # Find the matched profile name for the printout.
            matched = next(
                (r.get("profile") for r in profiles_data.get("defaults", [])
                 if r.get("source") in (src_dialect, "*")),
                "standard",
            )
            print(f"  [C] Auto-selected profile '{matched}' for source dialect '{src_dialect}'")

    result = run_identity_test(
        schema=args.schema,
        sf=args.sf,
        dialect=args.dialect,
        dsn=args.dsn,
        queries_yaml=Path(args.queries) if args.queries else None,
        source_schema=args.source_schema,
        target_schema=args.target_schema,
        skip_load=args.skip_load,
        save_yaml=Path(args.save_yaml) if args.save_yaml else None,
        seed=args.seed,
        threshold_node_jaccard=args.threshold_jaccard,
        threshold_within_2x=args.threshold_within_2x,
        use_extended_stats=not args.no_extended_stats,
        stats_source_dsn=args.stats_source_dsn,
        stats_source_dialect=args.stats_source_dialect,
        stats_source_schema=args.stats_source_schema,
        collection_config=collection_config,
        phases=args.phases.split(",") if args.phases else None,
        full_stats=args.full_stats_db2_ora,
    )

    print(result.summary())

    # Save JSON result
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone
    import subprocess as _sp
    import dataclasses
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    try:
        result.git_commit = _sp.check_output(
            ["git", "rev-parse", "HEAD"], cwd=_REPO_ROOT, text=True
        ).strip()
    except _sp.CalledProcessError:
        result.git_commit = "unknown"
    _from_suffix = f"-from-{args.stats_source_dialect}" if args.stats_source_dialect else ""
    out = RESULTS_DIR / f"{ts}-identity-{args.schema}-sf{args.sf}-{args.dialect}{_from_suffix}.json"
    out.write_text(json.dumps(dataclasses.asdict(result), indent=2, default=str))
    print(f"  Result saved → {out.relative_to(_REPO_ROOT)}\n")

    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
