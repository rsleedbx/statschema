# QEMU Parallelism and Capacity Planning

Findings from running the identity test matrix (36 combinations: 6 engines × 6 TPC schemas) across
native ARM64 (Podman) and x86\_64-emulated (QEMU/Lima) database engines on Apple Silicon.

---

## QEMU CPU math is fundamentally different

A Lima VM running x86\_64 on an Apple Silicon host uses QEMU software emulation.  Every x86
instruction the guest executes is decoded and re-issued as ARM64 instructions by the QEMU process.
Two consequences:

1. **Integer-heavy workloads run significantly slower than native.**  `UPDATE STATISTICS` and
   `RUNSTATS` are histogram computations — pure integer arithmetic.  Each x86 instruction is
   decoded and re-executed as ARM64 instructions by QEMU's JIT translator.  Published benchmarks
   for CPU-intensive x86 workloads on ARM QEMU report 5–15× slowdown (10–20% of native
   throughput).  This is consistent with our measurements: Podman-native engines complete all 6
   TPC schemas in 1–5 min; QEMU engines take 9–20 min for the same schemas.

2. **Active QEMU VMs compete for host CPU.**  Three VMs running active workloads simultaneously
   each slow to roughly half their uncontested speed (measured: SQL Server 18.7 min solo vs 32.6
   min with Oracle + DB2 also active — 1.7× slowdown).  Idle QEMU VMs also consume a small
   amount of host CPU (interrupt handling, kernel timers — typically 1–5% per VM), enough to
   affect sensitive single-run benchmarks.

---

## Measured contention: 3 simultaneous QEMU VMs

SQL Server alone at `schema_workers=3` (uncontested):

| Schema | Time |
|--------|------|
| tpcb, tpch | ~3 min |
| tpcc | ~8 min |
| tpcdi, tpcds | ~7–9 min each |
| tpce | ~11 min |
| **Total** | **18.7 min** |

SQL Server in wave 2 with Oracle + DB2 also running at `schema_workers=3`:

| Schema | Time (contended) | Slowdown |
|--------|-----------------|---------|
| tpcb | 5.25 min | 1.9× |
| tpcc | 10.4 min | 1.3× |
| tpcdi | 14.3 min | 2.0× |
| tpce | 22.2 min | ~2.0× |

**Three QEMU VMs competing at equal parallelism produce ~2× slowdown per VM.**

---

## Parallelism increases harm under CPU saturation

Adding `schema_workers` to a QEMU engine while other QEMU engines are also active does not
improve wall time — it divides a fixed CPU budget across more concurrent threads, each running
slower.

Measured result: two-wave run (wave 2 = all three QEMU engines at `sw=3` simultaneously):

- Wave 1 (Podman — postgres, mysql, cockroachdb): **4.7 min**
- Wave 2 (QEMU — sqlserver, oracle, db2 at `sw=3`): **32.6 min**
- Total: **37 min**

Running sqlserver alone at `sw=3` (wave 2 uncontested): **18.7 min**.  Oracle + DB2 QEMU
processes running concurrently caused a 14-minute regression for SQL Server.

**The optimal strategy for QEMU engines is sequential waves, not parallel fan-out.**

---

## Two-wave scheduling

`benchmarks/run_identity.sh` implements two-wave scheduling by default:

```
Wave 1: Podman engines (postgres, cockroachdb, mysql)
        → run at full native parallelism (sw=5–6 each)
        → completes in ~5 min

Wave 2: QEMU engines (sqlserver, oracle, db2)
        → run only after Wave 1 is fully complete
        → each engine gets sw=3 with no Podman competition
        → completes in ~32 min
```

Total wall time: **~37 min**.  Without two-wave (all 6 engines simultaneously at high parallelism),
QEMU contention caused runtimes of 55–60+ min.

---

## Profiling before optimizing I/O

`UPDATE STATISTICS` (SQL Server), `DBMS_STATS` (Oracle), and `RUNSTATS` (DB2) are CPU-bound,
not I/O-bound.  SQL Server `sys.dm_os_wait_stats` after a full identity test run:

| Wait type | % of total |
|-----------|-----------|
| `SOS_WORK_DISPATCHER` (CPU scheduling) | **91.1%** |
| `QDS_PERSIST_TASK_MAIN_LOOP_SLEEP` | 2.4% |
| `SOS_SCHEDULER_YIELD` | 0.6% |
| `WRITELOG` (log I/O) | **0.0%** |
| `PAGEIOLATCH_*` (data I/O) | **0.0%** |

Zero I/O wait means the database is not waiting on disk — it is waiting for CPU time.
I/O optimizations (recovery model changes, `NOLOGGING`) have no effect on a CPU-bound workload.

**Check wait stats before applying any performance fix.  An I/O solution to a CPU problem
produces no improvement.**

---

## Recovery model experiment: SQL Server SIMPLE recovery

`ALTER DATABASE [name] SET RECOVERY SIMPLE` was applied to each test database at creation
(in `create_schema`).  Result: confirmed via `sys.databases` — all identity test databases
show `recovery_model_desc = 'SIMPLE'`.

Timing comparison (sqlserver alone at `sw=3`):

