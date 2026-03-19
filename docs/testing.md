# Zerobus – Local Testing Strategy

This document is the authoritative guide for running the test suite locally and
in CI.  It is written to be consumed directly by AI agents (Cursor, Claude, etc.)
as well as human developers.

---

## TL;DR – just run the tests

```bash
# One-time setup (Python 3.11 + local Spark venv)
make venv-test

# Run everything (pure-Python + Spark + live-DB tests if configured)
make test

# Fast subset (no Spark data-generation, no live-DB tests)
make test-fast

# Only the Spark data-generation tests
make test-spark

# Only the live SQL Server tests (requires VM running + SQLSERVER_PASS set)
make test-live-sqlserver
```

Current baseline: **2053 passed, 3 skipped** (the 3 skips require live Databricks
credentials and are expected).  Live-DB tests add 14 more when SQL Server is running.

---

## Why two virtual environments?

| venv | Python | Key packages | Use for |
|------|--------|--------------|---------|
| `.venv` | 3.14 | `databricks-connect`, `pyspark` (remote-only) | Notebooks, Databricks Connect sessions |
| `.venv_test` | 3.11 | `pyspark` (standard), **no** `databricks-connect` | Full local test suite |

**The conflict**: `databricks-connect` replaces the standard `pyspark` package
with a version that blocks `SparkSession.builder.master("local[1]")` entirely,
raising:

```
RuntimeError: Only remote Spark sessions using Databricks Connect are supported.
```

Running tests from `.venv` therefore skips every test that starts a local
SparkSession.  Running from `.venv_test` (standard `pyspark`, no
`databricks-connect`) lets all those tests execute against an in-process JVM.

---

## One-time machine setup

### 1. Java (required for local PySpark)

Standard PyPI `pyspark` needs a JVM even in `local[1]` mode.

```bash
# macOS – install once
brew install openjdk@17
```

`conftest.py` at the repo root auto-detects Homebrew OpenJDK at
`/opt/homebrew/opt/openjdk@{17,21,11}` and sets `JAVA_HOME` before any test
runs, so you never need to export it manually.

To make `java` available in every new shell as well (recommended):

```bash
# Append to ~/.zshrc
export JAVA_HOME=/opt/homebrew/opt/openjdk@17
export PATH="$JAVA_HOME/bin:$PATH"
```

### 2. Create `.venv_test`

```bash
make venv-test
```

This runs:

```bash
python3.11 -m venv .venv_test
.venv_test/bin/pip install --upgrade pip
.venv_test/bin/pip install -r requirements-test.txt
```

`requirements-test.txt` installs everything in `requirements.txt` **except**
`databricks-connect`, plus:

- `pyspark>=3.5.0` – standard local-mode PySpark
- `pyarrow>=15.0.0` – required by PySpark 4.x for Arrow optimisations
- `sqlglot>=20.0.0` – DDL parser used by `ddl_parser.py`
- `pymssql>=2.2` – SQL Server Python driver for live-DB tests

---

## Makefile targets

| Target | Command | Description |
|--------|---------|-------------|
| `make venv-test` | `python3.11 -m venv .venv_test && pip install -r requirements-test.txt` | Create / refresh the test venv |
| `make test` | `.venv_test/bin/pytest tests/ -v` | Full suite |
| `make test-fast` | `pytest tests/ -v -k "not generate_data and not live"` | Skip Spark + live-DB tests |
| `make test-spark` | `pytest tests/ -v -k "generate_data"` | Only the 3 Spark data-gen tests |
| `make test-live-sqlserver` | `SQLSERVER_PASS=… SQLSERVER_PORT=14330 pytest tests/test_live_sqlserver.py -v` | Live SQL Server tests (Lima VM) |
| `make test-file FILE=…` | `pytest <FILE> -v` | Single test file |
| `make lint` | `ruff check src/ tests/` | Lint (non-blocking) |
| `make clean` | `find … __pycache__` | Remove `.pyc` / cache |

---

## Test suite structure

| File | Tests | Needs Spark | Needs live DB |
|------|-------|-------------|---------------|
| `tests/test_ddl_roundtrip.py` | ~185 | No | No |
| `tests/test_schema_parser.py` | ~124 | 3 tests | No |
| `tests/test_v1_bridge.py` | ~50 | No | No |
| `tests/test_protobuf_converter.py` | ~40 | No | No |
| `tests/test_zerobus_ingest.py` | ~60 | No | No (2 integration tests skipped) |
| `tests/test_live_sqlserver.py` | 14 | No | **Yes** – SQL Server via Lima VM |

### Tests that are always skipped (expected)

