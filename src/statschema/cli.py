"""
statschema command-line interface.

A DBA writes a YAML schema file and runs one of three commands — no Python required.

  python -m statschema ddl      schema.yaml --dialect postgres
  python -m statschema generate schema.yaml --sf 1 --format csv
  python -m statschema load     schema.yaml --dialect postgres --dsn "host=... dbname=..."

Sub-commands
------------
ddl
    Emit CREATE TABLE statements for every table in the schema.
    Output goes to stdout (pipe to a .sql file to save).

generate
    Stream synthetic rows to stdout (CSV or JSONL) without touching any database.
    Use --out-dir to write one file per table instead.

load
    Create tables in the target database and load synthetic data.
    The fastest available strategy is chosen per dialect automatically.
    Pass --strategy to override.

Connection
----------
The --dsn flag accepts a plain libpq / JDBC-style connection string.
For databases that use environment variables (MySQL, Oracle, Db2), the
matching BENCH_* variables can be set in the environment instead — see
`python -m statschema load --help` for the full list.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from .schema_io import load_canonical, resolve_load_order, resolve_row_counts, dump_schema
from .ddl_emitter import emit_ddl, SUPPORTED_DIALECTS
from .row_generator import generate_rows
from .data_loader import LoadStrategy, load_dataframe
from .schema_transforms import rename_tables, parse_table_map, TABLE_NAME_PRESETS
from .model import CanonicalTableSchema, CanonicalColumn, CanonicalForeignKey
from .stats_model import DatabaseStats

logger = logging.getLogger("statschema")

# ---------------------------------------------------------------------------
# Fastest proven strategy per dialect
# ---------------------------------------------------------------------------

_FASTEST: dict[str, LoadStrategy] = {
    "postgres":    LoadStrategy.BULK_COPY,
    "cockroachdb": LoadStrategy.BULK_COPY,
    "neon":        LoadStrategy.BULK_COPY,
    "mysql":       LoadStrategy.BULK_COPY,
    "mariadb":     LoadStrategy.BULK_COPY,
    "sqlserver":   LoadStrategy.BULK_COPY,
    "db2":         LoadStrategy.BULK_COPY,
    "oracle":      LoadStrategy.MULTI_ROW,
    "sqlite":      LoadStrategy.MULTI_ROW,
    "databricks":  LoadStrategy.MULTI_ROW,
}

_ALL_DIALECTS = sorted(set(SUPPORTED_DIALECTS) | set(_FASTEST))


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

def _connect(dialect: str, dsn: str | None) -> Any:
    """
    Open and return a DBAPI-2 connection.

    DSN formats by dialect
    ----------------------
    postgres / cockroachdb / neon
        libpq keyword string:  "host=localhost port=5432 dbname=mydb user=me password=s3cr3t"
        or URL:                "postgresql://me:s3cr3t@localhost/mydb"

    mysql / mariadb
        DSN format:            "host=localhost port=3306 user=root password=s3cr3t database=mydb"
        Or env vars:           STATSCHEMA_MYSQL_HOST / _PORT / _USER / _PASS / _DB

    sqlserver
        ADO-style string:      "SERVER=localhost,1433;DATABASE=mydb;UID=sa;PWD=s3cr3t"

    oracle
        Easy-connect string:   "localhost:1521/XEPDB1"
        With user/pass via env: STATSCHEMA_ORACLE_USER / _PASS / _DSN

    db2
        CLI DSN:               "DATABASE=mydb;HOSTNAME=localhost;PORT=50000;UID=u;PWD=p"

    sqlite
        File path:             "/tmp/mydb.db"  (use ":memory:" for in-memory)
    """
    env = os.environ

    if dialect == "sqlite":
        import sqlite3
        path = dsn or env.get("STATSCHEMA_SQLITE_PATH", ":memory:")
        return sqlite3.connect(path)

    if dialect in ("postgres", "cockroachdb", "neon"):
        import psycopg2
        conn_str = dsn or env.get("STATSCHEMA_PG_DSN", "")
        if not conn_str:
            _die(
                "Provide --dsn or set STATSCHEMA_PG_DSN.\n"
                '  Example: --dsn "host=localhost dbname=mydb user=postgres password=s3cr3t"'
            )
        return psycopg2.connect(conn_str)

    if dialect in ("mysql", "mariadb"):
        import pymysql
        if dsn:
            parts = dict(kv.split("=", 1) for kv in dsn.split() if "=" in kv)
            host = parts.get("host", "localhost")
            port = int(parts.get("port", 3306))
            user = parts.get("user", "root")
            password = parts.get("password", "")
            database = parts.get("database", "")
        else:
            host     = env.get("STATSCHEMA_MYSQL_HOST", "localhost")
            port     = int(env.get("STATSCHEMA_MYSQL_PORT", "3306"))
            user     = env.get("STATSCHEMA_MYSQL_USER", "root")
            password = env.get("STATSCHEMA_MYSQL_PASS", "")
            database = env.get("STATSCHEMA_MYSQL_DB", "")
        if not database:
            _die("Provide database= in --dsn or set STATSCHEMA_MYSQL_DB.")
        return pymysql.connect(
            host=host, port=port, user=user, password=password,
            database=database, local_infile=True, autocommit=False,
        )

    if dialect == "sqlserver":
        conn_str = dsn or env.get("STATSCHEMA_SQLSERVER_DSN", "")
        if not conn_str:
            _die(
                "Provide --dsn or set STATSCHEMA_SQLSERVER_DSN.\n"
                '  Example: --dsn "SERVER=localhost,1433;DATABASE=mydb;UID=sa;PWD=s3cr3t"'
            )
        try:
            import mssql_python
            return mssql_python.connect(conn_str)
        except ImportError:
            import pymssql
            parts = dict(p.split("=", 1) for p in conn_str.split(";") if "=" in p)
            server_str = parts.get("SERVER", "localhost")
            if "," in server_str:
                host, port_s = server_str.rsplit(",", 1)
                port = int(port_s.strip())
            else:
                host, port = server_str, 1433
            return pymssql.connect(
                server=host, port=port,
                database=parts.get("DATABASE", "master"),
                user=parts.get("UID", "sa"),
                password=parts.get("PWD", ""),
            )

    if dialect == "oracle":
        import oracledb
        oracle_dsn  = dsn or env.get("STATSCHEMA_ORACLE_DSN",  "localhost:1521/XEPDB1")
        oracle_user = env.get("STATSCHEMA_ORACLE_USER", "")
        oracle_pass = env.get("STATSCHEMA_ORACLE_PASS", "")
        if dsn and "@" in dsn:
            # URL form: user/pass@host:port/service
            cred, oracle_dsn = dsn.rsplit("@", 1)
            oracle_user, oracle_pass = cred.split("/", 1)
        if not oracle_user:
            _die(
                "Set STATSCHEMA_ORACLE_USER and STATSCHEMA_ORACLE_PASS, or use\n"
                '  --dsn "user/password@localhost:1521/XEPDB1"'
            )
        return oracledb.connect(user=oracle_user, password=oracle_pass, dsn=oracle_dsn)

    if dialect == "db2":
        import ibm_db_dbi
        conn_str = dsn or env.get("STATSCHEMA_DB2_DSN", "")
        if not conn_str:
            _die(
                "Provide --dsn or set STATSCHEMA_DB2_DSN.\n"
                '  Example: --dsn "DATABASE=mydb;HOSTNAME=localhost;PORT=50000;UID=u;PWD=p"'
            )
        return ibm_db_dbi.connect(conn_str, "", "")

    _die(f"Unsupported dialect {dialect!r}. Choices: {', '.join(_ALL_DIALECTS)}")


# ---------------------------------------------------------------------------
# DDL sub-command
# ---------------------------------------------------------------------------

def _build_table_map(args: argparse.Namespace) -> dict[str, str]:
    """Merge --table-preset and --table-map into a single mapping dict."""
    mapping: dict[str, str] = {}
    preset = getattr(args, "table_preset", None)
    raw    = getattr(args, "table_map", None)
    if preset:
        mapping.update(TABLE_NAME_PRESETS[preset])
    if raw:
        mapping.update(parse_table_map(raw))
    return mapping


def _cmd_ddl(args: argparse.Namespace) -> None:
    """Emit CREATE TABLE SQL for every table in the YAML schema."""
    tables = load_canonical(Path(args.schema))
    mapping = _build_table_map(args)
    if mapping:
        tables = rename_tables(tables, mapping)
    ordered = resolve_load_order(tables)

    for table in ordered:
        try:
            stmt = emit_ddl(table, args.dialect)
        except ValueError as exc:
            print(f"-- WARNING: {exc}", file=sys.stderr)
            continue
        print(stmt)
        print()


# ---------------------------------------------------------------------------
# Generate sub-command
# ---------------------------------------------------------------------------

def _cmd_generate(args: argparse.Namespace) -> None:
    """Stream synthetic rows to stdout (CSV or JSONL) or to per-table files."""
    tables  = load_canonical(Path(args.schema))
    mapping = _build_table_map(args)
    if mapping:
        tables = rename_tables(tables, mapping)
    ordered = resolve_load_order(tables)
    counts  = resolve_row_counts(ordered, scale_factor=args.sf)

    out_dir = Path(args.out_dir) if args.out_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    for table in ordered:
        n    = counts[table.name]
        cols = [c.name for c in table.columns]
        rows = generate_rows(table, n, parent_row_counts=counts, seed=args.seed)

        if out_dir:
            dest = out_dir / f"{table.name}.{args.format}"
            with dest.open("w", newline="") as fh:
                _write_rows(fh, rows, cols, args.format)
            print(f"  {table.name:<24} {n:>10,} rows → {dest}", file=sys.stderr)
        else:
            _write_rows(sys.stdout, rows, cols, args.format)


def _write_rows(fh, rows, cols: list[str], fmt: str) -> None:
    if fmt == "csv":
        writer = csv.DictWriter(fh, fieldnames=cols)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    else:  # jsonl
        for row in rows:
            fh.write(json.dumps(row, default=str) + "\n")


# ---------------------------------------------------------------------------
# Load sub-command
# ---------------------------------------------------------------------------

def _query_row_offsets(conn, ordered, dialect: str) -> dict[str, int]:
    """Return the current COUNT(*) for each table (used as row_offset in append mode)."""
    offsets: dict[str, int] = {}
    cur = conn.cursor()
    for table in ordered:
        tname = (
            table.name.upper() if dialect in ("oracle", "db2") else table.name
        )
        try:
            cur.execute(f"SELECT COUNT(*) FROM {tname}")
            row = cur.fetchone()
            offsets[table.name] = int(row[0]) if row else 0
        except Exception:
            offsets[table.name] = 0
    return offsets


def _cmd_load(args: argparse.Namespace) -> None:
    """Create tables and load synthetic data into the target database."""
    import dataclasses
    import time

    tables  = load_canonical(Path(args.schema))
    mapping = _build_table_map(args)
    if mapping:
        tables = rename_tables(tables, mapping)
    ordered = resolve_load_order(tables)
    counts  = resolve_row_counts(ordered, scale_factor=args.sf)
    append  = getattr(args, "append", False)

    strategy = (
        LoadStrategy[args.strategy.upper()]
        if args.strategy
        else _FASTEST.get(args.dialect, LoadStrategy.MULTI_ROW)
    )

    conn = _connect(args.dialect, args.dsn)

    if append:
        # Query existing rows to compute PK offsets and a fresh seed.
        existing = _query_row_offsets(conn, ordered, args.dialect)
        total_existing = sum(existing.values())
        effective_seed = args.seed + total_existing
        # FK parent ranges span existing + new rows so child FKs stay valid.
        parent_row_counts_for_gen = {
            t.name: existing.get(t.name, 0) + counts[t.name]
            for t in ordered
        }
        print(
            f"  Append mode: {total_existing:,} existing rows; "
            f"effective seed = {effective_seed}",
            file=sys.stderr,
        )
    else:
        existing = {t.name: 0 for t in ordered}
        effective_seed = args.seed
        parent_row_counts_for_gen = counts

        # ----- create tables (drop first) ----------------------------------------
        cur = conn.cursor()
        t_ddl = time.perf_counter()
        for table in ordered:
            tname = table.name
            # strip PK/unique to avoid uniqueness violations from random generator
            stripped = dataclasses.replace(
                table,
                columns=[dataclasses.replace(c, primary_key=False, unique=False) for c in table.columns],
                fk_constraints=None,
                foreign_keys=None,
            )
            _drop_table(cur, tname, args.dialect)
            try:
                stmt = emit_ddl(stripped, args.dialect)
            except ValueError:
                stmt = _simple_ddl(stripped)
            try:
                cur.execute(stmt)
            except Exception as exc:
                logger.warning("DDL failed for %s: %s", tname, exc)
        conn.commit()
        print(f"  Tables created ({time.perf_counter() - t_ddl:.1f}s)", file=sys.stderr)

    # ----- load data ----------------------------------------------------------
    total_rows = 0
    t0_all = time.perf_counter()
    for table in ordered:
        n          = counts[table.name]
        row_offset = existing.get(table.name, 0)
        cols       = [c.name for c in table.columns]
        insert_name = (
            table.name.upper() if args.dialect in ("oracle", "db2") else table.name
        )
        t0 = time.perf_counter()
        loaded = load_dataframe(
            generate_rows(
                table, n,
                parent_row_counts=parent_row_counts_for_gen,
                seed=effective_seed,
                row_offset=row_offset,
            ),
            conn, insert_name, args.dialect,
            strategy=strategy, cols=cols, commit=True,
        )
        elapsed = time.perf_counter() - t0
        rps = loaded / elapsed if elapsed > 0 else 0
        total_rows += loaded
        print(
            f"  {table.name:<24} +{loaded:>10,} rows (offset={row_offset:,})  "
            f"{elapsed:6.1f}s  {rps:>8,.0f} rows/s",
            file=sys.stderr,
        )

    total_s = time.perf_counter() - t0_all
    print(
        f"\n  TOTAL  +{total_rows:>10,} rows  {total_s:.1f}s  "
        f"{total_rows / total_s:,.0f} rows/s",
        file=sys.stderr,
    )
    conn.close()


def _drop_table(cur, tname: str, dialect: str) -> None:
    try:
        if dialect in ("postgres", "cockroachdb", "neon", "sqlite", "mysql", "mariadb"):
            cur.execute(f"DROP TABLE IF EXISTS {tname}")
        elif dialect == "sqlserver":
            cur.execute(f"IF OBJECT_ID('{tname}','U') IS NOT NULL DROP TABLE {tname}")
        elif dialect in ("oracle", "db2"):
            try:
                cur.execute(f'DROP TABLE "{tname.upper()}"')
            except Exception:
                pass
    except Exception:
        pass


def _simple_ddl(table) -> str:
    """Minimal SQLite-compatible DDL used when emit_ddl raises ValueError."""
    _map = {
        "integer": "INTEGER", "long": "INTEGER", "string": "TEXT",
        "boolean": "INTEGER", "decimal": "REAL", "float": "REAL",
        "double": "REAL", "date": "TEXT", "timestamp": "TEXT",
        "timestamptz": "TEXT", "time": "TEXT", "binary": "BLOB",
    }
    cols = ", ".join(
        f"{c.name} {_map.get(c.type.lower(), 'TEXT')}"
        for c in table.columns
    )
    return f"CREATE TABLE {table.name} ({cols})"


# ---------------------------------------------------------------------------
# collect sub-command — connect to a live database, pull schema + stats
# ---------------------------------------------------------------------------

def _prompt_if_missing(value: str | None, label: str, default: str | None = None,
                       secret: bool = False) -> str:
    """Return value if already set; otherwise prompt the user interactively."""
    if value:
        return value
    if not sys.stdin.isatty():
        if default is not None:
            return default
        _die(f"--{label.lower().replace(' ', '-')} is required (not running interactively).")
    display = f"  {label} [{default}]: " if default else f"  {label}: "
    if secret:
        import getpass
        result = getpass.getpass(display)
    else:
        result = input(display).strip()
    return result or default or ""


class _LoggingCursor:
    """Thin cursor wrapper that prints every SQL statement when show_sql=True."""

    def __init__(self, cursor, show_sql: bool) -> None:
        self._cur = cursor
        self._show = show_sql

    def execute(self, sql: str, params=None):
        if self._show:
            import re
            disp = re.sub(r"\s+", " ", sql.strip())[:300]
            print(f"    → SQL: {disp}", file=sys.stderr)
        return self._cur.execute(sql, params) if params is not None else self._cur.execute(sql)

    def fetchall(self):   return self._cur.fetchall()
    def fetchone(self):   return self._cur.fetchone()
    def __iter__(self):   return iter(self._cur)
    def __getattr__(self, name): return getattr(self._cur, name)


_DEFAULT_PORTS: dict[str, int] = {
    "mysql": 3306, "mariadb": 3306,
    "postgres": 5432, "cockroachdb": 26257, "neon": 5432,
    "sqlserver": 1433, "oracle": 1521, "db2": 50000, "sqlite": 0,
}

# information_schema / udt_name → canonical type
_SQL_TO_CANONICAL: dict[str, str] = {
    "int": "integer", "integer": "integer", "int4": "integer", "int32": "integer",
    "int2": "smallint", "smallint": "smallint",
    "bigint": "bigint", "int8": "bigint", "int64": "bigint",
    "tinyint": "tinyint",
    "float": "float", "float4": "float", "real": "float",
    "double": "double", "float8": "double", "double precision": "double",
    "decimal": "decimal", "numeric": "decimal", "number": "decimal",
    "varchar": "varchar", "character varying": "varchar",
    "nvarchar": "nvarchar",
    "char": "char", "character": "char", "bpchar": "char", "nchar": "nchar",
    "text": "text", "longtext": "text", "mediumtext": "text", "clob": "text",
    "boolean": "boolean", "bool": "boolean", "bit": "boolean",
    "date": "date",
    "timestamp": "timestamp", "timestamp without time zone": "timestamp",
    "datetime": "timestamp", "datetime2": "timestamp", "smalldatetime": "timestamp",
    "timestamptz": "timestamptz", "timestamp with time zone": "timestamptz",
    "time": "time",
    "json": "json", "jsonb": "json",
    "uuid": "uuid",
    "binary": "binary", "varbinary": "binary", "blob": "binary",
    "bytea": "binary", "image": "binary",
    "money": "decimal", "smallmoney": "decimal",
    "xml": "text",
}


def _canonical_type(sql_type: str, udt_name: str | None = None) -> str:
    key = (udt_name or sql_type).lower().strip()
    return _SQL_TO_CANONICAL.get(key) or _SQL_TO_CANONICAL.get(sql_type.lower().strip(), sql_type.lower())


def _connect_interactive(args) -> tuple[Any, str, int, str, str]:
    """
    Build a live DB connection from parsed args, prompting for any missing values.
    Returns (conn, host, port, user, database).
    """
    dialect = args.dialect

    if dialect == "sqlite":
        import sqlite3
        path = getattr(args, "database", None) or ":memory:"
        return sqlite3.connect(path), "localhost", 0, "", path

    host = _prompt_if_missing(getattr(args, "host", None),     "Host",     "localhost")
    port = int(_prompt_if_missing(str(getattr(args, "port", None) or ""),
                                  "Port", str(_DEFAULT_PORTS.get(dialect, 5432))))
    user = _prompt_if_missing(getattr(args, "user", None),     "Username", "")
    pw   = _prompt_if_missing(getattr(args, "password", None), "Password", secret=True)
    db   = _prompt_if_missing(getattr(args, "database", None), "Database", "")

    if dialect in ("postgres", "cockroachdb", "neon"):
        import psycopg2
        return psycopg2.connect(host=host, port=port, dbname=db, user=user, password=pw), host, port, user, db

    if dialect in ("mysql", "mariadb"):
        import pymysql
        return (pymysql.connect(host=host, port=port, user=user, password=pw,
                                database=db, local_infile=True, autocommit=True),
                host, port, user, db)

    if dialect == "sqlserver":
        try:
            import mssql_python
            cs = f"SERVER={host},{port};DATABASE={db};UID={user};PWD={pw}"
            return mssql_python.connect(cs), host, port, user, db
        except ImportError:
            import pymssql
            return pymssql.connect(server=host, port=port, database=db,
                                   user=user, password=pw), host, port, user, db

    if dialect == "oracle":
        import oracledb
        dsn = f"{host}:{port}/{db}"
        return oracledb.connect(user=user, password=pw, dsn=dsn), host, port, user, db

    if dialect == "db2":
        import ibm_db_dbi
        cs = f"DATABASE={db};HOSTNAME={host};PORT={port};UID={user};PWD={pw}"
        return ibm_db_dbi.connect(cs, "", ""), host, port, user, db

    _die(f"Unsupported dialect: {dialect!r}")


def _list_tables(cur: _LoggingCursor, dialect: str, schema: str | None,
                 pattern: str) -> list[str]:
    """Return table names matching the SQL LIKE pattern."""
    like = pattern.replace("*", "%")

    if dialect == "sqlite":
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ?", (like,))

    elif dialect in ("mysql", "mariadb"):
        if schema:
            cur.execute(
                "SELECT TABLE_NAME FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME LIKE %s ORDER BY TABLE_NAME",
                (schema, like),
            )
        else:
            cur.execute(f"SHOW TABLES LIKE %s", (like,))

    elif dialect in ("postgres", "cockroachdb", "neon"):
        pg_schema = schema or "public"
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s AND table_name LIKE %s "
            "AND table_type = 'BASE TABLE' ORDER BY table_name",
            (pg_schema, like),
        )

    elif dialect == "sqlserver":
        ss_schema = schema or "dbo"
        cur.execute(
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME LIKE %s "
            "AND TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME",
            (ss_schema, like),
        )

    elif dialect == "oracle":
        cur.execute(
            "SELECT TABLE_NAME FROM ALL_TABLES WHERE OWNER = :1 AND TABLE_NAME LIKE :2 ORDER BY TABLE_NAME",
            ((schema or "").upper(), like.upper()),
        )

    elif dialect == "db2":
        cur.execute(
            "SELECT TABNAME FROM SYSCAT.TABLES WHERE TABSCHEMA = ? AND TABNAME LIKE ? AND TYPE = 'T' ORDER BY TABNAME",
            ((schema or "").upper(), like.upper()),
        )

    else:
        return []

    return [row[0] for row in cur.fetchall()]


def _fk_groups(rows) -> list[CanonicalForeignKey]:
    """Group FK rows (constraint_name, col, ref_table, ref_col) into CanonicalForeignKey list."""
    from collections import defaultdict
    groups: dict[str, dict] = defaultdict(lambda: {"cols": [], "ref_table": "", "ref_cols": []})
    for (cname, col, ref_tbl, ref_col) in rows:
        g = groups[cname]
        g["cols"].append(col)
        g["ref_table"] = ref_tbl
        g["ref_cols"].append(ref_col)
    return [
        CanonicalForeignKey(columns=v["cols"], ref_table=v["ref_table"], ref_columns=v["ref_cols"])
        for v in groups.values()
    ]


def _collect_schema_mysql(cur: _LoggingCursor, table: str) -> CanonicalTableSchema | None:
    """Use SHOW CREATE TABLE — captures AUTO_INCREMENT, defaults, COMMENTs exactly."""
    from .ddl_parser import parse_ddl
    cur.execute(f"SHOW CREATE TABLE `{table}`")
    row = cur.fetchone()
    if not row:
        return None
    ddl = row[1]
    tables = parse_ddl(ddl, dialect="mysql")
    return tables[0] if tables else None


def _collect_schema_sqlite(cur: _LoggingCursor, table: str) -> CanonicalTableSchema | None:
    from .ddl_parser import parse_ddl
    cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,))
    row = cur.fetchone()
    if not row or not row[0]:
        return None
    tables = parse_ddl(row[0], dialect="sqlite")
    return tables[0] if tables else None


def _collect_schema_postgres(cur: _LoggingCursor, schema: str, table: str) -> CanonicalTableSchema | None:
    pg_schema = schema or "public"

    cur.execute(
        "SELECT column_name, udt_name, data_type, character_maximum_length, "
        "       numeric_precision, numeric_scale, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
        (pg_schema, table),
    )
    col_rows = cur.fetchall()
    if not col_rows:
        return None

    # PKs
    cur.execute(
        "SELECT kcu.column_name FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu "
        "     ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema "
        "WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = %s AND tc.table_name = %s "
        "ORDER BY kcu.ordinal_position",
        (pg_schema, table),
    )
    pk_cols = [r[0] for r in cur.fetchall()]

    # FKs
    cur.execute(
        "SELECT tc.constraint_name, kcu.column_name, ccu.table_name, ccu.column_name "
        "FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu "
        "     ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema "
        "JOIN information_schema.constraint_column_usage ccu "
        "     ON ccu.constraint_name = tc.constraint_name AND ccu.table_schema = tc.table_schema "
        "WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = %s AND tc.table_name = %s "
        "ORDER BY kcu.ordinal_position",
        (pg_schema, table),
    )
    fks = _fk_groups(cur.fetchall())

    cols = []
    for (col_name, udt_name, data_type, char_len, num_prec, num_scale, is_null, col_def) in col_rows:
        ctype = _canonical_type(data_type, udt_name)
        c = CanonicalColumn(
            name=col_name,
            type=ctype,
            not_null=(is_null == "NO"),
            primary_key=(col_name in pk_cols),
            length=char_len,
            precision=num_prec if ctype == "decimal" else None,
            scale=num_scale if ctype == "decimal" else None,
            default=col_def if col_def not in (None, "NULL", "null") else None,
        )
        cols.append(c)

    return CanonicalTableSchema(
        name=table,
        columns=cols,
        primary_key=pk_cols or None,
        fk_constraints=fks or None,
    )


def _collect_schema_sqlserver(cur: _LoggingCursor, schema: str, table: str) -> CanonicalTableSchema | None:
    ss_schema = schema or "dbo"

    cur.execute(
        "SELECT c.COLUMN_NAME, c.DATA_TYPE, c.CHARACTER_MAXIMUM_LENGTH, "
        "       c.NUMERIC_PRECISION, c.NUMERIC_SCALE, c.IS_NULLABLE, c.COLUMN_DEFAULT, "
        "       COLUMNPROPERTY(OBJECT_ID(c.TABLE_SCHEMA+'.'+c.TABLE_NAME), c.COLUMN_NAME, 'IsIdentity') "
        "FROM INFORMATION_SCHEMA.COLUMNS c "
        "WHERE c.TABLE_SCHEMA = %s AND c.TABLE_NAME = %s ORDER BY c.ORDINAL_POSITION",
        (ss_schema, table),
    )
    col_rows = cur.fetchall()
    if not col_rows:
        return None

    cur.execute(
        "SELECT kcu.COLUMN_NAME FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc "
        "JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu "
        "     ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME "
        "WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY' AND tc.TABLE_SCHEMA = %s AND tc.TABLE_NAME = %s "
        "ORDER BY kcu.ORDINAL_POSITION",
        (ss_schema, table),
    )
    pk_cols = [r[0] for r in cur.fetchall()]

    cur.execute(
        "SELECT tc.CONSTRAINT_NAME, kcu.COLUMN_NAME, ccu.TABLE_NAME, ccu.COLUMN_NAME "
        "FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc "
        "JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu "
        "     ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME "
        "JOIN INFORMATION_SCHEMA.CONSTRAINT_COLUMN_USAGE ccu "
        "     ON tc.CONSTRAINT_NAME = ccu.CONSTRAINT_NAME "
        "WHERE tc.CONSTRAINT_TYPE = 'FOREIGN KEY' AND tc.TABLE_SCHEMA = %s AND tc.TABLE_NAME = %s "
        "ORDER BY kcu.ORDINAL_POSITION",
        (ss_schema, table),
    )
    fks = _fk_groups(cur.fetchall())

    cols = []
    for (col_name, data_type, char_len, num_prec, num_scale, is_null, col_def, is_identity) in col_rows:
        ctype = _canonical_type(data_type)
        c = CanonicalColumn(
            name=col_name,
            type=ctype,
            not_null=(is_null == "NO"),
            primary_key=(col_name in pk_cols),
            auto_increment=bool(is_identity),
            length=char_len,
            precision=num_prec if ctype == "decimal" else None,
            scale=num_scale if ctype == "decimal" else None,
            default=col_def if col_def not in (None, "NULL", "(null)") else None,
        )
        cols.append(c)

    return CanonicalTableSchema(
        name=table,
        columns=cols,
        primary_key=pk_cols or None,
        fk_constraints=fks or None,
    )


def _collect_schema_oracle(cur: _LoggingCursor, schema: str, table: str) -> CanonicalTableSchema | None:
    from .ddl_parser import parse_ddl
    try:
        cur.execute(
            "SELECT DBMS_METADATA.GET_DDL('TABLE', :1, :2) FROM DUAL",
            (table.upper(), schema.upper()),
        )
        row = cur.fetchone()
    except Exception:
        row = None
    if row and row[0]:
        tables = parse_ddl(str(row[0]), dialect="oracle")
        return tables[0] if tables else None

    # Fallback: ALL_TAB_COLUMNS
    cur.execute(
        "SELECT COLUMN_NAME, DATA_TYPE, CHAR_LENGTH, DATA_PRECISION, DATA_SCALE, NULLABLE, DATA_DEFAULT "
        "FROM ALL_TAB_COLUMNS WHERE OWNER = :1 AND TABLE_NAME = :2 ORDER BY COLUMN_ID",
        (schema.upper(), table.upper()),
    )
    col_rows = cur.fetchall()
    if not col_rows:
        return None

    cur.execute(
        "SELECT cc.COLUMN_NAME FROM ALL_CONSTRAINTS c JOIN ALL_CONS_COLUMNS cc "
        "ON c.CONSTRAINT_NAME = cc.CONSTRAINT_NAME AND c.OWNER = cc.OWNER "
        "WHERE c.CONSTRAINT_TYPE = 'P' AND c.OWNER = :1 AND c.TABLE_NAME = :2 ORDER BY cc.POSITION",
        (schema.upper(), table.upper()),
    )
    pk_cols = [r[0] for r in cur.fetchall()]

    cols = [
        CanonicalColumn(
            name=col_name,
            type=_canonical_type(data_type),
            not_null=(nullable == "N"),
            primary_key=(col_name in pk_cols),
            length=char_len,
            precision=prec,
            scale=scale,
            default=default.strip() if default and default.strip() not in ("NULL",) else None,
        )
        for (col_name, data_type, char_len, prec, scale, nullable, default) in col_rows
    ]
    return CanonicalTableSchema(name=table, columns=cols, primary_key=pk_cols or None)


def _collect_schema_live(cur: _LoggingCursor, dialect: str, schema: str | None,
                         table: str) -> CanonicalTableSchema | None:
    """Dispatch to the dialect-specific schema collector."""
    if dialect in ("mysql", "mariadb"):
        return _collect_schema_mysql(cur, table)
    if dialect == "sqlite":
        return _collect_schema_sqlite(cur, table)
    if dialect in ("postgres", "cockroachdb", "neon"):
        return _collect_schema_postgres(cur, schema or "public", table)
    if dialect == "sqlserver":
        return _collect_schema_sqlserver(cur, schema or "dbo", table)
    if dialect == "oracle":
        return _collect_schema_oracle(cur, schema or "", table)
    _die(f"Schema collection not yet supported for dialect {dialect!r}; collect DDL manually.")


def _cmd_collect(args) -> None:
    """Connect to a live database, collect schema + statistics, write YAML files."""
    import time
    from .db_stats_collector import collect_table_stats

    dialect    = args.dialect
    schema     = getattr(args, "schema_name", None) or None
    pattern    = getattr(args, "tables", "%")
    show_sql   = getattr(args, "show_sql", False)
    out_schema = Path(getattr(args, "out_schema", "schema.yaml"))
    out_stats  = Path(getattr(args, "out_stats",  "stats.yaml"))
    do_analyze = getattr(args, "analyze", False)

    # ── connect ──────────────────────────────────────────────────────────────
    conn, host, port, user, database = _connect_interactive(args)
    print(f"\n  Connected to {dialect} @ {host}:{port}/{database} as {user}", file=sys.stderr)

    raw_cur = conn.cursor()
    cur = _LoggingCursor(raw_cur, show_sql)

    # ── optional ANALYZE ─────────────────────────────────────────────────────
    if do_analyze:
        if dialect in ("mysql", "mariadb"):
            print("  Running ANALYZE TABLE … (use --no-analyze to skip)", file=sys.stderr)
        elif dialect in ("postgres", "cockroachdb", "neon"):
            print("  Running ANALYZE … (use --no-analyze to skip)", file=sys.stderr)
            cur.execute("ANALYZE")
            conn.commit()

    # ── list tables ──────────────────────────────────────────────────────────
    tables_found = _list_tables(cur, dialect, schema, pattern)
    if not tables_found:
        _die(f"No tables found matching {pattern!r} in {dialect}@{database}")
    print(f"  Found {len(tables_found)} table(s) matching {pattern!r}\n", file=sys.stderr)

    # ── collect per table ─────────────────────────────────────────────────────
    canonical_tables: list[CanonicalTableSchema] = []
    table_stats_list = []

    for tname in tables_found:
        t0 = time.perf_counter()

        # Schema
        tbl = _collect_schema_live(cur, dialect, schema, tname)
        if tbl is None:
            print(f"  ⚠  {tname}: schema not found — skipped", file=sys.stderr)
            continue
        canonical_tables.append(tbl)
        n_cols = len(tbl.columns)
        n_fks  = len(tbl.fk_constraints or [])

        # Stats
        if do_analyze and dialect in ("mysql", "mariadb"):
            cur.execute(f"ANALYZE TABLE `{tname}`")

        try:
            ts = collect_table_stats(conn, tname, dialect=dialect, schema=schema)
            table_stats_list.append(ts)
            row_count = ts.row_count or 0
        except Exception as exc:
            logger.warning("Stats collection failed for %s: %s", tname, exc)
            row_count = 0

        elapsed = time.perf_counter() - t0
        fk_note = f", {n_fks} FK{'s' if n_fks != 1 else ''}" if n_fks else ""
        print(
            f"  ✓  {tname:<30} {n_cols} cols{fk_note}, {row_count:,} rows  ({elapsed:.1f}s)",
            file=sys.stderr,
        )

    if not canonical_tables:
        _die("No tables collected — nothing to write.")

    # ── write output ─────────────────────────────────────────────────────────
    dump_schema(canonical_tables, str(out_schema))
    db_stats = DatabaseStats(tables=table_stats_list)
    from .stats_io import dump_stats as _dump_stats
    _dump_stats(db_stats, str(out_stats))

    total_rows = sum(ts.row_count or 0 for ts in table_stats_list)
    print(
        f"\n  Wrote {out_schema}  ({len(canonical_tables)} tables)\n"
        f"  Wrote {out_stats}   ({len(table_stats_list)} tables, {total_rows:,} total rows)\n",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _add_rename_args(p: argparse.ArgumentParser) -> None:
    """Add --table-preset and --table-map to any subcommand parser."""
    p.add_argument(
        "--table-preset",
        dest="table_preset",
        choices=sorted(TABLE_NAME_PRESETS),
        metavar="PRESET",
        help=(
            "Apply a built-in table-name preset before processing.  "
            f"Available: {', '.join(sorted(TABLE_NAME_PRESETS))}.  "
            "pgbench renames TPC-B tables to pgbench_branches/tellers/accounts/history; "
            "cockroach-tpcc renames orders→order for cockroach workload tpcc."
        ),
    )
    p.add_argument(
        "--table-map",
        dest="table_map",
        metavar="old=new[,old=new…]",
        help=(
            "Comma-separated rename pairs, e.g. --table-map orders=order.  "
            "Applied after --table-preset if both are given."
        ),
    )


def _build_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="statschema",
        description=(
            "statschema — YAML-driven schema DDL, data generation, and loading.\n\n"
            "A DBA writes a YAML file and runs one command.  No Python required.\n\n"
            "Quick start\n"
            "-----------\n"
            "  # Print DDL for PostgreSQL\n"
            "  python -m statschema ddl myschema.yaml --dialect postgres\n\n"
            "  # Generate 10 000 rows to CSV files\n"
            "  python -m statschema generate myschema.yaml --sf 1 --out-dir ./data\n\n"
            "  # Create tables and load into PostgreSQL\n"
            '  python -m statschema load myschema.yaml --dialect postgres \\\n'
            '      --dsn "host=localhost dbname=mydb user=me password=s3cr3t"'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = root.add_subparsers(dest="cmd", required=True)

    # ── ddl ──────────────────────────────────────────────────────────────────
    p_ddl = sub.add_parser(
        "ddl",
        help="Emit CREATE TABLE SQL to stdout.",
        description=(
            "Print dialect-specific CREATE TABLE statements for every table in the YAML.\n"
            "Tables are emitted in FK dependency order (parents before children).\n\n"
            "Examples\n"
            "--------\n"
            "  python -m statschema ddl schema.yaml --dialect postgres\n"
            "  python -m statschema ddl schema.yaml --dialect mysql > schema.sql"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_ddl.add_argument("schema", help="Path to the canonical YAML schema file.")
    p_ddl.add_argument(
        "--dialect", required=True,
        choices=_ALL_DIALECTS,
        help="Target SQL dialect.",
    )
    _add_rename_args(p_ddl)

    # ── generate ─────────────────────────────────────────────────────────────
    p_gen = sub.add_parser(
        "generate",
        help="Stream synthetic rows to stdout (CSV/JSONL) or to files.",
        description=(
            "Generate synthetic rows driven by the generation rules in the YAML.\n"
            "No database connection is needed.\n\n"
            "Examples\n"
            "--------\n"
            "  # Stream all tables as JSONL\n"
            "  python -m statschema generate schema.yaml --sf 1\n\n"
            "  # Write one CSV file per table to ./data/\n"
            "  python -m statschema generate schema.yaml --sf 1 --out-dir ./data\n\n"
            "  # Generate 10× scale factor\n"
            "  python -m statschema generate schema.yaml --sf 10 --format csv"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_gen.add_argument("schema", help="Path to the canonical YAML schema file.")
    p_gen.add_argument("--sf",     type=float, default=1.0,  help="Scale factor (default: 1).")
    p_gen.add_argument("--seed",   type=int,   default=42,   help="Random seed (default: 42).")
    p_gen.add_argument("--format", choices=["csv", "jsonl"], default="jsonl",
                       help="Output format (default: jsonl).")
    p_gen.add_argument("--out-dir", metavar="DIR",
                       help="Write one file per table here instead of stdout.")
    _add_rename_args(p_gen)

    # ── load ─────────────────────────────────────────────────────────────────
    p_load = sub.add_parser(
        "load",
        help="Create tables and load synthetic data into a live database.",
        description=(
            "Create tables (drop-and-recreate) in the target database, then stream\n"
            "synthetic rows from the YAML generation rules directly into those tables.\n"
            "Tables are created and loaded in FK dependency order.\n\n"
            "Connection\n"
            "----------\n"
            "Pass --dsn or set the matching environment variable:\n\n"
            "  Dialect          --dsn format / env var\n"
            '  postgres         "host=H dbname=D user=U password=P"  |  STATSCHEMA_PG_DSN\n'
            '  mysql/mariadb    "host=H port=P user=U password=P database=D"  |  STATSCHEMA_MYSQL_*\n'
            '  sqlserver        "SERVER=H,P;DATABASE=D;UID=U;PWD=P"  |  STATSCHEMA_SQLSERVER_DSN\n'
            '  oracle           "user/pass@host:port/service"  |  STATSCHEMA_ORACLE_DSN/_USER/_PASS\n'
            '  db2              "DATABASE=D;HOSTNAME=H;PORT=P;UID=U;PWD=P"  |  STATSCHEMA_DB2_DSN\n'
            '  sqlite           "/path/to/file.db"  |  STATSCHEMA_SQLITE_PATH\n\n'
            "Examples\n"
            "--------\n"
            "  python -m statschema load schema.yaml --dialect postgres \\\n"
            '      --dsn "host=localhost dbname=mydb user=me password=s3cr3t"\n\n'
            "  python -m statschema load schema.yaml --dialect mysql --sf 10 \\\n"
            '      --dsn "host=localhost user=root password=secret database=bench"\n\n'
            "  python -m statschema load schema.yaml --dialect sqlite \\\n"
            "      --dsn /tmp/bench.db"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_load.add_argument("schema",  help="Path to the canonical YAML schema file.")
    p_load.add_argument("--dialect", required=True, choices=_ALL_DIALECTS,
                        help="Target database dialect.")
    p_load.add_argument("--dsn",    help="Connection string (see above).")
    p_load.add_argument("--sf",     type=float, default=1.0,
                        help="Scale factor — multiplied by row_count_per_sf (default: 1).")
    p_load.add_argument("--seed",   type=int,   default=42,
                        help="Random seed for reproducible data (default: 42).")
    p_load.add_argument(
        "--strategy",
        choices=[s.name.lower() for s in LoadStrategy],
        help="Load strategy override.  Default: fastest for the dialect.",
    )
    p_load.add_argument(
        "--append", action="store_true",
        help=(
            "Append mode: skip DROP/CREATE and add rows on top of what already "
            "exists.  Sequential PK columns continue from the current row count; "
            "a new seed is derived automatically so data values are distinct."
        ),
    )
    _add_rename_args(p_load)
    p_load.add_argument("-v", "--verbose", action="store_true",
                        help="Enable debug logging.")

    # ── collect ───────────────────────────────────────────────────────────────
    p_col = sub.add_parser(
        "collect",
        help="Connect to a live database and collect DDL + statistics into YAML.",
        description=(
            "Connect to a live database, extract the schema and column statistics\n"
            "for every table matching --tables, and write canonical YAML files.\n\n"
            "Any missing connection flags are prompted for interactively.\n"
            "Use --show-sql to see every SQL statement sent to the database.\n\n"
            "Examples\n"
            "--------\n"
            "  # Collect everything from a MySQL database (prompts for password)\n"
            "  statschema collect --dialect mysql --host localhost --user root \\\n"
            "      --database northwind --tables '%'\n\n"
            "  # Collect only order* tables from PostgreSQL, show SQL, custom output files\n"
            "  statschema collect --dialect postgres --host db.example.com \\\n"
            "      --user myuser --database prod --schema public --tables 'order%' \\\n"
            "      --show-sql --out-schema orders.yaml --out-stats orders_stats.yaml\n\n"
            "  # SQL Server — prompts for all connection details\n"
            "  statschema collect --dialect sqlserver"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_col.add_argument("--dialect", required=True, choices=_ALL_DIALECTS,
                       help="Source database dialect.")
    p_col.add_argument("--host",     metavar="HOST",
                       help="Database host (default: localhost — prompted if omitted).")
    p_col.add_argument("--port",     type=int, metavar="PORT",
                       help="Database port (dialect default — prompted if omitted).")
    p_col.add_argument("--user",     metavar="USER",
                       help="Database username (prompted if omitted).")
    p_col.add_argument("--password", metavar="PASS",
                       help="Database password (prompted securely if omitted).")
    p_col.add_argument("--database", metavar="DB",
                       help="Database / catalog name (prompted if omitted).")
    p_col.add_argument("--schema",   dest="schema_name", metavar="SCHEMA",
                       help="Schema / namespace within the database (e.g. public, dbo).")
    p_col.add_argument("--tables",   default="%", metavar="PATTERN",
                       help="SQL LIKE pattern for table names (default: %% = all tables). "
                            "Shell glob * is accepted and converted to %%.")
    p_col.add_argument("--show-sql", action="store_true",
                       help="Print every SQL statement sent to the database.")
    p_col.add_argument("--analyze",  action="store_true",
                       help="Run ANALYZE (MySQL/PostgreSQL) before collecting statistics.")
    p_col.add_argument("--out-schema", default="schema.yaml", metavar="FILE",
                       help="Output path for the canonical schema YAML (default: schema.yaml).")
    p_col.add_argument("--out-stats",  default="stats.yaml",  metavar="FILE",
                       help="Output path for the statistics YAML (default: stats.yaml).")
    p_col.add_argument("-v", "--verbose", action="store_true",
                       help="Enable debug logging.")

    return root


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args   = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.cmd == "ddl":
        _cmd_ddl(args)
    elif args.cmd == "generate":
        _cmd_generate(args)
    elif args.cmd == "load":
        _cmd_load(args)
    elif args.cmd == "collect":
        _cmd_collect(args)


if __name__ == "__main__":
    main()
