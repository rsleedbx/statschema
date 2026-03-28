# statschema — test objectives

**What the test suite must prove:** statschema works from source database statistics — no production rows required — and produces synthetic data that is correct in three ways: the schema is lossless across dialects, the generated rows have the right distributions and semantics for the target optimizer to behave like production, and standard application and benchmark workloads run against that data without errors.

Nine properties are required:

| # | Objective | Verified by |
|---|-----------|-------------|
| 1 | **Schema correctness** — DDL round-trips produce identical schemas across all dialects | Unit tests, live-DB DDL round-trip tests |
| 2 | **Source type coverage** — every data type the source database can produce maps to a canonical type and round-trips without loss | `test_ddl_roundtrip.py` Phase 5, live-DB round-trip tests |
| 3 | **Row count correctness** — synthetic loads produce the expected row counts at every scale factor | `check_run.py`, `row_count_report.md` |
| 4 | **Distribution fidelity** — null fractions, value ranges, and cardinality in generated data match source statistics within documented tolerances | `test_live_synth.py`, `test_live_stats_transpiler.py`, identity test fidelity metrics |
| 5 | **Stats injection fidelity** — statistics written directly into a target catalog match source stats within tolerance, per engine | `test_live_stats_transpiler.py`, `test_live_stats_minmax.py`, `test_live_lakebase.py` |
| 6 | **Semantic correctness** — generated values are semantically valid for their column type (emails, phones, addresses, UUIDs, SSNs) | `test_semantic_hints.py`, live application schema tests |
| 7 | **Plan fidelity** — EXPLAIN plans on the synthetic target match the source (`node_jaccard ≥ 0.70`, `within_2x ≥ 0.50`) | Identity test (`run_identity.sh`) |
| 8 | **Cross-engine portability** — statistics from any source engine produce correct plans on any target | Lakebase target test (`run_lakebase_target.sh`) |
| 9 | **Benchmark compatibility** — synthetic data drives standard workloads (pgbench, TPC-C, TPC-H, CockroachDB bank) | Benchmark test (`run_bench.sh`) |

---

## 1. Schema correctness

**Goal:** parsing and emitting DDL through the canonical YAML model is lossless — column types, precision, length, constraints, and foreign keys survive unchanged.

A migration that changes a column type silently (e.g. `MONEY` → `FLOAT` or `NVARCHAR` → `TEXT`) causes optimizer regressions unrelated to statistics. Schema correctness must hold before plan fidelity can be meaningful.

**What is tested:**

- Every canonical type (`integer`, `long`, `decimal`, `string`, `float`, `double`, `boolean`, `timestamp`, `date`, `binary`, `timestamptz`, `time`, `timetz`) round-trips through every dialect (MySQL, PostgreSQL, SQL Server, Oracle, Databricks, DB2) without losing precision, length, or constraint.
- All 103 source SQL types across MySQL (38), PostgreSQL (35), and SQL Server (30) parse to the documented canonical type.
- Precision and length boundaries (`DECIMAL(1,0)` through `DECIMAL(38,38)`, `VARCHAR(1)` through `VARCHAR(65535)`) are preserved exactly.
- FK patterns — inline `REFERENCES`, table-level `CONSTRAINT … FOREIGN KEY`, `ALTER TABLE … ADD CONSTRAINT`, cross-schema FKs — are all detected and propagated.
- Migration-specific type mappings hold: SQL Server `TIMESTAMP` → `binary` (not datetime), `MONEY` → `decimal(19,4)`, MySQL `TINYINT(1)` → `boolean`, Oracle `NUMBER(p)` → `integer`/`long`/`decimal` based on digit count.

**Pass criteria:** `make test-fast` (2568 tests, no live DB required) completes with zero failures.

See [`docs/test_plan_ddl_roundtrip.md`](test_plan_ddl_roundtrip.md) for the full phase-by-phase coverage matrix.

---

## 2. Source type coverage

