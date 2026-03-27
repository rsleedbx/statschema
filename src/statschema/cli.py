"""
statschema command-line interface — Click edition.

A DBA writes a YAML schema file and runs one of five commands — no Python required.

  statschema ddl      schema.yaml --dialect postgres
  statschema generate schema.yaml --sf 1 --format csv
  statschema load     schema.yaml --dialect postgres --dsn "host=... dbname=..."
  statschema collect  --dialect postgres --host db.host.com --catalog mydb
  statschema inject   --dialect postgres --stats stats.yaml --dsn "host=... dbname=..."

The ``main(argv)`` wrapper preserves backward compatibility with the argparse-era
test suite: it accepts an optional list of strings and calls the Click group with
``standalone_mode=False`` so the function returns normally on success and callers
can detect errors via ``SystemExit``.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import click

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
    "lakebase":    LoadStrategy.BULK_COPY,
    "mysql":       LoadStrategy.BULK_COPY,
    "mariadb":     LoadStrategy.BULK_COPY,
    "sqlserver":   LoadStrategy.BULK_COPY,
    "db2":         LoadStrategy.BULK_COPY,
    "oracle":      LoadStrategy.MULTI_ROW,
    "sqlite":      LoadStrategy.MULTI_ROW,
    "databricks":  LoadStrategy.MULTI_ROW,
}

_ALL_DIALECTS = sorted(set(SUPPORTED_DIALECTS) | set(_FASTEST))

_INJECT_FN: dict[str, str] = {
    "postgres":    "inject_stats_postgres",
    "cockroachdb": "inject_stats_postgres",
    "neon":        "inject_stats_postgres",
    "lakebase":    "inject_stats_postgres",
    "mysql":       "inject_stats_mysql",
    "mariadb":     "inject_stats_mysql",
    "sqlserver":   "inject_stats_sqlserver",
    "oracle":      "inject_stats_oracle",
    "db2":         "inject_stats_db2",
    "databricks":  "inject_stats_databricks",
}


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def _die(msg: str) -> None:
    click.echo(f"error: {msg}", err=True)
    sys.exit(1)


def _lakebase_connect(endpoint: str, host: str, dbname: str, user: str, port: int = 5432) -> Any:
    """Open a psycopg2 connection to Databricks Lakebase using OAuth."""
    try:
        from databricks.sdk import WorkspaceClient
    except ImportError:
        _die(
            "databricks-sdk is required for Lakebase connections.\n"
            "  pip install 'statschema[lakebase]'"
        )
    import psycopg2
    w = WorkspaceClient()
    credential = w.postgres.generate_database_credential(endpoint=endpoint)
    return psycopg2.connect(
        host=host, port=port, dbname=dbname,
        user=user, password=credential.token,
        sslmode="require",
        keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=5,
    )


def _connect(dialect: str, dsn: str | None) -> Any:
    """Open and return a DBAPI-2 connection from a DSN string."""
    env = os.environ

    if dialect == "sqlite":
        import sqlite3
        path = dsn or env.get("STATSCHEMA_SQLITE_PATH", ":memory:")
        return sqlite3.connect(path)

    if dialect == "lakebase":
        parts = dict(kv.split("=", 1) for kv in (dsn or "").split() if "=" in kv)
        endpoint = (parts.get("endpoint")
                    or env.get("STATSCHEMA_LAKEBASE_ENDPOINT")
                    or env.get("ENDPOINT_NAME", ""))
        host     = (parts.get("host")
                    or env.get("STATSCHEMA_LAKEBASE_HOST")
                    or env.get("PGHOST", ""))
        dbname   = (parts.get("dbname")
                    or env.get("STATSCHEMA_LAKEBASE_DB")
                    or env.get("PGDATABASE", "databricks_postgres"))
        user     = (parts.get("user")
                    or env.get("STATSCHEMA_LAKEBASE_USER")
                    or env.get("PGUSER")
                    or env.get("DATABRICKS_CLIENT_ID", ""))
        port     = int(parts.get("port") or env.get("PGPORT", "5432"))
        if not endpoint:
            _die(
                "Lakebase: provide endpoint= in --dsn or set STATSCHEMA_LAKEBASE_ENDPOINT."
            )
        if not host:
            _die("Lakebase: provide host= in --dsn or set STATSCHEMA_LAKEBASE_HOST.")
        if not user:
            _die("Lakebase: provide user= in --dsn or set STATSCHEMA_LAKEBASE_USER.")
        return _lakebase_connect(endpoint, host, dbname, user, port)

    if dialect in ("postgres", "cockroachdb", "neon"):
        import psycopg2
        conn_str = dsn or env.get("STATSCHEMA_PG_DSN", "")
        if not conn_str:
            _die("Provide --dsn or set STATSCHEMA_PG_DSN.")
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
            _die("Provide --dsn or set STATSCHEMA_SQLSERVER_DSN.")
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
            cred, oracle_dsn = dsn.rsplit("@", 1)
            oracle_user, oracle_pass = cred.split("/", 1)
        if not oracle_user:
            _die("Set STATSCHEMA_ORACLE_USER and STATSCHEMA_ORACLE_PASS.")
        return oracledb.connect(user=oracle_user, password=oracle_pass, dsn=oracle_dsn)

    if dialect == "db2":
        import ibm_db_dbi
        conn_str = dsn or env.get("STATSCHEMA_DB2_DSN", "")
        if not conn_str:
            _die("Provide --dsn or set STATSCHEMA_DB2_DSN.")
        return ibm_db_dbi.connect(conn_str, "", "")

    _die(f"Unsupported dialect {dialect!r}. Choices: {', '.join(_ALL_DIALECTS)}")


# ---------------------------------------------------------------------------
# Shared option helpers
# ---------------------------------------------------------------------------

def _build_table_map(table_preset: str | None, table_map: str | None) -> dict[str, str]:
    mapping: dict[str, str] = {}
    if table_preset:
        mapping.update(TABLE_NAME_PRESETS[table_preset])
    if table_map:
        mapping.update(parse_table_map(table_map))
    return mapping


def _rename_options(fn):
    """Decorator that adds --table-preset and --table-map to a Click command."""
    fn = click.option(
        "--table-map", "table_map",
        metavar="old=new[,old=new…]",
        default=None,
        help="Comma-separated rename pairs applied after --table-preset.",
    )(fn)
    fn = click.option(
        "--table-preset", "table_preset",
        type=click.Choice(sorted(TABLE_NAME_PRESETS)),
        default=None,
        metavar="PRESET",
        help=f"Built-in table-name preset. Available: {', '.join(sorted(TABLE_NAME_PRESETS))}.",
    )(fn)
    return fn


# ---------------------------------------------------------------------------
# Click group
# ---------------------------------------------------------------------------

@click.group()
@click.option("-v", "--verbose", is_flag=True, default=False, hidden=True,
              help="Enable debug logging.")
@click.pass_context
def cli(ctx: click.Context, verbose: bool) -> None:
    """statschema — YAML-driven schema DDL, data generation, and optimizer statistics."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


