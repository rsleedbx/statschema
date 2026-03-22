"""
Pure-Python row generator driven by the canonical schema model.

This module is the non-Spark complement of ``build_dataframe_from_canonical``.
It reads ``CanonicalTableSchema.columns[].generation`` rules (``GenerationRule``)
and FK constraints to produce a streaming ``Iterator[dict]`` of synthetic rows
without requiring Spark or any third-party library beyond the standard library.

Primary use-cases
-----------------
- Benchmark data loading from canonical YAML into any DBAPI2 database.
- Offline / CI generation without a Databricks/Spark environment.
- Simple integration tests where dbldatagen's Spark overhead is unwanted.

Priority order for each column value
-------------------------------------
1. ``generation.distribution == "sequential"``  — monotone counter from min_value.
2. ``generation.distribution == "constant"``    — fixed value from values[0] or min_value.
3. ``generation.values``                        — weighted random choice.
4. FK range from ``fk_constraints`` or ``col.references`` — random int in [1, parent_rows].
5. ``generation.min_value`` / ``generation.max_value``    — typed range sampling.
6. Type-based fallback                          — sensible defaults per canonical type.

Temporal ordering constraints
------------------------------
``CanonicalTableSchema.temporal_ordering_constraints`` expressions (e.g.
``"l_shipdate <= l_receiptdate"``) are *not* applied automatically — they require
a post-generation re-sample step which is outside the scope of a streaming
generator.  Add a post-processing pass if strict ordering must be enforced.
"""

from __future__ import annotations

import math
import random
import string
from datetime import date, datetime, timedelta
from typing import Any, Iterator

from .model import CanonicalColumn, CanonicalTableSchema, GenerationRule


# ---------------------------------------------------------------------------
# Format-pattern templates (stdlib-only — no Faker)
# ---------------------------------------------------------------------------

def _fmt_phone_us(rng: random.Random) -> str:
    # NXX-NXX-XXXX format (10 chars) — fits US CHAR(10) columns
    return f"{rng.randint(200,999)}-{rng.randint(200,999)}-{rng.randint(1000,9999)}"


def _fmt_phone_intl(rng: random.Random) -> str:
    # TPC-H spec format: CC-NNN-NNN-NNNN — exactly 15 chars, no + prefix
    # CC = 10-34, NNN = 100-999, NNNN = 1000-9999
    cc = rng.randint(10, 34)
    return f"{cc}-{rng.randint(100,999)}-{rng.randint(100,999)}-{rng.randint(1000,9999)}"


def _fmt_postal_us(rng: random.Random) -> str:
    return f"{rng.randint(10000,99999)}"


def _fmt_name_first(rng: random.Random) -> str:
    _first = [
        "Alice", "Bob", "Carol", "David", "Eve", "Frank", "Grace", "Henry",
        "Iris", "Jack", "Karen", "Liam", "Mia", "Noah", "Olivia", "Paul",
        "Quinn", "Rachel", "Sam", "Tara", "Uma", "Victor", "Wendy", "Xavier",
    ]
    return rng.choice(_first)


def _fmt_name_last(rng: random.Random) -> str:
    _last = [
        "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
        "Davis", "Wilson", "Martinez", "Anderson", "Taylor", "Thomas", "Moore",
        "Jackson", "Martin", "Lee", "Perez", "Thompson", "White", "Harris",
    ]
    return rng.choice(_last)


def _fmt_address(rng: random.Random) -> str:
    return (
        f"{rng.randint(1, 9999)} "
        f"{''.join(rng.choices(string.ascii_uppercase, k=1))}"
        f"{''.join(rng.choices(string.ascii_lowercase, k=rng.randint(4, 10)))} St"
    )


def _fmt_city(rng: random.Random) -> str:
    _cities = [
        "Springfield", "Shelbyville", "Oakdale", "Riverside", "Fairview",
        "Madison", "Georgetown", "Burlington", "Salem", "Clinton",
    ]
    return rng.choice(_cities)


