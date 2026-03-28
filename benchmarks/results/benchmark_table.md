# TPC Benchmark Results

Scale factors: TPC-C SF=1 (~599K rows), TPC-H SF=0.1 (~87K rows).  
Strategy: BULK_COPY for PostgreSQL / MySQL / MariaDB; MULTI_ROW for SQL Server / Oracle / Db2.  
Generator: `generate_rows()` driven by canonical schema YAML (`benchmarks/schemas/`).  
Seed: 42 (reproducible).  

| Database               | TPC-C SF=1                         | TPC-H SF=0.1                       |
|:----------------------:|:----------------------------------:|:----------------------------------:|
| PostgreSQL 14          | ERR                                | ERR                                |
| PostgreSQL 16          | ERR                                | ERR                                |
| PostgreSQL 18          | ERR                                | ERR                                |
| CockroachDB            | 20.2s · 29K r/s · 599K rows        | 39.6s · 21K r/s · 866K rows        |
| MySQL 5.7              | ERR                                | ERR                                |
| MySQL 8.0              | 14.2s · 42K r/s · 599K rows        | 49.6s · 17K r/s · 866K rows        |
| MariaDB 10.11          | ERR                                | ERR                                |
| MariaDB 11.4           | ERR                                | ERR                                |
| SQL Server 2022        | ERR                                | ERR                                |
| Oracle XE 21c          | ERR                                | ERR                                |
| IBM Db2 CE 11.5        | 489.2s · 1K r/s · 599K rows        | 950.8s · 0K r/s · 866K rows        |

---

**Columns:** `total_load_s · rows/s (K) · rows loaded`  
**ERR** = connection failed or load error (see `benchmarks/results/*.json` for details)  
**—** = not attempted

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
| PostgreSQL 14            | ERR: unreachable                       |
| PostgreSQL 16            | ERR: unreachable                       |
| PostgreSQL 18            | ERR: unreachable                       |
| CockroachDB              | 6.7s · 29K r/s · 200,011 rows          |
| MySQL 5.7                | ERR: unreachable                       |
| MySQL 8.0                | 5.3s · 37K r/s · 200,011 rows          |
| MariaDB 10.11            | ERR: unreachable                       |
| MariaDB 11.4             | ERR: unreachable                       |
| SQL Server 2022          | ERR: unreachable                       |
| Oracle XE 21c *(QEMU)*   | ERR: ORA-02373: Error parsing insert statement for tabl |
| IBM Db2 CE 11.5 *(QEMU)* | 7.0s · 2K r/s · 20,002 rows            |

### pgbench transaction throughput

After loading, primary keys are added back to `pgbench_accounts/branches/tellers`
so pgbench uses index seeks.  Run: `pgbench -c 4 -t 500 -n` (no vacuum).  

| Database                 | pgbench result                     |
|:------------------------:|:----------------------------------:|
| PostgreSQL 14            | n/a (unreachable)                  |
| PostgreSQL 16            | n/a (unreachable)                  |
| PostgreSQL 18            | n/a (unreachable)                  |
| CockroachDB              | 60.4 tps  (clients=4 txns=500)     |
| MySQL 5.7                | n/a (unreachable)                  |
| MySQL 8.0                | n/a (pgbench only targets PostgreSQL) |
| MariaDB 10.11            | n/a (unreachable)                  |
| MariaDB 11.4             | n/a (unreachable)                  |
| SQL Server 2022          | n/a (unreachable)                  |
| Oracle XE 21c            | n/a (load failed)                  |
| IBM Db2 CE 11.5          | n/a (pgbench only targets PostgreSQL) |

### Append load (SF=1 added on top of existing data)

Runs with `--append`: tables are not recreated; sequential PKs continue
from the current row count; FK ranges span existing + new rows.

| Database                 | TPC-B (append +SF=1)                   |
|:------------------------:|:--------------------------------------:|
| PostgreSQL 14            | ERR: unreachable                       |
| PostgreSQL 16            | ERR: unreachable                       |
| PostgreSQL 18            | ERR: unreachable                       |
| CockroachDB              | 5.8s · 34K r/s · 200,011 rows          |
| MySQL 5.7                | ERR: unreachable                       |
| MySQL 8.0                | 4.8s · 42K r/s · 200,011 rows          |
| MariaDB 10.11            | ERR: unreachable                       |
| MariaDB 11.4             | ERR: unreachable                       |
| SQL Server 2022          | ERR: unreachable                       |
| Oracle XE 21c *(QEMU)*   | ERR: skipped (initial failed)          |
| IBM Db2 CE 11.5 *(QEMU)* | 7.5s · 2K r/s · 20,002 rows            |
