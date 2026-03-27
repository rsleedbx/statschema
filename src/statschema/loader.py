"""
Schema format detection and loading.

Public API (unchanged) — implementation lives in ``core/loader.py``.
"""

from __future__ import annotations

from .core.loader import (
    SchemaSource,
    get_schema_source_from_data,
    detect_format,
    _load_data_from_path,
    _is_sql_dialect,
    load_schema,
    _is_multi_table_ydata,
)
