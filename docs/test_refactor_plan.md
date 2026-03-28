# Test refactoring plan

**Goal:** efficient, effective, reusable tests aligned to `docs/test_objectives.md`, runnable from a local terminal, a CI job, or a Databricks notebook with no code changes.

---

## Current state

### .sh vs .py line counts

| File | Lines | Purpose |
|---|---|---|
| `benchmarks/_common.sh` | 315 | Shared library: DB startup, DSN builders, `sf_for`, `run_check_run` |
| `benchmarks/run_identity.sh` | 255 | Identity test matrix orchestrator |
| `benchmarks/run_lakebase_target.sh` | 259 | Cross-engine Lakebase target test orchestrator |
| `benchmarks/run_bench.sh` | 91 | Benchmark test entry point |
| **Total .sh** | **920** | |
| `benchmarks/identity_test.py` | 2483 | Core identity test pipeline |
| `benchmarks/run_bench.py` | 592 | Single-schema load benchmark |
| `benchmarks/run_all_bench.py` | 320 | TPC-C/TPC-H matrix benchmark |
| `benchmarks/run_tpcb_bench.py` | 347 | TPC-B / pgbench benchmark |
| `benchmarks/check_run.py` | 387 | Row-count verification |
| `benchmarks/tpc_generators.py` | 556 | Data generation helpers |
| `benchmarks/tpc_schemas.py` | 357 | TPC schema definitions |
| **Total .py** | **5042** | |

### What is currently duplicated between .sh and .py

| Logic | In .sh | In .py | Problem |
|---|---|---|---|
| Scale factors | `_common.sh:sf_for()` | `run_bench.py` `run_all_bench.py` | Three places to update when a scale factor changes |
| DSN construction | `_common.sh:dsn_*()` | `check_run.py:_parse_dsn()` | Different format; .sh side depends on `limactl shell` for SQL Server password |
| Engine matrix | `run_identity.sh` (comma-string) | `run_all_bench.py` (Python list) | Duplicated, not shared |
| Logging | `info/warn/die` in .sh | `logging` in Python | Two logging systems; notebook only sees Python's |

### What belongs in .sh vs .py

| Concern | Right place | Why |
|---|---|---|
| `podman start/run` container management | `.sh` | Requires local `podman` CLI; not available in notebooks |
| `limactl start/shell` Lima VM management | `.sh` | Requires local `limactl`; not available in notebooks |
| SQL Server password extraction from Lima VM log | `.sh` (or `.env`) | `limactl shell` is shell-only |
| Port-availability polling (`nc -z`) | `.sh` or Python | Trivial either way; keep in `.sh` alongside startup |
| Scale factors, engine matrix, DSN format | **Python** | Pure data/config — no system dependency; needed in notebooks |
| Engine × schema loop, result aggregation | **Python** | Same loop runs from shell, CI, or notebook |
| Row-count verification | **Python** — already done | `check_run.py` already handles this |
| Stats injection, data loading, EXPLAIN scoring | **Python** — already done | All in `identity_test.py`, `run_bench.py` |

---

## The refactoring target

The shell scripts shrink to three responsibilities only:

```
run_identity.sh
  ├── parse CLI arguments
  ├── start_databases()          ← podman + limactl (stays in .sh)
  └── python benchmarks/run_matrix.py --mode identity ...  ← everything else

run_lakebase_target.sh
  ├── parse CLI arguments
  ├── start_databases()
  └── python benchmarks/run_matrix.py --mode lakebase ...

run_bench.sh
  ├── parse CLI arguments
  ├── start_databases()
  └── python benchmarks/run_matrix.py --mode bench ...
```

`run_matrix.py` becomes the single Python entry point that any environment can call directly — shell, CI, or notebook — without needing a shell wrapper at all.

---

## Phase 1 — Extract shared Python config (highest ROI, 1 day)

Create `benchmarks/bench_config.py` with the three pieces of logic duplicated across .sh and .py:

