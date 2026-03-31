"""Spark loaders — SparkInClusterLoader and SparkCloudStagedLoader.

Both loaders accept a pandas DataFrame and write it into a Spark / Delta table.
``conn`` is unused (Spark has no DBAPI2 connection); all I/O goes through
``ctx.spark_session``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from ..base import TopologyAwareLoader

logger = logging.getLogger(__name__)

_SPARK_TOPOLOGIES = frozenset({
    "spark_embedded",
    "spark_connect",
    "databricks_connect",
})


def _to_spark_df(spark: Any, df: Any, col_names: list[str]) -> Any:
    """Convert a pandas DataFrame to a Spark DataFrame, or pass Spark DF through."""
    try:
        import pandas as pd
        if isinstance(df, pd.DataFrame):
            return spark.createDataFrame(df[col_names])
    except ImportError:
        pass
    return df  # already a Spark DataFrame


class SparkInClusterLoader(TopologyAwareLoader):
    """
    Load a pandas (or Spark) DataFrame into a Spark / Delta table via
    ``SparkSession.createDataFrame().write.saveAsTable()``.

    Handles:
      - ``spark_embedded``    — Databricks cluster job or plain Spark notebook
      - ``spark_connect``     — community Spark 3.4+ via gRPC
      - ``databricks_connect``— Databricks Connect SDK

    ``table`` must be a two-part ("schema.table") or three-part
    ("catalog.schema.table") name visible to the session.
    ``conn`` is ignored.
    """

    def can_use(self, ctx: Any, dialect: str, col_types: list[str] | None) -> bool:
        return (
            dialect in ("spark", "databricks", "lakehouse")
            and ctx.topology in _SPARK_TOPOLOGIES
            and ctx.spark_session is not None
        )

    def bulk_load(
        self,
        ctx: Any,
        conn: Any,
        df: Any,
        table: str,
        col_names: list[str],
        wait: bool = True,
    ) -> int:
        spark = ctx.spark_session
        sdf = _to_spark_df(spark, df, col_names)
        sdf.write.format("delta").mode("append").saveAsTable(table)
        row_count = sdf.count() if wait else -1
        logger.info("SparkInClusterLoader: appended %d rows into %s", row_count, table)
        return row_count


class SparkCloudStagedLoader(TopologyAwareLoader):
    """
    Writes a pandas DataFrame to cloud object storage (Parquet) and then loads
    it into a Delta table via ``spark.read.format(fmt).load(uri).write.saveAsTable()``.

    Activated when ``topology == "cloud_staged"`` — statschema writes the staging
    file to cloud storage and Spark parallelises the ingest from there.  This is
    the correct path for EMR / Dataproc cloud-staged workflows.

    Requires ``ctx.cloud_staging_uri`` (e.g. ``"s3://bucket/prefix/"``).
    """

    def can_use(self, ctx: Any, dialect: str, col_types: list[str] | None) -> bool:
        return (
            dialect in ("spark", "databricks", "lakehouse")
            and ctx.topology == "cloud_staged"
            and ctx.spark_session is not None
            and bool(ctx.cloud_staging_uri)
        )

    def bulk_load(
        self,
        ctx: Any,
        conn: Any,
        df: Any,
        table: str,
        col_names: list[str],
        wait: bool = True,
    ) -> int:
        spark = ctx.spark_session
        fmt   = getattr(ctx, "cloud_format", "parquet")
        uri   = ctx.cloud_staging_uri.rstrip("/") + f"/{table}/{uuid.uuid4().hex}"

        sdf = _to_spark_df(spark, df, col_names)
        sdf.write.format(fmt).mode("overwrite").save(uri)

        loaded = spark.read.format(fmt).load(uri)
        loaded.write.format("delta").mode("append").saveAsTable(table)

        row_count = loaded.count() if wait else -1
        logger.info(
            "SparkCloudStagedLoader: staged %d rows via %s into %s",
            row_count, uri, table,
        )
        return row_count
