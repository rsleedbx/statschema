"""SQL Server 2019+ / Azure SQL / Synapse DDL emitter."""

from __future__ import annotations

from ...model import CanonicalTableSchema
from .._emitter_shared import col_ddl, primary_key_constraint, quote

DEFAULTS: dict[str, str] = {
    "integer":     "INT",
    "smallint":    "SMALLINT",
    "bigint":      "BIGINT",
    "long":        "BIGINT",
    "string":      "NVARCHAR(MAX)",
    "varchar":     "NVARCHAR(MAX)",
    "char":        "NCHAR(1)",
    "uuid":        "UNIQUEIDENTIFIER",
    "float":       "FLOAT",
    "double":      "FLOAT",
    "boolean":     "BIT",
    "timestamp":   "DATETIME2",
    "timestamptz": "DATETIMEOFFSET",
    "time":        "TIME",
    "timetz":      "TIME",
    "date":        "DATE",
    "decimal":     "DECIMAL(18,4)",
    "binary":      "VARBINARY(MAX)",
}

DIALECT = "sqlserver"


def emit_table(table: CanonicalTableSchema, if_not_exists: bool = True) -> str:
    tname = quote(table.name, DIALECT)
    lines = [col_ddl(c, DIALECT, DEFAULTS) for c in table.columns]
    pk    = primary_key_constraint(table, DIALECT)
    if pk:
        lines.append(pk)
    body  = ",\n".join(lines)
    ddl   = f"CREATE TABLE {tname} (\n{body}\n);"
    if if_not_exists:
        raw  = table.name.replace("'", "''")
        ddl  = (
            f"IF OBJECT_ID(N'{raw}', N'U') IS NULL\nBEGIN\n"
            + "  " + ddl.replace("\n", "\n  ").rstrip()
            + "\nEND;"
        )
    return ddl
