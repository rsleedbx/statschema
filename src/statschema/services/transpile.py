"""
Service: transpile

Parse source DDL into the canonical intermediate representation and emit
target DDL.  All callers go through this module rather than calling
``ddl_parser`` or ``ddl_emitter`` directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..model import CanonicalTableSchema
from ..ddl_parser import parse_ddl, parse_ddl_file
from ..ddl_emitter import emit_ddl, emit_ddl_all
from ..schema_io import dump_schema, load_canonical


def parse(
    sql: str,
    dialect: str | None = None,
) -> list[CanonicalTableSchema]:
    """Parse DDL SQL into a list of canonical table schemas.

    Parameters
    ----------
    sql:
        DDL SQL string containing one or more ``CREATE TABLE`` statements.
    dialect:
        Source dialect hint (``"mysql"``, ``"postgres"``, ``"sqlserver"``,
        ``"oracle"``, ``"db2"``, ``"databricks"``).  ``None`` → auto-detect.

    Returns
    -------
    list[CanonicalTableSchema]
        One entry per table found in the SQL.
    """
    return parse_ddl(sql, dialect=dialect)


def parse_file(
    path: str | Path,
    dialect: str | None = None,
) -> list[CanonicalTableSchema]:
    """Parse DDL from a ``.sql`` file into canonical table schemas.

    Parameters
    ----------
    path:
        Path to the ``.sql`` file.
    dialect:
        Source dialect hint.  ``None`` → auto-detect.
    """
    return parse_ddl_file(path, dialect=dialect)


def emit(
    table: CanonicalTableSchema,
    dialect: str,
    if_not_exists: bool = True,
) -> str:
    """Emit a single ``CREATE TABLE`` statement for the target dialect.

    Parameters
    ----------
    table:
        Canonical table schema.
    dialect:
        Target dialect (e.g. ``"postgres"``, ``"mysql"``, ``"databricks"``).
    if_not_exists:
        Prefix the statement with ``IF NOT EXISTS``.
    """
    return emit_ddl(table, dialect, if_not_exists=if_not_exists)


def emit_all(
    tables: list[CanonicalTableSchema],
    dialect: str,
    if_not_exists: bool = True,
) -> str:
    """Emit ``CREATE TABLE`` statements for all tables in FK dependency order.

    Parameters
    ----------
    tables:
        List of canonical table schemas.
    dialect:
        Target dialect.
    if_not_exists:
        Prefix each statement with ``IF NOT EXISTS``.
    """
    return emit_ddl_all(tables, dialect, if_not_exists=if_not_exists)


def transpile(
    sql: str,
    source_dialect: str | None,
    target_dialect: str,
    if_not_exists: bool = True,
) -> str:
    """Parse source DDL and emit target DDL in one call.

    Parameters
    ----------
    sql:
        Source DDL SQL string.
    source_dialect:
        Source dialect hint.  ``None`` → auto-detect.
    target_dialect:
        Target dialect to emit.
    if_not_exists:
        Prefix ``CREATE TABLE`` statements with ``IF NOT EXISTS``.

    Returns
    -------
    str
        Target DDL with all type mappings applied.
    """
    tables = parse_ddl(sql, dialect=source_dialect)
    return emit_ddl_all(tables, target_dialect, if_not_exists=if_not_exists)
