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
* v1's DataGenPlan fills the gaps in dbldatagen_builder.py (v0):
    - Native Zipf distribution (no more Gamma approximation)
    - Real Faker providers (no more regex-template hacks)
    - ForeignKeyRef with referential integrity + configurable distribution
    - Topological generation order (parents before children)
    - Pydantic → native JSON/YAML serialisation
    - ColumnStats (null_fraction, MCV weights, min/max) fed from TableStats

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

Known gaps vs v0 (post-generation only — require a Spark step after generate())
--------------------------------------------------------------------------------
inject_boundary_values  Use apply_boundary_rows(spark, df, table, stats) from
                        src/statschema/postgen.py after generation to union one
                        row with min values and one with max values.

inject_rare_events      Not yet implemented in either v0 or v1.

temporal_ordering       Not applied automatically.  Enforce table.temporal_ordering_
  _constraints          constraints after generation using a filter+re-sample or
                        date_add/lag approach.  See synthetic_data_shortcomings.md §2.

v0 FK support           dbldatagen v0 has no ForeignKeyRef.  Use this bridge (v1)
                        for any schema that contains FK constraints.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    pass

from .model import CanonicalForeignKey
from .semantic_hints import infer_format_pattern

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


def _fk_v1_distribution(fk: CanonicalForeignKey):  # pragma: no cover
    """
    Convert CanonicalForeignKey distribution settings to a dbldatagen.v1 distribution.

    fk_distribution  →  v1 object          notes
    ───────────────────────────────────────────────────────────────────────────
    "zipf"           →  Zipf(exponent)      default; hot-parent power law
    "uniform"        →  Uniform()           every parent equally likely
    "normal"         →  Normal(mean,std)    bell-curve around median parent
    "exponential"    →  Exponential(rate)   one-end-heavy decay
    """
    from dbldatagen.v1.schema import Exponential, Normal, Uniform, Zipf

    d = (fk.fk_distribution or "zipf").lower()
    p = fk.fk_distribution_params or {}
    if d == "zipf":
        return Zipf(exponent=float(p.get("exponent", 1.2)))
    if d == "uniform":
        return Uniform()
    if d == "normal":
        return Normal(mean=float(p.get("mean", 0.0)), stddev=float(p.get("stddev", 1.0)))
    if d == "exponential":
        return Exponential(rate=float(p.get("rate", 1.0)))
    return Zipf(exponent=1.2)  # unknown → default


def _cast_stat_v1(value: str, col_type: str):
    """Cast a ColumnStats min/max string to the Python type expected by v1 RangeColumn."""
    if value is None:
        return None
    try:
        if col_type in ("integer", "long"):
            return int(float(value))
        if col_type in ("float", "double", "decimal"):
            return float(value)
        if col_type in ("date", "timestamp", "timestamptz"):
            return str(value)
        return None
    except (ValueError, TypeError):
        return None