```python
# benchmarks/bench_config.py

SCALE_FACTORS: dict[str, dict[str, float]] = {
    "postgres":    {"tpcb": 1, "tpcc": 1, "tpch": 0.1, "tpcdi": 5,   "tpcds": 0.01, "tpce": 0.01},
    "cockroachdb": {"tpcb": 1, "tpcc": 1, "tpch": 0.1, "tpcdi": 5,   "tpcds": 0.01, "tpce": 0.01},
    "neon":        {"tpcb": 1, "tpcc": 1, "tpch": 0.1, "tpcdi": 5,   "tpcds": 0.01, "tpce": 0.01},
    "lakebase":    {"tpcb": 1, "tpcc": 1, "tpch": 0.1, "tpcdi": 5,   "tpcds": 0.01, "tpce": 0.01},
    "mysql":       {"tpcb": 1, "tpcc": 1, "tpch": 0.01,"tpcdi": 1,   "tpcds": 0.01, "tpce": 0.01},
    "db2":         {"tpcb": 1, "tpcc": 1, "tpch": 0.01,"tpcdi": 1,   "tpcds": 0.01, "tpce": 0.01},
    "sqlserver":   {"tpcb": 1, "tpcc": 1, "tpch": 0.01,"tpcdi": 1,   "tpcds": 0.01, "tpce": 0.1},
    "oracle":      {"tpcb": 1, "tpcc": 1, "tpch": 0.01,"tpcdi": 1,   "tpcds": 0.01, "tpce": 0.1},
}

DEFAULT_ENGINES = ["postgres", "cockroachdb", "mysql", "sqlserver", "oracle", "db2"]
DEFAULT_SCHEMAS = ["tpcb", "tpcc", "tpch", "tpcdi", "tpcds", "tpce"]

def sf_for(engine: str, schema: str) -> float:
    return SCALE_FACTORS.get(engine, SCALE_FACTORS["postgres"])[schema]

def build_dsn(engine: str, env: dict | None = None) -> str:
    """Build a DSN string for the given engine from environment variables."""
    e = env or os.environ
    ...
```

Update `_common.sh:sf_for()` to call `python benchmarks/bench_config.py sf_for <engine> <schema>` so there is one source of truth. Remove the duplicated scale factor tables from `run_bench.py` and `run_all_bench.py`.

**Files changed:** `bench_config.py` (new), `_common.sh`, `run_bench.py`, `run_all_bench.py`
**Gain:** scale factors defined in one place; `bench_config.py` is importable from a notebook

---

## Phase 2 — Python matrix runner (2 days)

Create `benchmarks/run_matrix.py` — a Python CLI that executes the engine × schema matrix. This is the logic currently split between `run_identity.sh`, `run_lakebase_target.sh`, and `run_bench.sh`.

```python
# benchmarks/run_matrix.py
#
# Runs the identity / lakebase / bench matrix.
# Callable from shell, CI, or a Databricks notebook.
#
# Usage:
#   python benchmarks/run_matrix.py identity \
#       --engines postgres,mysql --schemas tpcc,tpch \
#       --log-dir /tmp/logs
#
#   python benchmarks/run_matrix.py lakebase \
#       --source-engines postgres,mysql --schemas tpcb
#
#   python benchmarks/run_matrix.py bench \
#       --engines cockroachdb,mysql

import argparse
import concurrent.futures
from benchmarks.bench_config import sf_for, DEFAULT_ENGINES, DEFAULT_SCHEMAS

def run_identity_matrix(engines, schemas, dsns, skip_load, log_dir, ...):
    """Run identity test for each engine × schema.  Parallelises by engine."""
    with concurrent.futures.ThreadPoolExecutor() as pool:
        futures = {pool.submit(run_engine, e, schemas, dsns[e], ...): e for e in engines}
        ...

def run_engine(engine, schemas, dsn, skip_load, log_dir, ...):
    """Run all schemas sequentially for one engine."""
    for schema in schemas:
        run_one(engine, schema, dsn, sf_for(engine, schema), ...)

def run_one(engine, schema, dsn, sf, ...):
    """Run identity_test.py for one (engine, schema) pair."""
    ...
```

The shell scripts become:

