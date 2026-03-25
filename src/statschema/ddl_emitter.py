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


# ---------------------------------------------------------------------------
# Canonical type → dialect default SQL type (used when no precision stored)
# ---------------------------------------------------------------------------

_MYSQL_DEFAULTS: dict[str, str] = {
    "integer":     "INT",
    "long":        "BIGINT",
    "string":      "TEXT",          # no-length default; VARCHAR(N) used when length is set
    "uuid":        "CHAR(36)",      # MySQL has no UUID type; store as fixed-length string
    "float":       "FLOAT",
    "double":      "DOUBLE",
    "boolean":     "TINYINT(1)",
    "timestamp":   "DATETIME",      # DATETIME: no timezone conversion
    "timestamptz": "TIMESTAMP",     # TIMESTAMP: UTC storage, session-TZ display
    "time":        "TIME",
    "timetz":      "TIME",          # MySQL has no time-with-timezone; degrade to TIME
    "date":        "DATE",
    "decimal":     "DECIMAL(18,4)",
    "binary":      "LONGBLOB",      # no-length default; VARBINARY(N) when length is set
}

_POSTGRES_DEFAULTS: dict[str, str] = {
    "integer":     "INTEGER",
    "long":        "BIGINT",
    "string":      "TEXT",
    "uuid":        "UUID",          # PostgreSQL has a native UUID type
    "float":       "REAL",
    "double":      "DOUBLE PRECISION",
    "boolean":     "BOOLEAN",
    "timestamp":   "TIMESTAMP",
    "timestamptz": "TIMESTAMP WITH TIME ZONE",
    "time":        "TIME",
    "timetz":      "TIME WITH TIME ZONE",
    "date":        "DATE",
    "decimal":     "NUMERIC(18,4)",
    "binary":      "BYTEA",         # PostgreSQL always uses BYTEA (no length)
}

_SQLSERVER_DEFAULTS: dict[str, str] = {
    "integer":     "INT",
    "long":        "BIGINT",
    "string":      "NVARCHAR(MAX)",
    "uuid":        "UNIQUEIDENTIFIER",  # SQL Server native UUID type
    # SQL Server REAL (32-bit) and FLOAT (64-bit) both parse to DT.FLOAT via sqlglot,
    # making them indistinguishable in round-trips.  Emit FLOAT for both so that
    # parse(emit(t)) == t holds for all canonical tables.
    "float":       "FLOAT",
    "double":      "FLOAT",
    "boolean":     "BIT",
    "timestamp":   "DATETIME2",
    "timestamptz": "DATETIMEOFFSET",
    "time":        "TIME",
    "timetz":      "TIME",          # SQL Server TIME has no timezone; degrade to TIME
    "date":        "DATE",
    "decimal":     "DECIMAL(18,4)",
    "binary":      "VARBINARY(MAX)", # no-length default; VARBINARY(N) when length is set
}

_ORACLE_DEFAULTS: dict[str, str] = {
    "integer":     "NUMBER(10)",
    "long":        "NUMBER(19)",
    "string":      "VARCHAR2(4000)",  # no-length string; VARCHAR2(n) used when length set
    # CLOB avoided here: Oracle forbids CLOB in GROUP BY/ORDER BY/JOIN predicates
    "uuid":        "CHAR(36)",      # Oracle has no UUID type; store as fixed-length string
    "float":       "FLOAT",
    "double":      "FLOAT(53)",
    "boolean":     "NUMBER(1)",
    "timestamp":   "TIMESTAMP",
    "timestamptz": "TIMESTAMP WITH TIME ZONE",
    "time":        "TIMESTAMP",     # Oracle has no TIME type; degrade to TIMESTAMP
    "timetz":      "TIMESTAMP WITH TIME ZONE",
    "date":        "DATE",
    "decimal":     "NUMBER(18,4)",
    "binary":      "BLOB",          # no-length default; RAW(N) when length ≤ 2000
}

_DB2_DEFAULTS: dict[str, str] = {
    "integer":     "INTEGER",
    "long":        "BIGINT",
    "string":      "CLOB",           # no-length string → CLOB; VARCHAR(n) when length is set
    "uuid":        "CHAR(36)",       # Db2 has no UUID type; store as fixed-length CHAR
    "float":       "REAL",
    "double":      "DOUBLE",
    "boolean":     "BOOLEAN",        # Db2 11.1+
    "timestamp":   "TIMESTAMP",
    "timestamptz": "TIMESTAMP WITH TIME ZONE",
    "time":        "TIME",
    "timetz":      "TIME",           # Db2 has no timezone-aware TIME type; degrade to TIME
    "date":        "DATE",
    "decimal":     "DECIMAL(18,4)",
    "binary":      "BLOB",           # no-length default; BLOB(n) when length is set
}

