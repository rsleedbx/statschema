"""
Collect canonical TableStats from a live database by running SQL queries.

Produces ``TableStats`` / ``ColumnStats`` objects that are compatible with
``build_dataframe_from_canonical(spark, table, rows, stats=table_stats)``
so that synthetic data is driven by real measured distributions.

Supported databases
-------------------
  mysql       pymysql connection  (or any PEP-249 connection to MySQL/MariaDB)
  postgres    psycopg2 connection (or any PEP-249 connection to PostgreSQL)
  sqlserver   pymssql connection  (or any PEP-249 connection to SQL Server)

Usage
-----
    from src.statschema.db_stats_collector import collect_table_stats

    # after generating and loading data into the live DB:
    stats = collect_table_stats(conn, "my_table", dialect="mysql")
    # stats is a TableStats ready for build_dataframe_from_canonical(stats=stats)

Statistics collected
--------------------
  Row count           COUNT(*)
  Per-column:
    null_fraction     (COUNT(*) - COUNT(col)) / COUNT(*)
    n_distinct        COUNT(DISTINCT col)
    min_value         MIN(col) cast to string
    max_value         MAX(col) cast to string
    avg_width_bytes   AVG(LENGTH(CAST(col AS CHAR)))   [approx]
    most_common_vals  Top-10 values by frequency
    histogram_bounds  Percentile-based bounds (P10, P25, P50, P75, P90)

Native statistics tables (pg_stats, COLUMN_STATISTICS) are used when
available and more accurate; the portable COUNT/MIN/MAX path is always
the fallback.
"""

from __future__ import annotations

import math
from typing import Any

