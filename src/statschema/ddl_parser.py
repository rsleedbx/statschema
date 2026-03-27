"""
Parse SQL DDL (CREATE TABLE) into canonical schema using sqlglot.

Public API (unchanged):
    parse_ddl(sql, dialect=None)  → list[CanonicalTableSchema]
    parse_ddl_file(path, dialect) → list[CanonicalTableSchema]

Implementation lives in ``dialects/_parser_shared.py``.
Per-dialect thin wrappers are in ``dialects/<name>/parser.py``.
"""

from __future__ import annotations

# Re-export everything from the shared implementation for backward compat.
from .dialects._parser_shared import (
    # Public API
    parse_ddl,
    parse_ddl_file,
    # Public type maps (imported by tests)
    MYSQL_TYPE_TO_CANONICAL,
    POSTGRES_TYPE_TO_CANONICAL,
    ORACLE_TYPE_TO_CANONICAL,
    SQLSERVER_TYPE_TO_CANONICAL,
    # Internal helpers re-exported for test suite
    _detect_dialect,
    _parse_create_table,
    _preprocess_oracle_in_mysql,
    _preprocess_pg,
    _normalize_default,
    _apply_alter_fks,
    _dtype_info,
    _extract_params,
    _source_type_str,
    _fk_from_reference,
    _parse_fk_constraints,
    _parse_inline_fk_constraints,
    _DTYPE_TO_CANONICAL,
    _TEMPORAL_DTYPES,
    _LENGTH_DTYPES,
    _SERIAL_DTYPES,
    _FIXED_MONEY_PRECISION,
)

__all__ = [
    "parse_ddl",
    "parse_ddl_file",
    "MYSQL_TYPE_TO_CANONICAL",
    "POSTGRES_TYPE_TO_CANONICAL",
    "ORACLE_TYPE_TO_CANONICAL",
    "SQLSERVER_TYPE_TO_CANONICAL",
]
