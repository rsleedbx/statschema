"""
Canonical query workload model.

Public API (unchanged) — implementation lives in ``query/model.py``.
"""

from __future__ import annotations

from .query.model import (
    QueryEntry,
    QueryWorkload,
    ReplayResult,
    _entry_to_dict,
    _entry_from_dict,
    dump_queries,
    load_queries,
)

__all__ = [
    "QueryEntry",
    "QueryWorkload",
    "ReplayResult",
    "dump_queries",
    "load_queries",
]
