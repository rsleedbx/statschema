"""SQL Server bulk-copy loader (mssql-python BCP / BULK INSERT fallback)."""

from __future__ import annotations

import csv
import logging
import os
import tempfile
from typing import Any, Optional

from .._loader_shared import _iter_rows

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

    1. mssql-python (v1.4.0+) — uses conn.bulk_copy() via native BCP API.
    2. pyodbc / pymssql fallback — BULK INSERT from a staging CSV.
    """
    rows  = list(_iter_rows(df))
    count = len(rows)

    if type(conn).__module__.startswith("mssql_python"):
        conn.bulk_copy(f"{schema}.{table}", rows)
        conn.commit()
        logger.info(
            "bulk_load_sqlserver[mssql-python BCP]: loaded %d rows into %s.%s",
            count, schema, table,
        )
        return count

    cur  = conn.cursor()
    full = f"[{schema}].[{table}]"
    path = os.path.join(
        staging_dir or tempfile.gettempdir(), f"statschema_{table}.csv"
    )
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for row in rows:
            writer.writerow(["" if v is None else v for v in row])
    cur.execute(
        f"BULK INSERT {full} FROM '{path}' WITH ("
        f"FIELDTERMINATOR=',', ROWTERMINATOR='\\n', FIRSTROW=1, TABLOCK)"
    )
    conn.commit()
    os.unlink(path)
    logger.info("bulk_load_sqlserver[BULK INSERT]: loaded %d rows into %s", count, table)
    return count
