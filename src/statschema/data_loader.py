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
"""

from __future__ import annotations

import logging
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Re-export shared infrastructure for backward compat
# ---------------------------------------------------------------------------

from .dialects._loader_shared import (
    LoadStrategy,
    BatchConfig,
    _DIALECT_BATCH_DEFAULTS,
    _detect_paramstyle,
    _quote_id,
    _placeholder,
    _effective_batch_size,
    _nan_to_none,
    _iter_rows,
    _col_names,
    _build_multi_row_sql,
    _coerce_oracle_val,
    _coerce_db2_val,
    _coerce_db2_row,
    _build_oracle_all_sql,
    _insert_singleton,
    _insert_multi_row,
    discover_max_batch_size,
)

# Per-dialect bulk loaders
from .dialects.postgres.loader   import bulk_load_postgres
from .dialects.mysql.loader      import bulk_load_mysql
from .dialects.sqlserver.loader  import bulk_load_sqlserver
from .dialects.db2.loader        import (
    bulk_load_db2, _db2_container_copy,
    DB2AdminCmdLoader, DB2ImportLoader, DB2MultiRowLoader,
)
from .dialects.oracle.loader     import OracleDirectPathLoader, OracleMultiRowLoader

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Phase 1: topology-aware loader registry
# ---------------------------------------------------------------------------

_LOADER_REGISTRY: dict[str, list] = {
    "db2": [
        DB2AdminCmdLoader(),
        DB2ImportLoader(),
        DB2MultiRowLoader(),
    ],
    "oracle": [
        OracleDirectPathLoader(),
        OracleMultiRowLoader(),
    ],
}


def _select_loader(dialect: str, ctx: Any, col_types: list[str] | None):
    """
    Return the first loader in the registry that accepts the given dialect
    and DeploymentContext.  Raises RuntimeError if none matches.
    """
    loaders = _LOADER_REGISTRY.get(dialect, [])
    for loader in loaders:
        if loader.can_use(ctx, dialect, col_types):
            return loader
    raise RuntimeError(
        f"No topology-aware loader found for dialect={dialect!r}. "
        f"ctx.topology={getattr(ctx, 'topology', '?')!r}, "
        f"col_types_sample={col_types[:3] if col_types else None}"
    )


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
    col_types: Optional[list[str]] = None,
    staging_dir: Optional[str] = None,
    commit: bool = True,
    ctx: "Optional[Any]" = None,
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

    # --- Topology-aware registry dispatch (Phase 1) ---
    if ctx is not None and dialect in _LOADER_REGISTRY:
        loader = _select_loader(dialect, ctx, col_types)
        col_names = _col_names(df, cols)
        return loader.bulk_load(ctx, conn, df, table, col_names)

    if dialect == "databricks":
        raise NotImplementedError(
            "load_dataframe() does not support dialect='databricks'. "
            "Databricks does not use a DBAPI2 connection for writes. "
            "Use df.write.saveAsTable('catalog.schema.table') or "
            "df.write.format('delta').save(path) on the PySpark DataFrame directly."
        )

    if type(conn).__module__.split(".")[0] == "oracledb":
        _qcur = conn.cursor()
        _qcur.execute(
            "SELECT SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') FROM dual"
        )
        schema_name = _qcur.fetchone()[0]

        _qcur.execute(
            "SELECT COLUMN_NAME, DATA_TYPE FROM ALL_TAB_COLUMNS "
            "WHERE OWNER = :o AND TABLE_NAME = :t ORDER BY COLUMN_ID",
            {"o": schema_name, "t": table.upper()},
        )
        _col_types_map = {row[0]: row[1] for row in _qcur.fetchall()}
        # Include CLOB / NCLOB so that synthetic int values generated for
        # string columns are coerced to str before direct_path_load.
        # Oracle emits CLOB for canonical type="string" (unbounded text).
        _varchar_types = {"CHAR", "VARCHAR2", "NCHAR", "NVARCHAR2", "CLOB", "NCLOB", "LONG"}
        # NUMBER-family types: string MCV values from the Oracle stats collector
        # must be converted to float so direct_path_load binds them correctly.
        _number_types = {"NUMBER", "FLOAT", "BINARY_FLOAT", "BINARY_DOUBLE",
                         "INTEGER", "SMALLINT", "INT"}
        _upper_cols = [c.upper() for c in col_names]

        def _col_kind(col_name: str) -> tuple[bool, bool]:
            ddl_type = _col_types_map.get(col_name, "")
            # DATA_TYPE for NUMBER(p,s) includes the precision/scale suffix;
            # strip it so "NUMBER(18,4)" → "NUMBER" still matches.
            base = ddl_type.split("(")[0].strip()
            return base in _varchar_types, base in _number_types

        _col_kinds = [_col_kind(c) for c in _upper_cols]

        def _coerce_oracle_dp(val: Any, varchar: bool, numeric: bool) -> Any:
            val = _coerce_oracle_val(_nan_to_none(val))
            if varchar and val is not None and not isinstance(val, str):
                return str(val)
            # Oracle stats collector returns MCV values as strings; convert to
            # float so direct_path_load can bind them to NUMBER columns.
            if numeric and isinstance(val, str) and val is not None:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    pass
            return val

        coerced = [
            tuple(
                _coerce_oracle_dp(v, _col_kinds[i][0], _col_kinds[i][1])
                for i, v in enumerate(row)
            )
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

    if type(conn).__module__.split(".")[0] == "mssql_python":
        import mssql_python as _mssql  # type: ignore
        _qcur = conn.cursor()
        _qcur.execute("SELECT DB_NAME()")
        current_db = _qcur.fetchone()[0]
        tmpl = getattr(conn, "_mssql_conn_template", None)
        if tmpl:
            bc_conn_str = tmpl.format(db=current_db)
        else:
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
        inserted = _insert_singleton(
            cur, table, col_names, _iter_rows(df), dialect, paramstyle
        )

    elif strategy == LoadStrategy.MULTI_ROW:
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
                conn, df, table, col_names, staging_dir=staging_dir, col_types=col_types
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
