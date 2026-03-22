"""
Run TPC-B on every reachable live database and append results to
benchmarks/results/benchmark_table.md.

Also demonstrates append mode: after the initial SF=1 load, a second
SF=1 pass with append=True adds another 200K rows without recreating tables.

Run from the repo root:
    .venv_test/bin/python benchmarks/run_tpcb_bench.py
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from benchmarks.run_all_bench import TARGETS, DbTarget, probe
from benchmarks.run_bench import run_benchmark, connect, RESULTS_DIR
from src.statschema.data_loader import LoadStrategy

# ── Scale factors ────────────────────────────────────────────────────────────
# SF=1 → 200,011 rows (branch=1, teller=10, account=100K, history=100K)
# QEMU databases (SQL Server, Oracle, Db2) use SF=0.1 to keep runtime < 60 s
TPCB_SF_NATIVE = 1.0
TPCB_SF_QEMU   = 0.1
_QEMU_DIALECTS = {"sqlserver", "oracle", "db2"}


@dataclass
class BenchResult:
    label:  str
    rows:   int   = 0
    load_s: float = 0.0
    rows_s: float = 0.0
    sf:     float = 1.0
    error:  str   = ""

    @property
    def ok(self) -> bool:
        return not self.error


def _add_pgbench_indexes(target: DbTarget) -> None:
    """
    Add primary-key indexes on the three pgbench tables that need them.

    We strip all PKs during the bulk load for maximum throughput.  After
    loading completes, we restore them so that pgbench transaction SQL
    (UPDATE pgbench_accounts WHERE aid = :aid) uses index seeks instead of
    sequential scans.

    Only runs for PostgreSQL dialects — pgbench itself only targets Postgres.
    """
    if target.dialect not in ("postgres", "cockroachdb", "neon"):
        return

    saved = {k: os.environ.get(k) for k in target.env}
    os.environ.update(target.env)
    try:
        conn = connect(target.dialect)
        cur  = conn.cursor()
        stmts = [
            # Drop first so re-runs are idempotent
            "ALTER TABLE pgbench_accounts DROP CONSTRAINT IF EXISTS pgbench_accounts_pkey",
            "ALTER TABLE pgbench_branches DROP CONSTRAINT IF EXISTS pgbench_branches_pkey",
            "ALTER TABLE pgbench_tellers  DROP CONSTRAINT IF EXISTS pgbench_tellers_pkey",
            "ALTER TABLE pgbench_accounts ADD PRIMARY KEY (aid)",
            "ALTER TABLE pgbench_branches ADD PRIMARY KEY (bid)",
            "ALTER TABLE pgbench_tellers  ADD PRIMARY KEY (tid)",
        ]
        for stmt in stmts:
            try:
                cur.execute(stmt)
            except Exception as exc:
                print(f"    [index] {stmt[:60]}… → {exc}", flush=True)
        conn.commit()
        conn.close()
        print(f"  Primary keys added to pgbench_accounts/branches/tellers", flush=True)
    except Exception as exc:
        print(f"  WARNING: could not add pgbench indexes: {exc}", flush=True)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _run_pgbench(target: DbTarget, clients: int = 4, txns: int = 500) -> str:
    """
    Run pgbench against the target and return a one-line result summary.

    Uses -n (no vacuum) because our synthetic data is already clean.
    Returns an error string if pgbench is not available or the run fails.
    """
    if target.dialect not in ("postgres", "cockroachdb", "neon"):
        return "n/a (pgbench only runs against PostgreSQL)"

    saved = {k: os.environ.get(k) for k in target.env}
    os.environ.update(target.env)
    try:
        import subprocess, re

        dsn = target.env.get("BENCH_PG_DSN", "")
        parts = dict(kv.split("=", 1) for kv in dsn.split() if "=" in kv)
        host  = parts.get("host", "127.0.0.1")
        port  = parts.get("port", "5432")
        user  = parts.get("user", "postgres")
        dbname = parts.get("dbname", "testdb")
        pwd   = parts.get("password", "")

        env = os.environ.copy()
        env["PGPASSWORD"] = pwd

        cmd = [
            "pgbench",
            "-h", host, "-p", port, "-U", user, dbname,
            "-c", str(clients), "-t", str(txns),
            "-n",           # skip VACUUM
            "--no-vacuum",  # older pgbench compat
        ]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, env=env, timeout=120,
        )
        output = proc.stdout + proc.stderr
        # pgbench 14+: "tps = 123.456 (without initial connection time)"
        # pgbench 12-: "tps = 123.456 (excluding connections establishing)"
        m = re.search(r"tps\s*=\s*([\d.]+)\s*\((?:without|excluding)", output)
        if m:
            tps = float(m.group(1))
            return f"{tps:.1f} tps  (clients={clients} txns={txns})"
        if proc.returncode != 0:
            return f"ERR: {output.strip()[-200:]}"
        return output.strip()[-200:]
    except FileNotFoundError:
        return "ERR: pgbench not found in PATH"
    except Exception as exc:
        return f"ERR: {exc}"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def run_one(target: DbTarget, sf: float, append: bool = False) -> BenchResult:
    saved = {k: os.environ.get(k) for k in target.env}
    os.environ.update(target.env)
    try:
        result = run_benchmark(
            schema="tpcb", sf=sf, dialect=target.dialect,
            strategy=target.strategy, out_dir=RESULTS_DIR,
            seed=42, append=append,
        )
        totals = result["totals"]
        return BenchResult(
            label=target.label, sf=sf,
            rows=totals["rows"],
            load_s=totals["load_s"],
            rows_s=totals["rows_per_s"],
        )
    except Exception as exc:
        return BenchResult(label=target.label, sf=sf, error=str(exc)[:160])
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _cell(r: BenchResult | None) -> str:
    if r is None:
        return "—"
    if not r.ok:
        return f"ERR: {r.error[:50]}"
    return f"{r.load_s:.1f}s · {int(r.rows_s / 1000)}K r/s · {r.rows:,} rows"


def build_tpcb_section(
    initial:  dict[str, BenchResult],
    appended: dict[str, BenchResult],
    pgbench_tps: dict[str, str],
) -> str:
    labels = list(initial.keys())

    db_w  = 24
    col_w = 38
    tps_w = 34

    rows = [
        "",
        "---",
        "",
        "## TPC-B",
        "",
        "Tables use pgbench naming (`pgbench_accounts`, `pgbench_branches`, `pgbench_tellers`,",
        "`pgbench_history`) so pgbench can run its standard TPC-B-like transaction mix directly.  ",
        "Scale factor **SF=1** = 200,011 rows (branches=1, tellers=10, accounts=100K, history=100K).  ",
        "QEMU databases use SF=0.1 = 20,002 rows.  ",
        "Strategy: BULK_COPY for PostgreSQL / MySQL / MariaDB; MULTI_ROW for QEMU-hosted databases.  ",
        "",
        "### Initial load",
        "",
        f"| {'Database':<{db_w}} | {'TPC-B (initial load)':<{col_w}} |",
        f"|:{'-'*db_w}:|:{'-'*col_w}:|",
    ]
    for label in labels:
        r = initial.get(label)
        sf_note = " *(QEMU)*" if r and r.sf == TPCB_SF_QEMU else ""
        rows.append(f"| {(label + sf_note):<{db_w}} | {_cell(r):<{col_w}} |")

    rows += [
        "",
        "### pgbench transaction throughput",
        "",
        "After loading, primary keys are added back to `pgbench_accounts/branches/tellers`",
        "so pgbench uses index seeks.  Run: `pgbench -c 4 -t 500 -n` (no vacuum).  ",
        "",
        f"| {'Database':<{db_w}} | {'pgbench result':<{tps_w}} |",
        f"|:{'-'*db_w}:|:{'-'*tps_w}:|",
    ]
    for label in labels:
        tps = pgbench_tps.get(label, "—")
        rows.append(f"| {label:<{db_w}} | {tps:<{tps_w}} |")

    rows += [
        "",
        "### Append load (SF=1 added on top of existing data)",
        "",
        "Runs with `--append`: tables are not recreated; sequential PKs continue",
        "from the current row count; FK ranges span existing + new rows.",
        "",
        f"| {'Database':<{db_w}} | {'TPC-B (append +SF=1)':<{col_w}} |",
        f"|:{'-'*db_w}:|:{'-'*col_w}:|",
    ]
    for label in labels:
        r = appended.get(label)
        sf_note = " *(QEMU)*" if r and r.sf == TPCB_SF_QEMU else ""
        rows.append(f"| {(label + sf_note):<{db_w}} | {_cell(r):<{col_w}} |")

    rows += [""]
    return "\n".join(rows)


def main() -> None:
    print(f"\n{'='*72}")
    print(f"  statschema TPC-B benchmark  SF={TPCB_SF_NATIVE} (QEMU: SF={TPCB_SF_QEMU})")
    print(f"{'='*72}\n")

    initial:     dict[str, BenchResult] = {}
    appended:    dict[str, BenchResult] = {}
    pgbench_tps: dict[str, str]         = {}

    for target in TARGETS:
        print(f"\n{'─'*72}")
        print(f"  Probing {target.label} ({target.dialect}) ...")
        if not probe(target):
            print(f"  SKIP — unreachable")
            err = BenchResult(label=target.label, sf=TPCB_SF_NATIVE,
                              error="unreachable")
            initial[target.label]     = err
            appended[target.label]    = err
            pgbench_tps[target.label] = "n/a (unreachable)"
            continue

        sf = TPCB_SF_QEMU if target.dialect in _QEMU_DIALECTS else TPCB_SF_NATIVE

        # ── initial load (fresh tables) ───────────────────────────────────────
        print(f"\n  ── TPC-B SF={sf} INITIAL on {target.label}")
        r_init = run_one(target, sf, append=False)
        initial[target.label] = r_init
        if r_init.ok:
            print(f"  OK: {r_init.rows:,} rows in {r_init.load_s:.1f}s "
                  f"({int(r_init.rows_s):,} rows/s)")
        else:
            print(f"  ERR: {r_init.error}")

        # ── add primary key indexes so pgbench is efficient ────────────────────
        if r_init.ok and target.dialect in ("postgres", "cockroachdb", "neon"):
            print(f"\n  ── Adding PK indexes on {target.label}")
            _add_pgbench_indexes(target)

        # ── pgbench transaction run ────────────────────────────────────────────
        if r_init.ok and target.dialect in ("postgres", "cockroachdb", "neon"):
            print(f"\n  ── pgbench -c 4 -t 500 on {target.label}")
            tps_result = _run_pgbench(target, clients=4, txns=500)
            pgbench_tps[target.label] = tps_result
            print(f"  pgbench: {tps_result}")
        else:
            pgbench_tps[target.label] = (
                "n/a (pgbench only targets PostgreSQL)"
                if r_init.ok else "n/a (load failed)"
            )

        # ── append load (same SF on top of existing data) ─────────────────────
        if r_init.ok:
            print(f"\n  ── TPC-B SF={sf} APPEND on {target.label}")
            r_app = run_one(target, sf, append=True)
            appended[target.label] = r_app
            if r_app.ok:
                print(f"  OK (append): +{r_app.rows:,} rows in {r_app.load_s:.1f}s "
                      f"({int(r_app.rows_s):,} rows/s)")
            else:
                print(f"  ERR (append): {r_app.error}")
        else:
            appended[target.label] = BenchResult(
                label=target.label, sf=sf, error="skipped (initial failed)"
            )

    # ── append TPC-B section to benchmark_table.md ────────────────────────────
    md_path = RESULTS_DIR / "benchmark_table.md"
    existing_md = md_path.read_text() if md_path.exists() else ""

    # Remove any previous TPC-B section
    marker = "\n---\n\n## TPC-B"
    if marker in existing_md:
        existing_md = existing_md[:existing_md.index(marker)]
    existing_md = existing_md.rstrip("\n -")

    tpcb_section = build_tpcb_section(initial, appended, pgbench_tps)
    md_path.write_text(existing_md + "\n" + tpcb_section)

    print(f"\n{'='*72}")
    print(f"  TPC-B results appended → {md_path.relative_to(_REPO_ROOT)}")
    print(f"{'='*72}\n")
    print(tpcb_section)


if __name__ == "__main__":
    main()
