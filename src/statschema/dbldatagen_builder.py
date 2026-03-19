"""
Convert canonical schema to Databricks Labs Data Generator (dbldatagen) column specs.

Produces a list of column specs that can be applied to dg.DataGenerator(spark, ...).withColumn(...).

GenerationRule fields → dbldatagen mapping
------------------------------------------
distribution         "normal"       → distribution="normal" (or dg.distributions.Normal)
                     "zipf"         → dg.distributions.Gamma(0.75, 2.0)  ← approximation
                                       (dbldatagen has no native Zipf)
                     "exponential"  → dg.distributions.Exponential(scale)
                     "sequential"   → deterministic; random=False, no distribution
                     "constant"     → values=[single_value]
                     "uniform"      → default uniform (no distribution arg)
                     "auto"         → heuristic: pick distribution from stats

format_pattern       Named pattern  → template string (see _FORMAT_PATTERN_TEMPLATES)
                     Regex string   → template=pattern  (passed through)

inject_nulls_from_stats + null_fraction → percentNulls=null_fraction

use_mcv_weights + ColumnStats.most_common_values → values + weights list

inject_boundary_values  → post-process: union one row with min_value, one with max_value

inject_rare_events  → NOT natively supported; append tail rows (not yet implemented)
"""

from typing import Any, Callable, Optional

from .model import CanonicalColumn, CanonicalTableSchema, GenerationRule

# ---------------------------------------------------------------------------
# format_pattern → dbldatagen template string
# ---------------------------------------------------------------------------

_FORMAT_PATTERN_TEMPLATES: dict[str, str] = {
    # dbldatagen template mini-language:
    #   \w = random word    \n = random 0-255 number    d = decimal digit
    #   a = lowercase alpha  A = uppercase alpha  x = hex digit
    "email":        r"\w.\w@\w.com|\w@\w.co.u\k",
    "phone_us":     r"(ddd)-ddd-dddd|1(ddd) ddd-dddd",
    "phone_intl":   r"+\n\n-\n-ddd-dddd",
    "ip_v4":        r"\n.\n.\n.\n",
    "ip_v6":        r"xxxx:xxxx:xxxx:xxxx:xxxx:xxxx:xxxx:xxxx",
    "url":          r"https://\w.\w.com/\w/dddd",
    "postal_us":    r"ddddd|ddddd-dddd",
    "postal_uk":    r"A\nd \nAA|AA\nd \nAA",
    "ssn":          r"ddd-dd-dddd",
    "credit_card":  r"dddd-dddd-dddd-dddd",
    "iban":         r"AA\Ndddddddddddddddddddd",
    "name_first":   r"\w",
    "name_last":    r"\w",
    "company":      r"\W \W Ltd|\W \W Inc|\W \W Corp",
    "address":      r"\N \W \W",
    "city":         r"\W",
    "country_iso2": r"AA",
    "currency_iso": r"AAA",
    # UUID: RFC-4122 v4 — dbldatagen uses hex chars (x=lowercase hex)
    "uuid":         r"xxxxxxxx-xxxx-4xxx-xxxx-xxxxxxxxxxxx",
}


def _template_for_format_pattern(pattern: str) -> str:
    """Return a dbldatagen template string for a named format pattern or raw regex."""
    return _FORMAT_PATTERN_TEMPLATES.get(pattern, pattern)


# ---------------------------------------------------------------------------
# distribution name → dbldatagen distribution object or string
# ---------------------------------------------------------------------------

