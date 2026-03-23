"""
Parse SQL DDL (CREATE TABLE) into canonical schema using sqlglot.

Public API (unchanged):
    parse_ddl(sql, dialect=None)  → list[CanonicalTableSchema]
    parse_ddl_file(path, dialect) → list[CanonicalTableSchema]

Architecture
------------
sqlglot parses the DDL into a typed AST; we walk the AST for column defs,
constraints, PKs, and FKs.  Our semantic correction layer (MONEY precision,
NUMBER(p) widening, UNSIGNED widening, TINYINT(1)→boolean) is preserved
because sqlglot is a syntactic transpiler and does not encode these migration
semantics.

Supported source dialects (auto-detected or explicit):
  mysql       mysqldump --no-data / Aurora MySQL
  postgres    pg_dump -s / Cloud SQL PG / Neon (neondatabase/neon)
  sqlserver   mssql-scripter / SSMS Generate Scripts
  oracle      Oracle expdp DDL / SQL*Plus spool (first-class, not a fallback)
  databricks  Databricks SQL / Unity Catalog Delta

Public type-map dicts (MYSQL/POSTGRES/SQLSERVER_TYPE_TO_CANONICAL) are kept
because the test suite imports and validates them for coverage reporting.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import sqlglot

from .dialect_registry import SQLGLOT_DIALECT, normalize_dialect, sqlglot_dialect_name
from sqlglot import exp
from sqlglot.expressions import DataType as SgDataType

from .model import CanonicalColumn, CanonicalForeignKey, CanonicalTableSchema, GenerationRule


# ---------------------------------------------------------------------------
# Public type maps (kept for test-suite coverage validation)
# ---------------------------------------------------------------------------

MYSQL_TYPE_TO_CANONICAL: dict[str, str] = {
    "tinyint": "integer",      "smallint": "integer",   "mediumint": "integer",
    "int": "integer",          "integer": "integer",    "bigint": "long",
    "decimal": "decimal",      "numeric": "decimal",
    "float": "float",          "double": "double",
    # sqlglot normalises MySQL REAL to DT.FLOAT (same as FLOAT); maps to "float"
    "real": "float",
    "bit": "boolean",          "boolean": "boolean",    "bool": "boolean",
    "char": "string",          "varchar": "string",     "tinytext": "string",
    "text": "string",          "mediumtext": "string",  "longtext": "string",
    "json": "string",          "jsonb": "string",       "string": "string",
    "enum": "string",          "set": "string",
    "binary": "binary",        "varbinary": "binary",
    "tinyblob": "binary",      "blob": "binary",        "mediumblob": "binary",
    "longblob": "binary",
    "number": "decimal",       "varchar2": "string",    "nvarchar2": "string",
    "clob": "string",          "nclob": "string",       "raw": "binary",
    "date": "date",            "datetime": "timestamp",
    "timestamp": "timestamptz","timestamp_ntz": "timestamp",
    "time": "time",            "year": "integer",
}

POSTGRES_TYPE_TO_CANONICAL: dict[str, str] = {
    "smallint": "integer",     "integer": "integer",    "int": "integer",
    "int2": "integer",         "int4": "integer",
    "bigint": "long",          "int8": "long",
    "decimal": "decimal",      "numeric": "decimal",    "money": "decimal",
    "real": "float",           "float4": "float",
    "double precision": "double", "float8": "double",
    "boolean": "boolean",      "bool": "boolean",
    "character varying": "string", "varchar": "string", "character": "string",
    "char": "string",          "text": "string",
    "json": "string",          "jsonb": "string",       "uuid": "string",
    "xml": "string",           "cidr": "string",        "inet": "string",
    "macaddr": "string",       "macaddr8": "string",    "interval": "string",
    "bytea": "binary",         "bit": "binary",
    "bit varying": "binary",   "varbit": "binary",
    "date": "date",            "timestamp": "timestamp",
    "timestamp without time zone": "timestamp",
    "timestamp with time zone": "timestamptz",
    "timestamptz": "timestamptz",
    "time": "time",            "time without time zone": "time",
    "time with time zone": "timetz", "timetz": "timetz",
    "serial": "integer",       "smallserial": "integer", "bigserial": "long",
}

ORACLE_TYPE_TO_CANONICAL: dict[str, str] = {
    # ── Numeric ────────────────────────────────────────────────────────────
    "number":     "decimal",    # NUMBER(p,s); NUMBER(p) widened → integer/long
    "float":      "float",      # Oracle FLOAT(p) = IEEE 754 binary, p in bits
    "binary_float":  "float",
    "binary_double": "double",
    # ── String ─────────────────────────────────────────────────────────────
    "varchar2":   "string",
    "nvarchar2":  "string",
    "char":       "string",
    "nchar":      "string",
    "clob":       "string",     # sqlglot normalises Oracle CLOB → DT.TEXT
    "nclob":      "string",     # sqlglot maps to USERDEFINED kind='NCLOB'
    "long":       "string",     # Oracle LONG (deprecated LOB)
    "xmltype":    "string",
    # ── Binary ─────────────────────────────────────────────────────────────
    "raw":        "binary",     # RAW(n): fixed binary up to 2000 bytes
    "long raw":   "binary",
    "blob":       "binary",
    "bfile":      "binary",     # external file reference
    # ── Date / Time ─────────────────────────────────────────────────────────
    # Oracle DATE stores both date and time (unlike MySQL DATE).
    # We map to "date" for migration compatibility; use TIMESTAMP when time matters.
    "date":       "date",
    "timestamp":              "timestamp",
    "timestamp with time zone":        "timestamptz",
    "timestamp with local time zone":  "timestamptz",
    "interval year to month": "string",
    "interval day to second":  "string",
    # ── Boolean (no native type; conventionally NUMBER(1)) ──────────────────
    # NUMBER(1) handled by NUMBER(p) widening → "integer"; emitter maps integer→NUMBER(10)
}

SQLSERVER_TYPE_TO_CANONICAL: dict[str, str] = {
    "tinyint": "integer",      "smallint": "integer",   "int": "integer",
    "bigint": "long",
    "decimal": "decimal",      "numeric": "decimal",
    "money": "decimal",        "smallmoney": "decimal",
    # sqlglot normalises both SS REAL and FLOAT to DT.FLOAT — can't distinguish;
    # map both to "float" so parse→emit round-trips are stable.
    "float": "float",          "real": "float",
    "bit": "boolean",
    "char": "string",          "varchar": "string",     "nchar": "string",
    "nvarchar": "string",      "text": "string",        "ntext": "string",
    "xml": "string",           "sql_variant": "string", "hierarchyid": "string",
    "uniqueidentifier": "string",
    "binary": "binary",        "varbinary": "binary",   "image": "binary",
    "rowversion": "binary",    "timestamp": "binary",   # ⚠ SQL Server TIMESTAMP = ROWVERSION
    "geography": "binary",     "geometry": "binary",
    "date": "date",            "datetime": "timestamp", "datetime2": "timestamp",
    "smalldatetime": "timestamp", "datetimeoffset": "timestamptz", "time": "time",
}


# ---------------------------------------------------------------------------
# sqlglot DType → canonical type
# This is the runtime mapping; keeps in sync with the string maps above.
# ---------------------------------------------------------------------------

DT = SgDataType.Type

_DTYPE_TO_CANONICAL: dict[DT, str] = {
    # ── Integer ──────────────────────────────────────────────────────────────
    DT.TINYINT:       "integer",
    DT.UTINYINT:      "integer",    # TINYINT UNSIGNED (fits INT32)
    DT.SMALLINT:      "integer",
    DT.USMALLINT:     "integer",    # SMALLINT UNSIGNED (fits INT32)
    DT.MEDIUMINT:     "integer",
    DT.INT:           "integer",
    DT.UINT:          "long",       # INT UNSIGNED max > INT32 → widen to BIGINT
    DT.BIGINT:        "long",
    DT.UBIGINT:       "decimal",    # BIGINT UNSIGNED max > INT64 → DECIMAL(20,0)
    DT.UMEDIUMINT:    "integer",    # MEDIUMINT UNSIGNED fits INT32
    DT.INT128:        "decimal",
    DT.INT256:        "decimal",
    DT.YEAR:          "integer",
    # ── PG serial shorthands (DType carries auto_increment semantic) ─────────
    DT.SERIAL:        "integer",
    DT.SMALLSERIAL:   "integer",
    DT.BIGSERIAL:     "long",
    # ── Floating point ───────────────────────────────────────────────────────
    DT.FLOAT:         "float",      # sqlglot normalises REAL → FLOAT too
    DT.DOUBLE:        "double",
    # ── Fixed point ──────────────────────────────────────────────────────────
    DT.DECIMAL:       "decimal",    # sqlglot normalises NUMERIC → DECIMAL
    DT.MONEY:         "decimal",    # fixed precision injected below
    DT.SMALLMONEY:    "decimal",    # fixed precision injected below
    # ── Boolean ──────────────────────────────────────────────────────────────
    DT.BOOLEAN:       "boolean",
    DT.BIT:           "boolean",
    # ── String ───────────────────────────────────────────────────────────────
    DT.TEXT:          "string",
    DT.TINYTEXT:      "string",
    DT.MEDIUMTEXT:    "string",
    DT.LONGTEXT:      "string",
    DT.VARCHAR:       "string",
    DT.NVARCHAR:      "string",
    DT.CHAR:          "string",
    DT.NCHAR:         "string",
    DT.JSON:          "string",
    DT.JSONB:         "string",
    DT.UUID:          "uuid",        # UNIQUEIDENTIFIER / UUID
    DT.XML:           "string",
    DT.INET:          "string",
    DT.IPADDRESS:     "string",
    DT.INTERVAL:      "string",
    DT.ENUM:          "string",
    # DT.CLOB → sqlglot normalises Oracle CLOB to DT.TEXT (already covered)
    # DT.NCLOB → sqlglot maps to USERDEFINED; handled in _dtype_info USERDEFINED branch
    DT.VARIANT:       "string",     # SQL Server SQL_VARIANT
    # ── Binary ───────────────────────────────────────────────────────────────
    DT.BINARY:        "binary",
    DT.VARBINARY:     "binary",     # also BYTEA (sqlglot normalises PG BYTEA → VARBINARY)
    DT.BLOB:          "binary",
    DT.TINYBLOB:      "binary",
    DT.MEDIUMBLOB:    "binary",
    DT.LONGBLOB:      "binary",
    DT.IMAGE:         "binary",
    DT.ROWVERSION:    "binary",     # SQL Server ROWVERSION / TIMESTAMP (8-byte row counter)
    DT.GEOGRAPHY:     "binary",     # SQL Server spatial type
    DT.GEOMETRY:      "binary",     # SQL Server spatial type
    # ── Date / Time ──────────────────────────────────────────────────────────
    DT.DATE:          "date",
    DT.TIME:          "time",
    DT.TIMETZ:        "timetz",
    DT.DATETIME:      "timestamp",  # MySQL DATETIME (no TZ) / SQL Server DATETIME
    DT.DATETIME2:     "timestamp",  # SQL Server DATETIME2
    DT.SMALLDATETIME: "timestamp",
    # sqlglot normalises MySQL TIMESTAMP → TIMESTAMPTZ (UTC storage); PG TIMESTAMP stays TIMESTAMP
    DT.TIMESTAMP:     "timestamp",  # PG TIMESTAMP WITHOUT TIME ZONE
    DT.TIMESTAMPTZ:   "timestamptz",# MySQL TIMESTAMP (UTC) / PG TIMESTAMPTZ / SS DATETIMEOFFSET (via sqlglot)
    DT.TIMESTAMPNTZ:  "timestamp",  # Databricks TIMESTAMP_NTZ
    # DT.DATETIMEOFFSET → sqlglot normalises SS DATETIMEOFFSET to DT.TIMESTAMPTZ (already covered)
}

# DTypes whose single numeric param is fractional-seconds precision (fsp)
# Note: DATETIMEOFFSET doesn't exist as its own DType; sqlglot normalises it to TIMESTAMPTZ
_TEMPORAL_DTYPES = {
    DT.DATETIME, DT.DATETIME2,
    DT.TIMESTAMP, DT.TIMESTAMPTZ, DT.TIMESTAMPNTZ, DT.SMALLDATETIME,
    DT.TIME, DT.TIMETZ,
}

# DTypes whose single numeric param is byte/char length
_LENGTH_DTYPES = {
    DT.VARCHAR, DT.NVARCHAR, DT.CHAR, DT.NCHAR,
    DT.BINARY, DT.VARBINARY,
}

# DTypes that are PG serial auto-increment shorthands
_SERIAL_DTYPES = {DT.SERIAL, DT.SMALLSERIAL, DT.BIGSERIAL}

# Fixed precision+scale for currency types (no parens in DDL)
_FIXED_MONEY_PRECISION: dict[DT, tuple[int, int]] = {
    DT.MONEY:      (19, 4),   # SQL Server MONEY  → DECIMAL(19,4)
    DT.SMALLMONEY: (10, 4),   # SQL Server SMALLMONEY → DECIMAL(10,4)
}

# ---------------------------------------------------------------------------
# Dialect auto-detection (heuristic on raw SQL text)
# ---------------------------------------------------------------------------

def _detect_dialect(sql: str) -> str:
    """
    Heuristic dialect detection from raw SQL text.

    Checks high-signal keywords in order of specificity.  Oracle is now
    detected before the generic integer/bigint fallback so that Oracle DDL
    is parsed with sqlglot's oracle dialect (supporting RAW, NCLOB, NUMBER,
    VARCHAR2, etc. natively) rather than being pre-processed as MySQL.
    """
    s = sql[:4000].lower()
    # Oracle-specific keywords checked FIRST — VARCHAR2/NVARCHAR2 are unique to Oracle
    # and would otherwise be swallowed by the generic postgres/mysql heuristics.
    # number( (with immediate paren) avoids matching column names like "number_of_…".
    if ("varchar2" in s or "nvarchar2" in s or "nclob" in s
            or " raw(" in s or "\traw(" in s or "\nraw(" in s
            or ("number(" in s and "numeric(" not in s)):
        return "oracle"
    if ("character varying" in s or "timestamp with time zone" in s
            or "serial" in s or "timetz" in s):
        return "postgres"
    if ("[dbo]" in s or "nvarchar" in s or "nchar" in s
            or "uniqueidentifier" in s or "datetimeoffset" in s
            or "datetime2" in s or "rowversion" in s
            or "smallmoney" in s or "hierarchyid" in s):
        return "sqlserver"
    if "`" in s or "auto_increment" in s or "engine=" in s:
        return "mysql"
    if "integer" in s or "bigint" in s:
        return "postgres"
    return "mysql"


# ---------------------------------------------------------------------------
# Core: column DataType node → (canonical_type, length, precision, scale, fsp, unsigned)
# ---------------------------------------------------------------------------

def _extract_params(dt: SgDataType) -> list[int]:
    """Extract numeric parameters from a DataType node, silently skipping MAX/non-numeric."""
    result = []
    for p in (dt.expressions or []):
        raw = str(p).strip()
        try:
            result.append(int(raw))
        except ValueError:
            pass  # MAX, charset names, etc.
    return result


def _source_type_str(dt: SgDataType, dialect: str) -> str:
    """
    Return a clean, human-readable SQL type string for the original column type,
    e.g. "TIMESTAMP WITH TIME ZONE", "DATETIMEOFFSET", "NUMBER(10)", "TIMETZ".

    Used to populate constraints["source_type"] so the canonical YAML (and any
    downstream emitted DDL) can record what the original type was, even when the
    canonical model normalises it to a wider or less-specific type.
    """
    sg_dialect = sqlglot_dialect_name(dialect) or None
    try:
        s = dt.sql(dialect=sg_dialect).strip()
        # Strip redundant parentheses on bare types: "TIMESTAMP()" → "TIMESTAMP"
        if s.endswith("()"):  # pragma: no cover
            s = s[:-2].strip()  # pragma: no cover
        return s.upper() if s else dt.this.value.upper()  # pragma: no cover
    except Exception:  # pragma: no cover
        return dt.this.value.upper()  # pragma: no cover


def _dtype_info(
    dt: SgDataType,
    dialect: str,
) -> tuple[str, Optional[int], Optional[int], Optional[int], Optional[int], bool, bool]:
    """
    Map a sqlglot DataType node to canonical fields.

    Returns
    -------
    (canonical_type, length, precision, scale, fsp, unsigned, is_serial)
    """
    dtype = dt.this
    params = _extract_params(dt)

    unsigned = dtype in {DT.UINT, DT.USMALLINT, DT.UTINYINT, DT.UBIGINT, DT.UMEDIUMINT}
    is_serial = dtype in _SERIAL_DTYPES
    length = precision = scale = fsp = None

    # ── USERDEFINED types (Oracle RAW, PG VARBIT/CIDR/MACADDR, etc.) ─────────
    if dtype == DT.USERDEFINED:
        # kind may be an Identifier node or a plain string; normalise to str
        kind_arg = dt.args.get("kind")
        if hasattr(kind_arg, "name"):
            kind = (kind_arg.name or "").strip().lower()   # sqlglot Identifier
        else:
            kind = str(kind_arg or "").strip().lower()
        if kind == "raw":
            return "binary", params[0] if params else None, None, None, None, False, False
        if kind in ("varbit", "bit varying"):              # PG BIT VARYING
            return "binary", params[0] if params else None, None, None, None, False, False
        if kind in ("cidr", "inet", "macaddr", "macaddr8"):
            return "string", None, None, None, None, False, False
        if kind in ("geography", "geometry"):  # pragma: no cover
            return "binary", None, None, None, None, False, False  # pragma: no cover
        # nclob, varchar2 (from non-oracle dialect), hierarchyid, etc.
        return "string", None, None, None, None, False, False

    # ── Dialect-specific overrides ─────────────────────────────────────────
    # PG BIT(n) with n>1 is a bit-string (binary), not a boolean
    if dtype == DT.BIT and dialect == "postgres" and params:
        return "binary", params[0], None, None, None, False, False

    canonical = _DTYPE_TO_CANONICAL.get(dtype, "string")

    # ── TINYINT(1) → boolean (MySQL de-facto BOOLEAN alias) ─────────────────
    if dtype == DT.TINYINT and params == [1] and dialect == "mysql":
        return "boolean", None, None, None, None, False, False

    # ── UBIGINT → decimal(20,0) (BIGINT UNSIGNED overflow-safe widening) ────
    if dtype == DT.UBIGINT:
        return "decimal", None, 20, 0, None, True, False

    # ── MONEY / SMALLMONEY: inject fixed precision (dialect-sensitive) ───────
    if dtype == DT.MONEY:
        p, s = (19, 2) if dialect == "postgres" else (19, 4)
        return "decimal", None, p, s, None, False, False
    if dtype == DT.SMALLMONEY:
        return "decimal", None, 10, 4, None, False, False

    # ── Temporal FSP ─────────────────────────────────────────────────────────
    if dtype in _TEMPORAL_DTYPES and params:
        fsp = params[0]
        return canonical, None, None, None, fsp, False, is_serial

    # ── String / char length ─────────────────────────────────────────────────
    if dtype in _LENGTH_DTYPES and params:
        length = params[0]
        return canonical, length, None, None, None, unsigned, is_serial

    # ── Decimal / numeric precision (+ optional scale) ───────────────────────
    if canonical == "decimal" and params:
        precision = params[0]
        scale = params[1] if len(params) > 1 else None

        # Oracle NUMBER(p) without scale — widen to integer / long if safe ──
        if dtype == DT.DECIMAL and scale is None:
            if precision <= 9:
                return "integer", None, None, None, None, False, is_serial
            if precision <= 18:
                return "long", None, None, None, None, False, is_serial
            scale = 0  # NUMBER(p) with p>18 → DECIMAL(p,0)

        return canonical, None, precision, scale, None, unsigned, is_serial

    return canonical, length, precision, scale, fsp, unsigned, is_serial


# ---------------------------------------------------------------------------
# FK extraction helpers
# ---------------------------------------------------------------------------

def _fk_from_reference(
    ref: exp.Reference,
    child_cols: list[str],
    fk_name: Optional[str] = None,
) -> Optional[CanonicalForeignKey]:
    """
    Build a CanonicalForeignKey from a sqlglot Reference node.

    The Reference node structure from both table-level FOREIGN KEY and
    inline column REFERENCES is identical:
        Reference(this=Schema(this=Table(...), expressions=[Identifier, ...]))
    """
    ref_schema = ref.this
    if isinstance(ref_schema, exp.Schema):
        ref_table_node = ref_schema.this
        parent_table = ref_table_node.name if ref_table_node else None
        parent_schema_name = (ref_table_node.db or None) if ref_table_node else None
        parent_cols = [ident.name for ident in (ref_schema.expressions or [])]
    elif isinstance(ref_schema, exp.Table):
        parent_table = ref_schema.name
        parent_schema_name = ref_schema.db or None
        parent_cols = []
    else:
        return None
    if not parent_table:
        return None
    return CanonicalForeignKey(
        columns=child_cols,
        parent_table=parent_table,
        parent_columns=parent_cols,
        name=fk_name,
        parent_schema=parent_schema_name,
    )


def _parse_fk_constraints(ast: exp.Create) -> list[CanonicalForeignKey]:
    """Extract table-level FOREIGN KEY … REFERENCES … constraints."""
    fks: list[CanonicalForeignKey] = []
    for fk_node in ast.find_all(exp.ForeignKey):
        child_cols = [c.name for c in fk_node.expressions]
        ref = fk_node.find(exp.Reference)
        if not ref:  # pragma: no cover
            continue  # pragma: no cover

        # CONSTRAINT fk_name FOREIGN KEY …
        fk_name: Optional[str] = None
        parent = fk_node.parent
        if isinstance(parent, exp.Constraint):
            name_node = parent.args.get("this")
            fk_name = name_node.name if name_node else None

        fk = _fk_from_reference(ref, child_cols, fk_name)
        if fk:
            fks.append(fk)
    return fks


def _parse_inline_fk_constraints(
    ast: exp.Create,
    seen_columns: set[str],
) -> list[CanonicalForeignKey]:
    """
    Extract inline column-level REFERENCES constraints, e.g.::

        customer_id INTEGER NOT NULL REFERENCES customer(customer_id)

    These appear as Reference nodes directly inside a ColumnDef's constraint
    list, not as ForeignKey nodes at the table level.  Only columns already
    listed in *seen_columns* (i.e. successfully parsed columns) are included.
    Columns already covered by a table-level FK are de-duplicated by the caller.
    """
    fks: list[CanonicalForeignKey] = []
    for col_def in ast.find_all(exp.ColumnDef):
        col_name = col_def.name
        if col_name not in seen_columns:
            continue
        # Look for a Reference that is a direct child constraint of this col_def,
        # but NOT inside a ForeignKey node (those are handled by _parse_fk_constraints).
        for ref in col_def.find_all(exp.Reference):
            # Skip if this Reference lives inside a ForeignKey subtree
            parent = ref.parent
            while parent is not None and not isinstance(parent, exp.ColumnDef):
                if isinstance(parent, exp.ForeignKey):
                    break
                parent = parent.parent
            else:
                fk = _fk_from_reference(ref, [col_name])
                if fk:
                    fks.append(fk)
    return fks


# ---------------------------------------------------------------------------
# Single CREATE TABLE AST → CanonicalTableSchema
# ---------------------------------------------------------------------------

def _parse_create_table(ast: exp.Create, dialect: str) -> CanonicalTableSchema:
    table_node = ast.find(exp.Table)
    table_name = table_node.name if table_node else "unknown"

    # Collect table-level PRIMARY KEY columns
    pk_cols: set[str] = set()
    for pk_node in ast.find_all(exp.PrimaryKey):
        for col_expr in pk_node.expressions:
            pk_cols.add(col_expr.name)

    columns: list[CanonicalColumn] = []

    for col_def in ast.find_all(exp.ColumnDef):
        col_name = col_def.name
        dt = col_def.find(SgDataType)
        if dt is None:
            continue

        canonical, length, precision, scale, fsp, unsigned, is_serial = _dtype_info(dt, dialect)

        # Capture original SQL type for source_type annotation (see _build_constraints below).
        source_type_str = _source_type_str(dt, dialect)

        # Constraints on this column.
        # sqlglot 30+ uses NotNullColumnConstraint for BOTH "NULL" and "NOT NULL".
        # Distinguish them via the allow_null argument:
        #   NULL     → NotNullColumnConstraint(allow_null=True)   → nullable
        #   NOT NULL → NotNullColumnConstraint(allow_null=False)  → not null
        _nn = col_def.find(exp.NotNullColumnConstraint)
        if _nn is None:
            not_null = False
        else:
            not_null = not _nn.args.get("allow_null", False)
        auto_inc = (
            is_serial
            or col_def.find(exp.AutoIncrementColumnConstraint) is not None
            or col_def.find(exp.GeneratedAsIdentityColumnConstraint) is not None
        )
        unique = col_def.find(exp.UniqueColumnConstraint) is not None
        is_pk = (
            col_def.find(exp.PrimaryKeyColumnConstraint) is not None
            or col_name in pk_cols
        )
        if is_pk:
            pk_cols.add(col_name)

        # DEFAULT value — skip NULL defaults; normalise booleans to lowercase
        default: Optional[str] = None
        dft_constraint = col_def.find(exp.DefaultColumnConstraint)
        if dft_constraint:
            dft_expr = dft_constraint.args.get("this")
            if dft_expr is not None and not isinstance(dft_expr, exp.Null):
                raw = dft_expr.sql(dialect=sqlglot_dialect_name(dialect))
                # sqlglot normalises booleans to TRUE/FALSE; lower them for
                # consistency with conventional SQL style.
                if raw.upper() in ("TRUE", "FALSE"):
                    raw = raw.lower()
                # Remove empty parens sqlglot adds to CURRENT_TIMESTAMP etc.
                raw = _normalize_default(raw)
                default = raw

        # Build constraints dict — preserve serial_type and source_type annotation.
        # source_type is stored whenever the original SQL type carries information that
        # the canonical type alone cannot reconstruct, so that:
        #   1. The canonical YAML is self-documenting.
        #   2. Downstream DDL emitters can add "/* originally <type> */" comments when
        #      the emitted type differs from the original (e.g. TIMETZ → STRING in Databricks).
        col_constraints: dict = {}
        if is_serial:
            col_constraints["serial_type"] = dt.this.value
        # Record source_type for any type that the canonical name doesn't uniquely describe:
        #   - widened types    (NUMBER(10) → integer/long, MONEY → decimal(19,4))
        #   - degraded targets (TIMETZ → TIME, ROWVERSION → binary, INET → string)
        #   - spatial / exotic (GEOGRAPHY, HIERARCHYID, XML → binary/string)
        # Do NOT record source_type for types that are semantically equivalent to the
        # canonical (i.e., emit identically or with an accepted alias in every target).
        _LOSSLESS_CANONICAL = {
            # canonical type → set of original SQL type leading tokens that are lossless
            "integer":     {"INT", "INTEGER", "SMALLINT", "MEDIUMINT", "TINYINT"},
            "long":        {"BIGINT"},
            "float":       {"FLOAT", "REAL"},
            "double":      {"DOUBLE", "DOUBLE PRECISION"},
            "boolean":     {"BOOLEAN", "BOOL", "BIT"},
            "date":        {"DATE"},
            "string":      {"VARCHAR", "NVARCHAR", "CHAR", "NCHAR", "TEXT", "NTEXT",
                            "CLOB", "NCLOB", "STRING"},
            "binary":      {"BINARY", "VARBINARY", "BLOB", "BYTEA", "IMAGE"},
            "uuid":        {"UUID", "UNIQUEIDENTIFIER"},
            # All UTC-aware timestamp representations are semantically equivalent;
            # no annotation needed when moving between TIMESTAMPTZ / DATETIMEOFFSET /
            # MySQL TIMESTAMP / Oracle TIMESTAMP WITH TIME ZONE / Databricks TIMESTAMP.
            "timestamptz": {"TIMESTAMPTZ", "TIMESTAMP WITH TIME ZONE",
                            "DATETIMEOFFSET", "TIMESTAMP"},
            # Plain TIMESTAMP (no timezone) is unambiguous across dialects.
            "timestamp":   {"TIMESTAMP", "DATETIME", "DATETIME2", "TIMESTAMP_NTZ",
                            "TIMESTAMP WITHOUT TIME ZONE", "SMALLDATETIME"},
            "time":        {"TIME"},
            # NUMBER(p,s) is Oracle's spelling of DECIMAL(p,s) — semantically identical.
            # NUMBER(p) without scale is handled by widening (→ integer/long), so we only
            # reach here when scale is present; treat it as lossless.
            "decimal":     {"DECIMAL", "NUMERIC", "NUMBER"},
        }
        lossless_for_type = _LOSSLESS_CANONICAL.get(canonical, set())
        # Normalise the source_type_str to its leading token for comparison.
        # Skip source_type when serial_type is already stored (it fully captures the origin).
        _leading = source_type_str.split("(")[0].strip()
        if _leading not in lossless_for_type and not is_serial:
            col_constraints["source_type"]    = source_type_str
            col_constraints["source_dialect"] = dialect

        # Column COMMENT clause — stored in col.comment for semantic inference.
        ddl_comment: Optional[str] = None
        _comment_node = col_def.find(exp.CommentColumnConstraint)
        if _comment_node is not None and _comment_node.this is not None:
            ddl_comment = _comment_node.this.name or None

        # ── Inline REFERENCES (column-level FK) ─────────────────────────────
        # Store as col.references for backward compatibility.  The full
        # CanonicalForeignKey is built below after all columns are collected.
        inline_ref = col_def.find(exp.Reference)
        col_ref: Optional[tuple[str, str]] = None
        if inline_ref:
            ref_schema = inline_ref.this
            if isinstance(ref_schema, exp.Schema):
                pt = ref_schema.this.name if ref_schema.this else None
                pc = [i.name for i in (ref_schema.expressions or [])]
                if pt:
                    col_ref = (pt, pc[0] if pc else col_name)
            elif isinstance(ref_schema, exp.Table):
                col_ref = (ref_schema.name, col_name)

        columns.append(CanonicalColumn(
            name=col_name,
            type=canonical,
            length=length,
            precision=precision,
            scale=scale,
            fsp=fsp,
            unsigned=unsigned,
            not_null=not_null or is_pk,
            auto_increment=auto_inc,
            default=default,
            primary_key=is_pk,
            unique=unique,
            constraints=col_constraints if col_constraints else None,
            # PK columns always use sequential generation so that FK child
            # columns (sampled from [1, parent_row_count]) can join correctly.
            # For plain INT PRIMARY KEY (no SERIAL/AUTO_INCREMENT) this is
            # especially important: without sequential we'd emit random large
            # integers that don't match the FK range.
            generation=(
                GenerationRule(distribution="sequential", min_value=1)
                if is_pk else None
            ),
            comment=ddl_comment,
            references=col_ref,
        ))

    # Back-fill PK flag for columns only referenced by a table-level PRIMARY KEY.
    # In practice this is unreachable: is_pk (line 494-496) already incorporates
    # pk_cols, so col.primary_key is always True for any column in pk_cols.
    for col in columns:
        if col.name in pk_cols and not col.primary_key:  # pragma: no cover
            col.primary_key = True  # pragma: no cover
            col.not_null = True  # pragma: no cover
            col.generation = GenerationRule(distribution="sequential", min_value=1)  # pragma: no cover

    # Merge table-level and inline FK constraints; de-duplicate by child column set.
    seen_cols = {c.name for c in columns}
    table_fks  = _parse_fk_constraints(ast)
    table_fk_col_sets = {frozenset(fk.columns) for fk in table_fks}
    inline_fks = [
        fk for fk in _parse_inline_fk_constraints(ast, seen_cols)
        if frozenset(fk.columns) not in table_fk_col_sets
    ]
    fk_constraints = table_fks + inline_fks

    return CanonicalTableSchema(
        name=table_name,
        columns=columns,
        fk_constraints=fk_constraints if fk_constraints else None,
        description=f"Parsed from {dialect} schema dump",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

import re as _re

# Oracle/non-standard type names that can appear in DDL but that sqlglot doesn't
# support in certain dialects.  We translate them before parsing.
_ORACLE_IN_MYSQL_RE = _re.compile(
    r"\bRAW\s*(\([^)]+\))|\bNCLOB\b",
    _re.IGNORECASE,
)

_BIT_VARYING_RE = _re.compile(r"\bBIT\s+VARYING\b", _re.IGNORECASE)

# sqlglot normalises CURRENT_TIMESTAMP() → CURRENT_TIMESTAMP() but original DDL
# often omits the parens.  Remove the empty parens so round-trips stay stable.
_CURRENT_TS_RE = _re.compile(r"\bCURRENT_TIMESTAMP\s*\(\s*\)", _re.IGNORECASE)
_CURRENT_DATE_RE = _re.compile(r"\bCURRENT_DATE\s*\(\s*\)", _re.IGNORECASE)
_CURRENT_TIME_RE = _re.compile(r"\bCURRENT_TIME\s*\(\s*\)", _re.IGNORECASE)
_GETDATE_RE = _re.compile(r"\bGETDATE\s*\(\s*\)", _re.IGNORECASE)


def _preprocess_oracle_in_mysql(sql: str) -> str:
    """Replace Oracle/non-standard types with MySQL equivalents."""
    def _repl(m: _re.Match) -> str:
        tok = m.group(0).upper()
        if tok.startswith("RAW"):
            return f"VARBINARY{m.group(1)}"
        return "LONGTEXT"  # NCLOB → LONGTEXT
    return _ORACLE_IN_MYSQL_RE.sub(_repl, sql)


def _preprocess_pg(sql: str) -> str:
    """Replace PG types that sqlglot doesn't parse natively."""
    return _BIT_VARYING_RE.sub("VARBIT", sql)


