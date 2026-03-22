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
        # fk_distribution controls how parent rows are sampled during generation:
        #   zipf (default)  — hot parents get many children (power law)
        #   uniform         — every parent equally likely (lookup tables)
        #   normal          — bell-curve around median parent
        # fk_distribution_params: {exponent: 1.5}   # for zipf
        # fk_children_min: 1     # soft lower bound on children per parent
        # fk_children_max: 90    # soft upper bound on children per parent
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

Multi-instance / dedup (optional fields)
-----------------------------------------
A single table definition can represent many physical tables sharing the same schema.

  # 1000 numbered shards (Mautic-style):
  - name: email_stats
    instance_count: 1000          # default suffix: _{:04d} → _0001 … _1000
    # instance_suffix_format: "_{:04d}"   # optional override
    columns: [...]

  # Explicit aliases (same schema, different logical names):
  - name: audit_log
    aliases:
      - audit_log_archive
      - audit_log_staging
    columns: [...]

  # Both combined — numbered instances plus extra aliases:
  - name: events
    instance_count: 3
    aliases: [events_archive]
    columns: [...]
    # → events_0001, events_0002, events_0003, events_archive

Use ``load_canonical(path, expand=True)`` or call
``expand_table_instances(tables)`` to resolve these into individual
``CanonicalTableSchema`` objects before DDL emission or data generation.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional, Union

import yaml

from .model import CanonicalTableSchema, expand_table_instances

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
    *,
    expand: bool = False,
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
    expand
        When ``True``, call :func:`expand_table_instances` automatically so that
        tables with ``instance_count > 1`` or ``aliases`` are resolved into
        individual ``CanonicalTableSchema`` objects before returning.

        When ``False`` (default) the compact representation is returned as-is,
        preserving ``aliases`` / ``instance_count`` on each table for later
        inspection or re-serialization.  Call ``expand_table_instances(tables)``
        manually when you need the flat list.

    Returns
    -------
    List of ``CanonicalTableSchema`` objects ready for DDL emission or data
    generation.

    Examples
    --------
    ::

        # Compact form (default) — preserves instance_count / aliases in memory
        tables = load_canonical("schema.yaml")

        # Expanded form — one CanonicalTableSchema per physical table
        tables = load_canonical("schema.yaml", expand=True)
        for t in tables:
            print(emit_ddl(t, dialect="mysql"))

        # Manual expansion (equivalent to expand=True)
        from statschema import expand_table_instances
        tables = expand_table_instances(load_canonical("schema.yaml"))
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

    tables = [CanonicalTableSchema.from_dict(t) for t in data.get("tables", [])]
    return expand_table_instances(tables) if expand else tables


def resolve_load_order(tables: list[CanonicalTableSchema]) -> list[CanonicalTableSchema]:
    """Return tables in topological load order so every parent is loaded before its children.

    Dependency edges are collected from three sources (in priority order):
    1. ``CanonicalTableSchema.fk_constraints`` — the preferred structured form.
    2. ``CanonicalTableSchema.load_after``     — explicit overrides for logical FKs
       that have no DDL enforcement.
    3. ``CanonicalColumn.references``          — column-level FK tuple (legacy).

    Tables with no dependencies are returned in their original relative order.
    If a circular dependency is detected the original order is returned unchanged
    rather than raising an error, to avoid breaking callers in partial schemas.

    Parameters
    ----------
    tables
        Flat list of canonical tables (call ``expand_table_instances`` first
        if the list contains multi-instance table definitions).

    Returns
    -------
    Reordered list — same objects, new sequence.
    """
    name_to_table: dict[str, CanonicalTableSchema] = {t.name: t for t in tables}

    # table_name → set of table names it depends on
    deps: dict[str, set[str]] = {t.name: set() for t in tables}

    for table in tables:
        if table.fk_constraints:
            for fk in table.fk_constraints:
                if fk.parent_table in name_to_table and fk.parent_table != table.name:
                    deps[table.name].add(fk.parent_table)
        for dep in table.load_after:
            if dep in name_to_table and dep != table.name:
                deps[table.name].add(dep)
        for col in table.columns:
            if col.references:
                parent_table, _ = col.references
                if parent_table in name_to_table and parent_table != table.name:
                    deps[table.name].add(parent_table)

    # Kahn's algorithm for topological sort
    in_degree: dict[str, int] = {t.name: len(deps[t.name]) for t in tables}
    dependents: dict[str, list[str]] = defaultdict(list)
    for child, parents in deps.items():
        for parent in parents:
            dependents[parent].append(child)

    # Seed queue with tables that have no dependencies, preserving original order.
    queue: deque[CanonicalTableSchema] = deque(
        t for t in tables if in_degree[t.name] == 0
    )
    result: list[CanonicalTableSchema] = []
    while queue:
        table = queue.popleft()
        result.append(table)
        for child_name in dependents[table.name]:
            in_degree[child_name] -= 1
            if in_degree[child_name] == 0:
                queue.append(name_to_table[child_name])

    if len(result) != len(tables):
        # Circular dependency — return original order to avoid silent data loss.
        return tables
    return result


def resolve_row_counts(
    tables: list[CanonicalTableSchema],
    scale_factor: float = 1.0,
    default_rows: int = 1000,
) -> dict[str, int]:
    """Return a ``{table_name: row_count}`` mapping for data generation.

    Resolution order per table:

    1. ``table.row_count`` — fixed override; ignores scale factor entirely.
    2. ``table.row_count_per_sf * scale_factor`` — scale-aware formula.
    3. ``default_rows`` — fallback when neither field is set.

    Parameters
    ----------
    tables
        Canonical table list.
    scale_factor
        TPC-style scale factor (e.g. ``1.0`` = ~1 GB, ``10.0`` = ~10 GB).
        Only affects tables that set ``row_count_per_sf``.
    default_rows
        Row count for tables that specify neither ``row_count`` nor
        ``row_count_per_sf``.  Defaults to 1 000.

    Returns
    -------
    Dict mapping table name → row count (always >= 1).
    """
    counts: dict[str, int] = {}
    for t in tables:
        if t.row_count is not None:
            counts[t.name] = max(1, t.row_count)
        elif t.row_count_per_sf is not None:
            counts[t.name] = max(1, math.floor(t.row_count_per_sf * scale_factor))
        else:
            counts[t.name] = default_rows
    return counts