def _dbldatagen_distribution(
    distribution: str,
    params: dict[str, Any],
):
    """
    Convert our canonical distribution name to a dbldatagen distribution argument.

    Returns a string ("normal"), a distribution object, or None (= uniform / default).

    ⚠️  dbldatagen has no native Zipf distribution.  We approximate with Gamma(0.75, 2.0)
    which produces similar heavy-tailed / right-skewed behaviour for moderate datasets.
    The approximation is documented in the builder and in synthetic_data_shortcomings.md.
    """
    dist_lower = distribution.lower()
    if dist_lower in ("auto", "uniform"):
        return None
    if dist_lower == "sequential":
        return None          # handled by caller — no random=True
    if dist_lower == "constant":
        return None          # handled by caller — values=[single]

    try:
        import dbldatagen.distributions as _dist
    except ImportError:
        # No dbldatagen installed — return string hint only for normal
        return "normal" if dist_lower == "normal" else None

    if dist_lower == "normal":
        mean = params.get("mean", 0.0)
        std  = params.get("std", 1.0)
        return _dist.Normal(mean, std)
    if dist_lower == "zipf":
        # Approximate Zipf with Gamma: shape < 1 creates heavy right tail
        # a=1.5 (Zipf exponent) → shape≈0.75 is a reasonable proxy for moderate skew
        a = params.get("a", 1.5)
        shape = max(0.1, 1.5 / a)
        return _dist.Gamma(shape=shape, scale=2.0)
    if dist_lower == "exponential":
        scale = params.get("scale", 1.0)
        return _dist.Exponential(scale=scale)
    if dist_lower == "beta":
        alpha = params.get("alpha", 2.0)
        beta  = params.get("beta", 5.0)
        return _dist.Beta(alpha=alpha, beta=beta)
    if dist_lower == "gamma":
        shape = params.get("shape", 1.0)
        scale = params.get("scale", 2.0)
        return _dist.Gamma(shape=shape, scale=scale)
    return None


# ---------------------------------------------------------------------------
# Spark type map
# ---------------------------------------------------------------------------

try:
    from pyspark.sql.types import (
        BinaryType,
        BooleanType,
        DateType,
        DecimalType,
        DoubleType,
        FloatType,
        IntegerType,
        LongType,
        StringType,
        TimestampType,
    )
    _SPARK_TYPES: dict[str, tuple[Any, dict[str, Any]]] = {
        "integer":    (IntegerType(),   {"minValue": 0, "maxValue": 2**31 - 1, "random": True}),
        "long":       (LongType(),      {"minValue": 0, "maxValue": 2**63 - 1, "random": True}),
        "string":     (StringType(),    {"prefix": "v", "random": True}),
        "float":      (FloatType(),     {"minValue": 0.0, "maxValue": 1.0, "random": True}),
        "double":     (DoubleType(),    {"minValue": 0.0, "maxValue": 1.0, "random": True}),
        "boolean":    (BooleanType(),   {"random": True}),
        "timestamp":  (TimestampType(), {"begin": "2020-01-01 00:00:00",
                                         "end": "2024-12-31 00:00:00", "random": True}),
        "timestamptz":(TimestampType(), {"begin": "2020-01-01 00:00:00",
                                         "end": "2024-12-31 00:00:00", "random": True}),
        "date":       (DateType(),      {"begin": "2020-01-01", "end": "2024-12-31",
                                         "random": True}),
        "decimal":    (DecimalType(18, 4), {"minValue": 0.0, "maxValue": 1e12, "random": True}),
        "binary":     (BinaryType(),    {"random": True}),
        "time":       (StringType(),    {"template": r"dd:dd:dd", "random": True}),
        "timetz":     (StringType(),    {"template": r"dd:dd:dd+dd:dd", "random": True}),
    }
except ImportError:
    _SPARK_TYPES = {}


# ---------------------------------------------------------------------------
# Stat-value type casting
# ---------------------------------------------------------------------------

def _cast_stat_value(value: str, canonical_type: str):
    """
    Cast a stat string value (from ColumnStats.min_value / max_value) to the
    Python type expected by dbldatagen for the given canonical column type.

    Returns None if the value cannot be cast (caller skips the kwarg).
    """
    if value is None:
        return None
    try:
        if canonical_type in ("integer",):
            return int(float(value))
        if canonical_type in ("long",):
            return int(float(value))
        if canonical_type in ("float", "double", "decimal"):
            return float(value)
        if canonical_type in ("timestamp", "timestamptz", "date"):
            # Keep as string — dbldatagen accepts ISO date/datetime strings for these.
            return str(value)
        # For string, binary, uuid, time, etc.: skip min/max (not meaningful for generation)
        return None
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Core column-spec builder
# ---------------------------------------------------------------------------