def _normalize_default(raw: str) -> str:
    """
    Normalise function-call defaults so round-trips are stable.

    sqlglot may add empty parens (CURRENT_TIMESTAMP → CURRENT_TIMESTAMP()) or
    convert standard SQL functions to dialect equivalents (CURRENT_TIMESTAMP →
    GETDATE() for SQL Server).  Normalise these back to standard SQL spellings.
    """
    s = _CURRENT_TS_RE.sub("CURRENT_TIMESTAMP", raw)
    s = _CURRENT_DATE_RE.sub("CURRENT_DATE", s)
    s = _CURRENT_TIME_RE.sub("CURRENT_TIME", s)
    s = _GETDATE_RE.sub("CURRENT_TIMESTAMP", s)   # SS GETDATE() ≡ CURRENT_TIMESTAMP
    return s


def _apply_alter_fks(
    statements: list,
    tables_by_name: dict[str, "CanonicalTableSchema"],
) -> None:
    """
    Walk ALTER TABLE … ADD CONSTRAINT … FOREIGN KEY statements and attach
    the discovered FK constraints to the matching CanonicalTableSchema.

    This handles the common pattern used by Liquibase, Flyway, pg_dump, and
    Chinook where FK constraints are added *after* all CREATE TABLE statements::

        ALTER TABLE invoice
            ADD CONSTRAINT invoice_customer_id_fkey
            FOREIGN KEY (customer_id) REFERENCES customer (customer_id);

    The ALTER TABLE target may use quoted identifiers; we normalise to lower-case
    to match the table_name stored on CanonicalTableSchema.
    """
    for stmt in statements:
        if not isinstance(stmt, exp.Alter):
            continue
        if not (stmt.kind or "").upper() == "TABLE":  # type: ignore[union-attr]
            continue

        tbl_node = stmt.this
        tbl_name = tbl_node.name if tbl_node else None
        if not tbl_name:
            continue
        target = tables_by_name.get(tbl_name) or tables_by_name.get(tbl_name.lower())
        if target is None:
            continue

        for fk_node in stmt.find_all(exp.ForeignKey):
            child_cols = [c.name for c in fk_node.expressions]
            ref = fk_node.find(exp.Reference)
            if not ref:
                continue

            fk_name: Optional[str] = None
            p = fk_node.parent
            if isinstance(p, exp.Constraint):
                name_node = p.args.get("this")
                fk_name = name_node.name if name_node else None

            fk = _fk_from_reference(ref, child_cols, fk_name)
            if fk is None:
                continue

            # Skip duplicate: same child columns already covered by a prior entry.
            existing_col_sets = {
                frozenset(e.columns) for e in (target.fk_constraints or [])
            }
            if frozenset(fk.columns) in existing_col_sets:
                continue

            if target.fk_constraints is None:
                target.fk_constraints = []
            target.fk_constraints.append(fk)


