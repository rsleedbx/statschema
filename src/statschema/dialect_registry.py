"""
Single source of truth for SQL dialect names and aliases.

When adding a database that shares grammar with an existing engine (e.g. Neon → Postgres),
register aliases here and wire tests; avoid duplicating maps across ``ddl_parser``,
``ddl_emitter``, ``loader``, ``db_stats_collector``, and ``override_model``.
"""

from __future__ import annotations

# Canonical keys used by emitters, parser (after normalization), and round-trip tests.
CANONICAL_DIALECTS: tuple[str, ...] = (
    "mysql",
    "postgres",
    "sqlserver",
    "oracle",
    "databricks",
    "db2",
)

# Dump labels / product names → canonical. Used by ``emit_ddl``, ``parse_ddl``, stats, loader hints.
DIALECT_ALIASES: dict[str, str] = {
    "postgresql":  "postgres",
    "pg":          "postgres",
    "neon":        "postgres",
    "neondb":      "postgres",
    "cockroach":   "postgres",   # CockroachDB — Postgres wire protocol; DDL is Postgres-compatible
    "cockroachdb": "postgres",
    "crdb":        "postgres",
    "mariadb":     "mysql",      # MariaDB — MySQL wire protocol; DDL is MySQL-compatible
    "maria":       "mysql",
    "mariadb_columnstore": "mysql",
    "ibmdb2":      "db2",        # IBM Db2 — own wire protocol and DDL; no sqlglot support
    "ibm_db2":     "db2",
    "db2luw":      "db2",        # Db2 LUW (Linux/Unix/Windows)
    "db2z":        "db2",        # Db2 for z/OS
    "db2i":        "db2",        # Db2 for i (AS/400)
    "dashdb":      "db2",        # IBM dashDB (Db2 on Cloud predecessor)
    "mssql":       "sqlserver",
    "tsql":        "sqlserver",
    "azure_sql":   "sqlserver",
    "synapse":     "sqlserver",
    "spark":       "databricks",
    "delta":       "databricks",
    "dbsql":       "databricks",
}

# sqlglot ``dialect=`` argument per canonical dialect
SQLGLOT_DIALECT: dict[str, str] = {
    "mysql":      "mysql",
    "postgres":   "postgres",
    "sqlserver":  "tsql",
    "oracle":     "oracle",
    "databricks": "databricks",
    "db2":        "",            # sqlglot has no Db2 dialect; use ANSI SQL (empty string)
}

# ``schema_source`` / ``format_hint`` strings that mean Postgres-flavored DDL (not enum members).
SCHEMA_SOURCE_POSTGRES_ALIASES: frozenset[str] = frozenset(
    {"postgresql", "pg", "neon", "neondb", "cockroach", "cockroachdb", "crdb"}
)

# ``schema_source`` / ``format_hint`` strings that mean MySQL-flavored DDL (not enum members).
SCHEMA_SOURCE_MYSQL_ALIASES: frozenset[str] = frozenset(
    {"mariadb", "maria", "mariadb_columnstore"}
)

# ``schema_source`` / ``format_hint`` strings that mean Db2 DDL (not enum members).
SCHEMA_SOURCE_DB2_ALIASES: frozenset[str] = frozenset(
    {"ibmdb2", "ibm_db2", "db2luw", "db2z", "db2i", "dashdb"}
)

# All strings valid as ``load_schema(..., format_hint=...)`` when loading a ``.sql`` path.
DDL_FILE_FORMAT_HINTS: frozenset[str] = frozenset(DIALECT_ALIASES.keys()) | frozenset(
    CANONICAL_DIALECTS
)


def normalize_dialect(name: str) -> str:
    """Map a user or vendor label to a canonical dialect; unknown strings pass through lowercased."""
    n = name.lower().strip()
    return DIALECT_ALIASES.get(n, n)


def sqlglot_dialect_name(canonical: str) -> str:
    """sqlglot parser dialect for a *canonical* dialect name (after ``normalize_dialect``)."""
    return SQLGLOT_DIALECT.get(canonical, canonical)


__all__ = [
    "CANONICAL_DIALECTS",
    "DDL_FILE_FORMAT_HINTS",
    "DIALECT_ALIASES",
    "SCHEMA_SOURCE_DB2_ALIASES",
    "SCHEMA_SOURCE_MYSQL_ALIASES",
    "SCHEMA_SOURCE_POSTGRES_ALIASES",
    "SQLGLOT_DIALECT",
    "normalize_dialect",
    "sqlglot_dialect_name",
]
