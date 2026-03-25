# TPC benchmark query execution — prior art research and open questions

**Status: active development.** statschema can generate TPC-x datasets for all six benchmarks (TPC-B, TPC-C, TPC-H, TPC-DS, TPC-E, TPC-DI). This document covers portable query execution, the identity test design, and open questions for reviewers.

If you know of a Python reference implementation or approach not covered here, please open an issue or add a note to this file.

---

## Identity test — key success criterion

The identity test is the primary way statschema validates its own effectiveness and the way DBAs validate it on their own databases.

### What it proves

> Run TPC-x on the **source**. Use statschema to collect schema + statistics, generate synthetic data, and inject statistics into a **copy**. Run the same queries on the copy. If query plans on the copy match the source, statschema's statistics are accurate enough to drive the optimizer to the same decisions before any production data arrives.

This is a **control experiment**: because the source dataset is TPC-x (known, reproducible, independently verifiable), the experiment can be repeated by anyone.  If statschema passes the TPC-x identity test, a DBA can trust that running statschema on their own production schema will produce a target where their application's queries behave the same as on the source.

### The pipeline (implemented in `benchmarks/identity_test.py`)

```
Phase A  LOAD SOURCE    Load TPC-H at scale factor SF into <source_schema>.
                        Run ANALYZE so the planner sees fresh stats.

Phase B  BASELINE       EXPLAIN (FORMAT JSON) each query → record plan trees.

Phase C  COLLECT        collect_table_stats() on every source table.
                        Output: in-memory dict[table_name → TableStats]

Phase D  BUILD COPY     CREATE SCHEMA <target_schema>
                        Emit DDL → execute on target schema
                        build_rows_from_canonical(table, n, stats=collected_stats)
                        load_dataframe()
                        inject_stats_postgres(conn, stats, schema=target_schema)

Phase E  REPLAY         EXPLAIN (FORMAT JSON) same queries on target schema.

Phase F  SCORE          Compare source and target plan trees per query:
                          node_jaccard  — Jaccard of node-type multisets
                          within_2x    — fraction of nodes with row estimate ≤ 2×
                          within_10x   — fraction of nodes with row estimate ≤ 10×
```

### Pass criteria

| Metric | Threshold | Meaning |
|---|---|---|
| `mean_node_jaccard` | ≥ 0.70 | Same join operators chosen (Hash Join, Index Scan, etc.) |
| `mean_within_2x` | ≥ 0.50 | Majority of row-count estimates within 2× of source |

Relaxed thresholds (0.60 / 0.40) apply for SF=0.1 smoke tests — at low scale the planner may prefer sequential scans for small tables regardless of statistics.

### Score interpretation for DBAs

| Outcome | Interpretation |
|---|---|
| node_jaccard = 1.0, within_2x = 1.0 | Perfect fidelity — optimizer makes identical decisions |
| node_jaccard ≥ 0.7, within_2x ≥ 0.5 | **PASS** — plans are structurally equivalent; most estimates within 2× |
| node_jaccard < 0.7 | Join order diverges — statistics for at least one table are too imprecise |
| within_2x < 0.5 | Row estimates diverge — stats injection did not override the planner's empty-table estimates |

### How to run it

```bash
# Quick smoke test (SF=0.1, ~1 min, relaxed thresholds)
make test-live-identity PYTEST_FLAGS="-k quick"

# Full validation (SF=1, ~3-5 min, standard thresholds)
make test-live-identity PYTEST_FLAGS="-k sf1"

# Direct CLI (useful for debugging a specific dialect/SF)
python benchmarks/identity_test.py \\
    --schema tpch --sf 1 --dialect postgres \\
    --dsn "host=127.0.0.1 port=5416 dbname=testdb user=postgres password=testpass" \\
    --save-yaml /tmp/identity_artifacts
```

### How to run it on your own schema (not TPC-H)

The identity test framework is not limited to TPC-x schemas. Any database that statschema can `collect` from can be used as the source:

```bash
# Collect from production source (read-only connection)
statschema collect --dialect postgres --host prod-db --user readonly \
    --catalog myapp --top-queries 20

# Load copy to test target and run identity test
python benchmarks/identity_test.py \\
    --schema myapp --sf 1 --dialect postgres --dsn "..." \\
    --queries schema.yaml --skip-load
```

