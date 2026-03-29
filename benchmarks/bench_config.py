"""
benchmarks/bench_config.py

Single source of truth for:
  - SCALE_FACTORS per (engine, schema)
  - DEFAULT_ENGINES and DEFAULT_SCHEMAS
  - sf_for(engine, schema)
  - build_dsn(engine, env) — DSN string for identity_test.py --dsn argument

This module has no external dependencies beyond the standard library and
python-dotenv (optional), so it can be imported in shell scripts, pytest
fixtures, run_matrix.py, and Databricks notebooks alike.

CLI usage (called by _common.sh to replace the sf_for bash case statement):
    python benchmarks/bench_config.py sf_for postgres tpch   → 0.1
    python benchmarks/bench_config.py dsn postgres           → host=127.0.0.1 port=5418 ...
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Scale factors
# ---------------------------------------------------------------------------
# Smaller SFs are used for QEMU-hosted engines (SQL Server, Oracle, DB2) to
# keep identity test runtimes under ~60 s per schema.

SCALE_FACTORS: dict[str, dict[str, float]] = {
    "postgres":    {"tpcb": 1, "tpcc": 1, "tpch": 0.1,  "tpcdi": 5,   "tpcds": 0.01, "tpce": 0.01},
    "cockroachdb": {"tpcb": 1, "tpcc": 1, "tpch": 0.1,  "tpcdi": 5,   "tpcds": 0.01, "tpce": 0.01},
    "neon":        {"tpcb": 1, "tpcc": 1, "tpch": 0.1,  "tpcdi": 5,   "tpcds": 0.01, "tpce": 0.01},
    "lakebase":    {"tpcb": 1, "tpcc": 1, "tpch": 0.1,  "tpcdi": 5,   "tpcds": 0.01, "tpce": 0.01},
    "mysql":       {"tpcb": 1, "tpcc": 1, "tpch": 0.01, "tpcdi": 1,   "tpcds": 0.01, "tpce": 0.01},
    "mariadb":     {"tpcb": 1, "tpcc": 1, "tpch": 0.01, "tpcdi": 1,   "tpcds": 0.01, "tpce": 0.01},
    "db2":         {"tpcb": 1, "tpcc": 1, "tpch": 0.01, "tpcdi": 1,   "tpcds": 0.01, "tpce": 0.01},
    "sqlserver":   {"tpcb": 1, "tpcc": 1, "tpch": 0.01, "tpcdi": 1,   "tpcds": 0.01, "tpce": 0.1},
    "oracle":      {"tpcb": 1, "tpcc": 1, "tpch": 0.01, "tpcdi": 1,   "tpcds": 0.01, "tpce": 0.1},
}

# Fallback for any unrecognised engine (use postgres-class SFs)
_DEFAULT_SF = SCALE_FACTORS["postgres"]

DEFAULT_ENGINES: list[str] = [
    "postgres", "cockroachdb", "mysql", "sqlserver", "oracle", "db2",
]
DEFAULT_SCHEMAS: list[str] = [
    "tpcb", "tpcc", "tpch", "tpcdi", "tpcds", "tpce",
]

BENCH_SCHEMAS: list[str] = ["tpcc", "tpch"]   # schemas used by run_all_bench.py
BENCH_TPCC_SF: float = 1.0
BENCH_TPCH_SF: float = 0.1


def sf_for(engine: str, schema: str) -> float:
    """Return the scale factor for a given engine × schema combination."""
    return SCALE_FACTORS.get(engine, _DEFAULT_SF)[schema]


# ---------------------------------------------------------------------------
# DSN builders
# ---------------------------------------------------------------------------
# build_dsn() produces the space-separated key=value format consumed by
# identity_test.py --dsn.  Environment variables mirror those in .env.example.

def build_dsn(engine: str, env: dict[str, str] | None = None) -> str:
    """
    Build the DSN string for identity_test.py --dsn from environment variables.

    Falls back to local-dev defaults that match the Podman container setup
    documented in docs/local-databases.md.
    """
    e: dict[str, Any] = env if env is not None else os.environ

    if engine in ("postgres", "neon"):
        host = e.get("PG_HOST", "127.0.0.1")
        port = e.get("PG18_PORT", e.get("PG16_PORT", "5418"))
        db   = e.get("PG_DB",   "postgres")
        user = e.get("PG_USER", "postgres")
        pw   = e.get("PG18_PASS", e.get("PG_PASSWORD", "postgres"))
        return f"host={host} port={port} dbname={db} user={user} password={pw}"

    if engine == "cockroachdb":
        host = e.get("CRDB_HOST", "127.0.0.1")
        port = e.get("CRDB_SINGLE_PORT", "26257")
        db   = e.get("CRDB_DB", "defaultdb")
        user = e.get("CRDB_USER", "root")
        return f"host={host} port={port} dbname={db} user={user} sslmode=disable"

    if engine in ("mysql", "mariadb"):
        host = e.get("MYSQL_HOST", "127.0.0.1")
        port = e.get("MYSQL8_PORT", "3384") if engine == "mysql" else e.get("MARIADB_PORT", "3311")
        db   = e.get("MYSQL_DB",   "testdb")
        user = e.get("MYSQL_USER", "root")
        pw   = e.get("MYSQL_ROOT_PASS", e.get("MYSQL_PASS", "testpass"))
        return f"host={host} port={port} database={db} user={user} password={pw}"

    if engine == "sqlserver":
        host = e.get("SQLSERVER_HOST", "127.0.0.1")
        port = e.get("SQLSERVER_PORT", "14330")
        db   = e.get("SQLSERVER_DB", "master")
        pw   = e.get("SQLSERVER_PASS", "")
        if not pw:
            raise ValueError(
                "SQLSERVER_PASS is not set. "
                "Add it to .env or pass it as an environment variable."
            )
        return f"server={host} port={port} database={db} user=sa password={pw}"

    if engine == "oracle":
        host    = e.get("ORACLE_HOST",    "127.0.0.1")
        port    = e.get("ORACLE_PORT",    "1521")
        service = e.get("ORACLE_SERVICE", "XE")
        user    = e.get("ORACLE_USER",    "system")
        pw      = e.get("ORACLE_PASS",    "oracle")
        return f"host={host} port={port} service={service} user={user} password={pw}"

    if engine == "db2":
        host = e.get("DB2_HOST",     "127.0.0.1")
        port = e.get("DB2_PORT",     "50000")
        db   = e.get("DB2_DATABASE", "testdb")
        user = e.get("DB2_USER",     "db2inst1")
        pw   = e.get("DB2_PASS",     "testpass")
        return f"host={host} port={port} database={db} user={user} password={pw}"

    if engine == "lakebase":
        host  = e.get("LAKEBASE_HOST", "")
        port  = e.get("LAKEBASE_PORT", "5432")
        db    = e.get("LAKEBASE_DB",   "hive_metastore")
        user  = e.get("LAKEBASE_USER", "token")
        token = e.get("DATABRICKS_TOKEN", e.get("LAKEBASE_TOKEN", ""))
        if not host:
            raise ValueError("LAKEBASE_HOST is not set.")
        return f"host={host} port={port} dbname={db} user={user} password={token} sslmode=require"

    raise ValueError(f"Unknown engine: {engine!r}")


def engines_from_env(env: dict[str, str] | None = None) -> list[str]:
    """
    Return the subset of DEFAULT_ENGINES whose required credentials are present
    in the environment.  Useful for auto-detecting available targets in notebooks.
    """
    e = env if env is not None else os.environ
    available = []
    for engine in DEFAULT_ENGINES:
        try:
            build_dsn(engine, e)
            available.append(engine)
        except ValueError:
            pass
    return available


# ---------------------------------------------------------------------------
# CLI shim for _common.sh
# ---------------------------------------------------------------------------
# Usage:
#   python benchmarks/bench_config.py sf_for <engine> <schema>
#   python benchmarks/bench_config.py dsn <engine>

def _load_dotenv() -> None:
    repo_root = Path(__file__).parent.parent
    env_file = repo_root / ".env"
    if env_file.exists():
        try:
            from dotenv import load_dotenv as _ld
            _ld(env_file)
        except ImportError:
            # python-dotenv not available; parse manually
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())


if __name__ == "__main__":
    _load_dotenv()
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "sf_for" and len(sys.argv) == 4:
        engine, schema = sys.argv[2], sys.argv[3]
        print(sf_for(engine, schema))

    elif cmd == "dsn" and len(sys.argv) == 3:
        engine = sys.argv[2]
        try:
            print(build_dsn(engine))
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)

    elif cmd == "engines":
        print(",".join(engines_from_env()))

    elif cmd == "ping" and len(sys.argv) == 3:
        # Exit 0 if the engine accepts a real connection, 1 otherwise.
        # Used by _common.sh wait_db() to distinguish "port open" from "DB ready".
        engine = sys.argv[2]
        try:
            dsn = build_dsn(engine)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        try:
            sys.path.insert(0, str(Path(__file__).parent.parent))
            from benchmarks.dialects import get as _get_dialect  # noqa: E402
            conn = _get_dialect(engine).connect(dsn)
            conn.close()
            sys.exit(0)
        except Exception:
            sys.exit(1)

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print("Usage: bench_config.py sf_for <engine> <schema>", file=sys.stderr)
        print("       bench_config.py dsn <engine>", file=sys.stderr)
        print("       bench_config.py engines", file=sys.stderr)
        print("       bench_config.py ping <engine>", file=sys.stderr)
        sys.exit(1)