_DATABRICKS_DEFAULTS: dict[str, str] = {
    "integer":     "INT",
    "long":        "BIGINT",
    "string":      "STRING",        # no length in Databricks STRING
    "uuid":        "STRING",        # Databricks has no UUID type
    "float":       "FLOAT",
    "double":      "DOUBLE",
    "boolean":     "BOOLEAN",
    "timestamp":   "TIMESTAMP_NTZ", # no timezone (Delta 2.0+ / DBR 11.3+)
    "timestamptz": "TIMESTAMP",     # UTC-based
    "time":        "STRING",        # Databricks has no TIME type
    "timetz":      "STRING",
    "date":        "DATE",
    "decimal":     "DECIMAL(18,4)",
    "binary":      "BINARY",        # Databricks native BINARY type
}

_DEFAULT_MAPS: dict[str, dict[str, str]] = {
    "mysql":      _MYSQL_DEFAULTS,
    "postgres":   _POSTGRES_DEFAULTS,
    "sqlserver":  _SQLSERVER_DEFAULTS,
    "oracle":     _ORACLE_DEFAULTS,
    "databricks": _DATABRICKS_DEFAULTS,
    "db2":        _DB2_DEFAULTS,
}

# Identifier quoting conventions
_BACKTICK_DIALECTS  = {"mysql", "databricks"}
_BRACKET_DIALECTS   = {"sqlserver"}
_DQUOTE_DIALECTS    = {"postgres", "oracle", "db2"}


def _quote(name: str, dialect: str) -> str:
    if dialect in _BACKTICK_DIALECTS:
        return f"`{name}`"
    if dialect in _BRACKET_DIALECTS:
        return f"[{name}]"
    if dialect in _DQUOTE_DIALECTS:
        return f'"{name}"'
    return name  # pragma: no cover  — all supported dialects are enumerated above


# ---------------------------------------------------------------------------
# Type string builder — incorporates stored precision / length
# ---------------------------------------------------------------------------

def _build_col_type(col: CanonicalColumn, dialect: str) -> str:
    """
    Return the SQL type clause for *col* in *dialect*, using stored
    length / precision / scale when present, else dialect defaults.
    """
    key = col.type.lower().strip()
    defaults = _DEFAULT_MAPS[dialect]

    # ── string / character types ──────────────────────────────────────────
    if key == "string":
        if col.length is not None:
            if dialect == "mysql":
                return f"VARCHAR({col.length})"
            if dialect == "postgres":
                return f"CHARACTER VARYING({col.length})"
            if dialect == "sqlserver":
                return f"NVARCHAR({col.length})"
            if dialect == "oracle":
                return f"VARCHAR2({col.length})"
            if dialect == "db2":
                return f"VARCHAR({col.length})"
            # Databricks: STRING has no length
            return defaults.get(key, "STRING")
        return defaults.get(key, "TEXT")

    # ── binary / blob types ───────────────────────────────────────────────
    if key == "binary":
        if col.length is not None:
            if dialect == "mysql":
                return f"VARBINARY({col.length})"
            if dialect == "postgres":
                return "BYTEA"            # PostgreSQL BYTEA has no declared length
            if dialect == "sqlserver":
                return f"VARBINARY({col.length})"
            if dialect == "oracle":
                # Oracle RAW supports up to 2000 bytes; larger → BLOB
                return f"RAW({col.length})" if col.length <= 2000 else "BLOB"
            if dialect == "db2":
                return f"BLOB({col.length})"
            # Databricks: BINARY type (no length in Databricks DDL)
            return "BINARY"
        return defaults.get(key, "VARBINARY(MAX)")

    # ── decimal / numeric types ───────────────────────────────────────────
    if key == "decimal":
        if col.precision is not None:
            s = col.scale if col.scale is not None else 0
            if dialect == "oracle":
                return f"NUMBER({col.precision},{s})"
            if dialect == "postgres":
                return f"NUMERIC({col.precision},{s})"
            return f"DECIMAL({col.precision},{s})"
        return defaults.get(key, "DECIMAL(18,4)")

    # ── integer: PostgreSQL SERIAL shorthand when auto_increment ──────────
    if key == "integer" and col.auto_increment and dialect == "postgres":
        return "SERIAL"
    if key == "long" and col.auto_increment and dialect == "postgres":
        return "BIGSERIAL"

    # ── temporal types with fractional-seconds precision (fsp) ───────────
    # Emit fsp suffix when stored; fall back to no-fsp default otherwise.
    if key in ("timestamp", "timestamptz", "time", "timetz"):
        base_sql = defaults.get(key, "TIMESTAMP")
        if col.fsp is not None and dialect not in ("databricks",):
            # Databricks does not support fsp in DDL (TIMESTAMP_NTZ / TIMESTAMP only)
            if key == "timestamptz":
                if dialect == "postgres":
                    return f"TIMESTAMP({col.fsp}) WITH TIME ZONE"
                if dialect == "oracle":
                    return f"TIMESTAMP({col.fsp}) WITH TIME ZONE"
                # mysql / sqlserver: type name + (fsp)
                return f"{base_sql}({col.fsp})"
            if key == "timetz" and dialect == "postgres":
                return f"TIME({col.fsp}) WITH TIME ZONE"
            # timestamp / time: type name + (fsp) works for mysql, postgres, sqlserver, oracle
            return f"{base_sql}({col.fsp})"
        return base_sql

    return defaults.get(key, defaults.get("string", "TEXT"))