```bash
# run_identity.sh — after phase 2
set -euo pipefail
source "$(dirname "$0")/_common.sh"
load_dotenv
find_python

[[ $SKIP_SETUP -eq 0 ]] && start_databases "$ENGINES"

"$VENV" benchmarks/run_matrix.py identity \
    --engines  "$ENGINES" \
    --schemas  "$SCHEMAS" \
    --log-dir  "$LOG_DIR" \
    ${SKIP_LOAD:+--skip-load} \
    ${NO_EXT_STATS_FLAG:+--no-extended-stats}
```

The shell file shrinks from 255 lines to ~40. All logic is in Python and importable from a notebook:

```python
# In a Databricks notebook
from benchmarks.run_matrix import run_identity_matrix
from benchmarks.bench_config import DEFAULT_ENGINES, DEFAULT_SCHEMAS

run_identity_matrix(
    engines=["postgres", "mysql"],
    schemas=["tpcb", "tpcc"],
    dsns={"postgres": "host=... dbname=...", "mysql": "host=..."},
    log_dir="/tmp/identity-logs",
)
```

**Files changed:** `run_matrix.py` (new), `run_identity.sh` (trimmed), `run_lakebase_target.sh` (trimmed), `run_bench.sh` (trimmed)
**Gain:** full matrix runner callable from notebook; shell files reduced to ~40 lines each

---

## Phase 3 — Shared test fixtures (1 day)

Create `tests/conftest.py` with session-scoped pytest fixtures for every engine, replacing the `_get_connection()` function copied into each of the 8 `test_live_*.py` files.

```python
# tests/conftest.py
import pytest

@pytest.fixture(scope="session")
def mysql8_conn():
    """Session-scoped MySQL 8 connection. Auto-skips if unreachable."""
    pymysql = pytest.importorskip("pymysql")
    try:
        conn = pymysql.connect(host=_HOST, port=_PORT_8, ...)
        yield conn
        conn.close()
    except Exception as exc:
        pytest.skip(f"MySQL 8 unreachable: {exc}")

@pytest.fixture(scope="session")
def pg16_conn(): ...

@pytest.fixture(scope="session")
def crdb_conn(): ...

# ... oracle_conn, db2_conn, sqlserver_conn, mariadb_conn, neon_conn
```

Also create `tests/live_helpers.py` with a `LiveDbHelper` class so per-engine SQL differences are in one place:

```python
# tests/live_helpers.py
class LiveDbHelper:
    def __init__(self, conn, dialect: str): ...
    def execute(self, sql: str): ...
    def fetchall(self, sql: str) -> list[tuple]: ...
    def introspect(self, table: str) -> dict[str, dict]: ...
    def drop(self, table: str): ...
    def quote(self, name: str) -> str: ...  # dialect-aware quoting
```

Strip the duplicated `_get_connection`, `_execute`, `_fetchall`, `_introspect` from all 8 live test files.

**Files changed:** `conftest.py` (new), `live_helpers.py` (new), all 8 `test_live_*.py` files trimmed
**Lines removed:** ~400
**Gain:** consistent auto-skip; one edit point for connection logic; helpers available in notebook via `import`

---

## Phase 4 — Parametrized type coverage (2 days)

Create `tests/test_live_type_coverage.py` — one file parametrized across all engines, replacing the `TestTypeRoundTrips` class duplicated in mysql, pg, cockroachdb, and mariadb.

```python
# tests/test_live_type_coverage.py
import pytest
from tests.live_helpers import LiveDbHelper
from src.statschema import emit_ddl, parse_ddl

ENGINES = [
    pytest.param("mysql",      fixture="mysql8_conn",    id="mysql8"),
    pytest.param("postgres",   fixture="pg16_conn",      id="pg16"),
    pytest.param("cockroachdb",fixture="crdb_conn",      id="crdb"),
    pytest.param("mariadb",    fixture="mariadb_conn",   id="mariadb"),
    pytest.param("oracle",     fixture="oracle_conn",    id="oracle"),
    pytest.param("db2",        fixture="db2_conn",       id="db2"),
    pytest.param("sqlserver",  fixture="sqlserver_conn", id="sqlserver"),
]

@pytest.mark.parametrize("engine,conn_fixture", ENGINES)
class TestTypeRoundTripAcrossEngines:
    def test_integer_types(self, engine, conn_fixture, request): ...
    def test_string_types(self, engine, conn_fixture, request): ...
    def test_decimal_types(self, engine, conn_fixture, request): ...
    def test_boolean(self, engine, conn_fixture, request): ...
    def test_timestamp_types(self, engine, conn_fixture, request): ...
    def test_binary_types(self, engine, conn_fixture, request): ...
    def test_fk_constraint(self, engine, conn_fixture, request): ...
    def test_not_null_and_pk(self, engine, conn_fixture, request): ...
```

