"""
Post-run integrity checker for identity_test.py results.

Reads the saved JSON result, optionally scans the log file for genuine errors
(filtering out known-benign warnings), then reconnects to the live database
and verifies that actual table row counts match what was reported.

Usage
-----
    python3 benchmarks/check_run.py RESULT_JSON [LOG_FILE] --dialect DIALECT --dsn DSN

Examples
--------
    python3 benchmarks/check_run.py \\
        benchmarks/results/20260325-154446-identity-tpch-sf0.01-sqlserver.json \\
        /tmp/tpch_sqlserver.log \\
        --dialect sqlserver \\
        --dsn "server=127.0.0.1 port=14330 user=sa password=... database=master"

    # Omit the log file if you only want row-count verification:
    python3 benchmarks/check_run.py result.json --dialect oracle --dsn "..."
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Log-file patterns
# ---------------------------------------------------------------------------

# Lines that are always safe to ignore (benign operational messages)
_BENIGN_PATTERNS: list[re.Pattern] = [
    re.compile(r"collect_table_stats failed"),          # stats collection is best-effort
    re.compile(r"ADMIN_CMD loaded 0/\d+ rows.*falling back"),  # DB2 MULTI_ROW fallback
    re.compile(r"mssql-python bulkcopy unavailable"),   # old fallback message
    re.compile(r"BULK_COPY not implemented for dialect"),
    re.compile(r"DPY-4009"),                            # Oracle bind-param mismatch in stats
    re.compile(r"DPY-3013"),                            # Oracle type in stats
    re.compile(r"0 positional bind values"),
    re.compile(r"Incorrect syntax near '%s'"),          # SQL Server before %s→? fix
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
    seen_traceback = False
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
# Live row-count verification
# ---------------------------------------------------------------------------

def _connect(dialect: str, dsn: str):
    """Re-open the same connection used by the identity test."""
    # Inline the DSN parser from identity_test so we don't import the whole module
    p: dict[str, str] = {}
    for tok in dsn.split():
        if "=" in tok:
            k, _, v = tok.partition("=")
            p[k.lower()] = v
    # Also support semicolon-separated IBM DSNs
    if not p:
        for tok in dsn.split(";"):
            if "=" in tok:
                k, _, v = tok.partition("=")
                p[k.lower()] = v

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


def _count_rows(conn, dialect: str, schema: str, table: str) -> int | None:
    """Return COUNT(*) or None on error."""
    cur = conn.cursor()
    try:
        if dialect == "sqlserver":
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
    except Exception as e:
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
                f"  {label}.{table}: expected {exp:>10,}  actual {actual:>10,}"
                f"  {'⚠ MISMATCH' if actual != exp else 'ok'}"
            )
    return mismatches


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Post-run integrity checker for identity_test results")
    ap.add_argument("result_json", help="Path to the result JSON from identity_test.py")
    ap.add_argument("log_file", nargs="?", help="Optional path to the captured stdout/stderr log")
    ap.add_argument("--dialect", required=True, help="Database dialect (sqlserver, oracle, db2, mysql)")
    ap.add_argument("--dsn", required=True, help="DSN string (same format as identity_test --dsn)")
    args = ap.parse_args()

    result_path = Path(args.result_json)
    if not result_path.exists():
        print(f"ERROR: result file not found: {result_path}", file=sys.stderr)
        return 1

    result = json.loads(result_path.read_text())

    # ── Header ───────────────────────────────────────────────────────────────
    print("=" * 72)
    print(f"  check_run  {result_path.name}")
    print(f"  schema={result['schema']}  dialect={result['dialect']}  sf={result['sf']}")
    print(f"  overall: {'PASS ✓' if result['passed'] else 'FAIL ✗  ' + result.get('failure_reason','')}")
    print("=" * 72)

    any_problem = False

    # ── Log file scan ────────────────────────────────────────────────────────
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

    # ── Live row-count verification ──────────────────────────────────────────
    print("\n  [DB] Connecting to verify live row counts…")
    try:
        conn = _connect(args.dialect, args.dsn)
    except Exception as e:
        print(f"  [DB] connection failed: {e}")
        return 1

    src_expected = result.get("source_row_counts", {})
    tgt_expected = result.get("target_row_counts", {})
    src_schema   = result["source_schema"]
    tgt_schema   = result["target_schema"]

    src_mismatches = check_row_counts(conn, args.dialect, src_schema, src_expected, src_schema)
    tgt_mismatches = check_row_counts(conn, args.dialect, tgt_schema, tgt_expected, tgt_schema)

    all_mismatches = src_mismatches + tgt_mismatches
    if all_mismatches:
        any_problem = True
        print(f"  [DB] {len(all_mismatches)} row-count mismatch(es):")
        for m in all_mismatches:
            print(m)
    else:
        total_src = sum(src_expected.values())
        total_tgt = sum(tgt_expected.values())
        print(
            f"  [DB] source: {len(src_expected)} tables  {total_src:>12,} rows — all match ✓\n"
            f"  [DB] target: {len(tgt_expected)} tables  {total_tgt:>12,} rows — all match ✓"
        )

    try:
        conn.close()
    except Exception:
        pass

    # ── Phase timings ────────────────────────────────────────────────────────
    print("\n  [TIMING]")
    for phase, secs in result.get("phase_times", {}).items():
        print(f"    {phase:<30} {secs:>8.1f}s")

    # ── Query scores ─────────────────────────────────────────────────────────
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