# ---------------------------------------------------------------------------
# Column DDL line builder
# ---------------------------------------------------------------------------

def _auto_increment_clause(col: CanonicalColumn, dialect: str) -> str:
    """Return dialect auto-increment clause — empty for PG (uses SERIAL type)."""
    if not col.auto_increment:
        return ""
    if dialect == "mysql":
        return " AUTO_INCREMENT"
    if dialect == "postgres":
        return ""  # expressed via SERIAL / BIGSERIAL type
    if dialect == "sqlserver":
        return " IDENTITY(1,1)"
    if dialect in ("oracle", "databricks", "db2"):
        return " GENERATED ALWAYS AS IDENTITY"
    return ""  # pragma: no cover  — all supported dialects are enumerated above


def _not_null_clause(col: CanonicalColumn) -> str:
    if col.not_null or col.primary_key:
        return " NOT NULL"
    return ""


_BOOL_TRUE  = {"true",  "1", "yes"}
_BOOL_FALSE = {"false", "0", "no"}


def _normalize_default(default: str, col_type: str, dialect: str) -> str:
    """
    Translate boolean literals and other cross-dialect default expressions
    to the form the target dialect accepts.

    - SQL Server BIT:      TRUE/true → 1,  FALSE/false → 0
    - MySQL TINYINT(1):    true/false are accepted by MySQL 8 but not universally,
                           normalize to 1/0 for safety across 5.7 and 8.x.
    - Oracle NUMBER(1):    same as SQL Server/MySQL — use 1/0.
    - PostgreSQL BOOLEAN:  1/0 → TRUE/FALSE (PG rejects bare integers for BOOLEAN)
    - All others:          return default unchanged.
    """
    if col_type != "boolean":
        return default
    lower = default.strip().lower()
    if dialect in ("sqlserver", "mysql", "oracle"):
        if lower in _BOOL_TRUE:
            return "1"
        if lower in _BOOL_FALSE:
            return "0"
    elif dialect == "db2":
        # Db2 BOOLEAN accepts TRUE/FALSE literals (Db2 11.1+)
        if lower in _BOOL_TRUE or lower == "1":
            return "TRUE"
        if lower in _BOOL_FALSE or lower == "0":
            return "FALSE"
    elif dialect == "postgres":
        if lower in _BOOL_TRUE or lower == "1":
            return "TRUE"
        if lower in _BOOL_FALSE or lower == "0":
            return "FALSE"
    return default


def _default_clause(col: CanonicalColumn, dialect: str = "") -> str:
    if col.default is not None:
        val = _normalize_default(col.default, col.type, dialect) if dialect else col.default
        return f" DEFAULT {val}"
    return ""


def _unique_clause(col: CanonicalColumn) -> str:
    """Inline UNIQUE — only when not already part of PK (avoids redundancy)."""
    if col.unique and not col.primary_key:
        return " UNIQUE"
    return ""


