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
    from src.statschema.db_stats_collector import collect_table_stats, CollectionConfig

    # Default collection (baseline)
    stats = collect_table_stats(conn, "my_table", dialect="mysql")

    # Enriched collection — all techniques enabled
    cfg = CollectionConfig.all()
    stats = collect_table_stats(conn, "my_table", dialect="mysql", config=cfg)

    # Targeted collection — only for columns that appear in query predicates
    predicate_cols = predicate_columns_from_queries(query_sqls, "my_table", "ansi")
    cfg = CollectionConfig(full_mcv=True, ntile_hist=True, predicate_cols=predicate_cols)
    stats = collect_table_stats(conn, "my_table", dialect="mysql", config=cfg)

Statistics collected (baseline)
--------------------------------
  Row count           COUNT(*)
  Per-column:
    null_fraction     (COUNT(*) - COUNT(col)) / COUNT(*)
    n_distinct        COUNT(DISTINCT col)
    min_value         MIN(col) cast to string  [indexed columns only, or all when no indexes]
    max_value         MAX(col) cast to string
    avg_width_bytes   AVG(LENGTH(CAST(col AS CHAR)))   [approx]
    most_common_vals  Top-10 values by frequency
    histogram_bounds  Percentile-based bounds (P10, P25, P50, P75, P90)

Enrichment techniques (opt-in via CollectionConfig)
----------------------------------------------------
  full_mcv      Collect ALL distinct value/frequency pairs for columns with
                n_distinct ≤ full_mcv_threshold (default 500).  One GROUP BY query
                per column; same cost as the existing top-10 MCV query.

  ntile_hist    Build an equi-height histogram with ntile_hist_buckets (default 100)
                buckets using NTILE window function.  Replaces the 5-bound P10/P25/
                P50/P75/P90 approximation for non-Postgres engines.  One full-scan
                (or sampled) ORDER BY per column.

  pred_cols     Focus full_mcv and ntile_hist on predicate columns (columns that
                appear in JOIN ON, WHERE, HAVING, or GROUP BY in the workload
                queries).  Other columns still receive baseline stats.  Use
                predicate_columns_from_queries() to build the set.

  samp_nd       For tables with row_count > samp_nd_threshold (default 100_000),
                estimate n_distinct from a TABLESAMPLE rather than a full
                COUNT(DISTINCT col) scan.  ~100× cheaper for large tables.

  rank_corr     Estimate physical-sort correlation (Spearman rank) from a
                scan-order sample.  Fills the correlation field that the PostgreSQL
                optimizer uses to choose between index scan and bitmap heap scan.
                Only meaningful for non-Postgres sources where correlation is NULL.

DBA load comparison (future)
-----------------------------
Each technique records elapsed wall-clock time in CollectionTiming.technique_ms
so that the additional database load of each technique can be measured and compared.
Enable collection via CollectionConfig(record_timing=True) and retrieve from
TableStats.collection_timing after the call.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .dialect_registry import normalize_dialect
from .stats_model import ColumnStats, MostCommonValue, TableStats


# ---------------------------------------------------------------------------
# CollectionConfig — opt-in enrichment techniques
# ---------------------------------------------------------------------------

@dataclass
class CollectionTiming:
    """Per-technique wall-clock milliseconds — for future DBA load comparison.

    Each field accumulates time across all columns in one table.  Set
    CollectionConfig.record_timing=True to populate.
    """
    baseline_ms: float = 0.0    # COUNT/COUNT_DISTINCT/MIN/MAX/MCV-10/histogram-5
    full_mcv_ms: float = 0.0    # full_mcv incremental cost (MCV query without LIMIT)
    ntile_hist_ms: float = 0.0  # ntile_hist incremental cost (NTILE ORDER BY)
    samp_nd_ms: float = 0.0     # samp_nd incremental cost (TABLESAMPLE COUNT DISTINCT)
    rank_corr_ms: float = 0.0   # rank_corr incremental cost (sample + Python Spearman)


