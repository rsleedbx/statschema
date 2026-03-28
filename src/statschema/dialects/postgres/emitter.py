"""PostgreSQL / Aurora PostgreSQL / Neon DDL emitter."""

from __future__ import annotations

from ...model import CanonicalTableSchema
from .._emitter_shared import col_ddl, primary_key_constraint, quote

DEFAULTS: dict[str, str] = {
    "integer":     "INTEGER",
    "smallint":    "SMALLINT",
    "bigint":      "BIGINT",
    "long":        "BIGINT",
    "string":      "TEXT",
    "varchar":     "TEXT",
    "char":        "CHAR(1)",
    "uuid":        "UUID",
    "float":       "REAL",
    "double":      "DOUBLE PRECISION",
    "boolean":     "BOOLEAN",
    "timestamp":   "TIMESTAMP",
    "timestamptz": "TIMESTAMP WITH TIME ZONE",
    "time":        "TIME",
    "timetz":      "TIME WITH TIME ZONE",
    "date":        "DATE",
    "decimal":     "NUMERIC(18,4)",
    "binary":      "BYTEA",
}

DIALECT = "postgres"


def emit_table(table: CanonicalTableSchema, if_not_exists: bool = True) -> str:
    ine   = " IF NOT EXISTS" if if_not_exists else ""
    tname = quote(table.name, DIALECT)
    lines = [col_ddl(c, DIALECT, DEFAULTS) for c in table.columns]
    pk    = primary_key_constraint(table, DIALECT)
    if pk:
        lines.append(pk)
    body  = ",\n".join(lines)
    return f"CREATE TABLE{ine} {tname} (\n{body}\n);"
