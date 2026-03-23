# OLTP Migration Evaluation: Where statschema Helps and Where It Does Not

Based on publicly documented migration projects from Hacker News, Reddit, AWS blogs, and migration consultants (2023–2026). Five migration contexts are examined: SQL Server → PostgreSQL, MySQL → PostgreSQL, Oracle → PostgreSQL, MySQL → Aurora, and SQLite → Neon PostgreSQL.

---

## The common pattern in these stories

Across all documented migrations, the evaluation cycle has four phases where time is lost:

1. **Waiting for a sanitized production data copy** — teams block on legal/compliance review before testing can begin.
2. **Schema conversion errors found late** — type mismatches, FK patterns, IDENTITY vs SERIAL not discovered until data is loaded.
3. **Optimizer-blind period after migration** — target database has no statistics; queries regress; teams investigate what appears to be a failed migration.
4. **Performance tests at wrong scale** — testing at 1% of production volume masks issues that only appear at production scale.

---

## Where statschema shortens the evaluation cycle

### 1. Eliminates the production data copy blocker

GDPR fines reached €1.2 billion in 2024; HIPAA requires de-identification of 18 PHI identifiers before test use. In regulated industries (insurance, healthcare, finance), legal review of a production data copy commonly takes 4–12 weeks before the first test query can run.

statschema generates realistic, referentially consistent data from statistics and schema alone. No production rows are ever copied. The DDL and column statistics from the source database are the only inputs.

