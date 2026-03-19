# Stats Transpiler

`schema_parser` is the only library that **transpiles both DDL schema and column statistics** across database dialects.

Every other DDL transpiler stops at the schema.  A migrated database with correct
DDL but default (empty) statistics is still broken — the query optimizer has no
idea how many rows each table has, which values are common, or what the numeric
ranges look like.  It produces bad query plans from day one.

The **stats transpiler** fills that gap: collect column statistics from any source
database, store them as dialect-free YAML, and inject them into any target database
so the query optimizer sees production-scale distributions *before a single row
of production data is loaded*.

---

## What transfers correctly ✅

| Statistic | What it controls | Transfers? |
|-----------|-----------------|-----------|
| **MCVs** (most common values + frequencies) | Per-value selectivity — `WHERE status = 'pending'` vs `WHERE status = 'cancelled'` | ✅ Proven by test |
| **Null fraction** | `IS NULL` / `IS NOT NULL` selectivity | ✅ |
| **n_distinct** (number of distinct values) | Hash/sort join spill estimates | ✅ |
| **Histogram bounds** (numeric / date ranges) | Range predicate selectivity — `WHERE amount BETWEEN 100 AND 500` | ✅ |
| **Row count** | Table cardinality for join order decisions | ✅ (with caveats — see below) |

---

## What does NOT transfer ❌

### 1. Hardware-specific cost thresholds

`seq_page_cost`, `random_page_cost`, `work_mem`, `effective_cache_size` are tuned
to the specific hardware of the source machine.  A target on different hardware
(or a cloud instance with a different disk type) will have different cost
thresholds.  These are not part of the canonical `TableStats` and are not injected.

**Impact**: Cost-based join strategy decisions (nested loop vs. hash join vs.
merge join) may differ between source and target even with identical data
distributions.  This is expected and unavoidable without manual cost calibration.

### 2. Absolute row count accuracy in PostgreSQL 18

PostgreSQL 18's `pg_restore_relation_stats()` / `pg_restore_attribute_stats()`
scales injected `reltuples` proportionally to the **actual physical file size**
of the table's storage (`relpages`).  An empty table has zero physical pages, so
the planner always estimates ~1 row regardless of the injected value.

**Fix**: Load a representative sample first (e.g. 1 000–10 000 rows), run
`ANALYZE`, then inject the production statistics.  The injected MCVs will be
applied on top of the physical baseline.

### 3. Extended (multi-column) statistics

Correlations between columns — e.g. "if `country = 'US'` then `currency` is very
likely `'USD'`" — are not captured by single-column statistics.  Extended
statistics (PostgreSQL `CREATE STATISTICS`, Oracle composite column stats) are
not included in the canonical `TableStats` model.

**Timeline**: PostgreSQL 19 is planned to ship `pg_restore_extended_stats()`.
`schema_parser` will add support once that API is stable.

### 4. SQL Server column-level statistics

SQL Server does not expose a public API to inject column-level statistics
(histograms, MCVs) directly into the statistics catalog.  Only table-level
`ROWCOUNT` and `PAGECOUNT` can be set via `UPDATE STATISTICS … WITH ROWCOUNT`.

**Workaround**: Use `inject_stats_sqlserver()` for the table-level row count
(improves join order), then generate a representative sample with
`build_dataframe_from_canonical()` and load it, then run `UPDATE STATISTICS`
normally.  That path is tested in `test_live_stats_transpiler.py`.

---

## Known limitations by engine

### PostgreSQL 18

| Issue | Impact |
|-------|--------|
| Physical file-size scaling | Injected row count is silently scaled down to match actual page count. Empty table → planner always estimates ~1 row. Load a sample first. |
| Errors reported as WARNINGs | `pg_restore_attribute_stats` reports most failures as warnings, not errors. Partial injection can succeed silently. Always check the function's return value. |
| Extended statistics not supported | `CREATE STATISTICS` (column correlations) cannot be injected. `pg_restore_extended_stats()` is planned for PostgreSQL 19. |
| Index column identification | Bug fixed in Feb 2025 (PG 18.2): index column stats now use `attnum` not `attname`. Injecting index stats with PG 18.0/18.1 may silently produce wrong estimates. |

**Bottom line**: MCVs and null fractions transfer reliably for *table* columns. Index-column stats and extended stats do not.

---

### MySQL 8

