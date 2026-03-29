"""
benchmarks/run_matrix.py

Python matrix runner for identity, lakebase, and bench test modes.
Replaces the engine × schema loop previously embedded in run_identity.sh,
run_lakebase_target.sh, and run_bench.sh.

Callable from:
  - Shell:             python benchmarks/run_matrix.py identity --engines postgres,mysql
  - CI (Docker):       python benchmarks/run_matrix.py identity --engines postgres
  - Databricks notebook: from benchmarks.run_matrix import run_identity_matrix

Modes
-----
identity   Same-engine identity test (source == target dialect).
           6 engines × 6 TPC schemas = 36 combinations.

lakebase   Cross-engine identity test where the target is Databricks Lakebase.
           N source engines × 6 TPC schemas, target always lakebase.

bench      TPC-C and TPC-H load benchmarks on every reachable database.

Phases (--phases flag, comma-separated subset)
------
load_source      Phase A  — emit DDL + load synthetic rows into source schema
explain_source   Phase B  — EXPLAIN on benchmark queries → ground-truth plans
collect_stats    Phase C  — collect_table_stats() on source schema
load_target      Phase D  — build synthetic copy + inject stats
explain_target   Phase E  — EXPLAIN on same queries against copy
score            Phase F  — compute node_jaccard / within_2x
validate         post-run — check_run.py row-count verification

Default: all phases.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from benchmarks.bench_config import (
    DEFAULT_ENGINES,
    DEFAULT_SCHEMAS,
    BENCH_TPCC_SF,
    BENCH_TPCH_SF,
    build_dsn,
    sf_for,
)

logger = logging.getLogger(__name__)

ALL_PHASES = [
    "load_source", "explain_source", "collect_stats",
    "load_target", "explain_target", "score", "validate",
]

RESULTS_DIR = Path(__file__).parent / "results"
LOGS_DIR    = Path(__file__).parent / "logs"

# ---------------------------------------------------------------------------
# Per-engine schema parallelism limits
# ---------------------------------------------------------------------------
# Podman engines (native ARM64, Podman VM ≈ 8.3 GB shared):
#   postgres    sw=6 → 6/6 pass, 59 s  (sw=4 baseline: 112 s)
#   mysql       sw=5 → 6/6 pass, 84 s
#   cockroachdb sw=5 → 6/6 pass, 83 s  (sw=3 was 5/6: plan-quality miss)
#               Requires crdb-single launched with --memory=2g to avoid OOM.
#
# Lima QEMU VMs (8 GiB, no swap, x86_64 emulation via QEMU):
#   SQL Server:  ~2.7 GB RSS (buffer pool capped at 5632 MB)
#   Oracle XE:   ~3.1 GB (SGA hard-capped at 2 GB by XE edition)
#   DB2 CE:      STMM self-tunes to ~3.5 GB
#   Each schema load peaks at 300–500 MB.
#
#   QEMU emulation overhead means adding concurrent schemas within a VM
#   competes for the same emulated vCPUs and saturates the host, causing
#   3-4× per-operation slowdowns that negate any parallelism gain.  At sw=1,
#   each VM's schemas run sequentially at full speed.  The wall-time
#   bottleneck is sqlserver (all 6 schemas sequential ≈ 33 min).

_SCHEMA_WORKERS: dict[str, int] = {
    "postgres":    6,   # Podman, 6/6 at sw=6 (59 s vs 112 s at sw=4)
    "cockroachdb": 4,   # Podman, stable at sw=4; sw=5 risks liveness-session-expired under load
    "neon":        2,   # remote cloud, limit to avoid rate limits
    "mysql":       4,   # Podman, reduced from 5 to avoid TCP packet errors under heavy host load
    "mariadb":     3,   # Podman, similar to mysql; not yet empirically tested
    "lakebase":    2,   # remote cloud
    # Lima QEMU VMs — 8 GiB RAM, 4 vCPUs (x86_64 emulation on Apple Silicon).
    #
    # Standalone single-engine optimum (measured on SQL Server 2022, clean state1):
    #   sw=2 default order: ~20 min
    #   sw=3 LPT order:     ~13 min  ← sweet spot (-35%)
    #   sw=4 LPT order:     ~15 min  (worse: 4 vCPUs saturate, mutual interference)
    # sw=3 leaves 1 vCPU free for SQL Server background threads; sw=4 fights them.
    #
    # Multi-engine full run (run_identity.sh wave-2, all QEMU engines together):
    #   Use sw=1 to avoid cross-engine CPU saturation under QEMU emulation.
    #   run_identity.sh overrides to --schema-workers 3 for the QEMU-only wave.
    "sqlserver":   3,
    "oracle":      1,
    "db2":         1,
}
_DEFAULT_SCHEMA_WORKERS = 1

# Approximate per-schema wall-time (seconds) for identity test stats+explain phases
# on a single-engine SQL Server run with sw=2 (isolated, no other engines active).
# Derived from timestamped PASS lines: individual times inferred from thread-pool
# interleaving at sw=2 (two threads, schemas start in submission order).
# Used to sort schemas in Longest-Processing-Time (LPT) order before thread-pool
# submission so the heaviest schemas occupy the first slots and keep the
# critical path full from the start.  Unknown schemas fall back to 0 (last).
_SCHEMA_DURATION_S: dict[str, int] = {
    "tpce":  509,   # ~8m29s — most complex joins + largest working set
    "tpcdi": 464,   # ~7m44s
    "tpcds": 411,   # ~6m51s — many columns, more stat histogram buckets
    "tpch":  215,   # ~3m35s
    "tpcc":  165,   # ~2m45s
    "tpcb":   87,   # ~1m27s — simplest schema
}


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

class RunResult:
    def __init__(self, engine: str, schema: str, sf: float, mode: str):
        self.engine  = engine
        self.schema  = schema
        self.sf      = sf
        self.mode    = mode
        self.passed: bool | None = None
        self.score_line: str = ""
        self.exit_code: int = 0
        self.log_file: Path | None = None
        self.json_path: str = ""
        self.elapsed_s: float = 0.0

    def label(self) -> str:
        return f"{self.engine}×{self.schema}(sf={self.sf})"

    def summary_line(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        extra  = self.score_line or f"exit={self.exit_code}"
        return f"{status}  {self.label():<42}  {extra}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _python() -> str:
    """Return the Python executable (venv or current interpreter)."""
    venv = _REPO_ROOT / ".venv_test" / "bin" / "python"
    if venv.exists():
        return str(venv)
    return sys.executable


def _load_dotenv() -> None:
    env_file = _REPO_ROOT / ".env"
    if env_file.exists():
        try:
            from dotenv import load_dotenv as _ld
            _ld(env_file)
        except ImportError:
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())


def _extract_json_path(log_text: str) -> str:
    for line in log_text.splitlines():
        if "Result saved" in line and ".json" in line:
            for token in line.split():
                if token.endswith(".json"):
                    return token
    return ""


def _extract_score(log_text: str) -> str:
    for line in reversed(log_text.splitlines()):
        if "Overall:" in line or "PASS" in line or "node_jaccard" in line:
            return line.strip()
    return ""


def _run_check_run(
    json_path: str,
    log_file: Path,
    dialect: str,
    dsn: str,
    target_dialect: str | None = None,
    target_dsn: str | None = None,
) -> bool:
    if not json_path or not Path(json_path).exists():
        logger.warning("check_run: no result JSON at %s — skipping row-count check", json_path)
        return True
    cmd = [
        _python(), "benchmarks/check_run.py",
        json_path, str(log_file),
        "--dialect", dialect,
        "--dsn", dsn,
    ]
    if target_dialect and target_dialect != dialect:
        cmd += ["--target-dialect", target_dialect]
    if target_dsn and target_dsn != dsn:
        cmd += ["--target-dsn", target_dsn]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=_REPO_ROOT)
    if result.returncode != 0:
        logger.warning("check_run: row-count issues in %s\n%s", json_path, result.stdout)
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Identity mode
# ---------------------------------------------------------------------------

def _run_one_identity(
    engine: str,
    schema: str,
    dsn: str,
    log_dir: Path,
    phases: list[str],
    skip_load: bool,
    no_extended_stats: bool,
    extra_env: dict[str, str] | None = None,
    full_stats: bool = False,
) -> RunResult:
    sf     = sf_for(engine, schema)
    result = RunResult(engine, schema, sf, "identity")
    log_file = log_dir / f"{engine}_{schema}.log"
    result.log_file = log_file

    phases_str = ",".join(phases) if phases != ALL_PHASES else None
    cmd = [
        _python(), "benchmarks/identity_test.py",
        "--schema",  schema,
        "--sf",      str(sf),
        "--dialect", engine,
        "--dsn",     dsn,
    ]
    if skip_load:
        cmd.append("--skip-load")
    if no_extended_stats or engine not in ("postgres", "cockroachdb", "neon"):
        cmd.append("--no-extended-stats")
    if full_stats and engine in ("db2", "oracle"):
        cmd.append("--full-stats-db2-ora")
    if phases_str:
        cmd += ["--phases", phases_str]

    env = {**os.environ, **(extra_env or {})}
    if engine == "db2":
        env.setdefault("DB2_CONTAINER_NAME", os.environ.get("DB2_CONTAINER_NAME", ""))

    t0 = time.monotonic()
    proc = subprocess.run(
        cmd, capture_output=False,
        stdout=open(log_file, "w"), stderr=subprocess.STDOUT,
        env=env, cwd=_REPO_ROOT,
    )
    result.elapsed_s = time.monotonic() - t0
    result.exit_code  = proc.returncode
    log_text = log_file.read_text(errors="replace")
    result.json_path  = _extract_json_path(log_text)
    result.score_line = _extract_score(log_text)
    result.passed     = proc.returncode == 0

    if result.passed and "validate" in phases:
        _run_check_run(result.json_path, log_file, engine, dsn)

    return result


def _run_engine_identity(
    engine: str,
    schemas: list[str],
    dsn: str,
    log_dir: Path,
    phases: list[str],
    skip_load: bool,
    no_extended_stats: bool,
    schema_workers_override: int | None = None,
    full_stats: bool = False,
) -> list[RunResult]:
    """Run identity tests for all schemas on one engine.

    Schemas run sequentially for QEMU-hosted engines (oracle, db2, sqlserver)
    to avoid exhausting the VM's limited CPU/RAM.  Podman-hosted engines
    (postgres, cockroachdb, mysql) run up to N schemas in parallel since each
    schema occupies its own namespace and the engine can handle concurrent load.
    """
    schema_workers = schema_workers_override or _SCHEMA_WORKERS.get(engine, _DEFAULT_SCHEMA_WORKERS)

    if schema_workers == 1:
        results = []
        for schema in schemas:
            r = _run_one_identity(engine, schema, dsn, log_dir, phases, skip_load,
                                  no_extended_stats, full_stats=full_stats)
            status = "PASS" if r.passed else "FAIL"
            logger.info("%s  %s", status, r.label())
            results.append(r)
        return results

    # Parallel schema execution within the engine.
    # Submit in Longest-Processing-Time (LPT) order so the heaviest schemas
    # occupy the first available threads and keep the critical path full.
    lpt_schemas = sorted(
        schemas,
        key=lambda s: _SCHEMA_DURATION_S.get(s, 0),
        reverse=True,
    )
    results: list[RunResult] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=schema_workers) as pool:
        futures = {
            pool.submit(
                _run_one_identity,
                engine, schema, dsn, log_dir, phases, skip_load, no_extended_stats,
                full_stats=full_stats,
            ): schema
            for schema in lpt_schemas
        }
        for future in concurrent.futures.as_completed(futures):
            r = future.result()
            status = "PASS" if r.passed else "FAIL"
            logger.info("%s  %s", status, r.label())
            results.append(r)
    return results


def run_identity_matrix(
    engines: list[str] | None = None,
    schemas: list[str] | None = None,
    dsns: dict[str, str] | None = None,
    log_dir: Path | str | None = None,
    skip_load: bool = False,
    no_extended_stats: bool = False,
    phases: list[str] | None = None,
    max_workers: int | None = None,
    schema_workers_override: int | None = None,
    full_stats: bool = False,
) -> list[RunResult]:
    """
    Run the identity test matrix.  Engines run in parallel; schemas within
    each engine run sequentially to avoid overloading single-node instances.

    Parameters
    ----------
    engines : list of engine names, default DEFAULT_ENGINES
    schemas : list of TPC schema names, default DEFAULT_SCHEMAS
    dsns    : dict mapping engine → DSN string; built from env if not provided
    log_dir : directory for per-run log files
    phases  : subset of ALL_PHASES to run; default = all phases
    """
    _load_dotenv()
    engines = engines or DEFAULT_ENGINES
    schemas = schemas or DEFAULT_SCHEMAS
    phases  = phases  or ALL_PHASES
    dsns    = dsns    or {}

    ts      = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    log_dir = Path(log_dir) if log_dir else LOGS_DIR / f"identity-{ts}"
    log_dir.mkdir(parents=True, exist_ok=True)

    resolved_dsns: dict[str, str] = {}
    skipped: list[str] = []
    for engine in engines:
        try:
            resolved_dsns[engine] = dsns.get(engine) or build_dsn(engine)
        except ValueError as exc:
            logger.warning("Skipping %s: %s", engine, exc)
            skipped.append(engine)
    active_engines = [e for e in engines if e not in skipped]

    all_results: list[RunResult] = []
    # Default: one thread per engine, capped at 6 to avoid overwhelming the host.
    # Each engine thread may itself spawn up to _SCHEMA_WORKERS[engine] sub-threads
    # for intra-engine schema parallelism, so the total process count is bounded by
    # sum(_SCHEMA_WORKERS[e] for e in active_engines) ≤ ~12 for the default matrix.
    workers = max_workers or min(len(active_engines), 6)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _run_engine_identity,
                engine, schemas, resolved_dsns[engine],
                log_dir, phases, skip_load, no_extended_stats,
                schema_workers_override, full_stats,
            ): engine
            for engine in active_engines
        }
        for future in concurrent.futures.as_completed(futures):
            all_results.extend(future.result())

    _write_summary(all_results, log_dir)
    return all_results


# ---------------------------------------------------------------------------
# Lakebase mode
# ---------------------------------------------------------------------------

def run_lakebase_matrix(
    source_engines: list[str] | None = None,
    schemas: list[str] | None = None,
    dsns: dict[str, str] | None = None,
    log_dir: Path | str | None = None,
    skip_load: bool = False,
    phases: list[str] | None = None,
    max_jobs: int = 3,
    full_stats: bool = False,
) -> list[RunResult]:
    """
    Cross-engine identity test where the target is always Databricks Lakebase.
    """
    _load_dotenv()
    source_engines = source_engines or DEFAULT_ENGINES
    schemas        = schemas        or DEFAULT_SCHEMAS
    phases         = phases         or ALL_PHASES
    dsns           = dsns           or {}

    ts      = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    log_dir = Path(log_dir) if log_dir else LOGS_DIR / f"lakebase-{ts}"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Lakebase target DSN
    try:
        lakebase_dsn = dsns.get("lakebase") or build_dsn("lakebase")
    except ValueError as exc:
        raise RuntimeError(f"Lakebase not configured: {exc}") from exc

    resolved_dsns: dict[str, str] = {}
    for engine in source_engines:
        try:
            resolved_dsns[engine] = dsns.get(engine) or build_dsn(engine)
        except ValueError as exc:
            logger.warning("Skipping source engine %s: %s", engine, exc)

    all_results: list[RunResult] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_jobs) as pool:
        futures = []
        for engine in resolved_dsns:
            for schema in schemas:
                futures.append(pool.submit(
                    _run_one_lakebase,
                    engine, schema, resolved_dsns[engine], lakebase_dsn,
                    log_dir, phases, skip_load, full_stats,
                ))
        for future in concurrent.futures.as_completed(futures):
            r = future.result()
            all_results.append(r)
            logger.info("%s  %s", "PASS" if r.passed else "FAIL", r.label())

    _write_summary(all_results, log_dir)
    return all_results


def _run_one_lakebase(
    engine: str,
    schema: str,
    src_dsn: str,
    lakebase_dsn: str,
    log_dir: Path,
    phases: list[str],
    skip_load: bool,
    full_stats: bool = False,
) -> RunResult:
    sf     = sf_for(engine, schema)
    result = RunResult(engine, schema, sf, "lakebase")
    log_file = log_dir / f"{engine}_{schema}.log"
    result.log_file = log_file

    prefix     = engine[:2]
    src_schema = f"from_{prefix}_{schema}_src"
    tgt_schema = f"from_{prefix}_{schema}_tgt"
    ss_schema  = f"lbss_{schema}"

    cmd = [
        _python(), "benchmarks/identity_test.py",
        "--schema",               schema,
        "--sf",                   str(sf),
        "--dialect",              "lakebase",
        "--dsn",                  "",
        "--source-schema",        src_schema,
        "--target-schema",        tgt_schema,
        "--stats-source-dsn",     src_dsn,
        "--stats-source-dialect", engine,
        "--stats-source-schema",  ss_schema,
        "--no-extended-stats",
    ]
    if full_stats:
        cmd.append("--full-stats-db2-ora")
    if skip_load:
        cmd.append("--skip-load")

    t0 = time.monotonic()
    proc = subprocess.run(
        cmd,
        stdout=open(log_file, "w"), stderr=subprocess.STDOUT,
        cwd=_REPO_ROOT,
    )
    result.elapsed_s = time.monotonic() - t0
    result.exit_code  = proc.returncode
    log_text = log_file.read_text(errors="replace")
    result.json_path  = _extract_json_path(log_text)
    result.score_line = _extract_score(log_text)
    result.passed     = proc.returncode == 0

    if result.passed and "validate" in phases:
        _run_check_run(result.json_path, log_file, engine, src_dsn, "lakebase", lakebase_dsn)

    return result


# ---------------------------------------------------------------------------
# Bench mode
# ---------------------------------------------------------------------------

def run_bench_matrix(
    engines: list[str] | None = None,
    schemas: list[str] | None = None,
    log_dir: Path | str | None = None,
) -> None:
    """Run TPC-C and TPC-H load benchmarks via run_all_bench.py and run_tpcb_bench.py."""
    _load_dotenv()
    ts      = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    log_dir = Path(log_dir) if log_dir else LOGS_DIR / f"bench-{ts}"
    log_dir.mkdir(parents=True, exist_ok=True)

    logger.info("=== Phase 1: TPC-C SF=%s + TPC-H SF=%s (all engines) ===",
                BENCH_TPCC_SF, BENCH_TPCH_SF)
    subprocess.run(
        [_python(), "benchmarks/run_all_bench.py"],
        cwd=_REPO_ROOT, check=False,
    )

    logger.info("=== Phase 2: TPC-B pgbench ===")
    subprocess.run(
        [_python(), "benchmarks/run_tpcb_bench.py"],
        cwd=_REPO_ROOT, check=False,
    )


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def _write_summary(results: list[RunResult], log_dir: Path) -> None:
    summary_file = log_dir / "summary.txt"
    passed = [r for r in results if r.passed]
    failed = [r for r in results if not r.passed]

    lines = [
        f"# {datetime.now(timezone.utc).isoformat()}",
        "",
    ]
    for r in sorted(results, key=lambda r: (r.engine, r.schema)):
        lines.append(r.summary_line())
    lines += [
        "",
        f"Passed : {len(passed)} / {len(results)}",
        f"Failed : {len(failed)} / {len(results)}",
        f"Logs   : {log_dir}",
    ]
    summary_file.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


def print_results(results: list[RunResult]) -> None:
    """Print a summary table. Useful in notebooks."""
    passed = sum(1 for r in results if r.passed)
    for r in results:
        print(r.summary_line())
    print(f"\nPassed: {passed} / {len(results)}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="statschema matrix test runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="mode", required=True)

    # ── identity ──────────────────────────────────────────────────────────────
    ident = sub.add_parser("identity", help="Same-engine identity test matrix")
    ident.add_argument("--engines",  default=",".join(DEFAULT_ENGINES))
    ident.add_argument("--schemas",  default=",".join(DEFAULT_SCHEMAS))
    ident.add_argument("--log-dir",  default=None)
    ident.add_argument("--skip-load", action="store_true")
    ident.add_argument("--no-extended-stats", action="store_true")
    ident.add_argument("--full-stats-db2-ora", action="store_true",
                       help="DB2 and Oracle only: gather full per-column distribution stats "
                            "on the target schema (5–10× slower than the default predicate-"
                            "column-only stats). No effect on other engines. Use for "
                            "major-release or audit validation runs.")
    ident.add_argument(
        "--phases",
        default=",".join(ALL_PHASES),
        help=f"Comma-separated subset of: {', '.join(ALL_PHASES)}",
    )
    ident.add_argument(
        "--schema-workers", type=int, default=None, metavar="N",
        help=(
            "Override per-engine schema parallelism (default: engine-specific from "
            "_SCHEMA_WORKERS). Use 1 to force fully sequential execution."
        ),
    )

    # ── lakebase ──────────────────────────────────────────────────────────────
    lb = sub.add_parser("lakebase", help="Cross-engine Lakebase target test")
    lb.add_argument("--source-engines",    default=",".join(DEFAULT_ENGINES))
    lb.add_argument("--schemas",           default=",".join(DEFAULT_SCHEMAS))
    lb.add_argument("--log-dir",           default=None)
    lb.add_argument("--skip-load",         action="store_true")
    lb.add_argument("--max-jobs",          type=int, default=3)
    lb.add_argument("--full-stats-db2-ora", action="store_true",
                    help="Collect all-column distribution stats for DB2/Oracle source "
                         "schemas (slower; use for audit / major-release runs).")
    lb.add_argument(
        "--phases",
        default=",".join(ALL_PHASES),
        help=f"Comma-separated subset of: {', '.join(ALL_PHASES)}",
    )

    # ── score-only shortcut ───────────────────────────────────────────────────
    score = sub.add_parser("score", help="Re-score from saved result JSON (no DB needed)")
    score.add_argument("result", help="Path to a *-identity-*.json result file")

    # ── bench ─────────────────────────────────────────────────────────────────
    bench = sub.add_parser("bench", help="TPC-C/TPC-H/TPC-B load benchmarks")
    bench.add_argument("--log-dir", default=None)

    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="  [%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    _load_dotenv()
    p   = _build_parser()
    args = p.parse_args(argv)

    if args.mode == "identity":
        results = run_identity_matrix(
            engines=args.engines.split(","),
            schemas=args.schemas.split(","),
            log_dir=args.log_dir,
            skip_load=args.skip_load,
            no_extended_stats=args.no_extended_stats,
            phases=args.phases.split(","),
            schema_workers_override=getattr(args, "schema_workers", None),
            full_stats=getattr(args, "full_stats_db2_ora", False),
        )
        failed = sum(1 for r in results if not r.passed)
        return 1 if failed else 0

    if args.mode == "lakebase":
        results = run_lakebase_matrix(
            source_engines=args.source_engines.split(","),
            schemas=args.schemas.split(","),
            log_dir=args.log_dir,
            skip_load=args.skip_load,
            phases=args.phases.split(","),
            max_jobs=args.max_jobs,
            full_stats=getattr(args, "full_stats_db2_ora", False),
        )
        failed = sum(1 for r in results if not r.passed)
        return 1 if failed else 0

    if args.mode == "score":
        from benchmarks.identity_test import score_from_file  # type: ignore[attr-defined]
        score_from_file(args.result)
        return 0

    if args.mode == "bench":
        run_bench_matrix(log_dir=args.log_dir)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