**Goal:** every data type that a source database can produce — including vendor-specific types — maps correctly to a canonical type and round-trips through every target dialect without silent loss of precision, length, or semantics.

A migration tool that drops `MONEY` to `FLOAT`, silently converts `TIMESTAMP WITH TIME ZONE` to a naive timestamp, or maps SQL Server `TIMESTAMP` (a rowversion counter) to a datetime will produce DDL that is structurally valid but semantically wrong. Those errors cause data truncation or optimizer misclassification at load time.

**What is tested per source engine:**

| Engine | Types in parser map | Test coverage |
|---|---|---|
| MySQL 8+ | 51 types | 100% — `test_ddl_roundtrip.py` Phase 5 |
| PostgreSQL 13+ | 51 types | 100% — `test_ddl_roundtrip.py` Phase 5 |
| SQL Server 2019+ | 43 types | 100% — `test_ddl_roundtrip.py` Phase 5 |
| Oracle 19c | emit-only (no open-source parser) | Key types via MySQL fallback; `test_live_oracle.py` |
| Databricks SQL | via MySQL parser | via MySQL map |
| DB2 LUW 11.5 | key types | `test_live_db2.py` |

**Notable type mappings that are explicitly tested:**

- `TINYINT(1)` → `boolean` (MySQL BOOLEAN alias)
- `BIGINT UNSIGNED` → `decimal(20,0)` (exceeds INT64 max)
- `INT UNSIGNED` → `long` (exceeds INT32 max)
- SQL Server `TIMESTAMP` / `ROWVERSION` → `binary` (not a datetime)
- SQL Server `MONEY` → `decimal(19,4)`, `SMALLMONEY` → `decimal(10,4)`
- PostgreSQL `MONEY` → `decimal(19,2)`
- Oracle `NUMBER(p)` without scale → `integer` (p≤9), `long` (10≤p≤18), `decimal(p,0)` (p≥19)
- `BLOB` / `IMAGE` / `BYTEA` / `CLOB` → `binary` or `string` depending on dialect semantics
- Temporal types with fractional-seconds precision (`fsp`) preserved for MySQL (0–6), PostgreSQL (0–6), SQL Server (0–7), Oracle (0–9)

**Live-DB coverage:** `test_live_roundtrip.py` (~300 tests) connects to a real running database, issues `CREATE TABLE` for each canonical type, reads back the column definitions, and verifies that the parsed type matches what was emitted.

**Pass criteria:** all type-map entries have a corresponding test; `test_mysql_type_map_fully_covered`, `test_postgres_type_map_fully_covered`, and `test_sqlserver_type_map_fully_covered` assertions pass. `make test-fast` completes with zero failures.

---

## 3. Row count correctness

**Goal:** the number of rows generated and loaded into every table exactly matches the expected count derived from the schema's scale factor and foreign key cardinalities.

A schema that loads the wrong row counts produces wrong `n_distinct` values after `ANALYZE`, which breaks plan fidelity. Row count correctness is a prerequisite for plan fidelity.

**What is tested:**

- After every identity test run, `check_run.py` counts rows in source and target schemas and compares them to the JSON result file's expected values.
- Source row count equals target row count for every table across all 6 engines × 6 TPC schemas.
- `row_count_report.md` documents the last known-good counts grouped by `(schema, scale_factor)` so cross-engine comparisons are meaningful.

**Pass criteria:** `check_run.py` reports zero mismatches. `row_count_report.md` shows `src == tgt` for every combination.

To regenerate the report:

```bash
.venv_test/bin/python benchmarks/build_row_count_report.py
```

---

## 4. Distribution fidelity

**Goal:** null fractions, value ranges, and cardinality in the generated rows match the source statistics within documented tolerances, so that `ANALYZE` on the target produces a catalog that accurately reflects the source population.

Plan fidelity depends on this. If null fractions are wrong, selectivity estimates for `IS NULL` filters diverge. If value ranges are too narrow, the optimizer miscalculates range predicate selectivity. If cardinality (`n_distinct`) is off by an order of magnitude, join ordering breaks. Distribution fidelity is the data-quality gate that must pass before plan metrics are meaningful.

