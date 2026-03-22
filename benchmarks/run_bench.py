"""
statschema TPC load benchmark.

Uses the full statschema canonical pipeline:
  1. Load schema from benchmarks/schemas/{tpcc,tpch}_schema.yaml
  2. Resolve load order from FK constraints (resolve_load_order)
  3. Compute row counts from row_count_per_sf * scale_factor (resolve_row_counts)
  4. Emit dialect-specific DDL via emit_ddl()
  5. Generate rows via generate_rows() — driven by GenerationRule in the canonical model
  6. Load via load_dataframe() with the fastest strategy for the target dialect

Usage
-----
# SQLite (no setup required — good for CI sanity checks):
    python benchmarks/run_bench.py --schema tpcc --sf 1 --dialect sqlite

# PostgreSQL:
    BENCH_PG_DSN="host=localhost dbname=bench user=postgres password=secret" \\
    python benchmarks/run_bench.py --schema tpch --sf 1 --dialect postgres

# MySQL:
    BENCH_MYSQL_HOST=localhost BENCH_MYSQL_USER=root BENCH_MYSQL_PASS=secret \\
    BENCH_MYSQL_DB=bench \\
    python benchmarks/run_bench.py --schema tpcc --sf 1 --dialect mysql

# SQL Server (mssql-python preferred, pymssql fallback):
    BENCH_SQLSERVER_DSN="SERVER=localhost;DATABASE=bench;UID=sa;PWD=secret" \\
    python benchmarks/run_bench.py --schema tpcc --sf 1 --dialect sqlserver

# Oracle:
    BENCH_ORACLE_DSN="localhost/XEPDB1" BENCH_ORACLE_USER=bench BENCH_ORACLE_PASS=secret \\
    python benchmarks/run_bench.py --schema tpch --sf 1 --dialect oracle

# IBM Db2:
    BENCH_DB2_DSN="DATABASE=bench;HOSTNAME=localhost;PORT=50000;UID=db2inst1;PWD=secret" \\
    python benchmarks/run_bench.py --schema tpch --sf 1 --dialect db2

Environment variables
---------------------
BENCH_PG_DSN          PostgreSQL libpq connection string
BENCH_MYSQL_HOST/USER/PASS/DB/PORT
BENCH_SQLSERVER_DSN   mssql-python / pymssql connection string
BENCH_ORACLE_DSN/USER/PASS
BENCH_DB2_DSN         ibm_db_dbi connection string
BENCH_SQLITE_PATH     SQLite file path (default: benchmarks/results/bench.db)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure the repo root is on the path when running as a script
_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import dataclasses

from src.statschema.schema_io import load_canonical, resolve_load_order, resolve_row_counts
from src.statschema.ddl_emitter import emit_ddl
from src.statschema.model import CanonicalTableSchema
from src.statschema.row_generator import generate_rows
from src.statschema.data_loader import BatchConfig, LoadStrategy, load_dataframe

logger = logging.getLogger(__name__)

RESULTS_DIR  = Path(__file__).parent / "results"
SCHEMAS_DIR  = Path(__file__).parent / "schemas"

# ---------------------------------------------------------------------------
# Strategy selection: fastest proven path per dialect
# ---------------------------------------------------------------------------

_FASTEST: dict[str, LoadStrategy] = {
    "postgres":    LoadStrategy.BULK_COPY,   # COPY FROM STDIN — no temp file
    "cockroachdb": LoadStrategy.BULK_COPY,
    "neon":        LoadStrategy.BULK_COPY,
    "mysql":       LoadStrategy.BULK_COPY,   # LOAD DATA LOCAL INFILE
    "mariadb":     LoadStrategy.BULK_COPY,
    "sqlserver":   LoadStrategy.BULK_COPY,   # mssql-python BCP or BULK INSERT
    "db2":         LoadStrategy.BULK_COPY,   # ADMIN_CMD LOAD
    "oracle":      LoadStrategy.MULTI_ROW,   # INSERT ALL (Oracle has no COPY)
    "sqlite":      LoadStrategy.MULTI_ROW,
}


def fastest_strategy(dialect: str) -> LoadStrategy:
    return _FASTEST.get(dialect, LoadStrategy.MULTI_ROW)


# ---------------------------------------------------------------------------
# Connection factories
# ---------------------------------------------------------------------------

def connect(dialect: str) -> Any:
    """Open and return a DBAPI2 connection for the given dialect."""
    env = os.environ

    if dialect == "sqlite":
        import sqlite3
        path = env.get("BENCH_SQLITE_PATH", str(RESULTS_DIR / "bench.db"))
        return sqlite3.connect(path)

    if dialect in ("postgres", "cockroachdb", "neon"):
        import psycopg2
        dsn = env.get("BENCH_PG_DSN", "")
        if not dsn:
            raise RuntimeError("Set BENCH_PG_DSN to connect to PostgreSQL/CockroachDB/Neon.")
        return psycopg2.connect(dsn)

    if dialect in ("mysql", "mariadb"):
        import pymysql
        return pymysql.connect(
            host=env.get("BENCH_MYSQL_HOST", "localhost"),
            user=env.get("BENCH_MYSQL_USER", "root"),
            password=env.get("BENCH_MYSQL_PASS", ""),
            database=env.get("BENCH_MYSQL_DB", "bench"),
            port=int(env.get("BENCH_MYSQL_PORT", "3306")),
            local_infile=True,
            autocommit=False,
        )

    if dialect == "sqlserver":
        dsn = env.get("BENCH_SQLSERVER_DSN", "")
        if not dsn:
            raise RuntimeError("Set BENCH_SQLSERVER_DSN to connect to SQL Server.")
        try:
            import mssql_python
            return mssql_python.connect(dsn)
        except ImportError:
            import pymssql
            parts = dict(p.split("=", 1) for p in dsn.split(";") if "=" in p)
            server_str = parts.get("SERVER", "localhost")
            # pymssql requires host and port as separate arguments.
            # SQL Server DSN uses comma notation: "host,port".
            if "," in server_str:
                server_host, server_port_str = server_str.rsplit(",", 1)
                server_port = int(server_port_str.strip())
            else:
                server_host, server_port = server_str, 1433
            return pymssql.connect(
                server=server_host,
                port=server_port,
                database=parts.get("DATABASE", "master"),
                user=parts.get("UID", "sa"),
                password=parts.get("PWD", ""),
            )

    if dialect == "oracle":
        import oracledb
        dsn  = env.get("BENCH_ORACLE_DSN", "localhost/XEPDB1")
        user = env.get("BENCH_ORACLE_USER", "bench")
        pwd  = env.get("BENCH_ORACLE_PASS", "")
        return oracledb.connect(user=user, password=pwd, dsn=dsn)

    if dialect == "db2":
        import ibm_db_dbi
        dsn = env.get("BENCH_DB2_DSN", "")
        if not dsn:
            raise RuntimeError("Set BENCH_DB2_DSN to connect to IBM Db2.")
        return ibm_db_dbi.connect(dsn, "", "")

    raise ValueError(f"Unsupported dialect: {dialect!r}")


# ---------------------------------------------------------------------------
# DDL helpers
# ---------------------------------------------------------------------------

_SQLITE_TYPE_MAP = {
    "integer": "INTEGER", "long": "INTEGER",
    "string": "TEXT", "boolean": "INTEGER",
    "decimal": "REAL", "float": "REAL", "double": "REAL",
    "date": "TEXT", "timestamp": "TEXT", "timestamptz": "TEXT",
    "time": "TEXT", "timetz": "TEXT", "binary": "BLOB",
}


def _sqlite_ddl(table: CanonicalTableSchema) -> str:
    """Generate a minimal SQLite-compatible CREATE TABLE statement.

    Intentionally omits PRIMARY KEY and NOT NULL constraints so that
    benchmark data loads without uniqueness violations.  The SQLite path
    is for development / CI throughput testing only; production schemas
    are emitted by emit_ddl() for the real target dialect.
    """
    col_defs = []
    for col in table.columns:
        sql_type = _SQLITE_TYPE_MAP.get(col.type.lower(), "TEXT")
        col_defs.append(f"    {col.name} {sql_type}")
    return f"CREATE TABLE {table.name} (\n" + ",\n".join(col_defs) + "\n)"


def _strip_pk_constraints(table: CanonicalTableSchema) -> CanonicalTableSchema:
    """Return a copy of the table with PRIMARY KEY and UNIQUE constraints removed.

    Benchmark loads are ingested unconstrained for maximum throughput and to
    avoid uniqueness violations from the random generator.  Constraint-checked
    loads are outside the scope of a load-speed benchmark.
    """
    stripped_cols = [
        dataclasses.replace(col, primary_key=False, unique=False)
        for col in table.columns
    ]
    return dataclasses.replace(
        table, columns=stripped_cols, fk_constraints=None, foreign_keys=None
    )


def _emit_ddl_for_dialect(table: CanonicalTableSchema, dialect: str) -> str:
    """Emit constraint-free DDL for the target dialect, falling back to a simple
    SQLite-compatible statement for dialects not supported by emit_ddl."""
    no_pk = _strip_pk_constraints(table)
    try:
        return emit_ddl(no_pk, dialect)
    except ValueError:
        return _sqlite_ddl(table)  # SQLite fallback already strips constraints


# ---------------------------------------------------------------------------
# DDL execution
# ---------------------------------------------------------------------------

def create_tables(conn: Any, tables, dialect: str) -> None:
    """
    Drop-and-create each table in the given canonical schema list using
    dialect-specific DDL emitted by emit_ddl().
    """
    cur = conn.cursor()
    for table in tables:
        tname = table.name
        try:
            if dialect in ("postgres", "cockroachdb", "neon", "sqlite",
                           "mysql", "mariadb"):
                cur.execute(f"DROP TABLE IF EXISTS {tname}")
            elif dialect == "sqlserver":
                cur.execute(
                    f"IF OBJECT_ID('{tname}','U') IS NOT NULL DROP TABLE {tname}"
                )
            elif dialect in ("oracle", "db2"):
                # Oracle and Db2 emit DDL with uppercase table names; must drop
                # the same uppercase name to avoid stale tables with wrong schema.
                try:
                    cur.execute(f'DROP TABLE "{tname.upper()}"')
                except Exception:
                    pass
        except Exception:
            pass

        stmt = _emit_ddl_for_dialect(table, dialect)
        try:
            cur.execute(stmt)
        except Exception as exc:
            logger.warning("DDL failed for %s (continuing): %s", tname, exc)
    conn.commit()


# ---------------------------------------------------------------------------
# Progress reporting
# ---------------------------------------------------------------------------

def _fmt(n: int, elapsed: float) -> str:
    rps = n / elapsed if elapsed > 0 else 0
    return f"{n:>12,} rows  {elapsed:>7.2f}s  {rps:>10,.0f} rows/s"


# ---------------------------------------------------------------------------
# Core benchmark runner
# ---------------------------------------------------------------------------

def _query_row_offsets(
    conn: Any, tables, dialect: str
) -> dict[str, int]:
    """Return the current row count for each table, used as row_offset in append mode."""
    offsets: dict[str, int] = {}
    cur = conn.cursor()
    for table in tables:
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


def run_benchmark(
    schema: str,
    sf: float,
    dialect: str,
    strategy: LoadStrategy | None = None,
    out_dir: Path = RESULTS_DIR,
    seed: int = 42,
    append: bool = False,
) -> dict:
    """
    Run the full load benchmark for one schema × dialect combination.

    The full statschema canonical pipeline is exercised:
      load_canonical → resolve_load_order → resolve_row_counts
      → emit_ddl → generate_rows → load_dataframe

    When *append* is True the tables are not dropped/recreated; rows are
    added on top of whatever already exists.  Sequential PK columns continue
    from the current table MAX, and FK parent ranges include both existing
    and new rows.  A fresh seed is derived from the existing row totals so
    the appended data has different random values.

    Returns the result dict that was also saved to disk.
    """
    if strategy is None:
        strategy = fastest_strategy(dialect)

    # ── 1. Load canonical schema ─────────────────────────────────────────────
    yaml_path = SCHEMAS_DIR / f"{schema}_schema.yaml"
    if not yaml_path.exists():
        raise FileNotFoundError(
            f"Schema file not found: {yaml_path}.  "
            "Expected benchmarks/schemas/tpcc_schema.yaml, tpch_schema.yaml, "
            "or tpcb_schema.yaml."
        )
    tables = load_canonical(yaml_path)

    # ── 2. Resolve load order and row counts ─────────────────────────────────
    ordered_tables = resolve_load_order(tables)
    row_counts     = resolve_row_counts(tables, scale_factor=sf)

    mode_label = "APPEND" if append else "FRESH"
    print(f"\n{'='*70}")
    print(f"  statschema TPC benchmark  (canonical pipeline)  [{mode_label}]")
    print(f"  schema={schema}  sf={sf}  dialect={dialect}  strategy={strategy.value}")
    print(f"  tables: {len(ordered_tables)}  rows this batch: {sum(row_counts.values()):,}")
    print(f"{'='*70}")

    # ── 3. Connect, and optionally create tables ──────────────────────────────
    conn = connect(dialect)

    if append:
        # Query existing row counts to compute sequential PK offsets and
        # derive a seed that differs from all previous loads.
        existing_counts = _query_row_offsets(conn, ordered_tables, dialect)
        total_existing  = sum(existing_counts.values())
        effective_seed  = seed + total_existing   # guaranteed different per append round
        # FK parent ranges = existing rows + new rows (child can reference any)
        parent_row_counts_for_gen = {
            t.name: existing_counts.get(t.name, 0) + row_counts[t.name]
            for t in ordered_tables
        }
        print(f"\n  Append mode: existing rows = {total_existing:,}  "
              f"effective seed = {effective_seed}")
        ddl_s = 0.0
    else:
        existing_counts = {t.name: 0 for t in ordered_tables}
        effective_seed  = seed
        parent_row_counts_for_gen = row_counts

        print(f"\n  Creating tables...", end=" ", flush=True)
        t0 = time.perf_counter()
        create_tables(conn, ordered_tables, dialect)
        ddl_s = time.perf_counter() - t0
        print(f"done ({ddl_s:.2f}s)")

    # ── 4. Load each table ───────────────────────────────────────────────────
    table_results = {}
    total_rows    = 0
    total_load_s  = 0.0

    for table in ordered_tables:
        n_rows      = row_counts[table.name]
        row_offset  = existing_counts.get(table.name, 0)
        cols        = [col.name for col in table.columns]
        # Oracle and Db2 emit uppercase table names in DDL (e.g. "ITEM");
        # lowercase identifiers in double quotes are a different object.
        insert_name = (
            table.name.upper() if dialect in ("oracle", "db2") else table.name
        )
        print(
            f"\n  [{table.name:<14}] +{n_rows:>10,} rows "
            f"(offset={row_offset:,}) ... ",
            end="", flush=True,
        )

        t0 = time.perf_counter()
        n  = load_dataframe(
            generate_rows(
                table, n_rows,
                parent_row_counts=parent_row_counts_for_gen,
                seed=effective_seed,
                row_offset=row_offset,
            ),
            conn,
            insert_name,
            dialect,
            strategy=strategy,
            cols=cols,
            commit=True,
        )
        elapsed = time.perf_counter() - t0

        print(_fmt(n, elapsed))
        table_results[table.name] = {
            "rows": n, "load_s": round(elapsed, 4),
            "row_offset": row_offset,
        }
        total_rows   += n
        total_load_s += elapsed

    print(f"\n{'─'*70}")
    print(f"  TOTAL: {_fmt(total_rows, total_load_s)}")
    print(f"{'='*70}\n")

    # Determine driver name
    driver = type(conn).__module__.split(".")[0]

    # Git SHA (best-effort)
    try:
        git_sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_REPO_ROOT, text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        git_sha = "unknown"

    result = {
        "benchmark_id": (
            f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
            f"-{schema}-sf{sf}-{dialect}"
            + ("-append" if append else "")
        ),
        "timestamp":    datetime.now(timezone.utc).isoformat(),
        "host":         socket.gethostname(),
        "platform":     platform.platform(),
        "python":       platform.python_version(),
        "git_sha":      git_sha,
        "schema":       schema,
        "scale_factor": sf,
        "append":       append,
        "dialect":      dialect,
        "driver":       driver,
        "strategy":     strategy.value,
        "ddl_s":        round(ddl_s, 4),
        "tables":       table_results,
        "totals": {
            "rows":       total_rows,
            "load_s":     round(total_load_s, 4),
            "rows_per_s": round(total_rows / total_load_s, 0) if total_load_s > 0 else 0,
        },
    }

    # Save result
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{result['benchmark_id']}.json"
    out_path.write_text(json.dumps(result, indent=2, default=str))
    print(f"  Result saved → {out_path.relative_to(_REPO_ROOT)}\n")

    conn.close()
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="statschema TPC load benchmark",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--schema",   choices=["tpcc", "tpch", "tpcb"], default="tpcc",
                   help="TPC schema to benchmark (default: tpcc)")
    p.add_argument("--sf",       type=float, default=1.0,
                   help="Scale factor: warehouses for TPC-C, GB for TPC-H (default: 1)")
    p.add_argument("--dialect",  default="sqlite",
                   choices=["sqlite", "postgres", "cockroachdb", "neon",
                            "mysql", "mariadb", "sqlserver", "oracle", "db2"],
                   help="Target database dialect (default: sqlite)")
    p.add_argument("--strategy", default="auto",
                   choices=["auto", "singleton", "multi_row", "bulk_copy"],
                   help="Load strategy — auto selects the fastest for the dialect")
    p.add_argument("--out-dir",  default=str(RESULTS_DIR),
                   help="Directory for JSON result files (default: benchmarks/results/)")
    p.add_argument("--seed",     type=int, default=42,
                   help="Random seed for reproducible data generation (default: 42)")
    p.add_argument("--append",   action="store_true",
                   help=(
                       "Append mode: skip DROP/CREATE and add rows on top of "
                       "whatever already exists.  Sequential PKs continue from "
                       "the current table MAX; a new random seed is derived "
                       "automatically so data values differ from the first load."
                   ))
    p.add_argument("--verbose",  action="store_true",
                   help="Enable debug logging")
    return p


def main() -> None:
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
    )
    strategy = (
        None if args.strategy == "auto"
        else LoadStrategy(args.strategy)
    )
    run_benchmark(
        schema=args.schema,
        sf=args.sf,
        dialect=args.dialect,
        strategy=strategy,
        out_dir=Path(args.out_dir),
        seed=args.seed,
        append=args.append,
    )


if __name__ == "__main__":
    main()
