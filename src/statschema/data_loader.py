"""
data_loader — load a generated DataFrame into a target database.

Three strategies
----------------
SINGLETON   INSERT INTO t (cols) VALUES (row)          — one row at a time; maximum
                                                          compatibility, slowest path.
MULTI_ROW   INSERT INTO t (cols) VALUES (r1),(r2),…   — batched; 10–100× faster than
                                                          singleton for medium tables.
BULK_COPY   dialect-native bulk path                  — fastest for large tables:
              PostgreSQL / CockroachDB / Neon  — COPY FROM STDIN (no temp file)
              MySQL / MariaDB                 — LOAD DATA LOCAL INFILE
              SQL Server (mssql-python)       — conn.bulk_copy() via native BCP/DDBC
              SQL Server (pyodbc/pymssql)     — BULK INSERT from a staging CSV (fallback)
              IBM Db2 LUW                     — LOAD FROM … OF DEL FORMAT via ADMIN_CMD

Databricks
----------
Databricks is not supported by this loader. dbldatagen produces a PySpark DataFrame
and Databricks does not use a DBAPI2 connection for writes. Use Spark's native write
methods on the generated DataFrame:

    df.write.saveAsTable("catalog.schema.table")          # managed Delta table
    df.write.format("delta").save("/path/to/table")       # external Delta table

Spark's write path is already a distributed bulk write — there is no faster alternative
for Databricks.

Batch size limits and auto-discovery
-------------------------------------
Every database imposes limits on the number of rows or bind parameters per
INSERT statement.  Known safe defaults are stored in _DIALECT_BATCH_DEFAULTS.

auto_discover=True runs a binary search using SAVEPOINT / ROLLBACK TO SAVEPOINT
to find the actual server limit without committing any rows.  All-NULL probe
rows are used — the target table must accept NULLs on all probed columns.

Oracle and multi-row INSERT
----------------------------
Oracle does not support the standard multi-row VALUES syntax.
MULTI_ROW for Oracle uses INSERT ALL … SELECT 1 FROM DUAL:

    INSERT ALL
      INTO "t" ("a","b") VALUES (:v0_0, :v0_1)
      INTO "t" ("a","b") VALUES (:v1_0, :v1_1)
    SELECT 1 FROM DUAL

Parameter markers
-----------------
Detected from the connection's module paramstyle attribute:

    pyformat  (%s)   psycopg2 · pymysql · pymssql
    qmark     (?)    pyodbc · ibm_db_dbi · sqlite3
    numeric   (:N)   cx_Oracle / oracledb
"""

from __future__ import annotations

import csv
import io
import itertools
import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Iterator, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Strategy + config
# ---------------------------------------------------------------------------

class LoadStrategy(str, Enum):
    SINGLETON = "singleton"
    MULTI_ROW = "multi_row"
    BULK_COPY = "bulk_copy"


@dataclass
class BatchConfig:
    """Batch size constraints for MULTI_ROW inserts."""

    max_rows: int = 1000
    """Maximum rows per INSERT statement."""

    max_params: int = 65535
    """Maximum bind parameters per statement (rows × cols must not exceed this)."""

    auto_discover: bool = False
    """When True, binary-search for the actual server limit before loading."""


