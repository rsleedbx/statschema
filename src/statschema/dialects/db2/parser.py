"""db2 DDL parser — thin wrapper over the shared sqlglot-based parser."""

from __future__ import annotations

from pathlib import Path

from .._parser_shared import parse_ddl, parse_ddl_file
from ...model import CanonicalTableSchema

DIALECT = "db2"


def parse(sql: str) -> list[CanonicalTableSchema]:
    """Parse db2 DDL SQL into canonical table schemas."""
    return parse_ddl(sql, dialect=DIALECT)


def parse_file(path: str | Path) -> list[CanonicalTableSchema]:
    """Parse a db2 .sql file into canonical table schemas."""
    return parse_ddl_file(path, dialect=DIALECT)
