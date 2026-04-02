"""SQL Server statistics injector (UPDATE STATISTICS WITH ROWCOUNT)."""

from __future__ import annotations

import logging
from typing import Any

from ...stats_model import TableStats
from .._injection_result import InjectionResult

logger = logging.getLogger(__name__)


def inject_stats_sqlserver(  # pragma: no cover
    conn: Any,
    table_stats: TableStats,
    schema: str = "dbo",
) -> InjectionResult:
    """
    Inject table-level row count statistics into SQL Server.

    SQL Server does not expose a public column-level statistics injection API.
    This function uses ``UPDATE STATISTICS … WITH ROWCOUNT, PAGECOUNT`` which
    updates the table-level row/page estimates used for cardinality estimates.

    Parameters
    ----------
    conn        mssql-python connection to the SQL Server target database.
    table_stats TableStats collected from any source dialect.
    schema      Target schema (default: "dbo").
    """
    cur = conn.cursor()

    table     = table_stats.name
    row_count = table_stats.row_count or 0
    avg_row   = table_stats.avg_row_bytes or 44
    pages     = max(1, int(row_count * avg_row / 8192))
    warnings: list[str] = [
        "SQL Server does not support column-level stats injection without data. "
        "Table-level ROWCOUNT/PAGECOUNT injected only. "
        "For column MCVs load a representative sample and run UPDATE STATISTICS."
    ]

    full_table = f"[{schema}].[{table}]"

    cur.execute(
        f"SELECT COUNT(*) FROM sys.stats WHERE object_id = OBJECT_ID('{full_table}')"
    )
    stat_count = cur.fetchone()[0]

    if stat_count == 0:
        try:
            cur.execute(f"SELECT TOP 1 * FROM {full_table}")
        except Exception:
            pass

    try:
        cur.execute(
            f"UPDATE STATISTICS {full_table} WITH ROWCOUNT={row_count}, PAGECOUNT={pages}"
        )
        conn.autocommit(True)
        logger.info("SQL Server table stats injected: %s rows=%d", full_table, row_count)
        cols_ok = 0
    except Exception as exc:
        warnings.append(f"UPDATE STATISTICS failed: {exc}")
        cols_ok = 0

    return InjectionResult(
        dialect="sqlserver",
        table=table,
        rows_injected=row_count,
        columns_injected=cols_ok,
        columns_skipped=len(table_stats.columns),
        warnings=warnings,
    )