| Issue | Impact |
|-------|--------|
| Requires MySQL 8.0.31+ | `ANALYZE TABLE … USING DATA` was added in 8.0.31. Earlier MySQL 8.0.x silently fall back to gathering from data. |
| Equi-height histogram bug [#104789](https://bugs.mysql.com/bug.php?id=104789) | Equi-height buckets are not actually equi-height in practice; highly frequent values are merged while rare values get their own buckets, distorting range selectivity. |
| Equi-height equality predicate bug [#104109](https://bugs.mysql.com/bug.php?id=104109) | For non-integer columns (strings, doubles) the optimizer falls back to `1 / row_count` for equality predicates when using equi-height histograms. This makes cross-engine histogram bound transfer practically useless for string columns. |
| Singleton histograms work, but only for low-cardinality columns | `inject_stats_mysql` uses singleton histograms for MCVs (which avoids bug #104109). Singleton histograms are limited to 1 024 buckets, so high-cardinality columns cannot be fully represented. |
| `innodb_read_only` blocks injection | If the target is a read-replica or `innodb_read_only=ON`, ANALYZE TABLE fails. |
| JSON columns not supported | MySQL histograms do not support JSON-typed columns. |

**Bottom line**: MCVs inject correctly for low-to-medium cardinality string/integer columns via singleton histograms. Range histogram bounds for strings are unreliable due to optimizer bugs. Row count injection via `innodb_table_stats` works but requires `innodb_read_only=OFF`.

---

### Oracle

| Issue | Impact |
|-------|--------|
| `SET_COLUMN_STATS` cannot inject histograms directly | Histogram endpoint values require `PREPARE_COLUMN_VALUES` with `NUMARRAY` / `RAWARRAY`, which is Oracle-internal binary format. `inject_stats_oracle` only sets `distcnt`, `nullcnt`, and `avgclen` — no histogram bounds. |
| No cross-engine histogram format | Oracle's internal endpoint representation (OLE-encoded RAW values) has no documented portable text format. There is no equivalent of PostgreSQL's `most_common_vals` string array. |
| `NVARCHAR` and `ROWID` columns | `PREPARE_COLUMN_VALUES_NVARCHAR` / `_ROWID` do not support histograms at all (only 2-entry MIN/MAX). |
| Identifier case sensitivity | Column names must exactly match the Oracle data dictionary — uppercase for unquoted identifiers, quoted for mixed/lower case. Mismatch raises `ORA-20000`. |
| Best Oracle practice is same-engine transport | `DBMS_STATS.EXPORT_SCHEMA_STATS` / `IMPORT_SCHEMA_STATS` is Oracle's recommended path for cross-instance migration. Cross-engine injection is an approximation at best. |

**Bottom line**: Oracle injection sets cardinality metadata (n_distinct, null count) but **not histograms**. Useful for join order; not useful for range or equality predicate selectivity.

---

### SQL Server

| Issue | Impact |
|-------|--------|
| `UPDATE STATISTICS WITH ROWCOUNT, PAGECOUNT` is **undocumented** | No guarantee of stability across SQL Server versions. Not officially supported by Microsoft. |
| Table-level only | Only row count and page count are set. No column-level MCVs, no histogram bounds, no null fractions. All column-level selectivity defaults remain at SQL Server's built-in heuristics. |
| No public column-level injection API | Unlike PostgreSQL 18, Oracle, and MySQL 8, SQL Server has no SQL command to inject per-column statistics. The only supported path is `UPDATE STATISTICS` after loading real data. |
| Auto-update threshold sensitivity | SQL Server's auto-update statistics fires on modification counters. After injection, the first bulk load may trigger an auto-update that overwrites the injected row count. |

**Bottom line**: SQL Server injection sets only the table's row count. No selectivity improvement for any column filter. Useful purely for influencing join order on multi-table queries.

---

## Primary use cases

### 1. Same-engine migration / upgrade (best case)

Statistics collected from a source are 100% wire-compatible with the same engine
version on the target.  PostgreSQL 18 explicitly ships `pg_dump --statistics-only`
for this use case.

```
PG 14 (old production)  →  collect_table_stats()  →  stats.yaml
                                                           │
                                              load_stats() │
                                                           ▼
                                          PG 18 (new cluster) — inject before data load
```

### 2. Cross-engine migration bootstrap (most impactful)

Data distributions describe the data, not the engine.  A `status` column where
60 % of rows are `'pending'` is the same fact in MySQL, PostgreSQL, Oracle, and
SQL Server.  Injecting those MCVs into the target optimizer immediately gives
the planner the correct selectivity, rather than guessing 1 row for every filter
until `ANALYZE` completes on the full data load.

```
MySQL production (500k rows)
    → collect_table_stats()     → canonical TableStats / stats.yaml
    → emit_ddl("postgres")      → PostgreSQL 18 CREATE TABLE
    → inject_stats_postgres()   → pg_restore_attribute_stats(status MCVs)
    → EXPLAIN shows 300k rows for status='pending'  ✅ (not 1 row)
```

### 3. CI / CD query plan regression testing

Inject production statistics into a CI database that has no production data.
Verify that EXPLAIN plans match the production shape in every pull request.

```yaml
# .github/workflows/test.yml
- name: Inject production statistics into CI database
  run: |
    python -c "
    from schema_parser import load_stats, inject_stats_postgres
    import psycopg2
    conn = psycopg2.connect(...)
    inject_stats_postgres(conn, load_stats('stats/orders.yaml').tables['orders'])
    "
```

---

## Supported targets

| Target | API used | MCVs | Histogram bounds | Row count | Honest notes |
|--------|---------|------|-----------------|---------|-------------|
| **PostgreSQL 18+** | `pg_restore_attribute_stats` | ✅ | ✅ | ⚠️ scaled by file size | Best-in-class API. Empty table = ~1 row estimate regardless of injection. Extended stats (CREATE STATISTICS) not supported. PG 18.0/18.1 has index-column bug. |
| **MySQL 8.0.31+** | `ANALYZE TABLE … USING DATA` + `innodb_table_stats` | ✅ (singleton, ≤1024 buckets) | ⚠️ string equality broken | ✅ | Equi-height histogram bugs #104789 and #104109: string column equality predicates fall back to `1/row_count`. MCVs via singleton histogram work. Requires 8.0.31+. |
| **Oracle 12c+** | `DBMS_STATS.SET_COLUMN_STATS` | ⚠️ as `distcnt` only | ❌ not injectable | ✅ | No portable histogram format. Only n_distinct + null count set per column. Histogram injection requires Oracle-internal binary RAW format. |
| **SQL Server** | `UPDATE STATISTICS … WITH ROWCOUNT, PAGECOUNT` | ❌ | ❌ | ⚠️ undocumented API | Table-level only; undocumented and unsupported by Microsoft. No column-level injection exists. Auto-update may overwrite. |
| **Databricks** | synthetic sample + `ANALYZE TABLE … COMPUTE STATISTICS` | ⚠️ indirect | ⚠️ indirect | ⚠️ sample scale only | **Limited practical value** — see Databricks section below |

---

## Databricks / Delta Lake — honest assessment

> **TL;DR**: `inject_stats_databricks` provides little to no practical benefit for most
> Databricks deployments.  Read this section before using it.

### What Databricks already does automatically

**Delta file-level stats (data skipping)**
Delta collects `min`, `max`, and `null_count` per column on every write.  These
stats live in the Delta transaction log and drive data skipping without any manual
`ANALYZE` or `COMPUTE DELTA STATISTICS` call.  `COMPUTE DELTA STATISTICS` is only
useful for back-filling stats on old files written before liquid clustering was
enabled, or for changing which columns have stats — it adds nothing after a normal
Lakeflow Connect / `writeStream` snapshot load.

**CBO optimizer stats for Unity Catalog managed tables**
If your destination tables are Unity Catalog managed tables and Predictive
Optimization is enabled (default on most workspaces), Databricks runs `ANALYZE`
automatically.  You do not need to call `inject_stats_databricks` or `ANALYZE`
manually.

### When `inject_stats_databricks` *might* help

| Scenario | Worth running? |
|----------|---------------|
| UC managed table + Predictive Optimization enabled | ❌ No — Databricks does it automatically |
| UC managed table, Predictive Optimization disabled | ⚠️ Maybe — one-time `ANALYZE` after first big load is more accurate than a synthetic sample |
| External / non-managed table, heavily joined | ⚠️ Maybe — one-time `ANALYZE` after full data load; synthetic sample is a poor substitute |
| Empty table, no data yet, want optimizer to work before load | ✅ Only real use case — a synthetic sample gives the planner *something* vs. zero stats |
| Lakeflow Connect snapshot destination | ❌ No — Delta stats are written automatically on ingest |

### The fundamental limitation

Unlike PostgreSQL 18's `pg_restore_attribute_stats` or Oracle's `DBMS_STATS`,
Databricks has no API to inject statistics directly into the optimizer catalog.
`inject_stats_databricks` works around this by writing a synthetic data sample
and running `ANALYZE TABLE` on it — the optimizer then sees the sample's
distribution, not the actual production distribution.

This means:
- The row count the optimizer sees is `sample_rows` (e.g. 10 000), not the 500 000
  production rows.
- After loading production data, running `ANALYZE TABLE … COMPUTE STATISTICS`
  replaces the injected stats with accurate stats from the real data.
- A synthetic sample based on MCVs is a reasonable approximation of distribution
  *ratios* but does not capture correlations, tail behaviour, or exact cardinality.

### Recommendation

For Databricks targets:

1. **Enable Predictive Optimization** on your Unity Catalog workspace.  This makes
   `inject_stats_databricks` completely unnecessary for managed tables.

2. **For external tables or managed tables without Predictive Optimization**, run
   `ANALYZE TABLE … COMPUTE STATISTICS FOR COLUMNS` *after* loading production data.
   That gives real stats, not a synthetic approximation.

3. **Only consider `inject_stats_databricks`** if you genuinely need query plans to
   work correctly on a completely empty table before any production data lands, and
   even then, treat the results as a temporary bootstrap that will be replaced by a
   real `ANALYZE` post-load.

---

## Function reference

### `inject_stats_postgres(conn, table_stats, schema="public")`

```python
from schema_parser import load_stats, inject_stats_postgres
import psycopg2

conn   = psycopg2.connect(host="...", dbname="mydb", user="postgres", password="...")
stats  = load_stats("stats/orders.yaml").tables["orders"]
result = inject_stats_postgres(conn, stats)

print(result.success)            # True
print(result.columns_injected)   # 8
print(result.warnings)           # []
```

### `inject_stats_oracle(conn, table_stats, schema=None)`

```python
from schema_parser import load_stats, inject_stats_oracle
import oracledb

conn   = oracledb.connect(user="hr", password="...", dsn="localhost:1521/XEPDB1")
stats  = load_stats("stats/orders.yaml").tables["orders"]
result = inject_stats_oracle(conn, stats, schema="HR")
```

### `inject_stats_mysql(conn, table_stats, schema=None)`

```python
from schema_parser import load_stats, inject_stats_mysql
import pymysql

conn   = pymysql.connect(host="127.0.0.1", port=3384, user="root", password="...", db="shop")
stats  = load_stats("stats/orders.yaml").tables["orders"]
result = inject_stats_mysql(conn, stats, schema="shop")
```

### `inject_stats_sqlserver(conn, table_stats, schema="dbo")`

```python
from schema_parser import load_stats, inject_stats_sqlserver
import pymssql

conn   = pymssql.connect(host="127.0.0.1", port=14330, user="sa", password="...", database="shop")
stats  = load_stats("stats/orders.yaml").tables["orders"]
result = inject_stats_sqlserver(conn, stats, schema="dbo")
# result.warnings includes the column-level API limitation notice
```

### `inject_stats_databricks(spark, table_stats, canonical_schema, …)`

```python
from schema_parser import load_stats, load_canonical, inject_stats_databricks
from pyspark.sql import SparkSession

spark  = SparkSession.builder.appName("migration").getOrCreate()
schema = load_canonical("schema/orders.yaml")[0]
stats  = load_stats("stats/orders.yaml").tables["orders"]

result = inject_stats_databricks(
    spark,
    table_stats=stats,
    canonical_schema=schema,
    table_name="orders",
    database="catalog.shop",
    sample_rows=10_000,
    # table_format="delta" is the default for Databricks Runtime.
    # Use table_format="parquet" for local PySpark testing (see below).
)
print(result.rows_injected)   # 10000
print(result.warnings)        # ['Row count seen by optimizer = 10000 (synthetic sample) …']
```

#### Local PySpark testing

delta-spark 4.x with `DeltaCatalog` does not support `ANALYZE TABLE … COMPUTE STATISTICS`
locally (`NOT_SUPPORTED_COMMAND_FOR_V2_TABLE`).  For local CI testing, use
`table_format="parquet"` which creates a V1 Hive managed table that fully supports
`ANALYZE TABLE`:

```python
# Local PySpark test (CI / development)
from delta import configure_spark_with_delta_pip
from pyspark.sql import SparkSession

builder = (
    SparkSession.builder.master("local[2]")
    .config("spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension")
)
spark = configure_spark_with_delta_pip(builder).getOrCreate()

result = inject_stats_databricks(
    spark, stats, schema,
    table_name="orders_ci",
    table_format="parquet",    # V1 Hive table → ANALYZE TABLE works locally
    sample_rows=500,
)
```

---

## Always run ANALYZE after loading production data

Statistics injection is a **bootstrap**, not a permanent substitute.

```
                   ┌─ inject statistics ──────────────────────────┐
                   │   (query plans work from day one)             │
                   │                                               ▼
Migration DDL  ─── ┤               target DB (empty)  ── load data ─► run ANALYZE
                   │                                                         │
                   └─────────────────────────────────────────────────────────┘
                         (injected stats are replaced by real stats)
```

After loading production data and running native `ANALYZE` / `UPDATE STATISTICS` /
`DBMS_STATS.GATHER_TABLE_STATS`, the optimizer has real statistics from the actual
data and no longer depends on the injected values.

---

## Related

- [`docs/local-databases.md`](local-databases.md) — how to run MySQL, PostgreSQL, Oracle, SQL Server locally
- [`docs/testing.md`](testing.md) — running the stats transpiler test suite
- `tests/test_live_stats_transpiler.py` — live tests proving injection works for all four engines
- `src/schema_parser/stats_injector.py` — implementation
- `src/schema_parser/db_stats_collector.py` — collecting stats from a live source database
