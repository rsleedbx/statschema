"""
Strategy comparison benchmark.

Runs singleton / multi_row / bulk_copy against every reachable live database
for both TPC-C and TPC-H at a small scale factor, then appends a strategy
comparison table to benchmarks/results/benchmark_table.md.

Scale factors chosen so singleton completes in reasonable time on QEMU:
  TPC-H SF=0.001  →   8,690 rows  (lineitem=6,000 rows @ ~500 r/s = ~17s singleton)
  TPC-C SF=0.001  → 104,992 rows  (item fixed at 100K, skip singleton for QEMU)

Run from repo root:
    .venv_test/bin/python benchmarks/run_strategy_bench.py
"""

from __future__ import annotations

import os
import sys
import json
import time
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from benchmarks.run_bench import (
    connect, create_tables, RESULTS_DIR,
    _strip_pk_constraints, _emit_ddl_for_dialect,
)
from src.statschema.schema_io import load_canonical, resolve_load_order, resolve_row_counts
from src.statschema.row_generator import generate_rows
from src.statschema.data_loader import LoadStrategy, load_dataframe

# ---------------------------------------------------------------------------
# Scale factors
# ---------------------------------------------------------------------------
# TPC-H SF=0.001 — 8,690 rows, safe for singleton on all databases
TPCH_SF = 0.001
# TPC-C SF=0.001 — item is fixed 100K, too slow for singleton on QEMU;
# we skip singleton for TPC-C (multi_row and bulk_copy only)
TPCC_SF = 0.001

SCHEMAS_DIR = Path(__file__).parent / "schemas"

# Strategies to test per benchmark
STRATEGIES_TPCH = [LoadStrategy.SINGLETON, LoadStrategy.MULTI_ROW, LoadStrategy.BULK_COPY]
STRATEGIES_TPCC = [LoadStrategy.MULTI_ROW, LoadStrategy.BULK_COPY]  # skip singleton (100K item)

# Dialects that don't support BULK_COPY (fall back to MULTI_ROW silently)
_NO_BULK = {"oracle", "sqlite", "databricks"}


def _effective(strategy: LoadStrategy, dialect: str) -> LoadStrategy:
    """Bulk_copy is not available for all dialects; downgrade gracefully."""
    if strategy == LoadStrategy.BULK_COPY and dialect in _NO_BULK:
        return LoadStrategy.MULTI_ROW
    return strategy


# ---------------------------------------------------------------------------
# Target databases  (same as run_all_bench.py)
# ---------------------------------------------------------------------------

@dataclass
class DbTarget:
    label: str
    dialect: str
    env: dict[str, str]


def _pg(label, port):
    return DbTarget(label, "postgres", {"BENCH_PG_DSN": f"host=127.0.0.1 port={port} dbname=testdb user=postgres password=testpass"})

def _mysql(label, port):
    return DbTarget(label, "mysql", {"BENCH_MYSQL_HOST": "127.0.0.1", "BENCH_MYSQL_PORT": str(port), "BENCH_MYSQL_USER": "root", "BENCH_MYSQL_PASS": "testpass", "BENCH_MYSQL_DB": "testdb"})

def _mariadb(label, port):
    return DbTarget(label, "mariadb", {"BENCH_MYSQL_HOST": "127.0.0.1", "BENCH_MYSQL_PORT": str(port), "BENCH_MYSQL_USER": "root", "BENCH_MYSQL_PASS": "testpass", "BENCH_MYSQL_DB": "testdb"})


TARGETS = [
    _pg("PostgreSQL 14",   5414),
    _pg("PostgreSQL 16",   5416),
    _mysql("MySQL 5.7",    3357),
    _mariadb("MariaDB 10.11", 3310),
    _mariadb("MariaDB 11.4",  3311),
    DbTarget("SQL Server 2022", "sqlserver", {"BENCH_SQLSERVER_DSN": "SERVER=127.0.0.1,14330;DATABASE=master;UID=sa;PWD=Iedebaxoodoogee9choht7je1quohmuR"}),
    DbTarget("Oracle XE 21c",   "oracle",    {"BENCH_ORACLE_DSN": "127.0.0.1:1521/XE", "BENCH_ORACLE_USER": "system", "BENCH_ORACLE_PASS": "oracle"}),
    DbTarget("IBM Db2 CE 11.5", "db2",       {"BENCH_DB2_DSN": "DATABASE=testdb;HOSTNAME=127.0.0.1;PORT=50000;PROTOCOL=TCPIP;UID=db2inst1;PWD=testpass;"}),
]