**Three sub-metrics from `why-statschema.md`:**

| Metric | Definition | Target |
|---|---|---|
| `null_match` | Null fraction in generated data is within 0.02 of source null fraction | 1.000 across all engine × schema combinations |
| `range_covered` | Min/max values stay within 5% of the source range | ≥ 0.90 across all combinations |
| `ndistinct_w2x` | `n_distinct` after `ANALYZE` is within 2× of the source value | Varies by source engine's catalog depth; plan still passes even at lower values |

`null_match` is 1.000 across all 30 Lakebase combinations — null fraction transfer is exact for every engine and schema. `ndistinct_w2x` varies by source engine: engines with sparse catalog collection (DB2×TPC-DI: 0.058) produce lower scores, but plan fidelity still passes because FK cardinalities constrain join order independently of column-level `n_distinct`.

**What is tested:**

- `test_live_synth.py` (17 tests): runs the full collect → generate → load → `ANALYZE` → compare pipeline on MySQL 8 + PostgreSQL 16 + SQL Server; asserts that `ColumnStats` from the synthetic copy matches the source within tolerance.
- `test_live_stats_transpiler.py` (26 tests): MySQL 8 → PostgreSQL 18 cross-engine; verifies that null fractions and `n_distinct` injected from MySQL land in `pg_statistic` correctly.
- `test_live_stats_minmax.py` (9 tests): PostgreSQL 16 + MySQL 8 + SQL Server; verifies MIN/MAX collection for every significant data type.

**Pass criteria:** `null_match = 1.000`, `range_covered ≥ 0.90`, and plan fidelity passes for all engine × schema combinations tested.

---

## 5. Stats injection fidelity

**Goal:** statistics written directly into a target catalog via `inject_stats_*` match the source statistics within tolerance, and the optimizer uses those injected statistics to produce correct query plans — without loading any synthetic rows.

This is the fast path for PostgreSQL 18+. It eliminates the generate → load → `ANALYZE` round-trip entirely. The same canonical YAML that drives row generation also drives direct catalog injection; both paths must produce equivalent optimizer behavior.

**Per-engine injection capabilities:**

| Engine | What is injected | API used |
|---|---|---|
| PostgreSQL 18 · Neon · CockroachDB | Full: MCVs, null fractions, `n_distinct`, histogram bounds | `pg_restore_attribute_stats` |
| MySQL 8.0.31+ · MariaDB | MCVs (low-cardinality), null fractions, `n_distinct`, integer/date histograms | `inject_stats_mysql` |
| Oracle | Row count, `n_distinct`, null count per column | `DBMS_STATS` catalog tables |
| SQL Server | Table-level row count | `UPDATE STATISTICS WITH ROWCOUNT` |
| IBM Db2 | Row count, `n_distinct`, null count, MCVs, histogram quantile bounds | `SYSSTAT.TABLES`, `SYSSTAT.COLUMNS`, `SYSSTAT.COLDIST` |
| Databricks | Bootstrap empty table before first load | Delta / Predictive Optimization handles ongoing stats |

Stats injection is a bootstrap, not a permanent substitute. Native `ANALYZE` / `RUNSTATS` / `GATHER_TABLE_STATS` must run after loading production data.

**What is tested:**

- `test_live_stats_transpiler.py` (26 tests): MySQL 8 → PostgreSQL 18; stats collected from MySQL are injected into PG18 via `pg_restore_attribute_stats`; EXPLAIN estimates on the PG18 target are verified against the MySQL source.
- `test_live_stats_minmax.py` (9 tests): MIN/MAX values survive the collect → inject round-trip for every significant data type across PostgreSQL, MySQL, and SQL Server.
- `test_live_lakebase.py` (10 tests): Databricks Lakebase; DDL round-trip, schema read-back, collect → inject, bulk copy.
- `benchmarks/results/identity_results.md` PostgreSQL 18 section: 6/6 TPC schemas pass via direct injection with near-perfect scores (`node_jaccard = 1.000` on 5 of 6).

