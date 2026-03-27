"""
Query replayer — run EXPLAIN on each entry of a QueryWorkload against a live target.

Public API
----------
replay_queries(conn, workload, target_dialect, skip_manual_review)
    → list[ReplayResult]

print_replay_report(results, file)
    Pretty-prints a summary table to stdout (or any file-like object).

The replayer transpiles each query to the target dialect (using query_transpiler)
then issues ``EXPLAIN <query>`` against the live connection.  It never executes
the query itself — only the EXPLAIN plan is collected.

For SQL Server the ``SET SHOWPLAN_TEXT ON`` approach is used instead of EXPLAIN.
For Oracle, ``EXPLAIN PLAN FOR`` + ``SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY)`` is used.
"""

from __future__ import annotations

import dataclasses
import sys
from typing import Any, TextIO

from .model import QueryEntry, QueryWorkload, ReplayResult
from .transpiler import transpile_query

_PG_FAMILY   = {"postgres", "postgresql", "neon", "cockroachdb", "lakebase"}
_MYSQL_FAMILY = {"mysql", "mariadb"}


def _explain_postgres(cur: Any, sql: str) -> tuple[str, str | None]:
    try:
        cur.execute(f"EXPLAIN {sql}")
        rows = cur.fetchall()
        return "\n".join(r[0] for r in rows), None
    except Exception as exc:
        return "", str(exc)


def _explain_mysql(cur: Any, sql: str) -> tuple[str, str | None]:
    try:
        cur.execute(f"EXPLAIN {sql}")
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        lines = ["\t".join(cols)]
        for row in rows:
            lines.append("\t".join("" if v is None else str(v) for v in row))
        return "\n".join(lines), None
    except Exception as exc:
        return "", str(exc)


def _explain_sqlserver(cur: Any, sql: str) -> tuple[str, str | None]:
    try:
        cur.execute("SET SHOWPLAN_TEXT ON")
        cur.execute(sql)
        rows = cur.fetchall()
        plan = "\n".join(r[0] for r in rows if r[0])
        cur.execute("SET SHOWPLAN_TEXT OFF")
        return plan, None
    except Exception as exc:
        try:
            cur.execute("SET SHOWPLAN_TEXT OFF")
        except Exception:
            pass
        return "", str(exc)


def _explain_oracle(cur: Any, sql: str) -> tuple[str, str | None]:
    try:
        cur.execute(f"EXPLAIN PLAN FOR {sql}")
        cur.execute("SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY)")
        rows = cur.fetchall()
        return "\n".join(str(r[0]) for r in rows), None
    except Exception as exc:
        return "", str(exc)


def _explain_databricks(cur: Any, sql: str) -> tuple[str, str | None]:
    try:
        cur.execute(f"EXPLAIN {sql}")
        rows = cur.fetchall()
        return "\n".join(str(r[0]) for r in rows), None
    except Exception as exc:
        return "", str(exc)


def _run_explain(cur: Any, dialect: str, sql: str) -> tuple[str, str | None]:
    d = dialect.lower()
    if d in _PG_FAMILY:
        return _explain_postgres(cur, sql)
    if d in _MYSQL_FAMILY:
        return _explain_mysql(cur, sql)
    if d == "sqlserver":
        return _explain_sqlserver(cur, sql)
    if d == "oracle":
        return _explain_oracle(cur, sql)
    if d == "databricks":
        return _explain_databricks(cur, sql)
    return "", f"EXPLAIN not implemented for dialect '{dialect}'"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def replay_queries(
    conn: Any,
    workload: QueryWorkload,
    target_dialect: str,
    skip_manual_review: bool = False,
) -> list[ReplayResult]:
    """Transpile and EXPLAIN every query in *workload* against *conn*.

    Parameters
    ----------
    conn
        An open DB-API 2.0 connection to the target database.
    workload
        QueryWorkload produced by ``collect_top_queries`` or ``load_queries``.
    target_dialect
        Target dialect (e.g. ``"postgres"``, ``"mysql"``).
    skip_manual_review
        When True, entries flagged ``manual_review=True`` are skipped entirely
        rather than attempted.  Default: False (attempt them, expect failures).

    Returns
    -------
    list[ReplayResult]
        One result per query.  Inspect ``result.success`` and
        ``result.explain_output`` for plan text.
    """
    cur = conn.cursor()
    results: list[ReplayResult] = []

    for entry in workload.queries:
        transpiled = transpile_query(entry, target_dialect)

        if transpiled.manual_review and skip_manual_review:
            results.append(ReplayResult(
                query_id=entry.id,
                target_dialect=target_dialect,
                sql=entry.sql,
                explain_output="",
                success=False,
                error=f"Skipped (manual_review): {transpiled.transpile_error}",
            ))
            continue

        plan, err = _run_explain(cur, target_dialect, transpiled.sql)
        results.append(ReplayResult(
            query_id=entry.id,
            target_dialect=target_dialect,
            sql=transpiled.sql,
            explain_output=plan,
            success=err is None,
            error=err,
        ))

    return results


def print_replay_report(results: list[ReplayResult], file: TextIO = sys.stdout) -> None:
    """Print a human-readable summary of replay results.

    Shows one line per query with status, then full plan text for successes
    and error text for failures.
    """
    ok    = sum(1 for r in results if r.success)
    fail  = len(results) - ok
    skip  = sum(1 for r in results if not r.success and r.error and "Skipped" in r.error)
    error = fail - skip

    print(f"\n  Replay results: {ok} ok / {error} errors / {skip} skipped ({len(results)} total)\n", file=file)

    for r in results:
        status = "✓" if r.success else ("↷" if r.error and "Skipped" in r.error else "✗")
        print(f"  {status}  {r.query_id}", file=file)
        if not r.success and r.error:
            print(f"     {r.error}", file=file)

    for r in results:
        if r.success and r.explain_output:
            print(f"\n── Plan: {r.query_id} ──", file=file)
            print(r.explain_output, file=file)
