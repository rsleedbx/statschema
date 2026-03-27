"""
Table-name rename transform for canonical schemas.

Public API (unchanged) — implementation lives in ``spark/schema_transforms.py``.
"""

from __future__ import annotations

from .spark.schema_transforms import (
    TABLE_NAME_PRESETS,
    rename_tables,
    parse_table_map,
    _rename_table,
    _rename_col,
    _rename_fk,
)

__all__ = [
    "TABLE_NAME_PRESETS",
    "rename_tables",
    "parse_table_map",
]
