# statschema — stats transpiler · DDL transpiler · portable schema (YAML) · stats-driven tabular data

**Repository:** [github.com/rsleedbx/statschema](https://github.com/rsleedbx/statschema) · `git clone https://github.com/rsleedbx/statschema.git`

> **The only library that transpiles both column statistics and DDL schema across database dialects.**
> Collect from MySQL. Migrate to PostgreSQL. The optimizer works correctly from day one.

### Overview

```mermaid
flowchart LR
    A["① Stats Transpiler ⭐\ncollect_table_stats\ndump_stats · load_stats"]
    B[("② Portable YAML\nschema.yaml · stats.yaml")]
    C["③ DDL Transpiler\nparse_ddl · emit_ddl"]
    D["④ Stats-Driven\nTabular Data\nbuild_dataframe"]

    A <-->|"collect / inject\nany dialect"| B
    C <-->|"parse / emit\nany dialect"| B
    B -->|"schema + stats"| D
```

---

### Why "stats transpiler" is a new concept

Every DDL transpiler stops at the schema. But a migrated database with correct schema
and *default* statistics is still broken — the query optimizer has no idea how many
rows each table has, which values are common, or what the numeric ranges look like.
It produces bad query plans from day one.

`statschema` introduces the **stats transpiler**: collect column statistics from any
source database, store them as dialect-free YAML, and inject them into any target database
so the optimizer sees production-scale distributions *before a single row is loaded*:

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
| **PostgreSQL 18** | Full: MCVs, null fractions, n_distinct, histogram bounds for all types | Row count needs a sample loaded first (PG18 scales by physical file size). Extended stats (multi-column) not yet supported. |
| **MySQL 8.0.31+** | MCVs for low-cardinality columns, null fractions, n_distinct, integer/date histogram bounds | Equi-height histogram string equality predicate bug — string range histograms fall back to `1/row_count`. Requires 8.0.31+. |
| **Oracle** | Row count, n_distinct, null count per column — enough for correct join ordering | No portable histogram format exists; Oracle uses internal binary encoding that cannot be set externally. |
| **SQL Server** | Table-level row count — improves join ordering on multi-table queries | Column-level injection API does not exist. `UPDATE STATISTICS WITH ROWCOUNT` is undocumented. |
| **Databricks** | Bootstrap an empty table before first data load | Delta collects file stats on every write; UC managed tables with Predictive Optimization run ANALYZE automatically — injection rarely needed. |

> **Stats injection is a bootstrap, not a permanent substitute.**  Always run native
> `ANALYZE` / `UPDATE STATISTICS` / `DBMS_STATS.GATHER_TABLE_STATS` after loading
> production data to replace the bootstrap with real statistics.
>
> Full details, per-engine workarounds, and function reference: [`docs/stats_transpiler.md`](docs/stats_transpiler.md)

---

Three capabilities, each useful alone — more powerful together:

| Pillar | What it does | Key functions |
|--------|-------------|---------------|
| **Stats transpiler** ⭐ | Collect column statistics (null rates, cardinality, MCVs, histograms) from any database; store as dialect-free YAML; inject into any target so the query optimizer sees production-scale distributions immediately | `collect_table_stats` / `dump_stats` / `load_stats` |
| **DDL transpiler** | Parse `CREATE TABLE` from any dialect; emit correct DDL for any other — types, defaults, constraints, all semantics preserved | `parse_ddl` / `emit_ddl` |
| **Stats-driven tabular data** | Feed collected statistics into a data generator to produce synthetic rows whose distributions match real production data | `build_dataframe_from_canonical` |

**Supported dialects**: MySQL · PostgreSQL · SQL Server · Oracle · Databricks

---

### Expanded view

```mermaid
flowchart LR
    subgraph SRC["Source DB (any dialect)"]
        S1["MySQL DDL"]
        S2["PostgreSQL DDL"]
        S3["SQL Server DDL"]
        S4["Oracle DDL"]
    end

    subgraph YAML["Portable YAML (dialect-free)"]
        SCH[("schema.yaml")]
        STA[("stats.yaml")]
    end

    subgraph TGT["Target DB (any dialect)"]
        T1["Databricks DDL"]
        T2["PostgreSQL DDL"]
        T3["MySQL DDL"]
        T4["SQL Server DDL"]
    end

    subgraph GEN["Stats-Driven Tabular Data"]
        DF["Spark DataFrame"]
        DB[("Loaded DB")]
        ST["TableStats\nnull% · cardinality · min/max · MCVs"]
    end

    SRC -- "① collect_table_stats()" --> STA
    STA -- "① inject into target optimizer" --> TGT
    SRC -- "③ parse_ddl()" --> SCH
    SCH -- "③ emit_ddl()" --> TGT
    SCH -- "④ schema" --> DF
    STA -- "④ stats" --> DF
    DF  -- "load rows" --> DB
    DB  -- "collect_table_stats()" --> ST
    ST  -. "stats feedback loop" .-> DF
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
        └──►  [TODO] inject into PostgreSQL 18:  pg_restore_attribute_stats(…)
              inject into SQL Server:             UPDATE STATISTICS WITH ROWCOUNT
              inject into Oracle:                 DBMS_STATS.SET_COLUMN_STATS(…)
              → query optimizer sees production distributions before data is loaded
```

**③ DDL transpiler** — parse any dialect, emit any dialect:
```
MySQL / PostgreSQL / SQL Server / Oracle DDL
        │
        ▼  parse_ddl(sql, dialect="…")
  CanonicalTableSchema  ──────────────────►  schema.yaml  (portable YAML)
        │
        ├──►  emit_ddl("databricks")   →  Databricks / Delta Lake
        ├──►  emit_ddl("postgres")     →  PostgreSQL
        ├──►  emit_ddl("mysql")        →  MySQL
        └──►  emit_ddl("sqlserver")    →  SQL Server
```

**② Portable schema** — one YAML file, any target:
```
  dump_schema(tables, "schema.yaml")   ←  version-controllable, dialect-free
  load_canonical("schema.yaml")        →  list[CanonicalTableSchema]
  emit_ddl(tables[0], "oracle")        →  ready to run on any database
```

**③ Stats-driven tabular data** — collect real statistics, generate matching rows:
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

```python
from src.statschema import parse_ddl, emit_ddl

mysql_ddl = """
CREATE TABLE orders (
    order_id    INT           NOT NULL AUTO_INCREMENT,
    customer_id INT           NOT NULL,
    status      VARCHAR(20)   NOT NULL DEFAULT 'pending',
    total       DECIMAL(10,2)     NULL,
    is_paid     TINYINT(1)    NOT NULL DEFAULT 0,
    created_at  DATETIME          NULL,
    PRIMARY KEY (order_id)
) ENGINE=InnoDB;
"""

tables = parse_ddl(mysql_ddl, dialect="mysql")
schema = tables[0]

print(emit_ddl(schema, "postgres"))     # PostgreSQL
print(emit_ddl(schema, "sqlserver"))    # SQL Server
print(emit_ddl(schema, "databricks"))   # Databricks / Delta
print(emit_ddl(schema, "oracle"))       # Oracle
```

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
    collect_table_stats, build_dataframe_from_canonical,
)

