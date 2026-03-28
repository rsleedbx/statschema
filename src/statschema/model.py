"""
Canonical table/column model.

Public API (unchanged) — implementation lives in ``core/model.py``.
"""

from __future__ import annotations

from .core.model import (
    GenerationRule,
    CanonicalColumn,
    CanonicalForeignKey,
    CanonicalTableSchema,
    build_fk_max_map,
    expand_table_instances,
    _expand_one,
)
