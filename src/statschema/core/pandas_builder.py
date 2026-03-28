"""
Pure-Python synthetic data generator — numpy + pandas, no Spark/JVM required.

Produces a ``pd.DataFrame`` parameterized by a canonical schema, optional
``GenerationRule`` hints, and optional ``ColumnStats`` (null rates, MCVs,
min/max, distributions).

Supported distributions
-----------------------
sequential    monotonically increasing integers/dates from min_value
constant      every row gets the same value (values[0])
cyclic        cycles min..max, repeating every (max-min+1) rows
block         each value held for ``block_size`` consecutive rows
block_cyclic  block-sized steps, cycling over a fixed range
uniform       flat random draw over [min_value, max_value]
normal        Gaussian clamped to [min_value, max_value]
zipf          power-law heavy tail; approximated via numpy.random.zipf
exponential   exponential decay; parameterised by ``scale``
gamma         gamma distribution; parameterised by ``shape``, ``scale``
beta          beta distribution; parameterised by ``alpha``, ``beta``
auto          heuristic: MCVs if available, else uniform (default)

String format patterns
----------------------
Named patterns (email, phone_us, uuid, ip_v4, …) are generated without
any third-party library.  A lightweight template interpreter handles the
same named patterns as the dbldatagen builder.

Scale
-----
Suitable for evaluation and CI workloads up to ~10 M rows.  For
Databricks-scale generation (100 M+ rows across a cluster) use
``build_dataframe_from_canonical`` from ``statschema[spark]`` instead.
"""

from __future__ import annotations

import re
import uuid as _uuid_mod
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd

from .model import CanonicalColumn, CanonicalTableSchema, GenerationRule, build_fk_max_map
from .semantic_hints import infer_format_pattern

# ---------------------------------------------------------------------------
# String pattern generation
# ---------------------------------------------------------------------------

# A small but sufficient word list for realistic-looking names/domains
_WORDS = [
    "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf",
    "hotel", "india", "juliet", "kilo", "lima", "mike", "november",
    "oscar", "papa", "quebec", "romeo", "sierra", "tango", "uniform",
    "victor", "whiskey", "xray", "yankee", "zulu", "acme", "apex",
    "nova", "orbit", "pulse", "quest", "relay", "spark", "swift",
    "titan", "ultra", "vertex", "wave", "zenith",
]

_COUNTRY_ISO2 = [
    "US", "GB", "DE", "FR", "JP", "CN", "IN", "BR", "CA", "AU",
    "MX", "IT", "ES", "KR", "NL", "CH", "SE", "NO", "DK", "FI",
]

_CURRENCY_ISO = [
    "USD", "EUR", "GBP", "JPY", "CNY", "INR", "BRL", "CAD", "AUD",
    "MXN", "CHF", "SEK", "NOK", "DKK", "SGD", "HKD", "KRW", "PLN",
]


def _rng_words(rng: np.random.Generator, n: int) -> list[str]:
    idx = rng.integers(0, len(_WORDS), size=n)
    return [_WORDS[i] for i in idx]


