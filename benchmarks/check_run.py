"""
Post-run integrity checker for identity_test.py results.

Reads the saved JSON result, optionally scans the log file for genuine errors
(filtering out known-benign warnings), then reconnects to the live database
and verifies that actual table row counts match what was reported.

Usage
-----
    python3 benchmarks/check_run.py RESULT_JSON [LOG_FILE] --dialect DIALECT --dsn DSN

    # Cross-database (Lakebase target): separate source and target connections
    python3 benchmarks/check_run.py result.json \\
        --dialect cockroachdb --dsn "host=127.0.0.1 ..." \\
        --target-dialect lakebase

Examples
--------
    # Same-engine identity test (postgres, cockroachdb, mysql, sqlserver, oracle, db2)
    python3 benchmarks/check_run.py \\
        benchmarks/results/20260327-154446-identity-tpch-sf0.1-postgres.json \\
        benchmarks/logs/identity-20260327-154000/postgres_tpch.out \\
        --dialect postgres \\
        --dsn "host=127.0.0.1 port=5418 dbname=postgres user=postgres password=postgres"

    # Lakebase target test: source is cockroachdb, target is Lakebase
    python3 benchmarks/check_run.py result.json \\
        --dialect cockroachdb --dsn "host=127.0.0.1 port=26257 dbname=defaultdb user=root sslmode=disable" \\
        --target-dialect lakebase

    # Omit the log file if you only want row-count verification:
    python3 benchmarks/check_run.py result.json --dialect oracle --dsn "..."
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Log-file patterns
# ---------------------------------------------------------------------------

# Lines that are always safe to ignore (benign operational messages)
_BENIGN_PATTERNS: list[re.Pattern] = [
    re.compile(r"collect_table_stats failed"),
    re.compile(r"ADMIN_CMD loaded 0/\d+ rows.*falling back"),
    re.compile(r"mssql-python bulkcopy unavailable"),
    re.compile(r"BULK_COPY not implemented for dialect"),
    re.compile(r"DPY-4009"),
    re.compile(r"DPY-3013"),
    re.compile(r"0 positional bind values"),
    re.compile(r"Incorrect syntax near '%s'"),
]

# Patterns that indicate a genuine problem
_ERROR_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"Traceback \(most recent call last\)"), "Python exception raised"),
    (re.compile(r"Timeout Error.*deadline has elapsed"),  "bulkcopy timed out — data may be incomplete"),
    (re.compile(r"EXIT:1"),                               "process exited with error code 1"),
    (re.compile(r"Cannot connect|Connection refused|OperationalError"), "database connection failure"),
    (re.compile(r"RuntimeError:"),                        "unhandled RuntimeError"),
    (re.compile(r"loaded 0/\d{4,} rows.*falling back", re.IGNORECASE),
                                                           "large table fell back to MULTI_ROW (expected for DB2)"),
]


def _is_benign(line: str) -> bool:
    return any(p.search(line) for p in _BENIGN_PATTERNS)


def check_log(log_path: Path) -> list[str]:
    """Return a list of suspicious lines from the log file."""
    problems: list[str] = []
    with log_path.open(errors="replace") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.rstrip()
            if _is_benign(line):
                continue
            for pat, label in _ERROR_PATTERNS:
                if pat.search(line):
                    problems.append(f"  line {lineno:>5}: [{label}]\n            {line}")
                    break
    return problems


# ---------------------------------------------------------------------------
# Database connections — all six engines + Lakebase
# ---------------------------------------------------------------------------

def _connect(dialect: str, dsn: str):
    """Open and return a DBAPI2 connection for the given dialect."""
    p: dict[str, str] = _parse_dsn(dsn)

    if dialect in ("postgres", "cockroachdb", "neon"):
        import psycopg2  # type: ignore
        if dsn:
            return psycopg2.connect(dsn)
        raise RuntimeError(f"DSN required for {dialect}")

    if dialect == "lakebase":
        return _connect_lakebase()

    if dialect == "sqlserver":
        import mssql_python  # type: ignore
        host = p.get("server", p.get("host", "127.0.0.1"))
        port = p.get("port", "1433")
        db   = p.get("database", p.get("db", "master"))
        user = p.get("user", "sa")
        pwd  = p.get("password", "")
        cs   = f"SERVER={host},{port};DATABASE={db};UID={user};PWD={pwd};ENCRYPT=no;TrustServerCertificate=yes"
        conn = mssql_python.connect(cs)
        conn.setautocommit(True)
        return conn

    if dialect == "oracle":
        import oracledb  # type: ignore
        return oracledb.connect(
            user=p.get("user", "system"),
            password=p.get("password", "oracle"),
            dsn=f"{p.get('host','127.0.0.1')}:{p.get('port','1521')}/{p.get('service','XE')}",
        )

    if dialect == "db2":
        import ibm_db_dbi  # type: ignore
        ibm_dsn = (
            f"DATABASE={p.get('database', 'testdb')};"
            f"HOSTNAME={p.get('hostname', p.get('host', '127.0.0.1'))};"
            f"PORT={p.get('port', '50000')};"
            f"UID={p.get('uid', p.get('user', 'db2inst1'))};"
            f"PWD={p.get('pwd', p.get('password', ''))};"
            "PROTOCOL=TCPIP;"
        )
        return ibm_db_dbi.connect(ibm_dsn, "", "")

    if dialect in ("mysql", "mariadb"):
        import pymysql  # type: ignore
        return pymysql.connect(
            host=p.get("host", "127.0.0.1"),
            port=int(p.get("port", "3306")),
            user=p.get("user", "root"),
            password=p.get("password", ""),
            database=p.get("database", "mysql"),
            local_infile=True,
        )

    raise ValueError(f"Unsupported dialect for check_run: {dialect}")


def _connect_lakebase():
    """Connect to Lakebase using STATSCHEMA_LAKEBASE_* env vars (psycopg2)."""
    import psycopg2  # type: ignore

    host     = os.environ.get("STATSCHEMA_LAKEBASE_HOST", "")
    port     = int(os.environ.get("STATSCHEMA_LAKEBASE_PORT", "5432"))
    dbname   = os.environ.get("STATSCHEMA_LAKEBASE_DB", "databricks_postgres")
    user     = os.environ.get("DATABRICKS_CLIENT_ID", "")
    password = _lakebase_token()

    if not host:
        raise RuntimeError(
            "STATSCHEMA_LAKEBASE_HOST not set — run ./scripts/lakebase-up.sh"
        )
    return psycopg2.connect(
        host=host, port=port, dbname=dbname,
        user=user, password=password,
        sslmode="require",
    )


def _lakebase_token() -> str:
    """Generate a short-lived OAuth token for the Databricks service principal."""
    try:
        from databricks.sdk import WorkspaceClient  # type: ignore
        w = WorkspaceClient()
        return w.config.authenticate()["Authorization"].removeprefix("Bearer ")
    except Exception:
        return os.environ.get("DATABRICKS_CLIENT_SECRET", "")


def _parse_dsn(dsn: str) -> dict[str, str]:
    """Parse a space-separated key=value DSN string (or semicolon-separated IBM style)."""
    p: dict[str, str] = {}
    for tok in dsn.split():
        if "=" in tok:
            k, _, v = tok.partition("=")
            p[k.lower()] = v
    if not p:
        for tok in dsn.split(";"):
            if "=" in tok:
                k, _, v = tok.partition("=")
                p[k.lower()] = v
    return p


# ---------------------------------------------------------------------------
# Row-count verification
# ---------------------------------------------------------------------------

def _count_rows(conn, dialect: str, schema: str, table: str) -> int | None:
    """Return COUNT(*) for the given table, or None on error."""
    cur = conn.cursor()
    try:
        if dialect in ("postgres", "cockroachdb", "neon", "lakebase"):
            cur.execute(f'SELECT COUNT(*) FROM "{schema}"."{table}"')
        elif dialect == "sqlserver":
            cur.execute(f"USE [{schema}]")
            cur.execute(f"SELECT COUNT(*) FROM dbo.[{table}]")
        elif dialect == "oracle":
            cur.execute(f"ALTER SESSION SET CURRENT_SCHEMA = {schema}")
            cur.execute(f'SELECT COUNT(*) FROM "{table.upper()}"')
        elif dialect == "db2":
            cur.execute(f'SELECT COUNT(*) FROM "{schema.upper()}"."{table.upper()}"')
        elif dialect in ("mysql", "mariadb"):
            cur.execute(f"SELECT COUNT(*) FROM `{schema}`.`{table}`")
        else:
            return None
        row = cur.fetchone()
        return int(row[0]) if row else None
    except Exception:
        return None


def check_row_counts(
    conn,
    dialect: str,
    schema: str,
    expected: dict[str, int],
    label: str,
) -> list[str]:
    """Return mismatch lines for one schema (source or target)."""
    mismatches: list[str] = []
    for table, exp in expected.items():
        actual = _count_rows(conn, dialect, schema, table)
        if actual is None:
            mismatches.append(f"  {label}.{table}: could not query")
        elif actual != exp:
            mismatches.append(
                f"  {label}.{table}: expected {exp:>10,}  actual {actual:>10,}  ⚠ MISMATCH"
            )
    return mismatches


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Post-run integrity checker for identity_test results"
    )
    ap.add_argument("result_json",  help="Path to the result JSON from identity_test.py")
    ap.add_argument("log_file",     nargs="?", help="Optional path to the captured stdout/stderr log")
    ap.add_argument("--dialect",    required=True,
                    help="Source database dialect (postgres, cockroachdb, mysql, sqlserver, oracle, db2)")
    ap.add_argument("--dsn",        default="",
                    help="DSN for the source database (space-separated key=value pairs)")
    ap.add_argument("--target-dialect", default=None,
                    help="Target database dialect (defaults to same as --dialect). "
                         "Use 'lakebase' for cross-DB tests.")
    ap.add_argument("--target-dsn", default=None,
                    help="DSN for the target database (defaults to same as --dsn).")
    args = ap.parse_args()

    result_path = Path(args.result_json)
    if not result_path.exists():
        print(f"ERROR: result file not found: {result_path}", file=sys.stderr)
        return 1

    result = json.loads(result_path.read_text())

    # Resolve target dialect / dsn
    tgt_dialect = args.target_dialect or args.dialect
    tgt_dsn     = args.target_dsn     or args.dsn

    # ── Header ───────────────────────────────────────────────────────────────
    print("=" * 72)
    print(f"  check_run  {result_path.name}")
    print(f"  schema={result['schema']}  dialect={result['dialect']}  sf={result['sf']}")
    print(f"  overall: {'PASS ✓' if result['passed'] else 'FAIL ✗  ' + result.get('failure_reason','')}")
    if tgt_dialect != args.dialect:
        print(f"  src dialect={args.dialect}  tgt dialect={tgt_dialect}")
    print("=" * 72)

    any_problem = False

    # ── Log file scan ─────────────────────────────────────────────────────────
    if args.log_file:
        log_path = Path(args.log_file)
        if not log_path.exists():
            print(f"  [LOG] log file not found: {log_path}")
        else:
            problems = check_log(log_path)
            if problems:
                any_problem = True
                print(f"\n  [LOG] {len(problems)} suspicious line(s) in {log_path.name}:")
                for p in problems:
                    print(p)
            else:
                print(f"\n  [LOG] {log_path.name} — no unexpected errors or warnings ✓")

    # ── Row-count verification ────────────────────────────────────────────────
    src_expected = result.get("source_row_counts", {})
    tgt_expected = result.get("target_row_counts", {})
    src_schema   = result.get("source_schema", "")
    tgt_schema   = result.get("target_schema", "")

    same_db = (tgt_dialect == args.dialect and tgt_dsn == args.dsn)

    print("\n  [DB] Connecting to verify live row counts…")

    # Source connection
    src_conn = None
    try:
        src_conn = _connect(args.dialect, args.dsn)
    except Exception as e:
        print(f"  [DB] source connection failed: {e}")
        any_problem = True

    # Target connection — reuse source when same database
    tgt_conn = src_conn
    if not same_db:
        try:
            tgt_conn = _connect(tgt_dialect, tgt_dsn)
        except Exception as e:
            print(f"  [DB] target connection failed: {e}")
            any_problem = True
            tgt_conn = None

    all_mismatches: list[str] = []

    if src_conn and src_expected:
        src_mismatches = check_row_counts(src_conn, args.dialect, src_schema, src_expected, src_schema)
        all_mismatches += src_mismatches

    if tgt_conn and tgt_expected:
        tgt_mismatches = check_row_counts(tgt_conn, tgt_dialect, tgt_schema, tgt_expected, tgt_schema)
        all_mismatches += tgt_mismatches

    if all_mismatches:
        any_problem = True
        print(f"  [DB] {len(all_mismatches)} row-count mismatch(es):")
        for m in all_mismatches:
            print(m)
    elif src_conn or tgt_conn:
        total_src = sum(src_expected.values())
        total_tgt = sum(tgt_expected.values())
        print(
            f"  [DB] source: {len(src_expected)} tables  {total_src:>12,} rows — all match ✓\n"
            f"  [DB] target: {len(tgt_expected)} tables  {total_tgt:>12,} rows — all match ✓"
        )

    for conn in {src_conn, tgt_conn} - {None}:
        try:
            conn.close()
        except Exception:
            pass

    # ── Phase timings ─────────────────────────────────────────────────────────
    print("\n  [TIMING]")
    for phase, secs in result.get("phase_times", {}).items():
        print(f"    {phase:<30} {secs:>8.1f}s")

    # ── Query scores ──────────────────────────────────────────────────────────
    print("\n  [SCORES]")
    for q, m in result.get("query_metrics", {}).items():
        jac = m.get("node_jaccard", 0)
        w2x = m.get("within_2x", 0)
        src_n = m.get("source_nodes", "?")
        tgt_n = m.get("target_nodes", "?")
        flag = "" if jac >= 0.7 and w2x >= 0.5 else "  ⚠"
        print(f"    {q:<20} jaccard={jac:.2f}  within_2x={w2x:.2f}  nodes={src_n}→{tgt_n}{flag}")

    print()
    if any_problem:
        print("  Overall check: ISSUES FOUND ⚠")
        return 1
    print("  Overall check: CLEAN ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
