"""
Semantic hints for column name inference.

Public API (unchanged) — implementation lives in ``core/semantic_hints.py``.
"""

from __future__ import annotations

from .core.semantic_hints import (
    SemanticHints,
    load_hints,
    _PATTERNS_DIR,
    load_builtin_patterns,
    load_builtin_comment_patterns,
    infer_format_pattern,
    apply_hints,
    _infer_format_pattern_llm,
)