def _generate_pattern(pattern: str, n: int, rng: np.random.Generator) -> list[str]:
    """
    Generate ``n`` strings matching a named format pattern.

    Matches the named patterns exposed in model.GenerationRule.format_pattern.
    Returns plain random strings for unrecognised patterns.
    """
    if pattern == "email":
        users   = _rng_words(rng, n)
        domains = _rng_words(rng, n)
        tlds    = rng.choice(["com", "net", "org", "io"], size=n)
        return [f"{u}@{d}.{t}" for u, d, t in zip(users, domains, tlds)]

    if pattern in ("phone_us", "phone"):
        area = rng.integers(200, 999, size=n)
        mid  = rng.integers(100, 999, size=n)
        last = rng.integers(1000, 9999, size=n)
        return [f"({a})-{m}-{l}" for a, m, l in zip(area, mid, last)]

    if pattern == "phone_intl":
        cc   = rng.integers(1, 99, size=n)
        mid  = rng.integers(100, 999, size=n)
        last = rng.integers(1000, 9999, size=n)
        return [f"+{c}-{m}-{l}" for c, m, l in zip(cc, mid, last)]

    if pattern == "uuid":
        # Build UUID from 16 independent random bytes — avoids int64 overflow
        return [
            str(_uuid_mod.UUID(bytes=bytes(rng.integers(0, 256, size=16).tolist())))
            for _ in range(n)
        ]

    if pattern == "ip_v4":
        octs = rng.integers(0, 256, size=(n, 4))
        return [".".join(str(b) for b in row) for row in octs]

    if pattern == "ip_v6":
        hexes = rng.integers(0, 0xFFFF, size=(n, 8))
        return [":".join(f"{h:04x}" for h in row) for row in hexes]

    if pattern == "url":
        words   = _rng_words(rng, n)
        domains = _rng_words(rng, n)
        paths   = rng.integers(100, 9999, size=n)
        return [f"https://{d}.com/{w}/{p}" for w, d, p in zip(words, domains, paths)]

    if pattern == "postal_us":
        zips = rng.integers(10000, 99999, size=n)
        return [str(z) for z in zips]

    if pattern == "postal_uk":
        letters = "ABCDEFGHIJKLMNOPRSTUVWXY"
        result = []
        for _ in range(n):
            a1 = letters[rng.integers(0, len(letters))]
            a2 = letters[rng.integers(0, len(letters))]
            d1 = rng.integers(1, 9)
            d2 = rng.integers(1, 9)
            b1 = letters[rng.integers(0, len(letters))]
            b2 = letters[rng.integers(0, len(letters))]
            result.append(f"{a1}{a2}{d1} {d2}{b1}{b2}")
        return result

    if pattern == "ssn":
        a = rng.integers(100, 999, size=n)
        b = rng.integers(10, 99, size=n)
        c = rng.integers(1000, 9999, size=n)
        return [f"{x}-{y}-{z}" for x, y, z in zip(a, b, c)]

    if pattern == "credit_card":
        parts = [rng.integers(1000, 9999, size=n) for _ in range(4)]
        return [f"{p[0]}-{p[1]}-{p[2]}-{p[3]}" for p in zip(*parts)]

    if pattern == "iban":
        ccs   = rng.choice(_COUNTRY_ISO2, size=n)
        digs  = rng.integers(10, 99, size=n)
        accts = rng.integers(10**14, 10**16 - 1, size=n, dtype=np.int64)
        return [f"{cc}{d}{a}" for cc, d, a in zip(ccs, digs, accts)]

    if pattern in ("name_first", "name_last"):
        words = _rng_words(rng, n)
        return [w.capitalize() for w in words]

    if pattern == "company":
        w1   = _rng_words(rng, n)
        w2   = _rng_words(rng, n)
        sfxs = rng.choice(["Ltd", "Inc", "Corp", "LLC", "GmbH"], size=n)
        return [f"{a.capitalize()} {b.capitalize()} {s}" for a, b, s in zip(w1, w2, sfxs)]

    if pattern == "address":
        nums  = rng.integers(1, 9999, size=n)
        w1    = _rng_words(rng, n)
        w2    = _rng_words(rng, n)
        return [f"{num} {a.capitalize()} {b.capitalize()}" for num, a, b in zip(nums, w1, w2)]

    if pattern == "city":
        words = _rng_words(rng, n)
        return [w.capitalize() for w in words]

    if pattern == "country_iso2":
        return list(rng.choice(_COUNTRY_ISO2, size=n))

    if pattern == "currency_iso":
        return list(rng.choice(_CURRENCY_ISO, size=n))

    # Unknown pattern — generate alphanumeric strings of length ~12
    chars = np.array(list("abcdefghijklmnopqrstuvwxyz0123456789"))
    return ["".join(chars[rng.integers(0, len(chars), size=12)]) for _ in range(n)]


# ---------------------------------------------------------------------------
# Distribution helpers
# ---------------------------------------------------------------------------

