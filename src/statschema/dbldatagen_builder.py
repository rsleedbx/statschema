"""
Convert canonical schema to Databricks Labs Data Generator (dbldatagen) column specs.

Public API (unchanged) — implementation lives in ``spark/dbldatagen_builder.py``.
"""

from __future__ import annotations

from .spark.dbldatagen_builder import (
    to_dbldatagen_specs,
    build_dataframe_from_canonical,
    _template_for_format_pattern,
    _FORMAT_PATTERN_TEMPLATES,
    _dbldatagen_distribution,
    _cast_stat_value,
    _spark_type_and_options,
)

__all__ = [
    "to_dbldatagen_specs",
    "build_dataframe_from_canonical",
]