Engine-specific tests (MySQL `UNSIGNED`, PG `SERIAL`/`ARRAY`, CRDB `LOCALITY`) stay in the per-engine files. Only the shared cross-engine property tests move.

**Files changed:** `test_live_type_coverage.py` (new), 4 live test files trimmed
**Lines removed:** ~600
**Gain:** adding a new canonical type requires editing one file; a new engine is one new fixture + one new `pytest.param`

---

## Phase 5 — Non-Spark distribution fidelity (1 day)

Objective 4 (distribution fidelity) currently requires Spark, blocking it from the fast path. Split `test_live_synth.py`:

```
tests/
  test_live_distribution.py   ← NEW: pandas path only — null_match, range_covered, ndistinct_w2x
  test_live_synth.py          ← TRIMMED: Spark-only data generation tests
```

`test_live_distribution.py` uses `build_rows_from_canonical()` (pure Python, no Spark), loads 10,000 rows, runs `collect_table_stats`, and asserts the three sub-metrics:

```python
# tests/test_live_distribution.py
def test_null_fraction_preserved(pg16_conn):
    ...
    stats_v1 = collect_table_stats(conn, "table_v1")
    stats_v2 = collect_table_stats(conn, "table_v2")
    for col in stats_v1.columns:
        assert abs(col.null_fraction - stats_v2[col.name].null_fraction) <= 0.02

def test_range_covered(pg16_conn): ...

def test_ndistinct_within_2x(pg16_conn): ...
```

**Makefile target:**

```makefile
test-distribution:
    .venv_test/bin/pytest tests/test_live_distribution.py -v
```

**Gain:** objective 4 runnable in ~2 minutes without Spark; moves distribution fidelity into the standard `make test-live-*` group

---

## Phase 6 — Step-level CLI flags (1 day)

Add `--phases` to `identity_test.py` (or `run_matrix.py` after Phase 2) so individual pipeline steps are re-runnable:

```bash
# Re-run only the row-count validation on an already-loaded database
python benchmarks/run_matrix.py identity --phases validate_source,validate_target \
    --engines postgres --schemas tpcc

# Re-score from saved JSON — no live DB required
python benchmarks/run_matrix.py identity --phases score \
    --result benchmarks/results/20260327-135240-identity-tpcc-sf1.0-postgres.json

# Just emit DDL and create tables, skip data load
python benchmarks/run_bench.py --step ddl_only --schema tpch --dialect oracle --dsn "..."
```

Available phases: `db_startup`, `load_source`, `explain_source`, `collect_stats`, `load_target`, `explain_target`, `score`, `validate`.

**Gain:** any failing step re-runnable in isolation; `score` re-runnable from saved JSON with zero DB access; notebook workflows can call individual phases

---

## Phase 7 — Objective-aligned markers and make targets (2 hours)

Add `pytest.mark` decorators aligned to `test_objectives.md` and corresponding `make` targets so any objective can be validated independently.

**`pytest.ini` additions:**

```ini
[pytest]
markers =
    obj1: Schema correctness
    obj2: Source type coverage
    obj3: Row count correctness
    obj4: Distribution fidelity
    obj5: Stats injection fidelity
    obj6: Semantic correctness
    obj7: Plan fidelity
    obj8: Cross-engine portability
    obj9: Benchmark compatibility
```

**`Makefile` additions:**