def _zipf_array(rng: np.random.Generator, a: float, lo: int, hi: int, n: int) -> np.ndarray:
    """
    Generate n integers in [lo, hi] with Zipf-like (power-law) distribution.

    numpy.random.zipf generates values in [1, ∞); clip to [1, range] then shift.
    """
    span = max(hi - lo + 1, 1)
    # Draw with some headroom so clip doesn't flatten the tail excessively
    raw = rng.zipf(max(a, 1.01), size=n * 2)
    raw = np.clip(raw, 1, span)
    raw = (raw - 1) + lo       # shift to [lo, hi]
    # The oversample handles cases where zipf generates very large values early;
    # trim back to n after clipping.
    return raw[:n].astype(np.int64)


_TEMPORAL_DEFAULT_LO = datetime(2020, 1, 1)
_TEMPORAL_DEFAULT_HI = datetime(2024, 12, 31)


def _temporal_origin_and_span(
    col_type: str,
    lo: Optional[Any],
    hi: Optional[Any],
) -> tuple[datetime, float]:
    """
    Return (origin_datetime, span_seconds) for timestamp/date/time columns.

    Default range: 2020-01-01 → 2024-12-31.
    """
    default_lo = _TEMPORAL_DEFAULT_LO
    default_hi = _TEMPORAL_DEFAULT_HI

    def _parse(v: Any, default: datetime) -> datetime:
        if v is None:
            return default
        if isinstance(v, datetime):
            return v
        if isinstance(v, date):
            return datetime(v.year, v.month, v.day)
        try:
            return datetime.fromisoformat(str(v))
        except ValueError:
            return default

    origin = _parse(lo, default_lo)
    end    = _parse(hi, default_hi)
    span   = max((end - origin).total_seconds(), 1.0)
    return origin, span


# ---------------------------------------------------------------------------
# Per-column array generator
# ---------------------------------------------------------------------------