# Known safe defaults per dialect.
# Effective batch = min(max_rows, max_params // n_cols).
_DIALECT_BATCH_DEFAULTS: dict[str, BatchConfig] = {
    "mysql":       BatchConfig(max_rows=1000, max_params=65535),
    "mariadb":     BatchConfig(max_rows=1000, max_params=65535),
    "postgres":    BatchConfig(max_rows=1000, max_params=65535),
    "cockroachdb": BatchConfig(max_rows=1000, max_params=65535),
    "neon":        BatchConfig(max_rows=1000, max_params=65535),
    # SQL Server hard limit: 1000 rows per INSERT; 2100 bind params per batch.
    "sqlserver":   BatchConfig(max_rows=1000, max_params=2100),
    # Oracle INSERT ALL: conservative default; no hard row limit but INSERT ALL
    # becomes expensive above a few hundred rows.
    "oracle":      BatchConfig(max_rows=500,  max_params=65535),
    "db2":         BatchConfig(max_rows=1000, max_params=32767),
    # Databricks does not use DBAPI2 for writes — use df.write.saveAsTable() instead.
    # Included so dialect detection does not silently fall through to unknown defaults.
    "databricks":  BatchConfig(max_rows=1000, max_params=65535),
    # SQLite: SQLITE_LIMIT_VARIABLE_NUMBER was 999 before 3.32.0, 32766 after.
    # Use 32766 as the safe default; discover_max_batch_size() handles the rest.
    "sqlite":      BatchConfig(max_rows=1000, max_params=32766),
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _detect_paramstyle(conn: Any) -> str:
    """Return the DBAPI2 paramstyle for the connection's driver module."""
    module_name = type(conn).__module__.split(".")[0]
    module = sys.modules.get(module_name)
    style = getattr(module, "paramstyle", None)
    if style:
        return style
    if module_name in ("cx_Oracle", "oracledb"):
        return "numeric"
    if module_name in ("ibm_db_dbi", "ibm_db", "pyodbc", "sqlite3"):
        return "qmark"
    return "pyformat"


def _quote_id(name: str, dialect: str) -> str:
    """Quote an identifier for the given dialect."""
    if dialect == "sqlserver":
        return f"[{name}]"
    if dialect in ("mysql", "mariadb", "databricks"):
        return f"`{name}`"
    return f'"{name}"'


def _placeholder(paramstyle: str, index: int) -> str:
    """Return a single bind-parameter placeholder."""
    if paramstyle == "qmark":
        return "?"
    if paramstyle in ("numeric", "named"):
        return f":{index + 1}"
    return "%s"


def _effective_batch_size(config: BatchConfig, n_cols: int) -> int:
    """Compute the largest batch size that satisfies both row and param limits."""
    if n_cols == 0:
        return config.max_rows
    param_limited = max(1, config.max_params // n_cols)
    return min(config.max_rows, param_limited)


def _nan_to_none(val: Any) -> Any:
    """Convert NaN/Infinity floats (pandas null sentinels) to None (SQL NULL).

    Covers both Python float and numpy scalar subclasses (numpy.float32/64 etc.),
    which may not be instances of Python's built-in float in NumPy ≥ 2.
    """
    try:
        import math
        if math.isnan(val) or math.isinf(val):
            return None
    except (TypeError, ValueError):
        pass
    return val


def _iter_rows(df: Any) -> Iterator[tuple]:
    """Yield rows as tuples from a pandas DataFrame, PySpark DataFrame, or iterable."""
    module = type(df).__module__.split(".")[0]
    if module == "pandas":
        for row in df.itertuples(index=False, name=None):
            yield tuple(_nan_to_none(v) for v in row)
    elif module == "pyspark":
        for row in df.toLocalIterator():
            yield tuple(row)
    else:
        for row in df:
            # Dicts (e.g. from generate_rows) must yield values, not keys.
            if isinstance(row, dict):
                yield tuple(row.values())
            else:
                yield tuple(row)


def _col_names(df: Any, explicit_cols: Optional[list[str]]) -> list[str]:
    """Return column names from explicit list or infer from DataFrame."""
    if explicit_cols:
        return explicit_cols
    module = type(df).__module__.split(".")[0]
    if module == "pandas":
        return list(df.columns)
    if module == "pyspark":
        return list(df.columns)
    raise ValueError(
        "Cannot infer column names from df type; pass cols= explicitly."
    )


# ---------------------------------------------------------------------------
# SQL builders
# ---------------------------------------------------------------------------

def _build_multi_row_sql(
    table: str,
    col_names: list[str],
    batch: list[tuple],
    dialect: str,
    paramstyle: str,
) -> tuple[str, list]:
    """Build a multi-row INSERT and flat params list (all dialects except Oracle)."""
    qtable = _quote_id(table, dialect)
    qcols  = ", ".join(_quote_id(c, dialect) for c in col_names)
    n      = len(col_names)
    clauses: list[str] = []
    params: list = []
    offset = 0
    for row in batch:
        ph = ", ".join(_placeholder(paramstyle, offset + i) for i in range(n))
        clauses.append(f"({ph})")
        params.extend(row)
        offset += n
    return f"INSERT INTO {qtable} ({qcols}) VALUES {', '.join(clauses)}", params


_ORACLE_TIME_RE = __import__("re").compile(
    r"^(\d{1,2}):(\d{2}):(\d{2})(\.\d+)?(\+\d{2}:\d{2}|Z)?$"
)
_ORACLE_ISO_DATE_RE = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}$")


def _coerce_oracle_val(val: Any) -> Any:
    """Convert Python values that oracledb cannot auto-bind to safe equivalents.

    - "HH:MM:SS" strings → datetime.datetime (2000-01-01 anchor; Oracle TIME → TIMESTAMP)
    - "YYYY-MM-DD" strings → datetime.date (NLS-safe)
    """
    if not isinstance(val, str):
        return val
    from datetime import date as _date, datetime as _dt
    m = _ORACLE_TIME_RE.match(val)
    if m:
        h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return _dt(2000, 1, 1, h, mi, s)
    if _ORACLE_ISO_DATE_RE.match(val):
        return _date.fromisoformat(val)
    return val


def _build_oracle_all_sql(
    table: str,
    col_names: list[str],
    batch: list[tuple],
) -> tuple[str, dict]:
    """Build Oracle INSERT ALL … SELECT 1 FROM DUAL with named bind parameters."""
    qtable = _quote_id(table, "oracle")
    qcols  = ", ".join(_quote_id(c, "oracle") for c in col_names)
    n      = len(col_names)
    into_parts: list[str] = []
    params: dict[str, Any] = {}
    for r, row in enumerate(batch):
        ph = ", ".join(f":v{r}_{c}" for c in range(n))
        into_parts.append(f"  INTO {qtable} ({qcols}) VALUES ({ph})")
        for c, val in enumerate(row):
            params[f"v{r}_{c}"] = _coerce_oracle_val(val)
    sql = "INSERT ALL\n" + "\n".join(into_parts) + "\nSELECT 1 FROM DUAL"
    return sql, params


# ---------------------------------------------------------------------------
# Core insert functions
# ---------------------------------------------------------------------------

def _insert_singleton(
    cur: Any,
    table: str,
    col_names: list[str],
    rows: Iterable[tuple],
    dialect: str,
    paramstyle: str,
) -> int:
    """Execute one INSERT per row. Accepts any iterable — lists or generators."""
    qtable = _quote_id(table, dialect)
    qcols  = ", ".join(_quote_id(c, dialect) for c in col_names)
    n      = len(col_names)
    ph     = ", ".join(_placeholder(paramstyle, i) for i in range(n))
    sql    = f"INSERT INTO {qtable} ({qcols}) VALUES ({ph})"
    count  = 0
    for row in rows:
        cur.execute(sql, row)
        count += 1
    return count


def _insert_multi_row(
    cur: Any,
    table: str,
    col_names: list[str],
    rows: Iterable[tuple],
    dialect: str,
    paramstyle: str,
    batch_size: int,
) -> int:
    """
    Execute batched multi-row INSERTs.

    Accepts any iterable — lists, DataFrames, or generators.  Uses
    itertools.islice so TPC-H LINEITEM (6 M rows) streams without
    materialising all rows in memory.
    """
    inserted = 0
    it = iter(rows)
    while True:
        batch = list(itertools.islice(it, batch_size))
        if not batch:
            break
        if dialect == "oracle":
            sql, params = _build_oracle_all_sql(table, col_names, batch)
        else:
            sql, params = _build_multi_row_sql(table, col_names, batch, dialect, paramstyle)
        cur.execute(sql, params)
        inserted += len(batch)
    return inserted


# ---------------------------------------------------------------------------
# Batch size auto-discovery
# ---------------------------------------------------------------------------

def discover_max_batch_size(
    conn: Any,
    table: str,
    col_names: list[str],
    dialect: str,
    *,
    min_rows: int = 1,
    start_max: int | None = None,
) -> int:
    """
    Binary-search for the maximum rows per INSERT the server accepts.

    Uses SAVEPOINT / ROLLBACK TO SAVEPOINT so no rows are committed.
    Probe rows contain all NULLs — the table must accept NULLs on the
    probed columns for the search to work.

    SQL Server does not support SAVEPOINT; the search uses a plain
    ROLLBACK for that dialect.

    Parameters
    ----------
    conn        Live DBAPI2 connection to the target database.
    table       Target table name (must already exist).
    col_names   Column names in insert order.
    dialect     Database dialect string.
    min_rows    Lower bound for the search (default 1).
    start_max   Upper bound for the search.  Defaults to the dialect's
                BatchConfig.max_rows from _DIALECT_BATCH_DEFAULTS.

    Returns
    -------
    int — the largest batch size that the server accepted.
    """
    config = _DIALECT_BATCH_DEFAULTS.get(dialect, BatchConfig())
    hi     = start_max if start_max is not None else config.max_rows
    lo     = min_rows
    best   = min_rows

    paramstyle    = _detect_paramstyle(conn)
    null_row      = tuple(None for _ in col_names)
    cur           = conn.cursor()
    use_savepoint = dialect not in ("sqlserver",)

    while lo <= hi:
        mid   = (lo + hi) // 2
        batch = [null_row] * mid
        try:
            if use_savepoint:
                cur.execute("SAVEPOINT _probe")
            if dialect == "oracle":
                sql, params = _build_oracle_all_sql(table, col_names, batch)
            else:
                sql, params = _build_multi_row_sql(table, col_names, batch, dialect, paramstyle)
            cur.execute(sql, params)
            # Roll back the probe — we don't want to insert NULLs
            if use_savepoint:
                cur.execute("ROLLBACK TO SAVEPOINT _probe")
            else:
                conn.rollback()
            best = mid
            lo   = mid + 1
            logger.debug("discover_max_batch_size: mid=%d succeeded, best=%d", mid, best)
        except Exception as exc:
            logger.debug("discover_max_batch_size: mid=%d failed: %s", mid, exc)
            try:
                if use_savepoint:
                    cur.execute("ROLLBACK TO SAVEPOINT _probe")
                else:
                    conn.rollback()
            except Exception:
                pass
            hi = mid - 1

    logger.info(
        "discover_max_batch_size: dialect=%s table=%s result=%d", dialect, table, best
    )
    return best


# ---------------------------------------------------------------------------
# Bulk copy loaders
# ---------------------------------------------------------------------------

def bulk_load_postgres(  # pragma: no cover
    conn: Any,
    df: Any,
    table: str,
    col_names: list[str],
) -> int:
    """
    Load df into PostgreSQL / CockroachDB / Neon via COPY … FROM STDIN WITH CSV.

    Uses psycopg2 cursor.copy_expert() for streaming transfer — no temp file.
    """
    cur    = conn.cursor()
    qtable = _quote_id(table, "postgres")
    qcols  = ", ".join(_quote_id(c, "postgres") for c in col_names)
    sql    = f"COPY {qtable} ({qcols}) FROM STDIN WITH (FORMAT CSV, NULL '')"

    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    count = 0
    for row in _iter_rows(df):
        writer.writerow(["" if v is None else v for v in row])
        count += 1

    buf.seek(0)
    cur.copy_expert(sql, buf)
    conn.commit()
    logger.info("bulk_load_postgres: copied %d rows into %s", count, table)
    return count


def bulk_load_mysql(  # pragma: no cover
    conn: Any,
    df: Any,
    table: str,
    col_names: list[str],
    staging_dir: Optional[str] = None,
) -> int:
    """
    Load df into MySQL / MariaDB via LOAD DATA LOCAL INFILE.

    Requires the server to have local_infile=ON and the client connection
    to be opened with local_infile=True (pymysql: connect(..., local_infile=True)).
    """
    cur    = conn.cursor()
    qtable = _quote_id(table, "mysql")
    qcols  = ", ".join(_quote_id(c, "mysql") for c in col_names)
    path   = os.path.join(staging_dir or tempfile.gettempdir(), f"statschema_{table}.csv")

    count = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for row in _iter_rows(df):
            writer.writerow(["\\N" if v is None else v for v in row])
            count += 1

    cur.execute(
        f"LOAD DATA LOCAL INFILE %s INTO TABLE {qtable} "
        f"FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '\"' "
        f"LINES TERMINATED BY '\\n' ({qcols})",
        (path,),
    )
    conn.commit()
    os.unlink(path)
    logger.info("bulk_load_mysql: loaded %d rows into %s", count, table)
    return count


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

    Two paths, chosen automatically based on the connection driver:

    1. mssql-python (v1.4.0+, GA February 2026) — preferred.
       Uses conn.bulk_copy(table, rows), which calls the native SQL Server
       BCP API directly via DDBC with no temp file.
       Detected by type(conn).__module__.startswith("mssql_python").

    2. pyodbc / pymssql fallback — BULK INSERT from a staging CSV.
       The staging file must be accessible to the SQL Server process.
       For local development this is the same machine; for remote servers
       use a UNC path or pre-stage the file on the server.
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

    # pyodbc / pymssql fallback: BULK INSERT from a staging CSV
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


def _db2_container_copy(host_path: str, container_path: str, container_spec: str) -> bool:
    """Copy a file from the host to the filesystem that the Db2 server can see.

    ``container_spec`` is the value of the ``DB2_CONTAINER_NAME`` env var.  It
    supports three formats:

    * ``lima:<vm>:<container>``  — host file → Lima VM → podman container inside
      the VM.  Example: ``lima:db2:db2ce``.  This is the correct topology for the
      standard statschema test environment where Db2 runs inside a container that
      is itself managed by Lima.
    * ``<container>``            — direct docker/podman cp on the host.  Works when
      Db2 runs in a local container with no VM wrapper.
    * ``<vm>``                   — Lima copy only (no inner container).  Works when
      Db2 runs natively inside the Lima VM.

    Returns True if the copy succeeded, False otherwise.
    """
    import subprocess  # stdlib — deferred to avoid startup overhead

    parts = container_spec.split(":")
    if parts[0] == "lima" and len(parts) == 3:
        # Lima VM + inner container: host → VM → container
        _, lima_vm, inner_container = parts
        try:
            # Step 1: host → Lima VM /tmp
            r1 = subprocess.run(
                ["limactl", "copy", host_path, f"{lima_vm}:{container_path}"],
                capture_output=True, timeout=300,
            )
            if r1.returncode != 0:
                return False
            # Step 2: Lima VM → inner container (one ssh+podman round-trip)
            r2 = subprocess.run(
                [
                    "limactl", "shell", lima_vm, "bash", "-c",
                    f"sudo podman --root /var/lib/containers/storage cp "
                    f"{container_path} {inner_container}:{container_path}",
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
        # Lima VM only (no inner container)
        _, lima_vm = parts
        try:
            r = subprocess.run(
                ["limactl", "copy", host_path, f"{lima_vm}:{container_path}"],
                capture_output=True, timeout=60,
            )
            return r.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    # Direct docker/podman copy (container on the host)
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
) -> int:
    """
    Load df into IBM Db2 LUW.

    Strategy (tried in order):
    1. Container copy + ADMIN_CMD: if the env var ``DB2_CONTAINER_NAME`` is set,
       copy the staging CSV into the container with ``podman/docker cp`` and run
       ``SYSPROC.ADMIN_CMD('LOAD FROM …')``.  Fast; works with any containerised
       Db2 instance.
    2. Direct ADMIN_CMD: try with the host-side path.  Works when Db2 runs natively
       or the staging directory is shared with the server (e.g. via a bind mount).
       Silently loads 0 rows when the server cannot see the file — detected and
       retried below.
    3. MULTI_ROW fallback: parameterised batch INSERTs.  Works everywhere but is
       slow for tables with many columns or millions of rows.
    """
    import os as _os  # already imported at module level; kept for clarity

    container_name = _os.environ.get("DB2_CONTAINER_NAME", "").strip()

    cur = conn.cursor()
    if schema is None:
        cur.execute("VALUES CURRENT SCHEMA")
        row = cur.fetchone()
        schema = (row[0] if row else "").strip().upper()

    full  = f'"{schema}"."{table.upper()}"'
    rows  = list(_iter_rows(df))
    count = len(rows)
    host_path = os.path.join(
        staging_dir or tempfile.gettempdir(), f"statschema_{table}.del"
    )

    def _db2_csv_val(v: Any) -> Any:
        """Coerce a Python value to a DB2 DEL-format-compatible CSV cell.

        DB2 LOAD DEL format requires:
        - BOOLEAN columns: 1 or 0 (Python bool → str 'True'/'False' is rejected)
        - DATE/DATETIME: ISO string ('2010-01-01') via str()
        - None → empty string (NULL)
        """
        if v is None:
            return ""
        if isinstance(v, bool):
            return 1 if v else 0
        return v

    with open(host_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for row in rows:
            writer.writerow([_db2_csv_val(v) for v in row])

    # Determine which path to pass to ADMIN_CMD (host path or container path)
    admin_cmd_path = host_path
    if container_name:
        container_path = f"/tmp/statschema_{table}.del"
        if _db2_container_copy(host_path, container_path, container_name):
            admin_cmd_path = container_path
            logger.debug("bulk_load_db2: copied staging file to container %s:%s", container_name, container_path)
        else:
            logger.warning("bulk_load_db2: container copy to %s failed; will attempt ADMIN_CMD with host path", container_name)

    try:
        cur.execute(
            f"CALL SYSPROC.ADMIN_CMD('LOAD FROM {admin_cmd_path} OF DEL INSERT INTO {full} NONRECOVERABLE')"
        )
        conn.commit()
        # Verify: ADMIN_CMD silently loads 0 rows when the server cannot find the
        # file (e.g. running in a container with no host-filesystem mount).
        cur.execute(f"SELECT COUNT(*) FROM {full}")
        actual = cur.fetchone()[0]
    except Exception as e:
        logger.warning("bulk_load_db2 ADMIN_CMD failed (%s); falling back to MULTI_ROW", e)
        actual = 0
    finally:
        try:
            os.unlink(host_path)
        except OSError:
            pass

    if actual >= count:
        logger.info("bulk_load_db2[ADMIN_CMD]: loaded %d rows into %s", count, full)
        return count

    # Fallback: parameterised multi-row insert (works in all environments).
    if actual > 0:
        # Partial load — truncate before refilling so we don't double-count.
        cur.execute(f"DELETE FROM {full}")
        conn.commit()

    logger.warning(
        "bulk_load_db2: ADMIN_CMD loaded %d/%d rows (server cannot see client /tmp); "
        "falling back to MULTI_ROW inserts",
        actual, count,
    )
    paramstyle = _detect_paramstyle(conn)
    cfg        = _DIALECT_BATCH_DEFAULTS.get("db2", BatchConfig())
    batch_size = _effective_batch_size(cfg, len(col_names))
    # DB2 DDL emitter creates tables with quoted uppercase names ("BRANCH").
    # Pass the uppercased name so _build_multi_row_sql quotes it correctly.
    inserted   = _insert_multi_row(
        cur, table.upper(), col_names, iter(rows), "db2", paramstyle, batch_size
    )
    conn.commit()
    logger.info("bulk_load_db2[MULTI_ROW]: loaded %d rows into %s", inserted, full)
    return inserted


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def load_dataframe(
    df: Any,
    conn: Any,
    table: str,
    dialect: str,
    *,
    strategy: LoadStrategy = LoadStrategy.MULTI_ROW,
    config: Optional[BatchConfig] = None,
    cols: Optional[list[str]] = None,
    staging_dir: Optional[str] = None,
    commit: bool = True,
) -> int:
    """
    Load a DataFrame into a live database table using one of three strategies.

    Parameters
    ----------
    df          Pandas DataFrame, PySpark DataFrame, or any iterable of tuples.
    conn        Live DBAPI2 connection to the target database.
    table       Target table name (must already exist).
    dialect     One of: "postgres", "mysql", "mariadb", "sqlserver", "oracle",
                "db2", "sqlite", "cockroachdb", "neon", "lakebase", "databricks".
    strategy    LoadStrategy.SINGLETON / MULTI_ROW (default) / BULK_COPY.
    config      BatchConfig override.  None = use _DIALECT_BATCH_DEFAULTS[dialect].
    cols        Column names in insert order.  None = infer from df.
    staging_dir Directory for temp CSV files used by BULK_COPY loaders.
    commit      Commit the transaction after all rows are inserted (default True).
                Set False when the caller manages the transaction.

    Returns
    -------
    int — number of rows inserted.
    """
    col_names  = _col_names(df, cols)
    cfg        = config or _DIALECT_BATCH_DEFAULTS.get(dialect, BatchConfig())
    paramstyle = _detect_paramstyle(conn)
    cur        = conn.cursor()

    if cfg.auto_discover and strategy == LoadStrategy.MULTI_ROW:
        batch_size = discover_max_batch_size(conn, table, col_names, dialect)
        logger.info(
            "load_dataframe: auto-discovered batch_size=%d for %s", batch_size, dialect
        )
    else:
        batch_size = _effective_batch_size(cfg, len(col_names))

    if dialect == "databricks":
        raise NotImplementedError(
            "load_dataframe() does not support dialect='databricks'. "
            "Databricks does not use a DBAPI2 connection for writes. "
            "Use df.write.saveAsTable('catalog.schema.table') or "
            "df.write.format('delta').save(path) on the PySpark DataFrame directly."
        )

    # oracledb connections expose connection.direct_path_load() — use it for Oracle.
    # Direct path writes bypass the buffer cache and are ~10x faster than INSERT ALL.
    # Data is implicitly committed; the same _coerce_oracle_val/_nan_to_none pipeline
    # that feeds INSERT ALL is applied here to handle time strings, NaN, etc.
    if type(conn).__module__.split(".")[0] == "oracledb":
        _qcur = conn.cursor()
        _qcur.execute(
            "SELECT SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') FROM dual"
        )
        schema_name = _qcur.fetchone()[0]

        # direct_path_load is strict: Python int/float are rejected for VARCHAR2/CHAR
        # columns.  Fetch column types and build a per-column coercion mask so
        # non-string Python scalars are converted to str before the load.
        _qcur.execute(
            "SELECT COLUMN_NAME, DATA_TYPE FROM ALL_TAB_COLUMNS "
            "WHERE OWNER = :o AND TABLE_NAME = :t ORDER BY COLUMN_ID",
            {"o": schema_name, "t": table.upper()},
        )
        _col_types = {row[0]: row[1] for row in _qcur.fetchall()}
        _varchar_types = {"CHAR", "VARCHAR2", "NCHAR", "NVARCHAR2"}
        _upper_cols = [c.upper() for c in col_names]
        _is_varchar = [_col_types.get(c, "") in _varchar_types for c in _upper_cols]

        def _coerce_oracle_dp(val: Any, varchar: bool) -> Any:
            val = _coerce_oracle_val(_nan_to_none(val))
            if varchar and val is not None and not isinstance(val, str):
                return str(val)
            return val

        coerced = [
            tuple(_coerce_oracle_dp(v, _is_varchar[i]) for i, v in enumerate(row))
            for row in _iter_rows(df)
        ]
        conn.direct_path_load(
            schema_name=schema_name,
            table_name=table.upper(),
            column_names=_upper_cols,
            data=coerced,
        )
        inserted = len(coerced)
        logger.info(
            "load_dataframe: oracledb direct_path_load %d rows into %s",
            inserted, table,
        )
        return inserted

    # mssql-python connections expose cursor.bulkcopy() — use it unconditionally
    # for SQL Server. This is 100x faster than parameterised MULTI_ROW inserts and
    # doesn't require server-side file access (unlike BULK INSERT from CSV).
    if type(conn).__module__.split(".")[0] == "mssql_python":
        import mssql_python as _mssql  # type: ignore
        # bulkcopy() creates its own internal connection using the stored
        # connection_str, which still points to the original DATABASE (e.g. master).
        # Query the current database and build a fresh connection string so the
        # bulkcopy table lookup resolves to the correct schema.
        _qcur = conn.cursor()
        _qcur.execute("SELECT DB_NAME()")
        current_db = _qcur.fetchone()[0]
        tmpl = getattr(conn, "_mssql_conn_template", None)
        if tmpl:
            bc_conn_str = tmpl.format(db=current_db)
        else:
            # Fallback: replace Database= in the stored conn string, stripping
            # reserved keywords (Driver=, APP=) that mssql_python.connect() rejects.
            import re as _re
            raw = conn.connection_str
            raw = _re.sub(r"(?i)Driver=[^;]+;?", "", raw)
            raw = _re.sub(r"(?i)APP=[^;]+;?", "", raw)
            bc_conn_str = _re.sub(r"(?i)Database=[^;]+", f"Database={current_db}", raw)
        bc_conn = _mssql.connect(bc_conn_str)
        bc_conn.setautocommit(True)
        bc_cur  = bc_conn.cursor()
        bc_result = bc_cur.bulkcopy(
            f"dbo.[{table}]",
            _iter_rows(df),
            column_mappings=col_names,
            table_lock=True,
            timeout=3600,
        )
        bc_conn.close()
        inserted = bc_result.get("rows_copied", 0)
        logger.info(
            "load_dataframe: mssql-python bulkcopy %d rows into %s in %.2fs",
            inserted, table, bc_result.get("elapsed_time", 0),
        )
        return inserted

    if strategy == LoadStrategy.SINGLETON:
        # Singleton streams: no materialisation — safe for any size table.
        inserted = _insert_singleton(
            cur, table, col_names, _iter_rows(df), dialect, paramstyle
        )

    elif strategy == LoadStrategy.MULTI_ROW:
        # Multi-row streams via itertools.islice — safe for 6 M-row TPC-H tables.
        inserted = _insert_multi_row(
            cur, table, col_names, _iter_rows(df), dialect, paramstyle, batch_size
        )

    elif strategy == LoadStrategy.BULK_COPY:
        if dialect in ("postgres", "cockroachdb", "neon", "lakebase"):
            inserted = bulk_load_postgres(conn, df, table, col_names)
        elif dialect in ("mysql", "mariadb"):
            inserted = bulk_load_mysql(conn, df, table, col_names, staging_dir=staging_dir)
        elif dialect == "sqlserver":
            inserted = bulk_load_sqlserver(
                conn, df, table, col_names, staging_dir=staging_dir
            )
        elif dialect == "db2":
            inserted = bulk_load_db2(
                conn, df, table, col_names, staging_dir=staging_dir
            )
        else:
            logger.warning(
                "BULK_COPY not implemented for dialect=%s; falling back to MULTI_ROW",
                dialect,
            )
            inserted = _insert_multi_row(
                cur, table, col_names, _iter_rows(df), dialect, paramstyle, batch_size
            )
    else:
        raise ValueError(f"Unknown strategy: {strategy}")

    if commit and strategy != LoadStrategy.BULK_COPY:
        conn.commit()

    logger.info(
        "load_dataframe: inserted %d rows into %s (dialect=%s strategy=%s)",
        inserted, table, dialect, strategy,
    )
    return inserted
