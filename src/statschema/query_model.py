"""
Canonical query workload model.

A QueryWorkload is the query-side companion to schema.yaml / stats.yaml.
It stores the top-N queries collected from a source database in a dialect-agnostic
form so they can be transpiled to any target and replayed via EXPLAIN.

Typical flow
------------
1. ``statschema collect --top-queries 50`` writes ``queries.yaml`` alongside
   ``schema.yaml`` and ``stats.yaml``.
2. ``statschema replay --dialect postgres --queries queries.yaml --dsn ...``
   transpiles each entry and runs EXPLAIN on the target.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class QueryEntry:
    """One normalized query from the source workload."""

    id: str
    """Unique identifier — usually the database's internal query hash (queryid, sql_id, DIGEST)."""

    source_dialect: str
    """Dialect the SQL was collected from (mysql, postgres, tsql, oracle, databricks)."""

    sql: str
    """Normalized query text with literals replaced by placeholders where the DB provides them."""

    tables: list[str] = field(default_factory=list)
    """Table names referenced in the query (best-effort extraction)."""

    calls: int = 0
    """Execution count over the collection window."""

    total_elapsed_ms: float = 0.0
    """Total accumulated elapsed time across all executions (milliseconds)."""

    mean_elapsed_ms: float = 0.0
    """Mean per-call elapsed time (milliseconds)."""

    rank_by: str = "total_time"
    """Metric used to rank this entry: total_time | calls | mean_time."""

    manual_review: bool = False
    """True if transpilation failed or used constructs with no portable equivalent."""

    transpile_error: Optional[str] = None
    """Error message from the last failed transpilation attempt."""

    rewrites: dict[str, str] = field(default_factory=dict)
    """
    User-supplied target-dialect rewrites, keyed by dialect name.

    When present for a given target dialect, the rewrite is used verbatim instead of
    running sqlglot transpilation.  This is the escape hatch for queries that contain
    non-portable constructs (FOR XML, CONNECT BY, PIVOT, etc.) or where sqlglot
    produces incorrect output for a specific target.

    Example::

        rewrites:
          postgres:   "SELECT string_agg(name, ',') FROM categories"
          databricks: "SELECT array_join(collect_list(name), ',') FROM categories"

    Dialects not listed here fall back to the normal sqlglot transpilation path.
    """


@dataclass
class ReplayResult:
    """EXPLAIN output for one query on the target database."""

    query_id: str
    target_dialect: str
    sql: str
    """The transpiled SQL that was sent to EXPLAIN."""

    explain_output: str
    """Raw EXPLAIN text returned by the target."""

    success: bool = True
    error: Optional[str] = None


@dataclass
class QueryWorkload:
    """Collection of top queries from one source database."""

    source_dialect: str
    queries: list[QueryEntry] = field(default_factory=list)


# ---------------------------------------------------------------------------
# YAML serialisation
# ---------------------------------------------------------------------------

def _entry_to_dict(e: QueryEntry) -> dict:
    d: dict = {
        "id": e.id,
        "source_dialect": e.source_dialect,
        "sql": e.sql,
    }
    if e.tables:
        d["tables"] = e.tables
    if e.calls:
        d["calls"] = e.calls
    if e.total_elapsed_ms:
        d["total_elapsed_ms"] = round(e.total_elapsed_ms, 3)
    if e.mean_elapsed_ms:
        d["mean_elapsed_ms"] = round(e.mean_elapsed_ms, 3)
    if e.rank_by != "total_time":
        d["rank_by"] = e.rank_by
    if e.manual_review:
        d["manual_review"] = True
    if e.transpile_error:
        d["transpile_error"] = e.transpile_error
    if e.rewrites:
        d["rewrites"] = e.rewrites
    return d


def _entry_from_dict(d: dict) -> QueryEntry:
    return QueryEntry(
        id=d["id"],
        source_dialect=d["source_dialect"],
        sql=d["sql"],
        tables=d.get("tables", []),
        calls=d.get("calls", 0),
        total_elapsed_ms=d.get("total_elapsed_ms", 0.0),
        mean_elapsed_ms=d.get("mean_elapsed_ms", 0.0),
        rank_by=d.get("rank_by", "total_time"),
        manual_review=d.get("manual_review", False),
        transpile_error=d.get("transpile_error"),
        rewrites=d.get("rewrites", {}),
    )


def dump_queries(workload: QueryWorkload, path: str | Path) -> None:
    """Write a QueryWorkload to a YAML file."""
    data = {
        "source_dialect": workload.source_dialect,
        "queries": [_entry_to_dict(e) for e in workload.queries],
    }
    Path(path).write_text(yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False))


def load_queries(source: str | Path | dict) -> QueryWorkload:
    """Load a QueryWorkload from a YAML file or a pre-parsed dict."""
    if isinstance(source, dict):
        data = source
    else:
        data = yaml.safe_load(Path(source).read_text())
    return QueryWorkload(
        source_dialect=data["source_dialect"],
        queries=[_entry_from_dict(e) for e in data.get("queries", [])],
    )
