"""
Dialect registry.

Public API (unchanged) — implementation lives in ``core/dialect_registry.py``.
"""

from __future__ import annotations

from .core.dialect_registry import (
    CANONICAL_DIALECTS,
    DDL_FILE_FORMAT_HINTS,
    DIALECT_ALIASES,
    SCHEMA_SOURCE_DB2_ALIASES,
    SCHEMA_SOURCE_MYSQL_ALIASES,
    SCHEMA_SOURCE_POSTGRES_ALIASES,
    SQLGLOT_DIALECT,
    normalize_dialect,
    sqlglot_dialect_name,
)