```makefile
test-obj1:  .venv_test/bin/pytest tests/test_ddl_roundtrip.py tests/test_coverage_gaps.py -v
test-obj2:  .venv_test/bin/pytest tests/test_live_type_coverage.py -v
test-obj3:  .venv_test/bin/python benchmarks/check_run.py benchmarks/results/*.json
test-obj4:  .venv_test/bin/pytest tests/test_live_distribution.py -v
test-obj5:  .venv_test/bin/pytest tests/test_live_stats_transpiler.py tests/test_live_stats_minmax.py -v
test-obj6:  .venv_test/bin/pytest tests/test_semantic_hints.py tests/test_live_mautic.py tests/test_live_gitea.py -v
test-obj7:  bash benchmarks/run_identity.sh --skip-setup
test-obj8:  bash benchmarks/run_lakebase_target.sh --skip-setup
test-obj9:  bash benchmarks/run_bench.sh --skip-setup
```

---

## What stays in .sh after all phases

```
_common.sh (315 → ~80 lines)
├── load_dotenv              — reads .env; shell-side convenience
├── find_python              — locates .venv_test
├── info / warn / die        — shell log helpers
├── wait_port                — nc -z polling
├── start_postgres           — podman start/run
├── start_cockroachdb        — podman start/run
├── start_mysql              — podman start/run
├── start_sqlserver          — limactl start + systemctl
├── start_oracle             — limactl start + log polling
├── start_db2                — limactl start
└── start_databases          — orchestrates the above

run_identity.sh (~40 lines)
├── parse CLI args
├── start_databases          — if not --skip-setup
└── python run_matrix.py identity ...

run_lakebase_target.sh (~40 lines)
├── parse CLI args
├── start_databases
└── python run_matrix.py lakebase ...

run_bench.sh (~40 lines)
├── parse CLI args
├── start_databases
└── python run_matrix.py bench ...
```

Everything else moves to Python.

---

## What moves to Python after all phases

```
benchmarks/bench_config.py   ← NEW: SCALE_FACTORS, DEFAULT_ENGINES, sf_for(), build_dsn()
benchmarks/run_matrix.py     ← NEW: engine × schema loop, parallelism, result aggregation
tests/conftest.py            ← NEW: session-scoped fixtures for all engine connections
tests/live_helpers.py        ← NEW: LiveDbHelper(conn, dialect)
tests/test_live_type_coverage.py  ← NEW: parametrized type round-trip across all engines
tests/test_live_distribution.py   ← NEW: distribution fidelity without Spark
```

---

## Notebook compatibility after all phases

From a Databricks notebook, with databases accessible via DSN:

```python
# Cell 1 — config
from benchmarks.bench_config import sf_for, DEFAULT_SCHEMAS
dsns = {
    "postgres": dbutils.secrets.get("statschema", "pg18_dsn"),
    "mysql":    dbutils.secrets.get("statschema", "mysql8_dsn"),
}

# Cell 2 — run identity matrix (no shell required)
from benchmarks.run_matrix import run_identity_matrix
results = run_identity_matrix(
    engines=["postgres", "mysql"],
    schemas=DEFAULT_SCHEMAS,
    dsns=dsns,
    log_dir="/tmp/identity-logs",
)

# Cell 3 — check row counts
from benchmarks.check_run import check_all
check_all(results, dsns)

# Cell 4 — view summary
import pandas as pd
pd.DataFrame(results).sort_values("pass")
```

The DB startup cells (`start_databases`) are not needed in a notebook because cloud databases (Lakebase, RDS, Azure SQL) are always running. When testing against local Podman/Lima databases from a notebook, the databases are started from the terminal before opening the notebook.

---

## Phase 8 — Docker image (2 days)

### Assumptions

- The statschema image is self-contained: Python 3.11, Java 17, PySpark, and all database drivers are baked in.
- Databases are never started inside the image. They are provided as either Podman containers running on the host or live cloud endpoints (Lakebase, RDS, Neon, Azure SQL). The image connects to them via DSN.
- Credentials are passed in at runtime via `--env-file .env` or individual `--env` flags. Nothing secret is baked into the image.

### What breaks today without a code fix

One thing currently prevents tests from running inside any Linux container:

