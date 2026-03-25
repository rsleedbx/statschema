# TPC Load Benchmark Results

Measures raw load throughput using the `statschema` canonical pipeline:
`generate_rows()` (canonical YAML schema) → `load_dataframe()` (bulk loader).

All tables are created without PK/FK constraints (load benchmark, not constraint benchmark).
Seed: 42 (reproducible). Run dates: 2026-03-22 / 2026-03-24, macOS Apple Silicon (M-series).

---

## Row counts by scale factor

### TPC-C

| Table        | SF = 1 (standard) |
|:-------------|------------------:|
| item         |           100,000 |
| warehouse    |                 1 |
| district     |                10 |
| stock        |           100,000 |
| customer     |            30,000 |
| history      |            30,000 |
| orders       |            30,000 |
| new_order    |             9,000 |
| order_line   |           300,000 |
| **Total**    |       **599,011** |

`item` and `stock` are fixed-size regardless of SF; all other tables scale linearly with SF.

### TPC-H

| Table    | SF = 0.1 (ARM64) | SF = 0.01 (QEMU) |
|:---------|-----------------:|-----------------:|
| region   |                5 |                5 |
| nation   |               25 |               25 |
| supplier |            1,000 |              100 |
| part     |           20,000 |            2,000 |
| customer |           15,000 |            1,500 |
| partsupp |           80,000 |            8,000 |
| orders   |          150,000 |           15,000 |
| lineitem |          600,000 |           60,000 |
| **Total**|      **866,030** |       **86,630** |

`region` and `nation` are fixed; `lineitem` dominates (≈69% of rows at any SF).

---

## TPC-C

Scale factor **SF=1** = 599,011 rows across 9 tables.  
QEMU databases (SQL Server / Oracle / Db2) are run under Lima + QEMU x86_64
and are expected to be 3–10× slower than native ARM64.

| Database                | Strategy    | SF   | Rows loaded | Total (s) | Rows / s |
|:------------------------|:------------|-----:|------------:|----------:|---------:|
| SQLite (in-process)     | multi_row   |  1.0 |     599,011 |       8.1 |   74,259 |
| PostgreSQL 14           | bulk_copy   |  1.0 |     599,011 |      12.2 |   49,201 |
| PostgreSQL 16           | bulk_copy   |  1.0 |     599,011 |      12.0 |   49,935 |
| MariaDB 10.11           | bulk_copy   |  1.0 |     599,011 |      11.7 |   51,353 |
| MariaDB 11.4            | bulk_copy   |  1.0 |     599,011 |      11.1 |   53,794 |
| MySQL 5.7               | bulk_copy   |  1.0 |     599,011 |      26.9 |   22,299 |
| SQL Server 2022 *(QEMU)*| multi_row   |  0.1 |     149,902 |     170.2 |      881 |
| Oracle XE 21c *(QEMU)*  | multi_row   |  1.0 |     599,011 |     157.7 |    3,798 |
| IBM Db2 CE 11.5 *(QEMU)*| multi_row   |  1.0 |     599,011 |     313.1 |    1,913 |

---

## TPC-H

Scale factor **SF=0.1** = 866,030 rows (native ARM64). **SF=1** = 8,660,030 rows.  
QEMU databases use **SF=0.01** = 86,630 rows.

| Database                | Strategy    |   SF | Rows loaded | Total (s) | Rows / s |
|:------------------------|:------------|-----:|------------:|----------:|---------:|
| SQLite (in-process)     | multi_row   | 0.01 |      86,630 |       1.4 |   62,683 |
| PostgreSQL 14           | bulk_copy   |  0.1 |     866,030 |      19.2 |   45,054 |
| PostgreSQL 16           | bulk_copy   |  0.1 |     866,030 |      18.2 |   47,540 |
| MariaDB 10.11           | bulk_copy   |  0.1 |     866,030 |      18.6 |   46,526 |
| MariaDB 11.4            | bulk_copy   |  0.1 |     866,030 |      18.9 |   45,792 |
| MySQL 5.7               | bulk_copy   |  0.1 |     866,030 |      45.0 |   19,230 |
| CockroachDB 23          | bulk_copy   |  0.1 |     866,030 |      21.2 |   40,843 |
| MySQL 8.0               | multi_row   |  1.0 |   8,660,030 |     338.3 |   25,598 |
| MariaDB 10.11           | multi_row   |  1.0 |   8,660,030 |     322.4 |   26,858 |
| SQL Server 2022 *(QEMU)*| multi_row   | 0.01 |      86,630 |     182.7 |      474 |
| Oracle XE 21c *(QEMU)*  | multi_row   | 0.01 |      86,630 |     145.3 |      596 |
| IBM Db2 CE 11.5 *(QEMU)*| multi_row   | 0.01 |      86,630 |      65.0 |    1,332 |