def _col_to_v1_spec(col, pk_col_names: set[str], fk_map: dict[str, CanonicalForeignKey],  # pragma: no cover
                    col_stats=None):
    """Convert one CanonicalColumn to a dbldatagen.v1 ColumnSpec.

    Parameters
    ----------
    col          : CanonicalColumn
    pk_col_names : set of column names that are part of the PK
    fk_map       : {child_col_name: CanonicalForeignKey}
    col_stats    : Optional[ColumnStats] — when provided, drives null_fraction,
                   MCV weights (ValuesColumn + WeightedValues), and numeric min/max.
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

    # ── Null fraction from ColumnStats ────────────────────────────────────
    if col_stats is not None and (gen is None or gen.inject_nulls_from_stats):
        if col_stats.null_fraction and col_stats.null_fraction > 0:
            null_fraction = col_stats.null_fraction

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
        fk = fk_map[col.name]
        # For composite FKs, align child column index to parent column.
        try:
            idx = fk.columns.index(col.name)
        except ValueError:
            idx = 0
        parent_col = fk.parent_columns[idx] if idx < len(fk.parent_columns) else fk.parent_columns[0]
        ref = f"{fk.parent_table}.{parent_col}"
        return ColumnSpec(
            name=col.name,
            gen=ConstantColumn(value=None),
            foreign_key=ForeignKeyRef(
                ref=ref,
                distribution=_fk_v1_distribution(fk),
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
    # MCV weights from ColumnStats (mirrors v0 logic — applied when MCVs
    # dominate the column or the column is genuinely low-cardinality)
    # -----------------------------------------------------------------------
    if (col_stats is not None
            and col_stats.most_common_values
            and col.type != "boolean"
            and (gen is None or gen.use_mcv_weights)
            and (gen is None or gen.values is None)):
        total_mcv_freq = sum(m.frequency for m in col_stats.most_common_values)
        nd = col_stats.n_distinct if col_stats.n_distinct > 0 else float("inf")
        if ((gen is not None and gen.use_mcv_weights)
                or total_mcv_freq > 0.50
                or nd <= 20):
            mcv_values = [m.value for m in col_stats.most_common_values]
            mcv_weights = {str(m.value): float(m.frequency) for m in col_stats.most_common_values}
            return ColumnSpec(
                name=col.name,
                dtype=dtype,
                gen=ValuesColumn(values=mcv_values,
                                 distribution=WeightedValues(weights=mcv_weights)),
                nullable=nullable,
                null_fraction=null_fraction,
            )

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
        begin = str(gen.min_value) if gen and gen.min_value else (
            str(col_stats.min_value) if col_stats and col_stats.min_value else "2000-01-01 00:00:00"
        )
        end   = str(gen.max_value) if gen and gen.max_value else (
            str(col_stats.max_value) if col_stats and col_stats.max_value else "2030-12-31 23:59:59"
        )
        return ColumnSpec(
            name=col.name,
            dtype=dtype,
            gen=TimestampColumn(start=begin, end=end),
            nullable=nullable,
            null_fraction=null_fraction,
        )

    # -----------------------------------------------------------------------
    # String columns — Faker, UUID, pattern, or name-based fallback
    # -----------------------------------------------------------------------
    if col.type in ("string", "time", "timetz", "binary"):
        # Explicit GenerationRule.format_pattern takes priority; fall back to
        # built-in column-name inference (Option A, semantic_hints.py).
        fp = (gen.format_pattern if gen else None) or infer_format_pattern(
            col.name, col_comment=col.comment, col_description=col.description
        )

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
            spec.null_fraction = null_fraction
            return spec
        except ImportError:
            # Width-appropriate alpha template when col.length is known
            max_len = col.length or (gen.max_length if gen else None) or 32
            n = min(max_len, 64)
            return ColumnSpec(
                name=col.name,
                dtype=DataType.STRING,
                gen=PatternColumn(template="a" * n),
                nullable=nullable,
                null_fraction=null_fraction,
            )

    # -----------------------------------------------------------------------
    # Numeric columns
    # -----------------------------------------------------------------------
    # Priority: explicit GenerationRule > ColumnStats > type-based defaults
    lo = gen.min_value if gen else None
    hi = gen.max_value if gen else None
    if col_stats is not None and lo is None:
        lo = _cast_stat_v1(col_stats.min_value, col.type)
    if col_stats is not None and hi is None:
        hi = _cast_stat_v1(col_stats.max_value, col.type)

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


def to_v1_plan(  # pragma: no cover
    tables,
    *,
    row_counts: Optional[dict[str, int]] = None,
    stats=None,       # Optional[TableStats | dict[str, TableStats]]
    seed: int = 42,
):
    """Convert one or more CanonicalTableSchema objects to a dbldatagen.v1 DataGenPlan.

    Parameters
    ----------
    tables : CanonicalTableSchema | list[CanonicalTableSchema]
        Source canonical schema(s).
    row_counts : dict | None
        Optional per-table row count overrides, e.g. ``{"orders": 10_000}``.
    stats : TableStats | dict[str, TableStats] | None
        Optional statistics.  Drives null_fraction, MCV weights, and
        numeric/date min/max boundaries for each column.

        Pass a single ``TableStats`` when ``tables`` is a single table, or a
        ``{table_name: TableStats}`` dict when passing multiple tables.
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

    # Normalise stats: accept a single TableStats or a {name: TableStats} dict.
    # After normalisation, stats_map[table_name] → TableStats | None.
    if stats is None:
        stats_map: dict[str, object] = {}
    elif isinstance(stats, dict):
        stats_map = stats
    else:
        # Single TableStats: broadcast to all tables
        stats_map = {t.name: stats for t in tables}

    table_specs: list[TableSpec] = []

    for table in tables:
        # --- Primary key column names ---
        pk_cols = {c.name for c in table.columns if c.primary_key}

        # --- FK map: child_col → CanonicalForeignKey ---
        # Prefer structured fk_constraints (carry distribution metadata);
        # fall back to the legacy foreign_keys dict-list (default distribution).
        fk_map: dict[str, CanonicalForeignKey] = {}
        if table.fk_constraints:
            for fk in table.fk_constraints:
                for col_name in fk.columns:
                    fk_map[col_name] = fk
        elif table.foreign_keys:
            for fk_dict in table.foreign_keys:
                # legacy dict format: {from_col, to_table, to_col}
                if isinstance(fk_dict, dict):
                    from_col = fk_dict.get("from_col") or fk_dict.get("column")
                    to_table = fk_dict.get("to_table") or fk_dict.get("references_table")
                    to_col   = fk_dict.get("to_col") or fk_dict.get("references_column")
                    if from_col and to_table and to_col:
                        fk_map[from_col] = CanonicalForeignKey(
                            columns=[from_col],
                            parent_table=to_table,
                            parent_columns=[to_col],
                            # Default distribution preserved for backward compatibility
                        )

        tbl_stats = stats_map.get(table.name)
        col_specs = [
            _col_to_v1_spec(
                c, pk_cols, fk_map,
                col_stats=tbl_stats.column_stats(c.name) if tbl_stats else None,
            )
            for c in table.columns
        ]

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
