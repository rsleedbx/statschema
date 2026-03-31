"""IBM Db2 LUW bulk-copy loader (ADMIN_CMD LOAD / MULTI_ROW fallback)."""

from __future__ import annotations

import csv
import logging
import os
import tempfile
from typing import Any, Optional

from .._loader_shared import (
    _iter_rows,
    _coerce_db2_val,
    _coerce_db2_row,
    _detect_paramstyle,
    _effective_batch_size,
    _insert_multi_row,
    _DIALECT_BATCH_DEFAULTS,
    BatchConfig,
)

logger = logging.getLogger(__name__)


def _staging_root(staging_dir: Optional[str] = None) -> str:
    """Return the directory to use for staging DEL files.

    ``staging_dir`` must come from ``ctx.staging_write_dir()``
    (i.e. ``STATSCHEMA_CLIENT_STAGING_DIR``).  If unset, falls back to the
    system temp directory — note that ADMIN_CMD will only succeed if the DB2
    server process can also read that path (shared filesystem required).
    """
    if staging_dir:
        os.makedirs(staging_dir, exist_ok=True)
        return staging_dir
    return tempfile.gettempdir()


def bulk_load_db2(  # pragma: no cover
    conn: Any,
    df: Any,
    table: str,
    col_names: list[str],
    schema: Optional[str] = None,
    staging_dir: Optional[str] = None,
    col_types: Optional[list[str]] = None,
    _force_multi_row: bool = False,
    _ctx: Optional[Any] = None,
) -> int:
    """
    Load df into IBM Db2 LUW.

    Strategy (tried in order):
    1. Shared filesystem ADMIN_CMD: if ``ctx.server_staging_dir`` is set, the
       DB server can read the staged file directly — uses ``ctx.to_server_path()``
       to translate the write path to the server-side path (handles NFS mounts
       where the two sides differ).
    2. MULTI_ROW fallback: parameterised batch INSERTs.

    CLOB columns are skipped in path 1 (DB2 DEL-format LOAD does not support
    inline CLOB data); those tables go straight to MULTI_ROW.
    """
    # CLOB columns cannot be loaded inline via ADMIN_CMD DEL format.
    _has_clob = col_types and any(
        t.lower() in ("string", "clob", "nclob") for t in col_types
    )
    if _force_multi_row:
        _has_clob = True  # treat as CLOB to force the multi-row fallback path

    cur = conn.cursor()
    if schema is None:
        cur.execute("VALUES CURRENT SCHEMA")
        row = cur.fetchone()
        schema = (row[0] if row else "").strip().upper()

    full  = f'"{schema}"."{table.upper()}"'
    rows  = list(_iter_rows(df))
    count = len(rows)
    base  = os.path.basename(table)

    fd, host_path = tempfile.mkstemp(
        prefix=f"ss_{schema}_{base}_", suffix=".del",
        dir=_staging_root(staging_dir),
    )
    # mkstemp creates files 0o600 (owner-only).  The DB2 server process runs as
    # db2inst1 — make the file world-readable so ADMIN_CMD LOAD can open it.
    os.chmod(host_path, 0o644)

    def _db2_csv_val(v: Any) -> Any:
        if v is None:
            return ""
        if isinstance(v, bool):
            return 1 if v else 0
        return v

    with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for row in rows:
            writer.writerow([_db2_csv_val(v) for v in row])

    # Translate client-side write path to server-side read path.
    # When both dirs are the same (Lima / local disk) to_server_path is a no-op.
    if _ctx is not None and _ctx.server_staging_dir:
        admin_cmd_path = _ctx.to_server_path(host_path)
        logger.debug(
            "bulk_load_db2: shared filesystem — client=%s server=%s",
            host_path, admin_cmd_path,
        )
    else:
        admin_cmd_path = host_path

    actual = 0
    if _has_clob:
        logger.debug("bulk_load_db2: skipping ADMIN_CMD for %s (CLOB columns present)", table)
        try:
            os.unlink(host_path)
        except OSError:
            pass
    else:
        try:
            cur.execute(
                f"CALL SYSPROC.ADMIN_CMD('LOAD FROM {admin_cmd_path} OF DEL INSERT INTO {full} NONRECOVERABLE')"
            )
            conn.commit()
            cur.execute(f"SELECT COUNT(*) FROM {full}")
            actual = cur.fetchone()[0]
        except Exception as e:
            logger.error("bulk_load_db2 ADMIN_CMD failed (%s); falling back to MULTI_ROW inserts", e)
            actual = 0
        finally:
            try:
                os.unlink(host_path)
            except OSError:
                pass

    if actual >= count:
        logger.info("bulk_load_db2[ADMIN_CMD]: loaded %d rows into %s", count, full)
        return count

    if actual > 0:
        cur.execute(f"DELETE FROM {full}")
        conn.commit()

    logger.warning(
        "bulk_load_db2: ADMIN_CMD loaded %d/%d rows; falling back to MULTI_ROW inserts",
        actual, count,
    )
    paramstyle = _detect_paramstyle(conn)
    cfg        = _DIALECT_BATCH_DEFAULTS.get("db2", BatchConfig())
    batch_size = _effective_batch_size(cfg, len(col_names))
    if col_types:
        coerced = (_coerce_db2_row(row, col_types) for row in rows)
    else:
        coerced = (tuple(_coerce_db2_val(v) for v in row) for row in rows)
    inserted = _insert_multi_row(
        cur, table.upper(), col_names, coerced, "db2", paramstyle, batch_size
    )
    conn.commit()
    logger.info("bulk_load_db2[MULTI_ROW]: loaded %d rows into %s", inserted, full)
    return inserted