# ---------------------------------------------------------------------------
# ddl sub-command
# ---------------------------------------------------------------------------

@cli.command("ddl")
@click.argument("schema")
@click.option("--dialect", required=True, type=click.Choice(_ALL_DIALECTS),
              help="Target SQL dialect.")
@_rename_options
def cmd_ddl(schema: str, dialect: str, table_preset: str | None, table_map: str | None) -> None:
    """Emit CREATE TABLE SQL to stdout."""
    tables  = load_canonical(Path(schema))
    mapping = _build_table_map(table_preset, table_map)
    if mapping:
        tables = rename_tables(tables, mapping)
    ordered = resolve_load_order(tables)

    for table in ordered:
        try:
            stmt = emit_ddl(table, dialect)
        except ValueError as exc:
            click.echo(f"-- WARNING: {exc}", err=True)
            continue
        click.echo(stmt)
        click.echo()


# ---------------------------------------------------------------------------
# generate sub-command
# ---------------------------------------------------------------------------

@cli.command("generate")
@click.argument("schema")
@click.option("--sf",     type=float, default=1.0, show_default=True, help="Scale factor.")
@click.option("--seed",   type=int,   default=42,  show_default=True, help="Random seed.")
@click.option("--format", "fmt",
              type=click.Choice(["csv", "jsonl"]), default="jsonl", show_default=True,
              help="Output format.")
@click.option("--out-dir", "out_dir", metavar="DIR", default=None,
              help="Write one file per table here instead of stdout.")
