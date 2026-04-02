"""
benchmarks/bench_config.py

Single source of truth for:
  - SCALE_FACTORS per (engine, schema)
  - DEFAULT_ENGINES and DEFAULT_SCHEMAS
  - sf_for(engine, schema)
  - build_dsn(engine) — DSN string for identity_test.py --dsn argument (loaded from YAML profile)

CLI usage (called by _common.sh to replace the sf_for bash case statement):
    python benchmarks/bench_config.py sf_for postgres tpch   → 0.1
    python benchmarks/bench_config.py dsn postgres           → host=127.0.0.1 port=5418 ...
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Canonical default database name — single source of truth.
# All dialects, YAML profiles, and shell scripts use this name.
# ---------------------------------------------------------------------------

DEFAULT_CATALOG = "statschema"

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
# identity_test.py --dsn.  Credentials are loaded from config/statschema.tpcb.yaml.

_REPO_ROOT = Path(__file__).parent.parent
_DEFAULT_BENCH_YAML = str(_REPO_ROOT / "config" / "statschema.tpcb.yaml")

# Map from short engine alias → profile name in statschema.tpcb.yaml.
# Full profile names (e.g. "tpcb_postgres14") are also accepted directly by
# run_matrix.py's _profile_name() and _dialect_for_engine() helpers without
# needing an entry here.
_ENGINE_ENV_PREFIX: dict[str, str] = {
    "postgres":    "PG",
    "cockroachdb": "CRDB",
    "mysql":       "MYSQL",
    "mariadb":     "MYSQL",
    "sqlserver":   "MSSQL",
    "oracle":      "ORA",
    "db2":         "DB2",
}

_ENGINE_PROFILE: dict[str, str] = {
    "postgres":    "tpcb_postgres",
    "neon":        "tpcb_postgres",
    "cockroachdb": "tpcb_cockroachdb",
    "mysql":       "tpcb_mysql",
    "mariadb":     "tpcb_mariadb",
    "sqlserver":   "tpcb_sqlserver",
    "oracle":      "tpcb_oracle",
    "db2":         "tpcb_db2",
    "lakebase":    "tpcb_lakebase",
}


def build_dsn(engine: str, profile_yaml: str | None = None) -> str:
    """
    Build the DSN string for identity_test.py --dsn from the YAML profile.

    Loads credentials from config/statschema.tpcb.yaml (or *profile_yaml* if
    given) so that no environment variables need to be set by the caller.
    """
    sys.path.insert(0, str(_REPO_ROOT))
    from statschema.connection_profile import load_profile, Dialect

    yaml_path    = profile_yaml or _DEFAULT_BENCH_YAML
    profile_name = _ENGINE_PROFILE.get(engine, engine)  # accept short alias or full profile name

    p       = load_profile(yaml_path, profile_name)
    dialect = p.dialect.value  # e.g. "postgres", "mysql", "sqlserver"
    host    = p.host     or "127.0.0.1"
    port    = p.port
    db      = p.database or DEFAULT_CATALOG
    user    = p.username or "postgres"
    pw      = p.password or ""

    if dialect in ("postgres", "neon", "cockroachdb", "lakebase"):
        port = port or 5432
        ssl  = " sslmode=disable" if dialect == "cockroachdb" else (
               " sslmode=require" if dialect == "lakebase" else "")
        endpoint = getattr(p, "endpoint", "") or ""
        if dialect == "lakebase" and not host and not endpoint:
            raise ValueError(f"Lakebase host not set in profile '{profile_name}'.")
        return f"host={host} port={port} dbname={db} user={user} password={pw}{ssl}"

    if dialect in ("mysql", "mariadb"):
        port = port or 3306
        return f"host={host} port={port} database={db} user={user} password={pw}"

    if dialect == "sqlserver":
        port = port or 14330
        if not pw:
            raise ValueError(
                f"SQL Server password not set in profile '{profile_name}'."
            )
        return f"server={host} port={port} database={db} user={user} password={pw}"

    if dialect == "oracle":
        port    = port or 1521
        service = db
        return f"host={host} port={port} service={service} user={user} password={pw}"

    if dialect == "db2":
        port = port or 50000
        return f"host={host} port={port} database={db} user={user} password={pw}"

    raise ValueError(f"Unknown dialect {dialect!r} for engine {engine!r}")


def engines_from_env() -> list[str]:
    """
    Return the subset of DEFAULT_ENGINES whose profile can be loaded and has
    the minimum required credentials.  Useful for auto-detecting available targets.
    """
    available = []
    for engine in DEFAULT_ENGINES:
        try:
            build_dsn(engine)
            available.append(engine)
        except Exception as exc:
            print(f"  {engine}: credentials unavailable ({exc})", file=sys.stderr)
    return available


# ---------------------------------------------------------------------------
# CLI shim for _common.sh
# ---------------------------------------------------------------------------
# Usage:
#   python benchmarks/bench_config.py sf_for <engine> <schema>
#   python benchmarks/bench_config.py dsn <engine>

def _load_dotenv() -> None:
    import os
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

    elif cmd == "ensure_catalog" and len(sys.argv) == 3:
        # Create the application catalog if it doesn't exist.
        # Only meaningful for SQL Server where the catalog must exist before
        # connecting.  Other dialects (MySQL, Postgres) create schemas on the
        # fly; this is a no-op for them.
        engine = sys.argv[2]
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from benchmarks.dialects import get as _get_dialect  # noqa: E402
        dialect = _get_dialect(engine)
        sys_db = getattr(dialect, "system_database", None)
        if sys_db is None:
            sys.exit(0)  # nothing to do
        try:
            dsn = build_dsn(engine)
            parts = dict(p.split("=", 1) for p in dsn.split() if "=" in p)
            app_catalog = parts.get("database", "statschema")
            parts["database"] = sys_db
            sys_dsn = " ".join(f"{k}={v}" for k, v in parts.items())
            conn = dialect.connect(sys_dsn)
            # SQL Server: CREATE DATABASE if not exists
            if engine == "sqlserver":
                conn.setautocommit(True)
                cur = conn.cursor()
                cur.execute(
                    f"IF NOT EXISTS (SELECT 1 FROM sys.databases WHERE name = N'{app_catalog}') "
                    f"CREATE DATABASE [{app_catalog}]"
                )
            # PostgreSQL: CREATE DATABASE if not exists
            elif engine in ("postgres", "neon", "cockroachdb"):
                conn.autocommit = True
                cur = conn.cursor()
                cur.execute(
                    f"SELECT 1 FROM pg_database WHERE datname = %s", (app_catalog,)
                )
                if not cur.fetchone():
                    cur.execute(f'CREATE DATABASE "{app_catalog}"')
            conn.close()
            sys.exit(0)
        except Exception as exc:
            print(f"ERROR ensure_catalog {engine}: {exc}", file=sys.stderr)
            sys.exit(1)

    elif cmd == "provision" and len(sys.argv) >= 3:
        # Idempotently create the app user and catalog, grant access.
        # Connects to the system catalog as DBA (using the profile credentials),
        # then delegates to dialect.provision().
        #
        # Usage:  bench_config.py provision <engine> [--port PORT]
        #
        # App user / password come from env vars:
        #   PG_APP_USER   (default: same as catalog name)
        #   PG_APP_PASS   (default: testpass)
        #
        engine = sys.argv[2]
        # Parse optional --port override from remaining args.
        _port_override: str | None = None
        _rest = sys.argv[3:]
        for _i, _a in enumerate(_rest):
            if _a == "--port" and _i + 1 < len(_rest):
                _port_override = _rest[_i + 1]
                break
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from benchmarks.dialects import get as _get_dialect  # noqa: E402
        dialect = _get_dialect(engine)
        sys_db = getattr(dialect, "system_database", None)
        if sys_db is None:
            print(f"provision: {engine} has no system_database; nothing to do")
            sys.exit(0)
        try:
            dsn = build_dsn(engine)
            parts = dict(p.split("=", 1) for p in dsn.split() if "=" in p)
            if _port_override:
                parts["port"] = _port_override
            # DSN key varies by driver: postgres/cockroachdb uses "dbname", others use "database".
            db_key         = "dbname" if "dbname" in parts else "database"
            dba_username   = parts.get("user", "")
            app_catalog    = parts.get(db_key, DEFAULT_CATALOG)
            _env_pfx       = _ENGINE_ENV_PREFIX.get(engine, engine.upper())
            app_username   = os.environ.get(f"{_env_pfx}_APP_USER", app_catalog)
            app_password   = os.environ.get(f"{_env_pfx}_APP_PASS", "testpass")
            parts[db_key]  = sys_db
            sys_dsn = " ".join(f"{k}={v}" for k, v in parts.items())
            dba_conn = dialect.connect(sys_dsn)
            dialect.provision(dba_conn, app_catalog, app_username, app_password)
            dba_conn.close()
            profile_name = _ENGINE_PROFILE.get(engine, f"tpcb_{engine}")
            host = parts.get("host", "127.0.0.1")
            port = parts.get("port", "")
            dba_password = parts.get("password", "")
            print(
                f"provision {engine}: catalog={app_catalog!r} "
                f"dba_user={dba_username!r} app_user={app_username!r} ok\n"
                f"\n"
                f"  # statschema.tpcb.yaml snippet:\n"
                f"  {profile_name}:\n"
                f"    dialect: {engine}\n"
                f"    host: {host}\n"
                f"    port: {port}\n"
                f"    catalog: {app_catalog}\n"
                f"    username: {app_username}\n"
                f"    password: ${{oc.env:{_env_pfx}_PASSWORD}}\n"
                f"    dba_username: {dba_username}\n"
                f"    dba_password: ${{oc.env:{_env_pfx}_DBA_PASSWORD}}"
            )
            sys.exit(0)
        except Exception as exc:
            print(f"ERROR provision {engine}: {exc}", file=sys.stderr)
            sys.exit(1)

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
            dialect = _get_dialect(engine)
            # Use the dialect's system_database if defined so the ping succeeds
            # even when the application catalog does not exist yet (e.g. SQL
            # Server 'master', PostgreSQL 'postgres', MySQL 'mysql').
            sys_db = getattr(dialect, "system_database", None)
            if sys_db is not None:
                parts = dict(p.split("=", 1) for p in dsn.split() if "=" in p)
                db_key = "dbname" if "dbname" in parts else "database"
                parts[db_key] = sys_db
                dsn = " ".join(f"{k}={v}" for k, v in parts.items())
            conn = dialect.connect(dsn)
            conn.close()
            sys.exit(0)
        except Exception as exc:
            print(f"ERROR: ping {engine}: {exc}", file=sys.stderr)
            sys.exit(1)

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print("Usage: bench_config.py sf_for <engine> <schema>", file=sys.stderr)
        print("       bench_config.py dsn <engine>", file=sys.stderr)
        print("       bench_config.py engines", file=sys.stderr)
        print("       bench_config.py ping <engine>", file=sys.stderr)
        print("       bench_config.py ensure_catalog <engine>", file=sys.stderr)
        print("       bench_config.py provision <engine>", file=sys.stderr)
        sys.exit(1)
