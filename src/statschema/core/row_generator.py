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

_TEMPORAL_DEFAULT_LO = datetime(2020, 1, 1)
_TEMPORAL_DEFAULT_HI = datetime(2024, 12, 31)
from typing import Any, Iterator

from .builtin_generators import dispatch as _builtin_dispatch
from .model import CanonicalColumn, CanonicalTableSchema, GenerationRule, build_fk_max_map


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


_TPCC_SYLLABLES = (
    "BAR", "OUGHT", "ABLE", "PRI", "PRES", "ESE", "ANTI", "CALLY", "ATION", "EING"
)


def _tpcc_make_last_name(n: int) -> str:
    """0..999 → TPC-C three-syllable last name (e.g. 0 → 'BARBARBAR', 105 → 'ABLEOUGHTPRES')."""
    s = _TPCC_SYLLABLES
    return s[n // 100] + s[(n // 10) % 10] + s[n % 10]


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
# Zipf sampler (no third-party libs)
# ---------------------------------------------------------------------------

def _zipf_sample(rng: random.Random, a: float, n: int) -> int:
    """Return one integer from the discrete Zipf distribution on [1, n].

    Uses Devroye's (1986) rejection method for the zeta distribution.
    P(k) ∝ 1/k^a.  Requires a > 1.  For a ≤ 1 falls back to uniform.

    The expected number of iterations is O(1) for moderate exponents.
    """
    if a <= 1.001 or n <= 1:
        return rng.randint(1, max(1, n))
    b = 2.0 ** (a - 1.0)
    while True:
        u = 1.0 - rng.random()          # (0, 1]
        v = 1.0 - rng.random()
        x = int(u ** (-1.0 / (a - 1.0)))
        if x < 1:
            x = 1
        if x > n:
            continue
        t = (1.0 + 1.0 / x) ** (a - 1.0)
        if v * x * (t - 1.0) * b <= t * (b - 1.0):
            return x


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
    fk_dist: str = "uniform",
    fk_params: dict | None = None,
) -> Any:
    """Generate a single value for *col* at row index *row_idx*.

    *row_offset* shifts sequential counters so that an append run continues
    from where a previous run ended without PK collisions.

    *fk_dist* / *fk_params* control the FK sampling distribution for this
    column when *fk_max* is set.  Supported values: ``"uniform"`` (default),
    ``"zipf"`` (power-law; params: ``{exponent: 1.2}``).
    """
    ctype = col.type.lower().strip()
    if ctype == "bigint":
        ctype = "long"      # bigint is an alias for long in the canonical type system
    elif ctype == "smallint":
        ctype = "integer"   # smallint maps to 32-bit integer generation
    if fk_params is None:
        fk_params = {}

    # ── 1. Sequential ───────────────────────────────────────────────────────
    if g is not None and g.distribution == "sequential":
        base = int(g.min_value) if g.min_value is not None else 1
        return base + row_offset + row_idx

    # ── null_rate — applied before generating a real value, skipped for ──────
    # sequential columns (above), FK columns, and NOT NULL columns.
    if (
        g is not None
        and g.null_rate is not None
        and not col.not_null
        and fk_max is None
        and rng.random() < g.null_rate
    ):
        return None

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

    # ── 1b. Cyclic — cycles min_value..max_value for every row ───────────────
    # Useful for IDs that restart per parent block, e.g. c_id 1..3000 per district.
    if g is not None and g.distribution == "cyclic":
        base = int(g.min_value) if g.min_value is not None else 1
        top  = int(g.max_value) if g.max_value is not None else base + 999
        span = max(1, top - base + 1)
        return base + (row_idx + row_offset) % span

    # ── 1c. Block — increments every block_size rows ─────────────────────────
    # Useful for parent IDs, e.g. c_d_id = 1 for rows 0..2999, 2 for 3000..5999.
    # distribution_params: {block_size: 3000}
    if g is not None and g.distribution == "block":
        base       = int(g.min_value) if g.min_value is not None else 1
        block_size = int((g.distribution_params or {}).get("block_size", 1))
        return base + (row_idx + row_offset) // block_size

    # ── 1d. Block-cyclic — cyclic in block-sized steps ───────────────────────
    # Useful for order_line ol_o_id: each order ID repeats for block_size lines
    # then cycles back. E.g. block_size=10, cycle=3000: IDs 1..3000 × 10 lines each.
    # distribution_params: {block_size: 10, cycle: 3000}
    if g is not None and g.distribution == "block_cyclic":
        base       = int(g.min_value) if g.min_value is not None else 1
        params     = g.distribution_params or {}
        block_size = int(params.get("block_size", 1))
        cycle      = int(params.get("cycle", 1000))
        return base + ((row_idx + row_offset) // block_size) % cycle

    # ── 1e. Zipf — power-law integer sample ──────────────────────────────────
    # distribution_params: {exponent: 1.5, min_value: 1, max_value: N}
    # Produces hot/cold distributions; lower-numbered keys are most frequent.
    if g is not None and g.distribution == "zipf":
        a    = float((g.distribution_params or {}).get("exponent", 1.2))
        lo   = int(g.min_value) if g.min_value is not None else 1
        hi   = int(g.max_value) if g.max_value is not None else 1_000_000
        span = max(1, hi - lo + 1)
        return lo + _zipf_sample(rng, a, span) - 1

    # ── 1f. Normal / Gaussian ────────────────────────────────────────────────
    # distribution_params: {mean: 150000.0, std: 80000.0}
    # Falls back to uniform range when mean/std are absent.
    if g is not None and g.distribution == "normal":
        params = g.distribution_params or {}
        lo  = float(g.min_value) if g.min_value is not None else 0.0
        hi  = float(g.max_value) if g.max_value is not None else 1.0
        mu  = float(params.get("mean", (lo + hi) / 2.0))
        std = float(params.get("std",  (hi - lo) / 6.0))
        val = rng.gauss(mu, std)
        val = max(lo, min(hi, val))          # clamp to [min, max]
        if ctype in ("integer", "long"):
            return int(round(val))
        scale = col.scale if col.scale is not None else 2
        return round(val, scale)

    # ── 4. FK range ─────────────────────────────────────────────────────────
    if fk_max is not None:
        # Respect explicit GenerationRule min/max over the FK range if set
        if g is not None and g.min_value is not None and g.max_value is not None:
            lo = max(1, int(g.min_value))
            hi = min(fk_max, int(g.max_value))
        else:
            lo, hi = 1, fk_max
        if fk_dist == "zipf":
            a = float(fk_params.get("exponent", 1.2))
            return lo + _zipf_sample(rng, a, hi - lo + 1) - 1
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
        if ctype in ("time", "timetz"):
            def _time_to_s(v: Any) -> int:
                p = str(v).split(":")
                return int(p[0]) * 3600 + int(p[1]) * 60 + int(p[2]) if len(p) == 3 else 0
            lo_s = _time_to_s(g.min_value)
            hi_s = _time_to_s(g.max_value)
            s = rng.randint(max(0, lo_s), max(lo_s + 1, hi_s))
            h, rem = divmod(s, 3600)
            m, sec = divmod(rem, 60)
            tz = "+00:00" if ctype == "timetz" else ""
            return f"{h:02d}:{m:02d}:{sec:02d}{tz}"

    # ── 5b. format_pattern for strings ──────────────────────────────────────
    if g is not None and g.format_pattern and ctype in ("string", "varchar", "char"):
        # tpcc_last_name needs the current row position to guarantee full coverage
        # of all 1000 syllable combinations within each block of 3000 customers.
        if g.format_pattern == "tpcc_last_name":
            pos = (row_idx + row_offset) % 3000
            n   = pos if pos < 1000 else rng.randint(0, 999)
            return _tpcc_make_last_name(n)
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
        span_days = (_TEMPORAL_DEFAULT_HI - _TEMPORAL_DEFAULT_LO).days
        return (_TEMPORAL_DEFAULT_LO + timedelta(days=rng.randint(0, span_days))).date()
    if ctype in ("timestamp", "timestamptz"):
        span_days = (_TEMPORAL_DEFAULT_HI - _TEMPORAL_DEFAULT_LO).days
        d = _TEMPORAL_DEFAULT_LO + timedelta(days=rng.randint(0, span_days))
        return datetime(d.year, d.month, d.day,
                        rng.randint(0, 23), rng.randint(0, 59), rng.randint(0, 59))
    if ctype in ("time", "timetz"):
        s = rng.randint(0, 86399)
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        tz = "+00:00" if ctype == "timetz" else ""
        return f"{h:02d}:{m:02d}:{sec:02d}{tz}"
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
    # Delegate entirely to the built-in generator when the table declares one.
    # Built-in generators are deterministic and produce spec-correct rows
    # (e.g. TPC-DS date_dim, time_dim, customer_demographics).
    if table.builtin_generator:
        yield from _builtin_dispatch(table.builtin_generator, row_count, row_offset)
        return

    rng = random.Random(seed)

    # Build FK column → (parent_max, fk_dist, fk_params).
    # build_fk_max_map handles the common {col: count} dict; a second pass
    # attaches distribution metadata (fk_dist / fk_params) from fk_constraints.
    fk_max    = build_fk_max_map(table, parent_row_counts)
    fk_dist:   dict[str, str]  = {}
    fk_params: dict[str, dict] = {}

    if parent_row_counts and table.fk_constraints:
        for fk in table.fk_constraints:
            if parent_row_counts.get(fk.parent_table):
                for col_name in fk.columns:
                    fk_dist[col_name]   = fk.fk_distribution or "uniform"
                    fk_params[col_name] = fk.fk_distribution_params or {}

    # Pre-compute per-column specs so the inner loop is tight.
    col_specs: list[tuple[CanonicalColumn, GenerationRule | None, int | None, str, dict]] = [
        (col, col.generation, fk_max.get(col.name),
         fk_dist.get(col.name, "uniform"), fk_params.get(col.name, {}))
        for col in table.columns
    ]
    col_names = [col.name for col in table.columns]

    for row_idx in range(row_count):
        yield {
            name: _gen_value(col, g, fkm, row_idx, rng, row_offset, fd, fp)
            for name, (col, g, fkm, fd, fp) in zip(col_names, col_specs)
        }
