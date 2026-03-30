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


def _db2_container_copy(host_path: str, container_path: str, container_spec: str) -> bool:
    """Copy a staging file into the filesystem that the Db2 server can see.

    ``container_spec`` supports three formats:
    * ``lima:<vm>:<container>``  — host → Lima VM → podman container
    * ``lima:<vm>``              — host → Lima VM only
    * ``<container>``            — direct docker/podman cp on the host
    """
    import subprocess

    parts = container_spec.split(":")
    if parts[0] == "lima" and len(parts) == 3:
        _, lima_vm, inner_container = parts
        try:
            r1 = subprocess.run(
                ["limactl", "copy", host_path, f"{lima_vm}:{container_path}"],
                capture_output=True, timeout=300,
            )
            if r1.returncode != 0:
                return False
            r2 = subprocess.run(
                [
                    "limactl", "shell", lima_vm, "bash", "-c",
                    f"sudo podman --root /var/lib/containers/storage cp "
                    f"{container_path} {inner_container}:{container_path}"
                    f" && sudo podman --root /var/lib/containers/storage exec {inner_container}"
                    f" chmod 644 {container_path}",
                ],
                capture_output=True, timeout=300,
            )
            return r2.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            import logging as _log
            _log.getLogger(__name__).warning(
                "bulk_load_db2: Lima container copy timed out or failed (%s)", e
            )
            return False

    if parts[0] == "lima" and len(parts) == 2:
        _, lima_vm = parts
        try:
            r = subprocess.run(
                ["limactl", "copy", host_path, f"{lima_vm}:{container_path}"],
                capture_output=True, timeout=60,
            )
            return r.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    container_name = container_spec
    for runtime in ("podman", "docker"):
        try:
            result = subprocess.run(
                [runtime, "cp", host_path, f"{container_name}:{container_path}"],
                capture_output=True, timeout=30,
            )
            if result.returncode == 0:
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return False


def bulk_load_db2(  # pragma: no cover
    conn: Any,
    df: Any,
    table: str,
    col_names: list[str],
    schema: Optional[str] = None,
    staging_dir: Optional[str] = None,
    col_types: Optional[list[str]] = None,
    _container_spec: Optional[str] = None,
    _force_multi_row: bool = False,
) -> int:
    """
    Load df into IBM Db2 LUW.

    Strategy (tried in order):
    1. Container copy + ADMIN_CMD: if ``DB2_CONTAINER_NAME`` is set AND the
       table has no CLOB columns.  DB2 DEL-format LOAD does not support inline
       CLOB data; tables with CLOB columns go straight to MULTI_ROW.
    2. Direct ADMIN_CMD with the host-side path (same CLOB restriction).
    3. MULTI_ROW fallback: parameterised batch INSERTs.
    """
    # _container_spec from DeploymentContext takes priority; fall back to env var.
    container_name = _container_spec or os.environ.get("DB2_CONTAINER_NAME", "").strip()

    # CLOB columns cannot be loaded inline via ADMIN_CMD DEL format.
    # Skip ADMIN_CMD for tables that contain canonical "string" / "clob" cols.
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
        prefix=f"statschema_{base}_", suffix=".del",
        dir=staging_dir,
    )

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

    suffix = os.path.basename(host_path)
    admin_cmd_path = host_path
    if container_name and not _has_clob:
        container_path = f"/tmp/{suffix}"
        if _db2_container_copy(host_path, container_path, container_name):
            admin_cmd_path = container_path
            logger.debug("bulk_load_db2: copied to %s:%s", container_name, container_path)
        else:
            logger.warning("bulk_load_db2: container copy to %s failed; will attempt ADMIN_CMD with host path", container_name)

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
# Phase 1: topology-aware DB2 loader classes
# These classes use DeploymentContext instead of the DB2_CONTAINER_NAME
# env-var hack.  The old bulk_load_db2() function is kept intact for backward
# compat; the classes delegate to it.
# ---------------------------------------------------------------------------

from ..base import TopologyAwareLoader


class DB2AdminCmdLoader(TopologyAwareLoader):
    """
    Fast path: ADMIN_CMD LOAD from a staged DEL file.

    Requires either:
    - ctx.container_spec is set (file is copied into the DB container), OR
    - ctx.topology == "collocated" (statschema runs on the DB host directly)

    CLOB columns are not supported by ADMIN_CMD DEL format; falls back to
    DB2ImportLoader (IMPORT) when CLOBs are present.
    """

    _CLOB_TYPES = frozenset({"string", "clob", "nclob"})

    def can_use(self, ctx, dialect: str, col_types: list[str] | None) -> bool:
        if dialect != "db2":
            return False
        # Need to be able to stage the file where the server can see it.
        if not (ctx.container_spec or ctx.is_collocated()):
            return False
        # ADMIN_CMD DEL format cannot handle inline CLOB data.
        if col_types and any(t.lower() in self._CLOB_TYPES for t in col_types):
            return False
        return True

    def bulk_load(self, ctx, conn, df, table, col_names, wait=True) -> int:
        return bulk_load_db2(
            conn, df, table, col_names,
            staging_dir=ctx.server_staging_dir,
            col_types=None,             # CLOB guard already applied in can_use
            _container_spec=ctx.container_spec,
        )


class DB2ImportLoader(TopologyAwareLoader):
    """
    Medium path: IMPORT over the wire — works for CLOB columns but is slower
    than ADMIN_CMD LOAD for large tables.

    Requires ctx.container_spec or collocated topology (same as AdminCmd)
    so the server can resolve the file path, but does NOT exclude CLOB columns.
    """

    def can_use(self, ctx, dialect: str, col_types: list[str] | None) -> bool:
        if dialect != "db2":
            return False
        return bool(ctx.container_spec or ctx.is_collocated())

    def bulk_load(self, ctx, conn, df, table, col_names, wait=True) -> int:
        return bulk_load_db2(
            conn, df, table, col_names,
            staging_dir=ctx.server_staging_dir,
            col_types=None,
            _container_spec=ctx.container_spec,
        )


class DB2MultiRowLoader(TopologyAwareLoader):
    """
    Fallback: parameterised multi-row INSERTs.  Always available.
    Used when no ADMIN_CMD path is reachable (remote topology, no container spec).
    """

    def can_use(self, ctx, dialect: str, col_types: list[str] | None) -> bool:
        return dialect == "db2"

    def bulk_load(self, ctx, conn, df, table, col_names, wait=True) -> int:
        # Force the multi-row INSERT path by suppressing container staging.
        return bulk_load_db2(
            conn, df, table, col_names,
            col_types=None,
            _container_spec=None,       # no container staging
            _force_multi_row=True,
        )
