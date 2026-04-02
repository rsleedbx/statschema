"""Oracle 19c+ DDL emitter."""

from __future__ import annotations

from ...model import CanonicalTableSchema
from .._emitter_shared import col_ddl, primary_key_constraint, quote

DEFAULTS: dict[str, str] = {
    "integer":     "NUMBER(10)",
    "smallint":    "NUMBER(5)",
    "bigint":      "NUMBER(19)",
    "long":        "NUMBER(19)",
    "string":      "CLOB",
    "varchar":     "VARCHAR2(4000)",
    "char":        "CHAR(1)",
    "uuid":        "CHAR(36)",
    "float":       "FLOAT",
    "double":      "FLOAT(53)",
    "boolean":     "NUMBER(1)",
    "timestamp":   "TIMESTAMP",
    "timestamptz": "TIMESTAMP WITH TIME ZONE",
    "time":        "TIMESTAMP",
    "timetz":      "TIMESTAMP WITH TIME ZONE",
    "date":        "DATE",
    "decimal":     "NUMBER(18,4)",
    "binary":      "BLOB",
}

DIALECT = "oracle"


def emit_table(table: CanonicalTableSchema, if_not_exists: bool = True) -> str:
    tname = quote(table.name, DIALECT)
    lines = [col_ddl(c, DIALECT, DEFAULTS) for c in table.columns]
    pk    = primary_key_constraint(table, DIALECT)
    if pk:
        lines.append(pk)
    body  = ",\n".join(lines)
    ddl   = f"CREATE TABLE {tname} (\n{body}\n);"
    if if_not_exists:
        escaped = ddl.replace("'", "''").rstrip(";")
        ddl = (
            f"BEGIN\n"
            f"  EXECUTE IMMEDIATE '{escaped}';\n"
            f"EXCEPTION\n"
            f"  WHEN OTHERS THEN\n"
            f"    IF SQLCODE != -955 THEN RAISE; END IF; -- ORA-00955: table already exists\n"
            f"END;"
        )
    return ddl