# ---------------------------------------------------------------------------
# Topology-aware DB2 loader classes
# ---------------------------------------------------------------------------

from ..base import TopologyAwareLoader


class DB2AdminCmdLoader(TopologyAwareLoader):
    """
    Fast path: ADMIN_CMD LOAD from a staged DEL file.

    Requires topology="shared_fs": a filesystem path readable by both
    statschema and the DB2 server process (NFS, bind-mount, Lima virtfs, etc.).
    Set STATSCHEMA_SERVER_STAGING_DIR to enable.

    CLOB columns are not supported by ADMIN_CMD DEL format; falls back to
    DB2ImportLoader (IMPORT) when CLOBs are present.
    """

    _CLOB_TYPES = frozenset({"string", "clob", "nclob"})

    def can_use(self, ctx, dialect: str, col_types: list[str] | None) -> bool:
        if dialect != "db2":
            return False
        if not ctx.has_shared_fs():
            logger.info(
                "DB2AdminCmdLoader: skipped — no shared filesystem configured. "
                "Set STATSCHEMA_SERVER_STAGING_DIR to enable ADMIN_CMD LOAD."
            )
            return False
        if col_types and any(t.lower() in self._CLOB_TYPES for t in col_types):
            logger.info(
                "DB2AdminCmdLoader: skipped — CLOB columns present; "
                "ADMIN_CMD DEL format does not support inline CLOB data."
            )
            return False
        return True

    def bulk_load(self, ctx, conn, df, table, col_names, wait=True) -> int:
        return bulk_load_db2(
            conn, df, table, col_names,
            staging_dir=ctx.staging_write_dir(),
            col_types=None,
            _ctx=ctx,
        )


class DB2ImportLoader(TopologyAwareLoader):
    """
    Medium path: IMPORT over the wire — works for CLOB columns but is slower
    than ADMIN_CMD LOAD for large tables.

    Requires topology="shared_fs" (same gate as AdminCmd) so the server can
    resolve the file path, but does NOT exclude CLOB columns.
    """


    def can_use(self, ctx, dialect: str, col_types: list[str] | None) -> bool:
        if dialect != "db2":
            return False
        if not ctx.has_shared_fs():
            logger.info(
                "DB2ImportLoader: skipped — no shared filesystem configured. "
                "Set STATSCHEMA_SERVER_STAGING_DIR to enable IMPORT."
            )
            return False
        return True

    def bulk_load(self, ctx, conn, df, table, col_names, wait=True) -> int:
        return bulk_load_db2(
            conn, df, table, col_names,
            staging_dir=ctx.staging_write_dir(),
            col_types=None,
            _ctx=ctx,
        )


class DB2MultiRowLoader(TopologyAwareLoader):
    """
    Fallback: parameterised multi-row INSERTs.  Always available.
    Used when no shared filesystem is configured (remote topology).
    """


    def can_use(self, ctx, dialect: str, col_types: list[str] | None) -> bool:
        return dialect == "db2"

    def bulk_load(self, ctx, conn, df, table, col_names, wait=True) -> int:
        return bulk_load_db2(
            conn, df, table, col_names,
            col_types=None,
            _force_multi_row=True,
        )