| Config | Total time | tpce |
|--------|-----------|------|
| FULL recovery (before) | 18.7 min | ~11 min |
| SIMPLE recovery (after) | 24.7 min | 16.4 min |

The SIMPLE recovery run was slower.  Two contributing factors:

1. Oracle and DB2 QEMU VMs were running (idle but still consuming host CPU) during the SIMPLE
   recovery run; they were not confirmed running during the baseline measurement.
2. Run-to-run variance for QEMU workloads is large (~25%) because host CPU availability is
   influenced by background system processes.

**SIMPLE recovery is kept** because it prevents test transaction logs from growing unboundedly and
is semantically correct for ephemeral test data.  It does not reduce compute time.

---

## Oracle NOLOGGING experiment

Three approaches tested (Oracle alone at `sw=3`):

| Approach | Total time |
|----------|-----------|
| Per-table `ALTER TABLE … NOLOGGING` after DDL | 9.05 min |
| No NOLOGGING (baseline) | 7.73 min |
| Tablespace `ALTER TABLESPACE USERS NOLOGGING` in `create_schema` | 9.62 min |

Per-table NOLOGGING added ~79 s of catalog update overhead (20 tables × 6 schemas × 2 phases),
exceeding the redo savings at the tested scale factors (0.01–0.1 SF).

**Tablespace-level NOLOGGING** is kept in `oracle.py`'s `create_schema`:
- Zero per-run overhead (runs once per schema, is idempotent)
- Correct for test data — no recoverability needed
- Provides redo savings at larger scale factors where the savings exceed the single DDL statement cost

Run-to-run variance (three identically-configured oracle runs): **7.73 – 9.62 min (24% range)**.
Single-run A/B comparisons for QEMU workloads are unreliable without 3+ samples per condition.

---

## DB2 logging

DB2 already uses `ADMIN_CMD('LOAD … NONRECOVERABLE')` for all bulk loads.  `NONRECOVERABLE`
means extents are not logged and the loaded data is not forward-recoverable.  This is the DB2
equivalent of Oracle direct-path NOLOGGING.  No further logging optimization is available.

---

## A/B test methodology for QEMU workloads

Standard A/B comparison rules break on QEMU because:

- Background QEMU VM processes (idle or not) consume host CPU
- Run-to-run variance is 20–30% for CPU-intensive workloads
- A 15% measured difference may be within noise

Rules for valid A/B comparisons on QEMU:

1. **Identical VM inventory in both runs.** Stop VMs not under test; confirm with `limactl list`
   before each run.
2. **At least 3 samples per condition.**  Discard outliers; compare medians not single runs.
3. **Check host CPU pressure** during the run: `top -l 1 | head -5` on macOS or `ps aux | grep qemu`.
4. **Measure per-schema times**, not just total.  Total conflates scheduling order with per-schema speed.

---

## 8 GiB Lima VMs

SQL Server, Oracle, and DB2 Lima VMs were upgraded from 4 GiB to 8 GiB to enable `sw > 1`.
Config files updated: `config/lima/sqlserver.yaml`, `config/lima/oracle.yaml`, `config/lima/db2.yaml`.

SQL Server `memorylimitmb` raised from 3072 to 5632 in the provision script to use the extra RAM.

Memory budget at idle (8 GiB VMs):

| Engine | Idle used | Free | Safe workers |
|--------|----------|------|-------------|
| SQL Server | ~5.6 GB (buffer pool) | ~2.4 GB | 2–3 |
| Oracle XE | ~866 MB (SGA fixed) | ~7.1 GB | 3 |
| DB2 CE | ~818 MB (STMM capped) | ~7.2 GB | 3 |

**VM resource changes require delete + recreate.**  `cpus:` and `memory:` are baked into the QEMU
command at creation time and cannot be changed in place.

---

## DB2 instance ownership bug

After upgrading the DB2 VM to 8 GiB and recreating it, the DB2 instance directory became
unowned by `db2inst1`, causing `db2icrt` and `db2ftok` failures on container restart:

```
db2ftok.C:87: Failed to generate seed
ERROR: The instance home directory /home/db2inst1 is invalid because it is not owned by db2inst1
```

Root cause: `podman run` recreates the container from image on each VM start, and the image's
`useradd` assigns a new UID that may not match the volume's ownership.

Fix: `ExecStartPre` directives in `container-db2ce.service` (in `config/lima/db2.yaml`) run
`chown -R db2inst1:db2inst1 /home/db2inst1` and `chown db2inst1:db2iadm1 /home/db2inst1/sqllib/security/db2ftok`
before the service starts.

---

## Oracle XE 12 GB limit

Oracle XE has a hard 12 GB database size limit.  After multiple test runs, `users01.dbf`
accumulated data from old schemas until the limit was reached:

```
ORA-12954: The request exceeds the maximum allowed database size of 12 GB.
```

Fix:
1. Drop all old test schemas (`DROP USER schema CASCADE`).
2. Shrink the datafile: `ALTER DATABASE DATAFILE 'users01.dbf' RESIZE 3G`.
3. Cap growth: `ALTER DATABASE DATAFILE 'users01.dbf' AUTOEXTEND ON MAXSIZE 10G`.
