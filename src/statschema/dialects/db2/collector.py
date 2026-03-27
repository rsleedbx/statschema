"""db2 statistics collector — thin wrapper over the shared implementation."""

from __future__ import annotations

from typing import Any, Optional

from .._collector_shared import (
    CollectionConfig,
    CollectionTiming,
    _collect_column_stats,
    _introspect_columns,
    _count_rows,
    _indexed_columns,
    _safe_rollback,
    _collect_pg_composite_stats,
)
from ...stats_model import ColumnStats, TableStats

DIALECT = "db2"


def collect_table_stats(  # pragma: no cover
    conn: Any,
    table: str,
    schema: str | None = None,
    columns: list[str] | None = None,
    config: Optional[CollectionConfig] = None,
) -> TableStats:
    """Collect TableStats from a live db2 database table."""
    cfg = config or CollectionConfig()

    if columns is None:
        columns = _introspect_columns(conn, table, DIALECT, schema)

    row_count    = _count_rows(conn, table, DIALECT, schema)
    indexed_cols = _indexed_columns(conn, table, DIALECT, schema)

    tbl_key  = table.lower()
    pred_set: set[str] | None = cfg.predicate_col_map.get(tbl_key) if cfg.pred_cols else None
    timing   = CollectionTiming() if cfg.record_timing else None

    col_stats: list[ColumnStats] = []
    for col in columns:
        try:
            cs = _collect_column_stats(
                conn, table, col, DIALECT, schema, row_count, indexed_cols,
                cfg=cfg, is_predicate=(pred_set is None or col.lower() in pred_set),
                timing=timing,
            )
            col_stats.append(cs)
        except Exception:
            _safe_rollback(conn)
            col_stats.append(ColumnStats(name=col, null_fraction=0.0, n_distinct=1.0))

    composite: list = []
    if DIALECT == "postgres":
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
