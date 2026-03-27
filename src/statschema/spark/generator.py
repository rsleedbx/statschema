"""
SparkGenerator — dbldatagen + PySpark backend implementing the RowGenerator Protocol.

Requires ``pip install statschema[spark]`` (PySpark + dbldatagen).

Usage
-----
    from statschema.spark.generator import SparkGenerator
    from statschema.services.generate import generate

    gen = SparkGenerator(spark, partitions=16)
    df = generate(table, rows=1_000_000, stats=ts, generator=gen)
    df.write.saveAsTable("catalog.schema.my_table")

Or directly:

    df = gen.generate(table, rows=1_000_000, stats=ts)
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    pass

from ..core.generator import RowGenerator
from ..core.model import CanonicalTableSchema
from ..core.stats_model import TableStats


class SparkGenerator(RowGenerator):
    """
    Synthetic data backend using Databricks Labs Data Generator (dbldatagen).

    Designed for Databricks-scale generation (100 M+ rows across a cluster).
    For smaller workloads or non-Spark targets, use ``PandasGenerator`` instead.

    Parameters
    ----------
    spark
        Active ``SparkSession``.
    partitions
        Number of Spark partitions for the generated DataFrame.  Defaults to 8.
        Set higher for very large tables (> 100 M rows) to parallelize writes.
    random_type_choice
        Callable returning a random canonical type name for columns whose type
        has no direct Spark equivalent.  Pass ``None`` to use the default
        fallback (``string``).
    apply_boundary_rows
        When ``True`` (default), union one min-value row and one max-value row
        into the output DataFrame for every column whose
        ``GenerationRule.inject_boundary_values`` is set.  Requires ``stats``
        to be provided; silently skips if ``stats`` is ``None``.
    """

    def __init__(
        self,
        spark: Any,
        *,
        partitions: int | None = 8,
        random_type_choice: Any | None = None,
        apply_boundary_rows: bool = True,
    ) -> None:
        self._spark = spark
        self._partitions = partitions
        self._random_type_choice = random_type_choice
        self._apply_boundary_rows = apply_boundary_rows

    def generate(
        self,
        table: CanonicalTableSchema,
        rows: int,
        *,
        stats: TableStats | None = None,
        seed: int | None = None,
        parent_row_counts: dict[str, int] | None = None,
    ) -> Any:
        """
        Build a Spark DataFrame of synthetic rows using dbldatagen.

        Returns
        -------
        pyspark.sql.DataFrame
            Partitioned Spark DataFrame; use ``.write.saveAsTable(...)`` or
            ``.write.format("delta").save(...)`` to persist.
        """
        from .dbldatagen_builder import build_dataframe_from_canonical
        from ..postgen import apply_boundary_rows as _apply_boundary

        df = build_dataframe_from_canonical(
            self._spark,
            table,
            rows,
            partitions=self._partitions,
            seed=seed,
            random_type_choice=self._random_type_choice,
            stats=stats,
            parent_row_counts=parent_row_counts,
        )

        if self._apply_boundary_rows and stats is not None:
            df = _apply_boundary(self._spark, df, table, stats=stats)

        return df


__all__ = ["SparkGenerator"]
