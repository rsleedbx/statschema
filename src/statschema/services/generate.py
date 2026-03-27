"""
Service: generate

Generate referentially consistent synthetic rows from a canonical schema and
optional collected statistics.  All callers go through this module rather than
calling a backend builder directly.

The active backend is selected via the ``generator`` parameter.  When omitted,
``PandasGenerator`` is used — the default for all non-Spark targets.

    # Default (pandas)
    df = generate(table, rows=50_000, stats=ts)

    # Streaming (stdlib only, yields dict rows)
    from statschema.core.generator import StreamingGenerator
    rows_iter = generate(table, rows=50_000, generator=StreamingGenerator())

    # Spark / dbldatagen (Databricks targets)
    from statschema.spark.generator import SparkGenerator
    spark_df = generate(table, rows=5_000_000, stats=ts,
                        generator=SparkGenerator(spark, partitions=32))
"""

from __future__ import annotations

from typing import Any

from ..core.generator import PandasGenerator, RowGenerator
from ..core.model import CanonicalTableSchema
from ..core.stats_model import TableStats


def generate(
    table: CanonicalTableSchema,
    rows: int,
    stats: TableStats | None = None,
    seed: int | None = None,
    parent_row_counts: dict[str, int] | None = None,
    fk_range_overrides: dict[str, tuple[int, int]] | None = None,
    *,
    generator: RowGenerator | None = None,
) -> Any:
    """Generate synthetic rows for a canonical table.

    Parameters
    ----------
    table:
        Canonical table schema from ``parse_ddl`` / ``load_schema``.
    rows:
        Number of rows to generate.
    stats:
        Optional :class:`~statschema.stats_model.TableStats`.  When provided,
        null fractions, MCV weights, and min/max ranges are incorporated into
        the generated distribution (pandas and Spark backends only).
    seed:
        Integer seed for reproducibility.  ``None`` → non-deterministic.
    parent_row_counts:
        ``{table_name: row_count}`` for all parent tables.  FK integer columns
        sample from ``[1, parent_row_count]`` rather than ``[1, rows]``.
    fk_range_overrides:
        ``{column_name: (min_fk, max_fk)}`` to override the FK sampling range
        for specific columns.  Only applied when using ``PandasGenerator``
        (the default); ignored by other backends.
    generator:
        Backend to use for row synthesis.  Defaults to ``PandasGenerator``.
        Pass a ``StreamingGenerator`` for zero-memory streaming, or a
        ``SparkGenerator`` for Databricks-scale generation.

    Returns
    -------
    Return type depends on the active backend:

    - ``PandasGenerator`` (default) → ``pd.DataFrame``
    - ``StreamingGenerator`` → ``Iterator[dict[str, Any]]``
    - ``SparkGenerator`` → ``pyspark.sql.DataFrame``
    """
    if generator is None:
        generator = PandasGenerator(fk_range_overrides=fk_range_overrides)

    return generator.generate(
        table,
        rows,
        stats=stats,
        seed=seed,
        parent_row_counts=parent_row_counts,
    )
