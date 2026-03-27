"""
Table-name rename transform for canonical schemas.

The canonical YAML uses stable logical names (``orders``, ``branch``, etc.).
Some client drivers impose their own naming conventions on the same benchmark
schema — pgbench expects ``pgbench_accounts``, CockroachDB TPC-C uses
``order`` (singular), etc.

``rename_tables`` applies a mapping dict across every name reference in the
schema list (table name, ``load_after``, column ``references``, and
``fk_constraints``) so the downstream pipeline — DDL emission, data
generation, and loading — all use the renamed names consistently.

Built-in presets for well-known workloads are provided in
``TABLE_NAME_PRESETS``.  Any mapping can also be passed directly.

Example — generate pgbench-compatible DDL from the canonical TPC-B YAML:

    from statschema import load_canonical, emit_ddl
    from statschema.schema_transforms import rename_tables, TABLE_NAME_PRESETS

    tables = load_canonical("benchmarks/schemas/tpcb_schema.yaml")
    renamed = rename_tables(tables, TABLE_NAME_PRESETS["pgbench"])

    for t in renamed:
        print(emit_ddl(t, "postgres"))

Example — load TPC-C into CockroachDB using cockroach workload table names:

    from statschema import load_canonical, resolve_load_order, resolve_row_counts
    from statschema.schema_transforms import rename_tables, TABLE_NAME_PRESETS

    tables  = load_canonical("benchmarks/schemas/tpcc_schema.yaml")
    renamed = rename_tables(tables, TABLE_NAME_PRESETS["cockroach-tpcc"])
    ordered = resolve_load_order(renamed)
    counts  = resolve_row_counts(renamed, scale_factor=1.0)
"""

from __future__ import annotations

import dataclasses
from typing import Optional

from ..model import CanonicalColumn, CanonicalForeignKey, CanonicalTableSchema


# ---------------------------------------------------------------------------
# Built-in client presets
# ---------------------------------------------------------------------------

TABLE_NAME_PRESETS: dict[str, dict[str, str]] = {
    # ── pgbench (PostgreSQL built-in TPC-B benchmark) ─────────────────────
    # pgbench hardcodes pgbench_* table names; column names already match
    # the canonical TPC-B YAML (bid/bbalance/tid/tbalance/aid/abalance/filler).
    "pgbench": {
        "branch":  "pgbench_branches",
        "teller":  "pgbench_tellers",
        "account": "pgbench_accounts",
        "history": "pgbench_history",
    },

    # ── cockroach-tpcc (cockroach workload tpcc) ───────────────────────────
    # CockroachDB uses "order" (singular) where the TPC-C spec and most
    # implementations use "orders".  All other canonical names already match.
    "cockroach-tpcc": {
        "orders": "order",
    },

    # ── cockroach-tpch (cockroach workload tpch) ───────────────────────────
    # CockroachDB TPC-H table names match the canonical tpch_schema.yaml;
    # this preset is a no-op but is listed for documentation completeness.
    "cockroach-tpch": {},
}


# ---------------------------------------------------------------------------
# Core rename function
# ---------------------------------------------------------------------------

def rename_tables(
    tables: list[CanonicalTableSchema],
    mapping: dict[str, str],
) -> list[CanonicalTableSchema]:
    """Return a new list of tables with all *mapping* name substitutions applied.

    Every place a table name appears in the schema graph is updated:

    - ``table.name`` itself
    - ``table.load_after`` references
    - ``column.references`` parent-table name
    - ``fk_constraints[*].parent_table`` name

    Tables whose names are not in *mapping* are returned unchanged (a shallow
    copy is still made so the originals are never mutated).

    Parameters
    ----------
    tables
        List of canonical table schemas, typically from ``load_canonical()``.
    mapping
        ``{old_name: new_name}`` dict.  Keys not found in *mapping* are kept
        as-is.  Use ``TABLE_NAME_PRESETS["pgbench"]`` for pgbench-compatible
        TPC-B names, ``TABLE_NAME_PRESETS["cockroach-tpcc"]`` for CockroachDB
        TPC-C names, etc.

    Returns
    -------
    list[CanonicalTableSchema]
        A new list; the input tables are not modified.

    Raises
    ------
    ValueError
        If *mapping* introduces a collision (two canonical names map to the
        same new name), which would produce an ambiguous schema.
    """
    if not mapping:
        return list(tables)

    # Validate: no two distinct canonical names → same new name
    new_names = [mapping.get(t.name, t.name) for t in tables]
    if len(new_names) != len(set(new_names)):
        seen: dict[str, str] = {}
        for t in tables:
            n = mapping.get(t.name, t.name)
            if n in seen:
                raise ValueError(
                    f"rename_tables: collision — both '{seen[n]}' and '{t.name}' "
                    f"map to '{n}'"
                )
            seen[n] = t.name

    return [_rename_table(t, mapping) for t in tables]


def _rename_table(
    table: CanonicalTableSchema,
    mapping: dict[str, str],
) -> CanonicalTableSchema:
    """Apply *mapping* to a single table and all its cross-references."""

    new_name   = mapping.get(table.name, table.name)
    new_load_after = [mapping.get(n, n) for n in table.load_after]

    new_cols = [_rename_col(col, mapping) for col in table.columns]

    new_fks: list[CanonicalForeignKey] | None = None
    if table.fk_constraints:
        new_fks = [_rename_fk(fk, mapping) for fk in table.fk_constraints]

    return dataclasses.replace(
        table,
        name=new_name,
        load_after=new_load_after,
        columns=new_cols,
        fk_constraints=new_fks,
    )


def _rename_col(col: CanonicalColumn, mapping: dict[str, str]) -> CanonicalColumn:
    if col.references is None:
        return col
    parent_table, parent_col = col.references
    new_parent = mapping.get(parent_table, parent_table)
    if new_parent == parent_table:
        return col
    return dataclasses.replace(col, references=(new_parent, parent_col))


def _rename_fk(fk: CanonicalForeignKey, mapping: dict[str, str]) -> CanonicalForeignKey:
    new_parent = mapping.get(fk.parent_table, fk.parent_table)
    if new_parent == fk.parent_table:
        return fk
    return dataclasses.replace(fk, parent_table=new_parent)


# ---------------------------------------------------------------------------
# Convenience helper: parse a --table-map CLI string
# ---------------------------------------------------------------------------

def parse_table_map(raw: str) -> dict[str, str]:
    """Parse a ``old=new,old2=new2`` string into a mapping dict.

    Accepts either comma-separated or space-separated pairs:

        "orders=order"
        "branch=pgbench_branches,teller=pgbench_tellers"

    Raises ``ValueError`` for malformed input.
    """
    result: dict[str, str] = {}
    if not raw or not raw.strip():
        return result
    # Support both comma and space as pair separators
    pairs = [p.strip() for p in raw.replace(",", " ").split() if p.strip()]
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(
                f"parse_table_map: expected 'old=new' pair, got '{pair}'"
            )
        old, new = pair.split("=", 1)
        if not old or not new:
            raise ValueError(
                f"parse_table_map: empty name in pair '{pair}'"
            )
        result[old.strip()] = new.strip()
    return result
