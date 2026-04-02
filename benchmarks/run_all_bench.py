"""
Run TPC-C (SF=1) and TPC-H (SF=0.1) on every reachable live database and write
benchmarks/results/benchmark_table.md with a timing summary table.

Rows = database + version.  Columns = TPC-C / TPC-H (rows, total_s, rows/s).

Run from the repo root:
    .venv_test/bin/python benchmarks/run_all_bench.py
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from benchmarks.bench_config import BENCH_TPCC_SF as TPCC_SF, BENCH_TPCH_SF as TPCH_SF
from benchmarks.run_bench import run_benchmark, connect, RESULTS_DIR, _DEFAULT_BENCH_YAML
from src.statschema.data_loader import LoadStrategy

# ---------------------------------------------------------------------------
# Database matrix
# ---------------------------------------------------------------------------

@dataclass
class DbTarget:
    label:   str            # display name (e.g. "PostgreSQL 16")
    dialect: str            # statschema dialect
    strategy: LoadStrategy
    profile: str            # profile name in config/statschema.tpcb.yaml
    db_name: str = "bench"  # logical name for display
    skip: bool = False


TARGETS: list[DbTarget] = [
    DbTarget("PostgreSQL 14",    "postgres",    LoadStrategy.BULK_COPY,  "tpcb_postgres14"),
    DbTarget("PostgreSQL 16",    "postgres",    LoadStrategy.BULK_COPY,  "tpcb_postgres16"),
    DbTarget("PostgreSQL 18",    "postgres",    LoadStrategy.BULK_COPY,  "tpcb_postgres18"),
    DbTarget("CockroachDB",      "cockroachdb", LoadStrategy.BULK_COPY,  "tpcb_cockroachdb"),
    DbTarget("MySQL 5.7",        "mysql",       LoadStrategy.BULK_COPY,  "tpcb_mysql57"),
    DbTarget("MySQL 8.0",        "mysql",       LoadStrategy.BULK_COPY,  "tpcb_mysql8"),
    DbTarget("MariaDB 10.11",    "mariadb",     LoadStrategy.BULK_COPY,  "tpcb_mariadb_lts"),
    DbTarget("MariaDB 11.4",     "mariadb",     LoadStrategy.BULK_COPY,  "tpcb_mariadb_new"),
    DbTarget("SQL Server 2022",  "sqlserver",   LoadStrategy.MULTI_ROW,  "tpcb_sqlserver"),
    DbTarget("Oracle XE 21c",    "oracle",      LoadStrategy.MULTI_ROW,  "tpcb_oracle"),
    DbTarget("IBM Db2 CE 11.5",  "db2",         LoadStrategy.MULTI_ROW,  "tpcb_db2"),
]

# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

@dataclass
class BenchResult:
    label:   str
    schema:  str
    rows:    int    = 0
    load_s:  float  = 0.0
    rows_s:  float  = 0.0
    error:   str    = ""

    @property
    def ok(self) -> bool:
        return not self.error


def run_one(target: DbTarget, schema: str, sf: float) -> BenchResult:
    try:
        result = run_benchmark(
            schema=schema, sf=sf, dialect=target.dialect,
            strategy=target.strategy, out_dir=RESULTS_DIR, seed=42,
            profile_yaml=_DEFAULT_BENCH_YAML, profile_name=target.profile,
        )
        totals = result["totals"]
        return BenchResult(
            label=target.label, schema=schema,
            rows=totals["rows"],
            load_s=totals["load_s"],
            rows_s=totals["rows_per_s"],
        )
    except Exception as exc:
        return BenchResult(label=target.label, schema=schema, error=str(exc)[:120])


def probe(target: DbTarget) -> bool:
    """Return True if the database is reachable."""
    try:
        conn = connect(target.dialect, profile_yaml=_DEFAULT_BENCH_YAML, profile_name=target.profile)
        conn.close()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Markdown table builder
# ---------------------------------------------------------------------------

def _cell(r: BenchResult | None) -> str:
    if r is None:
        return "—"
    if not r.ok:
        return "ERR"
    rows_k = f"{r.rows / 1000:.0f}K"
    return f"{r.load_s:.1f}s · {int(r.rows_s / 1000)}K r/s · {rows_k} rows"


def build_markdown(
    results: dict[str, dict[str, BenchResult]],
    tpcc_sf: float,
    tpch_sf: float,
) -> str:
    # Column order: TPC-C cols first, then TPC-H
    labels = list(results.keys())

    header_tpcc = f"TPC-C SF={tpcc_sf:.0f}"
    header_tpch = f"TPC-H SF={tpch_sf}"

    col_w = 34
    db_w  = 22

    sep_db   = "-" * db_w
    sep_col  = "-" * col_w

    lines = [
        "# TPC Benchmark Results",
        "",
        f"Scale factors: TPC-C SF={tpcc_sf:.0f} (~{599:,}K rows), TPC-H SF={tpch_sf} (~87K rows).  ",
        "Strategy: BULK_COPY for PostgreSQL / MySQL / MariaDB; MULTI_ROW for SQL Server / Oracle / Db2.  ",
        "Generator: `generate_rows()` driven by canonical schema YAML (`benchmarks/schemas/`).  ",
        "Seed: 42 (reproducible).  ",
        "",
        f"| {'Database':<{db_w}} | {header_tpcc:<{col_w}} | {header_tpch:<{col_w}} |",
        f"|:{sep_db}:|:{sep_col}:|:{sep_col}:|",
    ]

    for label in labels:
        row = results[label]
        tpcc_cell = _cell(row.get("tpcc"))
        tpch_cell = _cell(row.get("tpch"))
        lines.append(
            f"| {label:<{db_w}} | {tpcc_cell:<{col_w}} | {tpch_cell:<{col_w}} |"
        )

    lines += [
        "",
        "---",
        "",
        "**Columns:** `total_load_s · rows/s (K) · rows loaded`  ",
        "**ERR** = connection failed or load error (see `benchmarks/results/*.json` for details)  ",
        "**—** = not attempted  ",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print(f"\n{'='*72}")
    print(f"  statschema multi-database TPC benchmark")
    print(f"  TPC-C SF={TPCC_SF:.0f}  TPC-H SF={TPCH_SF}")
    print(f"  targets: {len(TARGETS)}")
    print(f"{'='*72}\n")

    results: dict[str, dict[str, BenchResult]] = {}

    for target in TARGETS:
        print(f"\n{'─'*72}")
        print(f"  Probing {target.label} ({target.dialect}) ...")
        if not probe(target):
            print(f"  SKIP — cannot connect to {target.label}")
            results[target.label] = {
                "tpcc": BenchResult(target.label, "tpcc", error="unreachable"),
                "tpch": BenchResult(target.label, "tpch", error="unreachable"),
            }
            continue

        print(f"  Connected to {target.label}")
        results[target.label] = {}

        # TPC-C
        print(f"\n  ── TPC-C SF={TPCC_SF:.0f} on {target.label}")
        r_tpcc = run_one(target, "tpcc", TPCC_SF)
        results[target.label]["tpcc"] = r_tpcc
        if r_tpcc.ok:
            print(f"  TPC-C OK: {r_tpcc.rows:,} rows in {r_tpcc.load_s:.1f}s")
        else:
            print(f"  TPC-C ERR: {r_tpcc.error}")

        # TPC-H
        print(f"\n  ── TPC-H SF={TPCH_SF} on {target.label}")
        r_tpch = run_one(target, "tpch", TPCH_SF)
        results[target.label]["tpch"] = r_tpch
        if r_tpch.ok:
            print(f"  TPC-H OK: {r_tpch.rows:,} rows in {r_tpch.load_s:.1f}s")
        else:
            print(f"  TPC-H ERR: {r_tpch.error}")

    # Build and save markdown
    md = build_markdown(results, TPCC_SF, TPCH_SF)
    out_path = RESULTS_DIR / "benchmark_table.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md)
    print(f"\n\n{'='*72}")
    print(f"  Result table saved → {out_path.relative_to(_REPO_ROOT)}")
    print(f"{'='*72}\n")
    print(md)


if __name__ == "__main__":
    main()
