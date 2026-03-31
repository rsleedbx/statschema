"""Oracle bulk-load loaders (direct_path_load + External Table + sqlldr + multi-row INSERT)."""

from __future__ import annotations

import csv
import logging
import os
import subprocess
import uuid
from typing import Any

from .._loader_shared import (
    _iter_rows,
    _coerce_oracle_val,
    _nan_to_none,
    _detect_paramstyle,
    _effective_batch_size,
    _insert_multi_row,
    _DIALECT_BATCH_DEFAULTS,
    BatchConfig,
)
from ..base import TopologyAwareLoader

logger = logging.getLogger(__name__)

# Oracle column type buckets (mirrors the inline logic in data_loader.py).
_VARCHAR_TYPES = frozenset({
    "CHAR", "VARCHAR2", "NCHAR", "NVARCHAR2", "CLOB", "NCLOB", "LONG",
})
_NUMBER_TYPES = frozenset({
    "NUMBER", "FLOAT", "BINARY_FLOAT", "BINARY_DOUBLE",
    "INTEGER", "SMALLINT", "INT",
})


def _oracle_is_oracledb(conn: Any) -> bool:
    """Return True if *conn* was created by the python-oracledb driver."""
    return type(conn).__module__.split(".")[0] == "oracledb"


def _get_oracle_col_kinds(
    conn: Any, table: str, col_names: list[str]
) -> list[tuple[bool, bool]]:
    """
    Return a list of (is_varchar, is_numeric) tuples for each column in
    *col_names*, looked up from ALL_TAB_COLUMNS for *table* in the current
    schema.
    """
    cur = conn.cursor()
    cur.execute("SELECT SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') FROM dual")
    schema_name: str = cur.fetchone()[0]

    cur.execute(
        "SELECT COLUMN_NAME, DATA_TYPE FROM ALL_TAB_COLUMNS "
        "WHERE OWNER = :o AND TABLE_NAME = :t ORDER BY COLUMN_ID",
        {"o": schema_name, "t": table.upper()},
    )
    col_types_map = {row[0]: row[1] for row in cur.fetchall()}
    cur.close()

    def _col_kind(col_name: str) -> tuple[bool, bool]:
        ddl_type = col_types_map.get(col_name, "")
        base = ddl_type.split("(")[0].strip()
        return base in _VARCHAR_TYPES, base in _NUMBER_TYPES

    return [_col_kind(c) for c in col_names]


def _coerce_dp(val: Any, varchar: bool, numeric: bool) -> Any:
    val = _coerce_oracle_val(_nan_to_none(val))
    if varchar and val is not None and not isinstance(val, str):
        return str(val)
    if numeric and isinstance(val, str) and val is not None:
        try:
            return float(val)
        except (ValueError, TypeError):
            pass
    return val


class OracleDirectPathLoader(TopologyAwareLoader):
    """
    Fast path: ``conn.direct_path_load()`` via python-oracledb.

    Available whenever the connection was created by the oracledb driver.
    Works in all topologies (remote, shared_fs, embedded) — direct_path_load
    is a client-side bulk API, not a server-side LOAD command.
    """


    def can_use(self, ctx: Any, dialect: str, col_types: list[str] | None) -> bool:
        # ctx.conn is not stored — check is deferred to bulk_load() where we
        # have the connection object.  We optimistically return True here and
        # let bulk_load() fall back to OracleMultiRowLoader if needed.
        return dialect == "oracle"

    def bulk_load(
        self,
        ctx: Any,
        conn: Any,
        df: Any,
        table: str,
        col_names: list[str],
        wait: bool = True,
    ) -> int:
        if not _oracle_is_oracledb(conn):
            # Not an oracledb connection — caller should have used
            # OracleMultiRowLoader instead.  Raise so _select_loader skips us.
            raise TypeError("OracleDirectPathLoader requires an oracledb connection")

        cur = conn.cursor()
        cur.execute("SELECT SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') FROM dual")
        schema_name: str = cur.fetchone()[0]
        cur.close()

        upper_cols = [c.upper() for c in col_names]
        col_kinds = _get_oracle_col_kinds(conn, table, upper_cols)

        coerced = [
            tuple(
                _coerce_dp(v, col_kinds[i][0], col_kinds[i][1])
                for i, v in enumerate(row)
            )
            for row in _iter_rows(df)
        ]

        conn.direct_path_load(
            schema_name=schema_name,
            table_name=table.upper(),
            column_names=upper_cols,
            data=coerced,
        )
        inserted = len(coerced)
        logger.info(
            "OracleDirectPathLoader: loaded %d rows into %s", inserted, table
        )
        return inserted