| Test | Reason |
|------|--------|
| `TestIngestIntegration::test_phase1_ingest_snapshot` | Requires live Databricks + ZeroBus endpoint |
| `TestIngestIntegration::test_phase1_ingest_incremental` | Same |
| Any test calling `pytest.importorskip("databricks.connect")` from `.venv_test` | `databricks-connect` intentionally absent |

### Live-DB tests: skipped automatically when DB not configured

`tests/test_live_sqlserver.py` auto-skips if `SQLSERVER_PASS` is not set or
the connection fails, so `make test` always completes cleanly without a running
SQL Server.

---

## Live SQL Server tests – full setup

See [`docs/local-databases.md`](local-databases.md) for full setup of all databases.
PostgreSQL and MySQL use **Podman** (native ARM64 containers — free, no licence).
SQL Server uses **Lima + QEMU** — there is no ARM64 build of SQL Server for Linux
and Rosetta 2-based emulation is being phased out by Apple (~macOS 28, 2027).

Quick-start summary for SQL Server:

### 1. Start the VM

```bash
limactl start sqlserver22        # uses the existing sqlserver22 Lima VM
# or start from the checked-in config:
limactl start --name=sqlserver config/lima/sqlserver.yaml
```

SQL Server listens on **port 14330** in the existing `sqlserver22` VM
(not the standard 1433 — see port-forwarding note below).

### 2. Start the mssql-server service

SQL Server does not auto-start when the Lima VM boots.  Start it manually:

```bash
limactl shell sqlserver22 -- sudo systemctl start mssql-server
```

Verify it is up:

```bash
nc -zv 127.0.0.1 14330   # should succeed
```

### 3. Find the SA password

The password was generated at provision time and written to cloud-init logs:

```bash
limactl shell sqlserver22 -- grep "SQL Server sa password is" \
    /var/log/cloud-init-output.log | tail -1
```

### 4. Run the live tests

```bash
SQLSERVER_PASS="<password from above>" \
SQLSERVER_PORT=14330 \
.venv_test/bin/python -m pytest tests/test_live_sqlserver.py -v
```

All 14 tests should pass.  Typical runtime: ~5 seconds.

### Port-forwarding note

The `sqlserver22` VM was created with `guestPort: 14330` rather than the
standard `1433`.  The `config/lima/sqlserver.yaml` checked in to this repo
also uses `14330`.  If you create a fresh VM using the default Microsoft
documentation it may use `1433` instead — set `SQLSERVER_PORT` accordingly.

---

## How `_create_spark_session` works

`tests/test_schema_parser.py` contains a helper `_create_spark_session(app_name)`
that is called by every Spark-dependent test.  It tries two strategies in order:

```
1. DatabricksSession.builder.getOrCreate()   ← uses ~/.databrickscfg if present
2. SparkSession.builder.master("local[1]")   ← used when running from .venv_test
      .config("spark.driver.host", "localhost")
      .config("spark.driver.bindAddress", "127.0.0.1")
```

The `localhost` / `127.0.0.1` config prevents Spark from trying to resolve the
machine's canonical hostname, which fails in sandboxed or container environments.

If neither strategy succeeds the calling test is **skipped** (not failed).

---

## How `conftest.py` works (AI context)

`conftest.py` at the repo root runs automatically before any pytest collection.
It finds a Homebrew JDK and sets `JAVA_HOME` / `PATH` if not already set.
This makes `make test` and `.venv_test/bin/pytest tests/` work out-of-the-box
with no shell setup required.

```python
# conftest.py  (repo root)
_CANDIDATE_JAVA_HOMES = [
    "/opt/homebrew/opt/openjdk@17",
    "/opt/homebrew/opt/openjdk@21",
    "/opt/homebrew/opt/openjdk@11",
    "/opt/homebrew/opt/openjdk",
]
if not os.environ.get("JAVA_HOME"):
    found = _find_java_home()          # first existing candidate wins
    if found:
        os.environ["JAVA_HOME"] = found
        os.environ["PATH"] = f"{found}/bin:{os.environ.get('PATH', '')}"
```

---

## Note for AI agents running tests

### Spark tests need network sandbox bypass

Local PySpark binds to `127.0.0.1` sockets even in `local[1]` mode.
In Cursor's agent sandbox, network syscalls are blocked, causing:
`java.net.SocketException: Operation not permitted`.

**Workaround**: run pytest with `required_permissions: ["all"]`.
This affects only the 3 `generate_data` tests and the 14 live-DB tests;
all other ~2036 tests run fine without network access.

### Live-DB tests also need sandbox bypass

