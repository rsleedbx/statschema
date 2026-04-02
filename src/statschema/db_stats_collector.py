"""
Collect canonical TableStats from a live database by running SQL queries.

Produces ``TableStats`` / ``ColumnStats`` objects that are compatible with
``build_dataframe_from_canonical(spark, table, rows, stats=table_stats)``
so that synthetic data is driven by real measured distributions.

Supported databases
-------------------
  mysql       pymysql connection  (or any PEP-249 connection to MySQL / MariaDB)
  postgres    psycopg2 connection (or any PEP-249 connection to PostgreSQL / Neon / CockroachDB)
  sqlserver   mssql-python connection  (or any PEP-249 connection to SQL Server)
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
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .dialect_registry import normalize_dialect

logger = logging.getLogger(__name__)
from .stats_model import ColumnStats, TableStats

# ---------------------------------------------------------------------------
# Re-export CollectionConfig + CollectionTiming from the shared module
# ---------------------------------------------------------------------------

from .dialects._collector_shared import (
    CollectionConfig,
    CollectionTiming,
    # Internal helpers — re-exported for tests that import them directly
    _is_cockroachdb,
    _needs_qmark,
    _needs_numeric,
    _bind_sql,
    _fetchone,
    _fetchall,
    _safe_rollback,
    _quote,
    _schema_table,
    _introspect_columns,
    _count_rows,
    _fetch_min_max,
    _indexed_columns,
    _collect_column_stats,
    _sample_ndistinct,
    _collect_ntile_histogram,
    _collect_rank_correlation,
    _collect_histogram,
    _percentile_bounds,
    _parse_pg_array_str,
    _parse_pg_array_mcv,
    _collect_pg_composite_stats,
    _crdb_cache,
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

    tbl_key = table.lower()
    pred_set: set[str] | None = cfg.predicate_col_map.get(tbl_key) if cfg.pred_cols else None

    timing = CollectionTiming() if cfg.record_timing else None

    col_stats: list[ColumnStats] = []
    for col in columns:
        cs = _collect_column_stats(
            conn, table, col, d, schema, row_count, indexed_cols,
            cfg=cfg, is_predicate=(pred_set is None or col.lower() in pred_set),
            timing=timing,
        )
        col_stats.append(cs)

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
                return tbl_in_query
            actual = alias_to_table.get(tbl_ref, tbl_ref)
            return actual == tbl_lower

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
    """Build a predicate-column map by reading the database's own query store."""
    from .query_collector import collect_top_queries

    workload = collect_top_queries(
        conn, dialect, n=n, rank_by=rank_by,
        catalog=catalog, schema=schema, tables=schema_tables,
    )
    if not workload.queries:
        return {}

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
    """Build a {table_name: set[col]} map from a workload YAML file."""
    import yaml

    with open(yaml_path) as fh:
        workload = yaml.safe_load(fh)

    source_dialect = workload.get("source_dialect", "ansi")
    queries = [q["sql"] for q in workload.get("queries", []) if "sql" in q]

    return {
        tbl: predicate_columns_from_queries(queries, tbl, source_dialect)
        for tbl in schema_tables
    }