@_rename_options
def cmd_generate(schema: str, sf: float, seed: int, fmt: str,
                 out_dir: str | None, table_preset: str | None, table_map: str | None) -> None:
    """Stream synthetic rows to stdout (CSV/JSONL) or to files."""
    tables  = load_canonical(Path(schema))
    mapping = _build_table_map(table_preset, table_map)
    if mapping:
        tables = rename_tables(tables, mapping)
    ordered = resolve_load_order(tables)
    counts  = resolve_row_counts(ordered, scale_factor=sf)

    out_path = Path(out_dir) if out_dir else None
    if out_path:
        out_path.mkdir(parents=True, exist_ok=True)

    for table in ordered:
        n    = counts[table.name]
        cols = [c.name for c in table.columns]
        rows = generate_rows(table, n, parent_row_counts=counts, seed=seed)

        if out_path:
            dest = out_path / f"{table.name}.{fmt}"
            with dest.open("w", newline="") as fh:
                _write_rows(fh, rows, cols, fmt)
            click.echo(f"  {table.name:<24} {n:>10,} rows → {dest}", err=True)
        else:
            _write_rows(sys.stdout, rows, cols, fmt)


def _write_rows(fh, rows, cols: list[str], fmt: str) -> None:
    if fmt == "csv":
        writer = csv.DictWriter(fh, fieldnames=cols)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    else:
        for row in rows:
            fh.write(json.dumps(row, default=str) + "\n")


# ---------------------------------------------------------------------------
# load sub-command
# ---------------------------------------------------------------------------

@cli.command("load")
@click.argument("schema")
@click.option("--dialect", required=True, type=click.Choice(_ALL_DIALECTS),
              help="Target database dialect.")
@click.option("--dsn",      default=None,   help="Connection string.")
@click.option("--sf",       type=float, default=1.0, show_default=True, help="Scale factor.")
@click.option("--seed",     type=int,   default=42,  show_default=True, help="Random seed.")
@click.option("--strategy",
              type=click.Choice([s.name.lower() for s in LoadStrategy]),
              default=None, help="Load strategy override (default: fastest for dialect).")
@click.option("--append", is_flag=True, default=False,
              help="Append rows; skip DROP/CREATE.")
@click.option("-v", "--verbose", is_flag=True, default=False,
              help="Enable debug logging.")
@_rename_options
def cmd_load(schema: str, dialect: str, dsn: str | None, sf: float, seed: int,
             strategy: str | None, append: bool, verbose: bool,
             table_preset: str | None, table_map: str | None) -> None:
    """Create tables and load synthetic data into a live database."""
    import dataclasses
    import time

    if verbose:
        logging.basicConfig(level=logging.DEBUG)

    tables  = load_canonical(Path(schema))
    mapping = _build_table_map(table_preset, table_map)
    if mapping:
        tables = rename_tables(tables, mapping)
    ordered = resolve_load_order(tables)
    counts  = resolve_row_counts(ordered, scale_factor=sf)

    strat = (
        LoadStrategy[strategy.upper()]
        if strategy
        else _FASTEST.get(dialect, LoadStrategy.MULTI_ROW)
    )
    conn = _connect(dialect, dsn)

    if append:
        existing     = _query_row_offsets(conn, ordered, dialect)
        total_exist  = sum(existing.values())
        eff_seed     = seed + total_exist
        par_counts   = {t.name: existing.get(t.name, 0) + counts[t.name] for t in ordered}
        click.echo(
            f"  Append mode: {total_exist:,} existing rows; effective seed = {eff_seed}",
            err=True,
        )
    else:
        existing   = {t.name: 0 for t in ordered}
        eff_seed   = seed
        par_counts = counts
        cur = conn.cursor()
        t_ddl = time.perf_counter()
        for table in ordered:
            stripped = dataclasses.replace(
                table,
                columns=[dataclasses.replace(c, primary_key=False, unique=False) for c in table.columns],
                fk_constraints=None,
                foreign_keys=None,
            )
            _drop_table(cur, table.name, dialect)
            try:
                stmt = emit_ddl(stripped, dialect)
            except ValueError:
                stmt = _simple_ddl(stripped)
            try:
                cur.execute(stmt)
            except Exception as exc:
                logger.warning("DDL failed for %s: %s", table.name, exc)
        conn.commit()
        click.echo(f"  Tables created ({time.perf_counter() - t_ddl:.1f}s)", err=True)

    total_rows = 0
    t0_all = time.perf_counter()
    for table in ordered:
        n          = counts[table.name]
        row_offset = existing.get(table.name, 0)
        cols       = [c.name for c in table.columns]
        insert_name = table.name.upper() if dialect in ("oracle", "db2") else table.name
        t0 = time.perf_counter()
        loaded = load_dataframe(
            generate_rows(table, n, parent_row_counts=par_counts, seed=eff_seed, row_offset=row_offset),
            conn, insert_name, dialect, strategy=strat, cols=cols, commit=True,
        )
        elapsed = time.perf_counter() - t0
        rps = loaded / elapsed if elapsed > 0 else 0
        total_rows += loaded
        click.echo(
            f"  {table.name:<24} +{loaded:>10,} rows (offset={row_offset:,})  "
            f"{elapsed:6.1f}s  {rps:>8,.0f} rows/s",
            err=True,
        )

    total_s = time.perf_counter() - t0_all
    click.echo(
        f"\n  TOTAL  +{total_rows:>10,} rows  {total_s:.1f}s  "
        f"{total_rows / total_s:,.0f} rows/s",
        err=True,
    )
    conn.close()


