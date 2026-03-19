"""
Override specification for schema and statistics transformations.

Purpose
-------
When migrating data from one database to another, or when setting up test environments
from production schemas, many structural and statistical properties need adjustment:

  - Rename tables, columns, schemas, catalogs to match naming conventions
  - Remap schema/catalog hierarchy (MySQL db ≈ Oracle schema ≈ PG schema)
  - Scale row counts for subset / integration testing
  - Override column types (e.g. MySQL TINYINT(1) → Oracle NUMBER(1))
  - Adjust cardinality / null fractions when the test dataset differs
  - Prefix reserved words that conflict in the target database
  - Change column lengths (VARCHAR(255) → NVARCHAR(200) for SQL Server limits)

Common migration scenarios captured
-------------------------------------
MySQL → Oracle
  - Schema: MySQL `database` maps to Oracle `schema`
  - Identifiers: Oracle defaults to UPPERCASE; add uppercase_identifiers: true
  - TINYINT(1) used as boolean → NUMBER(1)
  - AUTO_INCREMENT → GENERATED ALWAYS AS IDENTITY
  - DATETIME → TIMESTAMP
  - TEXT → CLOB or VARCHAR2(4000)
  - Reserved words: `date`, `comment`, `level`, `order`, `select`, `value` need quoting

MySQL → PostgreSQL
  - TINYINT(1) → BOOLEAN
  - DATETIME → TIMESTAMP
  - AUTO_INCREMENT → SERIAL / GENERATED ALWAYS AS IDENTITY
  - ENUM('a','b') → VARCHAR(N) or custom TYPE
  - Unsigned integers: no equivalent, promote to next wider signed type

MySQL → Databricks
  - DATETIME → TIMESTAMP
  - ENUM → STRING
  - All string types → STRING (no length constraint)
  - AUTO_INCREMENT → BIGINT (Databricks uses GENERATED ALWAYS AS IDENTITY for Delta)

Oracle → PostgreSQL
  - NUMBER(p,0) → INTEGER or BIGINT
  - NUMBER(p,s) → NUMERIC(p,s)
  - VARCHAR2(N) → VARCHAR(N) or TEXT
  - DATE (Oracle includes time) → TIMESTAMP
  - CLOB → TEXT
  - BLOB → BYTEA
  - Identifiers: uppercase Oracle names → lowercase PG convention

SQL Server → Databricks
  - NVARCHAR → STRING
  - DATETIME2 → TIMESTAMP
  - BIT → BOOLEAN
  - IDENTITY → GENERATED ALWAYS AS IDENTITY
  - [schema].[table] → catalog.schema.table (Unity Catalog three-part naming)

Usage
------
    from src.statschema.override_model import OverrideSpec
    from src.statschema.stats_io import apply_overrides

    spec = OverrideSpec.from_yaml("overrides.yaml")
    schema2, stats2 = apply_overrides(schema, stats, spec)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


# ---------------------------------------------------------------------------
# Column-level override
# ---------------------------------------------------------------------------

@dataclass
class ColumnOverride:
    """
    Override specification for a single column.

    Any field set to a non-None value replaces the corresponding field in
    CanonicalColumn (structural) or ColumnStats (statistical).
    """

    # Structural overrides
    rename_to: Optional[str] = None       # new column name
    type: Optional[str] = None            # canonical type override
    length: Optional[int] = None          # new VARCHAR length
    precision: Optional[int] = None       # new DECIMAL precision
    scale: Optional[int] = None           # new DECIMAL scale
    not_null: Optional[bool] = None       # override nullability
    default: Optional[str] = None         # new DEFAULT expression

    # Statistical overrides
    null_fraction: Optional[float] = None
    n_distinct: Optional[int] = None
    min_value: Optional[str] = None
    max_value: Optional[str] = None
    most_common_values: Optional[list[dict[str, Any]]] = None  # [{value, frequency}, ...]

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in {
            "rename_to": self.rename_to,
            "type": self.type,
            "length": self.length,
            "precision": self.precision,
            "scale": self.scale,
            "not_null": self.not_null,
            "default": self.default,
            "null_fraction": self.null_fraction,
            "n_distinct": self.n_distinct,
            "min_value": self.min_value,
            "max_value": self.max_value,
            "most_common_values": self.most_common_values,
        }.items() if v is not None}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ColumnOverride:
        return cls(
            rename_to=d.get("rename_to"),
            type=d.get("type"),
            length=d.get("length"),
            precision=d.get("precision"),
            scale=d.get("scale"),
            not_null=d.get("not_null"),
            default=d.get("default"),
            null_fraction=d.get("null_fraction") and float(d["null_fraction"]),
            n_distinct=d.get("n_distinct") and int(d["n_distinct"]),
            min_value=d.get("min_value") and str(d["min_value"]),
            max_value=d.get("max_value") and str(d["max_value"]),
            most_common_values=d.get("most_common_values"),
        )


# ---------------------------------------------------------------------------
# Table-level override
# ---------------------------------------------------------------------------

@dataclass
class TableOverride:
    """
    Override specification for a single table.

    ``columns`` maps the *source* column name to its override.  After any
    column rename, subsequent pipeline stages see the new name.
    """

    rename_to: Optional[str] = None        # new table name
    rename_schema_to: Optional[str] = None # new schema for this table specifically
    row_count: Optional[int] = None        # explicit row count (beats row_count_scale)
    row_count_scale: Optional[float] = None  # multiply existing row_count by this factor
    columns: dict[str, ColumnOverride] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.rename_to is not None:
            d["rename_to"] = self.rename_to
        if self.rename_schema_to is not None:
            d["rename_schema_to"] = self.rename_schema_to
        if self.row_count is not None:
            d["row_count"] = self.row_count
        if self.row_count_scale is not None:
            d["row_count_scale"] = self.row_count_scale
        if self.columns:
            d["columns"] = {k: v.to_dict() for k, v in self.columns.items()}
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TableOverride:
        cols = {k: ColumnOverride.from_dict(v) for k, v in d.get("columns", {}).items()}
        return cls(
            rename_to=d.get("rename_to"),
            rename_schema_to=d.get("rename_schema_to"),
            row_count=d.get("row_count") and int(d["row_count"]),
            row_count_scale=d.get("row_count_scale") and float(d["row_count_scale"]),
            columns=cols,
        )


# ---------------------------------------------------------------------------
# Top-level override specification
# ---------------------------------------------------------------------------

@dataclass
class OverrideSpec:
    """
    Override specification applied to a (CanonicalTableSchema, TableStats) pair.

    Priority order (highest → lowest)
    -----------------------------------
    1. Table-level column override (``tables[tbl].columns[col]``)
    2. Global type mapping (``type_mappings``)
    3. Table-level row_count (``tables[tbl].row_count``)
    4. Table-level row_count_scale (``tables[tbl].row_count_scale``)
    5. Global row_count_scale
    6. Schema/catalog remapping

    YAML format
    -----------
    ::

        description: "Scale production to 1% for integration testing"

        # Global scale for all tables (table-level overrides beat this)
        row_count_scale: 0.01

        # Schema hierarchy remapping
        schema_mappings:
          public: app              # PG schema → Databricks schema
          dbo: app                 # SQL Server schema → Databricks schema

        catalog_mappings:
          mydb: main               # MySQL db / Oracle schema → Unity Catalog catalog

        # Identifier transformations
        uppercase_identifiers: false   # true for Oracle target
        lowercase_identifiers: true    # true for PG/Databricks target
        reserved_word_prefix: "_"      # prefix cols that clash with SQL reserved words

        # Global canonical type substitutions
        type_mappings:
          boolean: integer         # e.g. Oracle: no native BOOLEAN pre-23c
          long: integer            # downcast for target system limits

        # Table-level overrides
        tables:
          orders:
            rename_to: ORDERS
            row_count: 5000
            columns:
              order_date:
                rename_to: ORDER_DT     # avoid Oracle reserved word DATE
              status:
                n_distinct: 4
                most_common_values:
                  - {value: "active", frequency: 0.7}
                  - {value: "closed", frequency: 0.3}
          select_items:              # table whose name is a reserved word
            rename_to: selected_items
    """

    description: Optional[str] = None

    # Schema / catalog hierarchy remapping
    schema_mappings: dict[str, str] = field(default_factory=dict)    # old → new schema
    catalog_mappings: dict[str, str] = field(default_factory=dict)   # old → new catalog

    # Identifier case normalisation
    uppercase_identifiers: bool = False   # convert all identifiers to UPPER (Oracle target)
    lowercase_identifiers: bool = False   # convert all identifiers to lower (PG/Databricks)

    # Reserved-word handling (applied after case normalisation)
    reserved_word_prefix: Optional[str] = None    # e.g. "_" → `date` becomes `_date`
    reserved_words: set[str] = field(default_factory=set)  # caller-supplied list

    # Global canonical type substitutions (applied before column-level overrides)
    type_mappings: dict[str, str] = field(default_factory=dict)  # canonical → canonical

    # Global row count scale (0.01 = 1% of production)
    row_count_scale: Optional[float] = None

    # Per-table overrides
    tables: dict[str, TableOverride] = field(default_factory=dict)

    # ---------------------------------------------------------------------------
    # Built-in reserved word lists for common target dialects
    # ---------------------------------------------------------------------------

    # A representative (not exhaustive) set of reserved words per dialect.
    # Used when reserved_word_prefix is set and reserved_words is empty.
    _RESERVED: dict[str, set[str]] = field(default_factory=dict, repr=False, compare=False)

    ORACLE_RESERVED: set[str] = field(default_factory=lambda: {
        "access", "add", "all", "alter", "and", "any", "as", "asc", "audit",
        "between", "by", "char", "check", "cluster", "column", "comment",
        "compress", "connect", "create", "current", "date", "decimal",
        "default", "delete", "desc", "distinct", "drop", "else", "exclusive",
        "exists", "file", "float", "for", "from", "grant", "group", "having",
        "identified", "immediate", "in", "increment", "index", "initial",
        "insert", "integer", "intersect", "into", "is", "level", "like",
        "lock", "long", "maxextents", "minus", "mlslabel", "mode", "modify",
        "noaudit", "nocompress", "not", "nowait", "null", "number", "of",
        "offline", "on", "online", "option", "or", "order", "pctfree",
        "prior", "public", "raw", "rename", "resource", "revoke", "row",
        "rowid", "rownum", "rows", "select", "session", "set", "share",
        "size", "smallint", "start", "successful", "synonym", "sysdate",
        "table", "then", "to", "trigger", "uid", "union", "unique", "update",
        "user", "validate", "values", "varchar", "varchar2", "view",
        "whenever", "where", "with",
    }, repr=False, compare=False)

    POSTGRES_RESERVED: set[str] = field(default_factory=lambda: {
        "all", "analyse", "analyze", "and", "any", "array", "as", "asc",
        "asymmetric", "both", "case", "cast", "check", "collate", "column",
        "constraint", "create", "cross", "current_catalog", "current_date",
        "current_role", "current_schema", "current_time", "current_timestamp",
        "current_user", "default", "deferrable", "desc", "distinct", "do",
        "else", "end", "except", "false", "fetch", "for", "foreign", "from",
        "full", "grant", "group", "having", "in", "initially", "inner",
        "intersect", "into", "is", "isnull", "join", "lateral", "leading",
        "left", "like", "limit", "localtime", "localtimestamp", "natural",
        "not", "notnull", "null", "offset", "on", "only", "or", "order",
        "outer", "overlaps", "placing", "primary", "references", "returning",
        "right", "row", "select", "session_user", "similar", "some",
        "symmetric", "table", "tablesample", "then", "to", "trailing",
        "true", "union", "unique", "user", "using", "variadic", "verbose",
        "when", "where", "window", "with",
    }, repr=False, compare=False)

    SQLSERVER_RESERVED: set[str] = field(default_factory=lambda: {
        "add", "all", "alter", "and", "any", "as", "asc", "authorization",
        "backup", "begin", "between", "break", "browse", "bulk", "by",
        "cascade", "case", "check", "checkpoint", "close", "clustered",
        "coalesce", "collate", "column", "commit", "compute", "constraint",
        "contains", "containstable", "continue", "convert", "create", "cross",
        "current", "current_date", "current_time", "current_timestamp",
        "current_user", "cursor", "database", "dbcc", "deallocate", "declare",
        "default", "delete", "deny", "desc", "disk", "distinct", "distributed",
        "double", "drop", "dump", "else", "end", "errlvl", "escape", "except",
        "exec", "execute", "exists", "exit", "external", "fetch", "file",
        "fillfactor", "for", "foreign", "freetext", "freetexttable", "from",
        "full", "function", "goto", "grant", "group", "having", "holdlock",
        "identity", "in", "index", "inner", "insert", "intersect", "into",
        "is", "join", "key", "kill", "left", "like", "lineno", "load",
        "merge", "national", "nocheck", "nonclustered", "not", "null",
        "nullif", "of", "off", "offsets", "on", "open", "opendatasource",
        "openquery", "openrowset", "openxml", "option", "or", "order",
        "outer", "over", "percent", "pivot", "plan", "precision", "primary",
        "print", "proc", "procedure", "public", "raiserror", "read",
        "readtext", "reconfigure", "references", "replication", "restore",
        "restrict", "return", "revert", "revoke", "right", "rollback",
        "rowcount", "rowguidcol", "rule", "save", "schema", "securityaudit",
        "select", "semantickeyphrasetable", "semanticsimilaritydetailstable",
        "semanticsimilaritytable", "session_user", "set", "setuser",
        "shutdown", "some", "statistics", "system_user", "table",
        "tablesample", "textsize", "then", "to", "top", "tran", "transaction",
        "trigger", "truncate", "try_convert", "tsequal", "union", "unique",
        "unpivot", "update", "updatetext", "use", "user", "values",
        "varying", "view", "waitfor", "when", "where", "while", "with",
        "within", "writetext",
    }, repr=False, compare=False)

    def get_reserved_words(self, dialect: Optional[str] = None) -> set[str]:
        """Return the reserved word set to use — caller-supplied or dialect default."""
        if self.reserved_words:
            return self.reserved_words
        if dialect == "oracle":
            return self.ORACLE_RESERVED
        if dialect in ("postgres", "postgresql"):
            return self.POSTGRES_RESERVED
        if dialect in ("sqlserver", "mssql"):
            return self.SQLSERVER_RESERVED
        return set()

    # ---------------------------------------------------------------------------
    # Serialisation
    # ---------------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.description:
            d["description"] = self.description
        if self.schema_mappings:
            d["schema_mappings"] = self.schema_mappings
        if self.catalog_mappings:
            d["catalog_mappings"] = self.catalog_mappings
        if self.uppercase_identifiers:
            d["uppercase_identifiers"] = True
        if self.lowercase_identifiers:
            d["lowercase_identifiers"] = True
        if self.reserved_word_prefix:
            d["reserved_word_prefix"] = self.reserved_word_prefix
        if self.reserved_words:
            d["reserved_words"] = sorted(self.reserved_words)
        if self.type_mappings:
            d["type_mappings"] = self.type_mappings
        if self.row_count_scale is not None:
            d["row_count_scale"] = self.row_count_scale
        if self.tables:
            d["tables"] = {k: v.to_dict() for k, v in self.tables.items()}
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> OverrideSpec:
        tables = {k: TableOverride.from_dict(v) for k, v in d.get("tables", {}).items()}
        spec = cls(
            description=d.get("description"),
            schema_mappings={str(k): str(v) for k, v in d.get("schema_mappings", {}).items()},
            catalog_mappings={str(k): str(v) for k, v in d.get("catalog_mappings", {}).items()},
            uppercase_identifiers=bool(d.get("uppercase_identifiers", False)),
            lowercase_identifiers=bool(d.get("lowercase_identifiers", False)),
            reserved_word_prefix=d.get("reserved_word_prefix"),
            reserved_words=set(d.get("reserved_words", [])),
            type_mappings={str(k): str(v) for k, v in d.get("type_mappings", {}).items()},
            row_count_scale=d.get("row_count_scale") and float(d["row_count_scale"]),
            tables=tables,
        )
        return spec

    @classmethod
    def from_yaml(cls, source: str | Path) -> OverrideSpec:
        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(f"Override file not found: {path}")
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls.from_dict(data or {})

    def to_yaml(self, path: str | Path) -> None:
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(self.to_dict(), f, sort_keys=False, allow_unicode=True)
