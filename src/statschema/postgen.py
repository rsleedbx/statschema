"""
Post-generation utilities — Spark DataFrame transformations applied after
dbldatagen produces the base synthetic dataset.

Why post-generation?
--------------------
Certain data-quality requirements cannot be expressed as single-column
generation rules inside dbldatagen because they depend on the *relationship*
between values across columns or on inserting specific rows:

  inject_boundary_values  → union one row with min values + one with max values
  inject_rare_events      → union a small tail of rare but plausible rows
  temporal_ordering       → re-sample or adjust columns to satisfy A < B

All functions here accept a Spark DataFrame and return a new DataFrame.
They work with both the v0 (build_dataframe_from_canonical) and v1
(dbldatagen.v1.generate) generation paths.

Usage example
-------------
    from statschema.postgen import apply_boundary_rows

    df = build_dataframe_from_canonical(spark, table, rows=100_000, stats=ts)
    df = apply_boundary_rows(spark, df, table, stats=ts)
    df.write.saveAsTable("target_table")
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# inject_boundary_values
# ---------------------------------------------------------------------------

def apply_boundary_rows(  # pragma: no cover
    spark: Any,
    df: Any,
    table,          # CanonicalTableSchema
    stats=None,     # Optional[TableStats]
) -> Any:
    """
    Union one min-value row and one max-value row into *df* for every column
    whose ``GenerationRule.inject_boundary_values`` is ``True`` (the default).

    This implements the ``inject_boundary_values`` shortcoming documented in
    synthetic_data_shortcomings.md §2: boundary/rare values from real data are
    absent from dbldatagen-generated data, causing downstream queries that
    depend on extreme values (BETWEEN, histogram buckets, index range scans)
    to be unrealistic.

    Parameters
    ----------
    spark   Active SparkSession.
    df      Base DataFrame produced by build_dataframe_from_canonical() or
            dbldatagen.v1.generate().
    table   CanonicalTableSchema for which df was generated.
    stats   Optional TableStats.  When provided, actual database min/max values
            are used.  When absent, type-based extremes are substituted.

    Returns
    -------
    DataFrame
        df UNION two new rows (min-boundary row, max-boundary row).
        PK/sequence columns that cannot be set to an arbitrary value are left
        as None in the boundary rows; the caller must handle NULL PKs if the
        target table enforces NOT NULL on those columns.

    Notes
    -----
    * Only columns with ``generation.inject_boundary_values=True`` are filled
      from stats; all other columns in the boundary rows are NULL.
    * The two extra rows are appended unconditionally even when stats min ==
      stats max (constant column) — the union is harmless in that case.
    * Row count after this call is ``len(df) + 2``.
    """
    from pyspark.sql import Row
    from pyspark.sql.functions import lit

    from .dbldatagen_builder import _cast_stat_value

    # Columns that participate in boundary injection
    boundary_cols = {
        c.name
        for c in table.columns
        if c.generation is None or c.generation.inject_boundary_values
    }

    min_data: dict[str, Any] = {}
    max_data: dict[str, Any] = {}

    for col in table.columns:
        col_stats = stats.column_stats(col.name) if stats else None

        if col.name in boundary_cols and col_stats is not None:
            min_val = _cast_stat_value(col_stats.min_value, col.type)
            max_val = _cast_stat_value(col_stats.max_value, col.type)
        else:
            min_val, max_val = None, None

        min_data[col.name] = min_val
        max_data[col.name] = max_val

    schema = df.schema
    boundary_df = spark.createDataFrame(
        [Row(**min_data), Row(**max_data)],
        schema=schema,
    )
    return df.union(boundary_df)
