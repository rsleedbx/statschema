"""
Row generator (pure Python).

Public API (unchanged) — implementation lives in ``core/row_generator.py``.
"""

from __future__ import annotations

from .core.row_generator import (
    generate_rows,
    _fmt_phone_us,
    _fmt_phone_intl,
    _fmt_postal_us,
    _fmt_name_first,
    _fmt_name_last,
    _TPCC_SYLLABLES,
    _tpcc_make_last_name,
    _fmt_address,
    _fmt_city,
    _FORMAT_PATTERN_FN,
    _zipf_sample,
    _rand_string,
    _rand_date,
    _parse_date,
    _gen_value,
)