**Pass criteria:** injected stats match source stats within the per-engine tolerances above. PostgreSQL 18 identity test passes all 6 TPC schemas via direct injection.

---

## 6. Semantic correctness

**Goal:** generated values are semantically valid for their column type — emails look like emails, phone numbers have the correct format, SSNs follow the XXX-XX-XXXX pattern, addresses resolve to real-looking street names, and UUIDs are well-formed.

This matters for two reasons. First, application workloads (Mautic CRM, Gitea, AdventureWorks) validate input at the application layer — an email field containing random ASCII will cause the application to reject the row. Second, realistic values produce realistic cardinality: a column of random strings has near-unique cardinality regardless of scale, while a column of real-looking US zip codes clusters into ~40,000 distinct values the same way production data does.

**How semantic hints work:**

statschema infers value generators from three sources, applied in priority order:

1. Column name — `email`, `phone`, `zip_code`, `first_name` trigger built-in format patterns.
2. SQL `COMMENT` text — `-- SSN format: XXX-XX-XXXX` in the DDL comment drives the `ssn` generator.
3. `hints.yaml` override file — explicit `format_pattern` per column, checked in with the schema.

Supported locales include `en_US` and `de_DE`; names, addresses, and phone formats vary by locale.

**What is tested:**

- `test_semantic_hints.py` (53 tests): `infer_format_pattern` for all built-in column name patterns, locale loading (`en_US` + `de_DE`), `apply_hints` wiring, stats passthrough, custom hint files.
- `test_live_mautic.py` (19 tests): Mautic 5 + MySQL 8 — a real CRM application schema; generated rows must satisfy Mautic's field constraints (valid emails, formatted phone numbers).
- `test_live_gitea.py` (16 tests): Gitea + PostgreSQL — another real application schema; generated rows must satisfy Gitea's column constraints.
- `test_live_adventureworks.py` (34 tests): AdventureWorks on SQL Server — a reference application schema with address, email, phone, product name fields.

**Pass criteria:** `test_semantic_hints.py` passes offline. Live application schema tests (Mautic, Gitea, AdventureWorks) complete without constraint violations or application-layer rejections.

---

## 7. Plan fidelity (identity test)

**Goal:** after loading synthetic data and running `ANALYZE`, the target optimizer produces the same query plans as the source — the same node types in the same proportions, and row estimates within 2× of ground truth.

This is the core claim of the project. A planner that produces the right plans on synthetic data before any production rows arrive eliminates the compliance-gate delay in cross-engine migrations.

**Method:**

1. Load the TPC benchmark dataset into a source schema.
2. Run `EXPLAIN` on all benchmark queries — this is the ground truth.
3. Collect statistics from the source with `collect_table_stats`.
4. Emit DDL for the target, generate synthetic rows, load them, run `ANALYZE`.
5. Run `EXPLAIN` on the same queries against the synthetic target.
6. Score: `node_jaccard ≥ 0.70` AND `within_2x ≥ 0.50`.

For PostgreSQL 18, step 4 can be replaced by `pg_restore_attribute_stats`, which writes statistics directly into the target catalog without loading any rows.

**Scope:** 6 engines (PostgreSQL, CockroachDB, MySQL, SQL Server, Oracle, DB2) × 6 TPC schemas (B, C, H, DI, DS, E) = 36 combinations.

**Pass criteria:** all 36 combinations pass. Results are in [`benchmarks/results/identity_results.md`](../benchmarks/results/identity_results.md).

**Run:**

```bash
bash benchmarks/run_identity.sh
```

---

## 8. Cross-engine portability (Lakebase target test)

**Goal:** statistics collected from a source engine that differs from the target still produce passing plan fidelity on the target after synthetic data is loaded.