def _query_row_offsets(conn, ordered, dialect: str) -> dict[str, int]:
    offsets: dict[str, int] = {}
    cur = conn.cursor()
    for table in ordered:
        tname = table.name.upper() if dialect in ("oracle", "db2") else table.name
        try:
            cur.execute(f"SELECT COUNT(*) FROM {tname}")
            row = cur.fetchone()
            offsets[table.name] = int(row[0]) if row else 0
        except Exception:
            offsets[table.name] = 0
    return offsets


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
# collect sub-command
# ---------------------------------------------------------------------------

_DEFAULT_PORTS: dict[str, int] = {
    "mysql": 3306, "mariadb": 3306,
    "postgres": 5432, "cockroachdb": 26257, "neon": 5432, "lakebase": 5432,
    "sqlserver": 1433, "oracle": 1521, "db2": 50000, "sqlite": 0,
}

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


def _prompt_if_missing(value: str | None, label: str, default: str | None = None,
                       secret: bool = False) -> str:
    if value:
        return value
    if not sys.stdin.isatty():
        if default is not None:
            return default
        _die(f"--{label.lower().replace(' ', '-')} is required (not running interactively).")
    if secret:
        result = click.prompt(f"  {label}", default=default or "", hide_input=True)
    else:
        result = click.prompt(f"  {label}", default=default or "")
    return result or default or ""


class _LoggingCursor:
    def __init__(self, cursor, show_sql: bool) -> None:
        self._cur = cursor
        self._show = show_sql

    def execute(self, sql: str, params=None):
        if self._show:
            import re
            disp = re.sub(r"\s+", " ", sql.strip())[:300]
            click.echo(f"    → SQL: {disp}", err=True)
        return self._cur.execute(sql, params) if params is not None else self._cur.execute(sql)

    def fetchall(self):   return self._cur.fetchall()
    def fetchone(self):   return self._cur.fetchone()
    def __iter__(self):   return iter(self._cur)
    def __getattr__(self, name): return getattr(self._cur, name)


