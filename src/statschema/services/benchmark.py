"""
Service: benchmark

Entry point for programmatic use of the identity test harness.  The full
benchmark CLI lives in ``benchmarks/identity_test.py``; this module exposes
the same pipeline as a callable function for use from notebooks, the future
REST API, or other orchestration layers.
"""

from __future__ import annotations

from typing import Any


def run_identity_test(
    dsn: str,
    dialect: str,
    schema: str,
    sf: float = 1.0,
    source_schema: str | None = None,
    target_schema: str | None = None,
    stats_source_dsn: str | None = None,
    stats_source_dialect: str | None = None,
    seed: int = 42,
    jaccard_threshold: float = 0.70,
    within_2x_threshold: float = 0.50,
) -> dict[str, Any]:
    """Run the statschema identity test against a live database.

    The identity test loads a TPC benchmark dataset into a source schema,
    collects statistics, generates a synthetic copy with injected statistics,
    and scores how closely the synthetic target's ``EXPLAIN`` plans match the
    source's ``EXPLAIN`` plans.

    Parameters
    ----------
    dsn:
        Connection string for the target database (PostgreSQL wire format or
        dialect-specific DSN).
    dialect:
        Target database dialect (e.g. ``"postgres"``, ``"lakebase"``).
    schema:
        TPC benchmark schema name (``"tpch"``, ``"tpcc"``, ``"tpcb"``,
        ``"tpcdi"``, ``"tpcds"``, ``"tpce"``).
    sf:
        Scale factor (default: ``1.0``).
    source_schema:
        Source schema name in the target DB.  Defaults to
        ``f"{schema}_src"``.
    target_schema:
        Synthetic target schema name.  Defaults to ``f"{schema}_tgt"``.
    stats_source_dsn:
        If set, collect statistics from this DSN rather than the target DB
        (cross-engine test).
    stats_source_dialect:
        Dialect for ``stats_source_dsn``.
    seed:
        Random seed for synthetic data generation (default: 42).
    jaccard_threshold:
        Minimum ``node_jaccard`` to pass (default: 0.70).
    within_2x_threshold:
        Minimum ``within_2x`` fraction to pass (default: 0.50).

    Returns
    -------
    dict
        Serialisable result matching the ``IdentityTestResult`` dataclass
        from ``benchmarks/identity_test.py``.

    Notes
    -----
    This function imports ``benchmarks/identity_test.py`` at call time to
    avoid making the benchmark harness a hard dependency of the library.
    The benchmark module path must be on ``sys.path`` or installed as a
    package for this to work.
    """
    import importlib
    import sys
    from pathlib import Path

    # Try to import the benchmark module.  It lives outside the package so we
    # locate it relative to this file's repo root.
    _REPO_ROOT = Path(__file__).resolve().parents[4]
    _BENCH_DIR = _REPO_ROOT / "benchmarks"
    if str(_BENCH_DIR) not in sys.path:
        sys.path.insert(0, str(_BENCH_DIR))

    try:
        it = importlib.import_module("identity_test")
    except ModuleNotFoundError as exc:
        raise ImportError(
            "Could not import benchmarks/identity_test.py.  "
            "Ensure the benchmarks/ directory is on sys.path."
        ) from exc

    return it.run_programmatic(
        dsn=dsn,
        dialect=dialect,
        schema=schema,
        sf=sf,
        source_schema=source_schema or f"{schema}_src",
        target_schema=target_schema or f"{schema}_tgt",
        stats_source_dsn=stats_source_dsn,
        stats_source_dialect=stats_source_dialect,
        seed=seed,
        jaccard_threshold=jaccard_threshold,
        within_2x_threshold=within_2x_threshold,
    )