**`conftest.py` hardcodes macOS Homebrew Java paths.** Inside the image, `JAVA_HOME` is already set correctly by the Dockerfile, but `conftest.py` overwrites it with a Homebrew path search that finds nothing on Linux, leaving PySpark unable to start a JVM.

Fix: check `JAVA_HOME` first and only fall back to a path search when it is unset:

```python
# conftest.py — after fix
if not os.environ.get("JAVA_HOME"):
    for path in [
        "/usr/lib/jvm/java-17-openjdk-amd64",   # Debian/Ubuntu (Docker)
        "/usr/lib/jvm/java-17-openjdk",          # Alpine / RHEL
        "/opt/homebrew/opt/openjdk@17",          # macOS
        "/opt/homebrew/opt/openjdk@21",
    ]:
        if os.path.isdir(path):
            os.environ["JAVA_HOME"] = path
            break
```

Password extraction for Lima-hosted SQL Server (`limactl shell`) also doesn't apply inside the image. `bench_config.py:build_dsn()` (Phase 1) reads `SQLSERVER_PASS` directly from the environment; the `limactl` path is only used when running `.sh` scripts on the host.

### Dockerfile

```dockerfile
# Dockerfile
FROM python:3.11-slim

# Java 17 is required for PySpark local mode
RUN apt-get update && apt-get install -y --no-install-recommends \
        openjdk-17-jre-headless \
    && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
ENV PYTHONUNBUFFERED=1

WORKDIR /statschema

# Python dependencies: PySpark, all DB drivers, test framework
COPY requirements-test.txt .
RUN pip install --no-cache-dir -r requirements-test.txt

# statschema source, benchmarks, and tests
COPY pyproject.toml setup.cfg* setup.py* ./
COPY src/        src/
COPY benchmarks/ benchmarks/
COPY tests/      tests/
COPY conftest.py pytest.ini ./

RUN pip install --no-cache-dir -e .

# Default: offline tests only (no DB required)
ENTRYPOINT ["python", "-m", "pytest"]
CMD ["tests/", "-v", "-k", "not live"]
```

`requirements-test.txt` already includes `pyspark`, `pyarrow`, `delta-spark`, `pymysql`, `psycopg2-binary`, `pymssql`, `oracledb`, `ibm_db_dbi`, and all other drivers, so no additional installation steps are needed.

### Providing databases

The image connects to databases through environment variables. Start databases on the host (or point at cloud endpoints) before running the image.

**Option A — Podman containers on the host:**

```bash
# Start databases on the host once
podman start pg18 mysql8 crdb-single

# Run tests — --network host lets the container reach host Podman ports
docker run --rm --network host --env-file .env statschema:latest \
    tests/test_live_pg.py tests/test_live_mysql.py tests/test_live_cockroachdb.py -v
```

**Option B — Cloud or remote endpoints:**

```bash
# .env contains DSN details for Lakebase, RDS, Neon, Azure SQL, etc.
# No --network host needed; standard outbound network access is sufficient
docker run --rm --env-file .env statschema:latest \
    tests/test_live_lakebase.py tests/test_live_neon.py -v
```

**Option C — Lima-hosted databases (SQL Server, Oracle, DB2):**

Lima VMs expose ports on `127.0.0.1` of the host. Use `--network host` and set passwords explicitly since `limactl` is not available inside the image:

```bash
# .env must contain SQLSERVER_PASS, ORACLE_PASS, DB2_PASS
# (retrieve from the Lima logs once and persist in .env)
docker run --rm --network host --env-file .env statschema:latest \
    tests/test_live_sqlserver.py tests/test_live_oracle.py tests/test_live_db2.py -v
```

### Test modes

| Mode | Command | DB required |
|---|---|---|
| Offline (default) | `docker run --rm statschema:latest` | No |
| Spark data-generation tests | `docker run --rm statschema:latest tests/ -v -k "generate_data"` | No |
| Live-DB tests (Podman/host) | `docker run --rm --network host --env-file .env statschema:latest tests/test_live_pg.py` | Podman on host |
| Live-DB tests (cloud) | `docker run --rm --env-file .env statschema:latest tests/test_live_lakebase.py` | Cloud endpoint |
| Identity matrix | `docker run --rm --network host --env-file .env --entrypoint python statschema:latest benchmarks/run_matrix.py identity --engines postgres,mysql --schemas tpcb,tpcc` | Podman or cloud |
| Benchmark matrix | `docker run --rm --network host --env-file .env --entrypoint python statschema:latest benchmarks/run_matrix.py bench --engines cockroachdb,mysql` | Podman or cloud |

