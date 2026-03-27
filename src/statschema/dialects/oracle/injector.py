"""Oracle 19c+ statistics injector (DBMS_STATS)."""

from __future__ import annotations

import logging
from typing import Any

from ...stats_model import ColumnStats, TableStats
from .._injection_result import InjectionResult

logger = logging.getLogger(__name__)


def inject_stats_oracle(  # pragma: no cover
    conn: Any,
    table_stats: TableStats,
    schema: str | None = None,
) -> InjectionResult:
    """
    Inject TableStats into an Oracle database using DBMS_STATS procedures.

    Parameters
    ----------
    conn        oracledb connection to the Oracle target database.
    table_stats TableStats collected from any source dialect.
    schema      Oracle schema/owner (uppercase). Defaults to the connected user.
    """
    cur = conn.cursor()

    if schema is None:
        cur.execute("SELECT USER FROM DUAL")
        schema = cur.fetchone()[0]

    table     = table_stats.name.upper()
    row_count = table_stats.row_count or 0
    avg_row   = table_stats.avg_row_bytes or 44
    blocks    = max(1, int(row_count * avg_row / 8192))
    warnings: list[str] = []

    cur.execute(
        """
        BEGIN
            DBMS_STATS.SET_TABLE_STATS(
                ownname  => :owner,
                tabname  => :tab,
                numrows  => :rows,
                numblks  => :blks,
                avgrlen  => :rlen,
                no_invalidate => FALSE
            );
        END;
        """,
        {"owner": schema, "tab": table, "rows": row_count, "blks": blocks, "rlen": avg_row},
    )
    conn.commit()
    logger.info("Oracle table stats injected: %s.%s rows=%d", schema, table, row_count)

    cols_ok = cols_skip = 0
    for col in table_stats.columns:
        try:
            _inject_oracle_column(conn, schema, table, col, row_count, warnings)
            cols_ok += 1
        except Exception as exc:
            warnings.append(f"{col.name}: {exc}")
            cols_skip += 1

    conn.commit()
    return InjectionResult(
        dialect="oracle",
        table=table,
        rows_injected=row_count,
        columns_injected=cols_ok,
        columns_skipped=cols_skip,
        warnings=warnings,
    )


def _inject_oracle_column(  # pragma: no cover
    conn: Any,
    schema: str,
    table: str,
    col: ColumnStats,
    row_count: int,
    warnings: list[str],
) -> None:
    cur = conn.cursor()

    n_distinct = int(col.n_distinct or 1)
    null_count = int((col.null_fraction or 0.0) * row_count)
    avg_width   = int(col.avg_width_bytes or 8)

    raw = col.name
    if raw == raw.upper():
        col_name = raw
    else:
        col_name = f'"{raw}"'

    cur.execute(
        """
        BEGIN
            DBMS_STATS.SET_COLUMN_STATS(
                ownname  => :owner,
                tabname  => :tab,
                colname  => :col,
                distcnt  => :ndist,
                nullcnt  => :nulls,
                avgclen  => :avglen,
                no_invalidate => FALSE
            );
        END;
        """,
        {
            "owner":  schema,
            "tab":    table,
            "col":    col_name,
            "ndist":  n_distinct,
            "nulls":  null_count,
            "avglen": avg_width,
        },
    )
    logger.debug("Oracle column stats injected: %s.%s.%s dist=%d", schema, table, col.name, n_distinct)