def _spark_type_and_options(
    col: CanonicalColumn,
    rows: int | None = None,
    random_type_choice: Callable[[], str] | None = None,
    col_stats=None,   # Optional[ColumnStats] from stats_model
) -> tuple[Any, dict[str, Any]]:
    """
    Return (SparkType, kwargs) for dbldatagen .withColumn(name, colType, **kwargs).

    Applies GenerationRule fields in priority order:
      1. Explicit min/max/values/weights from GenerationRule
      2. MCV weights from ColumnStats (when use_mcv_weights=True)
      3. null_fraction from ColumnStats (when inject_nulls_from_stats=True)
      4. format_pattern → template
      5. distribution control
    """
    canonical_type = col.type.lower().strip()
    if canonical_type not in _SPARK_TYPES and random_type_choice:
        canonical_type = random_type_choice()
    if canonical_type not in _SPARK_TYPES:
        canonical_type = "string"

    spark_type, default_kwargs = _SPARK_TYPES[canonical_type]
    opts: dict[str, Any] = dict(default_kwargs)

    # Precision for DECIMAL — use stored precision/scale when available
    if canonical_type == "decimal" and (col.precision is not None or col.scale is not None):
        p = col.precision or 18
        s = col.scale or 4
        spark_type = DecimalType(p, s)

    g: Optional[GenerationRule] = col.generation

    # ── 1. Explicit GenerationRule overrides ─────────────────────────────
    if g:
        if g.min_value is not None:
            opts["minValue"] = g.min_value
        if g.max_value is not None:
            opts["maxValue"] = g.max_value

        # Explicit values list (enum / domain)
        if g.values is not None:
            opts["values"] = g.values
            opts.pop("minValue", None)
            opts.pop("maxValue", None)
        if g.weights is not None:
            opts["weights"] = g.weights

        # Unique constraint — important for PKs
        if g.unique and rows:
            opts["uniqueValues"] = rows
            opts.pop("minValue", None)
            opts.pop("maxValue", None)

    # ── 2. MCV weights from ColumnStats ──────────────────────────────────
    # Shortcoming #2 (hot-spot) and #6 (enum domain)
    # MCVs are applied only when they genuinely dominate the column distribution:
    #   - Explicit request via GenerationRule.use_mcv_weights, OR
    #   - Auto mode: MCVs cover >50% of rows OR column is low-cardinality (≤20 distinct).
    # Applying sparse MCVs from high-cardinality columns (e.g., 10 random integers out of
    # 1000 rows) would collapse the generated cardinality to those 10 values.
    # Boolean columns use random=True natively; MCV strings ("true"/"false"/"0"/"1")
    # cause type-mismatch errors with BooleanType so they are excluded.
    if (col_stats is not None
            and col_stats.most_common_values
            and canonical_type != "boolean"
            and (g is None or g.use_mcv_weights)):
        total_mcv_freq = sum(m.frequency for m in col_stats.most_common_values)
        nd = col_stats.n_distinct if col_stats.n_distinct > 0 else float("inf")
        mcv_meaningful = (
            (g is not None and g.use_mcv_weights)  # explicit request → always apply
            or total_mcv_freq > 0.50                # MCVs dominate the distribution
            or nd <= 20                             # genuinely low-cardinality column
        )
        if mcv_meaningful:
            mcv_values  = [m.value  for m in col_stats.most_common_values]
            mcv_weights = [m.frequency for m in col_stats.most_common_values]
            if g is None or g.values is None:
                opts["values"]  = mcv_values
                opts["weights"] = mcv_weights
                opts.pop("minValue",  None)
                opts.pop("maxValue",  None)
                opts["random"] = True

    # ── 3. NULL injection from ColumnStats ────────────────────────────────
    # Shortcoming #3 (null rates)
    if col_stats is not None and (g is None or g.inject_nulls_from_stats):
        if col_stats.null_fraction and col_stats.null_fraction > 0:
            opts["percentNulls"] = col_stats.null_fraction

    # ── 4. min/max from ColumnStats (when no explicit override) ──────────
    # Shortcoming #4 (boundary values)
    # min_value / max_value from the stats collector are strings; cast them to the
    # appropriate Python type so dbldatagen doesn't attempt str - str arithmetic.
    if col_stats is not None and g is None and "values" not in opts:
        if col_stats.min_value is not None and "minValue" not in opts:
            casted = _cast_stat_value(col_stats.min_value, canonical_type)
            if casted is not None:
                opts["minValue"] = casted
        if col_stats.max_value is not None and "maxValue" not in opts:
            casted = _cast_stat_value(col_stats.max_value, canonical_type)
            if casted is not None:
                opts["maxValue"] = casted

    # ── 5. format_pattern → template ─────────────────────────────────────
    # Shortcoming #5 (realistic string patterns) and #15 (UUID)
    if g and g.format_pattern and canonical_type == "string":
        tmpl = _template_for_format_pattern(g.format_pattern)
        opts["template"] = tmpl
        opts.pop("prefix", None)

    # ── 6. Distribution control ───────────────────────────────────────────
    # Shortcoming #1 (distribution fidelity) and #11 (shape)
    if g and g.distribution not in ("auto", "uniform", None):
        if g.distribution == "sequential":
            opts.pop("random", None)   # deterministic sequence
        else:
            dist_obj = _dbldatagen_distribution(g.distribution, g.distribution_params)
            if dist_obj is not None:
                opts["distribution"] = dist_obj
            opts.setdefault("random", True)

    # ── 7. String length from schema ─────────────────────────────────────
    # dbldatagen 0.4.x does not support a `length` kwarg directly.
    # PR #381 adds it; until that ships on PyPI we approximate via a template
    # that fills up to col.length characters using the `a` (alpha) placeholder.
    # We only do this when no explicit template/format_pattern was already set.
    if col.length is not None and canonical_type == "string" and "template" not in opts:
        # Cap at 64 chars to avoid overly wide templates; real truncation is fine.
        n = min(col.length, 64)
        opts["template"] = r"a" * n

    return spark_type, opts


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def to_dbldatagen_specs(
    table: CanonicalTableSchema,
    rows: int | None = None,
    random_type_choice: Callable[[], str] | None = None,
    stats=None,   # Optional[TableStats] from stats_model
) -> list[tuple[str, Any, dict[str, Any]]]:
    """
    Convert a canonical table schema to a list of (column_name, SparkType, withColumn_kwargs)
    for use with dbldatagen DataGenerator.

    Parameters
    ----------
    table               Canonical table schema from DDL parser.
    rows                Expected row count (used for uniqueValues).
    random_type_choice  Callable that returns a random canonical type name
                        for columns whose type is not in the Spark type map.
    stats               Optional TableStats; when provided, null_fraction, MCVs,
                        min/max, and skewness are incorporated automatically.

    Example usage
    -------------
        specs = to_dbldatagen_specs(canonical_table, rows=1000, stats=table_stats)
        gen = dg.DataGenerator(spark, name="t", rows=1000, ...)
        for name, col_type, kwargs in specs:
            gen = gen.withColumn(name, col_type, **kwargs)
        df = gen.build()
    """
    result: list[tuple[str, Any, dict[str, Any]]] = []
    for col in table.columns:
        col_stats = stats.column_stats(col.name) if stats else None
        spark_type, opts = _spark_type_and_options(
            col,
            rows=rows,
            random_type_choice=random_type_choice,
            col_stats=col_stats,
        )
        result.append((col.name, spark_type, opts))
    return result


