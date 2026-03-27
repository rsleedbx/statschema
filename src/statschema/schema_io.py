"""
Schema I/O.

Public API (unchanged) — implementation lives in ``core/schema_io.py``.
"""

from __future__ import annotations

from .core.schema_io import (
    _SCHEMA_VERSION,
    dump_schema,
    load_canonical,
    resolve_load_order,
    resolve_row_counts,
)
