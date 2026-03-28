"""
Pandas DataFrame builder.

Public API (unchanged) — implementation lives in ``core/pandas_builder.py``.
"""

from __future__ import annotations

from .core.pandas_builder import (
    build_rows_from_canonical,
    _WORDS,
    _COUNTRY_ISO2,
    _CURRENCY_ISO,
    _rng_words,
    _generate_pattern,
    _zipf_array,
    _temporal_origin_and_span,
    _TEMPORAL_DEFAULT_LO,
    _TEMPORAL_DEFAULT_HI,
    _generate_column,
    _generate_string,
    _generate_temporal,
    _apply_nulls,
    _CONSTRAINT_RE,
    _enforce_temporal_constraints,
)
from .core.model import build_fk_max_map as _build_fk_ranges  # backward-compat alias
