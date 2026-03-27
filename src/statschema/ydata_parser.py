"""
YData YAML parser.

Public API (unchanged) — implementation lives in ``core/ydata_parser.py``.
"""

from __future__ import annotations

from .core.ydata_parser import (
    YDATA_TYPE_TO_CANONICAL,
    _parse_ydata_field,
    parse_ydata_yaml,
    parse_ydata_yaml_file,
    parse_ydata_multi_yaml,
)
