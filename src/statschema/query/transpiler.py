"""
Query transpiler — rewrite SQL from source dialect to target dialect using sqlglot.

Public API
----------
transpile_query(entry, target_dialect)  → QueryEntry  (updated sql, flags manual_review on failure)
transpile_workload(workload, target_dialect)  → QueryWorkload

sqlglot dialect name mapping
-----------------------------
statschema dialect  sqlglot read dialect    sqlglot write dialect
------------------  --------------------    ---------------------
postgres            postgres                postgres
neon                postgres                postgres
cockroachdb         postgres                postgres
lakebase            postgres                postgres
mysql               mysql                   mysql
mariadb             mysql                   mysql
sqlserver           tsql                    tsql
oracle              oracle                  oracle
databricks          databricks              databricks
db2                 (not supported)         (not supported)
sqlite              sqlite                  sqlite

Constructs that sqlglot cannot transpile reliably are flagged with
``manual_review = True`` and a ``transpile_error`` message.  The original
SQL is preserved so the caller can inspect it.
"""

from __future__ import annotations

import dataclasses
from typing import Optional

import sqlglot
import sqlglot.errors

from .model import QueryEntry, QueryWorkload

# Map statschema dialect names to sqlglot dialect identifiers
_TO_SQLGLOT: dict[str, str] = {
    "postgres":    "postgres",
    "postgresql":  "postgres",
    "neon":        "postgres",
    "cockroachdb": "postgres",
    "lakebase":    "postgres",
    "mysql":       "mysql",
    "mariadb":     "mysql",
    "sqlserver":   "tsql",
    "tsql":        "tsql",
    "oracle":      "oracle",
    "databricks":  "databricks",
    "sqlite":      "sqlite",
}

# Constructs with no portable equivalent — detected by keyword presence
_MANUAL_REVIEW_PATTERNS = [
    "FOR XML",           # T-SQL XML shredding
    "CONNECT BY",        # Oracle hierarchical queries
    "START WITH",        # Oracle hierarchical queries
    "PIVOT",             # Dynamic pivot (T-SQL / Oracle)
    "UNPIVOT",
    "BULK INSERT",       # T-SQL bulk load
    "DBMS_",             # Oracle PL/SQL package calls
    "UTL_",
    "SYS_CONNECT_BY_PATH",
    "XMLELEMENT",
    "XMLAGG",
    "OPENJSON",          # T-SQL JSON
    "JSON_TABLE",        # MySQL / Oracle
    "MATCH_RECOGNIZE",   # Oracle / SQL Server pattern matching
    "WITHIN GROUP",      # ordered-set aggregates (some dialects only)
]


def _needs_manual_review(sql: str) -> Optional[str]:
    upper = sql.upper()
    for pattern in _MANUAL_REVIEW_PATTERNS:
        if pattern in upper:
            return f"Contains '{pattern}' — no portable equivalent; manual rewrite required."
    return None


def transpile_query(entry: QueryEntry, target_dialect: str) -> QueryEntry:
    """Return a new QueryEntry with SQL transpiled to *target_dialect*.

    Resolution order:
    1. ``entry.rewrites[target_dialect]`` — user-supplied rewrite, used verbatim.
    2. sqlglot transpilation from ``entry.source_dialect`` to ``target_dialect``.
    3. If sqlglot fails or detects non-portable constructs, ``manual_review=True``.

    The original SQL and rewrites dict are always preserved in the returned entry.
    """
    # ── 1. User-supplied rewrite wins unconditionally ─────────────────────────
    normalised_target = target_dialect.lower()
    # Check both the given name and common aliases (lakebase → postgres, neon → postgres)
    rewrite_key = _TO_SQLGLOT.get(normalised_target, normalised_target)
    user_sql = entry.rewrites.get(normalised_target) or entry.rewrites.get(rewrite_key)
    if user_sql:
        return dataclasses.replace(
            entry,
            sql=user_sql,
            manual_review=False,
            transpile_error=None,
        )

    review_reason = _needs_manual_review(entry.sql)
    if review_reason:
        return dataclasses.replace(entry, manual_review=True, transpile_error=review_reason)

    read_dialect  = _TO_SQLGLOT.get(entry.source_dialect.lower())
    write_dialect = _TO_SQLGLOT.get(target_dialect.lower())

    if read_dialect is None or write_dialect is None:
        unsupported = entry.source_dialect if read_dialect is None else target_dialect
        return dataclasses.replace(
            entry,
            manual_review=True,
            transpile_error=f"Dialect '{unsupported}' is not supported by the query transpiler.",
        )

    if read_dialect == write_dialect:
        # Same dialect family — no transformation needed
        return dataclasses.replace(entry, manual_review=False, transpile_error=None)

    try:
        result = sqlglot.transpile(entry.sql, read=read_dialect, write=write_dialect)
        transpiled = result[0] if result else entry.sql
        return dataclasses.replace(
            entry,
            sql=transpiled,
            manual_review=False,
            transpile_error=None,
        )
    except (sqlglot.errors.ParseError, sqlglot.errors.UnsupportedError) as exc:
        return dataclasses.replace(
            entry,
            manual_review=True,
            transpile_error=str(exc),
        )
    except Exception as exc:
        return dataclasses.replace(
            entry,
            manual_review=True,
            transpile_error=f"Unexpected transpilation error: {exc}",
        )


def transpile_workload(workload: QueryWorkload, target_dialect: str) -> QueryWorkload:
    """Transpile every entry in *workload* to *target_dialect*.

    Returns a new QueryWorkload; the original is not modified.
    Entries that cannot be transpiled have ``manual_review=True``.
    """
    transpiled = [transpile_query(e, target_dialect) for e in workload.queries]
    return QueryWorkload(source_dialect=workload.source_dialect, queries=transpiled)
