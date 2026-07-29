"""
statschema/services/describe.py — Generate a canonical schema from a structured description.

Used when no source database or DDL is available.  Produces CanonicalTableSchema
objects whose columns have Zipfian (or other) distribution hints set, ready for
``generate()`` and ``inject()`` without any prior ``collect()`` step.
"""
from __future__ import annotations

import random
from typing import Any

from ..core.model import CanonicalColumn, CanonicalTableSchema, GenerationRule
from ..core.stats_io import make_default_stats
from ..stats_model import DatabaseStats

# Maps the short type names accepted by ``describe()`` to canonical type strings.
_TYPE_MAP: dict[str, str] = {
    "int":     "integer",
    "integer": "integer",
    "long":    "long",
    "bigint":  "long",
    "float":   "float",
    "double":  "double",
    "text":    "string",
    "string":  "string",
    "varchar": "string",
    "bool":    "boolean",
    "boolean": "boolean",
    "date":    "date",
    "ts":      "timestamp",
    "timestamp": "timestamp",
}

# Sensible string length for generated text columns.
_DEFAULT_TEXT_LENGTH = 64


def describe(
    num_tables: int = 10,
    num_columns: int = 10,
    pk_type: str = "bigint",
    col_types: list[str] | None = None,
    distribution: str = "zipf",
    distribution_params: dict[str, Any] | None = None,
    seed: int | None = None,
    with_stats: bool = True,
    row_count: int = 10_000,
) -> tuple[list[CanonicalTableSchema], DatabaseStats | None]:
    """Generate a list of CanonicalTableSchema objects from a structured description.

    No source database or DDL file is required.  The resulting schemas can be
    passed directly to ``generate()`` and ``inject()``.

    Parameters
    ----------
    num_tables:
        Number of tables to generate (default: 10).
    num_columns:
        Number of non-PK columns per table (default: 10).
    pk_type:
        Canonical type for the primary key column (default: ``"bigint"``).
        Accepts short aliases (``"bigint"`` → ``"long"``).
    col_types:
        List of type names to draw from when assigning column types.
        Defaults to ``["int", "float", "text"]``.  Types are sampled
        uniformly at random; relative frequency is controlled by repetition
        (e.g. ``["int", "int", "text"]`` makes int twice as likely).
    distribution:
        Distribution assigned to every non-PK column (default: ``"zipf"``).
        Any value accepted by ``GenerationRule.distribution`` is valid.
    distribution_params:
        Free-form dict of distribution-specific parameters, e.g.
        ``{"exponent": 1.5}`` for Zipf.  ``None`` → library defaults.
    seed:
        Integer seed for reproducible column-layout generation.
        ``None`` → non-deterministic.
    with_stats:
        When ``True``, call ``make_default_stats()`` to produce heuristic
        ``DatabaseStats`` from the generated schema.  Pass these to
        ``inject()`` so the optimizer sees cardinality estimates before any
        real data is loaded (default: ``True``).
    row_count:
        Row count hint passed to ``make_default_stats()`` (default: 10 000).

    Returns
    -------
    (tables, stats)
        ``tables`` — list of ``CanonicalTableSchema`` ready for ``generate()`` /
        ``inject()``.
        ``stats`` — ``DatabaseStats`` when ``with_stats=True``, else ``None``.
    """
    rng = random.Random(seed)

    if col_types is None:
        col_types = ["int", "float", "text"]

    resolved_pk_type = _TYPE_MAP.get(pk_type.lower(), "long")
    dist_params: dict[str, Any] = distribution_params or {}

    tables: list[CanonicalTableSchema] = []

    for t_idx in range(1, num_tables + 1):
        table_name = f"table_{t_idx:02d}"

        pk_col = CanonicalColumn(
            name="id",
            type=resolved_pk_type,
            not_null=True,
            primary_key=True,
            auto_increment=True,
            generation=GenerationRule(distribution="sequential"),
        )

        data_cols: list[CanonicalColumn] = []
        for c_idx in range(1, num_columns + 1):
            raw_type = rng.choice(col_types)
            canon_type = _TYPE_MAP.get(raw_type.lower(), "string")
            col_name = f"col_{c_idx:02d}_{raw_type}"

            gen = GenerationRule(
                distribution=distribution,
                distribution_params=dict(dist_params),
            )

            col = CanonicalColumn(
                name=col_name,
                type=canon_type,
                length=_DEFAULT_TEXT_LENGTH if canon_type == "string" else None,
                generation=gen,
            )
            data_cols.append(col)

        tables.append(
            CanonicalTableSchema(
                name=table_name,
                columns=[pk_col, *data_cols],
                row_count=row_count,
            )
        )

    stats: DatabaseStats | None = None
    if with_stats:
        stats = make_default_stats(tables, row_count=row_count)

    return tables, stats