def _source_type_comment(col: CanonicalColumn, emitted_type: str, emit_dialect: str) -> str:
    """
    Return a SQL inline comment recording the original source type when:
      1. The column has a stored source_type (set during parsing), AND
      2. The emitted dialect differs from the source dialect (cross-dialect transpile), AND
      3. The emitted type string is not semantically equivalent to the original.

    Only fires on cross-dialect transpile so same-dialect round-trips remain idempotent.

    Examples
    --------
    TIMETZ (postgres) → STRING (databricks)          → "  /* originally TIMETZ (postgres) */"
    DATETIMEOFFSET (sqlserver) → TIMESTAMP (mysql)   → "  /* originally DATETIMEOFFSET (sqlserver) */"
    NUMBER(10) (oracle) → BIGINT (postgres)          → "  /* originally NUMBER(10) (oracle) */"
    TINYBLOB (mysql) → LONGBLOB (mysql)              → ""  (same dialect, no annotation)
    VARCHAR(100) (postgres) → VARCHAR(100) (mysql)   → ""  (types match, no annotation)
    """
    if not col.constraints:
        return ""
    source_type    = col.constraints.get("source_type", "")
    source_dialect = col.constraints.get("source_dialect", "")
    # serial_type (SERIAL/SMALLSERIAL/BIGSERIAL) is stored separately; use it as source_type
    # when no explicit source_type is present, since SERIAL emits very differently cross-dialect.
    if not source_type:
        st = col.constraints.get("serial_type", "")
        if st:
            source_type    = st.upper()
            source_dialect = source_dialect or "postgres"
    if not source_type:
        return ""

    norm_src  = normalize_dialect(source_dialect) if source_dialect else source_dialect
    norm_emit = normalize_dialect(emit_dialect)

    # Suppress for same-dialect: round-trips must be idempotent
    if norm_src and norm_src == norm_emit:
        return ""

    emitted_norm = emitted_type.strip().upper()
    source_norm  = source_type.strip().upper()
    if emitted_norm == source_norm:
        return ""

    # Suppress when the emitted type is an accepted alias for the same concept
    _EQUIVALENT_PAIRS = {
        frozenset({"TIMESTAMP WITH TIME ZONE", "DATETIMEOFFSET"}),
        frozenset({"TIMESTAMP WITH TIME ZONE", "TIMESTAMPTZ"}),
        frozenset({"TIME WITH TIME ZONE", "TIMETZ"}),
        frozenset({"BIGINT", "INT8"}),
        frozenset({"CHARACTER VARYING", "VARCHAR"}),
    }
    pair = frozenset({emitted_norm.split("(")[0].strip(), source_norm.split("(")[0].strip()})
    if pair in _EQUIVALENT_PAIRS:
        return ""

    dialect_note = f" ({source_dialect})" if source_dialect else ""
    return f"  /* originally {source_type}{dialect_note} */"


# Dialects that support inline COMMENT 'text' inside the CREATE TABLE column list.
_INLINE_COMMENT_DIALECTS = frozenset({"mysql", "databricks"})


def _col_inline_comment(col: CanonicalColumn, dialect: str) -> str:
    """Return the inline COMMENT clause for dialects that support it, else ''."""
    if dialect not in _INLINE_COMMENT_DIALECTS:
        return ""
    if not col.comment:
        return ""
    escaped = col.comment.replace("'", "\\'")
    return f" COMMENT '{escaped}'"


def _col_ddl(col: CanonicalColumn, dialect: str) -> str:
    name     = _quote(col.name, dialect)
    col_type = _build_col_type(col, dialect)
    auto     = _auto_increment_clause(col, dialect)
    # Oracle and Db2 IDENTITY columns are implicitly NOT NULL; the constraint
    # clause after GENERATED ALWAYS AS IDENTITY causes a syntax error on both.
    if dialect in ("oracle", "db2") and col.auto_increment:
        null = ""
    else:
        null = _not_null_clause(col)
    default      = _default_clause(col, dialect)
    unique       = _unique_clause(col)
    src_comment  = _source_type_comment(col, col_type, dialect)
    col_comment  = _col_inline_comment(col, dialect)
    # Oracle requires: type [DEFAULT value] [NOT NULL] — DEFAULT must precede NOT NULL.
    # All other dialects accept either order; keep the standard type+null+default for them.
    if dialect == "oracle":
        return f"  {name} {col_type}{auto}{default}{null}{unique}{col_comment}{src_comment}"
    return f"  {name} {col_type}{auto}{null}{default}{unique}{col_comment}{src_comment}"


def _primary_key_constraint(table: CanonicalTableSchema, dialect: str) -> str | None:
    pk_cols = [c for c in table.columns if c.primary_key]
    if not pk_cols:
        return None
    quoted = ", ".join(_quote(c.name, dialect) for c in pk_cols)
    return f"  PRIMARY KEY ({quoted})"