def _connect_interactive(dialect: str, host: str | None, port: int | None,
                         user: str | None, password: str | None,
                         catalog: str | None, endpoint: str | None) -> tuple[Any, str, int, str, str]:
    if dialect == "sqlite":
        import sqlite3
        path = catalog or ":memory:"
        return sqlite3.connect(path), "localhost", 0, "", path

    if dialect == "lakebase":
        env = os.environ
        ep   = _prompt_if_missing(
            endpoint or env.get("STATSCHEMA_LAKEBASE_ENDPOINT") or env.get("ENDPOINT_NAME"),
            "Lakebase endpoint (projects/.../branches/.../endpoints/...)",
        )
        h    = _prompt_if_missing(
            host or env.get("STATSCHEMA_LAKEBASE_HOST") or env.get("PGHOST"),
            "Lakebase host",
        )
        p    = int(port or os.environ.get("PGPORT", "5432"))
        db   = (catalog
                or env.get("STATSCHEMA_LAKEBASE_DB")
                or env.get("PGDATABASE", "databricks_postgres"))
        u    = _prompt_if_missing(
            user or env.get("STATSCHEMA_LAKEBASE_USER") or env.get("PGUSER") or env.get("DATABRICKS_CLIENT_ID"),
            "Service-principal client ID (PGUSER)",
        )
        conn = _lakebase_connect(ep, h, db, u, p)
        return conn, h, p, u, db

    h = _prompt_if_missing(host, "Host", "localhost")
    p = int(_prompt_if_missing(
        str(port or ""), "Port", str(_DEFAULT_PORTS.get(dialect, 5432))
    ))
    u  = _prompt_if_missing(user,     "Username", "")
    pw = _prompt_if_missing(password, "Password", secret=True)
    db = _prompt_if_missing(catalog,  "Catalog",  "")

    if dialect in ("postgres", "cockroachdb", "neon"):
        import psycopg2
        return psycopg2.connect(host=h, port=p, dbname=db, user=u, password=pw), h, p, u, db

    if dialect in ("mysql", "mariadb"):
        import pymysql
        return (pymysql.connect(host=h, port=p, user=u, password=pw,
                                database=db, local_infile=True, autocommit=True),
                h, p, u, db)

    if dialect == "sqlserver":
        try:
            import mssql_python
            cs = f"SERVER={h},{p};DATABASE={db};UID={u};PWD={pw}"
            return mssql_python.connect(cs), h, p, u, db
        except ImportError:
            import pymssql
            return pymssql.connect(server=h, port=p, database=db, user=u, password=pw), h, p, u, db

    if dialect == "oracle":
        import oracledb
        dsn = f"{h}:{p}/{db}"
        return oracledb.connect(user=u, password=pw, dsn=dsn), h, p, u, db

    if dialect == "db2":
        import ibm_db_dbi
        cs = f"DATABASE={db};HOSTNAME={h};PORT={p};UID={u};PWD={pw}"
        return ibm_db_dbi.connect(cs, "", ""), h, p, u, db

    _die(f"Unsupported dialect: {dialect!r}")


def _list_tables(cur: _LoggingCursor, dialect: str, schema: str | None, pattern: str) -> list[str]:
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
            cur.execute("SHOW TABLES LIKE %s", (like,))
    elif dialect in ("postgres", "cockroachdb", "neon", "lakebase"):
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
    from .ddl_parser import parse_ddl
    cur.execute(f"SHOW CREATE TABLE `{table}`")
    row = cur.fetchone()
    if not row:
        return None
    tables = parse_ddl(row[1], dialect="mysql")
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

    cur.execute(
        "SELECT kcu.column_name FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu "
        "     ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema "
        "WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = %s AND tc.table_name = %s "
        "ORDER BY kcu.ordinal_position",
        (pg_schema, table),
    )
    pk_cols = [r[0] for r in cur.fetchall()]

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
        cols.append(CanonicalColumn(
            name=col_name, type=ctype, not_null=(is_null == "NO"),
            primary_key=(col_name in pk_cols), length=char_len,
            precision=num_prec if ctype == "decimal" else None,
            scale=num_scale if ctype == "decimal" else None,
            default=col_def if col_def not in (None, "NULL", "null") else None,
        ))

    return CanonicalTableSchema(
        name=table, columns=cols,
        primary_key=pk_cols or None, fk_constraints=fks or None,
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
        "JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME "
        "JOIN INFORMATION_SCHEMA.CONSTRAINT_COLUMN_USAGE ccu ON tc.CONSTRAINT_NAME = ccu.CONSTRAINT_NAME "
        "WHERE tc.CONSTRAINT_TYPE = 'FOREIGN KEY' AND tc.TABLE_SCHEMA = %s AND tc.TABLE_NAME = %s "
        "ORDER BY kcu.ORDINAL_POSITION",
        (ss_schema, table),
    )
    fks = _fk_groups(cur.fetchall())

    cols = []
    for (col_name, data_type, char_len, num_prec, num_scale, is_null, col_def, is_identity) in col_rows:
        ctype = _canonical_type(data_type)
        cols.append(CanonicalColumn(
            name=col_name, type=ctype, not_null=(is_null == "NO"),
            primary_key=(col_name in pk_cols), auto_increment=bool(is_identity),
            length=char_len,
            precision=num_prec if ctype == "decimal" else None,
            scale=num_scale if ctype == "decimal" else None,
            default=col_def if col_def not in (None, "NULL", "(null)") else None,
        ))

    return CanonicalTableSchema(
        name=table, columns=cols,
        primary_key=pk_cols or None, fk_constraints=fks or None,
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
            name=col_name, type=_canonical_type(data_type), not_null=(nullable == "N"),
            primary_key=(col_name in pk_cols), length=char_len,
            precision=prec, scale=scale,
            default=default.strip() if default and default.strip() not in ("NULL",) else None,
        )
        for (col_name, data_type, char_len, prec, scale, nullable, default) in col_rows
    ]
    return CanonicalTableSchema(name=table, columns=cols, primary_key=pk_cols or None)