_FORMAT_PATTERN_FN = {
    "phone_us":    _fmt_phone_us,
    "phone_intl":  _fmt_phone_intl,
    "postal_us":   _fmt_postal_us,
    "name_first":  _fmt_name_first,
    "name_last":   _fmt_name_last,
    "address":     _fmt_address,
    "city":        _fmt_city,
}


# ---------------------------------------------------------------------------
# Type-aware value sampler
# ---------------------------------------------------------------------------

def _rand_string(length: int, rng: random.Random) -> str:
    n = max(1, min(length, 64))
    return "".join(rng.choices(string.ascii_lowercase, k=n))


def _rand_date(min_d: date, max_d: date, rng: random.Random) -> date:
    delta = max(0, (max_d - min_d).days)
    return min_d + timedelta(days=rng.randint(0, delta))


def _parse_date(v: Any) -> date:
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _gen_value(
    col: CanonicalColumn,
    g: GenerationRule | None,
    fk_max: int | None,
    row_idx: int,
    rng: random.Random,
    row_offset: int = 0,
) -> Any:
    """Generate a single value for *col* at row index *row_idx*.

    *row_offset* shifts sequential counters so that an append run continues
    from where a previous run ended without PK collisions.
    """
    ctype = col.type.lower().strip()

    # ── 1. Sequential ───────────────────────────────────────────────────────
    if g is not None and g.distribution == "sequential":
        base = int(g.min_value) if g.min_value is not None else 1
        return base + row_offset + row_idx

    # ── 2. Constant ─────────────────────────────────────────────────────────
    if g is not None and g.distribution == "constant":
        if g.values:
            return g.values[0]
        if g.min_value is not None:
            return g.min_value
        return None

    # ── 3. Explicit values list ─────────────────────────────────────────────
    if g is not None and g.values is not None:
        if g.weights:
            return rng.choices(g.values, weights=g.weights, k=1)[0]
        return rng.choice(g.values)

    # ── 4. FK range ─────────────────────────────────────────────────────────
    if fk_max is not None:
        # Respect explicit GenerationRule min/max over the FK range if set
        if g is not None and g.min_value is not None and g.max_value is not None:
            lo = max(1, int(g.min_value))
            hi = min(fk_max, int(g.max_value))
        else:
            lo, hi = 1, fk_max
        return rng.randint(lo, hi)

    # ── 5. Explicit min/max range ────────────────────────────────────────────
    if g is not None and g.min_value is not None and g.max_value is not None:
        if ctype in ("integer", "long"):
            return rng.randint(int(g.min_value), int(g.max_value))
        if ctype == "decimal":
            val = rng.uniform(float(g.min_value), float(g.max_value))
            scale = col.scale if col.scale is not None else 2
            return round(val, scale)
        if ctype in ("float", "double"):
            return rng.uniform(float(g.min_value), float(g.max_value))
        if ctype == "date":
            return _rand_date(_parse_date(g.min_value), _parse_date(g.max_value), rng)
        if ctype in ("timestamp", "timestamptz"):
            base = _parse_date(g.min_value)
            end  = _parse_date(g.max_value)
            d    = _rand_date(base, end, rng)
            return datetime(d.year, d.month, d.day,
                            rng.randint(0, 23), rng.randint(0, 59), rng.randint(0, 59))

    # ── 5b. format_pattern for strings ──────────────────────────────────────
    if g is not None and g.format_pattern and ctype == "string":
        fn = _FORMAT_PATTERN_FN.get(g.format_pattern)
        if fn:
            return fn(rng)

    # ── 6. Type-based fallback ───────────────────────────────────────────────
    if ctype in ("integer", "long"):
        return rng.randint(1, 1_000_000)
    if ctype == "decimal":
        scale = col.scale if col.scale is not None else 2
        precision = col.precision or 18
        max_int = 10 ** (precision - scale) - 1
        return round(rng.uniform(0.0, float(max_int)), scale)
    if ctype in ("float", "double"):
        return rng.uniform(0.0, 1_000.0)
    if ctype == "boolean":
        return bool(rng.randint(0, 1))
    if ctype == "date":
        base = date(1990, 1, 1)
        return base + timedelta(days=rng.randint(0, 3650))
    if ctype in ("timestamp", "timestamptz"):
        base = date(2000, 1, 1)
        d = base + timedelta(days=rng.randint(0, 8000))
        return datetime(d.year, d.month, d.day,
                        rng.randint(0, 23), rng.randint(0, 59), rng.randint(0, 59))
    # string / binary / unknown — try format_pattern first, then random chars
    if g is not None and g.format_pattern:
        fn = _FORMAT_PATTERN_FN.get(g.format_pattern)
        if fn:
            return fn(rng)
    length = col.length or 16
    return _rand_string(length, rng)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_rows(
    table: CanonicalTableSchema,
    row_count: int,
    parent_row_counts: dict[str, int] | None = None,
    seed: int | None = 42,
    row_offset: int = 0,
) -> Iterator[dict[str, Any]]:
    """
    Yield *row_count* rows of synthetic data for *table* as dicts.

    Column values are determined by the priority chain documented in this
    module's docstring.  FK columns are constrained to ``[1, parent_rows]``
    so every generated child key references a valid parent row when the
    database enforces referential integrity.

    Parameters
    ----------
    table
        Canonical table schema including ``GenerationRule`` annotations.
    row_count
        Number of rows to generate.
    parent_row_counts
        Map of ``{parent_table_name: row_count}`` for all tables already
        loaded (or planned for loading).  Used to constrain FK columns.
        Typically the output of :func:`statschema.resolve_row_counts`.
    seed
        Random seed for reproducibility.  The same seed + same schema always
        produces the same sequence, making benchmarks comparable across runs.
    row_offset
        Number of rows already present in the table from a previous load.
        Sequential columns will continue from ``min_value + row_offset``.
        Pass ``0`` (default) for a fresh load.  For append runs, query
        ``SELECT COUNT(*) FROM table`` and pass the result here.

    Yields
    ------
    ``dict[str, Any]`` — one per row, keys are column names in schema order.

    Example
    -------
    ::

        from statschema import load_canonical, resolve_row_counts
        from statschema.row_generator import generate_rows

        tables = load_canonical("benchmarks/schemas/tpch_schema.yaml")
        row_counts = resolve_row_counts(tables, scale_factor=1.0)
        for table in tables:
            for row in generate_rows(table, row_counts[table.name],
                                     parent_row_counts=row_counts):
                ...
    """
    rng = random.Random(seed)

    # Build FK column → parent max key mapping from fk_constraints and
    # column-level references (fk_constraints takes precedence).
    fk_max: dict[str, int] = {}
    if parent_row_counts:
        if table.fk_constraints:
            for fk in table.fk_constraints:
                parent_count = parent_row_counts.get(fk.parent_table)
                if parent_count:
                    for col_name in fk.columns:
                        fk_max[col_name] = parent_count
        for col in table.columns:
            if col.references and col.name not in fk_max:
                parent_table, _ = col.references
                parent_count = parent_row_counts.get(parent_table)
                if parent_count:
                    fk_max[col.name] = parent_count

    # Pre-compute per-column (g, fk_max) so the inner loop is tight.
    col_specs: list[tuple[CanonicalColumn, GenerationRule | None, int | None]] = [
        (col, col.generation, fk_max.get(col.name))
        for col in table.columns
    ]
    col_names = [col.name for col in table.columns]

    for row_idx in range(row_count):
        yield {
            name: _gen_value(col, g, fkm, row_idx, rng, row_offset)
            for name, (col, g, fkm) in zip(col_names, col_specs)
        }