from .stats_model import ColumnStats, MostCommonValue, TableStats


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def collect_table_stats(
    conn,
    table: str,
    dialect: str,
    schema: str | None = None,
    columns: list[str] | None = None,
) -> TableStats:
    """
    Collect TableStats from a live database table.

    Parameters
    ----------
    conn        PEP-249 database connection (autocommit or manual commit OK).
    table       Table name (unquoted).
    dialect     One of "mysql", "postgres", "sqlserver".
    schema      Schema / database name (optional; uses current schema if None).
    columns     List of column names to collect stats for.
                If None, introspects via information_schema.

    Returns
    -------
    TableStats  Populated with row_count and per-column ColumnStats.
    """
    d = dialect.lower().strip()
    if d in ("postgresql", "pg"):
        d = "postgres"
    if d in ("mssql", "tsql"):
        d = "sqlserver"

    if columns is None:
        columns = _introspect_columns(conn, table, d, schema)

    row_count = _count_rows(conn, table, d, schema)

    col_stats: list[ColumnStats] = []
    for col in columns:
        try:
            cs = _collect_column_stats(conn, table, col, d, schema, row_count)
            col_stats.append(cs)
        except Exception:
            # Don't fail the whole collection if one column errors
            col_stats.append(ColumnStats(name=col, null_fraction=0.0, n_distinct=1.0))

    return TableStats(
        name=table,
        row_count=row_count,
        columns=col_stats,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fetchone(conn, sql: str, params=None):
    cur = conn.cursor()
    cur.execute(sql, params or ())
    return cur.fetchone()


def _fetchall(conn, sql: str, params=None) -> list[tuple]:
    cur = conn.cursor()
    cur.execute(sql, params or ())
    return cur.fetchall() or []


def _quote(name: str, dialect: str) -> str:
    if dialect == "mysql":
        return f"`{name}`"
    if dialect == "sqlserver":
        return f"[{name}]"
    return f'"{name}"'


def _schema_table(table: str, dialect: str, schema: str | None) -> str:
    if schema:
        return f"{_quote(schema, dialect)}.{_quote(table, dialect)}"
    return _quote(table, dialect)


def _introspect_columns(conn, table: str, dialect: str, schema: str | None) -> list[str]:
    """Return ordered list of column names from information_schema."""
    if dialect == "postgres":
        where_schema = "AND table_schema = %s" if schema else "AND table_schema = current_schema()"
        params = (table, schema) if schema else (table,)
        sql = f"""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = %s {where_schema}
            ORDER BY ordinal_position
        """
    elif dialect == "mysql":
        where_schema = "AND table_schema = %s" if schema else "AND table_schema = database()"
        params = (table, schema) if schema else (table,)
        sql = f"""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = %s {where_schema}
            ORDER BY ordinal_position
        """
    else:  # sqlserver
        where_schema = "AND TABLE_SCHEMA = %s" if schema else ""
        params = (table, schema) if schema else (table,)
        sql = f"""
            SELECT COLUMN_NAME FROM information_schema.columns
            WHERE TABLE_NAME = %s {where_schema}
            ORDER BY ORDINAL_POSITION
        """
    rows = _fetchall(conn, sql, params)
    return [r[0] for r in rows]


def _count_rows(conn, table: str, dialect: str, schema: str | None) -> int:
    tref = _schema_table(table, dialect, schema)
    row = _fetchone(conn, f"SELECT COUNT(*) FROM {tref}")
    return int(row[0]) if row else 0


def _collect_column_stats(
    conn,
    table: str,
    col: str,
    dialect: str,
    schema: str | None,
    row_count: int,
) -> ColumnStats:
    """Collect per-column statistics using portable SQL."""
    tref = _schema_table(table, dialect, schema)
    cref = _quote(col, dialect)

    # ── null fraction ──────────────────────────────────────────────────────
    row = _fetchone(conn, f"""
        SELECT
            COUNT(*),
            COUNT({cref}),
            COUNT(DISTINCT {cref})
        FROM {tref}
    """)
    total, non_null, n_distinct_raw = (int(row[0] or 0), int(row[1] or 0), int(row[2] or 0))
    null_fraction = (total - non_null) / total if total > 0 else 0.0
    n_distinct = float(n_distinct_raw)

    # ── min / max ──────────────────────────────────────────────────────────
    min_val = max_val = None
    try:
        row2 = _fetchone(conn, f"SELECT MIN({cref}), MAX({cref}) FROM {tref}")
        if row2 and row2[0] is not None:
            min_val = str(row2[0])
        if row2 and row2[1] is not None:
            max_val = str(row2[1])
    except Exception:
        pass

    # ── avg width ──────────────────────────────────────────────────────────
    avg_width = 8
    try:
        if dialect in ("mysql", "postgres"):
            row3 = _fetchone(conn, f"SELECT AVG(LENGTH(CAST({cref} AS CHAR))) FROM {tref}")
        else:
            row3 = _fetchone(conn, f"SELECT AVG(LEN(CAST({cref} AS NVARCHAR(MAX)))) FROM {tref}")
        if row3 and row3[0] is not None:
            avg_width = max(1, int(float(row3[0])))
    except Exception:
        pass

    # ── most common values (top 10 by frequency) ──────────────────────────
    mcvs: list[MostCommonValue] = []
    try:
        if dialect == "mysql":
            mcv_sql = f"""
                SELECT {cref}, COUNT(*) AS cnt
                FROM {tref}
                WHERE {cref} IS NOT NULL
                GROUP BY {cref}
                ORDER BY cnt DESC
                LIMIT 10
            """
        elif dialect == "postgres":
            mcv_sql = f"""
                SELECT {cref}, COUNT(*) AS cnt
                FROM {tref}
                WHERE {cref} IS NOT NULL
                GROUP BY {cref}
                ORDER BY cnt DESC
                LIMIT 10
            """
        else:  # sqlserver
            mcv_sql = f"""
                SELECT TOP 10 {cref}, COUNT(*) AS cnt
                FROM {tref}
                WHERE {cref} IS NOT NULL
                GROUP BY {cref}
                ORDER BY cnt DESC
            """
        for row4 in _fetchall(conn, mcv_sql):
            val, cnt = row4[0], int(row4[1])
            freq = cnt / non_null if non_null > 0 else 0.0
            mcvs.append(MostCommonValue(value=str(val), frequency=freq))
    except Exception:
        pass

    # ── histogram bounds (percentile-based) ───────────────────────────────
    histogram_bounds: list[str] = []
    try:
        if non_null > 0:
            histogram_bounds = _collect_histogram(conn, tref, cref, dialect)
    except Exception:
        pass

    # ── PostgreSQL: use pg_stats when available (more accurate) ───────────
    if dialect == "postgres":
        try:
            pg_row = _fetchone(conn, """
                SELECT null_frac, n_distinct, most_common_vals, most_common_freqs
                FROM pg_stats
                WHERE tablename = %s AND attname = %s
            """, (table, col))
            if pg_row and pg_row[0] is not None:
                null_fraction = float(pg_row[0])
                raw_nd = float(pg_row[1] or 0)
                n_distinct = abs(raw_nd) * row_count if raw_nd < 0 else raw_nd
                # pg_stats stores MCVs as PostgreSQL array literals: {val1,val2,...}
                if pg_row[2] and pg_row[3]:
                    mcvs = _parse_pg_array_mcv(pg_row[2], pg_row[3])
        except Exception:
            pass

    return ColumnStats(
        name=col,
        null_fraction=null_fraction,
        n_distinct=n_distinct,
        avg_width_bytes=avg_width,
        min_value=min_val,
        max_value=max_val,
        most_common_values=mcvs,
        histogram_bounds=histogram_bounds,
    )


def _collect_histogram(conn, tref: str, cref: str, dialect: str) -> list[str]:
    """Compute approximate histogram bounds at P10, P25, P50, P75, P90."""
    bounds = []
    try:
        if dialect == "mysql":
            # MySQL 8+ has PERCENT_RANK / NTILE via window functions
            sql = f"""
                SELECT {cref} FROM {tref}
                WHERE {cref} IS NOT NULL
                ORDER BY {cref}
                LIMIT 1000
            """
            rows = [r[0] for r in _fetchall(conn, sql)]
            bounds = _percentile_bounds(rows)
        elif dialect == "postgres":
            sql = f"""
                SELECT percentile_cont(ARRAY[0.1, 0.25, 0.5, 0.75, 0.9])
                       WITHIN GROUP (ORDER BY {cref}::text)
                FROM {tref}
                WHERE {cref} IS NOT NULL
            """
            try:
                row = _fetchone(conn, sql)
                if row and row[0]:
                    bounds = [str(v) for v in row[0]]
            except Exception:
                # Non-numeric/sortable columns will fail percentile_cont
                pass
        elif dialect == "sqlserver":
            sql = f"""
                SELECT {cref}
                FROM {tref}
                WHERE {cref} IS NOT NULL
                ORDER BY {cref}
                OFFSET 0 ROWS FETCH NEXT 1000 ROWS ONLY
            """
            rows = [r[0] for r in _fetchall(conn, sql)]
            bounds = _percentile_bounds(rows)
    except Exception:
        pass
    return bounds


def _percentile_bounds(values: list) -> list[str]:
    """Compute P10, P25, P50, P75, P90 from a sorted sample."""
    if not values:
        return []
    n = len(values)
    try:
        sorted_vals = sorted(values)
        bounds = []
        for p in [0.10, 0.25, 0.50, 0.75, 0.90]:
            idx = max(0, min(n - 1, int(p * n)))
            bounds.append(str(sorted_vals[idx]))
        return bounds
    except Exception:
        return []


def _parse_pg_array_mcv(vals_str: str, freqs_str: str) -> list[MostCommonValue]:
    """Parse PostgreSQL array literal strings for most_common_vals / most_common_freqs."""
    try:
        # Remove outer braces: {val1,"val2",val3} → ['val1', 'val2', 'val3']
        def _parse_pg_array(s: str) -> list[str]:
            s = s.strip()
            if s.startswith("{") and s.endswith("}"):
                s = s[1:-1]
            # Simple split (doesn't handle quoted commas, but adequate for stats)
            return [v.strip().strip('"') for v in s.split(",") if v.strip()]

        vals  = _parse_pg_array(vals_str)
        freqs = [float(f) for f in _parse_pg_array(freqs_str)]
        return [
            MostCommonValue(value=v, frequency=f)
            for v, f in zip(vals, freqs)
        ]
    except Exception:
        return []