class OracleMultiRowLoader(TopologyAwareLoader):
    """
    Fallback: parameterised multi-row INSERTs.

    Works with any DBAPI-2 connection (cx_Oracle, python-oracledb thin/thick).
    Used when direct_path_load is unavailable or when the connection module
    is not oracledb.
    """


    def can_use(self, ctx: Any, dialect: str, col_types: list[str] | None) -> bool:
        return dialect == "oracle"

    def bulk_load(
        self,
        ctx: Any,
        conn: Any,
        df: Any,
        table: str,
        col_names: list[str],
        wait: bool = True,
    ) -> int:
        paramstyle = _detect_paramstyle(conn)
        cfg = _DIALECT_BATCH_DEFAULTS.get("oracle", BatchConfig())
        batch_size = _effective_batch_size(cfg, len(col_names))
        cur = conn.cursor()

        rows = list(_iter_rows(df))
        coerced = (
            tuple(_coerce_oracle_val(_nan_to_none(v)) for v in row)
            for row in rows
        )
        inserted = _insert_multi_row(
            cur, table.upper(), col_names, coerced, "oracle", paramstyle, batch_size
        )
        conn.commit()
        logger.info(
            "OracleMultiRowLoader: loaded %d rows into %s", inserted, table
        )
        return inserted


# ---------------------------------------------------------------------------
# Helpers shared by the two shared-filesystem loaders
# ---------------------------------------------------------------------------

def _oracle_staging_root(staging_dir: str | None = None) -> str:
    """Return the directory where statschema writes Oracle staging files.

    ``staging_dir`` must come from ``ctx.staging_write_dir()``
    (i.e. ``STATSCHEMA__CLIENT_STAGING_DIR``).  Oracle External Tables and
    SQL*Loader both require a shared filesystem, so there is no local-temp
    fallback — a missing value is a configuration error.
    """
    if staging_dir:
        os.makedirs(staging_dir, exist_ok=True)
        return staging_dir
    raise RuntimeError(
        "STATSCHEMA__CLIENT_STAGING_DIR is not set. "
        "Oracle External Tables and SQL*Loader require a filesystem path "
        "accessible to both statschema and the Oracle server process. "
        "Set STATSCHEMA__CLIENT_STAGING_DIR (write path) and "
        "STATSCHEMA__SERVER_STAGING_DIR (Oracle server read path). "
        "See .env.example for Lima and NFS examples."
    )


def _get_oracle_col_ddl_types(conn: Any, table: str, col_names: list[str]) -> list[str]:
    """Return DATA_TYPE string for each column in col_names from ALL_TAB_COLUMNS."""
    cur = conn.cursor()
    cur.execute("SELECT SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') FROM dual")
    schema: str = cur.fetchone()[0]
    cur.execute(
        "SELECT COLUMN_NAME, DATA_TYPE FROM ALL_TAB_COLUMNS "
        "WHERE OWNER = :o AND TABLE_NAME = :t ORDER BY COLUMN_ID",
        {"o": schema, "t": table.upper()},
    )
    type_map = {row[0]: row[1] for row in cur.fetchall()}
    cur.close()
    return [type_map.get(c, "VARCHAR2") for c in col_names]


def _oracle_current_schema(conn: Any) -> str:
    cur = conn.cursor()
    cur.execute("SELECT SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') FROM dual")
    schema: str = cur.fetchone()[0].strip()
    cur.close()
    return schema


def _oracle_sqlldr_userid(ctx: Any) -> str:
    """Build a sqlldr userid string from credentials stored in ``ctx``."""
    user = ctx.username or "system"
    pwd  = ctx.password or ""
    host = ctx.host or "localhost"
    port = ctx.port or 1521
    svc  = ctx.database or "XE"
    return f"{user}/{pwd}@//{host}:{port}/{svc}"


