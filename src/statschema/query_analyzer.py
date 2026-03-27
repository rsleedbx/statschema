"""
Layer 3 query-pattern analysis — extended statistics and FK range inference.

Public API (unchanged) — implementation lives in ``query/analyzer.py``.
"""

from __future__ import annotations

from .query.analyzer import (
    ColumnPredicate,
    extract_column_pairs,
    make_stat_name,
    classify_tables,
    extract_column_predicates,
    resolve_fk_generation_ranges,
    _pairs_from_sql,
    _predicates_from_sql,
)

__all__ = [
    "ColumnPredicate",
    "extract_column_pairs",
    "make_stat_name",
    "classify_tables",
    "extract_column_predicates",
    "resolve_fk_generation_ranges",
]
