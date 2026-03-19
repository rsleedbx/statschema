"""
Parse YData/Syda-style YAML schema into canonical model.

Source: YData / Syda (Syda Structured Data, python.syda.ai).
  - Syda YAML examples: https://python.syda.ai/examples/structured_only/yaml_schemas/
  - YData SDK (docs.sdk.ydata.ai) uses a different table-level YAML; this parser implements
    the Syda per-field format (type, description, constraints, __foreign_keys__).
  No formal grammar is published; structure is derived from Syda examples and docs.

Format (per table/file or multi-table):
- __table_description__: optional table description
- __foreign_keys__: optional { column_name: [ParentTable, parent_column] }
- Per-field: name as key, then type, description, constraints (primary_key, unique, max_length), references (for foreign_key)
- Types: number, text, email, foreign_key (and optionally date, datetime, boolean)
"""

from pathlib import Path
from typing import Any

import yaml

from .model import CanonicalColumn, CanonicalTableSchema, GenerationRule


# YData/Syda type -> canonical (Spark-oriented) type
YDATA_TYPE_TO_CANONICAL = {
    "number": "long",  # default to long; can override with generation min/max for int
    "integer": "integer",
    "int": "integer",
    "long": "long",
    "text": "string",
    "string": "string",
    "str": "string",
    "email": "string",
    "boolean": "boolean",
    "bool": "boolean",
    "date": "timestamp",
    "datetime": "timestamp",
    "timestamp": "timestamp",
    "float": "float",
    "double": "double",
    "foreign_key": "long",
}


def _parse_ydata_field(name: str, spec: dict[str, Any]) -> CanonicalColumn:
    raw_type = (spec.get("type") or "text").lower().strip()
    canonical_type = YDATA_TYPE_TO_CANONICAL.get(raw_type, "string")
    description = spec.get("description")
    constraints = spec.get("constraints") or {}
    primary_key = constraints.get("primary_key") is True
    unique = constraints.get("unique") is True
    max_length = constraints.get("max_length")

    generation = None
    if max_length is not None or unique:
        generation = GenerationRule(max_length=max_length, unique=unique)

    references = None
    if raw_type == "foreign_key" and "references" in spec:
        ref = spec["references"]
        if isinstance(ref, dict):
            references = (ref.get("schema", ref.get("table", "")), ref.get("field", ref.get("column", "")))
        elif isinstance(ref, (list, tuple)) and len(ref) >= 2:
            references = (str(ref[0]), str(ref[1]))

    return CanonicalColumn(
        name=name,
        type=canonical_type,
        length=max_length,   # carry YData max_length as DDL length hint
        not_null=primary_key,
        primary_key=primary_key,
        unique=unique,
        description=description,
        constraints=constraints if constraints else None,
        generation=generation,
        references=references,
    )


def parse_ydata_yaml(data: dict[str, Any], table_name: str | None = None) -> CanonicalTableSchema:
    """
    Parse a single-table YData-style YAML (dict) into CanonicalTableSchema.

    Single-table file: keys are __table_description__, __foreign_keys__, and field names.
    If table_name is not provided, uses "table" (caller can pass name from filename).
    """
    reserved = {"__table_description__", "__foreign_keys__", "schema_source", "source", "schema_format", "format"}
    table_description = data.get("__table_description__")
    foreign_keys_section = data.get("__foreign_keys__")
    table_name = table_name or "table"
    fields = {k: v for k, v in data.items() if k not in reserved}

    columns: list[CanonicalColumn] = []
    for key, value in fields.items():
        if key in reserved:
            continue
        if isinstance(value, dict) and ("type" in value or "description" in value or "constraints" in value):
            columns.append(_parse_ydata_field(key, value))
        elif isinstance(value, str):
            columns.append(CanonicalColumn(name=key, type=YDATA_TYPE_TO_CANONICAL.get(value.lower(), "string")))

    fk_list = None
    if isinstance(foreign_keys_section, dict):
        fk_list = [
            {"column": k, "parent_table": v[0], "parent_column": v[1] if len(v) > 1 else "id"}
            for k, v in foreign_keys_section.items()
            if isinstance(v, (list, tuple)) and len(v) >= 1
        ]

    return CanonicalTableSchema(
        name=table_name or "table",
        columns=columns,
        description=table_description,
        foreign_keys=fk_list,
    )


def parse_ydata_yaml_file(path: str | Path, table_name: str | None = None) -> CanonicalTableSchema:
    """Load a YData-style YAML file and return a single CanonicalTableSchema."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not data:
        raise ValueError(f"Empty YAML file: {path}")
    return parse_ydata_yaml(data, table_name=table_name)


def parse_ydata_multi_yaml(data: dict[str, Any]) -> list[CanonicalTableSchema]:
    """
    Parse a multi-table YData YAML where top-level keys are table names
    and values are { __table_description__, __foreign_keys__, field_name: { type, ... } }.
    """
    result: list[CanonicalTableSchema] = []
    reserved = {"__table_description__", "__foreign_keys__", "schema_source", "source", "schema_format", "format"}
    for name, table_spec in data.items():
        if name.startswith("__") or not isinstance(table_spec, dict):
            continue
        result.append(parse_ydata_yaml(table_spec, table_name=name))
    return result
