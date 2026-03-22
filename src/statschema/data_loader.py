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


def _iter_rows(df: Any) -> Iterator[tuple]:
    """Yield rows as tuples from a pandas DataFrame, PySpark DataFrame, or iterable."""
    module = type(df).__module__.split(".")[0]
    if module == "pandas":
        for row in df.itertuples(index=False, name=None):
            yield tuple(row)
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
            params[f"v{r}_{c}"] = val
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


def bulk_load_db2(  # pragma: no cover
    conn: Any,
    df: Any,
    table: str,
    col_names: list[str],
    schema: Optional[str] = None,
    staging_dir: Optional[str] = None,
) -> int:
    """
    Load df into IBM Db2 LUW via LOAD FROM … OF DEL FORMAT using ADMIN_CMD.

    Requires SYSADM or DBADM authority to execute LOAD via ADMIN_CMD.
    The staging file path must be accessible to the Db2 server process.
    """
    cur = conn.cursor()
    if schema is None:
        cur.execute("VALUES CURRENT SCHEMA")
        row = cur.fetchone()
        schema = (row[0] if row else "").strip().upper()

    full = f'"{schema}"."{table.upper()}"'
    path = os.path.join(
        staging_dir or tempfile.gettempdir(), f"statschema_{table}.del"
    )

    count = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for row in _iter_rows(df):
            writer.writerow(["" if v is None else v for v in row])
            count += 1

    cur.execute(
        f"CALL SYSPROC.ADMIN_CMD('LOAD FROM {path} OF DEL INSERT INTO {full} NONRECOVERABLE')"
    )
    conn.commit()
    os.unlink(path)
    logger.info("bulk_load_db2: loaded %d rows into %s.%s", schema, table, count)
    return count


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
                "db2", "sqlite", "cockroachdb", "neon", "databricks".
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
        if dialect in ("postgres", "cockroachdb", "neon"):
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
