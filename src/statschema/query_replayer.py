"""
Query replayer — run EXPLAIN on each entry of a QueryWorkload against a live target.

Public API (unchanged) — implementation lives in ``query/replayer.py``.
"""

from __future__ import annotations

from .query.replayer import (
    replay_queries,
    print_replay_report,
    _explain_postgres,
    _explain_mysql,
    _explain_sqlserver,
    _explain_oracle,
    _explain_databricks,
    _run_explain,
)

__all__ = [
    "replay_queries",
    "print_replay_report",
]
