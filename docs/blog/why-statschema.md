# Why statschema

## The plan was correct — for the wrong data

A database evaluation team migrates their schema to a new engine. They load a 10% sample, run
`ANALYZE`, and execute their benchmark queries. The optimizer chooses a sequential scan. On
production, with real cardinalities, it would choose an index seek. The team concludes the new
database is slower. The database is not slower — its optimizer is working from a description of data
it does not have.

This is not an edge case. A documented MySQL → PostgreSQL migration of a 400 million-row dataset
(Medium, 2026) found that testing at 10% scale caused the optimizer to cross the hash join vs. index
scan threshold: plans were correct for the test data and wrong for production volume. A PostgreSQL
14 → 16 upgrade (mu88.github.io, 2024) produced sequential scans on a 17 million-row table because
row estimates were off by two orders of magnitude — not because statistics were absent, but because
the post-upgrade statistics described the pre-upgrade data shape. In both cases, query performance
investigation began with the same question: **are the statistics accurate?**

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
tables, hundreds of millions of rows), a full `ANALYZE` takes hours. For a team evaluating whether
to migrate at all, real traffic is not available. The evaluation database has no data yet — or has
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
production source, stores them as portable YAML, generates synthetic rows whose distributions match
those statistics, and injects the statistics directly into the target catalog before the first
evaluation query runs.

---

## "Just run ANALYZE" — and why it is not enough

Running `ANALYZE` after loading data is correct procedure and should always be done. It is not
sufficient in three cases:

**Before real data arrives.** `ANALYZE` requires rows. A freshly provisioned evaluation database
has no rows to analyze. statschema generates rows that match production distributions and injects
statistics before the first query.

**After a cross-engine migration.** PostgreSQL 18 (released 2025) preserves `pg_statistic` across
same-engine major-version upgrades via `pg_upgrade`. That improvement eliminates the blind period
for PostgreSQL → PostgreSQL upgrades. It does not apply when the source engine is MySQL, Oracle,
SQL Server, DB2, or CockroachDB. Cross-engine migrations always start with an empty catalog on the
target.

**After loading at the wrong scale.** `ANALYZE` accurately describes whatever rows are loaded. If
those rows are a 10% sample, the statistics accurately describe a 10% dataset. The optimizer's
plans are correct for that dataset and incorrect for production volume. statschema generates at any
scale factor from the same schema YAML; the statistics injected reflect the target scale, not the
sample.

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
problem. statschema adds one step none of them perform: injecting the learned statistics directly
into the target catalog so the query optimizer sees production-representative cardinalities before
`ANALYZE` runs. The tools are additive in this configuration.

**Both — richer stats from dbldatagen fed into statschema.** This is the complementary path on the
roadmap. dbldatagen's `DataAnalyzer.summarizeToDF()` produces a statistical summary of any source
dataframe — joint distributions, column value frequencies, range bounds — that is richer than what
the source DB's catalog provides natively, especially for Oracle and DB2, whose default catalog
collection leaves many columns without histogram buckets.

In this configuration, dbldatagen's statistical output feeds statschema's intake (`collect_table_stats`
or a dedicated adapter), statschema generates synthetic rows from the richer distribution, and
`inject_stats_postgres` / `inject_stats_databricks` load the statistics into the target catalog.
dbldatagen handles data generation; statschema handles optimizer bootstrap. The cross-engine
benchmark results show where the richer stats would have the most impact: Oracle×TPC-E
(`ndistinct_w2x = 0.366`) and DB2×TPC-DI (`ndistinct_w2x = 0.058`) are the cases where catalog
sparsity limits what statschema can transfer today.

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
                          ┌───────────┴────────────┐
                          │  load synthetic rows   │
                          │  inject_stats_postgres │
                          │  inject_stats_databricks│
                          └────────────────────────┘