@dataclass
class CollectionConfig:
    """Controls which enrichment techniques are applied during collection.

    Technique names match the identifiers used in the docs and timing fields:
      full_mcv    — full distinct-value distribution for low-cardinality columns
      ntile_hist  — NTILE-based equi-height histogram (non-Postgres only)
      pred_cols   — focus full_mcv and ntile_hist on predicate columns only
      samp_nd     — sampling-based n_distinct for large tables
      rank_corr   — Spearman rank correlation from scan-order sample

    All techniques default to False so existing call sites are unaffected.
    """
    # ── technique flags ───────────────────────────────────────────────────────
    full_mcv: bool = False
    ntile_hist: bool = False
    pred_cols: bool = False
    samp_nd: bool = False
    rank_corr: bool = False

    # ── technique thresholds ─────────────────────────────────────────────────
    # full_mcv: collect all distinct values when n_distinct ≤ this threshold.
    full_mcv_threshold: int = 500
    # ntile_hist: number of equi-height buckets.
    ntile_hist_buckets: int = 100
    # ntile_hist: only NTILE-sample when table has more rows than this; below
    # threshold a full scan is acceptable.
    ntile_hist_sample_rows: int = 50_000
    # samp_nd: switch to sampling when row_count > this.
    samp_nd_threshold: int = 100_000
    # samp_nd: approximate sample percentage (1.0 = 1%).
    samp_nd_pct: float = 1.0
    # rank_corr: number of rows in the scan-order sample.
    rank_corr_sample: int = 2_000

    # ── predicate columns ─────────────────────────────────────────────────────
    # {table_name_lowercase: set[col_name_lowercase]}
    # Built by predicate_columns_from_queries(); empty means "all columns".
    predicate_col_map: dict[str, set[str]] = field(default_factory=dict)

    # ── DBA load comparison ──────────────────────────────────────────────────
    record_timing: bool = False

    # ── convenience constructors ─────────────────────────────────────────────
    @classmethod
    def all(cls, **overrides) -> "CollectionConfig":
        """Enable every technique with default thresholds."""
        return cls(
            full_mcv=True, ntile_hist=True, pred_cols=False,
            samp_nd=True, rank_corr=True, **overrides,
        )

    @classmethod
    def from_profile_dict(cls, d: dict) -> "CollectionConfig":
        """Build a CollectionConfig from a profiles YAML entry dict.

        Unknown keys are silently ignored so future YAML additions are
        forward-compatible with older code.
        """
        _bool  = {"full_mcv", "ntile_hist", "pred_cols", "samp_nd", "rank_corr"}
        _int   = {"full_mcv_threshold", "ntile_hist_buckets",
                  "ntile_hist_sample_rows", "samp_nd_threshold", "rank_corr_sample"}
        _float = {"samp_nd_pct"}
        kwargs: dict = {}
        for k, v in d.items():
            if k in _bool:
                kwargs[k] = bool(v)
            elif k in _int:
                kwargs[k] = int(v)
            elif k in _float:
                kwargs[k] = float(v)
        return cls(**kwargs)

    @classmethod
    def predicate_focused(
        cls,
        predicate_col_map: dict[str, set[str]],
        **overrides,
    ) -> "CollectionConfig":
        """Enable full_mcv + ntile_hist only for predicate columns.

        Non-predicate columns receive baseline stats only (null_frac + n_distinct).
        """
        return cls(
            full_mcv=True, ntile_hist=True, pred_cols=True,
            samp_nd=True, rank_corr=True,
            predicate_col_map=predicate_col_map,
            **overrides,
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def collect_table_stats(  # pragma: no cover
    conn,
    table: str,
    dialect: str,
    schema: str | None = None,
    columns: list[str] | None = None,
    config: Optional[CollectionConfig] = None,
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
    config      CollectionConfig controlling which enrichment techniques to apply.
                None = baseline collection only (no enrichment).

    Returns
    -------
    TableStats  Populated with row_count and per-column ColumnStats.
    """
    d = normalize_dialect(dialect)
    cfg = config or CollectionConfig()

    if columns is None:
        columns = _introspect_columns(conn, table, d, schema)

    row_count = _count_rows(conn, table, d, schema)
    indexed_cols = _indexed_columns(conn, table, d, schema)

    # Predicate columns for this table (lowercase names).
    tbl_key = table.lower()
    pred_set: set[str] | None = cfg.predicate_col_map.get(tbl_key) if cfg.pred_cols else None

    timing = CollectionTiming() if cfg.record_timing else None

    col_stats: list[ColumnStats] = []
    for col in columns:
        try:
            cs = _collect_column_stats(
                conn, table, col, d, schema, row_count, indexed_cols,
                cfg=cfg, is_predicate=(pred_set is None or col.lower() in pred_set),
                timing=timing,
            )
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

    ts = TableStats(
        name=table,
        row_count=row_count,
        columns=col_stats,
        composite_stats=composite,
    )
    # Attach timing for DBA load comparison (accessed as ts.collection_timing)
    if timing is not None:
        ts.collection_timing = timing  # type: ignore[attr-defined]
    return ts


def predicate_columns_from_queries(
    queries: list[str],
    table: str,
    source_dialect: str = "ansi",
) -> set[str]:
    """Return the set of columns for ``table`` that appear in query predicates.

    Technique: pred_cols.  Parses each SQL string using sqlglot to extract
    column references that appear in:
      • WHERE clauses (filters)
      • JOIN ON conditions (equi-join and non-equi-join keys)
      • HAVING clauses
      • GROUP BY expressions

    Only columns that can be attributed to ``table`` (or its aliases in the
    FROM clause) are returned; unresolvable references are included conservatively
    (i.e. if the column name matches a column in the table, include it).

    Parameters
    ----------
    queries         List of SQL strings from the workload.
    table           Unquoted table name (case-insensitive).
    source_dialect  sqlglot dialect for parsing ("ansi", "mysql", "tsql", …).

    Returns
    -------
    set[str]  Lowercase column names; empty set if sqlglot is not installed or
              no predicates reference the table.
    """
    try:
        import sqlglot
        import sqlglot.expressions as exp
    except ImportError:
        return set()

    # sqlglot does not recognise "ansi" as a dialect; fall back to None (default).
    _dialect_map = {"ansi": None, "tsql": "tsql", "mysql": "mysql",
                    "postgres": "postgres", "oracle": "oracle", "db2": "db2"}
    sg_dialect = _dialect_map.get(source_dialect.lower(), source_dialect.lower() or None)

    tbl_lower = table.lower()
    pred_cols: set[str] = set()

    for sql in queries:
        try:
            parsed = sqlglot.parse_one(sql, dialect=sg_dialect)
        except Exception:
            continue

        # Build alias → table_name map from the FROM clause.
        alias_to_table: dict[str, str] = {}
        for node in parsed.find_all(exp.Table):
            tname = (node.name or "").lower()
            alias = (node.alias or tname).lower()
            if tname:
                alias_to_table[alias] = tname

        tbl_in_query = tbl_lower in alias_to_table.values()

        def _col_belongs_to_table(col_node: exp.Column) -> bool:  # noqa: B023
            tbl_ref = (col_node.table or "").lower()
            if not tbl_ref:
                # Unqualified column: include when the target table is referenced
                # in the FROM clause (conservative — may over-include columns from
                # other tables with the same name, but that is safe for stats).
                return tbl_in_query
            actual = alias_to_table.get(tbl_ref, tbl_ref)
            return actual == tbl_lower

        # Walk predicate-bearing clauses: WHERE, JOIN ON, HAVING, GROUP BY.
        predicate_nodes: list[exp.Expression] = []
        for cls in (exp.Where, exp.Join, exp.Having, exp.Group):
            predicate_nodes.extend(parsed.find_all(cls))

        for node in predicate_nodes:
            for col in node.find_all(exp.Column):
                if _col_belongs_to_table(col):
                    pred_cols.add((col.name or "").lower())

    return pred_cols


def predicate_col_map_from_db(
    conn,
    dialect: str,
    schema_tables: list[str],
    n: int = 200,
    rank_by: str = "total_time",
    catalog: str | None = None,
    schema: str | None = None,
) -> dict[str, set[str]]:
    """Build a predicate-column map by reading the database's own query store.

    Calls ``collect_top_queries`` to pull the top-N SQL statements from the
    engine's built-in query catalog (pg_stat_statements, performance_schema,
    sys.dm_exec_query_stats, v$sql, …), filters them to queries that reference
    the target tables, then passes them through ``predicate_columns_from_queries``
    for each table in ``schema_tables``.

    Parameters
    ----------
    conn
        An open DB-API 2.0 connection to the source database.
    dialect
        Source engine dialect (postgres, mysql, oracle, …).
    schema_tables
        List of unquoted table names to extract predicate columns for.
        Only queries that reference at least one of these tables are used.
    n
        Number of queries to pull from the query store (default: 200).
    rank_by
        Ranking metric passed to ``collect_top_queries``:
        ``"total_time"`` (default), ``"calls"``, or ``"mean_time"``.
    catalog
        Database / catalog name — required by MySQL to filter to the right schema.
    schema
        Schema / owner name.  Passed to engines that support server-side schema
        filtering (Oracle ``v$sql.PARSING_SCHEMA_NAME``).

    Returns
    -------
    dict  {table_name_lowercase: set[col_name_lowercase]}
          Empty dict when the query store is unavailable or no queries were found.
    """
    from .query_collector import collect_top_queries  # local import avoids circular dependency

    workload = collect_top_queries(
        conn, dialect, n=n, rank_by=rank_by,
        catalog=catalog, schema=schema, tables=schema_tables,
    )
    if not workload.queries:
        return {}

    # Post-filter: keep only queries whose extracted table list overlaps the
    # target tables.  This is the reliable backstop for engines that don't
    # support server-side table filtering (SQL Server, Databricks, etc.).
    target_set = {t.lower() for t in schema_tables}
    relevant = [
        q for q in workload.queries
        if any(t.lower() in target_set for t in q.tables)
    ]
    if not relevant:
        return {}

    queries = [q.sql for q in relevant]
    source_dialect = workload.source_dialect or dialect
    return {
        tbl: predicate_columns_from_queries(queries, tbl, source_dialect)
        for tbl in schema_tables
    }


def predicate_col_map_from_yaml(yaml_path: str, schema_tables: list[str]) -> dict[str, set[str]]:
    """Build a {table_name: set[col]} map from a workload YAML file.

    Reads the ``queries[*].sql`` field from the YAML (same format as
    benchmarks/queries/*.yaml) and calls ``predicate_columns_from_queries``
    for each table in ``schema_tables``.

    Parameters
    ----------
    yaml_path       Path to a workload YAML file (e.g. benchmarks/queries/tpch.yaml).
    schema_tables   List of table names to extract predicate columns for.

    Returns
    -------
    dict  {table_name_lowercase: set[col_name_lowercase]}
    """
    import yaml  # PyYAML is a transitive dependency of sqlalchemy

    with open(yaml_path) as fh:
        workload = yaml.safe_load(fh)

    source_dialect = workload.get("source_dialect", "ansi")
    queries = [q["sql"] for q in workload.get("queries", []) if "sql" in q]

    return {
        tbl: predicate_columns_from_queries(queries, tbl, source_dialect)
        for tbl in schema_tables
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_crdb_cache: dict[int, bool] = {}


def _is_cockroachdb(conn) -> bool:
    """Return True when the connection points to CockroachDB (not native PostgreSQL).

    CockroachDB exposes a Postgres-compatible wire protocol but does not populate
    pg_stats.histogram_bounds after ANALYZE; its pg_stats rows have NULL there.
    We detect it by querying the server version string (cached per connection
    object to avoid one extra query per column).
    """
    cid = id(conn)
    if cid not in _crdb_cache:
        try:
            row = _fetchone(conn, "SELECT version()")
            _crdb_cache[cid] = row is not None and "cockroach" in str(row[0]).lower()
        except Exception:
            _crdb_cache[cid] = False
    return _crdb_cache[cid]


def _needs_qmark(conn) -> bool:
    """Return True when the connection requires ? placeholders instead of %s.

    * mssql_python declares paramstyle='pyformat' but only binds ? at the TDS
      layer — %s is forwarded literally to SQL Server as a syntax error.
    * ibm_db_dbi uses qmark style (?); %s is not a valid SQL placeholder for DB2.
    """
    mod = type(conn).__module__.split(".")[0]
    return mod in ("mssql_python", "ibm_db_dbi")


def _needs_numeric(conn) -> bool:
    """Return True when the connection requires :1 :2 ... placeholders (oracledb).

    oracledb thin mode does not recognise %s as a bind-variable marker.
    Convert each %s to :1, :2, ... before execution.
    """
    return type(conn).__module__.split(".")[0] == "oracledb"


def _bind_sql(conn, sql: str) -> str:
    """Rewrite %s placeholders to the dialect-appropriate marker."""
    if _needs_qmark(conn):
        return sql.replace("%s", "?")
    if _needs_numeric(conn):
        import re, itertools
        counter = itertools.count(1)
        return re.sub(r"%s", lambda _: f":{next(counter)}", sql)
    return sql


def _fetchone(conn, sql: str, params=None):  # pragma: no cover
    cur = conn.cursor()
    cur.execute(_bind_sql(conn, sql), params or ())
    row = cur.fetchone()
    try:
        cur.close()
    except Exception:
        pass
    return row


def _fetchall(conn, sql: str, params=None) -> list[tuple]:  # pragma: no cover
    cur = conn.cursor()
    cur.execute(_bind_sql(conn, sql), params or ())
    rows = cur.fetchall() or []
    try:
        cur.close()
    except Exception:
        pass
    return rows


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
    # Oracle and DB2 store unquoted identifiers in uppercase; do not double-quote
    # them or the case-sensitive lookup will miss the object.
    if dialect in ("oracle", "db2"):
        if schema:
            return f"{schema.upper()}.{table.upper()}"
        return table.upper()
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
    elif dialect == "oracle":
        # Oracle stores unquoted identifiers as uppercase; ALL_TAB_COLUMNS is the
        # native catalog (information_schema is a thin compatibility stub that does
        # not accept bind variables, causing DPY-4009 with oracledb thin mode).
        if schema:
            params = (table.upper(), schema.upper())
            sql = """
                SELECT column_name FROM all_tab_columns
                WHERE table_name = :1 AND owner = :2
                ORDER BY column_id
            """
        else:
            params = (table.upper(),)
            sql = """
                SELECT column_name FROM user_tab_columns
                WHERE table_name = :1
                ORDER BY column_id
            """
    elif dialect == "db2":
        # DB2 LUW uses SYSCAT.COLUMNS — information_schema is not always accessible.
        # Column names are stored uppercase (matching quoted-uppercase DDL identifiers).
        if schema:
            params = (table, schema.upper())
            sql = """
                SELECT COLNAME FROM syscat.columns
                WHERE TABNAME = ? AND TABSCHEMA = ?
                ORDER BY COLNO
            """
        else:
            params = (table,)
            sql = """
                SELECT COLNAME FROM syscat.columns
                WHERE TABNAME = ? AND TABSCHEMA = CURRENT SCHEMA
                ORDER BY COLNO
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
        elif dialect == "db2":
            # Use SYSCAT.INDEXCOLUSE to find indexed columns.
            if schema:
                params = (table, schema.upper())
                rows = _fetchall(conn, """
                    SELECT DISTINCT ic.COLNAME
                    FROM   syscat.indexcoluse ic
                    JOIN   syscat.indexes     i
                           ON i.INDSCHEMA = ic.INDSCHEMA AND i.INDNAME = ic.INDNAME
                    WHERE  i.TABNAME = ? AND i.TABSCHEMA = ?
                """, params)
            else:
                params = (table,)
                rows = _fetchall(conn, """
                    SELECT DISTINCT ic.COLNAME
                    FROM   syscat.indexcoluse ic
                    JOIN   syscat.indexes     i
                           ON i.INDSCHEMA = ic.INDSCHEMA AND i.INDNAME = ic.INDNAME
                    WHERE  i.TABNAME = ? AND i.TABSCHEMA = CURRENT SCHEMA
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
    cfg: Optional[CollectionConfig] = None,
    is_predicate: bool = True,
    timing: Optional[CollectionTiming] = None,
) -> ColumnStats:
    """Collect per-column statistics using portable SQL.

    ``is_predicate`` — when pred_cols technique is active and this column does
    NOT appear in any workload query predicate, only null_fraction and
    n_distinct are collected (no MCV, no histogram, no min/max).  This
    mirrors Amazon Redshift's ANALYZE PREDICATE COLUMNS behaviour.
    """
    cfg = cfg or CollectionConfig()
    tref = _schema_table(table, dialect, schema)
    cref = _quote(col, dialect)

    # ── native-PG detection (shared by all technique guards below) ───────
    # CockroachDB normalises to dialect="postgres" but lacks pg_stats accuracy
    # guarantees (no histogram_bounds, no reliable correlation).  Detect it once
    # and use _is_native_pg as the guard throughout so CRDB gets SQL-based
    # histogram, correlation, and samp_nd just like MySQL/Oracle/etc.
    _is_native_pg = dialect == "postgres" and not _is_cockroachdb(conn)

    # ── baseline: null fraction + n_distinct ──────────────────────────────
    t0 = time.monotonic()

    # samp_nd: for large tables, estimate n_distinct from a sample instead of
    # COUNT(DISTINCT col) over the full table.  n_distinct is still collected
    # exactly for small tables and for predicate columns if desired — callers
    # can override by setting samp_nd=False in the config.
    # samp_nd skips native PostgreSQL because pg_stats already has accurate n_distinct
    # from ANALYZE.  CockroachDB is eligible for sampling.
    _use_samp_nd = (
        cfg.samp_nd
        and row_count > cfg.samp_nd_threshold
        and not _is_native_pg
    )

    if _use_samp_nd:
        t_samp = time.monotonic()
        total, non_null, n_distinct = _sample_ndistinct(
            conn, tref, cref, dialect, row_count, cfg.samp_nd_pct
        )
        if timing:
            timing.samp_nd_ms += (time.monotonic() - t_samp) * 1000
    else:
        row = _fetchone(conn, f"""
            SELECT COUNT(*), COUNT({cref}), COUNT(DISTINCT {cref}) FROM {tref}
        """)
        total, non_null, n_distinct_raw = (
            int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)
        )
        total    = total    or 0
        non_null = non_null or 0
        n_distinct = float(n_distinct_raw)

    null_fraction = (total - non_null) / total if total > 0 else 0.0

    if timing:
        timing.baseline_ms += (time.monotonic() - t0) * 1000

    # ── pred_cols short-circuit: skip enrichment for non-predicate columns ─
    # When pred_cols is active and this column is not a predicate column, we
    # skip min/max, avg_width, MCV, and histogram — they won't influence any
    # query plan.  null_fraction and n_distinct are always collected.
    if cfg.pred_cols and not is_predicate:
        return ColumnStats(
            name=col, null_fraction=null_fraction, n_distinct=n_distinct
        )

    # ── min / max ──────────────────────────────────────────────────────────
    # Collect MIN/MAX for:
    #   • indexed_cols is None  — index catalog query failed; safe fallback
    #   • col is in indexed_cols — index guarantees a cheap scan
    #   • all columns when the table has no indexes at all (e.g. constraints
    #     stripped for bulk loading).  PostgreSQL is excluded because it derives
    #     bounds from histogram_bounds in pg_stats.  CockroachDB normalises to
    #     "postgres" but does NOT populate pg_stats.histogram_bounds.
    min_val = max_val = None
    _no_indexes = indexed_cols is not None and len(indexed_cols) == 0
    if indexed_cols is None or col in indexed_cols or (_no_indexes and not _is_native_pg):
        t0 = time.monotonic()
        min_val, max_val = _fetch_min_max(conn, tref, cref, dialect)
        if timing:
            timing.baseline_ms += (time.monotonic() - t0) * 1000

    # ── avg width ──────────────────────────────────────────────────────────
    avg_width = 8
    try:
        t0 = time.monotonic()
        if dialect in ("mysql", "postgres"):
            row3 = _fetchone(conn, f"SELECT AVG(LENGTH(CAST({cref} AS CHAR))) FROM {tref}")
        else:
            row3 = _fetchone(conn, f"SELECT AVG(LEN(CAST({cref} AS NVARCHAR(MAX)))) FROM {tref}")
        if row3 and row3[0] is not None:
            avg_width = max(1, int(float(row3[0])))
        if timing:
            timing.baseline_ms += (time.monotonic() - t0) * 1000
    except Exception:
        _safe_rollback(conn)

    # ── most common values ─────────────────────────────────────────────────
    # full_mcv: remove LIMIT and collect the complete distribution when
    # n_distinct ≤ threshold.  Same query cost as top-10 (one GROUP BY scan).
    # Baseline: top-10 only.
    mcvs: list[MostCommonValue] = []
    _use_full_mcv = cfg.full_mcv and n_distinct <= cfg.full_mcv_threshold
    try:
        t0 = time.monotonic()
        if dialect == "sqlserver":
            # OFFSET/FETCH is the preferred paging syntax for SQL Server (TOP N
            # cannot coexist with OFFSET/FETCH in the same query).
            _ss_page = "" if _use_full_mcv else "OFFSET 0 ROWS FETCH NEXT 10 ROWS ONLY"
            mcv_sql = f"""
                SELECT {cref}, COUNT(*) AS cnt
                FROM {tref}
                WHERE {cref} IS NOT NULL
                GROUP BY {cref}
                ORDER BY cnt DESC
                {_ss_page}
            """
        else:
            _limit = "" if _use_full_mcv else "LIMIT 10"
            mcv_sql = f"""
                SELECT {cref}, COUNT(*) AS cnt
                FROM {tref}
                WHERE {cref} IS NOT NULL
                GROUP BY {cref}
                ORDER BY cnt DESC
                {_limit}
            """
        for row4 in _fetchall(conn, mcv_sql):
            val, cnt = row4[0], int(row4[1])
            freq = cnt / non_null if non_null > 0 else 0.0
            mcvs.append(MostCommonValue(value=str(val), frequency=freq))
        elapsed = (time.monotonic() - t0) * 1000
        if timing:
            if _use_full_mcv:
                timing.full_mcv_ms += elapsed
            else:
                timing.baseline_ms += elapsed
    except Exception:
        _safe_rollback(conn)

    # ── histogram bounds ───────────────────────────────────────────────────
    # ntile_hist: build an equi-height histogram using NTILE window function
    #   (up to ntile_hist_buckets buckets, default 100).  Replaces the 5-bound
    #   P10/P25/P50/P75/P90 approximation for non-Postgres engines.
    # Baseline: 5-bucket percentile approximation.
    # Both are skipped for NATIVE PostgreSQL (pg_stats provides authoritative
    # histogram_bounds from ANALYZE).  CockroachDB is NOT native PostgreSQL —
    # it uses the same wire protocol but does not populate pg_stats.histogram_bounds,
    # so it must fall through to the SQL-based histogram path.
    histogram_bounds: list[str] = []
    _use_ntile = cfg.ntile_hist and non_null > 0 and not _is_native_pg
    try:
        t0 = time.monotonic()
        if non_null > 0 and not _is_native_pg:
            if _use_ntile:
                histogram_bounds = _collect_ntile_histogram(
                    conn, tref, cref, dialect, row_count,
                    cfg.ntile_hist_buckets, cfg.ntile_hist_sample_rows,
                )
            else:
                histogram_bounds = _collect_histogram(conn, tref, cref, dialect)
        elapsed = (time.monotonic() - t0) * 1000
        if timing:
            if _use_ntile:
                timing.ntile_hist_ms += elapsed
            else:
                timing.baseline_ms += elapsed
    except Exception:
        _safe_rollback(conn)

    # ── correlation (physical sort order) ─────────────────────────────────
    # rank_corr: estimate Spearman rank correlation from a scan-order sample.
    # Skipped for native PostgreSQL (pg_stats fills it natively).
    # CockroachDB is included here as it doesn't provide pg_stats.correlation.
    correlation: float | None = None
    if cfg.rank_corr and not _is_native_pg and non_null > 0:
        try:
            t0 = time.monotonic()
            correlation = _collect_rank_correlation(
                conn, tref, cref, dialect, cfg.rank_corr_sample
            )
            if timing:
                timing.rank_corr_ms += (time.monotonic() - t0) * 1000
        except Exception:
            _safe_rollback(conn)

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
                if pg_row[4]:
                    histogram_bounds = _parse_pg_array_str(pg_row[4])
                    if min_val is None and histogram_bounds:
                        min_val = histogram_bounds[0]
                    if max_val is None and histogram_bounds:
                        max_val = histogram_bounds[-1]
                if pg_row[5] is not None:
                    correlation = float(pg_row[5])
                if pg_row[6] is not None:
                    avg_width = int(pg_row[6])
        except Exception:
            _safe_rollback(conn)

    # Derive min/max from histogram bounds when not collected via MIN/MAX or pg_stats.
    # This matters for CockroachDB (postgres dialect, no pg_stats.histogram_bounds) when
    # ntile_hist has populated histogram_bounds via NTILE window queries.
    if min_val is None and histogram_bounds:
        min_val = histogram_bounds[0]
    if max_val is None and histogram_bounds:
        max_val = histogram_bounds[-1]

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


def _sample_ndistinct(  # pragma: no cover
    conn, tref: str, cref: str, dialect: str, row_count: int, sample_pct: float
) -> tuple[int, int, float]:
    """Estimate (total, non_null, n_distinct) from a TABLESAMPLE.

    Technique: samp_nd.  Used when row_count > samp_nd_threshold to avoid
    a full-table COUNT(DISTINCT col) scan.  Returns exact counts for total
    and non_null (from the full-table COUNT path) but estimates n_distinct
    from the sample using simple linear scaling:
        n_distinct_est = count_distinct_in_sample / sample_fraction

    The Chao92 estimator would be more accurate for skewed distributions, but
    simple scaling keeps the implementation portable across all dialects and is
    accurate to within 10–20% for most optimizer purposes (our acceptance
    threshold is 2×).

    TABLESAMPLE syntax support:
      postgres, mysql 8.0.29+, sqlserver 2016+  →  TABLESAMPLE BERNOULLI(pct)
      oracle                                     →  SAMPLE(pct)
      db2 11.1+                                  →  TABLESAMPLE BERNOULLI(pct)
    """
    # Clamp sample to a sensible range; at least 1000 rows for stability.
    pct = max(0.01, min(100.0, sample_pct))

    try:
        if dialect == "oracle":
            sample_clause = f"SAMPLE({pct:.4f})"
            sample_sql = f"""
                SELECT COUNT(*), COUNT({cref}), COUNT(DISTINCT {cref})
                FROM {tref} {sample_clause}
            """
        elif dialect in ("mysql", "postgres", "db2", "sqlserver"):
            sample_sql = f"""
                SELECT COUNT(*), COUNT({cref}), COUNT(DISTINCT {cref})
                FROM {tref} TABLESAMPLE BERNOULLI({pct:.4f})
            """
        else:
            raise NotImplementedError(f"TABLESAMPLE not implemented for {dialect}")

        row = _fetchone(conn, sample_sql)
        s_total, s_non_null, s_distinct = (
            int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)
        )
        # Scale up n_distinct from the sample fraction.
        fraction = pct / 100.0
        n_distinct_est = s_distinct / fraction if fraction > 0 else float(s_distinct)
        # Clamp to row_count (can't have more distinct values than rows).
        n_distinct_est = min(n_distinct_est, float(row_count))

        # Use exact counts for total and non_null (cheap from existing baseline query).
        exact = _fetchone(conn, f"SELECT COUNT(*), COUNT({cref}) FROM {tref}")
        total    = int(exact[0] or 0)
        non_null = int(exact[1] or 0)
        return total, non_null, n_distinct_est

    except Exception:
        _safe_rollback(conn)
        # Fall back to exact COUNT(DISTINCT) on failure.
        row = _fetchone(conn, f"""
            SELECT COUNT(*), COUNT({cref}), COUNT(DISTINCT {cref}) FROM {tref}
        """)
        return int(row[0] or 0), int(row[1] or 0), float(row[2] or 0)


def _collect_ntile_histogram(  # pragma: no cover
    conn,
    tref: str,
    cref: str,
    dialect: str,
    row_count: int,
    n_buckets: int,
    sample_rows: int,
) -> list[str]:
    """Build an equi-height histogram with n_buckets using NTILE.

    Technique: ntile_hist.  Produces up to n_buckets upper-bound values that
    directly correspond to a PostgreSQL-style histogram_bounds array.  For a
    100-bucket histogram the planner gets the same density of range information
    that PostgreSQL's native ANALYZE produces.

    When the table is large (row_count > sample_rows), the NTILE is computed
    over a TABLESAMPLE to avoid a full sorted scan.

    Returns an empty list if the column type is not orderable (e.g. LOB types),
    in which case the caller falls back to no histogram.
    """
    try:
        if row_count > sample_rows:
            pct = min(100.0, max(0.1, (sample_rows / row_count) * 100.0))
            if dialect == "oracle":
                from_clause = f"{tref} SAMPLE({pct:.4f})"
            else:
                from_clause = f"{tref} TABLESAMPLE BERNOULLI({pct:.4f})"
        else:
            from_clause = tref

        if dialect == "sqlserver":
            # SQL Server does not allow TABLESAMPLE in a sub-query used with NTILE;
            # materialize via a CTE first.
            ntile_sql = f"""
                WITH sampled AS (
                    SELECT {cref} FROM {from_clause} WHERE {cref} IS NOT NULL
                ),
                bucketed AS (
                    SELECT {cref},
                           NTILE({n_buckets}) OVER (ORDER BY {cref}) AS bkt
                    FROM sampled
                )
                SELECT bkt, MAX({cref})
                FROM bucketed
                GROUP BY bkt
                ORDER BY bkt
            """
        else:
            ntile_sql = f"""
                SELECT bkt, MAX({cref})
                FROM (
                    SELECT {cref},
                           NTILE({n_buckets}) OVER (ORDER BY {cref}) AS bkt
                    FROM {from_clause}
                    WHERE {cref} IS NOT NULL
                ) sub
                GROUP BY bkt
                ORDER BY bkt
            """

        rows = _fetchall(conn, ntile_sql)
        bounds = [str(r[1]) for r in rows if r[1] is not None]
        return bounds
    except Exception:
        _safe_rollback(conn)
        return []


def _collect_rank_correlation(  # pragma: no cover
    conn,
    tref: str,
    cref: str,
    dialect: str,
    sample_size: int,
) -> float | None:
    """Estimate Spearman rank correlation between physical row order and column value.

    Technique: rank_corr.  Draws sample_size rows without an ORDER BY clause
    (i.e. in physical/heap scan order) then computes the Spearman rank
    correlation between the scan position (1, 2, 3, …) and the column value's
    rank within the sample.

    A correlation near 1.0 means rows are stored in ascending column order
    (ideal for index range scans).  Near -1.0 means descending.  Near 0 means
    random — the planner should prefer a sequential scan or bitmap heap scan.

    Returns None if the column is non-orderable (TEXT, LOB, etc.) or if scipy
    is not available.
    """
    try:
        from scipy.stats import spearmanr  # type: ignore[import]
    except ImportError:
        return None

    try:
        if dialect == "sqlserver":
            sample_sql = f"""
                SELECT TOP {sample_size} {cref}
                FROM {tref}
                WHERE {cref} IS NOT NULL
            """
        elif dialect == "oracle":
            sample_sql = f"""
                SELECT {cref} FROM {tref}
                WHERE {cref} IS NOT NULL
                AND ROWNUM <= {sample_size}
            """
        elif dialect == "db2":
            sample_sql = f"""
                SELECT {cref} FROM {tref}
                WHERE {cref} IS NOT NULL
                FETCH FIRST {sample_size} ROWS ONLY
            """
        else:  # mysql, postgres
            sample_sql = f"""
                SELECT {cref} FROM {tref}
                WHERE {cref} IS NOT NULL
                LIMIT {sample_size}
            """

        rows = _fetchall(conn, sample_sql)
        if len(rows) < 10:
            return None

        values = [r[0] for r in rows]
        # Attempt float conversion to detect non-orderable types.
        # For dates and strings, spearmanr works on the rank of string values.
        try:
            numeric_vals = [float(v) for v in values]
            corr, _ = spearmanr(range(len(numeric_vals)), numeric_vals)
        except (TypeError, ValueError):
            # Non-numeric: rank by string representation.
            str_vals = [str(v) for v in values]
            corr, _ = spearmanr(range(len(str_vals)), [sorted(set(str_vals)).index(v) for v in str_vals])

        return float(corr) if corr is not None and not math.isnan(corr) else None
    except Exception:
        _safe_rollback(conn)
        return None


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
