"""
Collect canonical TableStats from a live database by running SQL queries.

Produces ``TableStats`` / ``ColumnStats`` objects that are compatible with
``build_dataframe_from_canonical(spark, table, rows, stats=table_stats)``
so that synthetic data is driven by real measured distributions.

Supported databases
-------------------
  mysql       pymysql connection  (or any PEP-249 connection to MySQL / MariaDB)
  postgres    psycopg2 connection (or any PEP-249 connection to PostgreSQL / Neon / CockroachDB)
  sqlserver   pymssql connection  (or any PEP-249 connection to SQL Server)
  oracle      oracledb connection

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

from .dialect_registry import normalize_dialect
from .stats_model import ColumnStats, MostCommonValue, TableStats


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def collect_table_stats(  # pragma: no cover
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
    dialect     One of "mysql", "postgres" (or "neon" / "neondb"), "sqlserver".
    schema      Schema / database name (optional; uses current schema if None).
    columns     List of column names to collect stats for.
                If None, introspects via information_schema.

    Returns
    -------
    TableStats  Populated with row_count and per-column ColumnStats.
    """
    d = normalize_dialect(dialect)

    if columns is None:
        columns = _introspect_columns(conn, table, d, schema)

    row_count = _count_rows(conn, table, d, schema)

    indexed_cols = _indexed_columns(conn, table, d, schema)

    col_stats: list[ColumnStats] = []
    for col in columns:
        try:
            cs = _collect_column_stats(conn, table, col, d, schema, row_count, indexed_cols)
            col_stats.append(cs)
        except Exception:
            _safe_rollback(conn)
            col_stats.append(ColumnStats(name=col, null_fraction=0.0, n_distinct=1.0))

    # Extended (multi-column) statistics — PostgreSQL only
    composite: list = []
    if d == "postgres":
        try:
            composite = _collect_pg_composite_stats(conn, table, schema)
        except Exception:
            _safe_rollback(conn)

    return TableStats(
        name=table,
        row_count=row_count,
        columns=col_stats,
        composite_stats=composite,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _needs_qmark(conn) -> bool:
    """Return True when the connection requires ? placeholders instead of %s.

    mssql_python declares paramstyle='pyformat' but only binds ? at the TDS
    layer — %s is forwarded literally to SQL Server and causes a syntax error.
    """
    return type(conn).__module__.split(".")[0] == "mssql_python"


def _fetchone(conn, sql: str, params=None):  # pragma: no cover
    cur = conn.cursor()
    if params and _needs_qmark(conn):
        sql = sql.replace("%s", "?")
    cur.execute(sql, params or ())
    return cur.fetchone()


def _fetchall(conn, sql: str, params=None) -> list[tuple]:  # pragma: no cover
    cur = conn.cursor()
    if params and _needs_qmark(conn):
        sql = sql.replace("%s", "?")
    cur.execute(sql, params or ())
    return cur.fetchall() or []


def _safe_rollback(conn) -> None:  # pragma: no cover
    """Roll back any aborted transaction without raising.

    Called in ``except`` blocks after SQL failures so that subsequent
    queries on the same connection are not poisoned by the aborted state.
    """
    try:
        conn.rollback()
    except Exception:
        pass


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


def _introspect_columns(conn, table: str, dialect: str, schema: str | None) -> list[str]:  # pragma: no cover
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


def _count_rows(conn, table: str, dialect: str, schema: str | None) -> int:  # pragma: no cover
    tref = _schema_table(table, dialect, schema)
    row = _fetchone(conn, f"SELECT COUNT(*) FROM {tref}")
    return int(row[0]) if row else 0


def _fetch_min_max(  # pragma: no cover
    conn,
    tref: str,
    cref: str,
    dialect: str,
) -> tuple[str | None, str | None]:
    """Return (min_val, max_val) as strings for an indexed column.

    Primary path: ``SELECT MIN(col), MAX(col)``.  Some types don't support
    the aggregate (SQL Server ``uniqueidentifier``, PostgreSQL ``json``/
    ``bytea``/arrays) — those raise an error that aborts the transaction.
    Fallback: two ``ORDER BY col LIMIT 1`` queries, which use the index and
    work for any orderable type.  If both paths fail (e.g. non-orderable
    PostgreSQL json), returns ``(None, None)`` without leaving the connection
    in an aborted state.
    """
    # Primary: single-pass aggregate (fastest when supported)
    try:
        row = _fetchone(conn, f"SELECT MIN({cref}), MAX({cref}) FROM {tref}")
        lo = str(row[0]) if row and row[0] is not None else None
        hi = str(row[1]) if row and row[1] is not None else None
        return lo, hi
    except Exception:
        _safe_rollback(conn)

    # Fallback: ORDER BY index scan — works for UUID, uniqueidentifier, etc.
    lo = hi = None
    if dialect == "sqlserver":
        lo_sql = f"SELECT TOP 1 {cref} FROM {tref} WHERE {cref} IS NOT NULL ORDER BY {cref} ASC"
        hi_sql = f"SELECT TOP 1 {cref} FROM {tref} WHERE {cref} IS NOT NULL ORDER BY {cref} DESC"
    elif dialect == "oracle":
        lo_sql = f"SELECT {cref} FROM {tref} WHERE {cref} IS NOT NULL ORDER BY {cref} ASC FETCH FIRST 1 ROW ONLY"
        hi_sql = f"SELECT {cref} FROM {tref} WHERE {cref} IS NOT NULL ORDER BY {cref} DESC FETCH FIRST 1 ROW ONLY"
    else:  # postgres / mysql / mariadb
        lo_sql = f"SELECT {cref} FROM {tref} WHERE {cref} IS NOT NULL ORDER BY {cref} ASC LIMIT 1"
        hi_sql = f"SELECT {cref} FROM {tref} WHERE {cref} IS NOT NULL ORDER BY {cref} DESC LIMIT 1"
    try:
        row = _fetchone(conn, lo_sql)
        if row and row[0] is not None:
            lo = str(row[0])
    except Exception:
        _safe_rollback(conn)
        return None, None   # non-orderable type (json, xml, etc.) — give up
    try:
        row = _fetchone(conn, hi_sql)
        if row and row[0] is not None:
            hi = str(row[0])
    except Exception:
        _safe_rollback(conn)
    return lo, hi


def _indexed_columns(  # pragma: no cover
    conn,
    table: str,
    dialect: str,
    schema: str | None,
) -> set[str]:
    """Return the set of column names that participate in at least one index.

    One catalog query per table.  Columns in this set are safe for a
    ``SELECT MIN(col), MAX(col)`` because the planner will use an index scan.
    Columns outside this set are skipped to avoid full sequential scans.
    """
    try:
        if dialect == "postgres":
            where_schema = "AND n.nspname = %s" if schema else ""
            params = (table, schema) if schema else (table,)
            rows = _fetchall(conn, f"""
                SELECT DISTINCT a.attname
                FROM   pg_index     i
                JOIN   pg_attribute a ON a.attrelid = i.indrelid
                                     AND a.attnum   = ANY(i.indkey)
                JOIN   pg_class     c ON c.oid = i.indrelid
                JOIN   pg_namespace n ON n.oid = c.relnamespace
                WHERE  c.relname = %s {where_schema}
            """, params)
        elif dialect == "mysql":
            where_schema = "AND TABLE_SCHEMA = %s" if schema else "AND TABLE_SCHEMA = DATABASE()"
            params = (table, schema) if schema else (table,)
            rows = _fetchall(conn, f"""
                SELECT DISTINCT COLUMN_NAME
                FROM   information_schema.STATISTICS
                WHERE  TABLE_NAME = %s {where_schema}
            """, params)
        elif dialect == "sqlserver":
            where_schema = "AND s.name = %s" if schema else ""
            params = (table, schema) if schema else (table,)
            rows = _fetchall(conn, f"""
                SELECT DISTINCT c.name
                FROM   sys.index_columns ic
                JOIN   sys.columns       c  ON c.object_id  = ic.object_id
                                           AND c.column_id  = ic.column_id
                JOIN   sys.objects       o  ON o.object_id  = ic.object_id
                JOIN   sys.schemas       s  ON s.schema_id  = o.schema_id
                WHERE  o.name = %s {where_schema}
            """, params)
        elif dialect == "oracle":
            where_owner = "AND ic.table_owner = UPPER(%s)" if schema else ""
            params = (table, schema) if schema else (table,)
            rows = _fetchall(conn, f"""
                SELECT DISTINCT ic.column_name
                FROM   all_ind_columns ic
                WHERE  ic.table_name = UPPER(%s) {where_owner}
            """, params)
        else:
            return set()
        return {r[0] for r in rows}
    except Exception:
        _safe_rollback(conn)
        return set()


def _collect_column_stats(  # pragma: no cover
    conn,
    table: str,
    col: str,
    dialect: str,
    schema: str | None,
    row_count: int,
    indexed_cols: set[str] | None = None,
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
    # Only run MIN/MAX for indexed columns.  On large tables a full sequential
    # scan for every non-indexed column would dominate collection time.
    # Non-indexed PostgreSQL columns get approximate bounds from histogram_bounds
    # (filled in below from pg_stats); other dialects leave min/max as None.
    min_val = max_val = None
    if indexed_cols is None or col in indexed_cols:
        min_val, max_val = _fetch_min_max(conn, tref, cref, dialect)

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
        _safe_rollback(conn)

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
        _safe_rollback(conn)

    # ── histogram bounds (percentile-based fallback) ──────────────────────
    # Skipped for postgres: pg_stats already holds authoritative histogram_bounds
    # from ANALYZE, and the percentile_cont query can fail on certain column
    # types, leaving the connection in an aborted-transaction state.
    histogram_bounds: list[str] = []
    try:
        if non_null > 0 and dialect != "postgres":
            histogram_bounds = _collect_histogram(conn, tref, cref, dialect)
    except Exception:
        _safe_rollback(conn)

    # ── correlation (physical sort order) — PostgreSQL only ───────────────
    correlation: float | None = None

    # ── PostgreSQL: use pg_stats when available (authoritative) ───────────
    # pg_stats reflects the last ANALYZE run; covers histogram_bounds and
    # correlation that our generic SQL approximations cannot replicate.
    if dialect == "postgres":
        try:
            where_schema = "AND schemaname = %s" if schema else ""
            params = (table, col, schema) if schema else (table, col)
            pg_row = _fetchone(conn, f"""
                SELECT null_frac, n_distinct, most_common_vals, most_common_freqs,
                       histogram_bounds, correlation, avg_width
                FROM pg_stats
                WHERE tablename = %s AND attname = %s {where_schema}
            """, params)
            if pg_row and pg_row[0] is not None:
                null_fraction = float(pg_row[0])
                raw_nd = float(pg_row[1] or 0)
                n_distinct = abs(raw_nd) * row_count if raw_nd < 0 else raw_nd
                if pg_row[2] and pg_row[3]:
                    mcvs = _parse_pg_array_mcv(pg_row[2], pg_row[3])
                # histogram_bounds from pg_stats supersedes percentile approximation
                if pg_row[4]:
                    histogram_bounds = _parse_pg_array_str(pg_row[4])
                    # For non-indexed columns where we skipped MIN/MAX, derive
                    # approximate bounds from the first/last histogram bucket.
                    if min_val is None and histogram_bounds:
                        min_val = histogram_bounds[0]
                    if max_val is None and histogram_bounds:
                        max_val = histogram_bounds[-1]
                if pg_row[5] is not None:
                    correlation = float(pg_row[5])
                # avg_width from pg_stats is the real byte width; use it instead of
                # the AVG(LENGTH(CAST(col AS CHAR))) approximation, which always
                # returns 1 in PostgreSQL because bare CHAR has length 1.
                if pg_row[6] is not None:
                    avg_width = int(pg_row[6])
        except Exception:
            _safe_rollback(conn)

    return ColumnStats(
        name=col,
        null_fraction=null_fraction,
        n_distinct=n_distinct,
        avg_width_bytes=avg_width,
        min_value=min_val,
        max_value=max_val,
        most_common_values=mcvs,
        histogram_bounds=histogram_bounds,
        correlation=correlation,
    )


def _collect_histogram(conn, tref: str, cref: str, dialect: str) -> list[str]:  # pragma: no cover
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
                _safe_rollback(conn)
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
        _safe_rollback(conn)
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


def _parse_pg_array_str(s: str) -> list[str]:
    """Parse a PostgreSQL text-array literal such as ``{a,b,c}`` into a list of strings."""
    s = s.strip()
    if s.startswith("{") and s.endswith("}"):
        s = s[1:-1]
    return [v.strip().strip('"') for v in s.split(",") if v.strip()]


def _parse_pg_array_mcv(vals_str, freqs_str) -> list[MostCommonValue]:
    """Parse pg_stats most_common_vals / most_common_freqs into MostCommonValue list.

    psycopg2 returns typed arrays (e.g. ``real[]``) as Python lists but returns
    ``anyarray`` columns (most_common_vals) as string literals like ``{R,A,N}``.
    Both representations are accepted here.
    """
    try:
        vals = (
            _parse_pg_array_str(vals_str)
            if isinstance(vals_str, str)
            else [str(v) for v in vals_str]
        )
        freqs = (
            [float(f) for f in freqs_str]
            if isinstance(freqs_str, list)
            else [float(f) for f in _parse_pg_array_str(freqs_str)]
        )
        return [MostCommonValue(value=v, frequency=f) for v, f in zip(vals, freqs)]
    except Exception:
        return []


def _collect_pg_composite_stats(  # pragma: no cover
    conn,
    table: str,
    schema: str | None,
) -> list:
    """
    Collect extended (multi-column) statistics from pg_stats_ext for one table.

    These capture column dependencies and combined n_distinct, which the optimizer
    uses for GROUP BY and multi-predicate cardinality estimation.  Without them, PG
    falls back to multiplying individual n_distinct values and badly overestimates
    the number of groups produced by GROUP BY on correlated columns.

    Requires PostgreSQL 10+ and at least one CREATE STATISTICS object on the table.
    Returns a list of CompositeColumnStats (empty when none exist).
    """
    from src.statschema.stats_model import CompositeColumnStats, MCVCombination

    results: list[CompositeColumnStats] = []
    try:
        where_schema = "AND s.schemaname = %s" if schema else ""
        params = (table, schema) if schema else (table,)
        rows = _fetchall(conn, f"""
            SELECT
                s.stxkeys::text,
                sd.stxdndistinct,
                sd.stxddependencies,
                sd.stxdmcv
            FROM pg_statistic_ext     s
            JOIN pg_statistic_ext_data sd ON sd.stxoid = s.oid
            JOIN pg_class              c  ON c.oid = s.stxrelid
            WHERE c.relname = %s {where_schema}
        """, params)

        for row in rows:
            col_nums_str, nd_json, dep_json, mcv_json = row

            # Resolve column numbers → names
            col_names: list[str] = []
            try:
                col_nums = [int(x) for x in col_nums_str.strip("{}").split()]
                name_rows = _fetchall(conn, """
                    SELECT attnum, attname FROM pg_attribute
                    WHERE attrelid = (
                        SELECT c.oid FROM pg_class c
                        JOIN pg_namespace n ON n.oid = c.relnamespace
                        WHERE c.relname = %s
                    )
                    AND attnum = ANY(%s)
                    ORDER BY attnum
                """, (table, col_nums))
                col_names = [r[1] for r in name_rows]
            except Exception:
                continue

            if len(col_names) < 2:
                continue

            # n_distinct from stxdndistinct (JSON: {"3, 4": -0.12, ...})
            n_distinct: int | None = None
            if nd_json:
                try:
                    import json as _json
                    nd_map = _json.loads(nd_json) if isinstance(nd_json, str) else nd_json
                    # Take the entry covering all columns in this stat
                    key = ", ".join(str(n) for n in sorted(
                        [r[0] for r in _fetchall(conn, """
                            SELECT attnum FROM pg_attribute
                            WHERE attrelid = (SELECT oid FROM pg_class WHERE relname = %s)
                            AND attname = ANY(%s)
                        """, (table, col_names))]
                    ))
                    val = nd_map.get(key)
                    if val is not None:
                        n_distinct = int(abs(float(val))) if float(val) < 0 else int(val)
                except Exception:
                    pass

            # functional dependencies from stxddependencies
            dependencies: dict[str, float] = {}
            if dep_json:
                try:
                    import json as _json
                    dep_map = _json.loads(dep_json) if isinstance(dep_json, str) else dep_json
                    # keys like "3 => 4" map to column-name pairs
                    for k, v in dep_map.items():
                        parts = k.split(" => ")
                        if len(parts) == 2:
                            dependencies[f"{parts[0].strip()} => {parts[1].strip()}"] = float(v)
                except Exception:
                    pass

            results.append(CompositeColumnStats(
                columns=col_names,
                n_distinct=n_distinct,
                dependencies=dependencies,
            ))
    except Exception:
        _safe_rollback(conn)

    return results