### Credential handling

The image reads credentials exclusively from environment variables (via `python-dotenv` loading `.env`, or direct `--env` / `--env-file` arguments). Priority order in `bench_config.py`:

1. `--env KEY=VALUE` flags passed to `docker run`
2. `--env-file .env` file passed to `docker run`
3. `.env` bind-mounted into the container: `-v $(pwd)/.env:/statschema/.env`
4. Compiled-in defaults for local dev ports and non-secret values (e.g. `PG_USER=postgres`)

Secrets — passwords, OAuth tokens, service principal credentials — are never baked into the image.

### CI workflow

```yaml
# .github/workflows/ci.yml
jobs:
  offline-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Build image
        run: docker build -t statschema:ci .
      - name: Run offline tests
        run: docker run --rm statschema:ci
        # Runs: pytest tests/ -v -k "not live"  — no DB required

  live-tests:
    runs-on: ubuntu-latest
    if: github.event_name == 'schedule'    # nightly only
    steps:
      - uses: actions/checkout@v4
      - name: Start databases (Podman)
        run: |
          podman run -d --name pg18 -e POSTGRES_PASSWORD=testpass -p 5418:5432 postgres:18
          podman run -d --name mysql8 -e MYSQL_ROOT_PASSWORD=testpass \
              -e MYSQL_DATABASE=testdb -p 3384:3306 mysql:8
          podman run -d --name crdb-single -p 26257:26257 \
              cockroachdb/cockroach:latest start-single-node --insecure
      - name: Build image
        run: docker build -t statschema:ci .
      - name: Run live tests
        run: |
          docker run --rm --network host \
            -e PG18_PORT=5418 -e PG_PASSWORD=testpass \
            -e MYSQL8_PORT=3384 -e MYSQL_ROOT_PASS=testpass \
            -e CRDB_SINGLE_PORT=26257 \
            statschema:ci \
            tests/test_live_pg.py tests/test_live_mysql.py tests/test_live_cockroachdb.py -v
```

Lima-hosted databases (SQL Server, Oracle, DB2) require a self-hosted runner with the VMs pre-provisioned. The nightly CI job above covers Podman-hosted engines; Lima engines are validated on developer machines via `make test-live-all`.

---

## Summary

| Phase | New files | Files trimmed | Effort | Primary gain |
|---|---|---|---|---|
| 1 — Shared Python config | `bench_config.py` | `_common.sh`, `run_bench.py`, `run_all_bench.py` | 1 day | Single source of truth for scale factors and DSNs |
| 2 — Python matrix runner | `run_matrix.py` | 3 .sh files (~40 lines each) | 2 days | Full matrix runner callable from notebook or Docker |
| 3 — Shared test fixtures | `conftest.py`, `live_helpers.py` | 8 `test_live_*.py` files | 1 day | ~400 lines removed; consistent auto-skip |
| 4 — Parametrized type coverage | `test_live_type_coverage.py` | 4 live test files | 2 days | ~600 lines removed; one edit point for new types |
| 5 — Non-Spark distribution fidelity | `test_live_distribution.py` | `test_live_synth.py` | 1 day | Obj 4 runs without Spark |
| 6 — Step CLI flags | (in `run_matrix.py`) | `identity_test.py` | 1 day | Any pipeline step re-runnable alone |
| 7 — Objective markers | `pytest.ini`, `Makefile` | — | 2 hours | `make test-obj4` etc. work |
| 8 — Docker image | `Dockerfile`, Java path fix in `conftest.py` | — | 2 days | Offline tests in CI; live tests against external DSNs |

**Total: ~10 days. Net line change: −1,000 .sh lines, +900 Python lines (net −100 total). Gains: notebook support, Docker/CI support, single source of truth for all config.**
