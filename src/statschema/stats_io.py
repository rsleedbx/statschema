"""
Statistics I/O, default generation, and override application.

Public API
----------
load_stats(source)                  Load DatabaseStats from YAML/JSON file or dict
dump_stats(db_stats, path)          Write DatabaseStats to YAML file
make_default_stats(tables, ...)     Generate brief default stats from schema alone
apply_overrides(schema, stats, spec) Apply OverrideSpec to schema + stats pair
"""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Optional

import yaml

from .model import CanonicalColumn, CanonicalForeignKey, CanonicalTableSchema
from .override_model import ColumnOverride, OverrideSpec, TableOverride
from .stats_model import (
    ColumnStats,
    CompositeColumnStats,
    DatabaseStats,
    ForeignKeyStats,
    IndexStats,
    MostCommonValue,
    TableStats,
)


# ---------------------------------------------------------------------------
# Load / dump
# ---------------------------------------------------------------------------

def load_stats(source: str | Path | dict) -> DatabaseStats:
    """
    Load DatabaseStats from a YAML file, JSON file, or dict.

    Parameters
    ----------
    source:
        Path to a ``.yaml`` / ``.yml`` / ``.json`` file, or a pre-parsed dict.
    """
    if isinstance(source, dict):
        return DatabaseStats.from_dict(source)
    path = Path(source)
    if not path.exists():
        raise FileNotFoundError(f"Stats file not found: {path}")
    with open(path, encoding="utf-8") as f:
        if path.suffix.lower() == ".json":
            data = json.load(f)
        else:
            data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Stats file must contain a YAML/JSON object: {path}")
    return DatabaseStats.from_dict(data)


def dump_stats(db_stats: DatabaseStats, path: str | Path) -> None:
    """Write DatabaseStats to a YAML file."""
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(db_stats.to_dict(), f, sort_keys=False, allow_unicode=True)


# ---------------------------------------------------------------------------
# Default / brief statistics generation
# ---------------------------------------------------------------------------

# Heuristic n_distinct for types when no real stats are available
_DEFAULT_N_DISTINCT: dict[str, float] = {
    "boolean": 2.0,
    "integer": -0.1,   # assume 10% of rows are distinct integers (FK-like)
    "long":    -0.5,   # assume 50% distinct (sequence-like)
    "float":   -1.0,   # assume all rows distinct
    "double":  -1.0,
    "decimal": -0.5,
    "string":  -0.3,   # 30% distinct strings
    "timestamp": -1.0,
    "date":     -0.1,
}

# Heuristic average byte widths
_DEFAULT_AVG_WIDTH: dict[str, int] = {
    "boolean": 1,
    "integer": 4,
    "long":    8,
    "float":   4,
    "double":  8,
    "decimal": 8,
    "string":  20,
    "timestamp": 8,
    "date":    4,
}


