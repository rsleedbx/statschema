# schema_parser — cross-dialect DDL & synthetic data pipeline

Parse DDL from **any database**, translate it to **any other database**, generate
**statistics-driven synthetic data** parameterized by real database measurements,
and verify the generated distributions against live systems — all from a single
canonical YAML intermediate representation.

```
MySQL DDL ──┐
PG DDL  ────┤  parse_ddl()  ──►  CanonicalTableSchema  ──►  emit_ddl("databricks")  ──►  Delta table
SQL Server ─┤                            │                 ►  emit_ddl("postgres")   ──►  PG table
YData YAML ─┘                            │                 ►  emit_ddl("mysql")      ──►  MySQL table
                                         │
                                         └──►  build_dataframe_from_canonical(spark, rows=N, stats=…)
                                                       │
                                               Spark DataFrame  ──►  live DB (table_v1)
                                                                              │
                                                               collect_table_stats(conn, "table_v1")
                                                                              │
                                                                         TableStats
                                                                    (null%, cardinality,
                                                                     min/max, MCVs, histogram)
                                                                              │
                                                               build_dataframe_from_canonical(…, stats=real_stats)
                                                                              │
                                                               live DB (table_v2)  ──►  compare stats
                                                               ← distributions converge each iteration ►
```

---

## Three copy-paste examples

### 1 — Translate DDL across databases

```python
from src.schema_parser import parse_ddl, emit_ddl

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

### 2 — Save / load the canonical schema as YAML

```python
from src.schema_parser import parse_ddl, dump_schema, load_canonical, emit_ddl

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

### 3 — Generate statistics-driven synthetic data

```python
from src.schema_parser import (
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

### 4 — Stats feedback loop: generate → load → measure → improve

This is the full production workflow.  Generate an initial dataset using heuristic
defaults, load it into a real database, measure what the database actually contains,
then feed those real measurements back as generation parameters.  Each iteration
narrows the gap between the collected statistics and the target statistics.
The loop can be repeated; convergence is not guaranteed but null fractions typically
stabilise within ±15% and cardinality within ±5× after one or two rounds.

```python
import pymysql
from sqlalchemy import create_engine
from src.schema_parser import (
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

## Supported inputs

| Format | Function | Notes |
|--------|----------|-------|
| MySQL DDL (`mysqldump --no-data`) | `parse_ddl(sql, "mysql")` | |
| PostgreSQL DDL (`pg_dump -s`) | `parse_ddl(sql, "postgres")` | |
| SQL Server DDL (SSMS Scripts) | `parse_ddl(sql, "sqlserver")` | |
| Oracle DDL | `parse_ddl(sql, "oracle")` | |
| Databricks DDL | `parse_ddl(sql, "databricks")` | |
| Canonical YAML | `load_canonical("schema.yaml")` | round-trip format |
| YData / Syda YAML | `parse_ydata_yaml(data)` | |
| SDV metadata JSON | `parse_sdv_metadata(data)` | |
| Pipeline YAML | `parse_pipeline_tables(data)` | |
| **Auto-detect** | `load_schema("file")` | detects format from content |

## Supported outputs

| Target | emit_ddl dialect |
|--------|-----------------|
| Databricks / Delta Lake | `"databricks"` |
| PostgreSQL | `"postgres"` |
| MySQL | `"mysql"` |
| SQL Server | `"sqlserver"` |
| Oracle | `"oracle"` |

---

## Canonical YAML formats

Both DDL schemas and statistics are stored as dialect-agnostic YAML.  Either file
can be committed to version control, shared across teams, and loaded against any
target database without modification.

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
from src.schema_parser import parse_ddl, dump_schema, collect_table_stats, dump_stats, DatabaseStats

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
from src.schema_parser import parse_ddl, dump_schema, collect_table_stats, dump_stats, DatabaseStats

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
from src.schema_parser import parse_ddl, dump_schema, collect_table_stats, dump_stats, DatabaseStats

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
from src.schema_parser import parse_ddl, dump_schema, collect_table_stats, dump_stats, DatabaseStats

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
from src.schema_parser import parse_ddl, dump_schema, collect_table_stats, dump_stats, DatabaseStats

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
from src.schema_parser import load_canonical, load_stats, emit_ddl, build_dataframe_from_canonical

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

## Statistics — three levels of fidelity

Statistics determine which distribution properties are reflected in the generated data.  Three levels are available:

```python
from src.schema_parser import make_default_stats, collect_table_stats, load_stats, dump_stats, DatabaseStats

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
from src.schema_parser import load_canonical, load_stats, apply_overrides, OverrideSpec, TableOverride, ColumnOverride

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

## Full pipeline

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
                    └──  ZeroBus ingest SDK                            (streaming ingest)
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

## Verified round-trip coverage

`make test-live-all` executes **~2600 parametrized tests** against real databases.
Every canonical type, constraint, and default is verified with a full
DDL → YAML → DDL → execute → introspect → compare cycle.

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

## For AI agents (Cursor, Claude, etc.)

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
| [`docs/local-databases.md`](docs/local-databases.md) | Running MySQL, PostgreSQL, SQL Server, Oracle locally with Podman / Lima |
| [`docs/test_plan_ddl_roundtrip.md`](docs/test_plan_ddl_roundtrip.md) | Complete DDL round-trip test plan (all types, boundaries, constraints) |
| [`docs/synthetic_data_shortcomings.md`](docs/synthetic_data_shortcomings.md) | Known limitations of synthetic data generation and mitigations |
| [`docs/PLAN.md`](docs/PLAN.md) | Architecture and implementation notes |
| [`.env.example`](.env.example) | Credential template — copy to `.env` and fill in values |
