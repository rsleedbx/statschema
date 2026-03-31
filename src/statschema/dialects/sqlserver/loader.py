"""SQL Server bulk loaders (mssql-python only).

Three load paths, all using the mssql-python driver:

  SQLServerBCPLoader          (loader_name="bcp")
      cursor.bulkcopy() — native BCP, no staging file.

  SQLServerBulkInsertLoader   (loader_name="bulk_insert")
      T-SQL BULK INSERT from a staging CSV on a shared filesystem.
      Requires STATSCHEMA_CLIENT_STAGING_DIR (or server_staging_dir).

  SQLServerMultiRowLoader     (loader_name="multi_row")
      Parameterised multi-row INSERTs via DBAPI-2 executemany.
      Always available; useful for small tables or environments
      where BCP is not permitted.

Select explicitly via env var:
    STATSCHEMA_SQLSERVER_LOADER=bcp          # default when no staging dir
    STATSCHEMA_SQLSERVER_LOADER=bulk_insert  # requires staging dir
    STATSCHEMA_SQLSERVER_LOADER=multi_row    # always works

When the env var is not set, the registry uses priority order:
BCP → BULK INSERT (if staging dir) → MULTI ROW.
Setting the env var disables fallback — if prerequisites for the
chosen loader are not met the call raises RuntimeError immediately.

pyodbc / pymssql are not supported.
"""

from __future__ import annotations

import csv
import logging
import os
import tempfile
from typing import Any

from .._loader_shared import (
    _iter_rows,
    _detect_paramstyle,
    _effective_batch_size,
    _insert_multi_row,
    _DIALECT_BATCH_DEFAULTS,
    BatchConfig,
)
from ..base import TopologyAwareLoader

logger = logging.getLogger(__name__)

_MSSQL_MODULE_PREFIX = "mssql_python"


def _require_mssql_python(conn: Any) -> None:
    """Raise if the connection was not created by the mssql-python driver."""
    if not type(conn).__module__.startswith(_MSSQL_MODULE_PREFIX):
        raise RuntimeError(
            f"SQL Server loaders require the mssql-python driver; "
            f"got {type(conn).__module__!r}.  "
            "Install mssql-python and use SQLServerDialect.connect()."
        )


def _require_statschema_db(conn: Any) -> str:
    """Return conn._statschema_db or raise RuntimeError if unset."""
    database = getattr(conn, "_statschema_db", None)
    if database is None:
        raise RuntimeError(
            "SQL Server loaders require conn._statschema_db to be set. "
            "Create the connection via SQLServerDialect.connect() so the "
            "current database is recorded at connect time."
        )
    return database


# ---------------------------------------------------------------------------
# Path 1: cursor.bulkcopy() — no staging file
# ---------------------------------------------------------------------------

def bulk_load_sqlserver_bcp(  # pragma: no cover
    conn: Any,
    df: Any,
    table: str,
    col_names: list[str],
    *,
    database: str,
) -> int:
    """Load using mssql-python cursor.bulkcopy() — no staging file required.

    cursor.bulkcopy() does not inherit the connection's current database
    context, so a fully-qualified [database].[dbo].[table] reference is
    built from the explicit ``database`` parameter.
    """
    _require_mssql_python(conn)
    rows  = list(_iter_rows(df))
    count = len(rows)
    full_table = f"[{database}].[dbo].[{table}]"
    cur    = conn.cursor()
    result = cur.bulkcopy(full_table, rows, column_mappings=col_names)
    conn.commit()
    rows_copied = (result.get("rows_copied") or count) if isinstance(result, dict) else count
    logger.info("bulk_load_sqlserver_bcp: loaded %d rows into %s", rows_copied, full_table)
    return rows_copied


# ---------------------------------------------------------------------------
# Path 2: T-SQL BULK INSERT from a staging CSV
# ---------------------------------------------------------------------------