The result gives you a per-query fidelity score for your actual workload — not just TPC-H.

### Files

| File | Purpose |
|---|---|
| `benchmarks/identity_test.py` | Orchestrator — Phases A through F |
| `benchmarks/queries/tpch.yaml` | 4 representative TPC-H queries (Q1, Q3, Q6, Q14) |
| `tests/test_live_identity.py` | pytest integration tests — `make test-live-identity` |
| `benchmarks/results/` | JSON result files from each test run |

---

---

## The problem

### What installs where

The single most important rule: **nothing that requires a binary, a JVM, or a system package should be required on the DBA's machine or in a Databricks notebook.** `pip install` is the only installation mechanism available in a Databricks serverless environment.

#### Development machine (statschema maintainers, CI)

These tools are used to build assets that are then checked into the repo. DBAs never run them.

| Tool | Purpose | Install |
|------|---------|---------|
| `pip install duckdb` | Extract TPC-H (22) and TPC-DS (99) SQL strings into `benchmarks/queries/` YAML files | Python wheel, no binary |
| `make extract-tpc-queries` | One-time script that runs DuckDB and writes the YAML files | Runs in CI; output is committed |

#### DBA / user machine (local laptop, Databricks notebook, serverless cluster)

```bash
# Local
pip install statschema          # or: pipx install statschema

# Databricks notebook (serverless or classic)
%pip install statschema
```

That is the entire installation. The TPC-H and TPC-DS SQL is already checked into the repo inside `benchmarks/queries/`. The Python TPC-B and TPC-C drivers are part of the statschema package. No DuckDB CLI, no CockroachDB binary, no HammerDB, no pgbench, no Java.

#### Current state vs target

| Benchmark | Current execution tool | Pip-installable? | Target (pip-only path) |
|-----------|----------------------|-----------------|------------------------|
| TPC-H | DuckDB `PRAGMA tpch(N)` — test only | ✓ (Python wheel) | Pre-extracted `tpc-h.yaml` → `statschema replay` |
| TPC-DS | DuckDB `PRAGMA tpcds(N)` — test only | ✓ (Python wheel) | Pre-extracted `tpc-ds.yaml` → `statschema replay` |
| TPC-E | Hand-written SELECT frames — DuckDB only | ✓ (Python wheel) | `tpc-e-frames.yaml` → `statschema replay` |
| TPC-B | `pgbench` | ✗ system package | `tpcb_driver.py` — ~100-line Python transaction loop |
| TPC-C | `cockroach workload tpcc` | ✗ Go binary | `tpcc_driver.py` — ~400-line Python transaction driver |
| HammerDB | Tcl/C binary + GUI | ✗ | Not needed — above drivers cover the same ground |
| BenchBase | Java 17 + Maven | ✗ | Not needed — above drivers cover the same ground |

---

## Copyright and query sourcing

### The problem with lifting from open source projects

DuckDB's TPC-H and TPC-DS SQL strings are ultimately derived from the [official TPC benchmark toolkit](https://www.tpc.org/) (the C programs `dbgen` / `dsdgen` / `qgen` / `dsqgen`). DuckDB is MIT-licensed, but the SQL content traces back to TPC-owned specifications.

### What TPC's license actually says

From the TPC-H Standard Specification ([tpc.org](https://www.tpc.org/), Revision 3.0.1, page 6):

> All parties are granted permission to copy and distribute to any party without fee all or part of this material provided that: 1) copying and distribution is done **for the primary purpose of disseminating TPC material**; 2) the TPC copyright notice, the title of the publication, and its date appear, and notice is given that copying is by permission of the Transaction Processing Performance Council.
>
> Parties wishing to copy and distribute TPC materials **other than for the purposes outlined above** (including incorporating TPC material in a nonTPC document, specification or report), **must secure the TPC's written permission**.

Incorporating TPC SQL into a statschema YAML file (a non-TPC artifact) technically requires TPC's written permission under this reading. The same applies to all five benchmark specs.

### Options for sourcing queries without copyright exposure