**Story this addresses:** Shepherd Insurance (SQLite → Neon, [HN 40820369](https://news.ycombinator.com/item?id=40820369)) and any healthcare or financial services migration where `pg_dump | pg_restore` to a staging environment is prohibited.

**What the migration team would run:**

```bash
# Step 1 — extract DDL from source (SQL Server example; no data leaves the server)
sqlcmd -S prod-sqlserver -Q "SET NOCOUNT ON; EXEC sp_helptext 'dbo.orders'" -o orders.sql

# Step 2 — parse source DDL into portable canonical YAML (one-time, Python)
python3 - <<'EOF'
from statschema.ddl_parser import parse_ddl
from statschema.schema_io import dump_schema
import pathlib

ddl = pathlib.Path("source_schema.sql").read_text()
tables = parse_ddl(ddl, dialect="sqlserver")
dump_schema(tables, path="schema.yaml")
print(f"Parsed {len(tables)} tables → schema.yaml")
EOF

# Step 3 — generate test data at SF=1 (no database required; no production rows)
python -m statschema generate schema.yaml --sf 1 --out-dir ./test-data

# Step 4 — load into the evaluation target (PostgreSQL staging instance)
python -m statschema load schema.yaml --dialect postgres --sf 1 \
    --dsn "host=staging-pg dbname=eval user=dba password=s3cr3t"
```

The `schema.yaml` produced in step 2 is the only artifact that crosses system boundaries.
It contains column names, types, constraints, and statistics — no row values.

---

### 2. Surfaces DDL type-conversion errors before data loading begins

`parse_ddl` converts source DDL to dialect-free canonical YAML in one step. Type mismatches are visible as soon as the YAML is inspected — no data load required.

Documented conversion failures that appear in every SQL Server → PostgreSQL guide:

| Source type | PostgreSQL target | statschema detection |
|-------------|------------------|----------------------|
| `NVARCHAR(n)` | `VARCHAR(n)` | Canonical column shows `varchar`; length preserved |
| `MONEY` | `NUMERIC(19,4)` | Canonical column shows `numeric`; scale explicit |
| `DATETIME` | `TIMESTAMP` | Detected at parse; timezone-awareness noted |
| `SMALLINT` → `INT` identity | `SERIAL` or `SMALLINT` | PK columns assigned `distribution: sequential` |
| `NCHAR(5)` PK (Northwind customer_id) | `CHAR(5)` | Preserved in canonical; FK children typed consistently |

**What the DBA would run on day 1 (before any data load):**

```bash
# Parse SQL Server DDL and emit PostgreSQL DDL immediately
python -m statschema ddl schema.yaml --dialect postgres

# Diff the two DDLs — type changes are immediately visible
python -m statschema ddl schema.yaml --dialect sqlserver > before.sql
python -m statschema ddl schema.yaml --dialect postgres  > after.sql
diff before.sql after.sql
```

Example diff output for the Northwind `order_details` table:

```diff
- unit_price    MONEY         NOT NULL
+ unit_price    NUMERIC(19,4) NOT NULL

- quantity      SMALLINT      NOT NULL DEFAULT 1
+ quantity      SMALLINT      NOT NULL DEFAULT 1

- discount      REAL          NOT NULL DEFAULT 0
+ discount      DOUBLE PRECISION NOT NULL DEFAULT 0.0
```

Every type conversion is explicit and reviewable before a single row is loaded. The 150+ T-SQL procedure review identified in documented SQL Server → PostgreSQL migrations can start from this diff rather than from a failed data load.

---

### 3. Injects optimizer statistics at migration cutover

The most common post-migration performance complaint in documented stories is query plan regression caused by missing statistics. From the PostgreSQL oracle-DBA series (dbakevlar.com, 2025):

> PostgreSQL uses lightweight sampling and assumes uniform data distribution. Statistics are collected via autovacuum or manual ANALYZE. Row estimate errors with skewed data lead to suboptimal plans.

After a migration, the target database has an empty `pg_statistic` table. Autovacuum fills it only after real traffic runs. During the evaluation window — often 1–4 weeks — the planner picks bad plans and teams conclude the target is slower.

statschema collects `ColumnStats` (null rates, `n_distinct`, MCVs, histograms) from the source, stores them as portable YAML, and injects them into the target before the first evaluation query runs.

**Story this addresses:** The PostgreSQL v14 → v16 upgrade timeout (mu88.github.io, 2024) where missing ANALYZE caused severe performance regression. Also the 17M-row Reddit subreddit tracker where queries took 30–60 seconds post-migration until the planner caught up.

**What the migration team would run:**

```python
# --- On the SOURCE database (MySQL example) ---
import pymysql
from statschema.db_stats_collector import collect_table_stats
from statschema.stats_io import dump_stats
from statschema.model import DatabaseStats, TableStats

conn_src = pymysql.connect(host="prod-mysql", user="readonly", password="...",
                           database="ecommerce")

table_names = ["customers", "orders", "order_items", "products", "categories"]
collected = []
for tbl in table_names:
    ts = collect_table_stats(conn_src, tbl, dialect="mysql")
    collected.append(ts)
    print(f"  {tbl}: {ts.row_count:,} rows, {len(ts.columns)} columns")

db_stats = DatabaseStats(database="ecommerce", tables=collected)
dump_stats(db_stats, "prod_stats.yaml")   # portable YAML — no row values
conn_src.close()
```

```bash
# prod_stats.yaml is now safe to move to any environment
# It contains null_fraction, n_distinct, MCVs, histograms — no PII
```

```python
# --- On the TARGET database (PostgreSQL) ---
import psycopg2
from statschema.stats_io import load_stats
from statschema.stats_injector import inject_stats_postgres

conn_tgt = psycopg2.connect("host=staging-pg dbname=ecommerce user=dba password=...")

db_stats = load_stats("prod_stats.yaml")
for ts in db_stats.tables:
    result = inject_stats_postgres(conn_tgt, ts, schema="public")
    print(f"  {ts.name}: injected — {result}")

conn_tgt.commit()
conn_tgt.close()

# The PostgreSQL planner now sees production-representative statistics.
# EXPLAIN plans are meaningful before a single production row has been loaded.
```

Without this step, `EXPLAIN (ANALYZE)` on the target shows row estimates of `rows=1` for every table. With injected statistics, estimates match production cardinalities and the planner chooses the same index paths it would use in production.

---

### 4. Enables volume-realistic testing at any scale factor

Documented migrations consistently report that performance issues only appear at production volume. Testing at 10% scale missed index scan vs. hash join crossover points in the 400M-row MySQL → PostgreSQL migration (Medium, 2026) and the 200GB SQL Server e-commerce migration (techreviewscorner.com).

statschema generates at any scale factor from the same YAML. FK integrity and distribution shapes are preserved at all scales.

```bash
# Evaluation POC — quick schema validation, takes seconds
python -m statschema load schema.yaml --dialect postgres --sf 0.1 \
    --dsn "host=dev-pg dbname=eval user=dba password=..." \
    --strategy bulk_copy

# Migration dry-run — matches production table sizes
python -m statschema load schema.yaml --dialect postgres --sf 1 \
    --dsn "host=staging-pg dbname=eval user=dba password=..." \
    --strategy bulk_copy

# Pre-cutover stress test — 10× production to expose plan regressions
# under future growth; catches hash join → seq scan crossovers
python -m statschema load schema.yaml --dialect postgres --sf 10 \
    --dsn "host=staging-pg dbname=stress user=dba password=..." \
    --strategy bulk_copy

# Append SF=1 on top of an existing load (simulates 2 years of growth)
python -m statschema load schema.yaml --dialect postgres --sf 1 \
    --dsn "host=staging-pg dbname=stress user=dba password=..." \
    --append
```

The `--append` flag continues sequential primary keys from the current max and derives a new seed automatically, so appended rows are distinct from the prior load.

---

### 5. Evaluates multiple target platforms from one schema

Teams choosing between Aurora PostgreSQL, CockroachDB, and Databricks SQL need comparable test loads on each platform. Generating separate test datasets by hand is a common bottleneck.

The same canonical YAML drives DDL emission and data generation for all nine supported dialects.

```bash
# Emit CREATE TABLE DDL for three candidate targets — review differences
python -m statschema ddl schema.yaml --dialect postgres     > ddl_postgres.sql
python -m statschema ddl schema.yaml --dialect cockroachdb  > ddl_crdb.sql
python -m statschema ddl schema.yaml --dialect mysql        > ddl_mysql.sql

# Load identical data into all three for a fair benchmark
python -m statschema load schema.yaml --dialect postgres --sf 1 \
    --dsn "host=aurora-pg ..."

python -m statschema load schema.yaml --dialect cockroachdb --sf 1 \
    --dsn "host=crdb-staging ..."

python -m statschema load schema.yaml --dialect mysql --sf 1 \
    --dsn "host=aurora-mysql ..."
```

Because the same YAML and the same seed (`--seed 42` default) drive all three loads, row counts, FK distributions, and value ranges are identical across platforms. Query latency differences are attributable to the engine, not to dataset variation.

---

### 6. Detects FK pattern differences that break application loading

Three FK patterns exist across dialects, and migration tools routinely miss one of the three:

| Pattern | Example schema | statschema detection |
|---------|---------------|----------------------|
| Inline `REFERENCES` | Django Auth, Sakila | `_parse_inline_fk_constraints` |
| Table-level `CONSTRAINT … FOREIGN KEY` | Sakila, Northwind | `_parse_fk_constraints` |
| `ALTER TABLE … ADD CONSTRAINT FOREIGN KEY` | Chinook, WordPress (DBA-enriched) | `_apply_alter_fks` |

WordPress's schema has no FK DDL at all (by design). The DBA appends FK definitions to the canonical YAML once, and statschema generates referentially consistent data from that point.

**Verifying FK detection before starting a migration:**

```python
from statschema.ddl_parser import parse_ddl
import pathlib

ddl = pathlib.Path("source_schema.sql").read_text()
tables = parse_ddl(ddl, dialect="mysql")      # or "sqlserver", "postgres"

for t in tables:
    fks = t.fk_constraints or []
    if fks:
        for fk in fks:
            print(f"  {t.name}.{fk.columns} → {fk.ref_table}.{fk.ref_columns}")

# If a known FK is missing from the output, add it to schema.yaml manually:
#
#   fk_constraints:
#     - columns: [user_id]
#       ref_table: wp_users
#       ref_columns: [ID]
#
# statschema will enforce referential integrity at data generation time.
```

---

## Where statschema does not help

### 1. Stored procedure and trigger rewriting

A 200GB SQL Server migration required rewriting 150+ T-SQL procedures to PL/pgSQL. Oracle → PostgreSQL migrations require full PL/SQL → PL/pgSQL translation. statschema has no stored procedure parser or transpiler. This work remains manual regardless.

---

### 2. Network and infrastructure performance

The SQLite → Neon migration (HN 40820369) showed p79 response time doubling after migration. Multiple HN commenters identified the cause as network round-trip latency between application server and database, not query plan quality or data distribution. statschema addresses optimizer statistics; it cannot compensate for the physics of TCP over a WAN.

---

### 3. Zero-downtime cutover mechanics

Dual-write setups, CDC pipelines, traffic mirroring, and tap-compare testing all require live production traffic. Reddit's comment backend migration used real traffic routed at a controlled percentage to the new system. statschema generates offline synthetic data; it is not a proxy, CDC tool, or traffic mirror.

---

### 4. ETL tool correctness

AWS DMS known issues include JSON/BLOB integrity loss, MySQL `0000-00-00` datetime edge cases, and auto-increment sequence misalignment after migration. These are defects in the ETL pipeline itself, not in the data's statistical shape. statschema cannot validate that DMS transferred rows correctly.

---

### 5. Application-layer business rule validation

statschema generates structurally and statistically correct data. It does not know that:

- A US shipping address requires a valid state code that matches the zip code prefix.
- A financial transaction's `debit` and `credit` amounts must balance per ledger entry.
- An insurance policy's `effective_date` must be before its `expiry_date`.

Unless these rules are encoded as `generation:` rules in the canonical YAML — `values:` lists, `min_value` / `max_value` constraints, custom format patterns — generated data will not satisfy them. Application test suites that assert business invariants will fail against statschema-generated data unless the YAML is enriched to match those invariants.

```yaml
# Example: encoding an insurance business rule in canonical YAML
columns:
  - name: policy_status
    data_type: varchar
    generation:
      values: ["active", "lapsed", "cancelled", "pending"]

  - name: effective_date
    data_type: date
    generation:
      min_value: "2020-01-01"
      max_value: "2024-12-31"

  - name: expiry_date
    data_type: date
    generation:
      min_value: "2025-01-01"   # always after effective_date range
      max_value: "2029-12-31"
```

Encoding every invariant this way is feasible for a DBA who knows the schema; it is not automatic.

---

### 6. ORM-generated query validation

If an application's ORM generates different SQL in the new dialect (e.g., `LIMIT` vs `TOP`, `||` vs `+` string concat, `ILIKE` vs `LIKE`), that regression is invisible to statschema. The tool validates schema and data shape, not application SQL generation.

---

## Summary

| Evaluation cycle phase | statschema impact | Command / API |
|------------------------|-------------------|---------------|
| Waiting for production data copy | **Eliminates** — no production rows required | `parse_ddl` + `dump_schema` + `load` |
| Schema type-conversion review | **Shortens** — errors visible at DDL parse time | `statschema ddl --dialect postgres` |
| Optimizer-blind period post-migration | **Eliminates** — statistics injected at cutover | `collect_table_stats` + `dump_stats` + `inject_stats_postgres` |
| Volume-realistic stress testing | **Shortens** — any SF from one YAML | `statschema load --sf 10` |
| Multi-platform comparison | **Shortens** — one YAML, any dialect | `statschema load --dialect cockroachdb` |
| FK pattern detection in heterogeneous DDL | **Shortens** — three FK patterns handled | `parse_ddl` + inspect `fk_constraints` |
| Stored procedure rewriting | **No impact** | — |
| Network/infrastructure performance | **No impact** | — |
| Zero-downtime cutover mechanics | **No impact** | — |
| ETL pipeline correctness | **No impact** | — |
| Business rule invariant testing | **Partial** — requires explicit YAML enrichment | `generation: values: / min_value: / max_value:` |
| ORM query generation differences | **No impact** | — |
