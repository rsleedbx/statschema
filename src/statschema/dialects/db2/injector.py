"""IBM Db2 LUW statistics injector (SYSSTAT updateable catalog views)."""

from __future__ import annotations

import logging
from typing import Any

from ...stats_model import ColumnStats, TableStats
from .._injection_result import InjectionResult

logger = logging.getLogger(__name__)


def inject_stats_db2(  # pragma: no cover
    conn: Any,
    table_stats: TableStats,
    schema: str | None = None,
) -> InjectionResult:
    """
    Inject TableStats into IBM Db2 LUW via SYSSTAT updateable catalog views.

    Parameters
    ----------
    conn        ibm_db_dbi connection to the Db2 LUW target database.
    table_stats TableStats collected from any source dialect.
    schema      Db2 schema name (uppercase). Defaults to ``CURRENT SCHEMA``.
    """
    cur = conn.cursor()

    if schema is None:
        cur.execute("VALUES CURRENT SCHEMA")
        row = cur.fetchone()
        schema = (row[0] if row else "").strip().upper()

    table     = table_stats.name.upper()
    row_count = table_stats.row_count or 0
    avg_row   = table_stats.avg_row_bytes or 44
    npages    = max(1, int(row_count * avg_row / 4096))
    warnings: list[str] = []

    cur.execute(
        "UPDATE SYSSTAT.TABLES SET CARD = ?, NPAGES = ?, FPAGES = ? "
        "WHERE TABSCHEMA = ? AND TABNAME = ?",
        (row_count, npages, npages, schema, table),
    )
    if cur.rowcount == 0:
        warnings.append(
            f"SYSSTAT.TABLES row not found for {schema}.{table}. "
            "Table may not exist or was never RUNSTATS'd."
        )
    conn.commit()
    logger.info("Db2 table stats injected: %s.%s rows=%d", schema, table, row_count)

    cols_ok = cols_skip = 0
    for col in table_stats.columns:
        try:
            _inject_db2_column(conn, schema, table, col, row_count, warnings)
            cols_ok += 1
        except Exception as exc:
            warnings.append(f"{col.name}: {exc}")
            cols_skip += 1

    conn.commit()
    return InjectionResult(
        dialect="db2",
        table=table,
        rows_injected=row_count,
        columns_injected=cols_ok,
        columns_skipped=cols_skip,
        warnings=warnings,
    )


def _inject_db2_column(  # pragma: no cover
    conn: Any,
    schema: str,
    table: str,
    col: ColumnStats,
    row_count: int,
    warnings: list[str],
) -> None:
    cur = conn.cursor()

    colcard   = int(col.n_distinct or 1)
    numnulls  = int((col.null_fraction or 0.0) * row_count)
    avgcollen = int(col.avg_width_bytes or 8)
    col_name  = col.name.upper()

    cur.execute(
        "UPDATE SYSSTAT.COLUMNS SET COLCARD = ?, NUMNULLS = ?, AVGCOLLEN = ? "
        "WHERE TABSCHEMA = ? AND TABNAME = ? AND COLNAME = ?",
        (colcard, numnulls, avgcollen, schema, table, col_name),
    )
    if cur.rowcount == 0:
        warnings.append(
            f"SYSSTAT.COLUMNS row not found for {schema}.{table}.{col_name}"
        )
    logger.debug(
        "Db2 column stats injected: %s.%s.%s colcard=%d numnulls=%d",
        schema, table, col_name, colcard, numnulls,
    )

    _inject_db2_coldist(conn, schema, table, col, row_count, warnings)


def _inject_db2_coldist(  # pragma: no cover
    conn: Any,
    schema: str,
    table: str,
    col: ColumnStats,
    row_count: int,
    warnings: list[str],
) -> None:
    cur = conn.cursor()

    cur.execute(
        "DELETE FROM SYSSTAT.COLDIST "
        "WHERE TABSCHEMA = ? AND TABNAME = ? AND COLNAME = ?",
        (schema, table, col.name.upper()),
    )

    mcv_count = 0
    if col.most_common_values:
        mcv_sorted = sorted(col.most_common_values, key=lambda m: m.frequency, reverse=True)
        for seqno, mcv in enumerate(mcv_sorted, start=1):
            valcount = max(1, int(mcv.frequency * row_count))
            colvalue = str(mcv.value)[:255]
            try:
                cur.execute(
                    "INSERT INTO SYSSTAT.COLDIST "
                    "(TABSCHEMA, TABNAME, COLNAME, TYPE, SEQNO, COLVALUE, VALCOUNT, DISTCOUNT) "
                    "VALUES (?, ?, ?, 'F', ?, ?, ?, NULL)",
                    (schema, table, col.name.upper(), seqno, colvalue, valcount),
                )
                mcv_count += 1
            except Exception as exc:
                warnings.append(f"{col.name} MCV seqno={seqno}: {exc}")

    hist_count = 0
    if col.histogram_bounds:
        n_bounds  = len(col.histogram_bounds)
        n_dist    = max(1, int(abs(col.n_distinct) or 1))
        for seqno, bound in enumerate(col.histogram_bounds, start=1):
            frac      = seqno / n_bounds
            valcount  = max(1, int(frac * row_count))
            distcount = max(1, int(frac * n_dist))
            colvalue  = str(bound)[:255]
            try:
                cur.execute(
                    "INSERT INTO SYSSTAT.COLDIST "
                    "(TABSCHEMA, TABNAME, COLNAME, TYPE, SEQNO, COLVALUE, VALCOUNT, DISTCOUNT) "
                    "VALUES (?, ?, ?, 'Q', ?, ?, ?, ?)",
                    (schema, table, col.name.upper(), seqno, colvalue, valcount, distcount),
                )
                hist_count += 1
            except Exception as exc:
                warnings.append(f"{col.name} histogram seqno={seqno}: {exc}")

    logger.debug(
        "Db2 COLDIST injected: %s.%s.%s mcv=%d histogram=%d",
        schema, table, col.name, mcv_count, hist_count,
    )
