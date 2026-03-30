"""
Run TPC-C (SF=1) and TPC-H (SF=0.1) on every reachable live database and write
benchmarks/results/benchmark_table.md with a timing summary table.

Rows = database + version.  Columns = TPC-C / TPC-H (rows, total_s, rows/s).

Run from the repo root:
    .venv_test/bin/python benchmarks/run_all_bench.py
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from benchmarks.bench_config import BENCH_TPCC_SF as TPCC_SF, BENCH_TPCH_SF as TPCH_SF
from benchmarks.run_bench import run_benchmark, connect, RESULTS_DIR
from src.statschema.data_loader import LoadStrategy

# ---------------------------------------------------------------------------
# Database matrix
# ---------------------------------------------------------------------------

@dataclass
class DbTarget:
    label: str           # display name (e.g. "PostgreSQL 16")
    dialect: str         # statschema dialect
    strategy: LoadStrategy
    env: dict[str, str]  # env vars to set before connecting
    db_name: str = "bench"  # logical name for display
    skip: bool = False

def _crdb(label: str, port: int) -> DbTarget:
    return DbTarget(
        label=label, dialect="cockroachdb",
        strategy=LoadStrategy.BULK_COPY,
        env={
            "BENCH_PG_DSN": (
                f"host=127.0.0.1 port={port} dbname=defaultdb "
                "user=root sslmode=disable"
            ),
        },
    )

def _pg(label: str, port: int) -> DbTarget:
    return DbTarget(
        label=label, dialect="postgres",
        strategy=LoadStrategy.BULK_COPY,
        env={
            "BENCH_PG_DSN": (
                f"host=127.0.0.1 port={port} dbname=testdb "
                "user=postgres password=testpass"
            ),
        },
    )

def _mysql(label: str, port: int) -> DbTarget:
    return DbTarget(
        label=label, dialect="mysql",
        strategy=LoadStrategy.BULK_COPY,
        env={
            "BENCH_MYSQL_HOST": "127.0.0.1",
            "BENCH_MYSQL_PORT": str(port),
            "BENCH_MYSQL_USER": "root",
            "BENCH_MYSQL_PASS": "testpass",
            "BENCH_MYSQL_DB":   "testdb",
        },
    )

def _mariadb(label: str, port: int) -> DbTarget:
    return DbTarget(
        label=label, dialect="mariadb",
        strategy=LoadStrategy.BULK_COPY,
        env={
            "BENCH_MYSQL_HOST": "127.0.0.1",
            "BENCH_MYSQL_PORT": str(port),
            "BENCH_MYSQL_USER": "root",
            "BENCH_MYSQL_PASS": "testpass",
            "BENCH_MYSQL_DB":   "testdb",
        },
    )

TARGETS: list[DbTarget] = [
    _pg("PostgreSQL 14",       5414),
    _pg("PostgreSQL 16",       5416),
    _pg("PostgreSQL 18",       5418),
    _crdb("CockroachDB",       26257),
    _mysql("MySQL 5.7",        3357),
    _mysql("MySQL 8.0",        3384),
    _mariadb("MariaDB 10.11",  3310),
    _mariadb("MariaDB 11.4",   3311),
    DbTarget(
        label="SQL Server 2022", dialect="sqlserver",
        strategy=LoadStrategy.MULTI_ROW,
        env={
            # Password resolved at runtime via BENCH_SQLSERVER_DSN env var
            # (set by _common.sh export_bench_sqlserver_dsn, which reads from
            # /var/opt/mssql/.sa_password inside the Lima VM).  Falls back to
            # a placeholder so probe() fails gracefully if not set.
            "BENCH_SQLSERVER_DSN": os.environ.get(
                "BENCH_SQLSERVER_DSN",
                "SERVER=127.0.0.1,14330;DATABASE=master;UID=sa;PWD=",
            ),
        },
    ),
    DbTarget(
        label="Oracle XE 21c", dialect="oracle",
        strategy=LoadStrategy.MULTI_ROW,
        env={
            "BENCH_ORACLE_DSN":  "127.0.0.1:1521/XE",
            "BENCH_ORACLE_USER": "system",
            "BENCH_ORACLE_PASS": "oracle",
        },
    ),
    DbTarget(
        label="IBM Db2 CE 11.5", dialect="db2",
        strategy=LoadStrategy.MULTI_ROW,
        env={
            "BENCH_DB2_DSN": (
                "DATABASE=testdb;HOSTNAME=127.0.0.1;PORT=50000;"
                "PROTOCOL=TCPIP;UID=db2inst1;PWD=testpass;"
            ),
        },
    ),
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
    # Apply env vars
    saved = {k: os.environ.get(k) for k in target.env}
    os.environ.update(target.env)
    try:
        result = run_benchmark(
            schema=schema, sf=sf, dialect=target.dialect,
            strategy=target.strategy, out_dir=RESULTS_DIR, seed=42,
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
    finally:
        # Restore env
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def probe(target: DbTarget) -> bool:
    """Return True if the database is reachable."""
    saved = {k: os.environ.get(k) for k in target.env}
    os.environ.update(target.env)
    try:
        conn = connect(target.dialect)
        conn.close()
        return True
    except Exception:
        return False
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


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
