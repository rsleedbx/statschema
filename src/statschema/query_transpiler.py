"""
Query transpiler — rewrite SQL from source dialect to target dialect.

Public API (unchanged) — implementation lives in ``query/transpiler.py``.
"""

from __future__ import annotations

from .query.transpiler import (
    transpile_query,
    transpile_workload,
    _needs_manual_review,
)

__all__ = [
    "transpile_query",
    "transpile_workload",
]
