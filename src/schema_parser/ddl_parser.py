"""
Parse SQL DDL (CREATE TABLE) from database schema-only dumps into canonical model.

Sources:
  - MySQL: mysqldump --no-data (or -d). Output is SQL with CREATE TABLE `tbl` ( `col` type ... );
    Ref: https://dev.mysql.com/doc/refman/8.4/en/mysqldump.html
  - PostgreSQL: pg_dump -s (--schema-only). Output is SQL with CREATE TABLE [schema.]tbl ( col type ... );
    Ref: https://www.postgresql.org/docs/current/app-pgdump.html
  - SQL Server: mssql-scripter (or SSMS Generate Scripts). Output is T-SQL with CREATE TABLE [schema].[tbl] ( [col] type ... );
    Ref: https://github.com/microsoft/mssql-scripter

All produce plain-text SQL; this module extracts table and column definitions and maps
dialect-specific types to the canonical schema (integer, long, string, float, double, boolean, timestamp).
"""

import re
from pathlib import Path
from typing import Any

from .model import CanonicalColumn, CanonicalTableSchema, GenerationRule


# Dialect-specific type name -> canonical (lowercase)
MYSQL_TYPE_TO_CANONICAL = {
    "tinyint": "integer",
    "smallint": "integer",
    "mediumint": "integer",
    "int": "integer",
    "integer": "integer",
    "bigint": "long",
    "decimal": "double",
    "numeric": "double",
    "float": "float",
    "double": "double",
    "real": "double",
    "bit": "boolean",
    "char": "string",
    "varchar": "string",
    "binary": "string",
    "varbinary": "string",
    "tinytext": "string",
    "text": "string",
    "mediumtext": "string",
    "longtext": "string",
    "json": "string",
    "date": "timestamp",
    "datetime": "timestamp",
    "timestamp": "timestamp",
    "time": "string",
    "year": "integer",
    "enum": "string",
    "set": "string",
}

POSTGRES_TYPE_TO_CANONICAL = {
    "smallint": "integer",
    "integer": "integer",
    "int": "integer",
    "int2": "integer",
    "int4": "integer",
    "bigint": "long",
    "int8": "long",
    "decimal": "double",
    "numeric": "double",
    "real": "float",
    "float4": "float",
    "double precision": "double",
    "float8": "double",
    "boolean": "boolean",
    "bool": "boolean",
    "character varying": "string",
    "varchar": "string",
    "character": "string",
    "char": "string",
    "text": "string",
    "json": "string",
    "jsonb": "string",
    "date": "timestamp",
    "timestamp": "timestamp",
    "timestamp without time zone": "timestamp",
    "timestamp with time zone": "timestamp",
    "timestamptz": "timestamp",
    "time": "string",
    "interval": "string",
    "uuid": "string",
    "serial": "integer",
    "bigserial": "long",
}

SQLSERVER_TYPE_TO_CANONICAL = {
    "tinyint": "integer",
    "smallint": "integer",
    "int": "integer",
    "bigint": "long",
    "decimal": "double",
    "numeric": "double",
    "float": "float",
    "real": "float",
    "bit": "boolean",
    "char": "string",
    "varchar": "string",
    "nchar": "string",
    "nvarchar": "string",
    "text": "string",
    "ntext": "string",
    "date": "timestamp",
    "datetime": "timestamp",
    "datetime2": "timestamp",
    "smalldatetime": "timestamp",
    "datetimeoffset": "timestamp",
    "time": "string",
    "uniqueidentifier": "string",
}


def _normalize_identifier(s: str, dialect: str) -> str:
    """Strip quotes/backticks/brackets from identifier."""
    s = s.strip()
    if dialect == "mysql" and s.startswith("`") and s.endswith("`"):
        return s[1:-1].strip()
    if dialect == "sqlserver" and s.startswith("[") and s.endswith("]"):
        return s[1:-1].strip()
    if s.startswith('"') and s.endswith('"'):
        return s[1:-1].strip()
    return s


