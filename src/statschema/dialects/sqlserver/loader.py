"""SQL Server bulk-copy loader (mssql-python BCP / BULK INSERT fallback)."""

from __future__ import annotations

import csv
import logging
import os
import tempfile
from typing import Any, Optional

from .._loader_shared import _iter_rows
from ..base import TopologyAwareLoader

logger = logging.getLogger(__name__)


def bulk_load_sqlserver(  # pragma: no cover
    conn: Any,
    df: Any,
    table: str,
    col_names: list[str],
    schema: str = "dbo",
    staging_dir: Optional[str] = None,
) -> int:
    """
    Load df into a SQL Server table using the fastest available path.

    1. mssql-python (v1.4.0+) — uses cursor.bulkcopy() via native BCP API.
    2. pyodbc / pymssql fallback — BULK INSERT from a staging CSV.
    """
    rows  = list(_iter_rows(df))
    count = len(rows)

    if type(conn).__module__.startswith("mssql_python"):
        # mssql-python exposes bulkcopy on the cursor (not the connection).
        # bulkcopy() does not inherit the current USE [db] context — it needs
        # the fully-qualified 3-part name [database].[dbo].[table].
        cur = conn.cursor()
        cur.execute("SELECT DB_NAME()")
        current_db = cur.fetchone()[0]
        full_table = f"[{current_db}].[dbo].[{table}]"
        result = cur.bulkcopy(full_table, rows, column_mappings=col_names)
        conn.commit()
        rows_copied = result.get("rows_copied", count) if isinstance(result, dict) else count
        logger.info(
            "bulk_load_sqlserver[mssql-python BCP]: loaded %d rows into %s",
            rows_copied, full_table,
        )
        return rows_copied

    cur  = conn.cursor()
    full = f"[{schema}].[{table}]"

    # staging_dir comes from ctx.staging_write_dir() (STATSCHEMA_CLIENT_STAGING_DIR).
    # If unset, falls back to the system temp directory; BULK INSERT will then
    # fail unless SQL Server can reach that path (e.g. UNC share).  For Lima
    # setups set STATSCHEMA_CLIENT_STAGING_DIR=/tmp/lima/statschema so the
    # Lima VM sees the file at the same path.
    if staging_dir:
        os.makedirs(staging_dir, exist_ok=True)
        stage_root = staging_dir
    else:
        stage_root = tempfile.gettempdir()

    fd, path = tempfile.mkstemp(
        prefix=f"ss_{schema}_{table}_", suffix=".csv",
        dir=stage_root,
    )
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for row in rows:
            writer.writerow(["" if v is None else v for v in row])
    try:
        cur.execute(
            f"BULK INSERT {full} FROM '{path}' WITH ("
            f"FIELDTERMINATOR=',', ROWTERMINATOR='\\n', FIRSTROW=1, TABLOCK)"
        )
        conn.commit()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    logger.info("bulk_load_sqlserver[BULK INSERT]: loaded %d rows into %s", count, table)
    return count


class SQLServerBCPLoader(TopologyAwareLoader):
    """Topology-aware wrapper around ``bulk_load_sqlserver``."""

    def can_use(self, ctx: Any, dialect: str, col_types: list[str] | None) -> bool:
        return dialect == "sqlserver"

    def bulk_load(
        self,
        ctx: Any,
        conn: Any,
        df: Any,
        table: str,
        col_names: list[str],
        wait: bool = True,
    ) -> int:
        staging = (
            ctx.staging_write_dir()
            if hasattr(ctx, "staging_write_dir")
            else getattr(ctx, "server_staging_dir", None)
        )
        return bulk_load_sqlserver(conn, df, table, col_names, staging_dir=staging)