# ---------------------------------------------------------------------------
# Per-dialect CREATE TABLE emitters
# ---------------------------------------------------------------------------

def _emit_mysql(table: CanonicalTableSchema, if_not_exists: bool) -> str:
    ine   = " IF NOT EXISTS" if if_not_exists else ""
    tname = _quote(table.name, "mysql")
    lines = [_col_ddl(c, "mysql") for c in table.columns]
    pk    = _primary_key_constraint(table, "mysql")
    if pk:
        lines.append(pk)
    body  = ",\n".join(lines)
    return f"CREATE TABLE{ine} {tname} (\n{body}\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;"


def _emit_postgres(table: CanonicalTableSchema, if_not_exists: bool) -> str:
    ine   = " IF NOT EXISTS" if if_not_exists else ""
    tname = _quote(table.name, "postgres")
    lines = [_col_ddl(c, "postgres") for c in table.columns]
    pk    = _primary_key_constraint(table, "postgres")
    if pk:
        lines.append(pk)
    body  = ",\n".join(lines)
    return f"CREATE TABLE{ine} {tname} (\n{body}\n);"


def _emit_sqlserver(table: CanonicalTableSchema, if_not_exists: bool) -> str:
    tname = _quote(table.name, "sqlserver")
    lines = [_col_ddl(c, "sqlserver") for c in table.columns]
    pk    = _primary_key_constraint(table, "sqlserver")
    if pk:
        lines.append(pk)
    body  = ",\n".join(lines)
    ddl   = f"CREATE TABLE {tname} (\n{body}\n);"
    if if_not_exists:
        raw  = table.name.replace("'", "''")
        ddl  = (
            f"IF OBJECT_ID(N'{raw}', N'U') IS NULL\nBEGIN\n"
            + "  " + ddl.replace("\n", "\n  ").rstrip()
            + "\nEND;"
        )
    return ddl


def _emit_oracle(table: CanonicalTableSchema, if_not_exists: bool) -> str:
    tname = _quote(table.name.upper(), "oracle")
    lines = [_col_ddl(c, "oracle") for c in table.columns]
    pk    = _primary_key_constraint(table, "oracle")
    if pk:
        lines.append(pk)
    body  = ",\n".join(lines)
    ddl   = f"CREATE TABLE {tname} (\n{body}\n);"
    if if_not_exists:
        escaped = ddl.replace("'", "''").rstrip(";")
        ddl = (
            f"BEGIN\n"
            f"  EXECUTE IMMEDIATE '{escaped}';\n"
            f"EXCEPTION\n"
            f"  WHEN OTHERS THEN\n"
            f"    IF SQLCODE != -955 THEN RAISE; END IF; -- ORA-00955: table already exists\n"
            f"END;"
        )
    return ddl


def _emit_db2(table: CanonicalTableSchema, if_not_exists: bool) -> str:
    # Db2 normalises unquoted names to uppercase internally.
    # Quote with double-quotes (ANSI SQL) to preserve case exactly.
    tname = _quote(table.name.upper(), "db2")
    lines = [_col_ddl(c, "db2") for c in table.columns]
    pk    = _primary_key_constraint(table, "db2")
    if pk:
        lines.append(pk)
    body  = ",\n".join(lines)
    ddl   = f"CREATE TABLE {tname} (\n{body}\n);"
    if if_not_exists:
        # Db2 11.1+ supports CREATE TABLE IF NOT EXISTS
        ddl = f"CREATE TABLE IF NOT EXISTS {tname} (\n{body}\n);"
    return ddl


def _emit_databricks(table: CanonicalTableSchema, if_not_exists: bool) -> str:
    ine   = " IF NOT EXISTS" if if_not_exists else ""
    tname = _quote(table.name, "databricks")
    lines = [_col_ddl(c, "databricks") for c in table.columns]
    pk    = _primary_key_constraint(table, "databricks")
    if pk:
        lines.append(pk)
    body  = ",\n".join(lines)
    return f"CREATE TABLE{ine} {tname} (\n{body}\n)\nUSING DELTA;"


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

# Dialects that use "COMMENT ON COLUMN tbl.col IS 'text';" (separate statement)
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

    Examples
    --------
    ::

        ddl      = emit_ddl(table, "postgres")
        comments = emit_column_comments(table, "postgres")
        # Execute ddl first, then each statement in comments.
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
