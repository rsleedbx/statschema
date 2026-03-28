"""Databricks SQL / Unity Catalog Delta DDL emitter."""

from __future__ import annotations

from ...model import CanonicalTableSchema
from .._emitter_shared import col_ddl, primary_key_constraint, quote

DEFAULTS: dict[str, str] = {
    "integer":     "INT",
    "smallint":    "SMALLINT",
    "bigint":      "BIGINT",
    "long":        "BIGINT",
    "string":      "STRING",
    "varchar":     "STRING",
    "char":        "CHAR(1)",
    "uuid":        "STRING",
    "float":       "FLOAT",
    "double":      "DOUBLE",
    "boolean":     "BOOLEAN",
    "timestamp":   "TIMESTAMP_NTZ",
    "timestamptz": "TIMESTAMP",
    "time":        "STRING",
    "timetz":      "STRING",
    "date":        "DATE",
    "decimal":     "DECIMAL(18,4)",
    "binary":      "BINARY",
}

DIALECT = "databricks"


def emit_table(table: CanonicalTableSchema, if_not_exists: bool = True) -> str:
    ine   = " IF NOT EXISTS" if if_not_exists else ""
    tname = quote(table.name, DIALECT)
    lines = [col_ddl(c, DIALECT, DEFAULTS) for c in table.columns]
    pk    = primary_key_constraint(table, DIALECT)
    if pk:
        lines.append(pk)
    body  = ",\n".join(lines)
    return f"CREATE TABLE{ine} {tname} (\n{body}\n)\nUSING DELTA;"
