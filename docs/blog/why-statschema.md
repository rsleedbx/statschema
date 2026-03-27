# Why statschema

## The plan was correct — for the wrong data

A database evaluation team migrates their schema to a new engine. They load a 10% sample, run
`ANALYZE`, and execute their benchmark queries. The optimizer chooses a sequential scan. On
production, with real cardinalities, it would choose an index seek. The team concludes the new
database is slower. The database is not slower — its optimizer is working from a description of data
it does not have.

This is not an edge case. A MySQL → PostgreSQL migration of a 400 million-row dataset found that
testing at 10% scale caused the optimizer to cross the hash join vs. index scan threshold: plans
were correct for the test data and wrong for production volume. A PostgreSQL major version upgrade
produced sequential scans on a 17 million-row table because row estimates were off by two orders of
magnitude — not because statistics were absent, but because the post-upgrade statistics described
the pre-upgrade data shape. In both cases, query performance investigation began with the same
question: **are the statistics accurate?**

---

## Why statistics are the problem

The query optimizer does not read table data. It reads statistics: row counts, null fractions,
cardinality estimates, most-common values, and histogram bounds stored in the database catalog
(`pg_statistic` in PostgreSQL and its wire-compatible derivatives). From those inputs it chooses
join order, join algorithm, and access path for every query.

When statistics describe the wrong population — a 10% sample, a post-upgrade snapshot, or a freshly
migrated table with no statistics at all — the optimizer makes wrong cardinality estimates. Wrong
estimates lead to wrong plan choices. Wrong plan choices produce the performance regression that
looks like the new database is at fault.

Autovacuum fills `pg_statistic` over time as real traffic runs. For a large database (hundreds of
tables, hundreds of millions of rows), a full `ANALYZE` takes time proportional to the data volume
— minutes to hours depending on hardware and PostgreSQL version. For a team evaluating whether to
migrate at all, real traffic is not available. The evaluation database has no data yet — or has
the wrong data.

---

## The evaluation cycle and where it breaks

Every database evaluation that involves an existing application follows the same sequence:

1. Stand up the candidate database with the production schema.
2. Load data that the optimizer can learn from.
3. Run benchmark queries and compare plans and latencies against the source system.

Step 2 has two practical blockers.

**The compliance gate.** In regulated industries — healthcare, insurance, financial services — a
production data copy requires legal and security review before it can be placed in a staging
environment. GDPR requires documented data transfer agreements. HIPAA requires de-identification of
18 PHI identifiers. This review commonly takes 4–12 weeks. The evaluation cannot start until it
clears.

**The statistics mismatch.** Even when a data copy is available, it describes one point in time at
one scale. Testing at 10% of production volume misses plan regressions that only appear at
production cardinalities. Testing at 100% scale with a stale copy misses skew in recent data.

statschema addresses both blockers through the same mechanism: it collects statistics from the
production source, stores them as portable YAML, and generates synthetic rows whose distributions
match those statistics. Loading those rows gives the optimizer accurate statistics after a
standard `ANALYZE` — with no production data leaving the source system.

---

## "Just run ANALYZE" — and why it is not enough

Running `ANALYZE` after loading data is correct procedure and should always be done. It is not
sufficient in three cases:

**Before real data arrives.** `ANALYZE` requires rows. A freshly provisioned evaluation database
has no rows to analyze. statschema generates rows that match production distributions so the first
`ANALYZE` run produces production-representative statistics.

**After a cross-engine migration.** Source engines differ in how they expose statistics. MySQL,
Oracle, SQL Server, and DB2 each have their own catalog format. statschema reads each catalog
natively — `information_schema.column_statistics` for MySQL, `ALL_TAB_COLUMNS` and `DBMS_STATS`
for Oracle, `sys.dm_db_stats_histogram` for SQL Server, `SYSCAT.COLUMNS` for DB2 — and produces a
common portable YAML. That YAML drives synthetic data generation for any target regardless of
source engine.