---

## Notes

- **Strategy**: `bulk_copy` uses the native driver copy protocol (PostgreSQL `COPY`,
  MySQL `LOAD DATA LOCAL INFILE`).  `multi_row` uses parameterized `executemany`.
- **QEMU overhead**: SQL Server, Oracle XE, and IBM Db2 CE have no `linux/arm64`
  images; they run in Lima VMs under QEMU x86_64 emulation.  This adds significant
  CPU overhead, masking driver and engine differences.
- **SQL Server SF=0.1**: SF=1 TPC-C for SQL Server was not completed in this run
  due to the QEMU slowdown (estimated ~1,800 s at 881 r/s).  SF=0.1 is used instead.
- **SQLite**: In-process file database, included as a development baseline.
  Throughput is CPU/disk-bound on the local machine, not comparable to network DBs.
- Per-table JSON results: `benchmarks/results/202603*-tpcc-*.json` and
  `benchmarks/results/202603*-tpch-*.json`.

---

## Strategy comparison

TPC-H SF=0.001 (8,690 rows) and TPC-C SF=0.001 (104,992 rows, item fixed at 100K).  
Singleton strategy is skipped for TPC-C because the fixed 100K `item` table makes it impractically slow on network databases.  
Strategy is measured as rows/s (higher is better).  
**n/a** = strategy not supported in this environment.  
¹ = dialect has no native COPY protocol; bulk_copy silently falls back to multi_row.

### TPC-H  — rows/s by strategy

| Database | singleton | multi_row | bulk_copy |
|:---------|----------:|----------:|----------:|
| PostgreSQL 14           |       2,937 r/s |      23,499 r/s |      41,196 r/s |
| PostgreSQL 16           |       3,266 r/s |      24,962 r/s |      40,844 r/s |
| MySQL 5.7               |       1,632 r/s |      10,091 r/s |      15,575 r/s |
| MariaDB 10.11           |       3,692 r/s |      26,343 r/s |      39,795 r/s |
| MariaDB 11.4            |       3,458 r/s |      26,052 r/s |      41,358 r/s |
| SQL Server 2022         |         280 r/s |         447 r/s |             n/a |
| Oracle XE 21c           |         839 r/s |         145 r/s |       172 r/s ¹ |
| IBM Db2 CE 11.5         |         287 r/s |         925 r/s |         600 r/s |

### TPC-C  — rows/s by strategy  *(singleton skipped)*

| Database | multi_row | bulk_copy |
|:---------|----------:|----------:|
| PostgreSQL 14           |      48,264 r/s |      78,934 r/s |
| PostgreSQL 16           |      49,974 r/s |      77,816 r/s |
| MySQL 5.7               |      20,271 r/s |      23,735 r/s |
| MariaDB 10.11           |      50,071 r/s |      77,589 r/s |
| MariaDB 11.4            |      50,996 r/s |      80,829 r/s |
| SQL Server 2022         |       1,125 r/s |             n/a |
| Oracle XE 21c           |       3,628 r/s |     4,189 r/s ¹ |
| IBM Db2 CE 11.5         |       6,329 r/s |       7,180 r/s |

**n/a** for SQL Server bulk_copy: `BULK INSERT` requires the staging CSV to be readable from the SQL Server process filesystem.
When SQL Server runs inside a Lima VM, macOS temp files are not accessible to the VM.
¹ Oracle, SQLite, and Databricks have no native COPY protocol; bulk_copy silently falls back to multi_row.

---

## TPC-B

Tables use pgbench naming (`pgbench_accounts`, `pgbench_branches`, `pgbench_tellers`,
`pgbench_history`) so pgbench can run its standard TPC-B-like transaction mix directly.  
Scale factor **SF=1** = 200,011 rows (branches=1, tellers=10, accounts=100K, history=100K).  
QEMU databases use SF=0.1 = 20,002 rows.  
Strategy: BULK_COPY for PostgreSQL / MySQL / MariaDB; MULTI_ROW for QEMU-hosted databases.  

### Initial load

