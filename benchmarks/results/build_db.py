"""
Build a SQLite results database from identity test JSON files.

The JSON files are the source of truth (committed to git, human-readable).
The SQLite database is derived and should be added to .gitignore — it exists
for local querying, charting, and cross-run comparisons only.

Usage
-----
    python benchmarks/results/build_db.py              # scans benchmarks/results/
    python benchmarks/results/build_db.py --db /tmp/results.db
    python benchmarks/results/build_db.py --query "SELECT schema, dialect, mean_within_2x FROM runs"

Schema
------
runs (one row per identity test execution)
    run_id              TEXT  PRIMARY KEY  — timestamp-schema-dialect filename stem
    schema              TEXT              — tpcb / tpcc / tpch / tpcdi / tpcds / tpce
    dialect             TEXT              — postgres / neon / lakebase / cockroachdb
    sf                  REAL              — scale factor
    run_date            TEXT              — ISO date extracted from filename
    source_schema       TEXT
    target_schema       TEXT
    mean_node_jaccard   REAL
    mean_within_2x      REAL
    mean_within_10x     REAL
    passed              INTEGER           — 1 / 0
    failure_reason      TEXT
    stats_injection_mode TEXT             — injected / analyze_fallback / none
    total_source_rows   INTEGER
    total_target_rows   INTEGER
    phase_times         TEXT              — JSON blob of phase → seconds dict
    json_file           TEXT              — source filename

query_results (one row per query per run)
    id                  INTEGER  PRIMARY KEY AUTOINCREMENT
    run_id              TEXT     REFERENCES runs(run_id)
    query_id            TEXT
    node_jaccard        REAL
    within_2x           REAL
    within_10x          REAL
    source_node_count   INTEGER
    target_node_count   INTEGER
    source_plan_depth   INTEGER
    target_plan_depth   INTEGER

plan_nodes (one row per plan node position per query per run)
    id                  INTEGER  PRIMARY KEY AUTOINCREMENT
    run_id              TEXT     REFERENCES runs(run_id)
    query_id            TEXT
    position            INTEGER  — 0-based index in the flattened node list
    source_node_type    TEXT
    target_node_type    TEXT
    row_ratio           REAL     — max(s/t, t/s), always ≥ 1
    within_2x           INTEGER  — 1 / 0
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
from pathlib import Path

_DEFAULT_DB = Path(__file__).parent / "identity_results.db"
_RESULTS_DIR = Path(__file__).parent


def _create_schema(cur: sqlite3.Cursor) -> None:
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            run_id               TEXT PRIMARY KEY,
            schema               TEXT,
            dialect              TEXT,
            sf                   REAL,
            run_date             TEXT,
            source_schema        TEXT,
            target_schema        TEXT,
            mean_node_jaccard    REAL,
            mean_within_2x       REAL,
            mean_within_10x      REAL,
            passed               INTEGER,
            failure_reason       TEXT,
            stats_injection_mode TEXT,
            total_source_rows    INTEGER,
            total_target_rows    INTEGER,
            phase_times          TEXT,
            json_file            TEXT
        );

        CREATE TABLE IF NOT EXISTS query_results (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id            TEXT REFERENCES runs(run_id),
            query_id          TEXT,
            node_jaccard      REAL,
            within_2x         REAL,
            within_10x        REAL,
            source_node_count INTEGER,
            target_node_count INTEGER,
            source_plan_depth INTEGER,
            target_plan_depth INTEGER
        );

        CREATE TABLE IF NOT EXISTS plan_nodes (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id           TEXT REFERENCES runs(run_id),
            query_id         TEXT,
            position         INTEGER,
            source_node_type TEXT,
            target_node_type TEXT,
            row_ratio        REAL,
            within_2x        INTEGER
        );
    """)


def _run_id_from_file(path: Path) -> str:
    return path.stem  # e.g. "20260325-000256-identity-tpce-sf0.01-postgres"


def _run_date_from_id(run_id: str) -> str:
    # stem starts with YYYYMMDD-HHMMSS
    parts = run_id.split("-")
    if len(parts) >= 1 and len(parts[0]) == 8:
        d = parts[0]
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    return ""


def _load_one(cur: sqlite3.Cursor, path: Path) -> tuple[int, int, int]:
    """Load one JSON file. Returns (runs_added, queries_added, nodes_added)."""
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        print(f"  SKIP {path.name}: {exc}", file=sys.stderr)
        return 0, 0, 0

    # Identify identity test files by their required keys
    if not all(k in data for k in ("schema", "dialect", "sf", "mean_node_jaccard")):
        return 0, 0, 0

    run_id = _run_id_from_file(path)

    # Skip if already loaded
    cur.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,))
    if cur.fetchone():
        return 0, 0, 0

    source_rows = sum((data.get("source_row_counts") or {}).values())
    target_rows = sum((data.get("target_row_counts") or {}).values())

    cur.execute("""
        INSERT INTO runs (
            run_id, schema, dialect, sf, run_date,
            source_schema, target_schema,
            mean_node_jaccard, mean_within_2x, mean_within_10x,
            passed, failure_reason, stats_injection_mode,
            total_source_rows, total_target_rows,
            phase_times, json_file
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        run_id,
        data.get("schema", ""),
        data.get("dialect", ""),
        data.get("sf", 0.0),
        _run_date_from_id(run_id),
        data.get("source_schema", ""),
        data.get("target_schema", ""),
        data.get("mean_node_jaccard", 0.0),
        data.get("mean_within_2x", 0.0),
        data.get("mean_within_10x", 0.0),
        1 if data.get("passed") else 0,
        data.get("failure_reason", ""),
        data.get("stats_injection_mode", ""),
        source_rows,
        target_rows,
        json.dumps(data.get("phase_times") or {}),
        path.name,
    ))

    queries_added = nodes_added = 0
    for qid, qm in (data.get("query_metrics") or {}).items():
        source_types = qm.get("source_node_types", [])
        target_types = qm.get("target_node_types", [])
        ratios       = qm.get("row_estimate_ratios", [])

        cur.execute("""
            INSERT INTO query_results (
                run_id, query_id, node_jaccard, within_2x, within_10x,
                source_node_count, target_node_count,
                source_plan_depth, target_plan_depth
            ) VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            run_id, qid,
            qm.get("node_jaccard", 0.0),
            qm.get("within_2x", 0.0),
            qm.get("within_10x", 0.0),
            len(source_types),
            len(target_types),
            qm.get("source_plan_depth", 0),
            qm.get("target_plan_depth", 0),
        ))
        queries_added += 1

        for pos, (stype, ttype, ratio) in enumerate(
            zip(source_types, target_types, ratios)
        ):
            cur.execute("""
                INSERT INTO plan_nodes (
                    run_id, query_id, position,
                    source_node_type, target_node_type,
                    row_ratio, within_2x
                ) VALUES (?,?,?,?,?,?,?)
            """, (
                run_id, qid, pos,
                stype, ttype,
                ratio,
                1 if ratio <= 2.0 else 0,
            ))
            nodes_added += 1

    return 1, queries_added, nodes_added


