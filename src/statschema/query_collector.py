"""
Top-query collection from live databases.

Public API (unchanged) — implementation lives in ``query/collector.py``.
"""

from __future__ import annotations

from .query.collector import (
    collect_top_queries,
    _collect_postgres,
    _collect_mysql,
    _collect_sqlserver,
    _collect_oracle,
    _collect_databricks,
    _short_id,
    _extract_tables,
)

__all__ = [
    "collect_top_queries",
]