| Option | Copyright risk | Complexity | Query fidelity |
|--------|---------------|------------|----------------|
| **Extract from DuckDB `tpch_queries()`** | Low in practice (MIT chain, widely done in open source), but TPC spec restriction applies to the SQL content | Trivial — one Python call | Exact DuckDB implementation (params pre-filled) |
| **Use TPC reference toolkit directly** (`qgen`, `dsqgen`) | Same risk — toolkit is TPC-owned | Requires C compilation + parameter substitution | Canonical TPC (with substitution variables) |
| **Parse TPC spec PDF directly** | Same risk — SQL in the PDF is TPC-owned | Significant — PDF parsing + template filling | Closest to canonical specification |
| **Independently implement queries from TPC functional descriptions** | None — functional descriptions specify intent, not SQL syntax; our SQL is our own work | High — 22 + 99 queries to write and validate | Equivalent but not identical |
| **Request TPC written permission** | None if granted | One email to TPC; typically granted for non-commercial benchmarking tools | Any source becomes usable |

### Recommendation

**Short term:** use DuckDB (`pip install duckdb`) to extract the pre-filled SQL. This is what DuckDB, Apache Spark benchmark suites, and virtually every open source database testing tool does. The practical risk is negligible for an evaluation tool that does not publish TPC-certified results. Include a TPC attribution notice in the generated YAML files.

**Best term:** request TPC's written permission. The TPC is a non-profit that exists to promote benchmarking. They routinely grant permission to evaluation tools. A one-paragraph email to contact@tpc.org describing statschema's non-commercial, non-results-publishing use is the cleanest path.

**If permission is denied or not pursued:** implement the 22 TPC-H queries and the highest-impact TPC-DS queries independently from the functional descriptions in the spec PDFs. The spec describes what each query computes (the business logic and relational algebra); our SQL is our own as long as we do not copy the spec's SQL syntax verbatim.

### Attribution block to include in generated YAML files

```yaml
# TPC-H query workload — 22 queries
# SQL derived from DuckDB tpch extension (MIT license), which implements the
# TPC-H benchmark specification. TPC Benchmark™ H is copyright © TPC, all rights reserved.
# TPC-H Standard Specification: https://www.tpc.org/tpch/
# This file is used for evaluation and development purposes only.
# statschema does not publish TPC-certified benchmark results.
source_dialect: duckdb
queries:
  ...
```

### The goal: run TPC queries against any target with nothing but pip

Database vendors publish TPC benchmark results at specific hardware and scale factors. A DBA evaluating a migration target (e.g. Lakebase) runs the same TPC workload to compare. But results change when the dataset changes or when query parameters change, so the evaluation cycle starts over. statschema fixes the dataset: collect statistics once, generate a reproducible synthetic dataset at any scale factor, inject statistics so the optimizer is calibrated. The TPC benchmark then validates that the fixed dataset produces correct and stable query behavior — and the DBA can run that validation from a notebook cell with no binary tools installed.

---

## Key finding: DuckDB already has clean TPC-H and TPC-DS SQL, pip-installable

`duckdb` is a Python wheel — `pip install duckdb` — with no binary tooling required. Its `tpch` and `tpcds` extensions expose all reference queries as plain SQL strings:

```python
import duckdb                              # pip install duckdb
conn = duckdb.connect()
conn.execute("INSTALL tpch; LOAD tpch")
conn.execute("INSTALL tpcds; LOAD tpcds")

tpch_queries  = conn.execute("SELECT query_nr, query FROM tpch_queries()").fetchall()   # 22 rows
tpcds_queries = conn.execute("SELECT query_nr, query FROM tpcds_queries()").fetchall()  # 99 rows
```

**Both sets have parameters pre-filled** — no substitution variables, no `qgen` invocation, no C compiler. The SQL is clean ANSI SQL.

This means DuckDB is a **TPC SQL registry**: extract the 22 + 99 queries once at build time, store them in `benchmarks/queries/tpc-h.yaml` and `benchmarks/queries/tpc-ds.yaml` (checked into the repo), and replay them against any target via `statschema replay`. At runtime the DBA never needs DuckDB — the YAML is already in the repo.

### Databricks notebook — full TPC-H validation in four cells