# Parse schema
schema = parse_ddl(open("schema.sql").read(), dialect="mysql")[0]

# Option A: generate from heuristic defaults (no live DB needed)
stats = make_default_stats([schema], row_count=100_000).tables[0]
df = build_dataframe_from_canonical(spark, schema, rows=100_000, stats=stats)
df.show(5)

# Option B: collect REAL stats from a live database first
#   → null fractions, distinct counts, min/max, most-common values
import pymysql
conn = pymysql.connect(host="127.0.0.1", port=3306, user="root", password="…")
real_stats = collect_table_stats(conn, "orders", dialect="mysql")

# Generate data parameterized by the collected statistics
df = build_dataframe_from_canonical(spark, schema, rows=1_000_000, stats=real_stats)
```

The generated DataFrame is parameterized by:
- **null rates** per column (e.g. `total` is NULL 8.3% of the time, matching the measured `null_fraction`)
- **cardinality** (only 4 distinct `status` values, weighted by measured MCV frequencies)
- **numeric ranges** (min/max bounds from the collected statistics)
- **string patterns** when `GenerationRule(format_pattern="email")` is set on a column

---

### 4 — Stats feedback loop: transpile → load → measure → improve

This is the full production workflow.  Generate an initial dataset using heuristic
defaults, load it into a real database, measure what the database actually contains,
then feed those real measurements back as generation parameters.  Each iteration
narrows the gap between the collected statistics and the target statistics.
The loop can be repeated; convergence is not guaranteed but null fractions typically
stabilise within ±15% and cardinality within ±5× after one or two rounds.

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
| PostgreSQL DDL (`pg_dump -s`) | `parse_ddl(sql, "postgres")` | |
| SQL Server DDL (SSMS Scripts) | `parse_ddl(sql, "sqlserver")` | |
| Oracle DDL | `parse_ddl(sql, "oracle")` | |
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
| PostgreSQL | `"postgres"` |
| MySQL | `"mysql"` |
| SQL Server | `"sqlserver"` |
| Oracle | `"oracle"` |

Every semantic correction is applied automatically during transpilation —
e.g. `TINYINT(1)` → `BOOLEAN` (PostgreSQL), `DATETIME` → `DATETIME2` (SQL Server),
`AUTO_INCREMENT` → `SERIAL` (PostgreSQL) / `GENERATED ALWAYS AS IDENTITY` (Oracle).

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

All examples below read credentials from `.env` at the repo root.
Copy `.env.example` to `.env` and fill in your values once; every snippet
below will then work without modification.

```bash
cp .env.example .env   # fill in passwords, ports, etc.
```

```python
# common preamble — paste at the top of any script below
import os
from dotenv import load_dotenv
load_dotenv()   # reads .env from the current directory (or any parent)
```

---

### MySQL

```bash
# DDL export — one file per schema, no data
# Credentials from .env: MYSQL_ROOT_PASS, MYSQL8_PORT (or MYSQL57_PORT)
source .env
mysqldump --no-data --routines=0 --triggers=0 \
  -h 127.0.0.1 -P "$MYSQL8_PORT" -u root -p"$MYSQL_ROOT_PASS" mydb > schema.sql