def _map_type_to_canonical(raw_type: str, dialect: str) -> str:
    """Map dialect-specific type to canonical (integer, long, string, float, double, boolean, timestamp)."""
    raw = raw_type.lower().strip()
    # Strip optional length/precision (e.g. varchar(255) -> varchar, int(11) -> int)
    base_full = re.sub(r"\([^)]*\)", "", raw).strip()
    base = base_full.split()[0] if base_full else raw

    if dialect == "mysql":
        m = MYSQL_TYPE_TO_CANONICAL
    elif dialect == "postgres":
        m = POSTGRES_TYPE_TO_CANONICAL
        if "character varying" in base_full or "varchar" in base_full:
            base = "varchar" if "varchar" in base_full else "character varying"
        elif "double precision" in base_full or base_full == "double precision":
            base = "double precision"
        elif "timestamp with time zone" in base_full or "timestamptz" in base_full:
            base = "timestamp"
        elif "timestamp without time zone" in base_full:
            base = "timestamp"
    elif dialect == "sqlserver":
        m = SQLSERVER_TYPE_TO_CANONICAL
    else:
        m = MYSQL_TYPE_TO_CANONICAL  # fallback

    return m.get(base_full, m.get(base, m.get(raw, "string")))


def _detect_dialect(sql: str) -> str:
    """Heuristic: detect mysql, postgres, or sqlserver from SQL text."""
    sql_lower = sql[: 4000].lower()
    if "character varying" in sql_lower or "timestamp with time zone" in sql_lower or "serial" in sql_lower:
        return "postgres"
    if "[dbo]" in sql_lower or "nvarchar" in sql_lower or "nchar" in sql_lower or "uniqueidentifier" in sql_lower:
        return "sqlserver"
    if "`" in sql_lower or "auto_increment" in sql_lower or "engine=" in sql_lower:
        return "mysql"
    if "integer" in sql_lower or "bigint" in sql_lower:
        return "postgres"  # default to postgres for generic integer/bigint
    return "mysql"


def _find_create_table_blocks(sql: str) -> list[tuple[str, str]]:
    """Return list of (table_name, body) for each CREATE TABLE in sql. Body is text inside first ( )."""
    blocks: list[tuple[str, str]] = []
    pos = 0
    create_re = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?", re.IGNORECASE)
    while True:
        m = create_re.search(sql, pos)
        if not m:
            break
        start = m.end()
        # Table name: identifier (possibly quoted) until ( or space+(
        name_match = re.match(r"\s*([^\s(]+)\s*\(", sql[start:])
        if not name_match:
            pos = start + 1
            continue
        name_part = name_match.group(1).strip()
        paren_start = start + name_match.end() - 1  # index of opening ( in sql
        # Find matching closing paren
        depth = 1
        i = paren_start + 1
        while i < len(sql):
            if sql[i] == "(":
                depth += 1
            elif sql[i] == ")":
                depth -= 1
                if depth == 0:
                    body = sql[paren_start + 1 : i].strip()
                    # Normalize table name
                    name_part = re.sub(r"^[\w.]*\.", "", name_part)
                    name_part = re.sub(r"^\[[^\]]+\]\.\[", "[", name_part)
                    name_part = name_part.strip("`[]\"")
                    if name_part and body:
                        blocks.append((name_part, body))
                    pos = i + 1
                    break
            i += 1
        else:
            pos = start + 1
    return blocks


def _split_body_lines(body: str) -> list[str]:
    """Split CREATE TABLE body into lines; when multiple columns are on one line (comma-separated), split by comma respecting parens."""
    segments: list[str] = []
    depth = 0
    start = 0
    for i, c in enumerate(body):
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "," and depth == 0:
            segments.append(body[start:i].strip())
            start = i + 1
    if start < len(body):
        segments.append(body[start:].strip())
    return [s for s in segments if s and not s.startswith("--") and not s.startswith("/*")]