`tests/test_live_sqlserver.py` opens a TCP connection to `127.0.0.1:14330`.
The same `required_permissions: ["all"]` bypass is needed.

### Lima VM startup is slow

`limactl start` takes 1–3 minutes for the QEMU x86_64 VM to boot.
`mssql-server` takes another ~30 seconds to initialise after the VM is up.
Always check the service is running before running the live tests:

```bash
limactl shell sqlserver22 -- sudo systemctl is-active mssql-server
# should print: active
```

If it prints `inactive`, start it:

```bash
limactl shell sqlserver22 -- sudo systemctl start mssql-server
sleep 10
nc -zv 127.0.0.1 14330
```

---

## Known type-normalisation behaviour (not bugs)

The canonical model deliberately normalises some database-specific types.
The live SQL Server tests confirmed the following are intentional:

| Source T-SQL type | Canonical type | Re-emitted as | Reason |
|-------------------|---------------|---------------|--------|
| `TINYINT` | `integer` | `INT` | Canonical int has no byte-width distinction |
| `SMALLINT` | `integer` | `INT` | Same |
| `MONEY` | `decimal(19,4)` | `DECIMAL(19,4)` | Fixed-precision widening for portability |
| `SMALLMONEY` | `decimal(10,4)` | `DECIMAL(10,4)` | Same |
| `REAL` | `float` | `FLOAT` | sqlglot maps both REAL and FLOAT to the same DType |
| `BINARY(n)` | `binary` | `VARBINARY(n)` | BINARY → VARBINARY is safe; VARBINARY is more portable |
| `ROWVERSION` | `binary` | `VARBINARY(8)` | ROWVERSION has no cross-DB equivalent |

`UNIQUEIDENTIFIER` → `uuid` → `UNIQUEIDENTIFIER` is a full lossless round-trip
(fixed in this session; previously incorrectly mapped to `string`).

---

## Bugs found by live-DB testing (fixed)

Running `test_live_sqlserver.py` against a real SQL Server 2022 instance
uncovered three bugs that the fixture-based round-trip tests had not caught:

| Bug | Symptom | Root cause | Fix |
|-----|---------|------------|-----|
| `"tsql"` dialect alias not recognised | `DATETIME2`, `MONEY`, `UNIQUEIDENTIFIER` failed to parse when `dialect="tsql"` was passed | `_SG_DIALECT` only mapped `"sqlserver"` → `"tsql"`, not the reverse | Added `"tsql"`, `"mssql"`, `"postgresql"` as accepted aliases |
| `NULL` keyword parsed as `NOT NULL` | `DECIMAL(18,4) NULL` produced `not_null=True` | sqlglot 30 uses `NotNullColumnConstraint(allow_null=True)` for `NULL` and `allow_null=False` for `NOT NULL`; parser only checked for presence | Changed to read `_nn.args.get("allow_null")` |
| `UNIQUEIDENTIFIER` lost to `NVARCHAR(MAX)` | Round-tripped `UNIQUEIDENTIFIER` became `NVARCHAR(MAX)` | `DT.UUID` was mapped to canonical `"string"` | New canonical type `"uuid"` with per-dialect emission: `UNIQUEIDENTIFIER` (SS), `UUID` (PG), `CHAR(36)` (MySQL/Oracle) |

---

## Adding new tests

- **Pure Python / no Spark**: add to any existing test file; runs in both `.venv` and `.venv_test`.
- **Spark data-generation**: use `_create_spark_session(app_name)` and wrap with `pytest.importorskip("dbldatagen")`.
- **Live-DB**: follow the pattern in `tests/test_live_sqlserver.py` — use a module-scoped fixture that auto-skips on connection failure; guard with `pytest.importorskip("pymssql")`.
- **Databricks integration**: guard with `pytest.importorskip("databricks.connect")`.

---

## References

- [`requirements-test.txt`](../requirements-test.txt) – pinned test deps
- [`requirements.txt`](../requirements.txt) – runtime deps (includes databricks-connect)
- [`Makefile`](../Makefile) – all dev shortcuts
- [`conftest.py`](../conftest.py) – auto JAVA_HOME detection
- [`tests/test_live_sqlserver.py`](../tests/test_live_sqlserver.py) – live SQL Server test suite
- [`docs/local-databases.md`](local-databases.md) – how to run real databases locally
- [`docs/test_plan_ddl_roundtrip.md`](test_plan_ddl_roundtrip.md) – DDL round-trip test plan
- [`docs/synthetic_data_shortcomings.md`](synthetic_data_shortcomings.md) – known synthetic data limitations
