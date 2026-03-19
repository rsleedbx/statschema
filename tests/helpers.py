"""
Shared test utilities for DDL and statistics round-trip tests.

Import in any test file:
    from tests.helpers import make_col, make_table, ddl_roundtrip, assert_ddl_roundtrip
"""

from __future__ import annotations

from typing import Optional

from src.schema_parser import (
    CanonicalColumn,
    CanonicalTableSchema,
    GenerationRule,
    emit_ddl,
    parse_ddl,
)


# ---------------------------------------------------------------------------
# Schema builder helpers
# ---------------------------------------------------------------------------

def make_col(
    name: str,
    ctype: str,
    *,
    length: Optional[int] = None,
    precision: Optional[int] = None,
    scale: Optional[int] = None,
    not_null: bool = False,
    auto_increment: bool = False,
    default: Optional[str] = None,
    primary_key: bool = False,
    unique: bool = False,
) -> CanonicalColumn:
    """
    Construct a CanonicalColumn with the given attributes.

    Examples
    --------
    >>> make_col("id", "integer", primary_key=True, not_null=True, auto_increment=True)
    >>> make_col("price", "decimal", precision=10, scale=2, not_null=True)
    >>> make_col("name", "string", length=255, default="'unknown'")
    """
    return CanonicalColumn(
        name=name,
        type=ctype,
        length=length,
        precision=precision,
        scale=scale,
        not_null=not_null,
        auto_increment=auto_increment,
        default=default,
        primary_key=primary_key,
        unique=unique,
        generation=GenerationRule(unique=True) if primary_key else None,
    )


def make_table(name: str, *columns: CanonicalColumn) -> CanonicalTableSchema:
    """
    Construct a CanonicalTableSchema from positional column args.

    Examples
    --------
    >>> make_table("orders",
    ...     make_col("id", "integer", primary_key=True, not_null=True),
    ...     make_col("total", "decimal", precision=10, scale=2),
    ... )
    """
    return CanonicalTableSchema(name=name, columns=list(columns))


def make_pk_table(
    table_name: str,
    *extra_cols: CanonicalColumn,
    pk_type: str = "integer",
) -> CanonicalTableSchema:
    """
    Convenience: table with a single auto-increment PK column plus extra cols.

    >>> make_pk_table("t", make_col("v", "string", length=100))
    """
    pk = make_col("id", pk_type, primary_key=True, not_null=True, auto_increment=True)
    return CanonicalTableSchema(name=table_name, columns=[pk, *extra_cols])


# ---------------------------------------------------------------------------
# Round-trip helpers
# ---------------------------------------------------------------------------

def ddl_roundtrip(
    table: CanonicalTableSchema,
    emit_dialect: str,
    parse_dialect: str,
) -> tuple[str, str]:
    """
    Perform one DDL round-trip: emit → parse → emit.

    Returns
    -------
    (ddl1, ddl2)
        ddl1 = emit_ddl(table, emit_dialect)
        ddl2 = emit_ddl(parse_ddl(ddl1, parse_dialect)[0], emit_dialect)

    The two strings must be identical for the round-trip to be idempotent.
    ``if_not_exists=False`` is used to avoid SQL Server / Oracle PL/SQL wrapping
    that the parser cannot re-ingest.
    """
    ddl1 = emit_ddl(table, emit_dialect, if_not_exists=False)
    parsed = parse_ddl(ddl1, parse_dialect)
    if not parsed:
        raise AssertionError(
            f"parse_ddl returned empty list for {parse_dialect!r}:\n{ddl1}"
        )
    ddl2 = emit_ddl(parsed[0], emit_dialect, if_not_exists=False)
    return ddl1, ddl2


def assert_ddl_roundtrip(
    table: CanonicalTableSchema,
    emit_dialect: str,
    parse_dialect: Optional[str] = None,
) -> str:
    """
    Assert that emit → parse → emit is byte-for-byte idempotent.

    Returns ddl1 for further inspection.
    """
    pd = parse_dialect or emit_dialect
    ddl1, ddl2 = ddl_roundtrip(table, emit_dialect, pd)
    assert ddl1 == ddl2, (
        f"DDL round-trip mismatch [{emit_dialect} / parse:{pd}]:\n"
        f"── First emit ──\n{ddl1}\n"
        f"── Second emit ──\n{ddl2}"
    )
    return ddl1


def parse_first_roundtrip(
    raw_ddl: str,
    dialect: str,
) -> tuple[str, str]:
    """
    Parse a raw DDL string then do the idempotent emit → parse → emit check.

    Returns
    -------
    (ddl1, ddl2)
        ddl1 = emit_ddl(parse(raw_ddl), dialect)
        ddl2 = emit_ddl(parse(ddl1), dialect)
    """
    tables = parse_ddl(raw_ddl, dialect)
    if not tables:
        raise AssertionError(f"parse_ddl returned empty list for {dialect!r}:\n{raw_ddl}")
    return ddl_roundtrip(tables[0], dialect, dialect)


def assert_parse_first_roundtrip(raw_ddl: str, dialect: str) -> None:
    """
    Parse raw DDL, then assert the subsequent emit → parse → emit is idempotent.
    """
    ddl1, ddl2 = parse_first_roundtrip(raw_ddl, dialect)
    assert ddl1 == ddl2, (
        f"Parse-first round-trip mismatch [{dialect}]:\n"
        f"── Input DDL ──\n{raw_ddl}\n"
        f"── First emit ──\n{ddl1}\n"
        f"── Second emit ──\n{ddl2}"
    )


def parse_col_type(raw_ddl: str, dialect: str, col_index: int = 1) -> str:
    """
    Parse a DDL string and return the canonical type of the column at *col_index*.

    Column 0 is assumed to be the PK. Default col_index=1 is the first data column.
    """
    tables = parse_ddl(raw_ddl, dialect)
    assert tables, f"parse_ddl returned empty list for {dialect!r}"
    return tables[0].columns[col_index].type
