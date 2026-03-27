"""
Stats I/O.

Public API (unchanged) — implementation lives in ``core/stats_io.py``.
"""

from __future__ import annotations

from .core.stats_io import (
    load_stats,
    dump_stats,
    make_default_stats,
    _apply_identifier_case,
    _apply_reserved_word,
    _apply_col_override_to_schema,
    _apply_col_override_to_stats,
    apply_overrides,
    apply_overrides_all,
)
