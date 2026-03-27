"""
RowGenerator Protocol — common interface for all synthetic data backends.

Three concrete implementations ship out of the box:

  PandasGenerator    numpy/pandas; no JVM; default for all non-Spark targets.
  StreamingGenerator stdlib random; yields dict rows; suited for large tables
                     or when pandas overhead is not desired.
  SparkGenerator     dbldatagen + PySpark; for Databricks-scale generation.
                     Lives in ``statschema.spark.generator``; requires
                     ``pip install statschema[spark]``.

Adding a new backend
--------------------
Implement the ``RowGenerator`` Protocol — a class with a ``generate`` method
matching the signature below.  No base class registration is required; the
Protocol uses structural subtyping (``typing.Protocol``).

    class MyGenerator:
        def generate(
            self,
            table: CanonicalTableSchema,
            rows: int,
            *,
            stats: TableStats | None = None,
            seed: int | None = None,
            parent_row_counts: dict[str, int] | None = None,
        ) -> Any:
            ...

Pass an instance to ``statschema.services.generate.generate(..., generator=MyGenerator())``.
"""

from __future__ import annotations

from typing import Any, Iterator, TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

from .model import CanonicalTableSchema
from .stats_model import TableStats


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

class RowGenerator:
    """
    Structural Protocol for synthetic data backends.

    Any object with a ``generate`` method matching this signature satisfies
    the protocol — no inheritance required.

    Return type is intentionally ``Any`` because:
    - PandasGenerator returns ``pd.DataFrame``
    - StreamingGenerator returns ``Iterator[dict[str, Any]]``
    - SparkGenerator returns a Spark ``DataFrame``

    Callers that need a concrete type should use the concrete class directly.
    """

    def generate(
        self,
        table: CanonicalTableSchema,
        rows: int,
        *,
        stats: TableStats | None = None,
        seed: int | None = None,
        parent_row_counts: dict[str, int] | None = None,
    ) -> Any:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# PandasGenerator
# ---------------------------------------------------------------------------

class PandasGenerator(RowGenerator):
    """
    Synthetic data backend using numpy + pandas.

    Suitable for up to ~10 M rows without a JVM.  Default backend for all
    non-Spark targets (PostgreSQL, MySQL, SQL Server, Oracle, DB2, Lakebase).

    Parameters
    ----------
    fk_range_overrides
        ``{column_name: (min_fk, max_fk)}`` — override FK sampling range for
        specific columns.  Derived from query predicates via
        ``resolve_fk_generation_ranges``.
    """

    def __init__(
        self,
        fk_range_overrides: dict[str, tuple[int, int]] | None = None,
    ) -> None:
        self._fk_range_overrides = fk_range_overrides

    def generate(
        self,
        table: CanonicalTableSchema,
        rows: int,
        *,
        stats: TableStats | None = None,
        seed: int | None = None,
        parent_row_counts: dict[str, int] | None = None,
    ) -> "pd.DataFrame":
        from .pandas_builder import build_rows_from_canonical

        return build_rows_from_canonical(
            table,
            rows,
            stats=stats,
            seed=seed,
            parent_row_counts=parent_row_counts,
            fk_range_overrides=self._fk_range_overrides,
        )


# ---------------------------------------------------------------------------
# StreamingGenerator
# ---------------------------------------------------------------------------

class StreamingGenerator(RowGenerator):
    """
    Synthetic data backend using only the Python standard library.

    Yields rows as ``dict[str, Any]`` one at a time — no pandas or numpy
    dependency, zero peak-memory overhead regardless of table size.  Suited
    for streaming inserts into any DBAPI2 target and for CLI output to CSV/JSONL.

    Limitations vs PandasGenerator
    --------------------------------
    - ``stats`` is accepted but ignored: the streaming generator works from
      ``GenerationRule`` annotations embedded in the schema only.  Run
      ``db_stats_collector.collect_table_stats`` and save the result to a
      schema YAML to bake statistics into ``GenerationRule`` hints before
      using this backend.
    - Temporal ordering constraints are not enforced (no post-generation
      re-sample step is possible in a pure streaming context).

    Parameters
    ----------
    row_offset
        Number of rows already present in the target table.  Sequential
        columns continue from ``min_value + row_offset``.
    """

    def __init__(self, row_offset: int = 0) -> None:
        self._row_offset = row_offset

    def generate(
        self,
        table: CanonicalTableSchema,
        rows: int,
        *,
        stats: TableStats | None = None,  # accepted, not used
        seed: int | None = None,
        parent_row_counts: dict[str, int] | None = None,
    ) -> Iterator[dict[str, Any]]:
        from .row_generator import generate_rows

        return generate_rows(
            table,
            rows,
            parent_row_counts=parent_row_counts,
            seed=seed if seed is not None else 42,
            row_offset=self._row_offset,
        )


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "RowGenerator",
    "PandasGenerator",
    "StreamingGenerator",
]
