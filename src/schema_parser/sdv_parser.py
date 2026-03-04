"""
Parse SDV (Synthetic Data Vault) metadata into canonical model.

Schema format source:
  **SDV** – Synthetic Data Vault (sdv-dev/SDV). Metadata spec is JSON (YAML with the same
  structure is supported here). Documented at:
  - https://docs.sdv.dev/sdv/concepts/metadata/metadata-json
  - https://docs.sdv.dev/sdv/reference/metadata-spec/metadata-api

  SDV uses METADATA_SPEC_VERSION, tables (primary_key, columns with sdtype), and
  relationships. Supported sdtypes: numerical, datetime, categorical, boolean, id,
  email, phone_number, and others (see SDV sdtypes reference).
"""

from pathlib import Path
from typing import Any

from .model import CanonicalColumn, CanonicalTableSchema, GenerationRule


# SDV sdtype -> canonical (Spark-oriented) type
SDV_SDTYPE_TO_CANONICAL = {
    "numerical": "long",
    "integer": "integer",
    "float": "float",
    "datetime": "timestamp",
    "date": "timestamp",
    "categorical": "string",
    "boolean": "boolean",
    "id": "long",
    "email": "string",
    "phone_number": "string",
    "address": "string",
    "person_name": "string",
    "unknown": "string",
}


def _parse_sdv_column(col_name: str, spec: dict[str, Any], primary_key: str | None) -> CanonicalColumn:
    """Convert one SDV column spec to CanonicalColumn."""
    sdtype = (spec.get("sdtype") or "categorical").strip().lower()
    canonical_type = SDV_SDTYPE_TO_CANONICAL.get(sdtype, "string")

    # SDV numerical can have computer_representation: Float, Int8, Int16, Int32, Int64, etc.
    if sdtype == "numerical":
        rep = (spec.get("computer_representation") or "").strip()
        if rep in ("Int8", "Int16", "Int32"):
            canonical_type = "integer"
        elif rep in ("Int64", "UInt8", "UInt16", "UInt32", "UInt64"):
            canonical_type = "long"
        elif rep == "Float":
            canonical_type = "float"

    is_primary = primary_key is not None and col_name == primary_key
    description = spec.get("description")
    pii = spec.get("pii")
    constraints: dict[str, Any] = {}
    if pii is not None:
        constraints["pii"] = pii
    if spec.get("regex_format"):
        constraints["regex_format"] = spec["regex_format"]
    if spec.get("datetime_format"):
        constraints["datetime_format"] = spec["datetime_format"]

    generation = None
    if is_primary:
        generation = GenerationRule(unique=True)

    return CanonicalColumn(
        name=col_name,
        type=canonical_type,
        description=description,
        primary_key=is_primary,
        constraints=constraints if constraints else None,
        generation=generation,
        references=None,
    )


def _build_foreign_keys_for_table(
    table_name: str,
    relationships: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """From SDV relationships list, return FK entries where child_table_name == table_name."""
    fk_list: list[dict[str, str]] = []
    for rel in relationships or []:
        if rel.get("child_table_name") != table_name:
            continue
        fk_list.append({
            "column": rel.get("child_foreign_key", ""),
            "parent_table": rel.get("parent_table_name", ""),
            "parent_column": rel.get("parent_primary_key", ""),
        })
    return fk_list if fk_list else None


def parse_sdv_metadata(data: dict[str, Any]) -> list[CanonicalTableSchema]:
    """
    Parse SDV metadata (JSON or YAML with SDV structure) into list of CanonicalTableSchema.

    Expects:
      - tables: dict mapping table name -> { primary_key, columns: { col_name -> { sdtype, ... } } }
      - relationships: list of { parent_table_name, parent_primary_key, child_table_name, child_foreign_key }
    Optional: METADATA_SPEC_VERSION (e.g. "V1") for detection; ignored for parsing.
    """
    tables_spec = data.get("tables") or {}
    relationships = data.get("relationships")
    if isinstance(relationships, list):
        pass
    else:
        relationships = []

    result: list[CanonicalTableSchema] = []
    for table_name, table_def in tables_spec.items():
        if not isinstance(table_def, dict):
            continue
        primary_key = table_def.get("primary_key")
        columns_spec = table_def.get("columns") or {}
        columns: list[CanonicalColumn] = []
        for col_name, col_spec in columns_spec.items():
            if not isinstance(col_spec, dict):
                continue
            columns.append(_parse_sdv_column(col_name, col_spec, primary_key))

        foreign_keys = _build_foreign_keys_for_table(table_name, relationships)
        result.append(CanonicalTableSchema(
            name=table_name,
            columns=columns,
            description=table_def.get("description"),
            foreign_keys=foreign_keys,
        ))
    return result


def parse_sdv_file(path: str | Path) -> list[CanonicalTableSchema]:
    """Load SDV metadata from a JSON or YAML file. Structure must match SDV metadata spec."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"SDV metadata file not found: {path}")

    with open(path, encoding="utf-8") as f:
        content = f.read()

    if path.suffix.lower() in (".json",):
        import json
        data = json.loads(content)
    else:
        import yaml
        data = yaml.safe_load(content)

    if not data or not isinstance(data, dict):
        raise ValueError(f"Invalid SDV metadata: empty or not a dict in {path}")
    return parse_sdv_metadata(data)
