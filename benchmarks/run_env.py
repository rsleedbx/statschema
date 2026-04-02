"""
benchmarks/run_env.py

Captures a reproducible snapshot of the test environment and writes it as
``run_env.yaml`` alongside ``summary.txt`` in each log directory.

Inspired by Facebook Hydra's practice of saving the exact configuration used
for every run so experiments can be reproduced, compared, and debugged.

Usage (called automatically by run_matrix.py):

    from benchmarks.run_env import collect, write
    env = collect(engines=["sqlserver"], test_params={...}, dsns={...})
    write(env, log_dir)

Contents of run_env.yaml
------------------------
run_id        : log directory basename (e.g. identity-20260329-213957)
timestamp     : ISO-8601 UTC timestamp
git_commit    : HEAD SHA from git
invocation    : full sys.argv + cwd so the run can be replayed exactly
host          : OS, Python version, CPU count
tools         : Lima, Podman, QEMU version strings (best-effort)
test_config   : engines, schemas, phases, schema_workers, lpt_ordering, scale factors
engines       : per-engine DB version string + driver + any capture error

Why DB version matters
----------------------
``icr.io/db2_community/db2:latest`` silently upgraded from 11.5.9 to 12.1.4.0
between runs.  The first-boot setup time jumped from ~10 min to ~26 min and
the probe timeout needed tripling — none of this was visible without version capture.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import logging

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Version SQL per engine
# ---------------------------------------------------------------------------
# These queries return a single-row, single-column result whose first cell is
# a human-readable version string.  All are read-only and take < 10 ms.

_VERSION_SQL: dict[str, str] = {
    "postgres":    "SELECT version()",
    "cockroachdb": "SELECT version()",
    "neon":        "SELECT version()",
    "lakebase":    "SELECT version()",
    "mysql":       "SELECT version()",
    "mariadb":     "SELECT version()",
    "sqlserver":   "SELECT @@VERSION",
    "oracle":      "SELECT banner FROM v$version WHERE ROWNUM=1",
    "db2":         "SELECT SERVICE_LEVEL FROM SYSIBMADM.ENV_INST_INFO FETCH FIRST 1 ROWS ONLY",
}

# Driver package names for version reporting
def _psycopg2_package() -> str:
    """Return the installed psycopg2 distribution name (binary or source)."""
    import importlib.metadata
    for name in ("psycopg2-binary", "psycopg2"):
        try:
            importlib.metadata.version(name)
            return name
        except importlib.metadata.PackageNotFoundError:
            pass
    return "psycopg2"  # fallback; _driver_version will return None


_PSYCOPG2 = _psycopg2_package()

_DRIVER_PACKAGE: dict[str, str] = {
    "postgres":    _PSYCOPG2,
    "cockroachdb": _PSYCOPG2,
    "neon":        _PSYCOPG2,
    "lakebase":    _PSYCOPG2,
    "mysql":       "pymysql",
    "mariadb":     "pymysql",
    "sqlserver":   "mssql_python",
    "oracle":      "oracledb",
    "db2":         "ibm_db",
}


def _query_db_version(engine: str, conn) -> str | None:
    """Run a lightweight version query on an open connection."""
    sql = _VERSION_SQL.get(engine)
    if not sql:
        return None
    cur = conn.cursor()
    cur.execute(sql)
    row = cur.fetchone()
    cur.close()
    return str(row[0]).strip() if row else None


def _driver_version(package: str) -> str | None:
    """Return the installed version of a Python driver package."""
    if not package:
        return None
    import importlib.metadata
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def _tool_version(*cmd: str) -> str | None:
    """Run a CLI command and return the first line of stdout, or None."""
    try:
        out = subprocess.check_output(
            list(cmd), text=True, stderr=subprocess.STDOUT, timeout=5
        )
        return out.splitlines()[0].strip() or None
    except FileNotFoundError:
        return None  # tool not installed
    except Exception as exc:
        logger.warning("version probe %s failed: %s", cmd[0], exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def collect(
    engines: list[str],
    test_params: dict[str, Any],
    dsns: dict[str, str] | None = None,
    log_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Build a full environment snapshot dict.

    Parameters
    ----------
    engines      : engines active in this run
    test_params  : arbitrary key→value test configuration (mode, schemas, phases, …)
    dsns         : pre-built DSN strings per engine; missing ones are built via bench_config
    log_dir      : used only to derive ``run_id``
    """
    dsns = dsns or {}
    run_id = Path(log_dir).name if log_dir else "unknown"

    # Host
    host: dict[str, Any] = {
        "os":        platform.platform(),
        "python":    platform.python_version(),
        "cpu_count": os.cpu_count(),
    }

    # git commit
    try:
        git_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, timeout=5
        ).strip()
    except Exception:
        git_commit = "unknown"

    # Tool versions (best-effort; not available if tool not installed)
    tools: dict[str, str | None] = {
        "lima":  _tool_version("limactl", "--version"),
        "podman": _tool_version("podman", "--version"),
        "qemu":  _tool_version("qemu-system-x86_64", "--version"),
    }

    # Per-engine: version string + driver version
    engines_info: dict[str, Any] = {}
    for engine in engines:
        entry: dict[str, Any] = {
            "version": None,
            "driver":  _DRIVER_PACKAGE.get(engine),
            "driver_version": _driver_version(_DRIVER_PACKAGE.get(engine, "")),
            "error":   None,
        }
        dsn = dsns.get(engine)
        if not dsn:
            try:
                sys.path.insert(0, str(Path(__file__).parent.parent))
                from benchmarks.bench_config import build_dsn  # noqa: PLC0415
                dsn = build_dsn(engine)
            except Exception as exc:
                entry["error"] = f"DSN build failed: {exc}"
                engines_info[engine] = entry
                continue
        try:
            from benchmarks.dialects import get as _get_dialect  # noqa: PLC0415
            dialect = _get_dialect(engine)
            conn = dialect.connect(dsn)
            entry["version"] = _query_db_version(engine, conn)
            conn.close()
        except Exception as exc:
            entry["error"] = str(exc)
        engines_info[engine] = entry

    return {
        "run_id":     run_id,
        "timestamp":  datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit,
        "invocation": {
            "argv": sys.argv[:],
            "cwd":  os.getcwd(),
        },
        "host":        host,
        "tools":       tools,
        "test_config": test_params,
        "engines":     engines_info,
    }


def write(env: dict[str, Any], log_dir: Path | str) -> Path:
    """Write ``run_env.yaml`` into *log_dir* and return its path."""
    path = Path(log_dir) / "run_env.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        yaml.dump(env, fh, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return path
