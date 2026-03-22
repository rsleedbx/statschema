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

from .schema_io import load_canonical, resolve_load_order, resolve_row_counts
from .ddl_emitter import emit_ddl, SUPPORTED_DIALECTS
from .row_generator import generate_rows
from .data_loader import LoadStrategy, load_dataframe

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

def _cmd_ddl(args: argparse.Namespace) -> None:
    """Emit CREATE TABLE SQL for every table in the YAML schema."""
    tables = load_canonical(Path(args.schema))
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
# Argument parser
# ---------------------------------------------------------------------------

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
    p_load.add_argument("-v", "--verbose", action="store_true",
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


if __name__ == "__main__":
    main()