def _parse_table_body(body: str, dialect: str, table_name: str) -> list[CanonicalColumn]:
    """Parse column and constraint lines from CREATE TABLE body. Returns columns with primary_key set."""
    lines = _split_body_lines(body)

    primary_key_columns: set[str] = set()
    columns: list[CanonicalColumn] = []

    for line in lines:
        # PRIMARY KEY (col) or PRIMARY KEY (`col`) or CONSTRAINT ... PRIMARY KEY (col)
        pk_match = re.search(
            r"PRIMARY\s+KEY\s*\(\s*([^)]+)\s*\)",
            line,
            re.IGNORECASE,
        )
        if pk_match:
            keys_str = pk_match.group(1).strip()
            for part in re.split(r"\s*,\s*", keys_str):
                col = _normalize_identifier(part.strip(), dialect)
                primary_key_columns.add(col)
            continue

        # Column definition: identifier type [NOT NULL] [DEFAULT ...] [AUTO_INCREMENT] [PRIMARY KEY]
        # Try single-word type first so "INT NOT NULL PRIMARY KEY" parses correctly; else multi-word (character varying, double precision)
        col_match = re.match(
            r"^([^\s]+)\s+([\w]+(?:\s*\([^)]*\))?)\s*(.*)$",
            line,
            re.IGNORECASE,
        )
        if col_match:
            rest_check = (col_match.group(3) or "").strip().upper()
            # If rest looks like more type (e.g. "varying(50)") try multi-word type
            if rest_check and not rest_check.startswith(("NOT NULL", "NULL", "DEFAULT", "PRIMARY", "AUTO_INCREMENT", "IDENTITY", "UNIQUE", "KEY", "CONSTRAINT")):
                col_match_multi = re.match(
                    r"^([^\s]+)\s+([\w]+(?:\s+[\w]+)*\s*(?:\([^)]*\))?)\s*(.*)$",
                    line,
                    re.IGNORECASE,
                )
                if col_match_multi:
                    col_match = col_match_multi
        if not col_match:
            col_match = re.match(
                r"^([^\s]+)\s+([\w]+(?:\s+[\w]+)*\s*(?:\([^)]*\))?)\s*(.*)$",
                line,
                re.IGNORECASE,
            )
        if not col_match:
            continue

        col_name = _normalize_identifier(col_match.group(1), dialect)
        type_str = col_match.group(2).strip()
        rest = col_match.group(3).strip().upper()

        # Inline PRIMARY KEY (e.g. [id] INT NOT NULL PRIMARY KEY) or from CONSTRAINT PRIMARY KEY (col)
        is_pk = (
            "PRIMARY KEY" in (rest or "").upper()
            or col_name in primary_key_columns
            or bool(re.search(r"\bPRIMARY\s+KEY\b", (rest or ""), re.IGNORECASE))
        )
        if is_pk:
            primary_key_columns.add(col_name)

        canonical_type = _map_type_to_canonical(type_str, dialect)
        constraints: dict[str, Any] = {}
        if "NOT NULL" in rest:
            constraints["not_null"] = True
        if "AUTO_INCREMENT" in rest or "IDENTITY" in rest:
            constraints["auto_increment"] = True

        generation = GenerationRule(unique=True) if is_pk else None
        columns.append(
            CanonicalColumn(
                name=col_name,
                type=canonical_type,
                primary_key=is_pk,
                constraints=constraints if constraints else None,
                generation=generation,
            )
        )

    # If we found PRIMARY KEY (...) before columns, mark those columns
    for c in columns:
        if c.name in primary_key_columns and not c.primary_key:
            c.primary_key = True
            c.generation = GenerationRule(unique=True)

    return columns


def parse_ddl(sql: str, dialect: str | None = None) -> list[CanonicalTableSchema]:
    """
    Parse SQL DDL string containing one or more CREATE TABLE statements.
    Returns list of CanonicalTableSchema.

    - sql: full schema dump text (e.g. from mysqldump --no-data, pg_dump -s, mssql-scripter).
    - dialect: "mysql" | "postgres" | "sqlserver" | None (auto-detect from content).
    """
    dialect = dialect or _detect_dialect(sql)
    dialect = dialect.lower().strip()
    if dialect not in ("mysql", "postgres", "sqlserver"):
        dialect = "mysql"

    result: list[CanonicalTableSchema] = []
    for table_name, body in _find_create_table_blocks(sql):
        columns = _parse_table_body(body, dialect, table_name)
        result.append(
            CanonicalTableSchema(
                name=table_name,
                columns=columns,
                description=f"Parsed from {dialect} schema dump",
            )
        )
    return result


def parse_ddl_file(path: str | Path, dialect: str | None = None) -> list[CanonicalTableSchema]:
    """Load a .sql schema dump file and parse CREATE TABLE statements."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"DDL file not found: {path}")
    with open(path, encoding="utf-8", errors="replace") as f:
        sql = f.read()
    return parse_ddl(sql, dialect=dialect)
