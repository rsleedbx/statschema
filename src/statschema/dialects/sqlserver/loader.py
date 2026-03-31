"""SQL Server bulk loaders (mssql-python only).

Three load paths exposed as DataLoader slots:

  native_bulk   — cursor.bulkcopy() — fastest, no staging file.
  server_file   — T-SQL BULK INSERT from a server-visible staging CSV.
                  Requires ctx.client_staging_dir.
  batch_insert  — Parameterised multi-row INSERTs via DBAPI-2 executemany.
                  Always available; useful for small tables.

Select via statschema.yaml profile (or STATSCHEMA__LOADER env var):
    loader: native_bulk     # default
    loader: server_file     # requires client_staging_dir
    loader: batch_insert    # always works

No fallback — if prerequisites are missing the call raises RuntimeError.
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
from ..base import DataLoader

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
    ctx: Any | None = None,
) -> int:
    """Load using T-SQL BULK INSERT from a server-visible staging CSV.

    The staging CSV is written to ``staging_dir`` (the client-side mount point,
    i.e. ``ctx.client_staging_dir``).  The path passed to T-SQL is the
    *server-side* equivalent, obtained via ``ctx.to_server_path()``.  When both
    sides share the same mount point (Lima / local disk) the translation is a
    no-op.  Pass ``ctx`` to handle NFS/CIFS mounts where the two paths differ.
    """
    _require_mssql_python(conn)
    rows  = list(_iter_rows(df))
    count = len(rows)
    full  = f"[{database}].[dbo].[{table}]"
    cur   = conn.cursor()

    os.makedirs(staging_dir, exist_ok=True)
    fd, client_path = tempfile.mkstemp(prefix=f"ss_{table}_", suffix=".csv", dir=staging_dir)
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for row in rows:
            writer.writerow(["" if v is None else v for v in row])

    # Translate to the path SQL Server sees (different when client and server
    # mount the shared filesystem at different paths, e.g. NFS / CIFS).
    if ctx is not None:
        server_path = ctx.to_server_path(client_path)
    else:
        server_path = client_path
    if server_path != client_path:
        logger.debug(
            "bulk_load_sqlserver_bulk_insert: path translation client=%s server=%s",
            client_path, server_path,
        )

    try:
        cur.execute(
            f"BULK INSERT {full} FROM N'{server_path}' WITH ("
            f"FIELDTERMINATOR=',', ROWTERMINATOR='\\n', FIRSTROW=1, TABLOCK)"
        )
        conn.commit()
    finally:
        try:
            os.unlink(client_path)
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
# Single DataLoader subclass — dispatches via ctx.loader slot name
# ---------------------------------------------------------------------------

class SQLServerLoader(DataLoader):
    """
    SQL Server DataLoader — all three load paths in one class.

    ctx.loader selects the path (set in statschema.yaml or STATSCHEMA__LOADER):
      native_bulk  — cursor.bulkcopy(), fastest, no staging file
      server_file  — T-SQL BULK INSERT from shared-FS CSV (requires ctx.client_staging_dir)
      batch_insert — parameterised executemany, always available
    """

    _supported_methods = ("native_bulk", "server_file", "batch_insert")
    _method_prerequisites: dict[str, list[str]] = {
        "server_file": ["client_staging_dir"],
    }
    _method_min_server_version: dict[str, str] = {}

    def can_use(self, ctx: Any, dialect: str, col_types: list[str] | None) -> bool:
        return dialect == "sqlserver"

    def native_bulk(
        self, ctx: Any, conn: Any, df: Any, table: str, col_names: list[str]
    ) -> int:
        _require_mssql_python(conn)
        database = _require_statschema_db(conn)
        return bulk_load_sqlserver_bcp(conn, df, table, col_names, database=database)

    def server_file(
        self, ctx: Any, conn: Any, df: Any, table: str, col_names: list[str]
    ) -> int:
        # ctx.client_staging_dir validated by DataLoader.load() before this is called
        _require_mssql_python(conn)
        database = _require_statschema_db(conn)
        return bulk_load_sqlserver_bulk_insert(
            conn, df, table, col_names,
            staging_dir=ctx.client_staging_dir,
            database=database,
            ctx=ctx,
        )

    def batch_insert(
        self, ctx: Any, conn: Any, df: Any, table: str, col_names: list[str]
    ) -> int:
        _require_mssql_python(conn)
        return bulk_load_sqlserver_multi_row(conn, df, table, col_names)