| Database                 | TPC-B (initial load)                   |
|:------------------------:|:--------------------------------------:|
| SQLite (in-process)      | 1.6s · 127K r/s · 200,011 rows         |
| PostgreSQL 14            | 2.1s · 94K r/s · 200,011 rows          |
| PostgreSQL 16            | 2.2s · 91K r/s · 200,011 rows          |
| MySQL 5.7                | 6.6s · 30K r/s · 200,011 rows          |
| MariaDB 10.11            | 2.0s · 98K r/s · 200,011 rows          |
| MariaDB 11.4             | 2.0s · 100K r/s · 200,011 rows         |
| CockroachDB 23           | 3.0s · 66K r/s · 200,011 rows          |
| SQL Server 2022 *(QEMU)* | 16.8s · 1K r/s · 20,002 rows           |
| Oracle XE 21c *(QEMU)*   | 6.3s · 3K r/s · 20,002 rows            |
| IBM Db2 CE 11.5 *(QEMU)* | 5.1s · 3K r/s · 20,002 rows            |

### pgbench transaction throughput

After loading, primary keys are added back to `pgbench_accounts/branches/tellers`
so pgbench uses index seeks.  Run: `pgbench -c 4 -t 500 -n` (no vacuum).  

| Database                 | pgbench result                     |
|:------------------------:|:----------------------------------:|
| PostgreSQL 14            | 1084.5 tps  (clients=4 txns=500)   |
| PostgreSQL 16            | 959.7 tps  (clients=4 txns=500)    |
| MySQL 5.7                | n/a (pgbench only targets PostgreSQL) |
| MariaDB 10.11            | n/a (pgbench only targets PostgreSQL) |
| MariaDB 11.4             | n/a (pgbench only targets PostgreSQL) |
| SQL Server 2022          | n/a (pgbench only targets PostgreSQL) |
| Oracle XE 21c            | n/a (pgbench only targets PostgreSQL) |
| IBM Db2 CE 11.5          | n/a (pgbench only targets PostgreSQL) |

### Append load (SF=1 added on top of existing data)

Runs with `--append`: tables are not recreated; sequential PKs continue
from the current row count; FK ranges span existing + new rows.

| Database                 | TPC-B (append +SF=1)                   |
|:------------------------:|:--------------------------------------:|
| PostgreSQL 14            | 2.3s · 88K r/s · 200,011 rows          |
| PostgreSQL 16            | 2.0s · 98K r/s · 200,011 rows          |
| MySQL 5.7                | 5.5s · 36K r/s · 200,011 rows          |
| MariaDB 10.11            | 2.0s · 97K r/s · 200,011 rows          |
| MariaDB 11.4             | 2.0s · 100K r/s · 200,011 rows         |
| SQL Server 2022 *(QEMU)* | 17.0s · 1K r/s · 20,002 rows           |
| Oracle XE 21c *(QEMU)*   | 3.1s · 6K r/s · 20,002 rows            |
| IBM Db2 CE 11.5 *(QEMU)* | 4.9s · 4K r/s · 20,002 rows            |
---

## TPC-DS

Scale factor **SF=0.1** = 3,834,791 rows across 24 tables (native ARM64).

| Database                | Strategy    |   SF | Rows loaded | Total (s) | Rows / s |
|:------------------------|:------------|-----:|------------:|----------:|---------:|
| SQLite (in-process)     | multi_row   |  0.1 |   3,834,791 |      23.1 |  166,317 |
| PostgreSQL 16           | bulk_copy   |  0.1 |   3,834,791 |      30.0 |  127,688 |
| CockroachDB 23          | bulk_copy   |  0.1 |   3,834,791 |      39.8 |   96,434 |
| MariaDB 10.11           | multi_row   |  0.1 |   3,834,791 |      65.1 |   58,924 |
| MySQL 8.0               | multi_row   |  0.1 |   3,834,791 |      72.0 |   53,264 |

`customer_demographics` (1.92M rows) dominates at SF=0.1; `date_dim` and `time_dim` are fixed-size.

---

## TPC-E

Scale factor **SF=0.1** = 8,228,274 rows across 32 tables (native ARM64).

| Database                | Strategy    |   SF | Rows loaded | Total (s) | Rows / s |
|:------------------------|:------------|-----:|------------:|----------:|---------:|
| SQLite (in-process)     | multi_row   |  0.1 |   8,228,274 |      56.7 |  145,126 |
| PostgreSQL 16           | bulk_copy   |  0.1 |   8,228,274 |      71.5 |  115,064 |
| CockroachDB 23          | bulk_copy   |  0.1 |   8,228,274 |      93.3 |   88,204 |
| MariaDB 10.11           | multi_row   |  0.1 |   8,228,274 |     126.6 |   65,009 |
| MySQL 8.0               | multi_row   |  0.1 |   8,228,274 |     135.4 |   60,780 |

`watch_item` (150K), `account_permission` (75K), `customer_account` (50K), `holding` (50K),
and `holding_history` (100K) dominate at SF=0.1.