This is the harder case: the source catalog format (MySQL `information_schema.column_statistics`, Oracle `DBMS_STATS`, DB2 `SYSCAT.COLUMNS`) differs from the target. The canonical YAML normalises those differences.

**Scope:** 5 source engines × 6 TPC schemas = 30 combinations, all targeting Databricks Lakebase.

**Pass criteria:** all 30 combinations pass. Results are in [`benchmarks/results/target_lakebase.md`](../benchmarks/results/target_lakebase.md).

**Run:**

```bash
bash benchmarks/run_lakebase_target.sh
```

---

## 9. Benchmark compatibility

**Goal:** synthetic data generated by statschema is structurally and statistically valid enough to drive standard OLTP and OLAP benchmark workloads — the same workloads real databases run.

A generator that only produces rows a `SELECT COUNT(*)` can verify is not sufficient. The rows must satisfy FK constraints, value constraints, and cardinality expectations well enough for pgbench, CockroachDB workload, and TPC-H queries to complete without errors.

**Workloads tested:**

| Workload | Tool | Schema | Engines |
|---|---|---|---|
| TPC-B (pgbench) | `pgbench` | `tpcb` | PostgreSQL, CockroachDB |
| TPC-C | `cockroach workload tpcc` | `tpcc` | CockroachDB, MySQL, Oracle, DB2 |
| TPC-H | `cockroach workload tpch` | `tpch` | CockroachDB, MySQL, Oracle, DB2 |
| CockroachDB bank | `cockroach workload bank` | `tpcb` | CockroachDB |

**Pass criteria:** all workload runs complete without errors and load the expected row counts. Results are in [`benchmarks/results/benchmark_table.md`](../benchmarks/results/benchmark_table.md).

**Run:**

```bash
bash benchmarks/run_bench.sh
```

---

## 10. Step-level testability

**Goal:** every phase of the pipeline can be exercised independently, so a failure in one step can be diagnosed without re-running the full end-to-end test.

End-to-end identity and benchmark tests take hours and touch multiple systems. When a failure occurs — a load error, a schema mismatch, a stats collection gap — it must be possible to re-run only the failing step against already-running databases, without restarting everything.

**The pipeline steps and their independent entry points:**

| Step | What it does | How to run in isolation |
|---|---|---|
| `db_startup` | Start the required databases (Podman containers, Lima VMs) | `start_databases` in `benchmarks/_common.sh`; `make podman-start` / `limactl start` |
| `db_shutdown` | Stop databases cleanly after a run | `podman stop` / `limactl stop` per engine |
| `load_source` | Emit DDL and load synthetic rows into the source schema | `python benchmarks/run_bench.py --schema tpcc --dialect postgres --dsn "..."` |
| `load_target` | Emit DDL and load synthetic rows into the target schema | `python benchmarks/run_bench.py --schema tpcc --dialect cockroachdb --dsn "..."` |
| `validate_source` | Verify the source schema's canonical model — row counts, column types, FK structure | `python benchmarks/check_run.py <result.json> --dialect postgres --dsn "..."` |
| `validate_target` | Verify the target schema's canonical model — row counts, column types, FK structure | `python benchmarks/check_run.py <result.json> --dialect cockroachdb --dsn "..." --target-dialect lakebase --target-dsn "..."` |
| `explain_source` | Run `EXPLAIN` on benchmark queries against the source — captures ground-truth plans | Phase B in `benchmarks/identity_test.py` |
| `explain_target` | Run `EXPLAIN` against the synthetic target — captured for scoring | Phase E in `benchmarks/identity_test.py` |
| `score` | Compute `node_jaccard` and `within_2x` from saved EXPLAIN output | Phase F in `benchmarks/identity_test.py`; re-runnable against saved JSON |

**What step isolation enables:**

- Re-run `validate_source` and `validate_target` against already-loaded databases without touching the load step.
- Re-run `score` against saved EXPLAIN JSON without any live database.
- Run `load_source` on a new engine without disturbing an existing target.
- Run `db_startup` once at the start of a development session and `db_shutdown` once at the end, running multiple test steps in between.

