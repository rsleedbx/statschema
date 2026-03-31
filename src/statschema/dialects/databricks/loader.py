"""Databricks-specific loaders: DatabricksSparkLoader and DatabricksCopyIntoLoader.

Both are registered under the ``"databricks"`` and ``"lakehouse"`` dialects.
They require ``ctx.is_databricks=True`` and a live ``ctx.spark_session``.
``conn`` is unused — all I/O goes through the SparkSession.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from ..spark.loader import SparkInClusterLoader, SparkCloudStagedLoader, _to_spark_df

logger = logging.getLogger(__name__)

_DATABRICKS_TOPOLOGIES = frozenset({"spark_embedded", "databricks_connect"})


class DatabricksSparkLoader(SparkInClusterLoader):
    """
    SparkInClusterLoader gated to Databricks topologies.

    Explicit Delta format + Unity Catalog three-part name (catalog.schema.table)
    are supported out-of-the-box.  On Databricks the SparkSession writes to the
    default catalog unless the table name overrides it.
    """


    def can_use(self, ctx: Any, dialect: str, col_types: list[str] | None) -> bool:
        return (
            dialect in ("databricks", "lakehouse")
            and ctx.topology in _DATABRICKS_TOPOLOGIES
            and ctx.spark_session is not None
            and ctx.is_databricks
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
        logger.info(
            "DatabricksSparkLoader: appended %d rows into %s", row_count, table
        )
        return row_count


class DatabricksCopyIntoLoader(SparkCloudStagedLoader):
    """
    Writes a pandas DataFrame to cloud staging (Parquet) and loads via COPY INTO.

    ``COPY INTO`` is Databricks' idempotent ingest command: transactional, schema-
    evolution-aware, and faster than INSERT/SELECT for large batches because the
    Databricks runtime can parallelise the file reads across the cluster.

    Requires ``ctx.cloud_staging_uri`` and ``ctx.is_databricks``.
    """


    def can_use(self, ctx: Any, dialect: str, col_types: list[str] | None) -> bool:
        cloud_topologies = _DATABRICKS_TOPOLOGIES | {"cloud_staged"}
        return (
            dialect in ("databricks", "lakehouse")
            and ctx.topology in cloud_topologies
            and ctx.spark_session is not None
            and ctx.is_databricks
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

        spark.sql(
            f"COPY INTO {table} "
            f"FROM '{uri}' "
            f"FILEFORMAT = {fmt.upper()} "
            f"FORMAT_OPTIONS ('inferSchema' = 'true') "
            f"COPY_OPTIONS ('mergeSchema' = 'true')"
        )
        row_count = spark.table(table).count() if wait else -1
        logger.info(
            "DatabricksCopyIntoLoader: COPY INTO %s from %s (%d rows)",
            table, uri, row_count,
        )
        return row_count