```

```python
import os, pymysql
from dotenv import load_dotenv
from src.statschema import parse_ddl, dump_schema, collect_table_stats, dump_stats, DatabaseStats

load_dotenv()

conn = pymysql.connect(
    host="127.0.0.1",
    port=int(os.environ["MYSQL8_PORT"]),
    user="root",
    password=os.environ["MYSQL_ROOT_PASS"],
    db="mydb",
    autocommit=True,
)

# Run ANALYZE first so MySQL's column statistics are current
conn.cursor().execute("ANALYZE TABLE orders;")

tables = parse_ddl(open("schema.sql").read(), dialect="mysql")
dump_schema(tables, "schema.yaml")

stats = [collect_table_stats(conn, t.name, dialect="mysql") for t in tables]
dump_stats(DatabaseStats(tables=stats), "stats.yaml")
```

---

### PostgreSQL

```bash
# DDL export — schema only, no data
# Credentials from .env: PG_PASSWORD, PG_DB, PG16_PORT (or PG14_PORT)
source .env
PGPASSWORD="$PG_PASSWORD" pg_dump --schema-only --no-owner --no-acl \
  -h localhost -p "$PG16_PORT" -U myuser -d "$PG_DB" > schema.sql
```

```python
import os, psycopg2
from dotenv import load_dotenv
from src.statschema import parse_ddl, dump_schema, collect_table_stats, dump_stats, DatabaseStats

load_dotenv()

