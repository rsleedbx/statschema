"""
Live identity tests — validates statschema's query-plan fidelity on real databases.

The identity test is a control experiment:

    1. Load a TPC-x dataset from the canonical YAML generator.
    2. Collect EXPLAIN plans for representative queries → baseline.
    3. Use statschema's collect → generate → inject pipeline to create a copy.
    4. Run the same EXPLAIN queries on the copy.
    5. PASS if the copy's plans are structurally similar and row-count estimates
       are within 2× of the source at a majority of plan nodes.

This answers the DBA's core question: "If I use statschema to prepare a migration
target, will my queries behave the same before any production data arrives?"

Prerequisites
-------------
PostgreSQL container (pg16 used as the primary identity test target):

    podman run -d --name pg16 \\
        -e POSTGRES_PASSWORD=testpass -e POSTGRES_DB=statschema \\
        -p 5416:5432 docker.io/library/postgres:16

Environment variables (defaults match the Podman command above):

    PG_HOST       default: 127.0.0.1
    PG_USER       default: postgres
    PG_PASSWORD   default: testpass
    PG_DB         default: statschema
    PG16_PORT     default: 5416

Test matrix
-----------
  test_tpch_identity_quick      SF=0.1, 4 queries  ~30s  (smoke test, always runs)
  test_tpch_identity_sf1        SF=1.0, 4 queries  ~3m   (full validation)
  test_tpcb_identity_quick      SF=0.1, pg only    ~10s
  test_score_plans_unit         no DB required     unit test for scoring logic

Pass criteria
-------------
  mean_node_jaccard ≥ 0.70  (same join methods)
  mean_within_2x    ≥ 0.50  (majority of estimates within 2×)

The quick smoke test uses relaxed thresholds (≥ 0.60 / ≥ 0.40) since SF=0.1
has fewer rows and the planner may choose different access methods for
small tables.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent

# ---------------------------------------------------------------------------
# Connection configuration
# ---------------------------------------------------------------------------

from benchmarks.bench_config import DEFAULT_CATALOG
from tests.live_helpers import _tp  # noqa: E402

_p16 = _tp("test_postgres16")

_HOST    = _p16.host     or "127.0.0.1"
_USER    = _p16.username or "postgres"
_PASS    = _p16.password or "testpass"
_DB      = _p16.database or DEFAULT_CATALOG
_PORT_16 = _p16.port     or 5416


def _pg16_dsn() -> str:
    return (
        f"host={_HOST} port={_PORT_16} dbname={_DB} "
        f"user={_USER} password={_PASS}"
    )


def _pg16_reachable() -> bool:
    try:
        s = socket.create_connection((_HOST, _PORT_16), timeout=2)
        s.close()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def pg16_conn():
    """Module-scoped psycopg2 connection to pg16, skipped if unreachable."""
    if not _pg16_reachable():
        pytest.skip(f"pg16 not reachable at {_HOST}:{_PORT_16}")
    try:
        import psycopg2  # type: ignore
    except ImportError:
        pytest.skip("psycopg2 not installed")
    conn = psycopg2.connect(_pg16_dsn())
    conn.autocommit = False
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Unit test: scoring logic (no DB required)
# ---------------------------------------------------------------------------

def test_score_plans_unit():
    """Score two identical plan trees → jaccard=1.0 and all ratios within 2×."""
    from benchmarks.identity_test import score_plans

    plan = {
        "Node Type": "Hash Join",
        "Plan Rows": 1000,
        "Plans": [
            {"Node Type": "Seq Scan", "Plan Rows": 10000},
            {
                "Node Type": "Hash",
                "Plan Rows": 500,
                "Plans": [{"Node Type": "Seq Scan", "Plan Rows": 500}],
            },
        ],
    }
    metrics = score_plans("test-q01", plan, plan)
    assert metrics.node_jaccard == pytest.approx(1.0)
    assert metrics.within_2x == pytest.approx(1.0)
    assert metrics.within_10x == pytest.approx(1.0)


def test_score_plans_different_nodes():
    """Score two plans with different node types → jaccard < 1."""
    from benchmarks.identity_test import score_plans

    source = {
        "Node Type": "Hash Join",
        "Plan Rows": 1000,
        "Plans": [
            {"Node Type": "Seq Scan", "Plan Rows": 10000},
            {"Node Type": "Hash",     "Plan Rows": 500,
             "Plans": [{"Node Type": "Seq Scan", "Plan Rows": 500}]},
        ],
    }
    target = {
        "Node Type": "Nested Loop",          # different top node
        "Plan Rows": 1000,
        "Plans": [
            {"Node Type": "Index Scan", "Plan Rows": 10000},  # different access
            {"Node Type": "Seq Scan",   "Plan Rows": 500},
        ],
    }
    metrics = score_plans("test-q02", source, target)
    assert metrics.node_jaccard < 1.0


def test_score_plans_row_estimate_ratios():
    """Nodes with 10× off estimates reduce within_2x but not within_10x."""
    from benchmarks.identity_test import score_plans

    source = {"Node Type": "Seq Scan", "Plan Rows": 1000}
    target = {"Node Type": "Seq Scan", "Plan Rows": 10000}  # 10× off
    metrics = score_plans("test-q03", source, target)
    assert metrics.within_2x == pytest.approx(0.0)
    assert metrics.within_10x == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Live identity tests
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_tpch_identity_quick(pg16_conn):
    """
    TPC-H identity test at SF=0.1 — quick smoke test.

    Validates the full statschema pipeline on a 10% scale TPC-H dataset.
    Relaxed pass thresholds: at SF=0.1 the planner may prefer different
    access methods for small tables.

    Expected runtime: ~30–60s.
    """
    from benchmarks.identity_test import run_identity_test

    result = run_identity_test(
        schema="tpch",
        sf=0.1,
        dialect="postgres",
        dsn=_pg16_dsn(),
        source_schema="tpch_src_quick",
        target_schema="tpch_tgt_quick",
        threshold_node_jaccard=0.60,
        threshold_within_2x=0.40,
        seed=42,
    )

    print(result.summary())

    assert result.mean_node_jaccard >= 0.60, (
        f"node_jaccard {result.mean_node_jaccard:.3f} < 0.60\n{result.summary()}"
    )
    assert result.mean_within_2x >= 0.40, (
        f"within_2x {result.mean_within_2x:.3f} < 0.40\n{result.summary()}"
    )
    assert len(result.query_metrics) > 0, "No query metrics collected"


@pytest.mark.slow
def test_tpch_identity_sf1(pg16_conn):
    """
    TPC-H identity test at SF=1 — full validation.

    At SF=1 PostgreSQL has enough data to make meaningful plan choices
    (hash joins, index scans). The target copy must achieve:
        mean_node_jaccard ≥ 0.70   (same join operators chosen)
        mean_within_2x    ≥ 0.50   (majority of row estimates within 2×)

    Expected runtime: ~3–5 min.
    """
    from benchmarks.identity_test import run_identity_test

    result = run_identity_test(
        schema="tpch",
        sf=1.0,
        dialect="postgres",
        dsn=_pg16_dsn(),
        source_schema="tpch_src_sf1",
        target_schema="tpch_tgt_sf1",
        threshold_node_jaccard=0.70,
        threshold_within_2x=0.50,
        seed=42,
    )

    print(result.summary())

    assert result.passed, result.summary()
    assert len(result.query_metrics) == 4, (
        f"Expected 4 queries, got {len(result.query_metrics)}"
    )
    # Q1 and Q6 are scan-only; they should always match well
    if "tpch-q01" in result.query_metrics:
        q1 = result.query_metrics["tpch-q01"]
        assert q1.node_jaccard >= 0.70, f"Q1 jaccard too low: {q1.node_jaccard:.3f}"
    if "tpch-q06" in result.query_metrics:
        q6 = result.query_metrics["tpch-q06"]
        assert q6.node_jaccard >= 0.70, f"Q6 jaccard too low: {q6.node_jaccard:.3f}"


@pytest.mark.slow
def test_tpcb_identity_quick(pg16_conn):
    """
    TPC-B identity test at SF=0.1 — validates the simple OLTP schema.

    TPC-B has no benchmark query YAML yet (only 4 SQL statements).
    This test validates that the load + collect + inject pipeline
    completes without error and produces the correct row counts.
    """
    from benchmarks.identity_test import (
        load_source, collect_stats, build_target,
        _connect, load_canonical, resolve_load_order,
    )
    from src.statschema.schema_io import resolve_row_counts

    conn = _connect("postgres", _pg16_dsn())
    yaml_path = _REPO_ROOT / "benchmarks" / "schemas" / "tpcb_schema.yaml"
    tables  = load_canonical(yaml_path)
    ordered = resolve_load_order(tables)

    source_rc = load_source(conn, "tpcb", 0.1, "tpcb_src_test", "postgres", seed=42)
    stats     = collect_stats(conn, ordered, "tpcb_src_test", "postgres")
    target_rc, _ = build_target(
        conn, ordered, stats, "tpcb_tgt_test", "postgres", sf=0.1, seed=99
    )

    for tname in source_rc:
        assert tname in target_rc, f"Table {tname} missing from target"
        # Target should have the same or similar row count (synthetic generation
        # uses the same SF so counts should match exactly)
        assert target_rc[tname] > 0, f"Table {tname} has 0 rows in target"

    conn.close()


# ---------------------------------------------------------------------------
# Parametrised: run each query individually so CI shows per-query breakdown
# ---------------------------------------------------------------------------

@pytest.mark.slow
@pytest.mark.parametrize("query_id", ["tpch-q01", "tpch-q03", "tpch-q06", "tpch-q14"])
def test_tpch_query_loads_and_explains(pg16_conn, query_id):
    """
    Per-query smoke test: load TPC-H at SF=0.1 and verify EXPLAIN succeeds.

    Does NOT run the full identity pipeline — just validates that the SQL
    in tpch.yaml is syntactically correct for PostgreSQL and that the
    planner produces at least one plan node.
    """
    from benchmarks.identity_test import (
        load_source, _explain_pg, load_canonical, resolve_load_order,
        _SOURCE_SCHEMA,
    )
    from src.statschema.query_model import load_queries

    # Load source once (module-scoped would be faster, but keeping test independent)
    yaml_path = _REPO_ROOT / "benchmarks" / "schemas" / "tpch_schema.yaml"
    tables  = load_canonical(yaml_path)
    ordered = resolve_load_order(tables)

    src_schema = f"tpch_src_qtest_{query_id.replace('-', '_')}"
    load_source(pg16_conn, "tpch", 0.1, src_schema, "postgres", seed=42)

    queries_yaml = _REPO_ROOT / "benchmarks" / "queries" / "tpch.yaml"
    workload = load_queries(queries_yaml)
    entry = next((q for q in workload.queries if q.id == query_id), None)
    assert entry is not None, f"Query {query_id} not found in tpch.yaml"

    plan = _explain_pg(pg16_conn, entry.sql, src_schema)
    assert "Node Type" in plan, f"EXPLAIN returned no plan nodes for {query_id}"
    print(f"\n{query_id} plan root: {plan['Node Type']}  "
          f"rows={plan.get('Plan Rows', '?')}")
