"""IBM Db2 LUW 11.1+ DDL emitter."""

from __future__ import annotations

from ...model import CanonicalTableSchema
from .._emitter_shared import col_ddl, primary_key_constraint, quote

DEFAULTS: dict[str, str] = {
    "integer":     "INTEGER",
    "long":        "BIGINT",
    "string":      "CLOB",
    "uuid":        "CHAR(36)",
    "float":       "REAL",
    "double":      "DOUBLE",
    "boolean":     "BOOLEAN",
    "timestamp":   "TIMESTAMP",
    "timestamptz": "TIMESTAMP WITH TIME ZONE",
    "time":        "TIME",
    "timetz":      "TIME",
    "date":        "DATE",
    "decimal":     "DECIMAL(18,4)",
    "binary":      "BLOB",
}

DIALECT = "db2"


def emit_table(table: CanonicalTableSchema, if_not_exists: bool = True) -> str:
    tname = quote(table.name.upper(), DIALECT)
    lines = [col_ddl(c, DIALECT, DEFAULTS) for c in table.columns]
    pk    = primary_key_constraint(table, DIALECT)
    if pk:
        lines.append(pk)
    body  = ",\n".join(lines)
    if if_not_exists:
        return f"CREATE TABLE IF NOT EXISTS {tname} (\n{body}\n);"
    return f"CREATE TABLE {tname} (\n{body}\n);"