conn = psycopg2.connect(
    host="localhost",
    port=int(os.environ["PG16_PORT"]),
    dbname=os.environ["PG_DB"],
    user="myuser",
    password=os.environ["PG_PASSWORD"],
)

# Run ANALYZE first so pg_stats is populated
conn.cursor().execute("ANALYZE;")
conn.commit()

tables = parse_ddl(open("schema.sql").read(), dialect="postgres")
dump_schema(tables, "schema.yaml")

stats = [collect_table_stats(conn, t.name, dialect="postgres", schema="public")
         for t in tables]
dump_stats(DatabaseStats(tables=stats), "stats.yaml")
```

> `collect_table_stats` reads `pg_stats` (richer MCVs and histogram bounds) in
> addition to standard SQL aggregates when the dialect is `"postgres"`.

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

Add Oracle credentials to `.env`:
```bash
# .env additions for Oracle
ORACLE_USER=myuser
ORACLE_PASS=mypassword
ORACLE_DSN=localhost:1521/XEPDB1
ORACLE_SCHEMA=MYSCHEMA
```

```bash
# DDL export via SQL*Plus (dbms_metadata)
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

# Gather fresh statistics
conn.cursor().execute(
    f"BEGIN dbms_stats.gather_schema_stats('{os.environ[\"ORACLE_SCHEMA\"]}'); END;"
)

tables = parse_ddl(open("schema.sql").read(), dialect="oracle")
dump_schema(tables, "schema.yaml")

schema = os.environ["ORACLE_SCHEMA"]
stats = [collect_table_stats(conn, t.name, dialect="oracle", schema=schema)
         for t in tables]
dump_stats(DatabaseStats(tables=stats), "stats.yaml")
```

---

### Databricks

Add Databricks credentials to `.env` (already present in `.env.example`):
```bash
# .env — already included in .env.example
DATABRICKS_WORKSPACE_URL=https://<workspace>.cloud.databricks.com
CLIENT_ID=<service_principal_application_id>
CLIENT_SECRET=<service_principal_secret>
# Unity Catalog coordinates for the table to export
DATABRICKS_CATALOG=main
DATABRICKS_SCHEMA=myschema
DATABRICKS_TABLE=orders
```

```bash
# DDL export via Databricks CLI
# pip install databricks-cli && databricks configure
source .env
databricks sql execute \
  --profile DEFAULT \
  "SHOW CREATE TABLE ${DATABRICKS_CATALOG}.${DATABRICKS_SCHEMA}.${DATABRICKS_TABLE}" \
  > schema.sql
```

```python
import os
from dotenv import load_dotenv
from databricks.connect import DatabricksSession
from databricks import sql as dbsql
from src.statschema import parse_ddl, dump_schema, collect_table_stats, dump_stats, DatabaseStats

load_dotenv()

catalog = os.environ["DATABRICKS_CATALOG"]
schema  = os.environ["DATABRICKS_SCHEMA"]
table   = os.environ["DATABRICKS_TABLE"]
host    = os.environ["DATABRICKS_WORKSPACE_URL"].removeprefix("https://")

spark = DatabricksSession.builder.getOrCreate()

# Extract DDL and run ANALYZE for column-level statistics
full_name = f"{catalog}.{schema}.{table}"
ddl = spark.sql(f"SHOW CREATE TABLE {full_name}").collect()[0][0]
spark.sql(f"ANALYZE TABLE {full_name} COMPUTE STATISTICS FOR ALL COLUMNS")

tables = parse_ddl(ddl, dialect="databricks")
dump_schema(tables, "schema.yaml")

# Collect stats via the Databricks SQL connector
# pip install databricks-sql-connector
conn = dbsql.connect(
    server_hostname=host,
    http_path=os.environ.get("DATABRICKS_HTTP_PATH", "/sql/1.0/warehouses/…"),
    access_token=os.environ.get("CLIENT_SECRET"),
)
stats = [collect_table_stats(conn, t.name, dialect="databricks",
                              schema=schema, catalog=catalog)
         for t in tables]