```

**collect_top_queries** captures the top-N SQL statements from the source database by execution
frequency or cumulative cost — `pg_stat_statements` for PostgreSQL, `sys.dm_exec_query_stats` for
SQL Server, `performance_schema` for MySQL. The output is a `queries.yaml` workload file. This
step is optional: the TPC benchmark results in this post use the standard TPC queries directly.
For production workloads, capturing the actual top queries produces more accurate plan comparisons
than using benchmark queries as a proxy.

**collect_table_stats** queries the source catalog for per-column statistics using dialect-specific
SQL — `pg_stats` for PostgreSQL and CockroachDB, `information_schema.column_statistics` for MySQL,
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
reproduced exactly. FK relationships are resolved in dependency order.

**inject_stats_postgres / inject_stats_databricks** writes the collected statistics directly into
the target's catalog using `pg_restore_attribute_stats` (PostgreSQL 18+ and Lakebase) or direct
`pg_statistic` manipulation. The optimizer reads those statistics on the next query — no `ANALYZE`
required, no real rows required.

---

## The methodology: identity testing

To measure whether the pipeline reproduces production-representative optimizer behavior, statschema
uses an identity test. The test runs on all six major TPC benchmark schemas (TPC-B, C, H, DI, DS,
E) because they span a range of schema complexity — from TPC-B's four tables to TPC-E's 33 tables
— and workload character — from simple OLTP transactions to complex analytical joins.

**Test procedure:**

1. Load the TPC benchmark dataset into a source schema (`A_load_source`).
2. Run `EXPLAIN` on all benchmark queries against the source schema — this is the ground truth
   (`B_baseline_explain`).
3. Collect statistics from the source with `collect_table_stats` (`C_collect_stats`).
4. Build a synthetic copy: emit DDL for the target dialect, generate rows with
   `build_rows_from_canonical`, load them, inject the collected stats (`D_build_target`).
5. Run `EXPLAIN` on the same queries against the synthetic target (`E_replay_explain`).
6. Score the results (`F_score`).

**Metrics:**

- **`node_jaccard`** — Jaccard similarity of plan node-type multisets between source and target.
  A score of 1.0 means the two plans use exactly the same node types in the same proportions.
  A score of 0.70 means 70% of plan nodes match.
- **`within_2x`** — fraction of plan nodes where the injected-stats row estimate is within 2× of
  the source estimate. A score of 0.50 means half of all row estimates are within 2× of ground
  truth.

**Pass criteria:** `node_jaccard ≥ 0.70` AND `within_2x ≥ 0.50`.

These thresholds are conservative: a plan that shares 70% of its node types and estimates half its
rows within 2× will make the same resource-allocation decisions — memory grants, parallelism
choices, join algorithm selection — as the source plan under production load.

---

## Results: same-engine (PostgreSQL identity test)

All six TPC schemas pass on PostgreSQL.

| Schema | Approx. rows | node\_jaccard | within\_2x | Pass |
|:-------|-------------:|-------------:|-----------:|:----:|
| TPC-B  | 200 K        | 1.000        | 1.000      | ✓   |
| TPC-C  | 569 K        | 0.859        | 0.932      | ✓   |
| TPC-H  | 866 K        | 0.964        | 0.771      | ✓   |
| TPC-DI | 697 K        | 0.867        | 0.525      | ✓   |
| TPC-DS | 2.18 M       | 1.000        | 1.000      | ✓   |
| TPC-E  | 856 K        | 1.000        | 1.000      | ✓   |

TPC-DI is the most demanding schema in this set: 14 source tables, multiple staging tables, and
date-dimension joins that produce plan nodes whose row estimates are sensitive to cardinality. Its
`within_2x` of 0.525 is just above the pass threshold. TPC-B, TPC-DS, and TPC-E reach perfect
scores — their schemas are either simple (TPC-B) or have regular join patterns that the collected
MCVs and histograms fully describe (TPC-DS, TPC-E).

---

## Results: cross-engine (Lakebase with six source databases)

The cross-engine test answers a harder question: if statistics come from a *different* engine than
the target — MySQL, Oracle, DB2, CockroachDB, SQL Server — do the injected statistics still
reproduce Lakebase's optimizer plans?

All 36 combinations (6 engines × 6 TPC schemas) pass.

### TPC-B (SF=1, ~200 K rows)

| Source engine   | ndistinct\_w2x | range\_covered | null\_match | node\_jaccard | within\_2x | Pass |
|:----------------|---------------:|---------------:|------------:|--------------:|-----------:|:----:|
| PostgreSQL 18   | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | ✓ |
| CockroachDB v23 | 0.882 | —     | 1.000 | 1.000 | 1.000 | ✓ |
| MySQL 8         | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | ✓ |
| SQL Server 2022 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | ✓ |
| Oracle 21c XE   | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | ✓ |
| DB2 LUW 11.5    | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | ✓ |

### TPC-E (SF=0.01, ~856 K rows — most complex schema tested)

| Source engine   | ndistinct\_w2x | range\_covered | null\_match | node\_jaccard | within\_2x | Pass |
|:----------------|---------------:|---------------:|------------:|--------------:|-----------:|:----:|
| PostgreSQL 18   | 0.780 | 0.943 | 1.000 | 1.000 | 1.000 | ✓ |
| CockroachDB v23 | 0.639 | —     | 1.000 | 1.000 | 0.929 | ✓ |
| MySQL 8         | 0.759 | 0.927 | 1.000 | 1.000 | 1.000 | ✓ |
| SQL Server 2022 | 0.759 | 0.927 | 1.000 | 1.000 | 1.000 | ✓ |
| Oracle 21c XE   | 0.366 | 0.629 | 1.000 | 0.842 | 0.661 | ✓ |
| DB2 LUW 11.5    | 0.534 | 0.943 | 1.000 | 0.853 | 0.655 | ✓ |

**What the fidelity metrics mean.** `ndistinct_w2x` measures how closely the injected `n_distinct`
matches what Lakebase's own `ANALYZE` produces after loading the same data. `range_covered`
measures whether min/max values stay within 5% of the source range. `null_match` measures whether
null fractions transfer within 0.02.

`null_match` is 1.000 across all 36 runs — null fraction transfer is exact for every engine and
every schema.

`ndistinct_w2x` is lowest for Oracle×TPC-E (0.366) and DB2×TPC-E (0.534). Both engines produce
sparse cardinality statistics by default: Oracle's `ALL_TAB_COLUMNS` gives `NUM_DISTINCT` but not
histogram buckets for all column types; DB2's `RUNSTATS` with default options skips per-column
histograms for most types. Despite the low fidelity scores, plan metrics pass — Lakebase's
optimizer tolerates moderate `n_distinct` mismatches when join selectivity is constrained by the
schema structure (foreign key cardinalities and indexed column ranges).

The lowest individual `ndistinct_w2x` in the entire test matrix is DB2×TPC-DI at 0.058 — DB2
transferred almost no cardinality information for the DI staging tables. The plan still passes
(`node_jaccard = 0.867`, `within_2x = 0.525`) because the join order for TPC-DI's staging queries
is primarily determined by FK relationships, not column cardinalities.

Full results for all 36 combinations are in
[`benchmarks/results/target_lakebase.md`](../results/target_lakebase.md).

---

## Where statschema does not help

**Stored procedure and trigger rewriting.** A documented SQL Server → PostgreSQL migration required
rewriting 150+ T-SQL procedures to PL/pgSQL. Oracle → PostgreSQL migrations require full PL/SQL →
PL/pgSQL translation. statschema has no stored procedure parser. This work is manual.

**Network and infrastructure performance.** A SQLite → Neon migration (HN 40820369) showed p79
response time doubling after migration. HN commenters identified the cause as network round-trip
latency between application and database, not query plan quality. statschema addresses optimizer
statistics; network physics are out of scope.

**Zero-downtime cutover mechanics.** CDC pipelines, dual-write setups, traffic mirroring, and
tap-compare testing require live production traffic. statschema generates offline synthetic data; it
is not a proxy, CDC tool, or traffic mirror.

**ETL pipeline correctness.** AWS DMS has documented issues with JSON/BLOB integrity, MySQL
`0000-00-00` datetime edge cases, and auto-increment sequence misalignment. These are defects in
the data movement tool. statschema cannot validate that transferred rows are correct.

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

The identity test validates the core claim from the problem statement: when injected statistics
match production statistics, the optimizer produces the same query plans. 6/6 TPC schemas pass on
PostgreSQL. 36/36 source-engine × TPC-schema combinations pass on Lakebase.

The pipeline — collect from source, generate synthetic rows, inject into target — reproduces
production-representative optimizer behavior without moving a single production row. That property
makes it applicable wherever a compliance gate, a wrong-scale test environment, or a cross-engine
migration leaves the target's catalog without accurate statistics.

statschema is open source. The benchmark harness is in
[`benchmarks/identity_test.py`](../../benchmarks/identity_test.py). To try it against your own
schema, start with `statschema collect` against your source database and `statschema load` against
your evaluation target.

Feedback on workloads where the pipeline fails, engines not yet supported, or plan metrics that
diverge from expectation drives the next round of improvements.