**Pass criteria:** each step can be invoked individually via `run_bench.py`, `check_run.py`, or `identity_test.py` phase flags without requiring a full pipeline re-run. The scripts in `benchmarks/_common.sh` expose `start_databases` and `dsn_*` helpers so individual engines can be started and connected to independently.

---

## Test pyramid

```
         ┌──────────────────────────────┐
         │   Identity + target tests    │  Plan fidelity across 66 combinations
         │   run_identity.sh            │  ~hours (live DBs required)
         │   run_lakebase_target.sh     │
         ├──────────────────────────────┤
         │   Benchmark tests            │  Workload compatibility
         │   run_bench.sh               │  ~30–60 min (live DBs required)
         ├──────────────────────────────┤
         │   Row count verification     │  check_run.py — post-identity check
         ├──────────────────────────────┤
         │   Live-DB integration tests  │  DDL round-trip, synth pipeline,
         │   tests/test_live_*.py       │  stats collect on real engines
         ├──────────────────────────────┤
         │   Offline unit tests         │  Schema parsing, DDL emit,
         │   make test-fast (2568)      │  stats model, data generation
         └──────────────────────────────┘
```

The offline unit tests have no database dependency and run in under 60 seconds. Every layer above them adds a live-DB requirement and a longer runtime. CI should always run the offline layer; the identity and benchmark layers run on demand or on a nightly schedule.

See [`docs/testing.md`](testing.md) for commands to run each layer.

---

## Deployment models (current and planned)

The test suite must verify the same correctness properties regardless of where statschema runs. The table below tracks current status and planned coverage.

### Current

| Deployment model | Status | Notes |
|---|---|---|
| Local developer machine — `.venv_test`, `make test` | ✅ Supported | Offline tests run in <60s with no DB |
| Local containers — Podman (MySQL, PG, CockroachDB, MariaDB, Neon) | ✅ Supported | `docs/local-databases.md` |
| Local VMs — Lima (SQL Server, Oracle, DB2) | ✅ Supported | `config/lima/` |
| Cloud database — Databricks Lakebase | ✅ Supported | `docs/databases/lakebase.md` |
| Cloud database — Neon | ✅ Supported | `docs/databases/neon.md` |
| Credentials — `.env` file at repo root | ✅ Supported | Loaded by `conftest.py` and `_common.sh` |
| CLI — `statschema collect`, `statschema load` | ✅ Supported | Tested by `tests/test_cli.py` |

### Planned

| Deployment model | Target | Notes |
|---|---|---|
| Databricks notebook (Databricks Connect) | Planned | `.venv` with `databricks-connect`; Spark session against remote workspace |
| GitHub Actions CI | Planned | Offline tests on every push; live tests on nightly schedule with secrets in GH Secrets |
| Credentials — Databricks secrets | Planned | `dbutils.secrets.get()` replaces `.env` inside notebooks |
| statschema Docker container | Planned | Single image with Java 17, Python 3.11, PySpark, all drivers pre-installed; eliminates per-developer venv setup |
| Run databases on the cloud (RDS, Azure SQL, etc.) | Planned | Connection DSN replaces local container; no code changes required |

### What each deployment model must satisfy

Regardless of deployment model, all nine objectives above must hold:

1. Schema round-trips are lossless.
2. Every source data type maps to a canonical type and round-trips without loss.
3. Row counts match expected values after every load.
4. Null fractions, value ranges, and cardinality in generated data match source statistics within tolerance.
5. Stats injected directly into the target catalog match source stats within per-engine tolerances.
6. Generated values are semantically valid for their column type.
7. Identity test passes for the target engine (`node_jaccard ≥ 0.70`, `within_2x ≥ 0.50`).
8. Cross-engine portability passes when source and target engines differ.
9. Standard benchmark workloads run to completion on the generated data.

Each objective maps to independently runnable steps — see objective 10 for how to exercise individual pipeline phases without re-running the full end-to-end test.
