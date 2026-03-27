"""MySQL 8.0+ statistics injector."""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime
from typing import Any

from ...stats_model import ColumnStats, TableStats
from .._injection_result import InjectionResult

logger = logging.getLogger(__name__)


def inject_stats_mysql(  # pragma: no cover
    conn: Any,
    table_stats: TableStats,
    schema: str | None = None,
) -> InjectionResult:
    """
    Inject TableStats into a MySQL 8.0+ database.

    Two mechanisms are used:
    1.  **Column histogram injection** via
        ``ANALYZE TABLE … UPDATE HISTOGRAM ON col USING DATA 'json'``
        This writes directly to ``information_schema.COLUMN_STATISTICS`` and
        reshapes the optimizer's selectivity estimates for filtered queries.

    2.  **InnoDB table-level row count** via
        ``UPDATE mysql.innodb_table_stats SET n_rows = … FLUSH TABLE``
        This sets the row count estimate used for full-table cost calculations.

    Parameters
    ----------
    conn        pymysql (or mysql-connector-python) connection.
    table_stats TableStats collected from any source dialect.
    schema      Target database/schema name.  Defaults to the connection's
                current database.
    """
    cur = conn.cursor()
    database = schema

    if database is None:
        cur.execute("SELECT DATABASE()")
        database = cur.fetchone()[0]

    table     = table_stats.name
    row_count = table_stats.row_count or 0
    warnings: list[str] = []

    try:
        cur.execute(
            "UPDATE mysql.innodb_table_stats "
            "SET n_rows = %s "
            "WHERE database_name = %s AND table_name = %s",
            (row_count, database, table),
        )
        if cur.rowcount == 0:
            cur.execute(f"ANALYZE TABLE `{database}`.`{table}`")
            cur.execute(
                "UPDATE mysql.innodb_table_stats "
                "SET n_rows = %s "
                "WHERE database_name = %s AND table_name = %s",
                (row_count, database, table),
            )
        cur.execute(f"FLUSH TABLE `{database}`.`{table}`")
        logger.info("MySQL InnoDB n_rows injected: %s.%s rows=%d", database, table, row_count)
    except Exception as exc:
        warnings.append(f"InnoDB table stats: {exc}")

    cols_ok = cols_skip = 0
    for col in table_stats.columns:
        try:
            _inject_mysql_column(conn, database, table, col, row_count, warnings)
            cols_ok += 1
        except Exception as exc:
            warnings.append(f"{col.name}: {exc}")
            cols_skip += 1

    return InjectionResult(
        dialect="mysql",
        table=table,
        rows_injected=row_count,
        columns_injected=cols_ok,
        columns_skipped=cols_skip,
        warnings=warnings,
    )


def _mysql_b64_str(value: str) -> str:
    return f"base64:type254:{base64.b64encode(value.encode('utf-8')).decode()}"


def _inject_mysql_column(  # pragma: no cover
    conn: Any,
    database: str,
    table: str,
    col: ColumnStats,
    row_count: int,
    warnings: list[str],
) -> None:
    cur = conn.cursor()
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")
    histogram: dict | None = None

    if col.most_common_values and len(col.most_common_values) >= 2:
        vals = [(v.value, v.frequency) for v in col.most_common_values]
        vals.sort(key=lambda x: str(x[0]))
        cum = 0.0
        buckets: list[Any] = []
        for val, freq in vals[:-1]:
            cum += freq
            buckets.append([_mysql_b64_str(str(val)), round(cum, 6)])
        buckets.append([_mysql_b64_str(str(vals[-1][0])), 1.0])
        histogram = {
            "buckets":                     buckets,
            "data-type":                   "string",
            "auto-update":                 False,
            "null-values":                 float(col.null_fraction or 0.0),
            "collation-id":                255,
            "last-updated":                now,
            "sampling-rate":               1.0,
            "histogram-type":              "singleton",
            "number-of-buckets-specified": len(buckets),
        }

    elif col.histogram_bounds and len(col.histogram_bounds) >= 3:
        bounds = col.histogram_bounds
        num_buckets = len(bounds) - 1
        if num_buckets < 1:
            return
        bucket_freq = 1.0 / num_buckets
        buckets = []
        for i in range(num_buckets):
            cum = round((i + 1) * bucket_freq, 6)
            try:
                lo = float(bounds[i])
                hi = float(bounds[i + 1])
            except (ValueError, TypeError):
                lo = str(bounds[i])
                hi = str(bounds[i + 1])
            buckets.append([lo, hi, min(cum, 1.0), 1])
        histogram = {
            "buckets":                     buckets,
            "data-type":                   "double",
            "auto-update":                 False,
            "null-values":                 float(col.null_fraction or 0.0),
            "collation-id":                8,
            "last-updated":                now,
            "sampling-rate":               1.0,
            "histogram-type":              "equi-height",
            "number-of-buckets-specified": len(buckets),
        }

    if histogram is None:
        return

    histogram_str = json.dumps(histogram)
    sql = (
        f"ANALYZE TABLE `{database}`.`{table}` "
        f"UPDATE HISTOGRAM ON `{col.name}` "
        f"USING DATA '{histogram_str}'"
    )
    cur.execute(sql)
    row = cur.fetchone()
    if row and len(row) >= 4 and "Error" in str(row[3]):
        raise RuntimeError(f"MySQL histogram injection failed: {row[3]}")
    logger.debug("MySQL column histogram injected: %s.%s.%s", database, table, col.name)