def _collect_schema_live(cur: _LoggingCursor, dialect: str, schema: str | None,
                         table: str) -> CanonicalTableSchema | None:
    if dialect in ("mysql", "mariadb"):
        return _collect_schema_mysql(cur, table)
    if dialect == "sqlite":
        return _collect_schema_sqlite(cur, table)
    if dialect in ("postgres", "cockroachdb", "neon", "lakebase"):
        return _collect_schema_postgres(cur, schema or "public", table)
    if dialect == "sqlserver":
        return _collect_schema_sqlserver(cur, schema or "dbo", table)
    if dialect == "oracle":
        return _collect_schema_oracle(cur, schema or "", table)
    _die(f"Schema collection not supported for dialect {dialect!r}.")


@cli.command("collect")
@click.option("--dialect",   required=True, type=click.Choice(_ALL_DIALECTS), help="Source database dialect.")
@click.option("--host",      default=None,  metavar="HOST",    help="Database host.")
@click.option("--port",      type=int, default=None, metavar="PORT", help="Database port.")
@click.option("--user",      default=None,  metavar="USER",    help="Database username.")
@click.option("--password",  default=None,  metavar="PASS",    help="Database password.")
@click.option("--catalog",   default=None,  metavar="CATALOG", help="Catalog / database name.")
@click.option("--schema",    "schema_name", default=None, metavar="SCHEMA", help="Schema / namespace.")
@click.option("--tables",    default="%",   metavar="PATTERN", help="SQL LIKE pattern (default: %%).")
@click.option("--endpoint",  default=None,  metavar="ENDPOINT_NAME", help="Lakebase endpoint resource path.")
@click.option("--show-sql",  "show_sql",    is_flag=True, default=False, help="Print SQL statements sent.")
@click.option("--analyze",   is_flag=True,  default=False, help="Run ANALYZE before collecting.")
@click.option("--out-schema", "out_schema", default="schema.yaml", show_default=True,
              metavar="FILE", help="Output schema YAML path.")
@click.option("--out-stats",  "out_stats",  default="stats.yaml",  show_default=True,
              metavar="FILE", help="Output stats YAML path.")
@click.option("--top-queries", "top_queries", type=int, default=0, metavar="N",
              help="Collect top-N queries (0 = disabled).")
@click.option("--rank-by",   "rank_by",
              type=click.Choice(["total_time", "calls", "mean_time"]),
              default="total_time", show_default=True, help="Query ranking metric.")
@click.option("--out-queries", "out_queries", default="queries.yaml", show_default=True,
              metavar="FILE", help="Output queries YAML path.")
