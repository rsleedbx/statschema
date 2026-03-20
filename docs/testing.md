# statschema – local testing strategy

This document is the authoritative guide for running the test suite locally and
in CI.  It is written to be consumed directly by AI agents (Cursor, Claude, etc.)
as well as human developers.

**Repo:** [github.com/rsleedbx/statschema](https://github.com/rsleedbx/statschema)

**New database integration** (NeonDB, CockroachDB, …): follow [`docs/adding-a-database.md`](adding-a-database.md).

---

## TL;DR – just run the tests

```bash
# 1. One-time setup (Python 3.11 + local Spark venv)
make venv-test

# 2. Copy the credential template and fill in your values
cp .env.example .env
# edit .env — at minimum set SQLSERVER_PASS (retrieve from the Lima VM log)

# Run everything (pure-Python + Spark + live-DB tests if configured)
make test

# Fast subset (no Spark data-generation, no live-DB tests)
make test-fast

# Only the Spark data-generation tests
make test-spark

# Only the live SQL Server tests (requires VM running; credentials from .env)
make test-live-sqlserver
```

Current baseline: **2053 passed, 3 skipped** (the 3 skips require live Databricks
credentials and are expected).  Live-DB tests add:
- **14** when SQL Server is running (`test_live_sqlserver.py`)
- **20** when Oracle XE is running (`test_live_oracle.py`)
- **19** when Mautic is running (`test_live_mautic.py`)
- **16** when Gitea is running (`test_live_gitea.py`)
- **34** when AdventureWorks is restored (`test_live_adventureworks.py` — AWLT 12 tables + full 71 tables)
- **16** when Chinook (SQL Server) is loaded (`test_live_chinook.py`)
- **20** when Oracle HR/CO schemas are installed (`test_live_oracle_hr.py`)
- **17** when all live databases and Spark are available (`test_live_synth.py`)
- **4** when Neon Local is running (`test_live_neon.py` — NeonDB / Postgres wire via [Neon Local](https://hub.docker.com/r/neondatabase/neon_local))

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

### 0. Credentials — `.env` file

All passwords and endpoints are stored in a `.env` file at the **repo root** that is
never committed to git.  `conftest.py` loads it automatically before any test runs
via `python-dotenv`, so you don't need to export variables in every shell session.

```bash
cp .env.example .env
# Open .env and fill in:
#   SQLSERVER_PASS   – retrieve from the Lima VM cloud-init log (see below)
#   CLIENT_SECRET    – your Databricks service principal secret
# All other values have working defaults for the Podman containers in
# docs/local-databases.md and can be left as-is.
```

The SQL Server password is generated randomly when the Lima VM first boots:

```bash
limactl shell sqlserver22 -- \
  sudo grep 'SQL Server sa password' /var/log/cloud-init-output.log \
  | tail -1 | awk '{print $NF}'
```

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
| `make test-live-mysql` | `pytest tests/test_live_mysql.py -v` | Live MySQL 5.7 + 8.x tests (Podman) |
| `make test-live-pg` | `pytest tests/test_live_pg.py -v` | Live PostgreSQL 14 + 16 tests (Podman) |
| `make test-live-neon` | `pytest tests/test_live_neon.py -v` | Live Neon via [Neon Local](https://hub.docker.com/r/neondatabase/neon_local) (cloud API key) |
| `make test-live-cockroachdb` | `CRDB_SINGLE_PORT=26257 CRDB_MULTI_PORT=26267 pytest tests/test_live_cockroachdb.py -v` | Live CockroachDB — single-node + multi-region (Podman) |
| `make test-live-oracle` | `pytest tests/test_live_oracle.py -v` | Live Oracle XE tests (Lima VM) |
| `make test-live-mautic` | `pytest tests/test_live_mautic.py -v` | Live Mautic application tests (Podman) |
| `make test-live-gitea` | `pytest tests/test_live_gitea.py -v` | Live Gitea application tests (PostgreSQL) |
| `make test-live-adventureworks` | `pytest tests/test_live_adventureworks.py -v` | AdventureWorksLT + AW2022 full (SQL Server) |
| `make test-live-chinook` | `pytest tests/test_live_chinook.py -v` | Live Chinook sample DB tests (SQL Server) |
| `make test-live-oracle-hr` | `pytest tests/test_live_oracle_hr.py -v` | Live Oracle HR/CO sample schema tests |
| `make test-live-roundtrip` | `pytest tests/test_live_roundtrip.py -v` | Double round-trip on all live DBs |
| `make test-live-synth` | `pytest tests/test_live_synth.py -v` | Full synth pipeline on all live DBs |
| `make test-live-all` | all `test-live-*` targets | Everything against every live DB |
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
| `tests/test_live_sqlserver.py` | 14 | No | **Yes** – SQL Server via Lima VM |
| `tests/test_live_mysql.py` | ~18 | No | **Yes** – MySQL 5.7 + 8.x via Podman |
| `tests/test_live_pg.py` | ~18 | No | **Yes** – PostgreSQL 14 + 16 via Podman |
| `tests/test_live_neon.py` | 4 | No | **Yes** – Neon Local proxy (`neondatabase/neon_local`) |
| `tests/test_live_cockroachdb.py` | ~20 | No | **Yes** – CockroachDB single-node + multi-region (Podman) |
| `tests/test_live_oracle.py` | 20 | No | **Yes** – Oracle XE via Lima VM |
| `tests/test_live_mautic.py` | 19 | No | **Yes** – Mautic 5 + MySQL 8 via Podman |
| `tests/test_live_roundtrip.py` | ~300 | No | **Yes** – MySQL + PG + SQL Server |
| `tests/test_live_synth.py` | 17 | **Yes** | **Yes** – MySQL 8 + PG 16 + SQL Server |

### Tests that are always skipped (expected)

| Test | Reason |
|------|--------|
| Any test calling `pytest.importorskip("databricks.connect")` from `.venv_test` | `databricks-connect` intentionally absent |

### Live-DB tests: skipped automatically when DB not configured

All live-DB test files auto-skip when their database is unreachable or the
required credentials are not set, so `make test` always completes cleanly:

| Test file | Skip condition |
|-----------|----------------|
| `test_live_sqlserver.py` | `SQLSERVER_PASS` not set or port closed |
| `test_live_mysql.py` | port 3357 or 3384 closed |
| `test_live_pg.py` | port 5414 or 5416 closed |
| `test_live_neon.py` | Neon Local port closed (default `NEON_LOCAL_PORT`) |
| `test_live_cockroachdb.py` | `CRDB_SINGLE_PORT` (26257) and `CRDB_MULTI_PORT` (26267) both closed |
| `test_live_oracle.py` | port 1521 closed (or `oracledb` not installed) |
| `test_live_mautic.py` | `mautic` DB unreachable on port 3384 |
| `test_live_roundtrip.py` | any required port closed |
| `test_live_synth.py` | any required DB port or Spark unavailable |

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

## Comprehensive live round-trip tests (`test_live_roundtrip.py`)

`tests/test_live_roundtrip.py` is the highest-confidence integration test.  It
reuses every table schema from the in-memory `test_ddl_roundtrip.py` Phase 1
(structured type × constraint cases) and Phase 2 (60 random tables per dialect),
then executes them on real databases.

**Test matrix:**

| Dialect | Live servers | Cases per server |
|---------|-------------|-----------------|
| MySQL | mysql57 (5.7 x86 emulation) + mysql8 (8.4 ARM64) | 29 + 60 = 89 |
| PostgreSQL | pg14 + pg16 | 24 + 60 = 84 |
| SQL Server | sqlserver22 (2022, QEMU) | 22 + 60 = 82 |

**What each case verifies (double round-trip):**

```
canonical table
      │
      ▼
emit_ddl(dialect)  → ddl1
      │
      ▼ execute on live DB  ← proves DDL is valid SQL
      │
parse_ddl(ddl1, dialect)  → canonical2
      │
      ▼
emit_ddl(dialect)  → ddl2
      │ assert ddl1 == ddl2  ← in-memory idempotency
      │
      ▼ execute ddl2 on live DB  ← proves round-tripped DDL is also valid
      │
parse_ddl(ddl2, dialect)  → canonical3
      │
      ▼
emit_ddl(dialect)  → ddl3
      │ assert ddl2 == ddl3  ← second round-trip stability
```

**Cross-dialect pipeline** (`TestCrossDialectLivePipeline`):

Three representative real-world tables (one from each dialect source) travel
through the full `many DDL → one canonical YAML → many DDL` path:

```
MySQL DDL ──parse──► canonical YAML ──emit──► MySQL DDL   → run on mysql57+8
                                   ──emit──► Postgres DDL → run on pg14+16
                                   ──emit──► SS DDL       → run on sqlserver22
```

Each target DDL is executed on its live server and parsed back.  The test then
asserts that column names and count are identical across all three dialect
representations.

**Run it:**

```bash
# Quick single-target check (uses env var defaults)
make test-live-roundtrip SQLSERVER_PASS=<password>

# Everything together (round-trip + type-specific + cross-dialect)
make test-live-all SQLSERVER_PASS=<password>
```

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
This affects only the 3 `generate_data` tests and all live-DB tests;
all other ~2036 tests run fine without network access.

### Live-DB tests also need sandbox bypass

All live-DB tests open TCP connections to `127.0.0.1`.
The same `required_permissions: ["all"]` bypass is needed.

### Lima VM startup is slow

`limactl start` takes 1–3 minutes for the QEMU x86_64 VM to boot.

**SQL Server** takes another ~30 seconds to initialise after the VM is up.
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

**Oracle XE** takes 3–5 minutes to initialise its data files on first boot.
The container restarts automatically when the VM boots, but you must wait
for the database to be fully ready before running tests.  Use polling:

```bash
until limactl shell oracle -- podman logs oracle-xe 2>/dev/null \
      | grep -q "DATABASE IS READY TO USE"; do
  echo "[$(date +%H:%M:%S)] waiting for Oracle XE…"
  sleep 15
done
nc -z 127.0.0.1 1521 && echo "Oracle port 1521 open"
```

After subsequent restarts (`limactl stop oracle` + `limactl start oracle`),
Oracle typically becomes ready within 30–60 seconds (no data file creation).

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

### Oracle-specific normalizations

| Source Oracle type | Canonical type | Re-emitted as | Reason |
|--------------------|---------------|---------------|--------|
| `NUMBER(p)` (no scale, p ≤ 9) | `integer` | `NUMBER(10)` | Canonical integer bucket |
| `NUMBER(p)` (no scale, 9 < p ≤ 18) | `long` | `NUMBER(19)` | Canonical long bucket |
| `NUMBER(1)` | `integer` | `NUMBER(10)` | Single-digit NUMBER widens |
| `CHAR(n)` | `string` | `VARCHAR2(n)` | CHAR → string normalisation |
| `CLOB` | `string` (no length) | `CLOB` | No-length string → CLOB |
| `DATE` | `datetime` | `DATE` | Oracle DATE includes time component |
| `RAW(n)` / `BLOB` | `binary` | `RAW(n)` / `BLOB` | Binary types preserved |

---

## Bugs found by live-DB testing (fixed)

### From `test_live_sqlserver.py` (SQL Server 2022)

| Bug | Symptom | Root cause | Fix |
|-----|---------|------------|-----|
| `"tsql"` dialect alias not recognised | `DATETIME2`, `MONEY`, `UNIQUEIDENTIFIER` failed to parse when `dialect="tsql"` | `_SG_DIALECT` only mapped `"sqlserver"` → `"tsql"`, not the reverse | Added `"tsql"`, `"mssql"`, `"postgresql"` as accepted aliases |
| `NULL` keyword parsed as `NOT NULL` | `DECIMAL(18,4) NULL` produced `not_null=True` | sqlglot 30 uses `NotNullColumnConstraint(allow_null=True)` for explicit `NULL` | Changed to read `_nn.args.get("allow_null")` |
| `UNIQUEIDENTIFIER` lost to `NVARCHAR(MAX)` | Round-tripped `UNIQUEIDENTIFIER` became `NVARCHAR(MAX)` | `DT.UUID` was mapped to canonical `"string"` | New canonical type `"uuid"` with per-dialect emission rules |

### From `test_live_roundtrip.py` (cross-dialect pipeline, MySQL 5.7+8.x, PG 14+16, SQL Server 22)

| Bug | Symptom | Root cause | Fix |
|-----|---------|------------|-----|
| `DEFAULT TRUE` rejected by SQL Server | PG `BOOLEAN NOT NULL DEFAULT TRUE` → SS `BIT NOT NULL DEFAULT true` caused `OperationalError: name "true" not permitted` | `_default_clause` emitted canonical default verbatim without dialect normalisation | Added `_normalize_default()` in `ddl_emitter.py`: maps `true/false` → `1/0` for SQL Server and MySQL; `1/0` → `TRUE/FALSE` for PostgreSQL |

### From `test_live_oracle.py` (Oracle XE 21c)

| Bug | Symptom | Root cause | Fix |
|-----|---------|------------|-----|
| `NOT NULL` order with `DEFAULT` | `ORA-00907` on columns with both DEFAULT and NOT NULL | Oracle requires `DEFAULT val NOT NULL`; emitter produced `NOT NULL DEFAULT val` | Reordered clauses in `_col_ddl` for Oracle dialect |
| Redundant `NOT NULL` on IDENTITY columns | `ORA-00907` on auto-increment columns | Oracle `GENERATED ALWAYS AS IDENTITY` is implicitly NOT NULL; explicit `NOT NULL` is a syntax error | Suppressed `NOT NULL` when `col.auto_increment` is true and `dialect == "oracle"` |
| Boolean `DEFAULT true` for `NUMBER(1)` | `ORA-00984: column not allowed here` | Oracle `NUMBER(1)` defaults must be `1` or `0`, not `TRUE`/`FALSE` | Extended `_normalize_default()` to include `"oracle"`, mapping `true/false` → `1/0` |
| No-length string emitted as `VARCHAR2(255)` | `CLOB` columns round-tripped to `VARCHAR2(255)` | `_ORACLE_DEFAULTS["string"]` was `"VARCHAR2(255)"` — Oracle VARCHAR2 has a 4000-byte limit, insufficient for long strings | Changed `_ORACLE_DEFAULTS["string"]` to `"CLOB"`; explicit `length` still produces `VARCHAR2(n)` |

### From `test_live_synth.py` (live synthetic data pipeline)

| Bug | Symptom | Root cause | Fix |
|-----|---------|------------|-----|
| High-cardinality MCV collapse | After stats-driven generation, `customer_id` had only 10 distinct values instead of ~1000 | `dbldatagen_builder.py` applied top-10 MCVs to ALL columns unconditionally when `col_stats.most_common_values` was non-empty — even for random integers where the MCVs cover only 1% of the data | Added `mcv_meaningful` guard: MCVs are only applied when they cover >50% of the data OR `n_distinct ≤ 20` |
| Boolean MCV type mismatch | Stats-driven generation of boolean `is_paid` raised `DATATYPE_MISMATCH.DATA_DIFF_TYPES` | MCVs from the live DB were string `"0"/"1"` or `"t"/"f"`, but the column uses `BooleanType()` — mixing types in the Spark CASE expression caused a type error | Boolean columns are now excluded from MCV-based generation; they use `random=True` natively |
| `str - str` in generation | `TypeError: unsupported operand type(s) for -: 'str' and 'str'` when using collected stats for timestamp columns | `min_value`/`max_value` from `collect_table_stats` are stored as strings; passing them verbatim as `minValue`/`maxValue` to dbldatagen causes arithmetic errors | Added `_cast_stat_value()` in `dbldatagen_builder.py`: casts stat strings to `int`/`float` for numeric types; passes strings as-is for timestamps; skips min/max for string/binary columns |

---

## Gitea application tests (`test_live_gitea.py`)

Gitea is a self-hosted Git service written in Go with **~112 PostgreSQL tables**.
It is the PostgreSQL application-level test target, exercising: `BIGINT`, `BOOLEAN`, `TEXT`,
`TIMESTAMP WITH TIME ZONE`, `BYTEA`, and FK relationships across CI/CD, repository, user, and
issue management domains.

### What is tested (16 tests)

| Class | Tests | What it checks |
|-------|-------|----------------|
| `TestGiteaConnection` | 2 | DB reachable; 7 core tables present |
| `TestGiteaParse` | 4 | All 112 tables parse; key columns on `user`, `repository`, `issue` |
| `TestGiteaEmit` | 5 | All 112 tables emit; double round-trip idempotency; cross-dialect to MySQL, SQL Server, Oracle |
| `TestGiteaMultiInstance` | 3 | `action_task` 3-shard expansion; `notification` aliases; YAML round-trip for `comment` |
| `TestGiteaCanonicalYaml` | 2 | Full 112-table YAML dump and reload; `repository` YAML content |

### Setup

```bash
make test-live-gitea
# See docs/local-databases.md – Gitea section for one-time container setup
```

---

## AdventureWorks application tests (`test_live_adventureworks.py`)

Two Microsoft canonical SQL Server sample databases, both restored into the SQL Server 22 Lima VM.

**AdventureWorksLT2022** (12 tables, `SalesLT` schema) — compact retail sample: customers,
addresses, products, sales orders.  Good baseline for SQL Server cross-schema FK relationships.

**AdventureWorks2022** (71 tables, 6 schemas) — the full Microsoft sample covering
HumanResources, Person, Production, Purchasing, and Sales departments.  The most type-rich
SQL Server test target, exercising user-defined types, `MONEY`, `UNIQUEIDENTIFIER`, `xml`,
`hierarchyid`, and `geography`.

### What is tested (34 tests)

| Class | Tests | What it checks |
|-------|-------|----------------|
| `TestAWLTConnection` | 2 | AWLT DB reachable; all 5 SalesLT tables present |
| `TestAWLTParse` | 4 | All 12 AWLT tables parse; Customer/Product/SalesOrderDetail columns |
| `TestAWLTEmit` | 5 | All 12 AWLT tables emit; double round-trip; cross-dialect to PostgreSQL, MySQL, Oracle |
| `TestAWLTMultiInstance` | 3 | Quarterly shard expansion; aliases; full YAML round-trip |
| `TestAWFullConnection` | 3 | AW2022 DB reachable; all 6 schemas present; per-schema table counts |
| `TestAWFullParse` | 6 | All 71 tables parse; Employee/Product/SalesOrderHeader columns; UDT resolution; TransactionHistory |
| `TestAWFullEmit` | 7 | All 71 tables emit; double round-trip for Employee and SalesOrderHeader; cross-dialect to PostgreSQL, Oracle, MySQL, Databricks |
| `TestAWFullMultiInstance` | 4 | 12-month archive shard expansion; region aliases; full 71-table YAML dump and reload; YAML type spot-check |

### Notable type findings

| SQL Server type | Canonical resolution |
|----------------|----------------------|
| `Name` UDT → `NVARCHAR(50)` | `string(50)` |
| `Flag` / `NameStyle` UDT → `BIT` | `boolean` or `integer` |
| `MONEY` | `decimal` |
| `UNIQUEIDENTIFIER` | `uuid` |
| `timestamp` / ROWVERSION | `string` (remapped via NVARCHAR MAX) |
| `xml`, `hierarchyid`, `geography` | `string` (remapped, no direct canonical) |

### Setup

```bash
make test-live-adventureworks
# See docs/local-databases.md – AdventureWorks section for one-time restore
```

---

## Chinook application tests (`test_live_chinook.py`)

[Chinook](https://github.com/lerocha/chinook-database) is the de-facto SQL Server sample database —
**11 tables** modelling a digital music store (based on iTunes).  It is the SQL Server
application-level test target, covering: `NVARCHAR`, `INTEGER`, `DECIMAL`, `DATETIME`,
`NUMERIC`, composite PKs, and multi-level FK chains.

### What is tested (16 tests)

| Class | Tests | What it checks |
|-------|-------|----------------|
| `TestChinookConnection` | 2 | DB reachable; all 11 tables present |
| `TestChinookParse` | 4 | All 11 tables parse; `Track` columns; `Invoice` columns; `PlaylistTrack` composite PK |
| `TestChinookEmit` | 5 | All 11 tables emit; double round-trip idempotency; cross-dialect to MySQL, PostgreSQL, Oracle |
| `TestChinookMultiInstance` | 3 | `Track` 5-shard expansion; `Invoice` aliases; Artist/Album YAML round-trip |
| `TestChinookCanonicalYaml` | 2 | Full 11-table YAML dump and reload; `Track` YAML field content |

### Setup

```bash
make test-live-chinook
# See docs/local-databases.md – Chinook section for one-time script loading
```

---

## Oracle HR/CO application tests (`test_live_oracle_hr.py`)

Oracle's canonical sample schemas from the [oracle-samples/db-sample-schemas](https://github.com/oracle-samples/db-sample-schemas)
repository.  **14 tables total** (HR: 7 + CO: 7), exercising Oracle-specific types:
`NUMBER(p,s)`, `VARCHAR2`, `CHAR`, `DATE`, `TIMESTAMP`, `INTERVAL`, and the FK hierarchy
required by Oracle's semantic constraints.

### What is tested (20 tests)

| Class | Tests | What it checks |
|-------|-------|----------------|
| `TestOracleHRConnection` | 2 | HR DB reachable; all 7 HR tables present |
| `TestOracleHRParse` | 4 | All 7 HR tables parse; EMPLOYEES columns; JOBS salary types; FK graph |
| `TestOracleHREmit` | 5 | All 7 HR tables emit; double round-trip idempotency; cross-dialect to MySQL, PostgreSQL, SQL Server |
| `TestOracleCOConnection` | 2 | CO DB reachable; all 7 CO tables present |
| `TestOracleCOParse` | 3 | All 7 CO tables parse and emit; ORDERS double round-trip |
| `TestOracleSampleMultiInstance` | 4 | EMPLOYEES 3-shard expansion; ORDERS aliases; full HR YAML round-trip; combined HR+CO 14-table YAML |

### Setup

```bash
make test-live-oracle-hr
# See docs/local-databases.md – Oracle HR/CO section for one-time schema creation
```

---

## Mautic application tests (`test_live_mautic.py`)

Mautic is an open-source marketing automation platform with ~108 MySQL tables.
It validates `statschema` against a real-world application schema with diverse
types, virtual generated columns, and natural multi-instance shard patterns.

### What is tested (19 tests)

| Class | Tests | What it checks |
|-------|-------|----------------|
| `TestMauticConnection` | 2 | DB reachable; 10 contacts exist in `leads` |
| `TestMauticParse` | 5 | All 108 tables parse; key columns on `leads`, `email_stats`, `campaign_lead_event_log`, `page_hits` |
| `TestMauticEmit` | 4 | All 108 tables emit; double round-trip idempotency; cross-dialect MySQL→PostgreSQL and MySQL→SQL Server |
| `TestMauticMultiInstance` | 5 | `instance_count` expansion; combined count+aliases; YAML round-trip; aliases pattern; 30-shard bulk test |
| `TestMauticCanonicalYaml` | 3 | Full 108-table YAML dump and reload; field-level YAML content; `leads` round-trip |

### Notable findings

| Mautic pattern | Handled by |
|----------------|------------|
| `GENERATED ALWAYS AS … VIRTUAL` column | Column silently dropped by `sqlglot` (not in the canonical model — this is correct behaviour) |
| `BIGINT UNSIGNED` FK refs | Widened to canonical `long` → emits as `BIGINT` |
| `TINYINT(1)` boolean flags | Canonical `integer` (Mautic uses 0/1 not `BOOLEAN`) |
| `LONGTEXT` with `DC2Type:array` comments | Canonical `string` |

### Setup

See [docs/local-databases.md – Mautic section](local-databases.md#mautic-application-level-testing) for the
full one-time setup commands.

```bash
make test-live-mautic
```

---

## Comprehensive live synthetic data tests (`test_live_synth.py`)

This test file validates the full DDL → Spark → DB → stats → Spark loop on real databases.

### Pipeline (6 tests per database)

```
1. emit DDL → CREATE TABLE on live DB (table_v1)
2. build_dataframe_from_canonical(default stats, 1000 rows)
   → toPandas() → pandas.to_sql() → loaded into table_v1
3. collect_table_stats(conn, "table_v1") → real ColumnStats
   (null_fraction, n_distinct, min/max, top-10 MCVs, histogram bounds)
4. build_dataframe_from_canonical(REAL stats, 1000 rows)  ← stats-driven
   → loaded into table_v2
5. collect stats from table_v2
6. compare table_v1 vs table_v2 stats (null fractions ≤ ±0.15, distinct ratios ≤ ×5)
```

### Databases covered

| Database | Version | Port |
|----------|---------|------|
| MySQL    | 8.4     | 3384 |
| PostgreSQL | 16    | 5416 |
| SQL Server | 2022  | 14330 (Lima QEMU) |

### Test table schema

A portable `synth_orders` table with diverse types:
`INT (PK, auto-increment)`, `INT (FK-like)`, `VARCHAR(20)`, `DECIMAL(10,2)`,
`FLOAT`, `BOOLEAN/BIT`, `DATETIME/TIMESTAMP`.

### Key design decisions

- **`if_exists="append"`**: tables are created with the DDL-emitted schema first; pandas uses `append` to preserve column types.
- **`spark.sql.ansi.enabled=false`**: allows DECIMAL overflow to produce NULL (instead of aborting the job) when a generated value slightly exceeds the column precision.
- **`PYSPARK_PYTHON` pinned**: forces PySpark workers to use the same Python interpreter as the driver (avoids `PYTHON_VERSION_MISMATCH` if a different system Python is found on `$PATH`).
- **Module-level `_PIPELINE_STATE` dict**: PG and SS connection objects are C-extension types (`psycopg2`, `pymssql`) that don't allow arbitrary attribute assignment, so inter-test state is passed via a plain Python dict.

### How to run

```bash
# MySQL 8 + PG 16 + SQL Server 22 must be running (see docs/local-databases.md)
make test-live-synth SQLSERVER_PASS=<password>

# or directly
SQLSERVER_PASS=<pw> .venv_test/bin/python -m pytest tests/test_live_synth.py -v
```

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
- [`tests/test_live_mysql.py`](../tests/test_live_mysql.py) – live MySQL 5.7 + 8.x test suite
- [`tests/test_live_pg.py`](../tests/test_live_pg.py) – live PostgreSQL 14 + 16 test suite
- [`tests/test_live_neon.py`](../tests/test_live_neon.py) – live Neon Local test suite (Neon cloud + `neon_local` proxy)
- [`tests/test_live_oracle.py`](../tests/test_live_oracle.py) – live Oracle XE test suite (20 tests)
- [`tests/test_live_mautic.py`](../tests/test_live_mautic.py) – live Mautic application test suite (19 tests)
- [`tests/test_live_gitea.py`](../tests/test_live_gitea.py) – live Gitea application test suite (16 tests, PostgreSQL)
- [`tests/test_live_adventureworks.py`](../tests/test_live_adventureworks.py) – AdventureWorksLT 2022 + AdventureWorks2022 full suite (34 tests, SQL Server)
- [`tests/test_live_chinook.py`](../tests/test_live_chinook.py) – live Chinook sample database suite (16 tests, SQL Server)
- [`tests/test_live_oracle_hr.py`](../tests/test_live_oracle_hr.py) – live Oracle HR/CO sample schema suite (20 tests, Oracle XE)
- [`tests/test_live_roundtrip.py`](../tests/test_live_roundtrip.py) – comprehensive double round-trip suite
- [`tests/test_live_synth.py`](../tests/test_live_synth.py) – live synthetic data pipeline tests
- [`src/statschema/db_stats_collector.py`](../src/statschema/db_stats_collector.py) – collect `TableStats` from a live database
- [`config/lima/oracle.yaml`](../config/lima/oracle.yaml) – Lima VM config for Oracle XE
- [`docs/local-databases.md`](local-databases.md) – how to run real databases locally
- [`docs/test_plan_ddl_roundtrip.md`](test_plan_ddl_roundtrip.md) – DDL round-trip test plan
- [`docs/synthetic_data_shortcomings.md`](synthetic_data_shortcomings.md) – known synthetic data limitations
