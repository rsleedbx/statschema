"""
Shared DDL-emission helpers used by all dialect emitter modules.

These functions are dialect-aware (they accept a ``dialect`` parameter) but
contain no dialect-specific business logic of their own — they delegate
type and clause decisions to per-dialect lookup tables.
"""

from __future__ import annotations

from ..dialect_registry import normalize_dialect
from ..model import CanonicalColumn, CanonicalTableSchema

# ---------------------------------------------------------------------------
# Identifier quoting conventions
# ---------------------------------------------------------------------------

_BACKTICK_DIALECTS = {"mysql", "databricks"}
_BRACKET_DIALECTS  = {"sqlserver"}
_DQUOTE_DIALECTS   = {"postgres", "oracle", "db2"}


def quote(name: str, dialect: str) -> str:
    if dialect in _BACKTICK_DIALECTS:
        return f"`{name}`"
    if dialect in _BRACKET_DIALECTS:
        return f"[{name}]"
    if dialect in _DQUOTE_DIALECTS:
        return f'"{name}"'
    return name  # pragma: no cover


# ---------------------------------------------------------------------------
# Type string builder — incorporates stored precision / length
# ---------------------------------------------------------------------------

def build_col_type(
    col: CanonicalColumn,
    dialect: str,
    defaults: dict[str, str],
) -> str:
    """Return the SQL type clause for *col* in *dialect*.

    Uses stored length / precision / scale when present; falls back to
    the per-dialect *defaults* mapping.
    """
    key = col.type.lower().strip()

    # ── string / character types ──────────────────────────────────────────
    if key in ("string", "varchar"):
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
            return defaults.get(key, defaults.get("string", "STRING"))
        return defaults.get(key, defaults.get("string", "TEXT"))

    if key == "char":
        if col.length is not None:
            if dialect == "sqlserver":
                return f"NCHAR({col.length})"
            return f"CHAR({col.length})"
        return defaults.get(key, "CHAR(1)")

    # ── binary / blob types ───────────────────────────────────────────────
    if key == "binary":
        if col.length is not None:
            if dialect == "mysql":
                return f"VARBINARY({col.length})"
            if dialect == "postgres":
                return "BYTEA"
            if dialect == "sqlserver":
                return f"VARBINARY({col.length})"
            if dialect == "oracle":
                return f"RAW({col.length})" if col.length <= 2000 else "BLOB"
            if dialect == "db2":
                return f"BLOB({col.length})"
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

    # ── temporal types with fractional-seconds precision ──────────────────
    if key in ("timestamp", "timestamptz", "time", "timetz"):
        base_sql = defaults.get(key, "TIMESTAMP")
        if col.fsp is not None and dialect not in ("databricks",):
            if key == "timestamptz":
                if dialect == "postgres":
                    return f"TIMESTAMP({col.fsp}) WITH TIME ZONE"
                if dialect == "oracle":
                    return f"TIMESTAMP({col.fsp}) WITH TIME ZONE"
                return f"{base_sql}({col.fsp})"
            if key == "timetz" and dialect == "postgres":
                return f"TIME({col.fsp}) WITH TIME ZONE"
            return f"{base_sql}({col.fsp})"
        return base_sql

    return defaults.get(key, defaults.get("string", "TEXT"))


# ---------------------------------------------------------------------------
# Column clause builders
# ---------------------------------------------------------------------------

def auto_increment_clause(col: CanonicalColumn, dialect: str) -> str:
    if not col.auto_increment:
        return ""
    if dialect == "mysql":
        return " AUTO_INCREMENT"
    if dialect == "postgres":
        return ""
    if dialect == "sqlserver":
        return " IDENTITY(1,1)"
    if dialect in ("oracle", "databricks", "db2"):
        return " GENERATED ALWAYS AS IDENTITY"
    return ""  # pragma: no cover


def not_null_clause(col: CanonicalColumn) -> str:
    if col.not_null or col.primary_key:
        return " NOT NULL"
    return ""


_BOOL_TRUE  = {"true",  "1", "yes"}
_BOOL_FALSE = {"false", "0", "no"}


def normalize_default(default: str, col_type: str, dialect: str) -> str:
    if col_type != "boolean":
        return default
    lower = default.strip().lower()
    if dialect in ("sqlserver", "mysql", "oracle"):
        if lower in _BOOL_TRUE:
            return "1"
        if lower in _BOOL_FALSE:
            return "0"
    elif dialect == "db2":
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


def default_clause(col: CanonicalColumn, dialect: str = "") -> str:
    if col.default is not None:
        val = normalize_default(col.default, col.type, dialect) if dialect else col.default
        return f" DEFAULT {val}"
    return ""


def unique_clause(col: CanonicalColumn) -> str:
    if col.unique and not col.primary_key:
        return " UNIQUE"
    return ""


def source_type_comment(col: CanonicalColumn, emitted_type: str, emit_dialect: str) -> str:
    if not col.constraints:
        return ""
    source_type    = col.constraints.get("source_type", "")
    source_dialect = col.constraints.get("source_dialect", "")
    if not source_type:
        st = col.constraints.get("serial_type", "")
        if st:
            source_type    = st.upper()
            source_dialect = source_dialect or "postgres"
    if not source_type:
        return ""

    norm_src  = normalize_dialect(source_dialect) if source_dialect else source_dialect
    norm_emit = normalize_dialect(emit_dialect)

    if norm_src and norm_src == norm_emit:
        return ""

    emitted_norm = emitted_type.strip().upper()
    source_norm  = source_type.strip().upper()
    if emitted_norm == source_norm:
        return ""

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


_INLINE_COMMENT_DIALECTS = frozenset({"mysql", "databricks"})


def col_inline_comment(col: CanonicalColumn, dialect: str) -> str:
    if dialect not in _INLINE_COMMENT_DIALECTS:
        return ""
    if not col.comment:
        return ""
    escaped = col.comment.replace("'", "\\'")
    return f" COMMENT '{escaped}'"


def col_ddl(col: CanonicalColumn, dialect: str, defaults: dict[str, str]) -> str:
    name     = quote(col.name, dialect)
    col_type = build_col_type(col, dialect, defaults)
    auto     = auto_increment_clause(col, dialect)
    if dialect in ("oracle", "db2") and col.auto_increment:
        null = ""
    else:
        null = not_null_clause(col)
    dflt        = default_clause(col, dialect)
    uniq        = unique_clause(col)
    src_comment = source_type_comment(col, col_type, dialect)
    col_comment = col_inline_comment(col, dialect)
    if dialect == "oracle":
        return f"  {name} {col_type}{auto}{dflt}{null}{uniq}{col_comment}{src_comment}"
    return f"  {name} {col_type}{auto}{null}{dflt}{uniq}{col_comment}{src_comment}"


def primary_key_constraint(table: CanonicalTableSchema, dialect: str) -> str | None:
    pk_cols = [c for c in table.columns if c.primary_key]
    if not pk_cols:
        return None
    quoted = ", ".join(quote(c.name, dialect) for c in pk_cols)
    return f"  PRIMARY KEY ({quoted})"