@click.option("-v", "--verbose", is_flag=True, default=False, help="Enable debug logging.")
def cmd_collect(dialect: str, host, port, user, password, catalog, schema_name,
                tables, endpoint, show_sql, analyze, out_schema, out_stats,
                top_queries, rank_by, out_queries, verbose) -> None:
    """Connect to a live database and collect DDL + statistics into YAML."""
    import time
    from .db_stats_collector import collect_table_stats

    if verbose:
        logging.basicConfig(level=logging.DEBUG)

    conn, h, p, u, db = _connect_interactive(
        dialect, host, port, user, password, catalog, endpoint
    )
    schema_note = f"/{schema_name}" if schema_name else ""
    click.echo(f"\n  Connected to {dialect} @ {h}:{p}/{db}{schema_note} as {u}", err=True)

    raw_cur = conn.cursor()
    cur = _LoggingCursor(raw_cur, show_sql)

    if analyze:
        if dialect in ("mysql", "mariadb"):
            click.echo("  Running ANALYZE TABLE …", err=True)
        elif dialect in ("postgres", "cockroachdb", "neon"):
            click.echo("  Running ANALYZE …", err=True)
            cur.execute("ANALYZE")
            conn.commit()

    tables_found = _list_tables(cur, dialect, schema_name, tables)
    if not tables_found:
        _die(f"No tables found matching {tables!r} in {dialect}@{db}{schema_note}")
    click.echo(f"  Found {len(tables_found)} table(s) matching {tables!r}\n", err=True)

    canonical_tables: list[CanonicalTableSchema] = []
    table_stats_list = []

    for tname in tables_found:
        t0 = time.perf_counter()
        tbl = _collect_schema_live(cur, dialect, schema_name, tname)
        if tbl is None:
            click.echo(f"  ⚠  {tname}: schema not found — skipped", err=True)
            continue
        canonical_tables.append(tbl)
        n_cols = len(tbl.columns)
        n_fks  = len(tbl.fk_constraints or [])

        if analyze and dialect in ("mysql", "mariadb"):
            cur.execute(f"ANALYZE TABLE `{tname}`")

        try:
            ts = collect_table_stats(conn, tname, dialect=dialect, schema=schema_name)
            table_stats_list.append(ts)
            row_count = ts.row_count or 0
        except Exception as exc:
            logger.warning("Stats collection failed for %s: %s", tname, exc)
            row_count = 0

        elapsed = time.perf_counter() - t0
        fk_note = f", {n_fks} FK{'s' if n_fks != 1 else ''}" if n_fks else ""
        click.echo(
            f"  ✓  {tname:<30} {n_cols} cols{fk_note}, {row_count:,} rows  ({elapsed:.1f}s)",
            err=True,
        )

    if not canonical_tables:
        _die("No tables collected — nothing to write.")

    dump_schema(canonical_tables, str(out_schema))
    db_stats = DatabaseStats(tables=table_stats_list)
    from .stats_io import dump_stats as _dump_stats
    _dump_stats(db_stats, str(out_stats))

    total_rows = sum(ts.row_count or 0 for ts in table_stats_list)
    click.echo(
        f"\n  Wrote {out_schema}  ({len(canonical_tables)} tables)\n"
        f"  Wrote {out_stats}   ({len(table_stats_list)} tables, {total_rows:,} total rows)",
        err=True,
    )

    if top_queries:
        from .query_collector import collect_top_queries
        from .query_model import dump_queries
        workload = collect_top_queries(conn, dialect, n=top_queries, rank_by=rank_by, catalog=catalog)
        if workload.queries:
            dump_queries(workload, str(out_queries))
            click.echo(
                f"  Wrote {out_queries}  ({len(workload.queries)} queries, ranked by {rank_by})\n",
                err=True,
            )
        else:
            click.echo(
                f"  ⚠  No queries collected (dialect: {dialect}).\n",
                err=True,
            )


# ---------------------------------------------------------------------------
# replay sub-command
# ---------------------------------------------------------------------------

@cli.command("replay")
@click.option("--dialect",  required=True, type=click.Choice(_ALL_DIALECTS), help="Target dialect.")
@click.option("--queries",  required=True, metavar="FILE", help="Path to queries.yaml.")
@click.option("--dsn",      default=None,  help="Connection string for the target.")
@click.option("--host",     default=None,  metavar="HOST")
@click.option("--port",     type=int, default=None, metavar="PORT")
@click.option("--user",     default=None,  metavar="USER")
@click.option("--password", default=None,  metavar="PASS")
@click.option("--catalog",  default=None,  metavar="CATALOG")
@click.option("--endpoint", default=None,  metavar="ENDPOINT_NAME")
@click.option("--skip-manual-review", "skip_manual", is_flag=True, default=False)
@click.option("--show-plans", "show_plans", is_flag=True, default=False)
@click.option("-v", "--verbose", is_flag=True, default=False)
def cmd_replay(dialect: str, queries: str, dsn: str | None,
               host, port, user, password, catalog, endpoint,
               skip_manual: bool, show_plans: bool, verbose: bool) -> None:
    """Transpile and EXPLAIN collected queries against a target database."""
    from .query_model import load_queries
    from .query_replayer import print_replay_report, replay_queries

    if verbose:
        logging.basicConfig(level=logging.DEBUG)

    queries_path = Path(queries)
    if not queries_path.exists():
        _die(f"queries file not found: {queries_path}")

    workload = load_queries(queries_path)
    click.echo(
        f"\n  Loaded {len(workload.queries)} queries (source: {workload.source_dialect})\n"
        f"  Target: {dialect}\n",
        err=True,
    )

    if dsn:
        conn = _connect(dialect, dsn)
        h, p, u, db = "?", 0, "?", "?"
    else:
        conn, h, p, u, db = _connect_interactive(dialect, host, port, user, password, catalog, endpoint)
        click.echo(f"  Connected to {dialect} @ {h}:{p}/{db} as {u}\n", err=True)

    results = replay_queries(conn, workload, target_dialect=dialect, skip_manual_review=skip_manual)

    if show_plans:
        print_replay_report(results)
    else:
        ok   = sum(1 for r in results if r.success)
        fail = len(results) - ok
        skip = sum(1 for r in results if not r.success and r.error and "Skipped" in (r.error or ""))
        click.echo(
            f"  Results: {ok} ok / {fail - skip} errors / {skip} skipped ({len(results)} total)\n",
            err=True,
        )
        for r in results:
            if not r.success:
                status = "↷" if r.error and "Skipped" in r.error else "✗"
                click.echo(f"  {status}  {r.query_id}  {r.error}", err=True)


