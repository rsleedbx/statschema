# statschema TPC Load Benchmark

Generates synthetic TPC-C and TPC-H data at a configurable scale factor and
loads it into any supported database, recording timing at table granularity.
Results are saved as JSON in `benchmarks/results/` and can be committed to
track progress over time.

## Quick start — SQLite (no setup)

```bash
python benchmarks/run_bench.py --schema tpcc --sf 1 --dialect sqlite
python benchmarks/run_bench.py --schema tpch --sf 1 --dialect sqlite
```

SQLite is always available and useful for validating the pipeline, but its
single-writer lock makes it a poor baseline for bulk-load performance.

## Scale factors

| Schema | SF | Approx rows | Notes |
|--------|-----|-------------|-------|
| TPC-C  | 1  | ~460 K      | 1 warehouse |
| TPC-C  | 10 | ~4.6 M      | 10 warehouses |
| TPC-H  | 1  | ~8.7 M      | LINEITEM alone is ~6 M rows |
| TPC-H  | 10 | ~87 M       | Requires bulk-copy strategy |

## Strategy auto-selection

`--strategy auto` (default) picks the fastest proven path per dialect:

| Dialect | Strategy | Mechanism |
|---------|----------|-----------|
| postgres / cockroachdb / neon | bulk\_copy | `COPY … FROM STDIN WITH CSV` — no temp file |
| mysql / mariadb | bulk\_copy | `LOAD DATA LOCAL INFILE` |
| sqlserver (mssql-python) | bulk\_copy | `conn.bulk_copy()` via native BCP/DDBC |
| sqlserver (pyodbc/pymssql) | bulk\_copy | `BULK INSERT` from staging CSV |
| db2 | bulk\_copy | `ADMIN_CMD('LOAD FROM … OF DEL FORMAT')` |
| oracle | multi\_row | `INSERT ALL … SELECT 1 FROM DUAL` |
| sqlite | multi\_row | `INSERT … VALUES (r1),(r2),…` |

## Live database setup

### PostgreSQL
```bash
export BENCH_PG_DSN="host=localhost dbname=bench user=postgres password=secret"
python benchmarks/run_bench.py --schema tpch --sf 1 --dialect postgres
```

### MySQL / MariaDB
```bash
export BENCH_MYSQL_HOST=localhost
export BENCH_MYSQL_USER=root
export BENCH_MYSQL_PASS=secret
export BENCH_MYSQL_DB=bench
python benchmarks/run_bench.py --schema tpcc --sf 1 --dialect mysql
```

### SQL Server
```bash
export BENCH_SQLSERVER_DSN="SERVER=localhost;DATABASE=bench;UID=sa;PWD=secret"
python benchmarks/run_bench.py --schema tpcc --sf 1 --dialect sqlserver
```
Install `mssql-python` for the fastest BCP path, or fall back to `pymssql`.

### Oracle
```bash
export BENCH_ORACLE_DSN="localhost/XEPDB1"
export BENCH_ORACLE_USER=bench
export BENCH_ORACLE_PASS=secret
python benchmarks/run_bench.py --schema tpch --sf 1 --dialect oracle
```

### IBM Db2
```bash
export BENCH_DB2_DSN="DATABASE=bench;HOSTNAME=localhost;PORT=50000;UID=db2inst1;PWD=secret"
python benchmarks/run_bench.py --schema tpch --sf 1 --dialect db2
```

## Result format

Each run writes `benchmarks/results/<timestamp>-<schema>-sf<N>-<dialect>.json`:

```json
{
  "benchmark_id": "20260322-100000-tpch-sf1-postgres",
  "timestamp": "2026-03-22T10:00:00+00:00",
  "host": "myhost",
  "git_sha": "abc1234",
  "schema": "tpch",
  "scale_factor": 1,
  "dialect": "postgres",
  "driver": "psycopg2",
  "strategy": "bulk_copy",
  "ddl_s": 0.12,
  "tables": {
    "region":   {"rows": 5,       "load_s": 0.001},
    "lineitem": {"rows": 6000000, "load_s": 4.21}
  },
  "totals": {
    "rows": 8661470,
    "load_s": 12.4,
    "rows_per_s": 698505
  }
}
```

Commit the result files to track improvements as the loader evolves.

## Tracking progress

```bash
# Compare two runs
python - <<'EOF'
import json, glob, sys
files = sorted(glob.glob("benchmarks/results/*.json"))
for f in files:
    r = json.load(open(f))
    t = r["totals"]
    print(f"{r['benchmark_id']:50s}  {t['rows']:>10,}  {t['rows_per_s']:>10,.0f} r/s")
EOF
```
