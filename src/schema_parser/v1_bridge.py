"""
Bridge: CanonicalTableSchema → dbldatagen.v1 DataGenPlan.

Pipeline
--------
DDL (any dialect)
  → parse_ddl()             (our multi-dialect DDL parser)
  → CanonicalTableSchema    (precision-faithful canonical model)
  → to_v1_plan()            (this module)
  → DataGenPlan             (dbldatagen.v1 Pydantic model, YAML-serializable)
  → dbldatagen.v1.generate(spark, plan)
  → Spark DataFrames

Why this bridge exists
----------------------
* Our DDL parser handles dialect-specific types (UNSIGNED, MONEY, RAW, ROWVERSION,
  NUMBER(p) widening, etc.) that v1's sqlglot-based SQL query parser does not cover.
* v1's DataGenPlan fills the gaps in our dbldatagen_builder.py (v0):
    - Native Zipf distribution (no more Gamma approximation)
    - Real Faker providers (no more regex-template hacks)
    - ForeignKeyRef with referential integrity
    - Topological generation order (parents before children)
    - Pydantic → native JSON/YAML serialisation

Canonical → v1 type mapping
-----------------------------
integer / long   → DataType.LONG   (RangeColumn or SequenceColumn for PKs)
float            → DataType.FLOAT
double           → DataType.DOUBLE
decimal          → DataType.DECIMAL
string           → DataType.STRING  (with Faker / pattern / values strategy)
boolean          → DataType.BOOLEAN (ValuesColumn [True, False])
date             → DataType.DATE    (TimestampColumn)
timestamp        → DataType.TIMESTAMP
timestamptz      → DataType.TIMESTAMP
time / timetz    → DataType.STRING  (HH:MM:SS.ffffff pattern)
binary           → DataType.STRING  (hex pattern; Spark BinaryType not in v1 DataType enum)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Format-pattern → Faker provider mapping
# ---------------------------------------------------------------------------

_FORMAT_TO_FAKER: dict[str, str] = {
    "email":        "email",
    "phone_us":     "phone_number",
    "phone_intl":   "phone_number",
    "name_first":   "first_name",
    "name_last":    "last_name",
    "name":         "name",
    "company":      "company",
    "address":      "street_address",
    "city":         "city",
    "country_iso2": "country_code",
    "postal_us":    "zipcode",
    "postal_uk":    "postcode",
    "url":          "url",
    "ip_v4":        "ipv4",
    "ip_v6":        "ipv6",
    "ssn":          "ssn",
    "user_agent":   "user_agent",
    "username":     "user_name",
}

# format_pattern values that map to v1 UUIDColumn instead
_UUID_PATTERNS = {"uuid", "guid"}


def _col_to_v1_spec(col, pk_col_names: set[str], fk_map: dict[str, str]):
    """Convert one CanonicalColumn to a dbldatagen.v1 ColumnSpec.

    Parameters
    ----------
    col          : CanonicalColumn
    pk_col_names : set of column names that are part of the PK
    fk_map       : {child_col_name: "parent_table.parent_col"} FK references
    """
    from dbldatagen.v1.schema import (
        ColumnSpec,
        ConstantColumn,
        DataType,
        ExpressionColumn,
        FakerColumn,
        ForeignKeyRef,
        PatternColumn,
        RangeColumn,
        SequenceColumn,
        TimestampColumn,
        UUIDColumn,
        ValuesColumn,
        Zipf,
        Uniform,
        Normal,
        LogNormal,
        Exponential,
        WeightedValues,
    )

    gen = col.generation  # GenerationRule | None
    null_fraction = 0.0
    nullable = not col.not_null

    # -----------------------------------------------------------------------
    # Resolve Spark DataType
    # -----------------------------------------------------------------------
    _TYPE_MAP = {
        "integer":    DataType.INT,
        "long":       DataType.LONG,
        "float":      DataType.FLOAT,
        "double":     DataType.DOUBLE,
        "decimal":    DataType.DECIMAL,
        "string":     DataType.STRING,
        "boolean":    DataType.BOOLEAN,
        "date":       DataType.DATE,
        "timestamp":  DataType.TIMESTAMP,
        "timestamptz": DataType.TIMESTAMP,
        "time":       DataType.STRING,
        "timetz":     DataType.STRING,
        "binary":     DataType.STRING,  # v1 has no BinaryType yet
    }
    dtype = _TYPE_MAP.get(col.type, DataType.STRING)

    # -----------------------------------------------------------------------
    # FK columns — placeholder gen replaced at plan-resolve time
    # -----------------------------------------------------------------------
    if col.name in fk_map:
        return ColumnSpec(
            name=col.name,
            gen=ConstantColumn(value=None),
            foreign_key=ForeignKeyRef(
                ref=fk_map[col.name],
                distribution=Zipf(exponent=1.2),
                nullable=nullable,
                null_fraction=0.0,
            ),
        )

    # -----------------------------------------------------------------------
    # PK columns — always a sequence
    # -----------------------------------------------------------------------
    if col.name in pk_col_names or col.primary_key:
        if col.auto_increment:
            return ColumnSpec(name=col.name, dtype=DataType.LONG, gen=SequenceColumn())
        if col.type == "string":
            return ColumnSpec(name=col.name, dtype=DataType.STRING, gen=UUIDColumn())
        return ColumnSpec(name=col.name, dtype=DataType.LONG, gen=SequenceColumn())

    # -----------------------------------------------------------------------
    # Build distribution object from GenerationRule
    # -----------------------------------------------------------------------
    def _distribution(gen):
        if gen is None:
            return Uniform()
        d = gen.distribution
        p = gen.distribution_params or {}
        if d == "normal":
            return Normal(mean=float(p.get("mean", 0.0)), stddev=float(p.get("stddev", 1.0)))
        if d == "lognormal":
            return LogNormal(mean=float(p.get("mean", 0.0)), stddev=float(p.get("stddev", 1.0)))
        if d in ("zipf", "power"):
            return Zipf(exponent=float(p.get("exponent", 1.5)))
        if d == "exponential":
            return Exponential(rate=float(p.get("rate", 1.0)))
        if d == "weighted" and gen.values and gen.weights:
            w = {str(v): float(wt) for v, wt in zip(gen.values, gen.weights)}
            return WeightedValues(weights=w)
        return Uniform()

    dist = _distribution(gen)

    # -----------------------------------------------------------------------
    # Explicit values list
    # -----------------------------------------------------------------------
    if gen and gen.values:
        return ColumnSpec(
            name=col.name,
            dtype=dtype,
            gen=ValuesColumn(values=gen.values, distribution=dist),
            nullable=nullable,
            null_fraction=null_fraction,
        )

    # -----------------------------------------------------------------------
    # Boolean
    # -----------------------------------------------------------------------
    if col.type == "boolean":
        return ColumnSpec(
            name=col.name,
            dtype=DataType.BOOLEAN,
            gen=ValuesColumn(values=[True, False]),
            nullable=nullable,
        )

    # -----------------------------------------------------------------------
    # Date / timestamp
    # -----------------------------------------------------------------------
    if col.type in ("date", "timestamp", "timestamptz"):
        begin = str(gen.min_value) if gen and gen.min_value else "2000-01-01 00:00:00"
        end   = str(gen.max_value) if gen and gen.max_value else "2030-12-31 23:59:59"
        return ColumnSpec(
            name=col.name,
            dtype=dtype,
            gen=TimestampColumn(begin=begin, end=end),
            nullable=nullable,
        )

    # -----------------------------------------------------------------------
    # String columns — Faker, UUID, pattern, or name-based fallback
    # -----------------------------------------------------------------------
    if col.type in ("string", "time", "timetz", "binary"):
        fp = gen.format_pattern if gen else None

        # UUID
        if fp in _UUID_PATTERNS:
            return ColumnSpec(name=col.name, dtype=DataType.STRING, gen=UUIDColumn(),
                              nullable=nullable)

        # Named Faker provider
        if fp and fp in _FORMAT_TO_FAKER:
            return ColumnSpec(
                name=col.name,
                dtype=DataType.STRING,
                gen=FakerColumn(provider=_FORMAT_TO_FAKER[fp]),
                nullable=nullable,
            )

        # Pattern template (pass through raw patterns)
        if fp:
            return ColumnSpec(
                name=col.name,
                dtype=DataType.STRING,
                gen=PatternColumn(template=fp),
                nullable=nullable,
            )

        # Fallback: semantic name inference from v1's name_mapper
        try:
            from dbldatagen.v1.connectors.sql.name_mapper import map_column_name
            spec = map_column_name(col.name, DataType.STRING)
            spec.nullable = nullable
            return spec
        except ImportError:
            max_len = col.length or (gen.max_length if gen else None) or 32
            return ColumnSpec(
                name=col.name,
                dtype=DataType.STRING,
                gen=PatternColumn(template=f"{col.name}_{{digit:6}}"),
                nullable=nullable,
            )

    # -----------------------------------------------------------------------
    # Numeric columns
    # -----------------------------------------------------------------------
    lo = gen.min_value if gen else None
    hi = gen.max_value if gen else None

    if col.type == "integer":
        lo = int(lo) if lo is not None else -(2**31)
        hi = int(hi) if hi is not None else (2**31 - 1)
    elif col.type == "long":
        lo = int(lo) if lo is not None else -(2**63)
        hi = int(hi) if hi is not None else (2**63 - 1)
    elif col.type in ("float", "double"):
        lo = float(lo) if lo is not None else 0.0
        hi = float(hi) if hi is not None else 1e6
    elif col.type == "decimal":
        p = col.precision or 18
        s = col.scale or 2
        max_int = 10 ** (p - s) - 1
        lo = float(lo) if lo is not None else -max_int
        hi = float(hi) if hi is not None else max_int

    return ColumnSpec(
        name=col.name,
        dtype=dtype,
        gen=RangeColumn(min=lo, max=hi, distribution=dist),
        nullable=nullable,
        null_fraction=null_fraction,
    )


def to_v1_plan(
    tables,
    *,
    row_counts: Optional[dict[str, int]] = None,
    seed: int = 42,
):
    """Convert one or more CanonicalTableSchema objects to a dbldatagen.v1 DataGenPlan.

    Parameters
    ----------
    tables : CanonicalTableSchema | list[CanonicalTableSchema]
        Source canonical schema(s).
    row_counts : dict | None
        Optional per-table row count overrides, e.g. ``{"orders": 10_000}``.
    seed : int
        Global random seed for deterministic generation.

    Returns
    -------
    DataGenPlan
        A fully populated Pydantic DataGenPlan ready for
        ``dbldatagen.v1.generate(spark, plan)``.
    """
    from dbldatagen.v1.schema import DataGenPlan, PrimaryKey, TableSpec

    if not isinstance(tables, list):
        tables = [tables]

    row_counts = row_counts or {}
    table_specs: list[TableSpec] = []

    for table in tables:
        # --- Primary key column names ---
        pk_cols = {c.name for c in table.columns if c.primary_key}

        # --- FK map: child_col → "parent_table.parent_col" ---
        fk_map: dict[str, str] = {}
        if table.foreign_keys:
            for fk in table.foreign_keys:
                # legacy dict format: {from_col, to_table, to_col}
                if isinstance(fk, dict):
                    from_col  = fk.get("from_col") or fk.get("column")
                    to_table  = fk.get("to_table") or fk.get("references_table")
                    to_col    = fk.get("to_col") or fk.get("references_column")
                    if from_col and to_table and to_col:
                        fk_map[from_col] = f"{to_table}.{to_col}"

        col_specs = [_col_to_v1_spec(c, pk_cols, fk_map) for c in table.columns]

        pk = PrimaryKey(columns=list(pk_cols)) if pk_cols else None
        rows = row_counts.get(table.name, 1_000)

        table_specs.append(
            TableSpec(
                name=table.name,
                columns=col_specs,
                rows=rows,
                primary_key=pk,
            )
        )

    return DataGenPlan(tables=table_specs, seed=seed)