# ---------------------------------------------------------------------------
# Single run
# ---------------------------------------------------------------------------

@dataclass
class StratResult:
    rows:   int   = 0
    load_s: float = 0.0
    rows_s: float = 0.0
    error:  str   = ""

    @property
    def ok(self) -> bool:
        return not self.error


def _run_one(target: DbTarget, schema_name: str, sf: float,
             strategy: LoadStrategy) -> StratResult:
    saved = {k: os.environ.get(k) for k in target.env}
    os.environ.update(target.env)
    try:
        conn = connect(target.dialect)
    except Exception as exc:
        _restore(saved)
        return StratResult(error=f"connect: {exc!s:.80}")

    try:
        tables_raw = load_canonical(SCHEMAS_DIR / f"{schema_name}_schema.yaml")
        ordered    = resolve_load_order(tables_raw)
        row_counts = resolve_row_counts(ordered, scale_factor=sf)

        eff_strategy = _effective(strategy, target.dialect)

        # Drop and recreate tables
        create_tables(conn, ordered, target.dialect)

        total_rows = 0
        t0 = time.perf_counter()

        for table in ordered:
            n    = row_counts[table.name]
            cols = [c.name for c in table.columns]
            insert_name = (
                table.name.upper() if target.dialect in ("oracle", "db2") else table.name
            )
            loaded = load_dataframe(
                generate_rows(table, n, parent_row_counts=row_counts, seed=42),
                conn, insert_name, target.dialect,
                strategy=eff_strategy, cols=cols, commit=True,
            )
            total_rows += loaded

        load_s = time.perf_counter() - t0
        conn.close()

        return StratResult(
            rows=total_rows,
            load_s=load_s,
            rows_s=total_rows / load_s if load_s > 0 else 0,
        )

    except Exception as exc:
        try:
            conn.close()
        except Exception:
            pass
        return StratResult(error=str(exc)[:120])
    finally:
        _restore(saved)


def _restore(saved):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _probe(target: DbTarget) -> bool:
    saved = {k: os.environ.get(k) for k in target.env}
    os.environ.update(target.env)
    try:
        conn = connect(target.dialect)
        conn.close()
        return True
    except Exception:
        return False
    finally:
        _restore(saved)


# ---------------------------------------------------------------------------
# Markdown builder
# ---------------------------------------------------------------------------

def _fmt(r: StratResult | None, dialect: str, strategy: LoadStrategy) -> str:
    eff = _effective(strategy, dialect)
    if r is None:
        return "—"
    if not r.ok:
        return "n/a"
    label = f"{int(r.rows_s):,} r/s"
    if eff != strategy:
        label += " ¹"   # footnote: strategy was downgraded
    return label


