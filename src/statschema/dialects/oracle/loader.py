"""Oracle bulk-load loaders (direct_path_load + multi-row INSERT fallback)."""

from __future__ import annotations

import logging
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
    Works in all topologies (remote, collocated, embedded) — direct_path_load
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