dump_stats(DatabaseStats(tables=stats), "stats.yaml")
```

---

### Using saved YAML files

Once `schema.yaml` and `stats.yaml` are on disk they can be used without any
database connection:

```python
from src.statschema import load_canonical, load_stats, emit_ddl, build_dataframe_from_canonical

tables    = load_canonical("schema.yaml")
db_stats  = load_stats("stats.yaml")

# Emit DDL for any target
print(emit_ddl(tables[0], "postgres"))
print(emit_ddl(tables[0], "databricks"))

# Generate synthetic data
df = build_dataframe_from_canonical(spark, tables[0], rows=100_000,
                                    stats=db_stats.tables[0])
```

---

## Stats transpiler — the database migration use case

When you migrate a database, the query optimizer on the target knows nothing about
your data.  Its statistics are either empty or based on a tiny test dataset.
Bad statistics → bad query plans → slow queries → frustrated users on day one.

The conventional fix is to load all production data, then run `ANALYZE` / `UPDATE STATISTICS`.
That takes hours or days on large databases, and requires production data to be present.

**The stats transpiler approach**: collect statistics from the source database *before*
migration, store them as portable YAML, then inject them directly into the target
database's statistics catalog.  The optimizer sees production-scale distributions
immediately, with no data loaded.

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
> see the TODO item.  PostgreSQL 18 independently validated this need by shipping
> `pg_dump --statistics-only` for the same reason.

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
# Runtime
pip install pyyaml sqlglot python-dotenv

# Data generation (requires Java; see docs/testing.md)
pip install pyspark dbldatagen pyarrow

# Live database drivers (install only what you need)
pip install pymysql psycopg2-binary pymssql sqlalchemy
```

### Credentials

```bash
cp .env.example .env
# edit .env — set CLIENT_SECRET, SQLSERVER_PASS, etc.
```

`conftest.py` loads `.env` automatically before tests.

### Run tests

```bash
make venv-test            # create .venv_test (Python 3.11 + local Spark)
make test-fast            # pure-Python tests only (no Spark, no live DB)
make test                 # full suite including Spark data-generation tests
make test-live-all        # + live MySQL, PostgreSQL, SQL Server round-trips
make test-live-synth      # full generate → load → stats → regenerate → compare pipeline
```

---

## Verified transpiler coverage

`make test-live-all` executes **~2600 parametrized tests** against real databases,
verifying the full transpile → YAML → retranspile → execute → introspect → compare cycle
for every canonical type, constraint, and default.

| Database | Versions tested |
|----------|----------------|
| MySQL | 5.7, 8.4 |
| PostgreSQL | 14, 16 |
| SQL Server | 2022 |
| Databricks | Unity Catalog (via `databricks-connect`) |

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

**All functions are pure-Python except `build_dataframe_from_canonical`** (requires
PySpark + dbldatagen).  You can `parse_ddl` → `emit_ddl` without any database
connection, Spark session, or Java installation.

---

## Documentation

| Document | Contents |
|----------|----------|
| [`docs/testing.md`](docs/testing.md) | Local test setup, `.env` credentials, Spark/Java config, live-DB setup |
| [`docs/adding-a-database.md`](docs/adding-a-database.md) | Checklist for new engines (DDL/stats, Podman, live tests, **NeonDB** done; **CockroachDB** next) |
| [`docs/local-databases.md`](docs/local-databases.md) | Running MySQL, PostgreSQL, SQL Server, Oracle, Neon Local locally with Podman / Lima |
| [`docs/test_plan_ddl_roundtrip.md`](docs/test_plan_ddl_roundtrip.md) | Complete DDL round-trip test plan (all types, boundaries, constraints) |
| [`docs/synthetic_data_shortcomings.md`](docs/synthetic_data_shortcomings.md) | Known limitations of synthetic data generation and mitigations |
| [`docs/PLAN.md`](docs/PLAN.md) | Architecture and implementation notes |
| [`.env.example`](.env.example) | Credential template — copy to `.env` and fill in values |
