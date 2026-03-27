"""Databricks / Delta Lake statistics injector (synthetic sample + ANALYZE TABLE)."""

from __future__ import annotations

import logging
from typing import Any

from ...stats_model import TableStats
from .._injection_result import InjectionResult

logger = logging.getLogger(__name__)


def inject_stats_databricks(  # pragma: no cover
    spark: Any,
    table_stats: TableStats,
    canonical_schema: Any,
    table_name: str | None = None,
    database: str = "default",
    sample_rows: int = 10_000,
    analyze_columns: list[str] | None = None,
    overwrite: bool = True,
    table_format: str = "delta",
) -> InjectionResult:
    """
    Bootstrap Databricks / Delta Lake optimizer statistics by writing a
    statistically-representative synthetic sample and running ANALYZE TABLE.

    Parameters
    ----------
    spark            Active SparkSession.
    table_stats      TableStats collected from any source dialect.
    canonical_schema CanonicalTableSchema for the target table.
    table_name       Override target table name; defaults to ``table_stats.name``.
    database         Target database / schema (default: "default").
    sample_rows      Number of synthetic rows to generate (default: 10 000).
    analyze_columns  Subset of column names to analyze; defaults to all columns.
    overwrite        If True, the sample replaces any existing table data.
    table_format     Underlying table format (default: "delta").
    """
    from ...dbldatagen_builder import build_dataframe_from_canonical

    tgt_table = table_name or table_stats.name
    full_ref   = f"{database}.{tgt_table}"

    df = build_dataframe_from_canonical(
        spark,
        canonical_schema,
        rows=sample_rows,
        stats=table_stats,
    )

    write_mode = "overwrite" if overwrite else "append"
    if table_format.lower() == "delta":
        import tempfile, os as _os
        delta_dir = _os.path.join(tempfile.gettempdir(), "statschema_delta", tgt_table)
        (
            df.write
            .format("delta")
            .mode(write_mode)
            .save(delta_dir)
        )
        logger.info("Wrote %d synthetic rows to Delta path %s", sample_rows, delta_dir)
        spark.sql(f"DROP TABLE IF EXISTS {full_ref}")
        spark.sql(f"CREATE TABLE {full_ref} USING DELTA LOCATION '{delta_dir}'")
        logger.info("Registered Delta table %s → %s", full_ref, delta_dir)
    else:
        spark.sql(f"DROP TABLE IF EXISTS {full_ref}")
        (
            df.write
            .format(table_format)
            .mode(write_mode)
            .saveAsTable(full_ref)
        )
        logger.info("Wrote %d synthetic rows to %s table %s", sample_rows, table_format, full_ref)

    col_names = analyze_columns or [c.name for c in (table_stats.columns or [])]
    if col_names:
        quoted_cols = ", ".join(f"`{c}`" for c in col_names)
        analyze_sql = f"ANALYZE TABLE {full_ref} COMPUTE STATISTICS FOR COLUMNS {quoted_cols}"
    else:
        analyze_sql = f"ANALYZE TABLE {full_ref} COMPUTE STATISTICS"
    spark.sql(analyze_sql)
    logger.info("ANALYZE TABLE completed for %s", full_ref)

    delta_stats_sql = f"ANALYZE TABLE {full_ref} COMPUTE DELTA STATISTICS"
    delta_stats_ok = False
    try:
        spark.sql(delta_stats_sql)
        logger.info("COMPUTE DELTA STATISTICS completed for %s", full_ref)
        delta_stats_ok = True
    except Exception as exc:
        logger.debug("COMPUTE DELTA STATISTICS not available (requires DBR 14.3+): %s", exc)

    warnings: list[str] = [
        f"Row count seen by optimizer = {sample_rows} (synthetic sample), not production row count. "
        f"Scale sample_rows up or re-run ANALYZE after loading production data.",
    ]
    if not delta_stats_ok:
        warnings.append(
            "ANALYZE TABLE … COMPUTE DELTA STATISTICS is not available on this runtime "
            "(requires Databricks Runtime 14.3+).  Delta data-skipping statistics were not set."
        )

    return InjectionResult(
        dialect="databricks",
        table=tgt_table,
        rows_injected=sample_rows,
        columns_injected=len(col_names),
        columns_skipped=0,
        warnings=warnings,
    )
