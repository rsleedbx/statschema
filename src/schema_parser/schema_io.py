"""
Canonical schema YAML I/O — the persistence layer for the full pipeline.

Pipeline flow
-------------

DDL (any dialect)                   ← one or many source databases
    │
    ▼  parse_ddl() / load_schema()
CanonicalTableSchema (in memory)    ← dialect-independent representation
    │
    ▼  dump_schema()
canonical_schema.yaml               ← portable, human-editable, versionable
    │
    ├──▶  load_canonical() → emit_ddl(dialect)  →  target DDL (any dialect)
    └──▶  load_canonical() → to_dbldatagen_specs() + DatabaseStats
                                        │
                                        ▼
                                Spark DataFrame (via dbldatagen)

This is what makes "many DDL → one canonical YAML → many DDL and data" work.

YAML format
-----------
version: "1.0"
tables:
  - name: orders
    description: "Order table"
    temporal_ordering_constraints:
      - "shipped_at > ordered_at"
    fk_constraints:
      - columns: [customer_id]
        parent_table: customers
        parent_columns: [id]
        name: fk_orders_customer
    columns:
      - name: id
        type: long
        primary_key: true
        not_null: true
        auto_increment: true
      - name: amount
        type: decimal
        precision: 10
        scale: 2
        not_null: true
      - name: status
        type: string
        length: 20
        default: "'pending'"
        generation:
          distribution: zipf
          distribution_params: {a: 1.5}
          use_mcv_weights: true
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

import yaml

from .model import CanonicalTableSchema

_SCHEMA_VERSION = "1.0"


def dump_schema(
    tables: Union[CanonicalTableSchema, list[CanonicalTableSchema]],
    path: Optional[Union[str, Path]] = None,
    version: str = _SCHEMA_VERSION,
) -> str:
    """
    Serialize one or more canonical tables to a YAML string.

    Parameters
    ----------
    tables   A single ``CanonicalTableSchema`` or a list of them.
    path     When given, also write the YAML to this file path.
    version  Schema format version (default "1.0").

    Returns
    -------
    YAML string — the full canonical schema document.

    Example
    -------
    ::

        tables = parse_ddl(mysql_ddl, dialect="mysql")
        yaml_str = dump_schema(tables, path="schema.yaml")

        # Later (or in a different target system):
        tables2 = load_canonical("schema.yaml")
        pg_ddl  = emit_ddl(tables2[0], "postgres")
        specs   = to_dbldatagen_specs(tables2[0], rows=100_000)
    """
    if isinstance(tables, CanonicalTableSchema):
        tables = [tables]

    doc: dict = {
        "version": version,
        "tables":  [t.to_dict() for t in tables],
    }
    yaml_str = yaml.dump(doc, sort_keys=False, allow_unicode=True)

    if path is not None:
        Path(path).write_text(yaml_str, encoding="utf-8")

    return yaml_str


def load_canonical(
    source: Union[str, Path, dict],
) -> list[CanonicalTableSchema]:
    """
    Load canonical tables from a YAML string, file path, or already-parsed dict.

    Parameters
    ----------
    source
        One of:
          - ``Path`` or file-path string → read from file
          - Multi-line YAML string        → parse inline
          - ``dict``                      → use directly (already parsed)

    Returns
    -------
    List of ``CanonicalTableSchema`` objects ready for DDL emission or data
    generation.

    Example
    -------
    ::

        tables = load_canonical("schema.yaml")
        for dialect in ("mysql", "postgres", "sqlserver", "databricks"):
            print(emit_ddl(tables[0], dialect))
    """
    if isinstance(source, dict):
        data = source
    elif isinstance(source, Path):
        data = yaml.safe_load(source.read_text(encoding="utf-8"))
    elif isinstance(source, str) and "\n" not in source.strip():
        # Treat as file path when there are no newlines (i.e. not inline YAML)
        data = yaml.safe_load(Path(source).read_text(encoding="utf-8"))
    else:
        # Inline YAML string
        data = yaml.safe_load(source)

    return [CanonicalTableSchema.from_dict(t) for t in data.get("tables", [])]