def make_default_stats(
    tables: list[CanonicalTableSchema],
    row_count: int = 10_000,
    source_dialect: Optional[str] = None,
) -> DatabaseStats:
    """
    Generate brief default statistics from a schema alone.

    Used when no real statistics are available.  The generated stats are
    conservative heuristics sufficient to drive synthetic data generation
    (cardinality, null fractions, value ranges).  Replace individual fields
    with real measurements as they become available.

    Parameters
    ----------
    tables:
        Canonical table schemas (from ``load_schema()``).
    row_count:
        Default row count to assign to every table.
    source_dialect:
        Optional hint for the source system (informational only).
    """
    table_stats_list: list[TableStats] = []

    for table in tables:
        col_stats: list[ColumnStats] = []
        indexes: list[IndexStats] = []
        fk_stats: list[ForeignKeyStats] = []

        pk_cols = [c.name for c in table.columns if c.primary_key]

        for col in table.columns:
            ctype = col.type.lower()

            # null_fraction: PKs and NOT NULL cols are 0
            null_frac = 0.0 if (col.primary_key or col.not_null) else 0.05

            # n_distinct
            if col.primary_key:
                n_distinct: float = float(row_count)  # unique per row
            elif col.unique:
                n_distinct = float(row_count)
            elif col.references:
                # FK column: assume the parent has ~10% of child row count
                n_distinct = max(1.0, float(row_count) * 0.1)
            else:
                raw = _DEFAULT_N_DISTINCT.get(ctype, -0.3)
                # negative means fraction → convert to count for default
                n_distinct = float(max(1, int(abs(raw) * row_count)))

            # avg_width
            avg_w = _DEFAULT_AVG_WIDTH.get(ctype, 16)
            if col.length:
                avg_w = min(col.length, max(4, col.length // 2))

            # min/max for simple types
            min_val = max_val = None
            if ctype in ("integer", "long"):
                min_val, max_val = "1", str(row_count)
            elif ctype == "boolean":
                min_val, max_val = "0", "1"
            elif ctype == "decimal":
                min_val, max_val = "0.00", "9999.99"
            elif ctype in ("timestamp", "date"):
                min_val, max_val = "2020-01-01", "2026-12-31"

            # most common values for boolean
            mcv: list[MostCommonValue] = []
            if ctype == "boolean":
                mcv = [MostCommonValue("true", 0.5), MostCommonValue("false", 0.5)]

            col_stats.append(ColumnStats(
                name=col.name,
                null_fraction=null_frac,
                n_distinct=n_distinct,
                avg_width_bytes=avg_w,
                min_value=min_val,
                max_value=max_val,
                most_common_values=mcv,
            ))

        # PK index
        if pk_cols:
            indexes.append(IndexStats(
                name=f"pk_{table.name}",
                columns=pk_cols,
                unique=True,
                cardinality=row_count,
                index_type="BTREE",
            ))

        # FK stats from CanonicalForeignKey constraints
        fk_sources: list[CanonicalForeignKey] = []
        if table.fk_constraints:
            fk_sources = table.fk_constraints
        elif table.foreign_keys:
            for fkd in table.foreign_keys:
                fk_sources.append(CanonicalForeignKey(
                    columns=[fkd.get("column", "")],
                    parent_table=fkd.get("parent_table", ""),
                    parent_columns=[fkd.get("parent_column", "id")],
                ))

        for fkc in fk_sources:
            fk_stats.append(ForeignKeyStats(
                columns=fkc.columns,
                parent_table=fkc.parent_table,
                parent_columns=fkc.parent_columns,
                parent_schema=fkc.parent_schema,
                name=fkc.name,
                cardinality_pattern="N:1",
                match_fraction=1.0,
                avg_children_per_parent=float(max(1, row_count // max(1, int(
                    _DEFAULT_N_DISTINCT.get("integer", -0.1) * -row_count or 1000
                )))),
            ))

        table_stats_list.append(TableStats(
            name=table.name,
            row_count=row_count,
            avg_row_bytes=sum(_DEFAULT_AVG_WIDTH.get(c.type.lower(), 16) for c in table.columns),
            columns=col_stats,
            indexes=indexes,
            foreign_keys=fk_stats,
        ))

    return DatabaseStats(
        tables=table_stats_list,
        source_dialect=source_dialect,
    )


# ---------------------------------------------------------------------------
# Override application
# ---------------------------------------------------------------------------

def _apply_identifier_case(name: str, spec: OverrideSpec) -> str:
    if spec.uppercase_identifiers:
        return name.upper()
    if spec.lowercase_identifiers:
        return name.lower()
    return name


def _apply_reserved_word(name: str, spec: OverrideSpec, dialect: Optional[str] = None) -> str:
    if not spec.reserved_word_prefix:
        return name
    rw = spec.get_reserved_words(dialect)
    if name.lower() in rw:
        return spec.reserved_word_prefix + name
    return name


