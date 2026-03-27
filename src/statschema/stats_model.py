"""
Statistics model.

Public API (unchanged) — implementation lives in ``core/stats_model.py``.
"""

from __future__ import annotations

from .core.stats_model import (
    MostCommonValue,
    ColumnStats,
    IndexStats,
    MCVCombination,
    CompositeColumnStats,
    ForeignKeyStats,
    TableStats,
    DatabaseStats,
)