def bulk_load_sqlserver_bulk_insert(  # pragma: no cover
    conn: Any,
    df: Any,
    table: str,
    col_names: list[str],
    *,
    staging_dir: str,
    database: str,
) -> int:
    """Load using T-SQL BULK INSERT from a server-visible staging CSV.

    The staging CSV is written to ``staging_dir``, which must be a path that
    the SQL Server process can read (shared filesystem or same host).
    Set via STATSCHEMA_CLIENT_STAGING_DIR.
    """
    _require_mssql_python(conn)
    rows  = list(_iter_rows(df))
    count = len(rows)
    full  = f"[{database}].[dbo].[{table}]"
    cur   = conn.cursor()

    os.makedirs(staging_dir, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix=f"ss_{table}_", suffix=".csv", dir=staging_dir)
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for row in rows:
            writer.writerow(["" if v is None else v for v in row])
    try:
        cur.execute(
            f"BULK INSERT {full} FROM N'{path}' WITH ("
            f"FIELDTERMINATOR=',', ROWTERMINATOR='\\n', FIRSTROW=1, TABLOCK)"
        )
        conn.commit()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    logger.info("bulk_load_sqlserver_bulk_insert: loaded %d rows into %s", count, full)
    return count


# ---------------------------------------------------------------------------
# Path 3: parameterised multi-row INSERTs
# ---------------------------------------------------------------------------

def bulk_load_sqlserver_multi_row(  # pragma: no cover
    conn: Any,
    df: Any,
    table: str,
    col_names: list[str],
) -> int:
    """Load using parameterised batch INSERTs (executemany).

    Always available; no driver-specific bulk API required.
    """
    _require_mssql_python(conn)
    paramstyle = _detect_paramstyle(conn)
    cfg        = _DIALECT_BATCH_DEFAULTS.get("sqlserver", BatchConfig())
    batch_size = _effective_batch_size(cfg, len(col_names))
    cur        = conn.cursor()
    rows       = list(_iter_rows(df))
    inserted   = _insert_multi_row(
        cur, table, col_names, iter(rows), "sqlserver", paramstyle, batch_size
    )
    conn.commit()
    logger.info("bulk_load_sqlserver_multi_row: loaded %d rows into %s", inserted, table)
    return inserted


# ---------------------------------------------------------------------------
# Topology-aware loader classes
# ---------------------------------------------------------------------------

def _staging_from_ctx(ctx: Any) -> str | None:
    """Extract the write-side staging path from a DeploymentContext."""
    if hasattr(ctx, "staging_write_dir"):
        return ctx.staging_write_dir()
    return getattr(ctx, "server_staging_dir", None)


class SQLServerBCPLoader(TopologyAwareLoader):
    """cursor.bulkcopy() — fastest, no staging file needed.

    Select with STATSCHEMA_SQLSERVER_LOADER=bcp.
    """

    loader_name = "bcp"

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
        _require_mssql_python(conn)
        database = _require_statschema_db(conn)
        return bulk_load_sqlserver_bcp(conn, df, table, col_names, database=database)


class SQLServerBulkInsertLoader(TopologyAwareLoader):
    """T-SQL BULK INSERT from a shared-filesystem staging CSV.

    Select with STATSCHEMA_SQLSERVER_LOADER=bulk_insert.
    Requires a staging directory (STATSCHEMA_CLIENT_STAGING_DIR).
    """

    loader_name = "bulk_insert"

    def can_use(self, ctx: Any, dialect: str, col_types: list[str] | None) -> bool:
        if dialect != "sqlserver":
            return False
        staging = _staging_from_ctx(ctx)
        if not staging:
            logger.info(
                "SQLServerBulkInsertLoader: skipped — no staging directory configured. "
                "Set STATSCHEMA_CLIENT_STAGING_DIR to enable BULK INSERT."
            )
            return False
        return True

    def bulk_load(
        self,
        ctx: Any,
        conn: Any,
        df: Any,
        table: str,
        col_names: list[str],
        wait: bool = True,
    ) -> int:
        _require_mssql_python(conn)
        database = _require_statschema_db(conn)
        staging  = _staging_from_ctx(ctx)
        if not staging:
            raise RuntimeError(
                "SQLServerBulkInsertLoader: no staging directory available. "
                "Set STATSCHEMA_CLIENT_STAGING_DIR."
            )
        return bulk_load_sqlserver_bulk_insert(
            conn, df, table, col_names, staging_dir=staging, database=database,
        )


class SQLServerMultiRowLoader(TopologyAwareLoader):
    """Parameterised multi-row INSERTs — always available.

    Select with STATSCHEMA_SQLSERVER_LOADER=multi_row.
    """

    loader_name = "multi_row"

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
        _require_mssql_python(conn)
        return bulk_load_sqlserver_multi_row(conn, df, table, col_names)
