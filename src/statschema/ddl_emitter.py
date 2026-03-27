"""
Emit CREATE TABLE DDL from canonical schema for target database dialects.

Goal: given a CanonicalTableSchema (parsed from any supported input — YData YAML,
Pipeline YAML, SDV metadata, or a source-system DDL dump), generate DDL that can be
executed on the target system to create the same table structure.  Combined with the
data generation path (canonical → dbldatagen → synthetic rows), this enables a
complete round-trip for testing:

  Source DDL / YAML schema
        ↓  load_schema()
  CanonicalTableSchema
        ├──→ emit_ddl(dialect)              →  CREATE TABLE on target system
        ├──→ emit_column_comments(dialect)  →  COMMENT ON COLUMN … (PG / Oracle)
        └──→ build_dataframe_from_canonical()  →  synthetic rows via dbldatagen

Round-trip fidelity
-------------------
When CanonicalColumn carries length / precision / scale (populated by the DDL parser),
the emitted DDL preserves those values:
  VARCHAR(200) → canonical(string, length=200) → VARCHAR(200) (MySQL)
                                                → CHARACTER VARYING(200) (PostgreSQL)
  DECIMAL(10,2) → canonical(decimal, precision=10, scale=2) → DECIMAL(10,2) (all)

When those fields are absent (e.g. from a YData/Pipeline YAML schema), sensible
dialect defaults are used:
  string, no length  → TEXT (MySQL/PostgreSQL)  NVARCHAR(MAX) (SQL Server)  STRING (Databricks)
  decimal, no prec.  → DECIMAL(18,4) (all)

Column COMMENT handling per dialect
-------------------------------------
CanonicalColumn.comment (from SQL COMMENT clause) is round-tripped as follows:

  mysql / mariadb   → inline  ``COMMENT 'text'``  in the CREATE TABLE body
  databricks        → inline  ``COMMENT 'text'``  in the CREATE TABLE body
  postgres / oracle → separate  ``COMMENT ON COLUMN tbl.col IS 'text';``
                       returned by  emit_column_comments(table, dialect)
  sqlserver / db2   → silently dropped (no standard inline column comment DDL)

Always call emit_column_comments() after emit_ddl() when targeting postgres or oracle
to preserve column comment metadata.

Supported target dialects
--------------------------
  mysql       MySQL 8+ / MariaDB / Aurora MySQL
  postgres    PostgreSQL 13+ / Aurora PostgreSQL / Cloud SQL / Neon (neondatabase/neon)
  sqlserver   SQL Server 2019+ / Azure SQL / Synapse
  oracle      Oracle 19c+
  databricks  Databricks SQL / Unity Catalog Delta tables
  db2         IBM Db2 LUW 11.1+ (Community Edition / on Cloud)
              Note: sqlglot has no Db2 dialect — DDL is emitted via a custom hand-written
              emitter.  ``parse_ddl(dialect="db2")`` uses ANSI SQL parsing.
"""

from __future__ import annotations

from typing import Callable

from .dialect_registry import DIALECT_ALIASES, normalize_dialect
from .model import CanonicalColumn, CanonicalTableSchema

# Per-dialect emitter modules — each exposes emit_table(table, if_not_exists)
from .dialects.mysql.emitter      import emit_table as _emit_mysql,      DEFAULTS as _MYSQL_DEFAULTS
from .dialects.postgres.emitter   import emit_table as _emit_postgres,   DEFAULTS as _POSTGRES_DEFAULTS
from .dialects.sqlserver.emitter  import emit_table as _emit_sqlserver,  DEFAULTS as _SQLSERVER_DEFAULTS
from .dialects.oracle.emitter     import emit_table as _emit_oracle,     DEFAULTS as _ORACLE_DEFAULTS
from .dialects.db2.emitter        import emit_table as _emit_db2,        DEFAULTS as _DB2_DEFAULTS
from .dialects.databricks.emitter import emit_table as _emit_databricks, DEFAULTS as _DATABRICKS_DEFAULTS

# Shared helpers (still needed for emit_column_comments and any callers)
from .dialects._emitter_shared import (
    quote        as _quote,
    build_col_type    as _build_col_type_impl,
    col_ddl      as _col_ddl_impl,
    primary_key_constraint as _primary_key_constraint,
    auto_increment_clause  as _auto_increment_clause,
    not_null_clause        as _not_null_clause,
    normalize_default      as _normalize_default,
    default_clause         as _default_clause,
    unique_clause          as _unique_clause,
    source_type_comment    as _source_type_comment,
    col_inline_comment     as _col_inline_comment,
)

# ---------------------------------------------------------------------------
# Backward-compatible type-map re-exports (used in tests + external code)
# ---------------------------------------------------------------------------

_DEFAULT_MAPS: dict[str, dict[str, str]] = {
    "mysql":      _MYSQL_DEFAULTS,
    "postgres":   _POSTGRES_DEFAULTS,
    "sqlserver":  _SQLSERVER_DEFAULTS,
    "oracle":     _ORACLE_DEFAULTS,
    "databricks": _DATABRICKS_DEFAULTS,
    "db2":        _DB2_DEFAULTS,
}