def _generate_column(
    col: CanonicalColumn,
    n: int,
    rng: np.random.Generator,
    col_stats: Any | None = None,   # Optional[ColumnStats]
    fk_max: int | None = None,
) -> np.ndarray | list:
    """
    Generate ``n`` values for a single canonical column.

    Returns a numpy array or a Python list (for object-dtype columns).
    """
    g: Optional[GenerationRule] = col.generation
    ctype = col.type.lower().strip()
    if ctype == "bigint":
        ctype = "long"      # bigint is an alias for long in the canonical type system
    elif ctype == "smallint":
        ctype = "integer"   # smallint maps to 32-bit integer generation

    # ── Explicit values list (enum / MCV domain override) ────────────────
    values: Optional[list[Any]] = g.values if g else None
    weights: Optional[list[float]] = g.weights if g else None

    # Detect near-unique columns from stats (n_distinct ≥ 95 % of rows).
    # These are effective PKs: force a sequential unique sequence regardless
    # of whether the schema has an explicit unique/sequential rule.
    _is_unique_col = (
        (g is not None and (g.unique or g.distribution == "sequential"))
        or (
            col_stats is not None
            and col_stats.n_distinct is not None
            and col_stats.n_distinct >= n * 0.95
        )
    )

    # MCV from stats (when no explicit values list is set).
    # Skip MCVs for unique/sequential columns — their whole purpose is to
    # produce distinct values, and MCVs from a GROUP-BY on a PK column only
    # capture an arbitrary 10-element subset which would make every generated
    # row collide with one of those 10 values.
    # Also skip MCVs for FK-constrained columns: the target must sample from the
    # full FK range [1, fk_max] to produce the same distinct-value count as the
    # source.  Using MCVs would limit the target to at most len(MCVs) distinct
    # values, causing autovacuum ANALYZE to record the wrong n_distinct and
    # producing divergent join-cardinality estimates in Phase E.
    if (
        values is None
        and fk_max is None
        and not _is_unique_col
        and col_stats is not None
        and col_stats.most_common_values
        and ctype != "boolean"
        and (g is None or g.use_mcv_weights)
    ):
        total_freq = sum(m.frequency for m in col_stats.most_common_values)
        nd = col_stats.n_distinct if col_stats.n_distinct > 0 else float("inf")
        if (g is not None and g.use_mcv_weights) or total_freq > 0.50 or nd <= 20:
            values  = [m.value  for m in col_stats.most_common_values]
            weights = [m.frequency for m in col_stats.most_common_values]

    # ── FK range: integers constrained to [1, fk_max] ────────────────────
    # Lower priority than explicit GenerationRule values/unique/min/max.
    use_fk = (
        fk_max is not None
        and values is None
        and ctype in ("integer", "long")
        and (g is None or (g.values is None and not g.unique))
    )

    # ── Distribution parameters ───────────────────────────────────────────
    dist   = (g.distribution if g else "auto") or "auto"
    params = (g.distribution_params if g else {}) or {}

    # Resolve effective min / max
    min_val = g.min_value if g else None
    max_val = g.max_value if g else None
    if col_stats and g is None:
        # Use stats boundaries when no explicit rule is set
        if min_val is None and col_stats.min_value is not None:
            min_val = col_stats.min_value
        if max_val is None and col_stats.max_value is not None:
            max_val = col_stats.max_value

    # ── Weighted / enum draw ──────────────────────────────────────────────
    if values is not None:
        probs: Optional[np.ndarray] = None
        if weights:
            w = np.array(weights, dtype=float)
            total = w.sum()
            if total > 0:
                probs = w / total
        chosen = rng.choice(values, size=n, p=probs)
        return _apply_nulls(chosen, col, g, col_stats, n, rng)

    # ── Sequential (explicit rule, or stats-inferred unique column) ───────
    if dist == "sequential" or (
        _is_unique_col
        and ctype in ("integer", "long")
        and dist == "auto"
        and values is None
    ):
        start = int(min_val) if min_val is not None else 1
        arr = np.arange(start, start + n, dtype=np.int64)
        return _apply_nulls(arr, col, g, col_stats, n, rng)

    # ── Constant ─────────────────────────────────────────────────────────
    if dist == "constant":
        val = min_val if min_val is not None else 0
        arr = np.full(n, val)
        return _apply_nulls(arr, col, g, col_stats, n, rng)

    # ── Cyclic ───────────────────────────────────────────────────────────
    if dist == "cyclic":
        lo = int(min_val) if min_val is not None else 1
        hi = int(max_val) if max_val is not None else lo + 999
        cycle = hi - lo + 1
        arr = (np.arange(n, dtype=np.int64) % cycle) + lo
        return _apply_nulls(arr, col, g, col_stats, n, rng)

    # ── Block ─────────────────────────────────────────────────────────────
    if dist == "block":
        lo         = int(min_val) if min_val is not None else 1
        block_size = int(params.get("block_size", 1))
        arr = (np.arange(n, dtype=np.int64) // block_size) + lo
        return _apply_nulls(arr, col, g, col_stats, n, rng)

    # ── Block cyclic ──────────────────────────────────────────────────────
    if dist == "block_cyclic":
        lo         = int(min_val) if min_val is not None else 1
        block_size = int(params.get("block_size", 1))
        cycle      = int(params.get("cycle", 1000))
        arr = ((np.arange(n, dtype=np.int64) // block_size) % cycle) + lo
        return _apply_nulls(arr, col, g, col_stats, n, rng)

    # ── Boolean ───────────────────────────────────────────────────────────
    if ctype == "boolean":
        arr = rng.integers(0, 2, size=n).astype(bool)
        return _apply_nulls(arr, col, g, col_stats, n, rng)

    # ── Temporal types ────────────────────────────────────────────────────
    if ctype in ("timestamp", "timestamptz", "date", "time", "timetz"):
        return _generate_temporal(ctype, n, rng, g, min_val, max_val, col, col_stats)

    # ── String ────────────────────────────────────────────────────────────
    if ctype in ("string", "varchar", "char", "text", "clob", "nvarchar", "nchar"):
        return _generate_string(col, n, rng, g, col_stats)

    # ── UUID ───────────────────────────────────────────────────────────────
    if ctype == "uuid":
        return [
            str(_uuid_mod.UUID(bytes=bytes(rng.integers(0, 256, size=16).tolist())))
            for _ in range(n)
        ]

    # ── Binary ────────────────────────────────────────────────────────────
    if ctype == "binary":
        byte_len = (col.length or 16)
        return [bytes(rng.integers(0, 256, size=byte_len).tolist()) for _ in range(n)]

    # ── Numeric: integer / long ───────────────────────────────────────────
    if ctype in ("integer", "long"):
        dtype   = np.int32 if ctype == "integer" else np.int64
        max_int = 2**31 - 1 if ctype == "integer" else 2**62
        lo = int(min_val) if min_val is not None else (1 if use_fk else 0)
        hi = int(max_val) if max_val is not None else (fk_max if use_fk else max_int)
        hi = max(lo + 1, hi)

        if dist in ("zipf",):
            a = float(params.get("exponent", params.get("a", 1.5)))
            arr = _zipf_array(rng, a, lo, hi, n).astype(dtype)
        elif dist == "normal":
            mean = float(params.get("mean", (lo + hi) / 2))
            std  = float(params.get("std",  (hi - lo) / 6))
            arr  = rng.normal(mean, std, size=n)
            arr  = np.clip(arr, lo, hi).astype(dtype)
        elif dist == "exponential":
            scale = float(params.get("scale", max(1.0, (hi - lo) / 5)))
            arr   = rng.exponential(scale, size=n)
            arr   = np.clip(arr + lo, lo, hi).astype(dtype)
        else:
            # uniform (default) or auto
            arr = rng.integers(lo, hi + 1, size=n, dtype=dtype)

        if g and g.unique:
            arr = np.arange(lo, lo + n, dtype=dtype)
            rng.shuffle(arr)

        return _apply_nulls(arr, col, g, col_stats, n, rng)

    # ── Numeric: float / double / decimal ────────────────────────────────
    if ctype in ("float", "double", "decimal"):
        lo_f = float(min_val) if min_val is not None else 0.0
        hi_f = float(max_val) if max_val is not None else 1e6

        if dist == "normal":
            mean = float(params.get("mean", (lo_f + hi_f) / 2))
            std  = float(params.get("std",  (hi_f - lo_f) / 6))
            arr  = rng.normal(mean, std, size=n)
            arr  = np.clip(arr, lo_f, hi_f)
        elif dist == "exponential":
            scale = float(params.get("scale", max(1.0, (hi_f - lo_f) / 5)))
            arr   = np.clip(rng.exponential(scale, size=n) + lo_f, lo_f, hi_f)
        elif dist in ("gamma",):
            shape = float(params.get("shape", 1.0))
            scale = float(params.get("scale", max(1.0, (hi_f - lo_f) / 5)))
            arr   = np.clip(rng.gamma(shape, scale, size=n) + lo_f, lo_f, hi_f)
        elif dist == "beta":
            alpha = float(params.get("alpha", 2.0))
            beta  = float(params.get("beta", 5.0))
            arr   = rng.beta(alpha, beta, size=n) * (hi_f - lo_f) + lo_f
        else:
            arr = rng.uniform(lo_f, hi_f, size=n)

        if ctype == "decimal":
            scale = col.scale if col.scale is not None else 4
            arr = np.round(arr, scale)
        elif ctype == "float":
            arr = arr.astype(np.float32)

        return _apply_nulls(arr, col, g, col_stats, n, rng)

    # Fallback: generate opaque strings for unknown types
    return [f"{ctype}_{i}" for i in range(n)]


# ---------------------------------------------------------------------------
# String column generator
# ---------------------------------------------------------------------------

def _generate_string(
    col: CanonicalColumn,
    n: int,
    rng: np.random.Generator,
    g: Optional[GenerationRule],
    col_stats: Any | None,
) -> list[str | None]:
    """Generate string column values."""
    fp = (g.format_pattern if g else None) or infer_format_pattern(
        col.name, col_comment=col.comment, col_description=col.description
    )
    max_len = (g.max_length if g else None) or col.length

    if fp:
        result: list[str | None] = _generate_pattern(fp, n, rng)
    elif max_len:
        chars = np.array(list("abcdefghijklmnopqrstuvwxyz"))
        result = [
            "".join(chars[rng.integers(0, 26, size=min(max_len, 32))].tolist())
            for _ in range(n)
        ]
    else:
        words = _rng_words(rng, n)
        nums  = rng.integers(0, 10000, size=n)
        result = [f"{w}_{num}" for w, num in zip(words, nums)]

    if max_len:
        result = [s[:max_len] if s else s for s in result]

    # Adjust string lengths to match the source's avg_width_bytes (from pg_stats).
    # Postgres reports avg_width as storage bytes = 1 (varlena header) + char length
    # for strings < 127 bytes, so we subtract 1 to get the target character length.
    # This ensures physical page density (relpages) matches the source, which keeps
    # the query planner's join-order cost model consistent between source and target.
    if col_stats and col_stats.avg_width_bytes:
        target_str_len = max(1, int(col_stats.avg_width_bytes) - 1)
        if max_len:
            target_str_len = min(target_str_len, max_len)
        chars_arr = np.array(list("abcdefghijklmnopqrstuvwxyz"))
        adjusted: list[str | None] = []
        for s in result:
            if s is None:
                adjusted.append(None)
            elif len(s) < target_str_len:
                pad = "".join(
                    chars_arr[rng.integers(0, 26, size=target_str_len - len(s))].tolist()
                )
                adjusted.append(s + pad)
            else:
                adjusted.append(s[:target_str_len])
        result = adjusted

    return _apply_nulls(result, col, g, col_stats, n, rng)


# ---------------------------------------------------------------------------
# Temporal column generator
# ---------------------------------------------------------------------------

def _generate_temporal(
    ctype: str,
    n: int,
    rng: np.random.Generator,
    g: Optional[GenerationRule],
    min_val: Any,
    max_val: Any,
    col: CanonicalColumn,
    col_stats: Any | None,
) -> list | np.ndarray:
    if ctype in ("time", "timetz"):
        # Generate random HH:MM:SS strings
        hours   = rng.integers(0, 24, size=n)
        minutes = rng.integers(0, 60, size=n)
        seconds = rng.integers(0, 60, size=n)
        if ctype == "timetz":
            result = [f"{h:02d}:{m:02d}:{s:02d}+00:00"
                      for h, m, s in zip(hours, minutes, seconds)]
        else:
            result = [f"{h:02d}:{m:02d}:{s:02d}"
                      for h, m, s in zip(hours, minutes, seconds)]
        return _apply_nulls(result, col, g, col_stats, n, rng)

    origin, span = _temporal_origin_and_span(ctype, min_val, max_val)
    offsets = rng.uniform(0, span, size=n)
    datetimes = [origin + timedelta(seconds=float(off)) for off in offsets]

    if ctype == "date":
        result = [dt.date() for dt in datetimes]
    else:
        result = datetimes

    return _apply_nulls(result, col, g, col_stats, n, rng)


# ---------------------------------------------------------------------------
# Null injection
# ---------------------------------------------------------------------------

def _apply_nulls(
    arr: Any,
    col: CanonicalColumn,
    g: Optional[GenerationRule],
    col_stats: Any | None,
    n: int,
    rng: np.random.Generator,
) -> Any:
    """
    Randomly set a fraction of values to None/NaN.

    Source priority (highest first):
      1. GenerationRule.null_rate
      2. ColumnStats.null_fraction (when inject_nulls_from_stats=True)
    NOT NULL columns are always left untouched.
    """
    if col.not_null:
        return arr

    null_frac: Optional[float] = None
    if g and g.null_rate is not None:
        null_frac = g.null_rate
    elif (col_stats is not None
          and col_stats.null_fraction
          and col_stats.null_fraction > 0
          and (g is None or g.inject_nulls_from_stats)):
        null_frac = col_stats.null_fraction

    if not null_frac:
        return arr

    null_mask = rng.random(n) < null_frac

    if isinstance(arr, np.ndarray):
        obj = arr.astype(object)
        obj[null_mask] = None
        return obj
    else:
        lst = list(arr)
        for i, m in enumerate(null_mask):
            if m:
                lst[i] = None
        return lst


# ---------------------------------------------------------------------------
# Temporal ordering constraint enforcement
# ---------------------------------------------------------------------------

_CONSTRAINT_RE = re.compile(
    r"^\s*(\w+)\s*(<=?|>=?)\s*(\w+)\s*$"
)


def _enforce_temporal_constraints(
    df: pd.DataFrame,
    constraints: list[str],
    rng: np.random.Generator,
) -> pd.DataFrame:
    """
    Fix rows that violate temporal ordering constraints.

    Supported operators: <, <=, >, >=
    Example: "end_date > start_date" → if end_date <= start_date, add 1–30 days.
    """
    for constraint in constraints:
        m = _CONSTRAINT_RE.match(constraint)
        if not m:
            continue
        left_col, op, right_col = m.group(1), m.group(2), m.group(3)
        if left_col not in df.columns or right_col not in df.columns:
            continue

        left  = df[left_col]
        right = df[right_col]

        # Identify violations
        if op == "<":
            bad = left >= right
        elif op == "<=":
            bad = left > right
        elif op == ">":
            bad = left <= right
        elif op == ">=":
            bad = left < right
        else:
            continue

        if not bad.any():
            continue

        # Fix: shift the "later" column forward by 1–30 days
        num_bad = int(bad.sum())
        shifts  = rng.integers(1, 31, size=num_bad)
        try:
            delta = pd.to_timedelta(shifts, unit="D")
            if op in ("<", "<="):
                target_col = right_col
                base = pd.to_datetime(df.loc[bad, left_col])
            else:
                target_col = left_col
                base = pd.to_datetime(df.loc[bad, right_col])

            new_vals = base + delta

            # Preserve date-only columns: convert Timestamp → datetime.date
            sample = df[target_col].dropna().iloc[0] if df[target_col].notna().any() else None
            if isinstance(sample, date) and not isinstance(sample, datetime):
                new_vals = pd.Series(new_vals).dt.date.values

            df.loc[bad, target_col] = new_vals
        except (TypeError, ValueError, IndexError):
            pass  # column may not be datetime-compatible; skip silently

    return df




# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_rows_from_canonical(
    table: CanonicalTableSchema,
    rows: int,
    stats: Any | None = None,       # Optional[TableStats]
    seed: int | None = None,
    parent_row_counts: dict[str, int] | None = None,
    fk_range_overrides: dict[str, tuple[int, int]] | None = None,
) -> pd.DataFrame:
    """
    Generate a ``pd.DataFrame`` of synthetic rows for a canonical table.

    Parameters
    ----------
    table
        Canonical table schema (from ``parse_ddl`` / ``load_canonical``).
    rows
        Number of rows to generate.
    stats
        Optional ``TableStats`` object.  When provided, ``null_fraction``,
        MCV weights, and ``min_value`` / ``max_value`` are incorporated
        automatically into the generated distribution.
    seed
        Integer seed for reproducibility.  ``None`` → non-deterministic.
    parent_row_counts
        ``{table_name: row_count}`` for all parent tables.  FK integer
        columns are constrained to ``[1, parent_row_count]`` so generated
        child keys always reference valid parent rows.
    fk_range_overrides
        ``{col_name: (min_val, max_val)}`` — override the generation range
        for specific FK columns.  Derived from query predicates via
        ``resolve_fk_generation_ranges``; constrains synthetic fact-table
        FK values to the same data window as the real workload.

    Returns
    -------
    pd.DataFrame with one column per canonical column.
    """
    from dataclasses import replace as _dc_replace
    from .model import GenerationRule

    rng = np.random.default_rng(seed)
    fk_ranges = build_fk_max_map(table, parent_row_counts)

    data: dict[str, Any] = {}
    for col in table.columns:
        col_stats = stats.column_stats(col.name) if stats else None
        fk_max    = fk_ranges.get(col.name)

        # Apply predicate-derived FK range override when present.
        effective_col = col
        effective_fk_max = fk_max
        if fk_range_overrides and col.name in fk_range_overrides:
            ov_min, ov_max = fk_range_overrides[col.name]
            old_gen = col.generation
            if old_gen is not None:
                new_gen = _dc_replace(old_gen, min_value=ov_min, max_value=ov_max)
            else:
                new_gen = GenerationRule(min_value=ov_min, max_value=ov_max)
            effective_col = _dc_replace(col, generation=new_gen)
            effective_fk_max = None  # generation rule takes precedence

        data[col.name] = _generate_column(effective_col, rows, rng, col_stats=col_stats, fk_max=effective_fk_max)

    df = pd.DataFrame(data)

    if table.temporal_ordering_constraints:
        df = _enforce_temporal_constraints(df, table.temporal_ordering_constraints, rng)

    return df
