"""
SDV metadata parser.

Public API (unchanged) — implementation lives in ``core/sdv_parser.py``.
"""

from __future__ import annotations

from .core.sdv_parser import (
    SDV_SDTYPE_TO_CANONICAL,
    _parse_sdv_column,
    _build_foreign_keys_for_table,
    parse_sdv_metadata,
    parse_sdv_file,
)