# ---------------------------------------------------------------------------
# inject sub-command
# ---------------------------------------------------------------------------

@cli.command("inject")
@click.option("--dialect", required=True, type=click.Choice(list(_INJECT_FN)), help="Target dialect.")
@click.option("--dsn",     default=None,  help="Connection string.")
@click.option("--stats",   required=True, metavar="FILE", help="Path to stats.yaml.")
@click.option("--schema",  "schema_name", default=None, metavar="SCHEMA")
@click.option("--tables",  default=None,  metavar="TABLE[,TABLE…]",
              help="Comma-separated table names to inject (default: all).")
@click.option("--show-sql", "show_sql", is_flag=True, default=False)
@click.option("-v", "--verbose", is_flag=True, default=False)
def cmd_inject(dialect: str, dsn: str | None, stats: str, schema_name: str | None,
               tables: str | None, show_sql: bool, verbose: bool) -> None:
    """Inject collected statistics into a target database optimizer."""
    import importlib
    from .stats_io import load_stats as _load_stats

    if verbose:
        logging.basicConfig(level=logging.DEBUG)

    fn_name = _INJECT_FN.get(dialect)
    if fn_name is None:
        _die(f"Stats injection not supported for dialect {dialect!r}.")

    inject_fn = getattr(importlib.import_module(".stats_injector", "statschema"), fn_name)

    db_stats = _load_stats(stats)
    only_tbls = {t.strip() for t in tables.split(",")} if tables else None
    tbl_list  = [ts for ts in db_stats.tables if not only_tbls or ts.name in only_tbls]
    if not tbl_list:
        _die(f"No matching tables found in {stats}.")

    conn = _connect(dialect, dsn)

    click.echo(f"\n  Injecting stats from {stats} → {dialect}", err=True)
    if only_tbls:
        click.echo(f"  Tables: {', '.join(sorted(only_tbls))}", err=True)

    ok = err = 0
    for ts in tbl_list:
        try:
            kwargs: dict = {}
            if dialect in ("postgres", "cockroachdb", "neon") and schema_name:
                kwargs["schema"] = schema_name
            elif dialect in ("mysql", "mariadb") and schema_name:
                kwargs["database"] = schema_name
            if show_sql:
                click.echo(f"  → injecting {ts.name} ({ts.row_count or 0:,} rows) …", err=True)
            result = inject_fn(conn, ts, **kwargs)
            status = getattr(result, "status", "ok")
            click.echo(f"  ✓  {ts.name:<30} {status}", err=True)
            ok += 1
        except Exception as exc:
            click.echo(f"  ✗  {ts.name:<30} {exc}", err=True)
            err += 1

    conn.close()
    msg = f"\n  {ok} table(s) injected"
    if err:
        msg += f" — {err} error(s)"
    click.echo(msg, err=True)
    if err:
        sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point — backward-compatible with argparse-era tests
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    """
    Invoke the Click CLI.

    Backward-compatible with ``main(["ddl", "schema.yaml", "--dialect", "postgres"])``
    style calls from tests and scripts.
    """
    try:
        cli.main(args=argv, standalone_mode=False, prog_name="statschema")
    except click.UsageError as exc:
        click.echo(f"Error: {exc.format_message()}", err=True)
        sys.exit(2)
    except click.ClickException as exc:
        exc.show()
        sys.exit(exc.exit_code)
    except SystemExit:
        raise


if __name__ == "__main__":
    main()