def parse_ddl(sql: str, dialect: str | None = None) -> list[CanonicalTableSchema]:
    """
    Parse SQL DDL string containing one or more CREATE TABLE statements.
    Returns list[CanonicalTableSchema].

    Parameters
    ----------
    sql : str
        Full schema dump text (mysqldump --no-data, pg_dump -s, mssql-scripter,
        Oracle expdp DDL, Databricks SHOW CREATE TABLE, …).
    dialect : str | None
        "mysql" | "postgres" | "neon" | "neondb" | "sqlserver" | "oracle" | "databricks" | None.
        When None, the dialect is auto-detected from SQL content.
        ``neon`` / ``neondb`` are aliases for PostgreSQL (Neon is wire- and DDL-compatible).
    """
    if dialect is None:
        dialect = _detect_dialect(sql)
    dialect = normalize_dialect(dialect)
    if dialect not in SQLGLOT_DIALECT:
        dialect = "mysql"   # unknown dialect: MySQL grammar is most permissive

    sg_dialect = sqlglot_dialect_name(dialect)
    # Minimal pre-processing for types sqlglot doesn't support in a given dialect.
    # Oracle DDL auto-detected above → uses oracle dialect, no preprocessing needed.
    # Explicit dialect="mysql" with Oracle syntax → backward-compat preprocessing.
    if dialect == "mysql":
        sql = _preprocess_oracle_in_mysql(sql)
    elif dialect == "postgres":
        sql = _preprocess_pg(sql)
    try:
        statements = sqlglot.parse(sql, dialect=sg_dialect, error_level=sqlglot.ErrorLevel.WARN)
    except Exception:  # pragma: no cover
        statements = sqlglot.parse(sql, error_level=sqlglot.ErrorLevel.WARN)  # pragma: no cover

    result: list[CanonicalTableSchema] = []
    for stmt in statements:
        if isinstance(stmt, exp.Create) and stmt.kind and stmt.kind.upper() == "TABLE":
            result.append(_parse_create_table(stmt, dialect))

    # Second pass: apply FK constraints from ALTER TABLE … ADD CONSTRAINT … FOREIGN KEY.
    # This pattern is used by pg_dump, Liquibase, Flyway, Chinook, and many other tools
    # that emit CREATE TABLE first and ADD CONSTRAINT FK in a separate ALTER statement.
    if result:
        tables_by_name = {t.name: t for t in result}
        _apply_alter_fks(statements, tables_by_name)

    return result


def parse_ddl_file(path: str | Path, dialect: str | None = None) -> list[CanonicalTableSchema]:
    """Load a .sql schema dump file and parse CREATE TABLE statements."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"DDL file not found: {path}")
    with open(path, encoding="utf-8", errors="replace") as f:
        sql = f.read()
    return parse_ddl(sql, dialect=dialect)