```python
# Cell 1 — install (works on serverless; no binary required)
%pip install statschema

# Cell 2 — generate TPC-H data on the attached SQL Warehouse / Lakebase
import statschema
statschema.cli.main([
    "load", "benchmarks/schemas/tpch_schema.yaml",
    "--dialect", "databricks",
    "--dsn", "token:dapi.../default",
    "--sf", "1",
])

# Cell 3 — inject optimizer statistics
statschema.cli.main([
    "inject", "--dialect", "databricks",
    "--stats", "tpch_stats.yaml",
    "--dsn", "token:dapi.../default",
])

# Cell 4 — replay all 22 TPC-H queries, collect EXPLAIN plans
statschema.cli.main([
    "replay", "--dialect", "databricks",
    "--queries", "benchmarks/queries/tpc-h.yaml",
    "--show-plans",
    "--dsn", "token:dapi.../default",
])
```

No DuckDB CLI, no `qgen`, no binary tools. The TPC-H SQL is already in the checked-in YAML.

---

## TPC coverage map

| Benchmark | Type | Queries | statschema data gen | Query source | Portable Python execution |
|-----------|------|---------|---------------------|--------------|--------------------------|
| TPC-B | OLTP (simple) | 4 SQL statements | ✓ `tpcb_schema.yaml` | pgbench built-in | **Gap** — pgbench only |
| TPC-C | OLTP (transactional) | 5 transaction types | ✓ `tpcc_schema.yaml` | cockroach workload / hammerdb | **Gap** — no Python reference |
| TPC-H | DSS (analytical) | 22 queries | ✓ `tpch_schema.yaml` | **DuckDB `tpch_queries()`** | ✓ extractable now |
| TPC-DS | DSS (analytical) | 99 queries | ✓ `tpcds_schema.yaml` | **DuckDB `tpcds_queries()`** | ✓ extractable now |
| TPC-E | OLTP (complex) | 10 transaction types | ✓ `tpce_schema.yaml` | hand-written SELECTs in tests | Partial — SELECT frames only |
| **TPC-DI** | **ETL / data integration** | **business rules, not SQL queries** | ✓ **`tpcdi_schema.yaml` (18 tables, target DW)** | **TPC DIGen.JAR (Java)** | ✓ **YAML pipeline replaces JAR** |

---

## TPC-DI: the most relevant benchmark for statschema — and the biggest gap