def _sqlldr_ctl_content(
    csv_path_in_container: str,
    schema: str,
    table: str,
    col_names: list[str],
    col_ddl_types: list[str],
) -> str:
    """Generate a SQL*Loader control file with per-column type directives."""
    col_specs = []
    for col, dtype in zip(col_names, col_ddl_types):
        base = dtype.split("(")[0].strip().upper()
        if "TIMESTAMP" in base:
            col_specs.append(
                f'  {col} TIMESTAMP "YYYY-MM-DD HH24:MI:SS.FF6" NULLIF {col}=BLANKS'
            )
        elif base == "DATE":
            col_specs.append(f'  {col} DATE "YYYY-MM-DD HH24:MI:SS" NULLIF {col}=BLANKS')
        else:
            col_specs.append(f"  {col}")
    col_block = ",\n".join(col_specs)
    return (
        f"LOAD DATA\n"
        f"INFILE '{csv_path_in_container}'\n"
        f"INTO TABLE {schema}.{table}\n"
        f"FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '\"'\n"
        f"TRAILING NULLCOLS\n"
        f"(\n{col_block}\n)\n"
    )


def _ext_table_select_sql(
    schema: str,
    target_table: str,
    ext_table: str,
    col_names: list[str],
    col_ddl_types: list[str],
) -> str:
    """Build INSERT … SELECT with explicit casts for TIMESTAMP/DATE/NUMBER columns."""
    exprs = []
    for col, dtype in zip(col_names, col_ddl_types):
        base = dtype.split("(")[0].strip().upper()
        if "TIMESTAMP" in base:
            exprs.append(
                f"CASE WHEN {col} IS NULL OR {col} = '' THEN NULL "
                f"ELSE TO_TIMESTAMP({col}, 'YYYY-MM-DD HH24:MI:SS.FF6') END"
            )
        elif base == "DATE":
            exprs.append(
                f"CASE WHEN {col} IS NULL OR {col} = '' THEN NULL "
                f"ELSE TO_DATE({col}, 'YYYY-MM-DD HH24:MI:SS') END"
            )
        elif base in ("NUMBER", "FLOAT", "BINARY_FLOAT", "BINARY_DOUBLE",
                      "INTEGER", "SMALLINT", "INT"):
            exprs.append(
                f"CASE WHEN {col} IS NULL OR {col} = '' THEN NULL "
                f"ELSE TO_NUMBER({col}) END"
            )
        else:
            exprs.append(f"NULLIF({col}, '')")
    cols_str = ", ".join(col_names)
    exprs_str = ", ".join(exprs)
    return (
        f"INSERT INTO {schema}.{target_table} ({cols_str})\n"
        f"SELECT {exprs_str}\n"
        f"FROM {schema}.{ext_table}"
    )


# ---------------------------------------------------------------------------
# Collocated file-based loaders (disabled under QEMU via server_is_emulated)
# ---------------------------------------------------------------------------