**After loading at the wrong scale.** `ANALYZE` accurately describes whatever rows are loaded. If
those rows are a 10% sample, the statistics accurately describe a 10% dataset. statschema generates
at any scale factor from the same schema YAML; distributions reflect the source statistics, not
the sample.

---

## dbldatagen — three modes of use

How statschema relates to row-based synthetic data generators depends on what production data
access is available.

**No production data access.** statschema works standalone from source catalog metadata —
`pg_stats`, `information_schema`, `ALL_TAB_COLUMNS`, `SYSCAT.COLUMNS`. No rows are read.
The output is a `stats.yaml` file that contains no PII and requires no legal review to move between
environments. Tools that generate synthetic data from real rows cannot operate in this mode; they
require row access to learn distributions.

**Production data access available, optimizer bootstrap not needed.**
[Databricks Labs Data Generator (dbldatagen)](https://databrickslabs.github.io/dbldatagen/public_docs/generating_from_existing_data.html)
uses its `DataAnalyzer` class to analyze a source dataframe and generate synthetic data matching
its distributions — a natural fit for teams already on Databricks or Lakebase. Other tools in this
category generate synthetic or privacy-safe copies from real rows. All solve the data generation
problem. statschema adds one step none of them perform: generating rows from catalog statistics
alone, without row access. The tools are additive in this configuration.

**Both — richer stats from dbldatagen fed into statschema.** This is the complementary path on the
roadmap. dbldatagen's `DataAnalyzer.summarizeToDF()` produces a per-column statistical summary of any
source dataframe — value frequencies, range bounds, distribution shape — that is richer than what
the source DB's catalog provides natively, especially for source engines whose default catalog
collection leaves many columns without histogram buckets.

In this configuration, dbldatagen's statistical output feeds statschema's intake (`collect_table_stats`
or a dedicated adapter), statschema generates synthetic rows from the richer distribution, and
the target runs `ANALYZE` on those rows to build an accurate optimizer catalog. dbldatagen handles
data generation from real rows; statschema handles catalog-only generation and schema conversion.

This adapter is on the roadmap.

---

## How statschema works

The pipeline has four steps. No step requires production row access.

```
source DB (production)
   │
   ├─ collect_top_queries() ──▶ queries.yaml   (top-N SQL by frequency / cost)
   │          │                                  [optional — skip for TPC workloads]
   │          │ predicate columns
   │          ▼ (WHERE / JOIN ON / GROUP BY)
   ├─ collect_table_stats(pred_cols=queries.yaml)
   │          ──▶ stats.yaml   (null fractions, n_distinct, MCVs, histogram
   │                            bounds — full depth on predicate columns,
   │                            lightweight on non-predicate columns)
   │
   └─ parse_ddl() ────────────▶ schema.yaml  (types, constraints, FKs)
                                      │
                          build_rows_from_canonical()
                                      │
                              target DB (empty schema)
                                      │
                          ┌───────────┴──────────────────┐
                          │  load synthetic rows          │
                          │  ANALYZE → optimizer catalog  │
                          │                               │
                          │  [optional, pg18+ only]       │
                          │  inject_stats_postgres()      │
                          │  skips load + ANALYZE entirely│
                          └───────────────────────────────┘
```

**collect_top_queries** captures the top-N SQL statements from the source database by execution
frequency or cumulative cost — `pg_stat_statements` for PostgreSQL, `sys.dm_exec_query_stats` for
SQL Server, `performance_schema` for MySQL. The output is a `queries.yaml` workload file. This
step is optional: the TPC benchmark results in this post use the standard TPC queries directly.
For production workloads, capturing the actual top queries produces more accurate plan comparisons
than using benchmark queries as a proxy.

**collect_table_stats** queries the source catalog for per-column statistics using dialect-specific
SQL — `pg_stats` for PostgreSQL, `information_schema.column_statistics` for MySQL,
`sys.dm_db_stats_histogram` for SQL Server, `ALL_TAB_COLUMNS` and `DBMS_STATS` for Oracle,
`SYSCAT.COLUMNS` with `RUNSTATS` for DB2. It collects null fraction, `n_distinct`, min/max values,
most-common values, and histogram bounds. When `queries.yaml` is supplied via the `pred_cols`
option in `CollectionConfig`, `predicate_columns_from_queries` identifies which columns appear in
`WHERE`, `JOIN ON`, and `GROUP BY` clauses. `collect_table_stats` applies full-depth collection
(complete MCV lists, 100-bucket ntile histograms) to those predicate columns and lightweight
collection to the rest. This reduces catalog load on large tables while concentrating precision
where the optimizer needs it most.

**parse_ddl** converts source DDL — MySQL, SQL Server, Oracle, DB2, PostgreSQL — into a
dialect-free canonical YAML. Type mappings (`MONEY` → `NUMERIC(19,4)`, `NVARCHAR` → `VARCHAR`,
`DATETIME` → `TIMESTAMP`) are applied at parse time. FK patterns — inline `REFERENCES`, table-level
`CONSTRAINT … FOREIGN KEY`, and `ALTER TABLE … ADD CONSTRAINT` — are all detected.

**build_rows_from_canonical** generates a `pandas.DataFrame` of referentially consistent synthetic
rows. Value distributions match the collected statistics: MCVs are sampled at their observed
frequencies; histogram-bounded columns sample from the observed range; null fractions are
reproduced exactly. FK relationships are resolved in dependency order. Loading those rows and
running `ANALYZE` gives the target optimizer an accurate view of the data shape.

**inject_stats_postgres** (optional, PostgreSQL 18+ only) writes the collected statistics directly
into the target's catalog using `pg_restore_attribute_stats`. When this path is available it
bypasses data loading entirely — no synthetic rows are needed, no `ANALYZE` is required. The
optimizer reads the injected statistics on the next query. This path is not available on MySQL,
Oracle, SQL Server, DB2, or Lakebase; those targets use the data generation path.

---

## The methodology: identity testing

To measure whether the pipeline reproduces production-representative optimizer behavior, statschema
uses an identity test. The test runs on all six major TPC benchmark schemas (TPC-B, C, H, DI, DS,
E) because they span a range of schema complexity — from TPC-B's four tables to TPC-E's 33 tables
— and workload character — from simple OLTP transactions to complex analytical joins.

**Test procedure (data generation path):**

1. Load the TPC benchmark dataset into a source schema (`A_load_source`).
2. Run `EXPLAIN` on all benchmark queries against the source schema — this is the ground truth
   (`B_baseline_explain`).
3. Collect statistics from the source with `collect_table_stats` (`C_collect_stats`).
4. Build a synthetic copy: emit DDL for the target dialect, generate rows with
   `build_rows_from_canonical`, load them, run `ANALYZE` (`D_build_target`).
5. Run `EXPLAIN` on the same queries against the synthetic target (`E_replay_explain`).
6. Score the results (`F_score`).

For PostgreSQL 18, step 4 can instead inject statistics directly via `pg_restore_attribute_stats`,
skipping row generation and `ANALYZE` entirely. The cross-engine Lakebase tests and all non-PG18
engine tests use the data generation path.

**Metrics:**

- **`node_jaccard`** — Jaccard similarity of plan node-type multisets between source and target.
  A score of 1.0 means the two plans use exactly the same node types in the same proportions.
  A score of 0.70 means 70% of plan nodes match.
- **`within_2x`** — fraction of plan nodes where the synthetic-target row estimate is within 2× of
  the source estimate. A score of 0.50 means half of all row estimates are within 2× of ground
  truth.

**Pass criteria:** `node_jaccard ≥ 0.70` AND `within_2x ≥ 0.50`.

These thresholds are conservative: a plan that shares 70% of its node types and estimates half its
rows within 2× will make the same resource-allocation decisions — memory grants, parallelism
choices, join algorithm selection — as the source plan under production load. The identity test
measures plan structure and row estimate fidelity from `EXPLAIN` output. It does not measure actual
runtime performance, which depends on additional factors outside statistics: hardware, buffer pool
state, parallel worker availability, and runtime data skew.

---

## Results: cross-engine (Lakebase with six source databases)

The cross-engine test answers a harder question: if statistics come from a *different* engine than
the target — MySQL, Oracle, DB2, SQL Server, PostgreSQL 18 — do synthetic rows generated from those
statistics still reproduce Lakebase's optimizer plans after `ANALYZE`?

All 30 combinations (5 engines × 6 TPC schemas) pass. The target is Lakebase (Databricks managed
PostgreSQL); the data generation path is used throughout — no direct stats injection.

### TPC-B (SF=1, ~200 K rows)

| Source engine   | ndistinct\_w2x | range\_covered | null\_match | node\_jaccard | within\_2x | Pass |
|:----------------|---------------:|---------------:|------------:|--------------:|-----------:|:----:|
| PostgreSQL 18   | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | ✓ |
| MySQL 8         | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | ✓ |
| SQL Server 2022 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | ✓ |
| Oracle 21c XE   | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | ✓ |
| DB2 LUW 11.5    | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | ✓ |

### TPC-E (SF=0.01, ~856 K rows — most complex schema tested)

| Source engine   | ndistinct\_w2x | range\_covered | null\_match | node\_jaccard | within\_2x | Pass |
|:----------------|---------------:|---------------:|------------:|--------------:|-----------:|:----:|
| PostgreSQL 18   | 0.780 | 0.943 | 1.000 | 1.000 | 1.000 | ✓ |
| MySQL 8         | 0.759 | 0.927 | 1.000 | 1.000 | 1.000 | ✓ |
| SQL Server 2022 | 0.759 | 0.927 | 1.000 | 1.000 | 1.000 | ✓ |
| Oracle 21c XE   | 0.366 | 0.629 | 1.000 | 0.842 | 0.661 | ✓ |
| DB2 LUW 11.5    | 0.534 | 0.943 | 1.000 | 0.853 | 0.655 | ✓ |

**What the fidelity metrics mean.** `ndistinct_w2x` measures how closely `n_distinct` values
derived from synthetic data (after `ANALYZE`) match the values from the source engine's catalog.
`range_covered` measures whether min/max values stay within 5% of the source range. `null_match`
measures whether null fractions transfer within 0.02.

`null_match` is 1.000 across all 30 runs — null fraction transfer is exact for every engine and
every schema.

`ndistinct_w2x` varies across source engines. The lower values appear where a source engine's
default catalog collection produces sparse cardinality data — fewer histogram buckets or missing
`n_distinct` estimates for certain column types. Despite those lower fidelity scores, plan metrics
still pass: Lakebase's optimizer tolerates moderate `n_distinct` mismatches when join selectivity
is constrained by the schema structure (foreign key cardinalities and indexed column ranges).

The lowest individual `ndistinct_w2x` in the entire test matrix is 0.058 (DB2×TPC-DI), where
almost no cardinality information transferred for the DI staging tables. The plan still passes
(`node_jaccard = 0.867`, `within_2x = 0.525`) because the join order for TPC-DI's staging queries
is primarily determined by FK relationships, not column cardinalities.

Full results for all 36 combinations are in
[`benchmarks/results/target_lakebase.md`](../results/target_lakebase.md).

---

## Results: same-engine with direct injection (PostgreSQL 18)

PostgreSQL 18 introduced `pg_restore_attribute_stats`, which writes collected statistics directly
into `pg_statistic` without loading any rows. This is an optimization path available only on
PostgreSQL 18+ — all other databases use the data generation path above.

All six TPC schemas pass on PostgreSQL 18 with direct injection.

| Schema | Approx. rows | node\_jaccard | within\_2x | Pass |
|:-------|-------------:|-------------:|-----------:|:----:|
| TPC-B  | 200 K        | 1.000        | 1.000      | ✓   |
| TPC-C  | 569 K        | 1.000        | 0.977      | ✓   |
| TPC-H  | 866 K        | 0.964        | 0.833      | ✓   |
| TPC-DI | 697 K        | 1.000        | 1.000      | ✓   |
| TPC-DS | 2.18 M       | 1.000        | 0.900      | ✓   |
| TPC-E  | 856 K        | 1.000        | 1.000      | ✓   |

Direct injection eliminates the round-trip through synthetic data generation and `ANALYZE`, which
accounts for the uniformly high `node_jaccard` scores (5/6 at 1.000). Where the data generation
path produces variance in row estimates (e.g., `within_2x = 0.525` for TPC-DI on Lakebase), direct
injection reproduces `within_2x = 1.000` because the exact source statistics land in the target
catalog unchanged.

---

## Where statschema does not help

**Stored procedure and trigger rewriting.** Cross-dialect migrations that include stored procedures
require rewriting procedural code — T-SQL to PL/pgSQL, PL/SQL to PL/pgSQL, and so on. statschema
has no stored procedure parser. This work is done manually or with a dedicated tool such as
[BladeBridge](https://www.databricks.com/company/newsroom/press-releases/databricks-acquires-bladebridge-technology-and-talent)
(now part of Databricks), which provides LLM-powered code analysis and conversion across more than
20 enterprise data warehouses and ETL tools.

**Network and infrastructure performance.** A SQLite → Neon migration (HN 40820369) showed p79
response time doubling after migration. HN commenters identified the cause as network round-trip
latency between application and database, not query plan quality. statschema addresses optimizer
statistics; network physics are out of scope.

**Zero-downtime cutover mechanics.** CDC pipelines, dual-write setups, traffic mirroring, and
tap-compare testing require live production traffic. statschema generates offline synthetic data; it
is not a proxy, CDC tool, or traffic mirror. For CDC ingestion into Databricks, 
[Lakeflow Connect](https://docs.databricks.com/aws/en/ingestion/overview) provides managed
connectors with built-in change data capture from databases and SaaS applications. Similar
CDC tooling exists for other targets.

**ETL pipeline correctness.** Data movement between engines surfaces cross-compatibility edge cases:
invalid date values such as MySQL's `0000-00-00`, JSON and BLOB encoding differences, and
auto-increment sequence misalignment after a bulk load. These are correctness issues in the
data movement layer. statschema cannot validate that transferred rows are correct.

**Real-world query workloads.** The benchmarks above use TPC standard queries. Real applications
have workloads that differ from TPC in join patterns, filter selectivity, and table access order.
statschema includes `collect_top_queries` to capture the top-N queries from a production workload,
which can then be replayed against the synthetic target to measure plan fidelity on application
queries rather than benchmark queries. This capability is implemented but not yet covered in the
benchmark results above.

**Business rule invariants.** statschema generates structurally and statistically correct data. It
does not enforce application-specific rules — that a US zip code matches its state prefix, that
debit and credit entries balance, that an insurance policy's effective date precedes its expiry
date — unless those rules are encoded explicitly in the schema YAML using `generation: values:`,
`min_value:`, `max_value:`, and `temporal_ordering_constraints:`.

---

## Conclusion

The identity test validates the core claim from the problem statement: when synthetic rows are
generated from production statistics and loaded into the target, `ANALYZE` produces an optimizer
catalog accurate enough to reproduce the source query plans. 30/30 source-engine × TPC-schema
combinations pass on Lakebase using only data generation — no direct stats injection. On
PostgreSQL 18, direct catalog injection (`pg_restore_attribute_stats`) is also available as a
faster alternative that eliminates synthetic data loading entirely; 6/6 TPC schemas pass on that
path with near-perfect scores.

The pipeline — collect statistics from the source, generate synthetic rows from those statistics,
load and analyze on the target — reproduces production-representative optimizer behavior without
moving a single production row. That property makes it applicable wherever a compliance gate, a
wrong-scale test environment, or a cross-engine migration leaves the target's catalog without
accurate statistics.

statschema is open source. The benchmark harness is in
[`benchmarks/identity_test.py`](../../benchmarks/identity_test.py). To try it against your own
schema, start with `statschema collect` against your source database and `statschema load` against
your evaluation target.

Feedback on workloads where the pipeline fails, engines not yet supported, or plan metrics that
diverge from expectation drives the next round of improvements.