def _apply_col_override_to_schema(
    col: CanonicalColumn,
    ov: ColumnOverride,
    spec: OverrideSpec,
    target_dialect: Optional[str] = None,
) -> CanonicalColumn:
    """Return a new CanonicalColumn with overrides applied."""
    col = copy.copy(col)

    if ov.rename_to is not None:
        col.name = ov.rename_to
    if ov.type is not None:
        col.type = ov.type
    if ov.length is not None:
        col.length = ov.length
    if ov.precision is not None:
        col.precision = ov.precision
    if ov.scale is not None:
        col.scale = ov.scale
    if ov.not_null is not None:
        col.not_null = ov.not_null
    if ov.default is not None:
        col.default = ov.default

    # Apply global type mapping after column-level type override
    if col.type in spec.type_mappings:
        col.type = spec.type_mappings[col.type]

    # Case normalisation
    col.name = _apply_identifier_case(col.name, spec)
    # Reserved word check
    col.name = _apply_reserved_word(col.name, spec, target_dialect)

    return col


def _apply_col_override_to_stats(
    cs: ColumnStats,
    ov: ColumnOverride,
    new_name: Optional[str],
) -> ColumnStats:
    """Return a new ColumnStats with overrides applied."""
    cs = copy.copy(cs)
    if new_name is not None:
        cs.name = new_name
    if ov.null_fraction is not None:
        cs.null_fraction = ov.null_fraction
    if ov.n_distinct is not None:
        cs.n_distinct = float(ov.n_distinct)
    if ov.min_value is not None:
        cs.min_value = ov.min_value
    if ov.max_value is not None:
        cs.max_value = ov.max_value
    if ov.most_common_values is not None:
        cs.most_common_values = [MostCommonValue.from_dict(v) for v in ov.most_common_values]
    return cs


