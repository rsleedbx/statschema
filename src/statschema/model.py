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
    expand_table_instances,
    _expand_one,
)