_EMITTERS: dict[str, Callable[[CanonicalTableSchema, bool], str]] = {
    "mysql":      _emit_mysql,
    "postgres":   _emit_postgres,
    "db2":        _emit_db2,
    "sqlserver":  _emit_sqlserver,
    "oracle":     _emit_oracle,
    "databricks": _emit_databricks,
}

SUPPORTED_DIALECTS: list[str] = sorted(_EMITTERS)


# ---------------------------------------------------------------------------
# Backward-compat thin wrappers for private helpers used in tests
# ---------------------------------------------------------------------------

def _build_col_type(col: CanonicalColumn, dialect: str) -> str:
    return _build_col_type_impl(col, dialect, _DEFAULT_MAPS[dialect])


def _col_ddl(col: CanonicalColumn, dialect: str) -> str:
    return _col_ddl_impl(col, dialect, _DEFAULT_MAPS[dialect])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def emit_ddl(
    table: CanonicalTableSchema,
    dialect: str,
    if_not_exists: bool = True,
) -> str:
    """Return a CREATE TABLE statement for *table* in the given *dialect*.

    Parameters
    ----------
    table:
        Canonical table schema (from ``load_schema()`` or any parser).
    dialect:
        Target SQL dialect.  Supported values (and common aliases):
        ``"mysql"``, ``"postgres"`` (postgresql, neon, neondb),
        ``"sqlserver"`` (mssql, tsql, azure_sql, synapse),
        ``"oracle"``, ``"databricks"`` (spark, delta, dbsql).
    if_not_exists:
        Wrap the statement so it is safe to run on an already-existing table.
        Default ``True``.  Oracle uses a PL/SQL BEGIN/EXCEPTION block because
        Oracle < 23c has no IF NOT EXISTS clause.

    Round-trip fidelity
    -------------------
    When the canonical schema was produced by parsing DDL (i.e. ``parse_ddl()``),
    the emitted DDL preserves VARCHAR lengths, DECIMAL precision/scale, NOT NULL,
    DEFAULT values, AUTO_INCREMENT, UNIQUE, and PRIMARY KEY exactly.
    Emitting to the *same* dialect is idempotent:
    ``emit_ddl(parse_ddl(emit_ddl(t, d), d)[0], d) == emit_ddl(t, d)``

    Examples
    --------
    >>> from src.statschema import load_schema, emit_ddl
    >>> tables = load_schema("my_schema.yaml")
    >>> for t in tables:
    ...     print(emit_ddl(t, "databricks"))
    ...     print(emit_ddl(t, "postgres"))
    """
    normalized = normalize_dialect(dialect)
    if normalized not in _EMITTERS:
        raise ValueError(
            f"Unsupported dialect: {dialect!r}. "
            f"Supported: {SUPPORTED_DIALECTS} "
            f"(aliases: {sorted(DIALECT_ALIASES)})"
        )
    return _EMITTERS[normalized](table, if_not_exists)


def emit_ddl_all(
    tables: list[CanonicalTableSchema],
    dialect: str,
    if_not_exists: bool = True,
    separator: str = "\n\n",
) -> str:
    """Emit DDL for every table in *tables*, joined by *separator*."""
    return separator.join(
        emit_ddl(t, dialect, if_not_exists=if_not_exists) for t in tables
    )


# ---------------------------------------------------------------------------
# Column comment emission (dialects that use separate COMMENT ON statements)
# ---------------------------------------------------------------------------

_COMMENT_ON_DIALECTS = frozenset({"postgres", "oracle"})


def emit_column_comments(
    table: CanonicalTableSchema,
    dialect: str,
) -> list[str]:
    """
    Return a list of ``COMMENT ON COLUMN`` statements for columns that carry a
    ``col.comment`` value (from the SQL COMMENT clause in the source DDL).

    Column comment handling is dialect-dependent:

    +--------------+-----------------------------------------------------------+
    | Dialect      | Behavior                                                  |
    +==============+===========================================================+
    | mysql        | Inline in CREATE TABLE — no statements returned here      |
    | databricks   | Inline in CREATE TABLE — no statements returned here      |
    | postgres     | ``COMMENT ON COLUMN tbl.col IS 'text';``                  |
    | oracle       | ``COMMENT ON COLUMN tbl.col IS 'text';``                  |
    | sqlserver    | Silently dropped (no standard inline comment DDL)         |
    | db2          | Silently dropped                                          |
    +--------------+-----------------------------------------------------------+

    Call this function *after* ``emit_ddl()`` when targeting PostgreSQL or Oracle to
    preserve column comment metadata from the source schema.

    Parameters
    ----------
    table:
        Canonical table schema.
    dialect:
        Target SQL dialect string (same aliases as ``emit_ddl()``).

    Returns
    -------
    List of SQL statement strings (may be empty).  Each string ends with ``;``.
    """
    normalized = normalize_dialect(dialect)
    if normalized not in _COMMENT_ON_DIALECTS:
        return []

    stmts: list[str] = []
    tname = _quote(table.name.upper() if normalized == "oracle" else table.name, normalized)
    for col in table.columns:
        if not col.comment:
            continue
        cname = _quote(col.name.upper() if normalized == "oracle" else col.name, normalized)
        escaped = col.comment.replace("'", "''")
        stmts.append(f"COMMENT ON COLUMN {tname}.{cname} IS '{escaped}';")
    return stmts