def apply_overrides(
    schema: CanonicalTableSchema,
    stats: Optional[TableStats],
    spec: OverrideSpec,
    target_dialect: Optional[str] = None,
) -> tuple[CanonicalTableSchema, Optional[TableStats]]:
    """
    Apply an OverrideSpec to a (CanonicalTableSchema, TableStats) pair.

    Returns new objects; the inputs are not modified.

    Parameters
    ----------
    schema:
        Canonical DDL schema for one table.
    stats:
        Statistics for the same table, or None (only schema overrides applied).
    spec:
        Override specification.
    target_dialect:
        Target database dialect — used for reserved-word lookup.

    Returns
    -------
    (new_schema, new_stats)
        new_stats is None when *stats* is None.
    """
    schema = copy.deepcopy(schema)
    stats = copy.deepcopy(stats) if stats is not None else None

    tbl_ov: Optional[TableOverride] = spec.tables.get(schema.name)

    # --- table rename --------------------------------------------------------
    old_table_name = schema.name
    new_table_name = old_table_name

    if tbl_ov and tbl_ov.rename_to:
        new_table_name = tbl_ov.rename_to
    new_table_name = _apply_identifier_case(new_table_name, spec)
    new_table_name = _apply_reserved_word(new_table_name, spec, target_dialect)
    schema.name = new_table_name
    if stats is not None:
        stats.name = new_table_name

    # --- schema / catalog remap ----------------------------------------------
    src_schema = stats.schema if stats else None
    if tbl_ov and tbl_ov.rename_schema_to:
        new_schema = tbl_ov.rename_schema_to
    elif src_schema and src_schema in spec.schema_mappings:
        new_schema = spec.schema_mappings[src_schema]
    elif src_schema:
        new_schema = _apply_identifier_case(src_schema, spec)
    else:
        new_schema = src_schema
    if stats is not None:
        stats.schema = new_schema

    if stats is not None and stats.catalog in spec.catalog_mappings:
        stats.catalog = spec.catalog_mappings[stats.catalog]

    # --- row count -----------------------------------------------------------
    if stats is not None:
        if tbl_ov and tbl_ov.row_count is not None:
            stats.row_count = tbl_ov.row_count
        elif tbl_ov and tbl_ov.row_count_scale is not None:
            stats.row_count = max(1, int(math.ceil(stats.row_count * tbl_ov.row_count_scale)))
        elif spec.row_count_scale is not None:
            stats.row_count = max(1, int(math.ceil(stats.row_count * spec.row_count_scale)))

    # --- column overrides ----------------------------------------------------
    col_overrides: dict[str, ColumnOverride] = (tbl_ov.columns if tbl_ov else {})

    new_columns: list[CanonicalColumn] = []
    col_rename_map: dict[str, str] = {}  # old_name → new_name

    for col in schema.columns:
        ov = col_overrides.get(col.name)
        if ov:
            new_col = _apply_col_override_to_schema(col, ov, spec, target_dialect)
        else:
            # Apply global type mapping + identifier transforms
            new_col = copy.copy(col)
            if new_col.type in spec.type_mappings:
                new_col.type = spec.type_mappings[new_col.type]
            new_col.name = _apply_identifier_case(new_col.name, spec)
            new_col.name = _apply_reserved_word(new_col.name, spec, target_dialect)

        col_rename_map[col.name] = new_col.name
        new_columns.append(new_col)

    schema.columns = new_columns

    # Update FK constraint column names to match renames
    if schema.fk_constraints:
        for fk in schema.fk_constraints:
            fk.columns = [col_rename_map.get(c, c) for c in fk.columns]
            fk.parent_table = _apply_identifier_case(fk.parent_table, spec)
            if fk.parent_schema and fk.parent_schema in spec.schema_mappings:
                fk.parent_schema = spec.schema_mappings[fk.parent_schema]

    # Apply column overrides to stats
    if stats is not None:
        new_col_stats: list[ColumnStats] = []
        stats_by_name = {cs.name: cs for cs in stats.columns}
        for old_name, new_name in col_rename_map.items():
            cs = stats_by_name.get(old_name)
            if cs is None:
                continue
            ov = col_overrides.get(old_name)
            if ov:
                cs = _apply_col_override_to_stats(cs, ov, new_name if new_name != old_name else None)
            elif new_name != old_name:
                cs = copy.copy(cs)
                cs.name = new_name
            new_col_stats.append(cs)
        stats.columns = new_col_stats

        # Update index / FK column names
        for idx in stats.indexes:
            idx.columns = [col_rename_map.get(c, c) for c in idx.columns]
        for fk in stats.foreign_keys:
            fk.columns = [col_rename_map.get(c, c) for c in fk.columns]
            fk.parent_table = _apply_identifier_case(fk.parent_table, spec)
        for cs_stat in stats.composite_stats:
            cs_stat.columns = [col_rename_map.get(c, c) for c in cs_stat.columns]

    return schema, stats


def apply_overrides_all(
    schemas: list[CanonicalTableSchema],
    db_stats: Optional[DatabaseStats],
    spec: OverrideSpec,
    target_dialect: Optional[str] = None,
) -> tuple[list[CanonicalTableSchema], Optional[DatabaseStats]]:
    """
    Apply OverrideSpec to all tables in a schema list + DatabaseStats.

    Parameters
    ----------
    schemas:
        All canonical table schemas.
    db_stats:
        All statistics (matching schemas by table name), or None.
    spec:
        Override specification.
    target_dialect:
        Target dialect for reserved-word lookup.

    Returns
    -------
    (new_schemas, new_db_stats)
    """
    new_schemas: list[CanonicalTableSchema] = []
    new_table_stats: list[TableStats] = []

    for schema in schemas:
        ts = db_stats.table_stats(schema.name) if db_stats else None
        new_schema, new_ts = apply_overrides(schema, ts, spec, target_dialect)
        new_schemas.append(new_schema)
        if new_ts is not None:
            new_table_stats.append(new_ts)

    new_db_stats: Optional[DatabaseStats] = None
    if db_stats is not None:
        new_db_stats = DatabaseStats(
            tables=new_table_stats,
            version=db_stats.version,
            source_dialect=db_stats.source_dialect,
        )

    return new_schemas, new_db_stats
