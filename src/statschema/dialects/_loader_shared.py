"""
Shared infrastructure for dialect bulk-copy loaders.

All dialect loader modules import their shared helpers from here.
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


_DIALECT_BATCH_DEFAULTS: dict[str, BatchConfig] = {
    "mysql":       BatchConfig(max_rows=1000, max_params=65535),
    "mariadb":     BatchConfig(max_rows=1000, max_params=65535),
    "postgres":    BatchConfig(max_rows=1000, max_params=65535),
    "cockroachdb": BatchConfig(max_rows=1000, max_params=65535),
    "neon":        BatchConfig(max_rows=1000, max_params=65535),
    "sqlserver":   BatchConfig(max_rows=1000, max_params=2100),
    "oracle":      BatchConfig(max_rows=500,  max_params=65535),
    "db2":         BatchConfig(max_rows=1000, max_params=32767),
    "databricks":  BatchConfig(max_rows=1000, max_params=65535),
    "sqlite":      BatchConfig(max_rows=1000, max_params=32766),
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _detect_paramstyle(conn: Any) -> str:
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
    """Quote a SQL identifier, handling schema-qualified names (schema.table)."""
    if "." in name:
        schema, ident = name.split(".", 1)
        return f"{_quote_id(schema, dialect)}.{_quote_id(ident, dialect)}"
    if dialect == "sqlserver":
        return f"[{name}]"
    if dialect in ("mysql", "mariadb", "databricks"):
        return f"`{name}`"
    return f'"{name}"'


def _placeholder(paramstyle: str, index: int) -> str:
    if paramstyle == "qmark":
        return "?"
    if paramstyle in ("numeric", "named"):
        return f":{index + 1}"
    return "%s"


def _effective_batch_size(config: BatchConfig, n_cols: int) -> int:
    if n_cols == 0:
        return config.max_rows
    param_limited = max(1, config.max_params // n_cols)
    return min(config.max_rows, param_limited)


def _nan_to_none(val: Any) -> Any:
    try:
        import math
        if math.isnan(val) or math.isinf(val):
            return None
    except (TypeError, ValueError):
        pass
    return val


def _iter_rows(df: Any) -> Iterator[tuple]:
    module = type(df).__module__.split(".")[0]
    if module == "pandas":
        for row in df.itertuples(index=False, name=None):
            yield tuple(_nan_to_none(v) for v in row)
    elif module == "pyspark":
        for row in df.toLocalIterator():
            yield tuple(row)
    else:
        for row in df:
            if isinstance(row, dict):
                yield tuple(row.values())
            else:
                yield tuple(row)


def _col_names(df: Any, explicit_cols: Optional[list[str]]) -> list[str]:
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


def _coerce_db2_val(val: Any) -> Any:
    if val is None:
        return val
    if isinstance(val, bool):
        return int(val)
    try:
        import pandas as _pd
        if isinstance(val, _pd.Timestamp):
            return val.date() if val.hour == 0 and val.minute == 0 and val.second == 0 else val.to_pydatetime()
        if isinstance(val, _pd.NA.__class__):
            return None
    except Exception:
        pass
    try:
        import numpy as _np
        if isinstance(val, _np.integer):
            return int(val)
        if isinstance(val, _np.floating):
            return float(val)
        if isinstance(val, _np.bool_):
            return int(val)
    except Exception:
        pass
    return val


# "string" is the canonical YAML type; "clob"/"varchar" are the DB2 DDL types.
# Both are included so MULTI_ROW coercion works whether col_types carries
# canonical names (from identity_test.py) or resolved DDL type names.
_DB2_TEXT_TYPES = frozenset({
    "string", "varchar", "char", "clob", "nclob", "blob",
    "text", "nchar", "nvarchar",
})


def _coerce_db2_row(row: tuple, col_types: list[str]) -> tuple:
    result = []
    for val, ctype in zip(row, col_types):
        v = _coerce_db2_val(val)
        if v is not None and not isinstance(v, str) and ctype.lower() in _DB2_TEXT_TYPES:
            v = str(v)
        result.append(v)
    return tuple(result)


def _build_oracle_all_sql(
    table: str,
    col_names: list[str],
    batch: list[tuple],
) -> tuple[str, dict]:
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


def discover_max_batch_size(
    conn: Any,
    table: str,
    col_names: list[str],
    dialect: str,
    *,
    min_rows: int = 1,
    start_max: int | None = None,
) -> int:
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
