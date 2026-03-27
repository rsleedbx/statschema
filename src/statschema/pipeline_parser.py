"""
Pipeline config parser.

Public API (unchanged) — implementation lives in ``core/pipeline_parser.py``.
"""

from __future__ import annotations

from .core.pipeline_parser import (
    PIPELINE_TYPE_TO_CANONICAL,
    parse_pipeline_tables,
    parse_pipeline_config,
)