def build_dataframe_from_canonical(
    spark: Any,
    table: CanonicalTableSchema,
    rows: int,
    partitions: int | None = 8,
    seed: int | None = None,
    random_type_choice: Callable[[], str] | None = None,
    stats=None,   # Optional[TableStats]
):
    """
    Build a Spark DataFrame from a canonical table schema using dbldatagen.

    Requires dbldatagen and pyspark to be installed (Databricks environment or
    local via ``databricks-connect``).

    Parameters
    ----------
    stats   Optional TableStats; when provided, column statistics are used to
            drive more realistic generation (MCV weights, null rates, boundaries).

    Notes on temporal ordering constraints
    ---------------------------------------
    ``table.temporal_ordering_constraints`` are not applied automatically here.
    They must be enforced **after** generation using a post-processing step:

        df = build_dataframe_from_canonical(spark, table, rows)
        for constraint in table.temporal_ordering_constraints:
            # Parse "end_date > start_date" and fix violations with date_add / re-sample
            # See: apply_temporal_ordering_constraints(df, table)
            ...

    This is because dbldatagen's constraint system (LiteralRange, UniqueCombinations,
    SqlExpr) validates but does not *repair* ordering between two arbitrary columns.
    See synthetic_data_shortcomings.md §2 row #10 for the recommended workaround.
    """
    import dbldatagen as dg

    specs = to_dbldatagen_specs(
        table, rows=rows, random_type_choice=random_type_choice, stats=stats
    )
    gen = (
        dg.DataGenerator(
            spark,
            name=table.name,
            rows=rows,
            partitions=partitions or 8,
            randomSeed=seed if seed is not None else -1,
            randomSeedMethod="hash_fieldname",
        )
        .withIdOutput()
    )
    for name, col_type, kwargs in specs:
        gen = gen.withColumn(name, col_type, **kwargs)
    return gen.build()
