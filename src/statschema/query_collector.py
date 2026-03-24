"""
Top-query collection from live databases.

Public API
----------
collect_top_queries(conn, dialect, n, rank_by, catalog, schema)
    → QueryWorkload

Supported dialects
------------------
postgres / neon / cockroachdb / lakebase
    Reads pg_stat_statements (requires the extension to be enabled).
    Falls back gracefully if the extension is absent.

mysql / mariadb
    Reads performance_schema.events_statements_summary_by_digest.
    Filters to the supplied catalog (database name) when given.

sqlserver
    Reads sys.dm_exec_query_stats + sys.dm_exec_sql_text.

oracle
    Reads v$sql (requires SELECT privilege on V$SQL).

databricks
    Reads system.query.history (requires system catalog access).

sqlite
    Not supported — SQLite has no query-statistics catalog.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from .query_model import QueryEntry, QueryWorkload

# Maps the rank_by alias to (pg_col, mysql_col, tsql_col, oracle_col, databricks_col)
_RANK_COLS: dict[str, dict[str, str]] = {
    "total_time": {
        "postgres":   "total_exec_time",
        "mysql":      "SUM_TIMER_WAIT",
        "sqlserver":  "total_elapsed_time",
        "oracle":     "elapsed_time",
        "databricks": "duration",
    },
    "calls": {
        "postgres":   "calls",
        "mysql":      "COUNT_STAR",
        "sqlserver":  "execution_count",
        "oracle":     "executions",
        "databricks": "duration",   # no separate calls count in system.query.history
    },
    "mean_time": {
        "postgres":   "mean_exec_time",
        "mysql":      "AVG_TIMER_WAIT",
        "sqlserver":  "total_elapsed_time / NULLIF(execution_count, 0)",
        "oracle":     "elapsed_time / NULLIF(executions, 0)",
        "databricks": "duration",
    },
}

_PG_FAMILY = {"postgres", "postgresql", "neon", "cockroachdb", "lakebase"}
_MYSQL_FAMILY = {"mysql", "mariadb"}


def _short_id(sql: str) -> str:
    return hashlib.md5(sql.encode()).hexdigest()[:12]


def _extract_tables(sql: str) -> list[str]:
    """Best-effort extraction of table names from a query."""
    # Match: FROM tbl, JOIN tbl — strips schema prefix and aliases
    pattern = r"\b(?:FROM|JOIN)\s+(?:`?\w+`?\.)?`?(\w+)`?"
    return list(dict.fromkeys(re.findall(pattern, sql, re.IGNORECASE)))


# ---------------------------------------------------------------------------
# PostgreSQL family
# ---------------------------------------------------------------------------

def _collect_postgres(cur: Any, n: int, rank_by: str) -> list[QueryEntry]:
    order_col = _RANK_COLS.get(rank_by, _RANK_COLS["total_time"])["postgres"]
    try:
        cur.execute(f"""
            SELECT
                queryid::text,
                query,
                calls,
                total_exec_time,
                mean_exec_time
            FROM pg_stat_statements
            ORDER BY {order_col} DESC
            LIMIT %s
        """, (n,))
    except Exception:
        # Extension not installed or no SELECT privilege
        return []

    entries = []
    for row in cur.fetchall():
        qid, sql, calls, total_ms, mean_ms = row
        entries.append(QueryEntry(
            id=str(qid) if qid else _short_id(sql),
            source_dialect="postgres",
            sql=sql.strip(),
            tables=_extract_tables(sql),
            calls=int(calls or 0),
            total_elapsed_ms=float(total_ms or 0),
            mean_elapsed_ms=float(mean_ms or 0),
            rank_by=rank_by,
        ))
    return entries


# ---------------------------------------------------------------------------
# MySQL / MariaDB
# ---------------------------------------------------------------------------

def _collect_mysql(cur: Any, n: int, rank_by: str, catalog: str | None) -> list[QueryEntry]:
    order_col = _RANK_COLS.get(rank_by, _RANK_COLS["total_time"])["mysql"]
    where = "WHERE SCHEMA_NAME = %s" if catalog else "WHERE SCHEMA_NAME IS NOT NULL"
    params: tuple = (catalog, n) if catalog else (n,)
    try:
        cur.execute(f"""
            SELECT
                COALESCE(DIGEST, MD5(DIGEST_TEXT)) AS id,
                DIGEST_TEXT,
                COUNT_STAR,
                SUM_TIMER_WAIT  / 1e6,
                AVG_TIMER_WAIT  / 1e6
            FROM performance_schema.events_statements_summary_by_digest
            {where}
            ORDER BY {order_col} DESC
            LIMIT %s
        """, params)
    except Exception:
        return []

    entries = []
    for row in cur.fetchall():
        qid, sql, calls, total_ms, mean_ms = row
        if sql is None:
            continue
        entries.append(QueryEntry(
            id=str(qid) if qid else _short_id(sql),
            source_dialect="mysql",
            sql=sql.strip(),
            tables=_extract_tables(sql),
            calls=int(calls or 0),
            total_elapsed_ms=float(total_ms or 0),
            mean_elapsed_ms=float(mean_ms or 0),
            rank_by=rank_by,
        ))
    return entries


# ---------------------------------------------------------------------------
# SQL Server
# ---------------------------------------------------------------------------

def _collect_sqlserver(cur: Any, n: int, rank_by: str) -> list[QueryEntry]:
    order_col = _RANK_COLS.get(rank_by, _RANK_COLS["total_time"])["sqlserver"]
    try:
        cur.execute(f"""
            SELECT TOP {n}
                CONVERT(varchar(24), qs.sql_handle, 1) AS id,
                SUBSTRING(
                    qt.text,
                    (qs.statement_start_offset / 2) + 1,
                    ((CASE qs.statement_end_offset
                          WHEN -1 THEN DATALENGTH(qt.text)
                          ELSE qs.statement_end_offset END
                      - qs.statement_start_offset) / 2) + 1
                ) AS query_text,
                qs.execution_count,
                qs.total_elapsed_time / 1000.0,
                qs.total_elapsed_time / NULLIF(qs.execution_count, 0) / 1000.0
            FROM sys.dm_exec_query_stats qs
            CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) qt
            ORDER BY {order_col} DESC
        """)
    except Exception:
        return []

    entries = []
    for row in cur.fetchall():
        qid, sql, calls, total_ms, mean_ms = row
        if sql is None:
            continue
        entries.append(QueryEntry(
            id=str(qid) if qid else _short_id(sql),
            source_dialect="tsql",
            sql=sql.strip(),
            tables=_extract_tables(sql),
            calls=int(calls or 0),
            total_elapsed_ms=float(total_ms or 0),
            mean_elapsed_ms=float(mean_ms or 0),
            rank_by=rank_by,
        ))
    return entries


# ---------------------------------------------------------------------------
# Oracle
# ---------------------------------------------------------------------------

def _collect_oracle(cur: Any, n: int, rank_by: str) -> list[QueryEntry]:
    order_col = _RANK_COLS.get(rank_by, _RANK_COLS["total_time"])["oracle"]
    try:
        cur.execute(f"""
            SELECT sql_id, sql_text, executions,
                   elapsed_time / 1000.0,
                   elapsed_time / NULLIF(executions, 0) / 1000.0
            FROM (
                SELECT sql_id, sql_text, executions, elapsed_time
                FROM v$sql
                WHERE executions > 0
                ORDER BY {order_col} DESC
            )
            WHERE ROWNUM <= :n
        """, {"n": n})
    except Exception:
        return []

    entries = []
    for row in cur.fetchall():
        qid, sql, calls, total_ms, mean_ms = row
        if sql is None:
            continue
        entries.append(QueryEntry(
            id=str(qid) if qid else _short_id(sql),
            source_dialect="oracle",
            sql=str(sql).strip(),
            tables=_extract_tables(str(sql)),
            calls=int(calls or 0),
            total_elapsed_ms=float(total_ms or 0),
            mean_elapsed_ms=float(mean_ms or 0),
            rank_by=rank_by,
        ))
    return entries


# ---------------------------------------------------------------------------
# Databricks
# ---------------------------------------------------------------------------

def _collect_databricks(cur: Any, n: int, rank_by: str) -> list[QueryEntry]:
    try:
        cur.execute(f"""
            SELECT
                statement_id,
                statement_text,
                1            AS calls,
                duration     AS total_elapsed_ms,
                duration     AS mean_elapsed_ms
            FROM system.query.history
            WHERE statement_text IS NOT NULL
            ORDER BY duration DESC
            LIMIT {n}
        """)
    except Exception:
        return []

    entries = []
    for row in cur.fetchall():
        qid, sql, calls, total_ms, mean_ms = row
        if sql is None:
            continue
        entries.append(QueryEntry(
            id=str(qid) if qid else _short_id(sql),
            source_dialect="databricks",
            sql=sql.strip(),
            tables=_extract_tables(sql),
            calls=int(calls or 0),
            total_elapsed_ms=float(total_ms or 0),
            mean_elapsed_ms=float(mean_ms or 0),
            rank_by=rank_by,
        ))
    return entries


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def collect_top_queries(
    conn: Any,
    dialect: str,
    n: int = 50,
    rank_by: str = "total_time",
    catalog: str | None = None,
    schema: str | None = None,
) -> QueryWorkload:
    """Collect the top-N queries from a live database.

    Parameters
    ----------
    conn
        An open DB-API 2.0 connection.
    dialect
        One of: postgres, mysql, mariadb, sqlserver, oracle, databricks,
        neon, cockroachdb, lakebase.
    n
        Number of queries to return (default: 50).
    rank_by
        Ranking metric: ``"total_time"`` (default), ``"calls"``, ``"mean_time"``.
    catalog
        Database / catalog name — used by MySQL to filter to the right schema.
    schema
        Schema name (unused by most backends, reserved for future use).

    Returns
    -------
    QueryWorkload
        Contains the collected entries sorted by the chosen metric descending.
        Returns an empty workload if the required catalog view is unavailable.
    """
    d = dialect.lower()
    cur = conn.cursor()

    if d in _PG_FAMILY:
        entries = _collect_postgres(cur, n, rank_by)
        source_dialect = "postgres"
    elif d in _MYSQL_FAMILY:
        entries = _collect_mysql(cur, n, rank_by, catalog)
        source_dialect = "mysql"
    elif d == "sqlserver":
        entries = _collect_sqlserver(cur, n, rank_by)
        source_dialect = "tsql"
    elif d == "oracle":
        entries = _collect_oracle(cur, n, rank_by)
        source_dialect = "oracle"
    elif d == "databricks":
        entries = _collect_databricks(cur, n, rank_by)
        source_dialect = "databricks"
    else:
        entries = []
        source_dialect = d

    return QueryWorkload(source_dialect=source_dialect, queries=entries)