def _build_strategy_section(
    results: dict,          # {label: {schema: {strategy: StratResult}}}
    tpcc_sf: float,
    tpch_sf: float,
) -> str:
    rows = []

    header = (
        "## Strategy comparison\n\n"
        f"TPC-H SF={tpch_sf} ({8690:,} rows) and "
        f"TPC-C SF={tpcc_sf} ({104992:,} rows, item fixed at 100K).  \n"
        "Singleton strategy is skipped for TPC-C because the fixed 100K `item` "
        "table makes it impractically slow on network databases.  \n"
        "Strategy is measured as rows/s (higher is better).  \n"
        "¹ = dialect does not support bulk_copy; multi_row used instead.\n\n"
    )

    # TPC-H table
    rows.append("### TPC-H  — rows/s by strategy")
    rows.append("")
    rows.append("| Database | singleton | multi_row | bulk_copy |")
    rows.append("|:---------|----------:|----------:|----------:|")
    for label, data in results.items():
        d = data.get("tpch", {})
        dialect = data.get("dialect", "")
        s = _fmt(d.get(LoadStrategy.SINGLETON), dialect, LoadStrategy.SINGLETON)
        m = _fmt(d.get(LoadStrategy.MULTI_ROW),  dialect, LoadStrategy.MULTI_ROW)
        b = _fmt(d.get(LoadStrategy.BULK_COPY),  dialect, LoadStrategy.BULK_COPY)
        rows.append(f"| {label:<23} | {s:>15} | {m:>15} | {b:>15} |")

    rows.append("")

    # TPC-C table (no singleton)
    rows.append("### TPC-C  — rows/s by strategy  *(singleton skipped)*")
    rows.append("")
    rows.append("| Database | multi_row | bulk_copy |")
    rows.append("|:---------|----------:|----------:|")
    for label, data in results.items():
        d = data.get("tpcc", {})
        dialect = data.get("dialect", "")
        m = _fmt(d.get(LoadStrategy.MULTI_ROW),  dialect, LoadStrategy.MULTI_ROW)
        b = _fmt(d.get(LoadStrategy.BULK_COPY),  dialect, LoadStrategy.BULK_COPY)
        rows.append(f"| {label:<23} | {m:>15} | {b:>15} |")

    rows.append("")
    rows.append("¹ Oracle, SQLite, and Databricks have no native COPY protocol; bulk_copy falls back to multi_row.")
    rows.append("")

    return header + "\n".join(rows) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"\n{'='*72}")
    print(f"  Strategy benchmark   TPC-H SF={TPCH_SF}   TPC-C SF={TPCC_SF}")
    print(f"  strategies: singleton / multi_row / bulk_copy")
    print(f"{'='*72}\n")

    # results[label] = {dialect, tpch: {strategy: StratResult}, tpcc: {...}}
    results = {}

    for target in TARGETS:
        print(f"\n{'─'*72}")
        print(f"  {target.label}  ({target.dialect})")
        if not _probe(target):
            print(f"  SKIP — cannot connect")
            continue

        results[target.label] = {"dialect": target.dialect, "tpch": {}, "tpcc": {}}

        # TPC-H: all 3 strategies
        for strategy in STRATEGIES_TPCH:
            eff = _effective(strategy, target.dialect)
            tag = f"{strategy.value}" + ("→multi_row" if eff != strategy else "")
            print(f"    tpch  {tag:<22} ", end="", flush=True)
            r = _run_one(target, "tpch", TPCH_SF, strategy)
            if r.ok:
                print(f"{r.rows:>7,} rows  {r.load_s:6.1f}s  {r.rows_s:>9,.0f} r/s")
            else:
                print(f"ERR: {r.error[:60]}")
            results[target.label]["tpch"][strategy] = r

        # TPC-C: skip singleton
        for strategy in STRATEGIES_TPCC:
            eff = _effective(strategy, target.dialect)
            tag = f"{strategy.value}" + ("→multi_row" if eff != strategy else "")
            print(f"    tpcc  {tag:<22} ", end="", flush=True)
            r = _run_one(target, "tpcc", TPCC_SF, strategy)
            if r.ok:
                print(f"{r.rows:>7,} rows  {r.load_s:6.1f}s  {r.rows_s:>9,.0f} r/s")
            else:
                print(f"ERR: {r.error[:60]}")
            results[target.label]["tpcc"][strategy] = r

    # Save JSON
    out_json = RESULTS_DIR / "strategy_results.json"
    # Convert enum keys to strings for JSON
    serialisable = {
        label: {
            "dialect": data["dialect"],
            "tpch": {s.value: {"rows": r.rows, "load_s": r.load_s, "rows_s": r.rows_s, "error": r.error}
                     for s, r in data["tpch"].items()},
            "tpcc": {s.value: {"rows": r.rows, "load_s": r.load_s, "rows_s": r.rows_s, "error": r.error}
                     for s, r in data["tpcc"].items()},
        }
        for label, data in results.items()
    }
    out_json.write_text(json.dumps(serialisable, indent=2))

    # Append strategy section to benchmark_table.md
    bench_md = RESULTS_DIR / "benchmark_table.md"
    section  = _build_strategy_section(results, TPCC_SF, TPCH_SF)

    existing = bench_md.read_text() if bench_md.exists() else ""
    # Replace any previous strategy section, or append
    MARKER = "\n---\n\n## Strategy comparison\n"
    if MARKER in existing:
        existing = existing[:existing.index(MARKER)]
    bench_md.write_text(existing.rstrip() + "\n\n---\n\n" + section)

    print(f"\n\n{'='*72}")
    print(f"  Results saved → {bench_md.relative_to(_REPO_ROOT)}")
    print(f"{'='*72}\n")
    print(section)


if __name__ == "__main__":
    main()
