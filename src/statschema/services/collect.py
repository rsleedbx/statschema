"""
Service: collect

Collect column-level statistics and/or query workloads from a live source
database.  All callers (CLI, API, notebooks) go through these functions rather
than calling ``db_stats_collector`` or ``query_collector`` directly.
"""

from __future__ import annotations

from typing import Any

from ..db_stats_collector import collect_table_stats, CollectionConfig
from ..stats_model import TableStats, DatabaseStats
from ..query_collector import collect_top_queries
from ..query_model import QueryWorkload


def collect(
    conn: Any,
    tables: list[str],
    dialect: str,
    schema: str | None = None,
    config: CollectionConfig | None = None,
) -> DatabaseStats:
    """Collect statistics for one or more tables from a live source database.

    Parameters
    ----------
    conn:
        Open DBAPI-2 connection to the source database.
    tables:
        Table names to collect statistics for.
    dialect:
        Source engine dialect (e.g. ``"postgres"``, ``"mysql"``, ``"oracle"``).
    schema:
        Source schema / database name.  ``None`` uses the connection default.
    config:
        :class:`~statschema.db_stats_collector.CollectionConfig` controlling
        enrichment techniques.  ``None`` uses baseline collection only.

    Returns
    -------
    DatabaseStats
        One :class:`~statschema.stats_model.TableStats` entry per table.
    """
    collected: list[TableStats] = []
    for table in tables:
        ts = collect_table_stats(conn, table, dialect, schema=schema, config=config)
        collected.append(ts)
    return DatabaseStats(database=schema or "", tables=collected)


def collect_queries(
    conn: Any,
    dialect: str,
    n: int = 50,
    rank_by: str = "total_time",
    catalog: str | None = None,
    schema: str | None = None,
    tables: list[str] | None = None,
) -> QueryWorkload:
    """Collect the top-N queries from a live source database.

    Parameters
    ----------
    conn:
        Open DBAPI-2 connection to the source database.
    dialect:
        Source engine dialect.
    n:
        Number of queries to collect (default: 50).
    rank_by:
        Ranking metric: ``"total_time"`` (default), ``"calls"``,
        ``"mean_time"``.
    catalog:
        Database / catalog name for MySQL-family engines.
    schema:
        Schema name to filter queries by.
    tables:
        Limit collection to queries referencing these tables.

    Returns
    -------
    QueryWorkload
        Top-N queries ranked by the chosen metric.
    """
    return collect_top_queries(
        conn,
        dialect,
        n=n,
        rank_by=rank_by,
        catalog=catalog,
        schema=schema,
        tables=tables,
    )
