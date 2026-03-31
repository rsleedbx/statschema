"""
benchmarks/run_sweep.py

Unified sweep runner — dispatches any test from the test catalog to its
appropriate runner (run_matrix.py for identity/bench, pytest for live tests).

Usage
-----
    # Show what's available
    python benchmarks/run_sweep.py --list
    python benchmarks/run_sweep.py --list-categories

    # Run ALL tests in a category
    python benchmarks/run_sweep.py --categories tpch
    python benchmarks/run_sweep.py --categories app
    python benchmarks/run_sweep.py --categories dtypes,ddl

    # Random sample across all tests
    python benchmarks/run_sweep.py --random 8

    # Random sample within a category
    python benchmarks/run_sweep.py --categories app --random 3
    python benchmarks/run_sweep.py --categories tpch,tpcdi --random 4

    # Dry run — print what would be executed without running
    python benchmarks/run_sweep.py --categories app --dry-run

    # Reproducible sample (fixed seed)
    python benchmarks/run_sweep.py --random 10 --seed 42

Category semantics
------------------
--categories A,B  matches tests tagged A OR B (union).
Engine-gating: tests with engine= are skipped if that engine is unreachable
(same logic as run_matrix.py's DSN check).

Exit code
---------
0   all selected tests passed
1   one or more tests failed
2   no tests matched the filter
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from benchmarks.test_registry import TestRegistry, TestSpec

logger = logging.getLogger(__name__)

# Prefer .venv_test (has mssql_python, ibm_db, oracledb, etc.) so that all
# dialects are available regardless of which Python launched this script.
# Fall back to sys.executable if .venv_test is absent (e.g. CI environments).
_VENV_PYTHON = _REPO_ROOT / ".venv_test" / "bin" / "python"
_PYTHON = [str(_VENV_PYTHON) if _VENV_PYTHON.exists() else sys.executable, "-u"]


# ---------------------------------------------------------------------------
# Runner dispatch
# ---------------------------------------------------------------------------

def _run_identity(spec: TestSpec, dry_run: bool) -> bool:
    """Dispatch to run_matrix.py for a single identity (engine × schema) test."""
    cmd = [
        *_PYTHON, "benchmarks/run_matrix.py", "identity",
        "--engines", spec.engine or "",
        "--schemas", spec.schema or "",
    ]
    return _exec(cmd, spec.id, dry_run)


def _run_pytest(spec: TestSpec, dry_run: bool) -> bool | None:
    """Dispatch to pytest for a live/pytest-based test."""
    assert spec.pytest_file, f"pytest runner requires 'file' for spec {spec.id}"
    cmd = [*_PYTHON, "-m", "pytest", spec.pytest_file, "-v", "--tb=short"]
    if spec.pytest_marks:
        cmd += ["-m", " and ".join(spec.pytest_marks)]
    return _exec(cmd, spec.id, dry_run, capture=True)


def _run_bench(spec: TestSpec, dry_run: bool) -> bool:
    """Dispatch to run_matrix.py bench mode."""
    cmd = [*_PYTHON, "benchmarks/run_matrix.py", "bench"]
    return _exec(cmd, spec.id, dry_run)


_RUNNERS = {
    "run_matrix":       _run_identity,
    "pytest":           _run_pytest,
    "run_matrix_bench": _run_bench,
}


_ALL_SKIPPED = __import__("re").compile(r"=+ ([\d\w ,]+) =+\s*$", __import__("re").MULTILINE)
_PASSED_RE   = __import__("re").compile(r"\b(\d+) passed\b")


def _is_all_skipped(output: str) -> bool:
    """Return True when pytest ran but zero tests actually passed or failed."""
    return bool(output) and not _PASSED_RE.search(output)


def _exec(cmd: list[str], label: str, dry_run: bool, capture: bool = False) -> bool | None:
    """Run *cmd* and return True on success, False on failure, None on all-skipped.

    When *capture* is True (pytest runs), stdout/stderr are captured so we can
    detect the all-skipped case, then forwarded to the terminal.
    """
    cmd_str = " ".join(cmd)
    if dry_run:
        logger.info("[DRY-RUN] %s  →  %s", label, cmd_str)
        return True
    logger.info("START  %s", label)
    t0 = time.monotonic()
    result = subprocess.run(
        cmd, cwd=str(_REPO_ROOT),
        capture_output=capture, text=capture,
    )
    elapsed = time.monotonic() - t0
    if capture:
        sys.stdout.write(result.stdout or "")
        sys.stderr.write(result.stderr or "")
    if result.returncode == 0 and capture and _is_all_skipped(result.stdout or ""):
        logger.warning("SKIP   %s  (%.0fs)  — all tests skipped; app schema may not be loaded",
                       label, elapsed)
        return None
    ok = result.returncode == 0
    logger.info("%s   %s  (%.0fs)", "PASS" if ok else "FAIL", label, elapsed)
    return ok


# ---------------------------------------------------------------------------
# Engine reachability gate
# ---------------------------------------------------------------------------

def _engine_reachable(engine: str) -> bool:
    """Return True if we can open a connection to the engine."""
    try:
        r = subprocess.run(
            [*_PYTHON, "benchmarks/bench_config.py", "ping", engine],
            capture_output=True, timeout=15, cwd=str(_REPO_ROOT),
        )
        return r.returncode == 0
    except Exception:
        return False


def _env_gate(spec: TestSpec) -> bool:
    """Return False (and log a warning) when any required env var is absent or empty."""
    missing = [v for v in spec.requires_env if not os.environ.get(v, "").strip()]
    if missing:
        logger.warning(
            "SKIP  %s — missing required env vars: %s", spec.id, ", ".join(missing)
        )
        return False
    return True


def _ctx_for_spec(spec: TestSpec) -> "Any":
    """Build a DeploymentContext for *spec* by reading the current environment.

    If *spec* declares a ``topology``, it overrides the value resolved by
    ``from_env()``; credential values (cloud_staging_uri, oracle_directory, etc.)
    still come from the environment.  This lets the catalog control which loader
    path is exercised for each spec without touching the credentials in ``.env``.
    """
    import dataclasses
    from src.statschema.loader_context import DeploymentContext
    base = DeploymentContext.from_env()
    if spec.topology:
        return dataclasses.replace(base, topology=spec.topology)  # type: ignore[arg-type]
    return base


def _filter_reachable(specs: list[TestSpec]) -> tuple[list[TestSpec], list[TestSpec]]:
    """Split specs into (reachable, skipped-due-to-unreachable-engine)."""
    reachable, skipped = [], []
    checked: dict[str, bool] = {}
    for spec in specs:
        if spec.engine is None:
            reachable.append(spec)
            continue
        if spec.engine not in checked:
            checked[spec.engine] = _engine_reachable(spec.engine)
            if not checked[spec.engine]:
                logger.warning("Engine '%s' unreachable — skipping %s tests",
                               spec.engine,
                               sum(1 for s in specs if s.engine == spec.engine))
        (reachable if checked[spec.engine] else skipped).append(spec)
    return reachable, skipped


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_sweep.py",
        description=(
            "Unified test sweep runner.  Select tests by category, run them all "
            "or take a random sample, dispatching to the appropriate runner."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python benchmarks/run_sweep.py --list
  python benchmarks/run_sweep.py --list-categories
  python benchmarks/run_sweep.py --categories tpch
  python benchmarks/run_sweep.py --categories app --random 3
  python benchmarks/run_sweep.py --random 10 --seed 42 --dry-run
  python benchmarks/run_sweep.py --categories identity --random 6
""",
    )
    p.add_argument(
        "--categories", default=None, metavar="CAT[,CAT]",
        help="Comma-separated category tags.  Matches tests carrying ANY tag (union).",
    )
    p.add_argument(
        "--random", type=int, default=None, metavar="N",
        help="Randomly sample N tests from the (filtered) pool instead of running all.",
    )
    p.add_argument(
        "--seed", type=int, default=None,
        help="Random seed for reproducible sampling.",
    )
    p.add_argument(
        "--skip-engine-check", action="store_true",
        help="Skip the engine-reachability gate; run all selected specs.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Print commands without executing them.",
    )
    p.add_argument(
        "--list", action="store_true",
        help="Print all tests (or filtered set) and exit.",
    )
    p.add_argument(
        "--list-categories", action="store_true",
        help="Print all category names with test counts and exit.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="  [%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    args = _build_parser().parse_args(argv)
    reg = TestRegistry()

    # ── --list-categories ────────────────────────────────────────────────
    if args.list_categories:
        cats = reg.list_categories()
        print(f"\n{'Category':<22} {'Tests':>6}  Description")
        print("-" * 70)
        catalog_descs: dict[str, str] = {}
        import yaml
        with (Path(__file__).parent / "test_catalog.yaml").open() as fh:
            catalog_descs = yaml.safe_load(fh).get("categories", {})
        for cat, count in cats.items():
            desc = catalog_descs.get(cat, "")
            print(f"  {cat:<20} {count:>6}  {desc}")
        print(f"\nTotal tests: {len(reg.all())}")
        return 0

    # ── Build the test pool ──────────────────────────────────────────────
    cats = [c.strip() for c in args.categories.split(",")] if args.categories else None
    pool = reg.by_category(*cats) if cats else reg.all()

    if not pool:
        print(f"No tests matched categories: {args.categories}", file=sys.stderr)
        return 2

    # ── --list (before engine gate, shows full universe) ─────────────────
    if args.list:
        print(f"\n{'ID':<45} {'Runner':<14} Categories")
        print("-" * 100)
        for spec in sorted(pool, key=lambda s: s.id):
            cats_str = " ".join(f"[{c}]" for c in sorted(spec.categories))
            print(f"  {spec.id:<43} {spec.runner:<14} {cats_str}")
        print(f"\nTotal: {len(pool)} tests")
        return 0

    # ── Env-var gate (requires_env) ──────────────────────────────────────
    # Applied before engine check so cloud-only tests are filtered early.
    _env_ok: list[bool] = [_env_gate(s) for s in pool]
    env_skipped = [s for s, ok in zip(pool, _env_ok) if not ok]
    pool = [s for s, ok in zip(pool, _env_ok) if ok]
    if env_skipped:
        logger.info("Skipped %d test(s) with missing required env vars", len(env_skipped))

    # ── Engine-reachability filter (before random sample) ────────────────
    # Gate first so --random N draws only from reachable tests.
    if not args.skip_engine_check:
        pool, skipped = _filter_reachable(pool)
        if skipped:
            logger.info("Skipped %d test(s) with unreachable engines", len(skipped))

    if args.random:
        pool = reg.sample(n=args.random, categories=cats, seed=args.seed,
                          _prefiltered=pool)

    if not pool:
        logger.error("No reachable tests to run after engine-gate filtering.")
        return 2

    # ── Run ──────────────────────────────────────────────────────────────
    logger.info(
        "Running %d test(s)%s%s",
        len(pool),
        f" from categories [{args.categories}]" if args.categories else "",
        " [DRY-RUN]" if args.dry_run else "",
    )
    if args.seed is not None:
        logger.info("Random seed: %d", args.seed)

    passed, failed, skipped_all = [], [], []
    for spec in pool:
        runner_fn = _RUNNERS.get(spec.runner)
        if runner_fn is None:
            logger.error("Unknown runner '%s' for spec %s", spec.runner, spec.id)
            failed.append(spec)
            continue
        result = runner_fn(spec, dry_run=args.dry_run)
        if result is None:
            skipped_all.append(spec)
        elif result:
            passed.append(spec)
        else:
            failed.append(spec)

    # ── Summary ──────────────────────────────────────────────────────────
    total = len(pool)
    print("\n" + "=" * 60)
    print(f"Passed  : {len(passed)} / {total}")
    if skipped_all:
        print(f"Skipped : {len(skipped_all)} / {total}  (all tests skipped — app schema not loaded?)")
    print(f"Failed  : {len(failed)} / {total}")
    if failed:
        print("\nFailed tests:")
        for s in failed:
            print(f"  FAIL  {s.id}")
    if skipped_all:
        print("\nAll-skipped tests:")
        for s in skipped_all:
            print(f"  SKIP  {s.id}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