[TPC-DI](https://www.tpc.org/tpcdi/) benchmarks **data integration** — extracting from OLTP source systems, transforming, and loading into a data warehouse. This is the closest TPC benchmark to what statschema actually does: migrate data from source databases into a target (Lakebase, Databricks Lakehouse).

### How databricks-tpc-di works today

[github.com/shannon-barrow/databricks-tpc-di](https://github.com/shannon-barrow/databricks-tpc-di) is a Databricks implementation of TPC-DI v1.1.0. It demonstrates the exact problems we want to solve:

**Data generation requires a Java JAR:**
> "To generate data you need to be on a non-serverless Unity Catalog-enabled cluster. As of today (until a workaround is found) you will also need to use DBR 15.4 or under (DBR 16.x or higher does not like running the JAR on the driver!)"

The JAR (`DIGen.JAR`) is a TPC-provided binary. It cannot run on Databricks serverless clusters, breaks on DBR 16+, and requires a storage-optimized driver node for large scale factors. This is precisely the binary-dependency problem statschema's pip-only model eliminates.

**No official TPC submission is possible for cloud platforms:**
> "As of writing, the TPC-DI has not modified its submittal/scoring metrics to accommodate cloud-based platforms and infrastructure. Furthermore, as of writing, there has never been an official submission to this benchmark."

Databricks is working with TPC to amend the rules. Until then, any TPC-DI result on Databricks is explicitly non-comparable to published TPC results. This applies to our use case too — we are not publishing TPC results, we are using the benchmark structure for evaluation.

**The disclaimer pattern to follow:**

The repo's footnote is the model for our attribution block:
> *"The Databricks TPC-DI is derived from the TPC-DI and as such is not comparable to published TPC-DI results. [...] Databricks withholds the ability to submit an official submission to the TPC for this benchmark upon future revision of its scoring metrics."*

### Where statschema could replace DIGen.JAR

TPC-DI data generation produces flat files representing OLTP source systems (customer records, trade records, financial data). statschema's `build_rows_from_canonical` already generates realistic synthetic rows from a schema YAML at any scale factor — in pure Python, with no JAR, on any cluster including serverless.

If statschema had a `tpcdi_schema.yaml` covering the TPC-DI source tables, the DBA workflow would become:

```bash
# Today (databricks-tpc-di)
# Requires: non-serverless cluster, DBR ≤ 15.4, JAR on driver
java -jar DIGen.JAR -sf 10 -o /tmp/tpcdi

# Target (statschema)
# Works on: serverless, any DBR, pip install only
%pip install statschema
statschema load benchmarks/schemas/tpcdi_schema.yaml --dialect databricks --sf 10 \
    --dsn "token:dapi.../tpcdi"
```

### The scale factor alignment

databricks-tpc-di scale factors map linearly to raw data size: SF=10 → ~1 GB, SF=100 → ~9.7 GB, SF=1000 → ~97 GB. statschema's `--sf` parameter multiplies `row_count_per_sf` from the schema YAML — exactly the same concept. A `tpcdi_schema.yaml` with appropriate `row_count_per_sf` values per table would produce the same scale factor growth without the JAR.

### Implementation decisions made

All four decisions were made and implemented:

**Decision 1 — Target DW schema only.** `tpcdi_schema.yaml` models the 17 target DW tables, not the DIGen.JAR flat files. The FINWIRE flat file is non-relational and does not fit the YAML schema model. statschema skips ETL entirely: load synthetic data directly into the DW shape.

**Decision 2 — YAML + Python generators for DimDate and DimTime.** The two calendar/clock dimensions need accurate arithmetic, not distribution-based random values. `tpcdi_dimdate_rows()` and `tpcdi_dimtime_rows()` in `benchmarks/tpc_generators.py` provide deterministic sequences. All 15 remaining tables use the YAML pipeline (`generate_rows`).

**Decision 3 — Spec-faithful row counts.** Row counts per SF are derived from TPC-DI v1.1.0 spec §5.3 and cross-checked against the databricks-tpc-di implementation. Noted as approximate in the YAML header.

**Decision 4 — ETL queries deferred.** No `tpcdi-etl.yaml` yet. TPC-DI ETL SQL is dialect-specific (`MERGE INTO`, SCD Type 2 patterns) and needs its own design pass.

### Implementation findings

The schema generates 18 tables (17 DW + DimMessage audit log):

| Scale factor | Total rows | DimTime contribution |
|---|---|---|
| SF=0.1 | ~102 K | 86,400 fixed |
| SF=1 | ~211 K | 86,400 fixed |
| SF=10 | ~1.3 M | 86,400 fixed |
| SF=100 | ~12.2 M | 86,400 fixed |

**DimTime dominates at low scale factors.** At SF=0.1, DimTime's fixed 86,400 rows represent 84% of total rows. This is expected and acceptable — the time dimension is a fixed cardinality lookup table, not scaled data.

**DimDate row count is 3652, not 3650.** The 10-year range 2010-01-01 to 2019-12-31 has 3652 calendar days (2 leap years: 2012, 2016). The YAML and generator are aligned to 3652.

**`time` type is already supported.** The statschema model definition (line 210) explicitly supports `time  TIME (time-of-day without timezone)`. DimTime.TimeValue uses this without needing any new support.

**All 9 FK relationships in DimTrade load correctly.** resolve_load_order handles the star-schema fan-in without cycles; the YAML `load_after` list explicitly declares all 7 parent dimensions.

**`tpcds` and `tpce` were missing from `run_bench.py` choices.** Their YAML schemas already existed but were not listed in `--schema`. Fixed in the same pass.

### Updated workflow (statschema replaces DIGen.JAR)

```bash
# Today (databricks-tpc-di)
# Requires: non-serverless cluster, DBR ≤ 15.4, Java JAR on driver
java -jar DIGen.JAR -sf 10 -o /tmp/tpcdi

# With statschema
# Works on: serverless, any DBR, pip install only
%pip install statschema
statschema load benchmarks/schemas/tpcdi_schema.yaml --dialect databricks --sf 10 \
    --dsn "token:dapi.../tpcdi"
```

### Remaining action items

1. **Contact TPC for written permission** — the same email covering TPC-H/DS (contact@tpc.org) covers TPC-DI. Required before publishing the YAML with TPC-DI attribution.
2. **Add the databricks-tpc-di disclaimer** verbatim to any statschema output that references TPC-DI results.
3. **ETL queries YAML** — future work: capture the transformation SQL from the TPC-DI spec into `benchmarks/queries/tpcdi-etl.yaml` for replay testing.

---

## TPC-H and TPC-DS: extractable today

### Proposed approach

1. Extract all 22 TPC-H and 99 TPC-DS queries from DuckDB at build time into canonical YAML files.
2. Store as `benchmarks/queries/tpc-h.yaml` and `benchmarks/queries/tpc-ds.yaml` in `queries.yaml` format with `source_dialect: duckdb`.
3. `statschema replay --dialect <target> --queries benchmarks/queries/tpc-h.yaml` transpiles via sqlglot and runs EXPLAIN (and eventually execute) against any target.

```yaml
# benchmarks/queries/tpc-h.yaml (generated)
source_dialect: duckdb
queries:
  - id: "tpch-q01"
    source_dialect: duckdb
    sql: |
      SELECT l_returnflag, l_linestatus, sum(l_quantity) AS sum_qty, ...
      FROM lineitem
      WHERE l_shipdate <= CAST('1998-09-02' AS DATE)
      GROUP BY l_returnflag, l_linestatus
      ORDER BY l_returnflag, l_linestatus
    tables: [lineitem]
    calls: 1
    total_elapsed_ms: 0.0
  - id: "tpch-q02"
    ...
```

### Transpilation gaps for TPC-H/DS → non-DuckDB targets

DuckDB SQL is ANSI-close but has some constructs that sqlglot may mistranslate. Open question for reviewers:

| Construct | DuckDB syntax | PostgreSQL | SQL Server | Oracle | Databricks |
|-----------|--------------|------------|------------|--------|------------|
| Date literals | `CAST('1998-09-02' AS DATE)` | same | `CAST('...' AS DATE)` | `DATE '...'` | same |
| `INTERVAL` arithmetic | `INTERVAL '90' DAY` | same | `DATEADD(day, 90, ...)` | `INTERVAL '90' DAY TO SECOND` | same |
| `EXTRACT` | standard | standard | `DATEPART(...)` | standard | standard |
| Correlated subqueries in `FROM` | standard | standard | standard | standard | standard |
| `WITH` (CTEs) | standard | standard | standard | standard | standard |
| `GROUPING SETS` / `ROLLUP` | supported | supported | supported | supported | supported |

TPC-H is mostly clean ANSI SQL. TPC-DS uses more advanced constructs (complex CTEs, `ROLLUP`, correlated subqueries, `CASE` expressions) but should still be >80% portable via sqlglot.

---

## TPC-C: the transactional problem

TPC-C cannot be represented as a list of SQL strings. Its five transaction types (New Order, Payment, Order Status, Delivery, Stock Level) are multi-statement transactions with:
- `BEGIN` / `COMMIT` / `ROLLBACK`
- Row-level locking (`SELECT ... FOR UPDATE`)
- Application-side logic between statements (compute totals, select warehouse at random)
- Retry loops for serialization failures

This makes TPC-C fundamentally different from TPC-H/DS. The queries are not atomic SQL — they are database-application interaction sequences.

### Existing options and their pip-installability

| Project | Install | Targets | Status |
|---------|---------|---------|--------|
| `pytpcc` (py-tpcc) | `pip install` from GitHub | PostgreSQL, MySQL, SQLite | Unmaintained (~2018). Works but fragile. **Closest to pip-only.** |
| CockroachDB `cockroach workload tpcc` | Binary download | Postgres-wire (any) | Active, well-tested. **Not pip-installable.** |
| HammerDB | Binary + GUI | Multi-dialect | Active. **Not pip-installable.** |
| BenchBase | Java 17 + Maven | Multi-dialect | Active. **Not pip-installable.** |

**For the pip-only model:** `pytpcc` is the only option that installs via pip, but it is unmaintained. The right path is a minimal Python TPC-C driver built into statschema — roughly 300–400 lines covering the five transaction types as DB-API calls, with a configurable concurrency level and duration. This would work against any DB-API connection including Databricks SQL Connector.

```bash
# Target model (not yet implemented)
statschema tpcc --dialect lakebase --dsn "..." --duration 60s --concurrency 4
statschema tpcc --dialect databricks --dsn "token:dapi..." --duration 60s --concurrency 4
```

For Databricks SQL Warehouse, `SELECT ... FOR UPDATE` is not supported — TPC-C on Databricks would require a serializable-transaction-aware dialect adaptation.

---

## TPC-B: pgbench is the reference, but not pip-installable

TPC-B is five SQL statements executed in a transaction loop:

```sql
UPDATE pgbench_accounts SET abalance = abalance + :delta WHERE aid = :aid;
SELECT abalance FROM pgbench_accounts WHERE aid = :aid;
UPDATE pgbench_tellers  SET tbalance = tbalance + :delta WHERE tid = :tid;
UPDATE pgbench_branches SET bbalance = bbalance + :delta WHERE bid = :bid;
INSERT INTO pgbench_history (tid, bid, aid, delta, mtime)
    VALUES (:tid, :bid, :aid, :delta, CURRENT_TIMESTAMP);
```

pgbench requires the PostgreSQL client package — **not pip-installable**, and not available in a Databricks notebook environment.

**For the pip-only model:** these five statements can be implemented as a plain Python function using any DB-API connection. The transaction driver (random parameter selection, concurrency, duration loop, TPS measurement) is ~100 lines of Python with `threading` or `asyncio`. This would work against PostgreSQL, Lakebase, CockroachDB, and any Postgres-wire target from a notebook cell.

```bash
# Target model (not yet implemented)
statschema tpcb --dialect lakebase --dsn "..." --duration 60s --concurrency 4 --scale 10
```

---

## TPC-E: SELECT frames vs full transactions

TPC-E has 10 transaction types, each with 2–4 "frames" that are separate SQL statements. `tests/test_tpce_workload.py` implements SELECT frames for all 10 types as individual queries against DuckDB. These are not the official TPC-E queries — they are representative SELECTs that test the schema FK chains.

No Python reference implementation of full TPC-E exists. The official TPC-E toolkit is C++ (EGenLoader, EGenDriver). The SELECT-frame approach in the existing tests is the best available Python approximation.

**Recommendation:** Keep the SELECT-frame tests as-is. For full TPC-E, the path is to extract the frame SQL from the official C++ toolkit and store it in `benchmarks/queries/tpc-e.yaml` — but this requires parsing the C++ source, which is substantial work.

---

## Proposed implementation: `benchmarks/queries/` directory + pip-only drivers

```
benchmarks/
  schemas/
    tpcb_schema.yaml
    tpcc_schema.yaml
    tpch_schema.yaml       ← data generation (exists)
    tpcds_schema.yaml      ← data generation (exists)
    tpce_schema.yaml
  queries/                 ← NEW (checked in; no DuckDB required at runtime)
    tpc-h.yaml             ← 22 queries extracted from DuckDB tpch_queries()
    tpc-ds.yaml            ← 99 queries extracted from DuckDB tpcds_queries()
    tpc-e-frames.yaml      ← SELECT frames from tests/test_tpce_workload.py
    tpc-b.yaml             ← 5 statements (EXPLAIN only; driver handles execution)
src/statschema/
  tpcb_driver.py           ← NEW: ~100-line Python TPC-B transaction loop (no pgbench)
  tpcc_driver.py           ← NEW: ~400-line Python TPC-C transaction driver (no cockroach workload)
```

`make extract-tpc-queries` (run once at dev time, requires `pip install duckdb`) writes `tpc-h.yaml` and `tpc-ds.yaml`. After extraction these files are checked into the repo — **no DuckDB installation required at DBA runtime**.

### pip dependency model

| Feature | pip extras | What it adds |
|---------|-----------|-------------|
| `pip install statschema` | (core) | TPC data gen, DDL transpile, stats inject, replay (EXPLAIN) |
| `pip install statschema[bench]` | `duckdb` | Extract TPC-H/DS SQL at dev time; run TPC-E SELECT frames |
| `pip install statschema[tpcc]` | (none beyond core) | Python TPC-C driver (pure DB-API) |

At DBA runtime, `pip install statschema` is sufficient for TPC-H and TPC-DS replay because the SQL is pre-extracted into the repo. `duckdb` is only a dev/CI dependency for extracting and regenerating those files.

---

## What this enables

With `benchmarks/queries/tpc-h.yaml` in `queries.yaml` format:

```bash
# Generate TPC-H data on Lakebase
statschema load benchmarks/schemas/tpch_schema.yaml --dialect lakebase --sf 1

# Inject optimizer statistics
statschema inject --dialect lakebase --stats tpch_stats.yaml

# Run all 22 TPC-H queries and collect EXPLAIN plans
statschema replay --dialect lakebase --queries benchmarks/queries/tpc-h.yaml --show-plans

# Future: execute queries, collect row counts, compare to DuckDB reference results
statschema replay --dialect lakebase --queries benchmarks/queries/tpc-h.yaml --execute
```

For the first time, this gives a **portable, database-agnostic TPC-H and TPC-DS runner** backed by statschema's reproducible synthetic dataset — without vendor-specific tooling.

---

## Open questions for reviewers

1. **sqlglot coverage for TPC-H → non-DuckDB targets:** Run `statschema replay --dialect postgres --queries benchmarks/queries/tpc-h.yaml` and report which of the 22 queries produce wrong SQL. TPC-H Q13 (outer join with `NOT LIKE`) and Q21 (correlated EXISTS subqueries) are the most likely failure points.

2. **TPC-DS transpilation:** 99 queries is a large surface. Are there specific TPC-DS constructs — complex `ROLLUP`, deeply nested CTEs, `GROUPING SETS` — that sqlglot cannot handle for Databricks SQL?

3. **Parameter substitution for TPC-H/DS execute mode:** DuckDB pre-fills parameters with specific values (e.g. `CAST('1998-09-02' AS DATE)` for Q1). When we eventually add `--execute` mode, should parameters be re-randomized per the TPC spec, or always use DuckDB's fixed values for reproducibility?

4. **TPC-C for Databricks SQL Warehouse:** `SELECT ... FOR UPDATE` is not supported on Databricks SQL Warehouse. Can TPC-C be adapted to use optimistic concurrency (no row locking, application-side conflict detection) and still be a meaningful OLTP benchmark on Databricks? Or is TPC-C out of scope for Lakehouse targets?

5. **Reference results:** DuckDB produces known-correct TPC-H results at each scale factor. Should `benchmarks/queries/tpc-h.yaml` include expected row counts per query at SF=0.01 so `statschema replay --execute` can validate correctness (not just "no error")?

6. **Missed pip-installable implementations:** Is there a maintained Python package (installable via `pip install`) that runs TPC-H or TPC-DS queries against multiple database backends and handles parameter substitution per the TPC spec? Specifically something that works in a Databricks notebook (`%pip install`) without binary dependencies.

7. **Databricks serverless cluster constraints:** Databricks serverless clusters restrict what can be installed and how long init scripts run. Are there constraints on `%pip install duckdb` in serverless context that would break the `[bench]` extras path? If so, the pre-extracted YAML approach (no duckdb at runtime) is even more important.

---

## References

- DuckDB TPC-H extension: [duckdb.org/docs/extensions/tpch](https://duckdb.org/docs/extensions/tpch)
- DuckDB TPC-DS extension: [duckdb.org/docs/extensions/tpcds](https://duckdb.org/docs/extensions/tpcds)
- Official TPC-H spec + qgen: [tpc.org/tpch](https://www.tpc.org/tpch/)
- Official TPC-DS spec + dsdgen/dsqgen: [tpc.org/tpcds](https://www.tpc.org/tpcds/)
- Official TPC-C spec: [tpc.org/tpcc](https://www.tpc.org/tpcc/)
- Official TPC-E spec: [tpc.org/tpce](https://www.tpc.org/tpce/)
- CockroachDB workload TPC-C: [cockroachlabs.com/docs/stable/cockroach-workload](https://www.cockroachlabs.com/docs/stable/cockroach-workload.html)
- py-tpcc (unmaintained): [github.com/apavlo/py-tpcc](https://github.com/apavlo/py-tpcc)
- pgbench (TPC-B): ships with PostgreSQL client package