def build(results_dir: Path, db_path: Path, verbose: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    cur = conn.cursor()
    _create_schema(cur)
    conn.commit()

    files = sorted(results_dir.glob("*identity*.json"))
    total_runs = total_queries = total_nodes = 0
    for f in files:
        r, q, n = _load_one(cur, f)
        total_runs += r
        total_queries += q
        total_nodes += n

    conn.commit()
    if verbose:
        cur.execute("SELECT COUNT(*) FROM runs")
        all_runs = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM query_results")
        all_queries = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM plan_nodes")
        all_nodes = cur.fetchone()[0]
        print(f"Loaded {total_runs} new run(s), {total_queries} query result(s), "
              f"{total_nodes} plan node(s).")
        print(f"Database totals: {all_runs} runs, {all_queries} query results, "
              f"{all_nodes} plan nodes.")
    return conn


def _print_summary(conn: sqlite3.Connection) -> None:
    print("\nRuns by (schema, dialect, sf):")
    print(f"  {'schema':<10} {'dialect':<14} {'sf':>6}  {'jaccard':>8}  {'within_2x':>9}  {'pass':>5}")
    print(f"  {'-'*10} {'-'*14} {'-'*6}  {'-'*8}  {'-'*9}  {'-'*5}")
    for row in conn.execute("""
        SELECT schema, dialect, sf,
               AVG(mean_node_jaccard), AVG(mean_within_2x),
               SUM(passed), COUNT(*)
        FROM runs
        GROUP BY schema, dialect, sf
        ORDER BY schema, dialect, sf
    """):
        schema, dialect, sf, jac, w2x, passes, total = row
        print(f"  {schema:<10} {dialect:<14} {sf:>6.2f}  {jac:>8.3f}  {w2x:>9.3f}  "
              f"{'✓' if passes == total else '✗':>5} ({passes}/{total})")

    print("\nEntry counts (projected at full coverage):")
    print("  Current:")
    for tbl in ("runs", "query_results", "plan_nodes"):
        n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        print(f"    {tbl:<15}: {n:>5} rows")

    distinct_dialects = conn.execute("SELECT COUNT(DISTINCT dialect) FROM runs").fetchone()[0]
    distinct_combos   = conn.execute(
        "SELECT COUNT(DISTINCT schema||'|'||sf) FROM runs WHERE dialect='postgres'"
    ).fetchone()[0]
    projected_runs    = distinct_combos * 4  # postgres + neon + lakebase + cockroachdb
    avg_queries = conn.execute(
        "SELECT AVG(cnt) FROM (SELECT COUNT(*) AS cnt FROM query_results GROUP BY run_id)"
    ).fetchone()[0] or 4
    avg_nodes   = conn.execute(
        "SELECT AVG(cnt) FROM (SELECT COUNT(*) AS cnt FROM plan_nodes GROUP BY run_id)"
    ).fetchone()[0] or 40
    print(f"\n  Projected at 4 dialects × {distinct_combos} (schema, sf) combinations:")
    print(f"    runs          : {projected_runs:>5} rows")
    print(f"    query_results : {int(projected_runs * avg_queries):>5} rows")
    print(f"    plan_nodes    : {int(projected_runs * avg_nodes):>5} rows")
    print(f"\n  → SQLite is appropriate; total projected size < 1 MB.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1].strip())
    parser.add_argument("--db",    default=str(_DEFAULT_DB),
                        help="Output SQLite path (default: benchmarks/results/identity_results.db)")
    parser.add_argument("--dir",   default=str(_RESULTS_DIR),
                        help="Directory to scan for JSON files")
    parser.add_argument("--query", default="",
                        help="Run an ad-hoc SQL query against the database and print results")
    parser.add_argument("--summary", action="store_true",
                        help="Print cross-run summary table after building")
    args = parser.parse_args()

    db_path = Path(args.db)
    results_dir = Path(args.dir)

    conn = build(results_dir, db_path)

    if args.summary or not args.query:
        _print_summary(conn)

    if args.query:
        print(f"\nQuery: {args.query}")
        for row in conn.execute(args.query):
            print(" ", row)

    conn.close()
    print(f"\nDatabase: {db_path}")


if __name__ == "__main__":
    main()
