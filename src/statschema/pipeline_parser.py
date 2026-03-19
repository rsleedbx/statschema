"""
Parse pipeline config YAML (current format: tables[].name, tables[].columns[].name/type) into canonical schema.
"""

from typing import Any

from .model import CanonicalColumn, CanonicalTableSchema


# Pipeline type names -> canonical (already aligned with Spark)
PIPELINE_TYPE_TO_CANONICAL = {
    "integer": "integer",
    "long": "long",
    "string": "string",
    "float": "float",
    "double": "double",
    "boolean": "boolean",
    "timestamp": "timestamp",
}


def parse_pipeline_tables(tables: list[dict[str, Any]]) -> list[CanonicalTableSchema]:
    """
    Convert pipeline config 'tables' list to list of CanonicalTableSchema.

    Each table: { "name": str, "columns": [ { "name": str, "type": str | null } ] }
    If type is null or omitted, canonical type is left as "string" (caller can randomize later).
    """
    result: list[CanonicalTableSchema] = []
    for t in tables:
        name = t.get("name") or "table"
        cols_spec = t.get("columns") or []
        columns: list[CanonicalColumn] = []
        for c in cols_spec:
            col_name = c.get("name") or "col"
            raw_type = c.get("type")
            if raw_type is None:
                canonical_type = "string"  # placeholder; can randomize in builder
            else:
                canonical_type = PIPELINE_TYPE_TO_CANONICAL.get(
                    str(raw_type).lower().strip(), str(raw_type).lower()
                )
            columns.append(CanonicalColumn(name=col_name, type=canonical_type))
        result.append(CanonicalTableSchema(name=name, columns=columns))
    return result


def parse_pipeline_config(config: dict[str, Any]) -> list[CanonicalTableSchema]:
    """
    From full pipeline config (with pipeline.tables), return list of CanonicalTableSchema.
    """
    pipeline = config.get("pipeline") or config
    tables = pipeline.get("tables") or []
    return parse_pipeline_tables(tables)