class OracleExternalTableLoader(TopologyAwareLoader):
    """
    File-based load via Oracle External Tables (ORACLE_LOADER driver).

    Writes a CSV to the staging directory then executes:
        INSERT INTO target SELECT <cols with type casts> FROM ext_table

    Requires topology="shared_fs", oracle_directory set (an Oracle DIRECTORY
    object whose filesystem path is on a shared filesystem visible to both
    statschema and the Oracle server process — local disk, NFS, or bind-mount),
    and server_is_emulated=False.

    On native x86_64 this is comparable in throughput to sqlldr.  Under QEMU
    emulation on Apple Silicon the KUP process spawn incurs ~7× overhead versus
    direct_path_load; set server_is_emulated=True (or STATSCHEMA_SERVER_IS_EMULATED=1)
    to fall through to OracleDirectPathLoader in that environment.
    """


    def can_use(self, ctx: Any, dialect: str, col_types: list[str] | None) -> bool:
        if dialect != "oracle":
            return False
        if not ctx.has_shared_fs():
            logger.info(
                "OracleExternalTableLoader: skipped — no shared filesystem configured. "
                "Set STATSCHEMA__SERVER_STAGING_DIR and STATSCHEMA__ORACLE__DIRECTORY to enable "
                "External Tables."
            )
            return False
        if ctx.oracle_directory is None:
            logger.info(
                "OracleExternalTableLoader: skipped — ORACLE_SERVER_DIRECTORY is not set. "
                "Create an Oracle DIRECTORY object pointing to the server-side staging path "
                "and set ORACLE_SERVER_DIRECTORY to its name."
            )
            return False
        if ctx.server_is_emulated:
            logger.info(
                "OracleExternalTableLoader: skipped — server_is_emulated=True; "
                "KUP process-fork overhead under QEMU makes External Tables ~7× slower "
                "than direct_path_load. Set server_is_emulated: false or use OracleDirectPathLoader."
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
        # Write to the client-side mount; Oracle reads from the server-side mount.
        # If both are the same (Lima / local), staging_write_dir() == server_staging_dir.
        write_dir = _oracle_staging_root(ctx.staging_write_dir())
        schema = _oracle_current_schema(conn)
        upper_cols = [c.upper() for c in col_names]
        col_ddl_types = _get_oracle_col_ddl_types(conn, table, upper_cols)

        uid = uuid.uuid4().hex[:8]
        csv_name = f"ss_ext_{table.lower()}_{uid}.csv"
        ext_table = f"SS_EXT_{table.upper()}_{uid}"
        csv_path = os.path.join(write_dir, csv_name)

        rows = list(_iter_rows(df))
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            for row in rows:
                writer.writerow(["" if v is None else v for v in row])
        os.chmod(csv_path, 0o644)

        cur = conn.cursor()
        inserted = 0
        try:
            # All ext-table columns are VARCHAR2; type casts happen in the INSERT.
            col_defs = ", ".join(f"{c} VARCHAR2(4000)" for c in upper_cols)
            cur.execute(
                f"CREATE TABLE {schema}.{ext_table} ({col_defs}) "
                f"ORGANIZATION EXTERNAL ("
                f"  TYPE ORACLE_LOADER "
                f"  DEFAULT DIRECTORY {ctx.oracle_directory} "
                f"  ACCESS PARAMETERS ("
                f"    RECORDS DELIMITED BY NEWLINE "
                f"    CHARACTERSET AL32UTF8 "
                f"    NOLOGFILE NODISCARDFILE NOBADFILE "
                f"    FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '\"' "
                f"    MISSING FIELD VALUES ARE NULL"
                f"  ) LOCATION ('{csv_name}')"
                f") REJECT LIMIT UNLIMITED"
            )
            conn.commit()

            insert_sql = _ext_table_select_sql(
                schema, table.upper(), ext_table, upper_cols, col_ddl_types
            )
            cur.execute(insert_sql)
            inserted = cur.rowcount
            conn.commit()
        finally:
            try:
                cur.execute(f"DROP TABLE {schema}.{ext_table}")
                conn.commit()
            except Exception:
                pass
            try:
                os.unlink(csv_path)
            except OSError:
                pass
            cur.close()

        logger.info(
            "OracleExternalTableLoader: loaded %d rows into %s.%s",
            inserted, schema, table,
        )
        return inserted


class OracleSqlldrLoader(TopologyAwareLoader):
    """
    High-throughput load via Oracle SQL*Loader (sqlldr DIRECT=TRUE).

    Writes a CSV + control file to a local staging directory, then runs sqlldr
    as a local subprocess on the statschema host.  sqlldr connects to Oracle
    over TCP — no shared filesystem required.

    Requires ``oracle_sqlldr_binary`` to be set in the connection profile.
    Credentials (host, port, database, username, password) are read from the
    DeploymentContext, which is populated from the connection profile.

    On native x86_64 at ≥100K rows, sqlldr DIRECT=TRUE is typically faster
    than direct_path_load because it formats Oracle blocks in parallel with
    minimal redo generation.  Under QEMU the fixed process-spawn overhead (~1.9s
    measured) and JIT translation overhead make it slower at all tested scales.
    """


    def _sqlldr_binary(self, ctx: Any) -> str:
        """Resolve the local sqlldr path from ``ctx.oracle_sqlldr_binary``.

        Set ``oracle_sqlldr_binary`` in the connection profile (or
        ``STATSCHEMA__ORACLE__SQLLDR_BINARY`` env var).  Install examples:

          Linux x86_64 (RPM):   /usr/lib/oracle/21/client64/bin/sqlldr
          macOS Instant Client: /opt/oracle/instantclient_21_12/sqlldr
        """
        binary = (ctx.oracle_sqlldr_binary or "").strip()
        if not binary:
            raise RuntimeError(
                "oracle_sqlldr_binary is not set in the connection profile. "
                "Install Oracle Instant Client Tools on the statschema host and set "
                "oracle_sqlldr_binary to the local sqlldr path."
            )
        return binary

    def can_use(self, ctx: Any, dialect: str, col_types: list[str] | None) -> bool:
        import shutil
        if dialect != "oracle":
            return False
        binary = (ctx.oracle_sqlldr_binary or "").strip()
        if not binary:
            logger.info(
                "OracleSqlldrLoader: skipped — oracle_sqlldr_binary is not set in the "
                "connection profile.  Install Oracle Instant Client Tools and set "
                "oracle_sqlldr_binary."
            )
            return False
        if not (os.path.isfile(binary) or shutil.which(binary)):
            raise RuntimeError(
                f"oracle_sqlldr_binary={binary!r} is set but the file does not exist. "
                "Verify the path or remove the field to skip this loader."
            )
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
        import tempfile as _tempfile
        # sqlldr runs locally — use client_staging_dir if set, else system temp.
        write_dir = ctx.staging_write_dir() or _tempfile.gettempdir()
        os.makedirs(write_dir, exist_ok=True)

        schema = _oracle_current_schema(conn)
        upper_cols = [c.upper() for c in col_names]
        col_ddl_types = _get_oracle_col_ddl_types(conn, table, upper_cols)

        uid = uuid.uuid4().hex[:8]
        csv_name = f"ss_sqlldr_{table.lower()}_{uid}.csv"
        ctl_name = f"ss_sqlldr_{table.lower()}_{uid}.ctl"
        log_name = f"ss_sqlldr_{table.lower()}_{uid}.log"
        bad_name = f"ss_sqlldr_{table.lower()}_{uid}.bad"
        csv_path = os.path.join(write_dir, csv_name)
        ctl_path = os.path.join(write_dir, ctl_name)
        log_path = os.path.join(write_dir, log_name)
        bad_path = os.path.join(write_dir, bad_name)

        rows = list(_iter_rows(df))
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            for row in rows:
                writer.writerow(["" if v is None else v for v in row])
        os.chmod(csv_path, 0o644)

        ctl_content = _sqlldr_ctl_content(
            csv_path_in_container=csv_path,   # local path — sqlldr reads it here
            schema=schema,
            table=table.upper(),
            col_names=upper_cols,
            col_ddl_types=col_ddl_types,
        )
        with open(ctl_path, "w", encoding="utf-8") as f:
            f.write(ctl_content)
        os.chmod(ctl_path, 0o644)

        userid = _oracle_sqlldr_userid(ctx)
        cmd = [
            self._sqlldr_binary(ctx),
            f"userid={userid}",
            f"control={ctl_path}",
            f"log={log_path}",
            f"bad={bad_path}",
            "direct=true",
            "silent=header,feedback",
        ]

        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            # sqlldr exit codes: 0=success, 2=partial success (rows rejected),
            # 1=fatal error.  Treat 0 and 2 as acceptable; raise on anything else.
            if r.returncode not in (0, 2):
                raise RuntimeError(
                    f"OracleSqlldrLoader: sqlldr failed (rc={r.returncode})\n"
                    f"stdout: {r.stdout[-2000:]}\nstderr: {r.stderr[-2000:]}"
                )
        finally:
            for p in (csv_path, ctl_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass

        logger.info(
            "OracleSqlldrLoader: loaded %d rows into %s.%s",
            len(rows), schema, table,
        )
        return len(rows)
