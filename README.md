# statschema — Statistics and Synthetic Data Generator for DBAs and Data Migration Practitioners

[![CI](https://github.com/rsleedbx/statschema/actions/workflows/ci.yml/badge.svg)](https://github.com/rsleedbx/statschema/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/badge/pypi-coming%20soon-orange)](https://pypi.org/project/statschema/)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://pypi.org/project/statschema/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

```
pip install statschema
```

**Repository:** [github.com/rsleedbx/statschema](https://github.com/rsleedbx/statschema) · `git clone https://github.com/rsleedbx/statschema.git`

> **Your query optimizer produces correct plans before you load a single row.**
> Collect schema, column comments, and statistics from any source database into dialect-free YAML. Transpile DDL to any target dialect. Generate semantic-aware synthetic data with correct types, realistic values, and referential integrity. Inject production-scale optimizer statistics into the target database at migration time — without moving a single production row.
>
> **No Spark, no Java, no custom code required for core workflows.** The CLI (`statschema collect`, `inject`, `generate`, `load`) works out of the box. Spark is an optional extra only for large-scale distributed data generation on Databricks.
>
> **The YAML is the living test artifact.** `statschema collect` produces `schema.yaml` and `stats.yaml` automatically from the source database — but the files are plain text and live in version control. After the first test run, developers edit them directly to tune distributions, tighten FK ranges, add missing constraints, or model a second test scenario. The next `statschema generate` call picks up the changes immediately. No code to rewrite, no data generator to redeploy — just edit YAML, regenerate, rerun.

### Quick start

**Transpile DDL — parse any dialect, emit all six targets:**
```python
from statschema import parse_ddl, emit_ddl

tables = parse_ddl("""
CREATE TABLE orders (
    order_id    INT           NOT NULL AUTO_INCREMENT,
    status      VARCHAR(20)   NOT NULL DEFAULT 'pending',
    total       DECIMAL(10,2)     NULL,
    is_paid     TINYINT(1)    NOT NULL DEFAULT 0,
    created_at  DATETIME          NULL,
    PRIMARY KEY (order_id)
) ENGINE=InnoDB;
""", dialect="mysql")

print(emit_ddl(tables[0], "postgres"))    # SERIAL, BOOLEAN, NUMERIC, TIMESTAMP
print(emit_ddl(tables[0], "oracle"))      # NUMBER, TIMESTAMP, GENERATED AS IDENTITY
print(emit_ddl(tables[0], "sqlserver"))   # BIT, DATETIME2, IDENTITY(1,1)
print(emit_ddl(tables[0], "databricks"))  # BIGINT, BOOLEAN, TIMESTAMP_NTZ
print(emit_ddl(tables[0], "mysql"))       # round-trip: AUTO_INCREMENT, TINYINT(1)
print(emit_ddl(tables[0], "db2"))         # GENERATED ALWAYS AS IDENTITY, TIMESTAMP
```

**Generate referentially-correct synthetic data from YAML — with realistic skew and FK fan-out:**

`orders_schema.yaml`:
```yaml
tables:
  - name: orders
    primary_key: [order_id]
    row_count_per_sf: 100000
    columns:
      - name: order_id
        type: integer
        not_null: true
        generation: {distribution: sequential, min_value: 1}
      - name: customer_id
        type: integer
        not_null: true
        generation:
          distribution: zipf          # power-law: top 10% of customers place 60% of orders
          min_value: 1
          max_value: 50000
      - name: status
        type: varchar
        length: 20
        not_null: true
        generation:
          values:   [pending, processing, shipped, delivered, cancelled]
          weights:  [0.08,    0.12,       0.25,    0.50,      0.05]
      - name: order_date
        type: date
        not_null: true
        generation: {distribution: uniform, min_value: "2023-01-01", max_value: "2024-12-31"}
      - name: ship_date
        type: date
        generation:
          distribution: uniform
          min_value: "2023-01-15"
          max_value: "2025-01-31"
          null_rate: 0.08             # ~8% of orders still unshipped

  - name: order_details
    primary_key: [detail_id]
    row_count_per_sf: 350000          # avg 3.5 items/order — but Zipf means a few orders have 20+
    columns:
      - name: detail_id
        type: integer
        not_null: true
        generation: {distribution: sequential, min_value: 1}
      - name: order_id                # FK — sampled with Zipf skew: hot orders get most line items
        type: integer
        not_null: true
      - name: product_id
        type: integer
        not_null: true
        generation:
          distribution: zipf          # top products dominate — long-tail catalog
          min_value: 1
          max_value: 10000
      - name: quantity
        type: integer
        not_null: true
        generation: {distribution: normal, mean: 3, std: 2, min_value: 1, max_value: 100}
      - name: unit_price
        type: decimal
        precision: 10
        scale: 2
        not_null: true
        generation: {distribution: normal, mean: 49.99, std: 30.0, min_value: 0.99, max_value: 999.99}
      - name: discount_pct
        type: decimal
        precision: 5
        scale: 2
        generation:
          distribution: zipf          # most items get no discount; a few get deep cuts
          min_value: 0
          max_value: 50
          null_rate: 0.65             # 65% NULL = no discount at all
    fk_constraints:
      - columns: [order_id]
        parent_table: orders
        parent_columns: [order_id]
```
```python
from statschema import (
    load_canonical, build_rows_from_canonical,
    resolve_row_counts, resolve_load_order,
)

tables  = load_canonical("orders_schema.yaml")
counts  = resolve_row_counts(tables, scale_factor=1.0)
ordered = resolve_load_order(tables)        # topological sort: orders before order_details

for tbl in ordered:
    df = build_rows_from_canonical(tbl, rows=counts[tbl.name], parent_row_counts=counts)
    print(f"{tbl.name}: {len(df):,} rows  —  {list(df.columns)}")
# orders:        100,000 rows
# order_details: 350,000 rows  (Zipf FK: top 1% of orders absorb ~30% of line items)

# Load into any database via pandas / SQLAlchemy — no Spark required
df.to_sql("orders", engine, if_exists="replace", index=False)
```

Three patterns most tools can't express without custom code: **Zipf FK fan-out** (a few hot orders accumulate most line items, matching real e-commerce data), **weighted status distribution** (50% delivered, 5% cancelled — not uniform), and **conditional nulls** (`discount_pct` NULL 65% of the time, non-zero only on promoted SKUs). All declared in YAML; no Python per table.

After the first test run a developer can open `schema.yaml`, change the `delivered` weight from `0.50` to `0.80` to stress-test the hot path, add a `temporal_ordering_constraints: ["ship_date > order_date"]` entry to prevent nonsensical rows, or swap in an override file that cranks up null rates for a data-quality test scenario. The next `statschema generate` call reflects the change immediately. This edit-regenerate-rerun loop — not the initial auto-generation — is where statschema saves the most time in practice.

**Collect DDL and statistics from any live database — no SQL knowledge required:**
```bash
# MySQL — catalog = database name; prompts for password interactively
statschema collect --dialect mysql --host localhost --user root --catalog northwind

# PostgreSQL — catalog = database, schema = namespace (e.g. public)
statschema collect --dialect postgres --host db.example.com \
    --user myuser --catalog prod --schema public --tables 'order%' --show-sql

# SQL Server — catalog = database, schema = dbo (or other); prompts for missing options
statschema collect --dialect sqlserver --catalog mydb --schema dbo

# Oracle — --schema = owner name; prompts for everything else
statschema collect --dialect oracle --host orahost --schema HR

# Lakebase — workspace auth from ~/.databrickscfg DEFAULT profile; no password prompt
statschema collect --dialect lakebase \
    --endpoint 'projects/<proj>/branches/<branch>/endpoints/<ep>' \
    --host '<ep>.database.<region>.cloud.databricks.com' \
    --user '<service-principal-client-id>' --catalog databricks_postgres
```
```
  Connected to postgres @ db.example.com:5432/prod as myuser
  Found 4 table(s) matching 'order%'

    → SQL: SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ...
    → SQL: SELECT column_name, udt_name ... FROM information_schema.columns WHERE table_name = 'orders' ...
    → SQL: SELECT COUNT(*) FROM orders
    → SQL: SELECT status, COUNT(*) FROM orders GROUP BY status ORDER BY 2 DESC LIMIT 10
  ✓  orders                         8 cols, 2 FKs, 50,000 rows  (0.4s)
  ✓  order_details                  6 cols, 2 FKs, 175,000 rows  (1.1s)
  ✓  order_status_history           4 cols, 1 FK,  62,000 rows   (0.8s)
  ✓  order_payments                 7 cols, 1 FK,  48,500 rows   (0.6s)

  Wrote schema.yaml  (4 tables)
  Wrote stats.yaml   (4 tables, 335,500 total rows)
```
Then generate referentially-correct synthetic data at any scale factor:
```bash
statschema generate schema.yaml --sf 10 --out-dir ./data
statschema load     schema.yaml --dialect sqlite --dsn /tmp/test.db --sf 10
```

Python 3.10+ · [Full docs below](#overview)

---

### Evaluate a migration to Lakebase in one afternoon

This end-to-end workflow goes from any OLTP source to Lakebase with realistic, referentially correct data and meaningful `EXPLAIN` plans — no production rows copied, no manual DDL editing, no Spark required.

**Step 1 — Collect schema + statistics from the source (read-only, no data leaves)**
```bash
statschema collect --dialect mysql \
    --host prod-mysql --user readonly \
    --catalog orders_db --tables 'order%'
# Writes schema.yaml  (DDL, FK constraints, comments)
#        stats.yaml   (null rates, MCVs, histograms — kilobytes, no PII)
```

**Step 2 — Spin up a Lakebase endpoint** *(once per project; idempotent)*
```bash
make lakebase-up
# scripts/lakebase-up.sh creates or re-enables the endpoint
# and writes STATSCHEMA_LAKEBASE_HOST / _ENDPOINT / _USER to .env
```

**Step 3 — Transpile DDL and create tables on Lakebase**
```bash
statschema ddl schema.yaml --dialect lakebase > lakebase.sql
psql "host=$STATSCHEMA_LAKEBASE_HOST dbname=databricks_postgres \
      user=$STATSCHEMA_LAKEBASE_USER sslmode=require" -f lakebase.sql
```

**Step 4 — Generate and load realistic synthetic data**
```bash
statschema load schema.yaml --dialect lakebase --sf 1 \
    --dsn "host=$STATSCHEMA_LAKEBASE_HOST dbname=databricks_postgres \
           user=$STATSCHEMA_LAKEBASE_USER sslmode=require"
# FK integrity, Zipf skew, weighted enums, null rates — all from stats.yaml
```

**Step 5 — Inject optimizer statistics so EXPLAIN plans are meaningful from day one**
```bash
statschema inject --dialect postgres \
    --dsn "host=$STATSCHEMA_LAKEBASE_HOST dbname=databricks_postgres \
           user=$STATSCHEMA_LAKEBASE_USER sslmode=require" \
    --stats stats.yaml
# pg_statistic now reflects production null rates, MCVs, and histograms
# EXPLAIN plans match production shape before a single production row is loaded
```

After step 5 you can run application queries, benchmark suites, or migration regression tests against Lakebase with data that behaves like production. Edit `schema.yaml` to tune distributions between runs — change a weight, add a constraint, tighten a range — then re-run from step 4. See [`docs/databases/lakebase.md`](docs/databases/lakebase.md) and the [OLTP migration analysis](docs/learnings/oltp-migration-analysis.md) for the full story.

> **Teardown**: `make lakebase-down` disables the endpoint (zero compute cost, data preserved).

---

### Who is this for

statschema is built for **DBAs and data engineers doing cross-dialect database migrations**. It addresses the gap that exists in the migration window: the target database has a schema but no data, so the optimizer is blind and test queries produce bad plans.

Other synthetic data tools solve a different problem:

| Tool category | Primary user | Requires production data | Optimizer stats injection | Editable YAML tuning artifact | Live multi-dialect test methodology published |
|---|---|---|---|---|---|
| Faker · Mockaroo | App developer — unit test fixtures | No — generates random plausible values | No | No — code per table | No — tests against in-memory data only |
| SDV · Gretel · Tonic | Data scientist / QA — privacy-safe production clone | Yes — trains on or anonymizes actual rows | No | No — black-box model | No — SaaS products; internal test infra not published |
| AWS SCT · pgloader | DBA — schema and data migration | No — schema or data only, no generation | No | No | No — closed source |
| **statschema** | **DBA — cross-dialect migration validation** | **No — works from statistics without the data** | **Yes** | **Yes — plain-text YAML, version-controlled, edit between runs** | **Yes — per-dialect Podman setup, live integration tests, contributor guide** |

The "no production data required" row is the key difference for DBAs. Moving production data to a test environment has two hard blockers:

- **Volume**: a 10 TB production database cannot be copied just to validate a migration target.
- **Security and compliance**: PII, PHI, and PCI data cannot leave the production environment without a de-identification pipeline — which is a separate project in itself.

statschema collects only column statistics (null rates, MCVs, histograms) from the source database. Statistics are read-only, contain no customer data, are kilobytes in size, and are already exposed through standard catalog views (`pg_stats`, `INFORMATION_SCHEMA.COLUMN_STATISTICS`, `ALL_TAB_COL_STATISTICS`). A DBA can collect them, check them into version control alongside the schema, and use them to validate DDL correctness and bootstrap the optimizer on the target — without moving a single production row.

### Overview

```mermaid
flowchart TD
    A["① Optimizer Bootstrap ⭐\ncollect_table_stats\ndump_stats · load_stats · inject_stats_*"]
    B[("② Portable YAML\nschema.yaml  —  structure · comment\nstats.yaml   —  null% · MCVs · histogram\nhints.yaml · patterns/  —  semantic rules")]
    C["③ DDL Transpiler\nparse_ddl · emit_ddl · emit_column_comments"]
    D["④ Stats-Driven Tabular Data\nbuild_dataframe"]
    E["⑤ Semantic Hints\ninfer_format_pattern · apply_hints"]

    A <-->|"collect / inject  —  any dialect"| B
    C <-->|"parse DDL + comments  /  emit DDL ± COMMENT ON"| B
    B -->|"schema · comment · stats"| D
    B <-->|"load patterns & hints.yaml\nwrite inferred generation rules"| E
    E -->|"name → comment → stats inference"| D
```

---

### What makes synthetic data correct

Correct synthetic data requires four ingredients to be in sync:

| Ingredient | Parsed from | Stored in | Drives |
|---|---|---|---|
| **Data types & constraints** | DDL `CREATE TABLE` | `schema.yaml` → `col.type`, `col.not_null`, `col.length`, … | Correct Spark types, NOT NULL, AUTO_INCREMENT, defaults |
| **Column semantics** | SQL `COMMENT` clause, column name, description | `schema.yaml` → `col.comment`, `col.name`, `col.description` | SSN, email, phone, address, UUID generators |
| **Distributions & cardinality** | Live DB statistics | `stats.yaml` → `ColumnStats` | Null rates, MCVs, min/max bounds, histogram shape |
| **FK referential integrity** | DDL constraints + FK statistics | `schema.yaml` → `CanonicalForeignKey`, `stats.yaml` → `ForeignKeyStats` | Valid child rows, realistic fan-out per parent |

statschema parses all four from source databases into dialect-free YAML. Semantic hints infer realistic value generators from column names and `COMMENT` text. Statistics drive null rates, cardinality, and value distributions. FK constraints produce referentially valid rows at the correct fan-out ratio. The portable YAML is the single source of truth — version-controllable, human-editable, and usable to emit DDL for any target database.

---

### Optimizer bootstrap ⭐ — unique to statschema

A migrated database with correct schema but default statistics produces bad query plans: the optimizer has no row counts, no common-value frequencies, and no numeric ranges.

`statschema` collects column statistics from any source database, stores them as dialect-free YAML, and injects them into any target database so the optimizer sees production-scale distributions *before a single row is loaded*. Native `ANALYZE` / `RUNSTATS` / `GATHER_TABLE_STATS` still runs after loading production data — statschema provides the bootstrap so the optimizer is not blind during the cutover window:

```
Source DB (MySQL)                        Target DB (PostgreSQL)
─────────────────                        ──────────────────────
  schema  ──── DDL transpiler ────────►  CREATE TABLE (correct types)
  stats   ──── stats transpiler ──────►  pg_restore_attribute_stats(…)
                                             null_frac, n_distinct,
                                             most_common_vals, histogram_bounds
                                         → optimizer plans match production
                                           before a single row is loaded
```

PostgreSQL 18 independently validated this need by shipping `pg_restore_attribute_stats`
and `pg_dump --statistics-only` — portable optimizer statistics are now a first-class
deployment artifact in PostgreSQL itself.
`statschema` is the only tool that makes those statistics **cross-dialect**.

#### What you get per target engine

Stats injection improves on the baseline (zero statistics = optimizer guesses 1 row
for every filter) even when it cannot inject everything.  The benefit scales with
how much the target engine exposes:

| Engine | What injection gives you | Caveats |
|--------|--------------------------|---------|
| **PostgreSQL 18** · Neon · CockroachDB | Full: MCVs, null fractions, n_distinct, histogram bounds for all types — uses `inject_stats_postgres` (PostgreSQL wire protocol) | Row count needs a sample loaded first (PG18 scales by physical file size). Extended stats (multi-column) not yet supported. CockroachDB and Neon accept the same `pg_restore_attribute_stats` calls. |
| **MySQL 8.0.31+** · MariaDB | MCVs for low-cardinality columns, null fractions, n_distinct, integer/date histogram bounds — uses `inject_stats_mysql` | Equi-height histogram string equality predicate bug — string range histograms fall back to `1/row_count`. Requires MySQL 8.0.31+. MariaDB histogram format differs; injection is best-effort. |
| **Oracle** | Row count, n_distinct, null count per column — enough for correct join ordering | No portable histogram format exists; Oracle uses internal binary encoding that cannot be set externally. |
| **SQL Server** | Table-level row count — improves join ordering on multi-table queries | Column-level injection API does not exist. `UPDATE STATISTICS WITH ROWCOUNT` is undocumented. |
| **Databricks** | Bootstrap an empty table before first data load | Delta collects file stats on every write; UC managed tables with Predictive Optimization run ANALYZE automatically — injection rarely needed. |
| **IBM Db2 LUW** | Full column stats via `inject_stats_db2`: row count (`SYSSTAT.TABLES`), n_distinct / null count / avg length (`SYSSTAT.COLUMNS`), MCVs and histogram quantile bounds (`SYSSTAT.COLDIST TYPE='F'/'Q'`) | Requires SYSADM, SECADM, or CONTROL privilege. |

> **Stats injection is a bootstrap, not a permanent substitute.**  Always run native
> `ANALYZE` / `UPDATE STATISTICS` / `DBMS_STATS.GATHER_TABLE_STATS` after loading
> production data to replace the bootstrap with real statistics.
>
> Full details, per-engine workarounds, and function reference: [`docs/stats_transpiler.md`](docs/stats_transpiler.md)

---

### Built for AI-assisted development

statschema is designed so that an AI agent can add a new dialect, a new stats field, or a new semantic pattern — and the test suite immediately confirms whether it is correct across all nine supported databases.

**3,232 tests · 25 test files · 9 live dialects**

| Category | Tests | What is covered |
|---|---|---|
| DDL round-trip (offline) | 1,830 | Same-dialect identity, cross-dialect emission, Oracle/Postgres/MySQL/SQLServer type mapping, decimal boundaries, string lengths, temporal types, defaults, migration edge cases, canonical YAML pipeline |
| Semantic hints (offline) | 150 | Name inference, comment inference, locale (`en_US`, `de_DE`), custom hint files, `apply_hints` wiring, stats passthrough |
| Stats model, schema parser, builder (offline) | 420 | `ColumnStats` / `TableStats` / `DatabaseStats` serialization, stats I/O, v1 bridge, dbldatagen builder, loader edge paths, override application |
| **Migration round-trip (offline)** | **5** | **Parse real-world DDL → generate at SF=0.1 → load SQLite → run representative queries. Covers Northwind (SQL Server), Sakila (MySQL), Django Auth (PostgreSQL), WordPress (MySQL, no-FK DDL), Chinook (PostgreSQL)** |
| **TPC-DS workload (offline, DuckDB)** | **2** | **Generate all 24 tables at SF=0.01 → load DuckDB → run all 99 official TPC-DS queries; 0 SQL errors, ≥40 queries return rows** |
| **TPC-E workload (offline, DuckDB)** | **16** | **Generate all 32 tables at SF=0.01 → load DuckDB → run representative queries for all 10 TPC-E transaction types; every query returns rows** |
| Live — DDL round-trip | 430+ | Parse DDL on a real database, emit to every other dialect, verify column types survive |
| Live — stats collection | 200+ | `collect_table_stats` on MySQL, PostgreSQL, SQL Server, Oracle, Db2, CockroachDB, MariaDB |
| Live — real schemas | 170+ | Chinook music DB, AdventureWorks, Mautic CRM, Oracle HR — multi-table FK schemas |

**2,539 tests run offline** (no database required) — any contributor or AI agent can run the full offline suite in under 60 seconds on a laptop with no setup. The 693 live tests run against real databases spun up locally with Podman using the per-dialect guides in [`docs/databases/`](docs/databases/).

The migration round-trip tests (`tests/test_migration_roundtrip.py`) are the canonical example of how statschema handles real-world database migrations. Each test represents a migration story a DBA would actually encounter:

| Test | Source | FK pattern | Story |
|------|--------|------------|-------|
| Northwind | SQL Server `NVARCHAR`/`MONEY`/`SMALLINT` | `ALTER TABLE ADD CONSTRAINT` | ERP systems (Dynamics 365, SAP) → PostgreSQL |
| Sakila | MySQL `AUTO_INCREMENT`, `TINYINT UNSIGNED` | Inline `CONSTRAINT … FOREIGN KEY` | Content/media apps (MySQL 5.7 → Aurora) |
| Django Auth | PostgreSQL `SERIAL` | Inline anonymous `REFERENCES` | Any Django web application |
| WordPress | MySQL `BIGINT UNSIGNED`, `longtext` | No FK DDL — DBA-enriched | PHP CMS migration (WordPress, Drupal, Joomla) |
| Chinook | PostgreSQL `INT` PKs | Named `ALTER TABLE` FKs | Analytics and BI platform migrations |

The WordPress test specifically exercises the "no FK DDL" pattern that is pervasive in PHP applications: WordPress deliberately omits foreign key constraints from its schema. The DBA appends the FK definitions to the canonical YAML once, and statschema generates referentially consistent data from that point on — no application code changes, no custom Python.

Every test file follows a single pattern — `pytest` classes with descriptive names — so an AI adding a new feature can read an existing test class, understand the contract, and generate a matching test class for the new feature without reading the full codebase.

Full testing methodology: [`docs/testing.md`](docs/testing.md) · Local database setup: [`docs/local-databases.md`](docs/local-databases.md)

---

Four capabilities, each useful alone — more powerful together:

| Pillar | What it does | Key functions |
|--------|-------------|---------------|
| **Optimizer bootstrap** ⭐ | Collect column statistics (null rates, cardinality, MCVs, histograms) from any source database; store as dialect-free YAML; inject into any target so the optimizer is not blind during migration cutover. Native `ANALYZE` still runs post-load to replace the bootstrap with real statistics. | `collect_table_stats` / `dump_stats` / `load_stats` / `inject_stats_*` |
| **DDL transpiler** | Parse `CREATE TABLE` from any dialect; emit correct DDL for any other — types, defaults, constraints, column comments all preserved per-dialect | `parse_ddl` / `emit_ddl` / `emit_column_comments` |
| **Stats-driven tabular data** | Feed collected statistics into a data generator to produce synthetic rows whose distributions match real production data. **`build_rows_from_canonical`** (pure Python, no Spark) returns a `pd.DataFrame` — suitable for evaluation-scale workloads up to ~10 M rows. **`build_dataframe_from_canonical`** (requires `statschema[spark]`) returns a Spark DataFrame for Databricks-scale generation. | `build_rows_from_canonical` / `build_dataframe_from_canonical` |
| **Semantic hints** | Infer realistic generators (SSN, email, phone, name, …) from column name, SQL COMMENT text, or description; extend or override via `hints.yaml`; locale-aware | `infer_format_pattern` / `load_hints` / `apply_hints` |

**Supported dialects**: MySQL · MariaDB · PostgreSQL · CockroachDB · Neon · **Lakebase** · SQL Server · Oracle · IBM Db2 · Databricks

**Roadmap**: [`docs/ROADMAP.md`](docs/ROADMAP.md)

---

### Expanded view

```mermaid
flowchart TD
    subgraph SRC["Source DB (any dialect)"]
        S1["MySQL DDL\n+ COMMENTs"]
        S2["PostgreSQL DDL\n(COMMENT ON)"]
        S3["SQL Server DDL\n(no comments)"]
        S4["Oracle DDL\n(COMMENT ON)"]
    end

    subgraph YAML["Portable YAML (dialect-free)"]
        SCH[("schema.yaml\nstructure · col.comment\ncol.description")]
        STA[("stats.yaml\nnull% · n_distinct\nMCVs · histogram")]
    end

    subgraph TGT["Target DB (any dialect)"]
        T1["Databricks\ninline COMMENT"]
        T2["PostgreSQL\nCOMMENT ON COLUMN"]
        T3["MySQL\ninline COMMENT"]
        T4["SQL Server\n(comments dropped)"]
        T5["IBM Db2\n(comments dropped)"]
    end

    subgraph GEN["Stats-Driven Tabular Data"]
        DF["Spark DataFrame"]
        DB[("Loaded DB")]
        ST["TableStats\nnull% · cardinality · min/max · MCVs"]
    end

    subgraph SEM["⑤ Semantic Hints (priority order)"]
        SH1["1 col.name\nvs patterns/*.yaml"]
        SH2["2 col.comment\nvs *.comments.yaml"]
        SH3["3 col stats\n(MCVs · planned)"]
        SH4["4 LLM\n(planned)"]
    end

    SRC -- "① collect_table_stats()" --> STA
    STA -- "① inject_stats_*()" --> TGT
    SRC -- "③ parse_ddl()\n(structure + comments)" --> SCH
    SCH -- "③ emit_ddl()\n+ emit_column_comments()" --> TGT
    SCH -- "④ schema + comments" --> DF
    STA -- "④ stats" --> DF
    SEM -- "⑤ format_pattern\ninference" --> DF
    DF  -- "load rows" --> DB
    DB  -- "collect_table_stats()" --> ST
    ST  -. "stats feedback loop" .-> DF
    STA -. "db_stats= param" .-> SEM
```

---

### Detail

**① Stats transpiler** ⭐ — collect statistics from any source, inject into any target:
```
  collect_table_stats(mysql_conn, "orders", dialect="mysql")
        │
        ▼  dump_stats(db_stats, "orders_stats.yaml")   ←  portable, dialect-free
        │
        ▼  load_stats("orders_stats.yaml")             →  DatabaseStats object
        │
        ├──►  build_dataframe_from_canonical(…, stats)  →  generate matching tabular data
        │
        └──►  inject_stats_postgres(conn, stats)   →  pg_restore_attribute_stats(…)  (PG 18)
              inject_stats_mysql(conn, stats)       →  INFORMATION_SCHEMA / ANALYZE TABLE
              inject_stats_sqlserver(conn, stats)   →  UPDATE STATISTICS WITH ROWCOUNT
              inject_stats_oracle(conn, stats)      →  DBMS_STATS.SET_COLUMN_STATS(…)
              inject_stats_databricks(spark, stats) →  Delta bootstrap
              inject_stats_db2(conn, stats)         →  UPDATE SYSSTAT.TABLES / SYSSTAT.COLUMNS
              → query optimizer sees production distributions before data is loaded
```

**③ DDL transpiler** — parse any dialect, emit any dialect, round-trip column comments:
```
MySQL / PostgreSQL / SQL Server / Oracle DDL  (incl. COMMENT clauses)
        │
        ▼  parse_ddl(sql, dialect="…")
  CanonicalTableSchema                        schema.yaml  (portable YAML)
    col.name / col.type / …  ──────────────►  structure
    col.comment              ──────────────►  comment  (from SQL COMMENT clause)
    col.description          ──────────────►  description (human/LLM-added)
        │
        ├──►  emit_ddl("mysql")        →  col … COMMENT 'text'  (inline)
        ├──►  emit_ddl("databricks")   →  col … COMMENT 'text'  (inline)
        ├──►  emit_ddl("postgres")     →  col …  (no inline comment)
        │     emit_column_comments()   →  COMMENT ON COLUMN tbl.col IS 'text';
        ├──►  emit_ddl("oracle")       →  col …  (no inline comment)
        │     emit_column_comments()   →  COMMENT ON COLUMN tbl.col IS 'text';
        └──►  emit_ddl("sqlserver")    →  col …  (comments silently dropped)
             emit_ddl("db2")           →  col …  (comments silently dropped)
```

**② Portable schema** — one YAML file, any target:
```
  dump_schema(tables, "schema.yaml")   ←  version-controllable, dialect-free
  load_canonical("schema.yaml")        →  list[CanonicalTableSchema]
  emit_ddl(tables[0], "oracle")        →  ready to run on any database
```

**④ Stats-driven tabular data** — collect real statistics, generate matching rows:
```
  schema.yaml  +  TableStats (optional)
        │
        ▼  build_dataframe_from_canonical(spark, rows=N, stats=…)
  Spark DataFrame  ──►  load into live DB
                                │
                                ▼  collect_table_stats(conn, "table")
                           TableStats
                     (null%, cardinality, min/max, MCVs)
                                │
                                └──►  feed back  ──►  next generation
                                      (distributions converge each round)
```

---

## Three copy-paste examples

### 1 — Transpile DDL across databases

Same `parse_ddl` / `emit_ddl` call shown in the Quick-start above.

Output (PostgreSQL):
```sql
CREATE TABLE IF NOT EXISTS "orders" (
  "order_id"    SERIAL                NOT NULL,
  "customer_id" INTEGER               NOT NULL,
  "status"      CHARACTER VARYING(20) NOT NULL DEFAULT 'pending',
  "total"       NUMERIC(10,2),
  "is_paid"     BOOLEAN               NOT NULL DEFAULT FALSE,
  "created_at"  TIMESTAMP,
  PRIMARY KEY ("order_id")
);
```

> `TINYINT(1) DEFAULT 0` → `BOOLEAN DEFAULT FALSE` (PostgreSQL), `BIT DEFAULT 0` (SQL Server).
> `DATETIME` → `TIMESTAMP` (PostgreSQL), `DATETIME2` (SQL Server), `TIMESTAMP_NTZ` (Databricks).
> Every semantic correction is applied automatically.

---

### 2 — Save as portable schema (YAML)

```python
from src.statschema import parse_ddl, dump_schema, load_canonical, emit_ddl

# Parse once — save as portable YAML
tables = parse_ddl(open("schema.sql").read(), dialect="mysql")
dump_schema(tables, "schema.yaml")          # version-controllable, dialect-free

# Later: load and target any database
tables = load_canonical("schema.yaml")
print(emit_ddl(tables[0], "databricks"))
```

`schema.yaml` excerpt (see [Canonical YAML formats](#canonical-yaml-formats) for the full structure):
```yaml
tables:
  - name: orders
    columns:
      - name: order_id
        type: integer
        not_null: true
        primary_key: true
        auto_increment: true
      - name: customer_ssn
        type: string
        length: 11
        comment: "Customer social security number"   # from SQL COMMENT clause → drives SSN generation
      - name: is_paid
        type: boolean
        not_null: true
        default: "false"
```

---

### 3 — Generate stats-driven tabular data

```python
from src.statschema import (
    parse_ddl, make_default_stats,
    collect_table_stats, build_rows_from_canonical,
)

# Parse schema
schema = parse_ddl(open("schema.sql").read(), dialect="mysql")[0]

# Option A: generate from heuristic defaults (no live DB, no Spark)
stats = make_default_stats([schema], row_count=100_000).tables[0]
df = build_rows_from_canonical(schema, rows=100_000, stats=stats)
df.to_sql("orders", engine, if_exists="replace", index=False)

# Option B: collect REAL stats from a live database first
import pymysql
conn = pymysql.connect(host="127.0.0.1", port=3306, user="root", password="…")
real_stats = collect_table_stats(conn, "orders", dialect="mysql")
df = build_rows_from_canonical(schema, rows=100_000, stats=real_stats)

# For Databricks-scale generation (100M+ rows on a cluster), use the Spark path:
#   pip install 'statschema[spark]'
#   from statschema import build_dataframe_from_canonical
#   df = build_dataframe_from_canonical(spark, schema, rows=1_000_000, stats=real_stats)
```

The generated DataFrame is parameterized by:
- **null rates** per column (e.g. `total` is NULL 8.3% of the time, matching the measured `null_fraction`)
- **cardinality** (only 4 distinct `status` values, weighted by measured MCV frequencies)
- **numeric ranges** (min/max bounds from the collected statistics)
- **string patterns** when a `format_pattern` is set on a column — either explicitly or inferred automatically by name

---

## Semantic data generation

Column names like `ssn`, `email`, `first_name`, and `phone` are automatically matched to realistic Faker-based generators — no manual configuration needed.

### Three input sources, stored separately in YAML

The DDL parser and stats collector populate three independent fields that semantic inference draws from, in priority order:

| Priority | Source | Field | Stored in | Pattern file |
|----------|--------|-------|-----------|--------------|
| 1 (highest) | Column name / identifier | `col.name` | `schema.yaml` | `patterns/<locale>.yaml` |
| 2 | SQL COMMENT clause | `col.comment` | `schema.yaml` | `patterns/<locale>.comments.yaml` |
| 3 | Human / LLM description | `col.description` | `schema.yaml` | `patterns/<locale>.comments.yaml` |
| 4 *(planned)* | Column statistics | `ColumnStats` | `stats.yaml` | MCV pattern matching |
| 5 *(planned)* | LLM inference | — | — | Foundation model call |

**Option A — built-in name inference (zero config)**

`infer_format_pattern()` matches a column name against a shipped YAML pattern file and returns a `format_pattern`.  Called automatically inside `build_dataframe_from_canonical()` and `to_v1_plan()`.

```python
from src.statschema.semantic_hints import infer_format_pattern, load_builtin_patterns

infer_format_pattern("customer_ssn")                           # → "ssn"
infer_format_pattern("email_address")                          # → "email"
infer_format_pattern("vorname", hints=load_builtin_patterns("de_DE"))  # → "name_first"
```

When the DDL parser extracts a `COMMENT 'text'` clause, the text is stored in `col.comment`.  If the column name gives no match, the comment text is tried automatically:

```python
# col.comment = "Customer social security number" (from DDL COMMENT clause)
infer_format_pattern("col_x", col_comment="Customer social security number")  # → "ssn"

# Column name still wins over comment
infer_format_pattern("email", col_comment="social security number")  # → "email"

# Human description works as a fallback when col.comment is absent
infer_format_pattern("col_x", col_description="Customer email address")  # → "email"

# Disable comment / description matching
infer_format_pattern("col_x", col_comment="...", comment_hints=False)  # → None
```

Shipped locales: `en_US`, `de_DE`.  Each locale has two pattern files:

| File | Matched against |
|------|----------------|
| `patterns/<locale>.yaml` | `col.name` (SQL identifier) |
| `patterns/<locale>.comments.yaml` | `col.comment` / `col.description` (free-form prose) |

Both use the same YAML format and can be edited without touching Python code.  Comment patterns use word-boundary anchors (`\b`) and natural-language phrasing to work accurately against running text.

**Option B — external `hints.yaml` (user-configurable overrides)**

`load_hints()` + `apply_hints()` annotates tables before generation.  Use this to map project-specific column names, add custom `min`/`max` ranges, or override the built-in patterns.  Pass `db_stats` to expose column statistics for future stats-based inference.

```python
from src.statschema.semantic_hints import load_hints, apply_hints, load_builtin_comment_patterns

hints  = load_hints("hints.yaml")
tables = apply_hints(tables, hints)                        # comment matching on by default
tables = apply_hints(tables, hints, comment_hints=False)  # disable comment matching
tables = apply_hints(tables, hints,
    comment_hints=load_builtin_comment_patterns("de_DE"),  # German comment patterns
    db_stats=db_stats)                                     # stats plumbed for future use
```

`hints.yaml` format (same format works for comment pattern files):

```yaml
version: "1.0"
hints:
  - pattern: "tax_id|tin"
    generation:
      format_pattern: ssn

  - pattern: "salary|compensation"
    generation:
      min_value: 30000
      max_value: 500000
      distribution: normal
      distribution_params: {mean: 80000, std: 30000}
```

Each `pattern` is a case-insensitive Python regex.  For comment files, use word-boundary anchors and natural-language phrasing (`"social.?security"` rather than `"social_security"`).  First match wins.  Any `GenerationRule` fields are valid in the `generation` block.

To use a non-default locale:

```python
from src.statschema.semantic_hints import load_builtin_patterns, load_builtin_comment_patterns
de_names    = load_builtin_patterns("de_DE")
de_comments = load_builtin_comment_patterns("de_DE")
tables = apply_hints(tables, de_names, comment_hints=de_comments)
```

**Option C — LLM inference (planned)**

A future fallback using a Databricks Foundation Model endpoint to classify column semantics from column name and sample values, modelled on [Databricks LogSentinel](https://www.databricks.com/blog/logsentinel-how-databricks-uses-databricks-for-llm-powered-pii-detection-and-governance).  Activated via `llm_inference: true` in `hints.yaml`.  Not yet implemented — `_infer_format_pattern_llm()` currently raises `NotImplementedError`.

---

### 4 — Stats feedback loop: transpile → load → measure → improve

This is the full production workflow.  Generate an initial dataset using heuristic
defaults, load it into a real database, measure what the database actually contains,
then feed those real measurements back as generation parameters.  Each iteration
narrows the gap between the collected statistics and the target statistics.

```python
import pymysql
from sqlalchemy import create_engine
from src.statschema import (
    parse_ddl, emit_ddl,
    make_default_stats, collect_table_stats, dump_stats,
    build_dataframe_from_canonical,
)

# ── 1. Parse schema ────────────────────────────────────────────────────────────
schema = parse_ddl(open("orders.sql").read(), dialect="mysql")[0]
conn   = pymysql.connect(host="127.0.0.1", port=3306, user="root", password="testpass",
                         db="mydb", autocommit=True)
engine = create_engine("mysql+pymysql://root:testpass@127.0.0.1:3306/mydb")

# ── 2. Create the table on the live database ───────────────────────────────────
conn.cursor().execute(emit_ddl(schema, "mysql", if_not_exists=False))

# ── 3. First generation — heuristic defaults (no prior data needed) ────────────
default_stats = make_default_stats([schema], row_count=50_000).tables[0]
df_v1 = build_dataframe_from_canonical(spark, schema, rows=50_000, stats=default_stats, seed=42)

load_cols = [c.name for c in schema.columns if not c.auto_increment]
df_v1.select(load_cols).toPandas().to_sql("orders", engine, if_exists="append", index=False)

# ── 4. Collect REAL statistics from what was actually loaded ───────────────────
real_stats = collect_table_stats(conn, "orders", dialect="mysql")
# real_stats now contains per-column:
#   .null_fraction     e.g. 0.082  (total is NULL 8.2% of the time)
#   .n_distinct        e.g. 4.0    (only 4 distinct status values)
#   .min_value / .max_value        (actual numeric / date ranges)
#   .most_common_values            (top-10 values with real frequencies)
#   .histogram_bounds              (P10 / P25 / P50 / P75 / P90)

dump_stats_yaml = dump_stats  # optional: persist for reproducibility
# dump_stats(DatabaseStats(tables=[real_stats]), "orders_stats.yaml")

# ── 5. Second generation — driven by real measurements ─────────────────────────
df_v2 = build_dataframe_from_canonical(spark, schema, rows=50_000, stats=real_stats, seed=99)

conn.cursor().execute(
    "RENAME TABLE orders TO orders_v1; "
    + emit_ddl(schema, "mysql", if_not_exists=False).replace("`orders`", "`orders`", 1)
)
df_v2.select(load_cols).toPandas().to_sql("orders", engine, if_exists="append", index=False)

# ── 6. Verify accuracy: compare stats from both tables ─────────────────────────
stats_v1 = collect_table_stats(conn, "orders_v1", dialect="mysql")
stats_v2 = collect_table_stats(conn, "orders",    dialect="mysql")

for col in load_cols:
    cs1 = stats_v1.column_stats(col)
    cs2 = stats_v2.column_stats(col)
    if cs1 and cs2:
        nf_err   = abs(cs1.null_fraction - cs2.null_fraction)
        dist_rat = cs2.n_distinct / cs1.n_distinct if cs1.n_distinct else 1.0
        print(f"{col:20s}  null_frac_Δ={nf_err:.3f}  distinct_ratio={dist_rat:.2f}")
```

Example output after one iteration:
```
customer_id          null_frac_Δ=0.000  distinct_ratio=0.98
status               null_frac_Δ=0.003  distinct_ratio=1.00   ← only 4 distinct values, exact match
total                null_frac_Δ=0.007  distinct_ratio=1.12
discount_pct         null_frac_Δ=0.005  distinct_ratio=0.94
is_paid              null_frac_Δ=0.002  distinct_ratio=1.00
created_at           null_frac_Δ=0.008  distinct_ratio=1.03
```

Each iteration narrows the statistical gap.  After one or two rounds, null
fractions are typically within ±15% and cardinality within ±5× of the target
(verified by `make test-live-synth`).

**What the stats capture** (all collected by `collect_table_stats` using standard SQL):

| Statistic | Collected via | Used by dbldatagen |
|-----------|--------------|-------------------|
| Row count | `COUNT(*)` | `rows=` parameter |
| Null fraction | `(COUNT(*) - COUNT(col)) / COUNT(*)` | `percentNulls=` |
| Distinct count | `COUNT(DISTINCT col)` | `uniqueValues=` (high-card) |
| Min / Max | `MIN(col)`, `MAX(col)` | `minValue=`, `maxValue=` |
| Most-common values | `GROUP BY … ORDER BY cnt DESC LIMIT 10` | `values=`, `weights=` |
| Histogram bounds | P10/P25/P50/P75/P90 percentiles | range shaping |
| pg_stats (PG only) | `null_frac`, `n_distinct`, `most_common_vals` | richer MCVs |

**When MCVs are applied**: only when the top-10 values cover >50% of the column
(truly low-cardinality, e.g. `status`) or `n_distinct ≤ 20`.  High-cardinality
columns (e.g. random integers) use min/max ranges instead, preserving full spread.

---

## Supported dialects for transpilation

### Inputs — parse DDL from

| Format | Function | Notes |
|--------|----------|-------|
| MySQL DDL (`mysqldump --no-data`) | `parse_ddl(sql, "mysql")` | |
| MariaDB DDL | `parse_ddl(sql, "mariadb")` | alias → mysql path |
| PostgreSQL DDL (`pg_dump -s`) | `parse_ddl(sql, "postgres")` | |
| CockroachDB DDL | `parse_ddl(sql, "cockroachdb")` | alias → postgres path |
| Lakebase DDL | `parse_ddl(sql, "lakebase")` | alias → postgres path |
| SQL Server DDL (SSMS Scripts) | `parse_ddl(sql, "sqlserver")` | |
| Oracle DDL | `parse_ddl(sql, "oracle")` | |
| IBM Db2 DDL | `parse_ddl(sql, "db2")` | ANSI SQL parse (no sqlglot Db2 dialect) |
| Databricks DDL | `parse_ddl(sql, "databricks")` | |
| Portable schema YAML | `load_canonical("schema.yaml")` | dialect-free round-trip format |
| YData / Syda YAML | `parse_ydata_yaml(data)` | |
| SDV metadata JSON | `parse_sdv_metadata(data)` | |
| Pipeline YAML | `parse_pipeline_tables(data)` | |
| **Auto-detect** | `load_schema("file")` | infers format from content |

### Outputs — transpile DDL to

| Target dialect | `emit_ddl` argument |
|----------------|---------------------|
| Databricks / Delta Lake | `"databricks"` |
| PostgreSQL / Neon / CockroachDB | `"postgres"` |
| MySQL / MariaDB | `"mysql"` |
| SQL Server | `"sqlserver"` |
| Oracle | `"oracle"` |
| IBM Db2 LUW | `"db2"` |

Every semantic correction is applied automatically during transpilation —
e.g. `TINYINT(1)` → `BOOLEAN` (PostgreSQL), `DATETIME` → `DATETIME2` (SQL Server),
`AUTO_INCREMENT` → `SERIAL` (PostgreSQL) / `GENERATED ALWAYS AS IDENTITY` (Oracle / Db2).

---

## Portable schema — canonical YAML formats

The portable schema YAML is the central intermediate representation.  Both the
DDL schema and the column statistics are stored as dialect-agnostic YAML files
that can be committed to version control, shared across teams, and retargeted at
any database without modification.  A schema collected from MySQL today can drive
a Databricks Delta table or an Oracle schema tomorrow — no manual conversion needed.

### Schema YAML (`schema.yaml`)

Produced by `dump_schema(tables, "schema.yaml")`, consumed by `load_canonical("schema.yaml")`:

```yaml
tables:
  - name: orders
    columns:
      - name: order_id
        type: integer
        not_null: true
        primary_key: true
        auto_increment: true
      - name: status
        type: string
        length: 20
        not_null: true
        default: "'pending'"
      - name: total
        type: decimal
        precision: 10
        scale: 2
      - name: is_paid
        type: boolean
        not_null: true
        default: "false"
      - name: created_at
        type: timestamp
```

### Statistics YAML (`orders_stats.yaml`)

Produced by `dump_stats(db_stats, "orders_stats.yaml")`, consumed by `load_stats("orders_stats.yaml")`:

```yaml
version: "1.0"
source_dialect: mysql
tables:
  - name: orders
    row_count: 50000
    avg_row_bytes: 44
    columns:
      - name: status
        null_fraction: 0.0
        n_distinct: 4.0
        most_common_values:
          - {value: pending,    frequency: 0.42}
          - {value: shipped,    frequency: 0.31}
          - {value: delivered,  frequency: 0.18}
          - {value: cancelled,  frequency: 0.09}
      - name: total
        null_fraction: 0.082
        n_distinct: 12500.0
        min_value: "4.99"
        max_value: "9987.50"
        histogram_bounds: ["4.99", "120.00", "450.00", "1200.00", "9987.50"]
      - name: is_paid
        null_fraction: 0.0
        n_distinct: 2.0
    indexes:
      - name: pk_orders
        columns: [order_id]
        unique: true
        index_type: BTREE
```

Both formats round-trip exactly: `load_canonical(dump_schema(...))` and
`load_stats(dump_stats(...))` reproduce the original objects without loss.
The stats YAML is database-agnostic — stats collected from MySQL can drive
generation for PostgreSQL or Databricks without conversion.

---

## Exporting DDL and statistics from each database

For **MySQL**, **PostgreSQL**, **MariaDB**, **CockroachDB**, **Neon**, **Databricks**, and **Lakebase**, full DDL export and stats collection instructions live in the per-database setup pages:

| Database | Setup + export guide |
|----------|----------------------|
| MySQL | [`docs/databases/mysql.md`](docs/databases/mysql.md) |
| PostgreSQL | [`docs/databases/postgres.md`](docs/databases/postgres.md) |
| MariaDB | [`docs/databases/mariadb.md`](docs/databases/mariadb.md) |
| CockroachDB | [`docs/databases/cockroachdb.md`](docs/databases/cockroachdb.md) |
| Neon | [`docs/databases/neon.md`](docs/databases/neon.md) |
| Databricks / Lakebase | [`docs/databases/lakebase.md`](docs/databases/lakebase.md) |

The three sections below cover **SQL Server**, **Oracle**, and **Db2** — the databases where the DDL export tooling is least obvious.

---

### SQL Server

```bash
# DDL export via mssql-scripter (pip install mssql-scripter)
# Credentials from .env: SQLSERVER_PASS, SQLSERVER_PORT
source .env
mssql-scripter -S "127.0.0.1,$SQLSERVER_PORT" -d mydb -U sa -P "$SQLSERVER_PASS" \
  --schema-and-data=False --display-progress > schema.sql
```

```python
import os, pymssql
from dotenv import load_dotenv
from src.statschema import parse_ddl, dump_schema, collect_table_stats, dump_stats, DatabaseStats

load_dotenv()

conn = pymssql.connect(
    server="127.0.0.1",
    port=int(os.environ["SQLSERVER_PORT"]),
    user="sa",
    password=os.environ["SQLSERVER_PASS"],
    database="mydb",
    autocommit=True,
)

# Update statistics so measurements are current
conn.cursor().execute("EXEC sp_updatestats;")

tables = parse_ddl(open("schema.sql").read(), dialect="sqlserver")
dump_schema(tables, "schema.yaml")

stats = [collect_table_stats(conn, t.name, dialect="sqlserver") for t in tables]
dump_stats(DatabaseStats(tables=stats), "stats.yaml")
```

---

### Oracle

```bash
# DDL export via SQL*Plus (dbms_metadata) — no data, schema only
# Credentials from .env: ORACLE_USER, ORACLE_PASS, ORACLE_DSN, ORACLE_SCHEMA
source .env
sqlplus "$ORACLE_USER/$ORACLE_PASS@//$ORACLE_DSN" <<'EOF'
SET PAGESIZE 0 LONG 99999 FEEDBACK OFF
SELECT dbms_metadata.get_ddl('TABLE', table_name, '$ORACLE_SCHEMA')
FROM all_tables WHERE owner = '$ORACLE_SCHEMA';
EXIT;
EOF > schema.sql
```

```python
import os, oracledb
from dotenv import load_dotenv
from src.statschema import parse_ddl, dump_schema, collect_table_stats, dump_stats, DatabaseStats

load_dotenv()

conn = oracledb.connect(
    user=os.environ["ORACLE_USER"],
    password=os.environ["ORACLE_PASS"],
    dsn=os.environ["ORACLE_DSN"],
)

# Gather fresh statistics before collecting
conn.cursor().execute(
    f"BEGIN dbms_stats.gather_schema_stats('{os.environ['ORACLE_SCHEMA']}'); END;"
)

tables = parse_ddl(open("schema.sql").read(), dialect="oracle")
dump_schema(tables, "schema.yaml")

schema = os.environ["ORACLE_SCHEMA"]
stats = [collect_table_stats(conn, t.name, dialect="oracle", schema=schema)
         for t in tables]
dump_stats(DatabaseStats(tables=stats), "stats.yaml")
```

---

### Db2

Db2 has no native `arm64` image; it runs inside a Lima VM on Apple Silicon.
See [`docs/databases/db2.md`](docs/databases/db2.md) for VM setup (`limactl start --name=db2 config/lima/db2.yaml`).

```bash
# DDL export via db2look (runs inside the Lima VM)
# Credentials from .env: DB2_PASS, DB2_PORT (default 50000)
limactl shell db2 -- su - db2inst1 -c \
  "db2look -d testdb -e -a -o /tmp/schema.sql && cat /tmp/schema.sql" > schema.sql
```

```python
import os, ibm_db_dbi
from dotenv import load_dotenv
from src.statschema import parse_ddl, dump_schema, collect_table_stats, dump_stats, DatabaseStats

load_dotenv()

dsn = (
    f"DATABASE=testdb;HOSTNAME=127.0.0.1;"
    f"PORT={os.environ.get('DB2_PORT', '50000')};"
    f"PROTOCOL=TCPIP;UID=db2inst1;PWD={os.environ['DB2_PASS']};"
)
conn = ibm_db_dbi.connect(dsn, "", "")

# Gather fresh column statistics before collecting
cur = conn.cursor()
cur.execute("CALL SYSPROC.ADMIN_CMD('RUNSTATS ON TABLE db2inst1.ORDERS WITH DISTRIBUTION')")
conn.commit()

tables = parse_ddl(open("schema.sql").read(), dialect="db2")
dump_schema(tables, "schema.yaml")

stats = [collect_table_stats(conn, t.name, dialect="db2", schema="DB2INST1")
         for t in tables]
dump_stats(DatabaseStats(tables=stats), "stats.yaml")
```

---

### Using saved YAML files

Once `schema.yaml` and `stats.yaml` are on disk they drive all downstream
steps without any database connection:

```python
from src.statschema import load_canonical, load_stats, emit_ddl, build_rows_from_canonical

tables   = load_canonical("schema.yaml")
db_stats = load_stats("stats.yaml")

# Emit DDL for any target dialect
print(emit_ddl(tables[0], "postgres"))
print(emit_ddl(tables[0], "lakebase"))
print(emit_ddl(tables[0], "databricks"))

# Generate synthetic data — no Spark required
df = build_rows_from_canonical(tables[0], rows=100_000,
                               stats=db_stats.tables[0])
df.to_sql(tables[0].name, engine, if_exists="replace", index=False)
```


## Stats transpiler — the database migration use case

Collect statistics from the source before migration, store as portable YAML, inject
into the target's statistics catalog — optimizer sees production-scale distributions
immediately, before a single row is loaded.  See [Optimizer bootstrap](#optimizer-bootstrap--inject-statistics-before-migrating-data) for context.

```python
import pymysql
from src.statschema import collect_table_stats, dump_stats, load_stats, DatabaseStats

# 1. Collect statistics from the SOURCE database (MySQL)
src_conn = pymysql.connect(host="prod-mysql", user="reader", password="…", db="orders_db")
stats = [collect_table_stats(src_conn, t, dialect="mysql") for t in ["orders", "customers"]]
dump_stats(DatabaseStats(tables=stats), "production_stats.yaml")

# 2. Migrate DDL (transpile schema)
#    parse_ddl(…, dialect="mysql") → emit_ddl(…, dialect="postgres")  [see Example 1]

# 3. Inject statistics into TARGET database (PostgreSQL)
#    [TODO: pg_restore_attribute_stats() bridge — see roadmap]
pg_stats = load_stats("production_stats.yaml")
# → optimizer knows: orders has 50M rows, status has 4 distinct values (42% 'pending'),
#   total ranges from $4.99–$9987, created_at histogram spans 5 years
#   → index vs sequential scan decisions match production from minute one
```

> **Status**: statistics collection and portable YAML are production-ready.
> Injecting into target optimizer statistics catalogs (`pg_restore_attribute_stats`,
> `DBMS_STATS.SET_COLUMN_STATS`, `UPDATE STATISTICS WITH ROWCOUNT`) is on the roadmap —
> see the TODO item.

---

## Stats-driven tabular data — three levels of fidelity

Column statistics control how closely the generated tabular data matches the
source database's distributions.  Three levels are available, from zero-dependency
heuristics to real measurements collected from a live system:

```python
from src.statschema import make_default_stats, collect_table_stats, load_stats, dump_stats, DatabaseStats

# Level 1 – heuristic defaults (no DB connection required)
#   Conservative estimates: null_fraction=0.05 for nullable cols, n_distinct from type.
db_stats   = make_default_stats(tables, row_count=50_000)
table_stats = db_stats.tables[0]

# Level 2 – measure a live database (standard SQL, works on MySQL / PG / SQL Server)
import psycopg2
conn        = psycopg2.connect("host=localhost dbname=prod user=reader password=…")
table_stats = collect_table_stats(conn, "orders", dialect="postgres", schema="public")
#   → per-column: null_fraction, n_distinct, min/max, top-10 MCVs, P10–P90 bounds
#   → PostgreSQL: also reads pg_stats for richer MCV data after ANALYZE

# Level 3 – save stats YAML for reproducibility / CI
dump_stats(DatabaseStats(tables=[table_stats]), "orders_stats.yaml")
db_stats    = load_stats("orders_stats.yaml")   # exact round-trip
```

See **Example 4** above for the full generate → load → collect → regenerate → compare loop.

---

## Schema overrides — rename, retype, rescale

```python
from src.statschema import load_canonical, load_stats, apply_overrides, OverrideSpec, TableOverride, ColumnOverride

spec = OverrideSpec(tables=[
    TableOverride(
        name="orders",
        rename="purchase_orders",          # rename table for target system
        row_count=5_000,                   # generate a 5k-row subset
        columns=[
            ColumnOverride(name="status",  type="varchar", length=30),   # widen type
            ColumnOverride(name="total",   rename="amount"),              # rename column
        ],
    )
])

tables = load_canonical("schema.yaml")
stats  = load_stats("stats.yaml")
tables2, stats2 = apply_overrides(tables[0], stats.tables[0], spec)
```

Common migration patterns (MySQL → Databricks, Oracle → PostgreSQL, etc.) are all
handled through the same override mechanism.

---

## Full pipeline — transpile · portable schema · tabular data

```
Source schema (any format)
        │
        ▼  load_schema() / parse_ddl()
CanonicalTableSchema + CanonicalForeignKey
        │
        ├──  dump_schema("schema.yaml")        ◄── version-controllable, dialect-free
        │
        ├──  apply_overrides(spec)             ◄── rename / retype / rescale
        │
        ├──  emit_ddl("databricks")   ──►  CREATE TABLE  (Databricks / Unity Catalog)
        ├──  emit_ddl("postgres")     ──►  CREATE TABLE  (PostgreSQL)
        ├──  emit_ddl("mysql")        ──►  CREATE TABLE  (MySQL)
        ├──  emit_ddl("sqlserver")    ──►  CREATE TABLE  (SQL Server)
        ├──  emit_ddl("oracle")       ──►  CREATE TABLE  (Oracle)
        │
        ├──  make_default_stats()             ◄── heuristic stats when none available
        │    OR collect_table_stats(conn)     ◄── real stats from live database
        │    OR load_stats("stats.yaml")      ◄── pre-saved stats (reproducible)
        │
        └──  build_dataframe_from_canonical(spark, rows=N, stats=…)
                    │
                    ▼  dbldatagen (Databricks Labs Data Generator)
             Spark DataFrame — synthetic rows parameterized by schema + collected statistics
                    │
                    ├──  df.write.saveAsTable("catalog.schema.table")  (Databricks)
                    ├──  pandas .to_sql(engine)                        (any DB via SQLAlchemy)
                    └──  write to DB / Delta / warehouse                (your choice)
```

---

## Quick start

### Install

```bash
# Core — CLI, DDL transpilation, stats collect/inject, and pandas-based synthetic data.
# No Spark, no Java, no JVM required.
pip install statschema

# Large-scale Spark-based synthetic data (adds PySpark + dbldatagen; requires Java/JVM):
pip install 'statschema[spark]'

# Live database drivers — install only the ones you need:
pip install 'statschema[postgres]'   # psycopg2-binary
pip install 'statschema[mysql]'      # pymysql
pip install 'statschema[mssql]'      # pymssql
pip install 'statschema[oracle]'     # oracledb

# Databricks Lakebase (OAuth token generation + Postgres driver):
pip install 'statschema[lakebase]'   # databricks-sdk>=0.89.0 + psycopg2-binary
```

### Credentials

```bash
cp .env.example .env
# edit .env — set CLIENT_SECRET, SQLSERVER_PASS, etc.
```

`conftest.py` loads `.env` automatically before tests.

### Run tests

```bash
make venv-test            # create .venv_test (Python 3.11; adds Spark for data-generation tests)
make test-fast            # core tests only — no Spark, no live DB required
make test                 # full suite including Spark-based data-generation tests
make test-live-all        # + live MySQL, PostgreSQL, SQL Server round-trips
make test-live-synth      # full generate → load → stats → regenerate → compare pipeline
make test-live-lakebase   # Lakebase: spin up → run tests → disable endpoint
```

---

## Contributing: adding a new database

**[`docs/adding-a-database.md`](docs/adding-a-database.md)** is the single contributor guide for integrating a new engine as a source or target for DDL transpilation, stats collection, and live testing.  It covers everything from pre-checks (sqlglot support, ARM64 availability, wire protocol) through dialect registration, optional custom DDL emitter, local container/VM setup, live test module, and Makefile wiring.

Local database setup recipes live in **[`docs/local-databases.md`](docs/local-databases.md)** (index) and individual per-database pages under **[`docs/databases/`](docs/databases/)** — see the [Database setup](#database-setup) table in the Documentation section below for the full list.

---

## Verified data generation

All five TPC benchmark schemas ship as canonical YAML under `benchmarks/schemas/`. All five have been verified end-to-end: TPC-B/C/H against live client workloads (pgbench, CockroachDB), and TPC-DS/E against DuckDB using the official 99-query TPC-DS suite and representative queries for all 10 TPC-E transaction types.

| Schema | Tables | SF=1 rows | Status |
|--------|--------|-----------|--------|
| `tpcb_schema.yaml` | 4 | 100,011 | Verified — `pgbench` on PostgreSQL; `cockroach workload bank` on CockroachDB |
| `tpcc_schema.yaml` | 9 | 599,011 | Verified — `cockroach workload tpcc`, all 5 transaction types, 0 errors |
| `tpch_schema.yaml` | 8 | ~8,600,000 | Verified — `cockroach workload tpch` analytical queries |
| `tpcds_schema.yaml` | 24 | ~19,500,000 | Verified — all 99 official TPC-DS queries execute against DuckDB with 0 SQL errors at SF=0.01; fixed dimension tables (date_dim, time_dim, customer_demographics) use deterministic built-in generators |
| `tpce_schema.yaml` | 32 | ~82,000,000 | Verified — representative SELECT queries for all 10 TPC-E transaction types (Broker-Volume, Customer-Position, Market-Watch, Security-Detail, Trade-Lookup ×3, Trade-Order, Trade-Result, Trade-Status, Trade-Update) return rows against DuckDB at SF=0.01 |

Generation, DDL emission, and loading are all driven from the same YAML file — no custom Python per database engine. The YAML schema format is designed to be the declarative standard for synthetic data generation: the same role SQL plays for queries. Write the schema once; `statschema` generates correct, referentially-consistent data for any supported engine.

---

## Verified transpiler coverage

`make test-live-all` executes **~2600 parametrized tests** against real databases,
verifying the full transpile → YAML → retranspile → execute → introspect → compare cycle
for every canonical type, constraint, and default.

| Database | Versions tested |
|----------|----------------|
| MySQL | 5.7, 8.4 |
| MariaDB | 10.11 LTS, 11.4 |
| PostgreSQL | 14, 16 |
| CockroachDB | latest (single-node + 3-node multi-region) |
| Neon | Neon Local proxy |
| SQL Server | 2022 |
| Oracle | XE 21c |
| IBM Db2 | CE 11.5 |
| Databricks | Unity Catalog (via `databricks-connect`) |
| **Lakebase** | Managed Postgres on Databricks (OAuth via `databricks-sdk`) |

Live synthetic data tests (`make test-live-synth`) additionally verify that
stats collected from a first-generation load drive a second generation whose
distributions match within ±15% null fraction and ±5× cardinality.

---

## API for AI agents (Cursor, Claude, etc.)

The public API is intentionally narrow and composable:

```python
# Parse schema
tables = parse_ddl(sql_string, dialect)              # → list[CanonicalTableSchema]
tables = load_schema("file.sql")                     # auto-detect format

# Inspect
schema = tables[0]
[(c.name, c.type, c.not_null) for c in schema.columns]

# Emit DDL for any target
ddl = emit_ddl(schema, "postgres")                   # → str  (CREATE TABLE …)
all_ddl = emit_ddl_all(schema)                       # → dict[dialect, str]

# Statistics
stats = collect_table_stats(conn, table, dialect)    # → TableStats
stats = make_default_stats([schema], row_count=N)    # → DatabaseStats (no DB needed)

# Generate synthetic data
df = build_dataframe_from_canonical(spark, schema, rows=N, stats=table_stats)

# Save / load canonical representations
dump_schema(tables, "schema.yaml")
tables = load_canonical("schema.yaml")
dump_stats(db_stats, "stats.yaml")
db_stats = load_stats("stats.yaml")
```

**All functions are pure-Python.** `parse_ddl`, `emit_ddl`, `collect`, `inject`, and
pandas-based synthetic data generation all work with `pip install statschema` — no
database connection, no Spark session, no Java installation required.
`build_dataframe_from_canonical` additionally requires `statschema[spark]` (PySpark +
dbldatagen) and is only needed when generating large datasets in a Databricks environment.

---

## Real-world migration evidence

[`docs/learnings/oltp-migration-analysis.md`](docs/learnings/oltp-migration-analysis.md) analyses five documented migrations drawn from Hacker News, Reddit, AWS blogs, and migration consultants (2023–2026): SQL Server → PostgreSQL, MySQL → PostgreSQL, Oracle → PostgreSQL, MySQL → Aurora, and SQLite → Neon.

Every migration hit the same four phases where time was lost. statschema's impact on each:

| Evaluation cycle phase | Typical delay | statschema impact | Command |
|------------------------|--------------|-------------------|---------|
| Waiting for sanitized production data copy | **4–12 weeks** — GDPR/HIPAA legal review | **Eliminates** — only DDL + statistics (no row values) cross boundaries | `statschema collect` + `statschema generate` |
| Schema type-conversion errors found late | Days–weeks of debugging failed loads | **Eliminates** — `MONEY`→`NUMERIC`, `DATETIME`→`TIMESTAMP`, `NVARCHAR`→`VARCHAR` visible at parse time | `statschema ddl --dialect postgres` |
| Optimizer-blind period post-migration | 1–4 weeks diagnosing "slow target" | **Eliminates** — production-representative statistics injected before first evaluation query | `collect_table_stats` + `inject_stats_postgres` |
| Performance tests at wrong scale | Misses crossovers only visible at production volume | **Shortens** — any scale factor from one YAML | `statschema load --sf 10` |

Where statschema does **not** help: stored procedure rewriting (T-SQL→PL/pgSQL, PL/SQL→PL/pgSQL), network/infrastructure latency, zero-downtime cutover mechanics (CDC, dual-write), ETL pipeline correctness (AWS DMS defects), and ORM query generation differences. Full analysis with commands and worked examples in the document.

---

## Documentation

> Full documentation map with hierarchy: [`docs/toc.md`](docs/toc.md)

### Data generation

| Document | Contents |
|----------|----------|
| [`docs/dba-yaml-guide.md`](docs/dba-yaml-guide.md) | DBA quick-start: write one YAML file, run three commands, no Python required |
| [`docs/canonical_yaml.md`](docs/canonical_yaml.md) | Canonical schema YAML — feature reference (overview page) |
| [`docs/canonical_yaml/01-core-structure.md`](docs/canonical_yaml/01-core-structure.md) | Core structure: `name`, `columns`, `primary_key` |
| [`docs/canonical_yaml/02-column-types.md`](docs/canonical_yaml/02-column-types.md) | Column types and type mapping |
| [`docs/canonical_yaml/03-column-constraints.md`](docs/canonical_yaml/03-column-constraints.md) | Column constraints: `nullable`, `unique`, `default` |
| [`docs/canonical_yaml/04-column-precision-length.md`](docs/canonical_yaml/04-column-precision-length.md) | Precision and length for numeric/string columns |
| [`docs/canonical_yaml/05-foreign-keys.md`](docs/canonical_yaml/05-foreign-keys.md) | Foreign key declarations and referential integrity |
| [`docs/canonical_yaml/06-generation-rules.md`](docs/canonical_yaml/06-generation-rules.md) | `generation` block: distributions, patterns, value lists |
| [`docs/canonical_yaml/07-temporal-ordering.md`](docs/canonical_yaml/07-temporal-ordering.md) | Temporal ordering and `depends_on` for time-series data |
| [`docs/canonical_yaml/08-multi-instance-expansion.md`](docs/canonical_yaml/08-multi-instance-expansion.md) | Multi-instance expansion for replicated table patterns |
| [`docs/canonical_yaml/09-round-trip-guarantee.md`](docs/canonical_yaml/09-round-trip-guarantee.md) | Round-trip guarantee: parse → emit → parse stability |
| [`docs/implementation.md`](docs/implementation.md) | Project structure, schema sources, and data-generation wiring |
| [`docs/synthetic_data_shortcomings.md`](docs/synthetic_data_shortcomings.md) | Known limitations of synthetic data generation and mitigations |
| [`docs/dbldatagen.md`](docs/dbldatagen.md) | dbldatagen v0/v1 integration notes |

### Benchmarks

| Document | Contents |
|----------|----------|
| [`benchmarks/README.md`](benchmarks/README.md) | TPC load benchmark guide: how to run, scale factors, output |
| [`benchmarks/results/benchmark_table.md`](benchmarks/results/benchmark_table.md) | Latest benchmark results (rows/sec by engine and SF) |

### Stats transpiler

| Document | Contents |
|----------|----------|
| [`docs/stats_transpiler.md`](docs/stats_transpiler.md) | Full details, per-engine workarounds, and function reference |

### Testing

| Document | Contents |
|----------|----------|
| [`docs/testing.md`](docs/testing.md) | Local test setup, `.env` credentials, Spark/Java config, live-DB setup |
| [`docs/test_plan_ddl_roundtrip.md`](docs/test_plan_ddl_roundtrip.md) | Complete DDL round-trip test plan (all types, boundaries, constraints) |
| [`docs/learnings/README.md`](docs/learnings/README.md) | Learnings index: gotchas and decisions captured while building statschema |
| [`docs/learnings/oltp-migration-analysis.md`](docs/learnings/oltp-migration-analysis.md) | **Real-world OLTP migration analysis** — five migrations, per-phase evidence. Summary in [Real-world migration evidence](#real-world-migration-evidence) above. |

### Database setup

| Document | Contents |
|----------|----------|
| [`docs/local-databases.md`](docs/local-databases.md) | Index of all local database setup guides |
| [`docs/databases/postgres.md`](docs/databases/postgres.md) | PostgreSQL — Podman, native ARM64 |
| [`docs/databases/neon.md`](docs/databases/neon.md) | Neon — Podman + Neon Local cloud proxy |
| [`docs/databases/cockroachdb.md`](docs/databases/cockroachdb.md) | CockroachDB — Podman, native ARM64 (single-node + multi-region) |
| [`docs/databases/mysql.md`](docs/databases/mysql.md) | MySQL — Podman, native ARM64 |
| [`docs/databases/mariadb.md`](docs/databases/mariadb.md) | MariaDB — Podman, native ARM64 |
| [`docs/databases/sqlserver.md`](docs/databases/sqlserver.md) | SQL Server — Lima VM + QEMU (x86_64) |
| [`docs/databases/oracle.md`](docs/databases/oracle.md) | Oracle — Lima VM + Podman + QEMU (x86_64) |
| [`docs/databases/oracle-hr.md`](docs/databases/oracle-hr.md) | Oracle HR/CO sample schemas — application-level testing |
| [`docs/databases/db2.md`](docs/databases/db2.md) | DB2 — Lima VM + Podman + QEMU (x86_64) |
| [`docs/databases/adventureworks.md`](docs/databases/adventureworks.md) | AdventureWorks — application-level testing (SQL Server) |
| [`docs/databases/chinook.md`](docs/databases/chinook.md) | Chinook — application-level testing (SQL Server) |
| [`docs/databases/mautic.md`](docs/databases/mautic.md) | Mautic — application-level testing (MySQL) |
| [`docs/databases/gitea.md`](docs/databases/gitea.md) | Gitea — application-level testing (PostgreSQL) |
| [`docs/databases/lakebase.md`](docs/databases/lakebase.md) | Lakebase — Databricks managed Postgres with OAuth (`scripts/lakebase-up.sh`) |
| [`docs/faq/README.md`](docs/faq/README.md) | FAQ index — local database tooling |
| [`docs/faq/17-what-is-gvenzl-oracle-xe.md`](docs/faq/17-what-is-gvenzl-oracle-xe.md) | FAQ: what is the gvenzl/oracle-xe image? |
| [`docs/faq/18-podman-machine-on-macos.md`](docs/faq/18-podman-machine-on-macos.md) | FAQ: Podman machine setup on macOS |

### Contributing & project

| Document | Contents |
|----------|----------|
| [`docs/adding-a-database.md`](docs/adding-a-database.md) | **Contributor guide**: add a new engine end-to-end (pre-checks, dialect registry, emitter, tests, docs) |
| [`docs/PLAN.md`](docs/PLAN.md) | Architecture and implementation notes |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | Roadmap and planned work |
| [`docs/git-submodules.md`](docs/git-submodules.md) | Git submodule workflow (commit and push `.cursor` + parent repo) |
| [`.env.example`](.env.example) | Credential template — copy to `.env` and fill in values |

---

## License

MIT — see [LICENSE](LICENSE).

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup instructions, the contributor guide for adding a new database dialect, and the test patterns all contributors follow.
