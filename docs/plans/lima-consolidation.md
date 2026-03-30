# Lima VM Consolidation Plan: Unified x86_64 Stack

**Status:** Draft  
**Context:** SQL Server, Oracle XE, and IBM Db2 CE all require x86_64 Linux because
no ARM64 images exist.  Today each runs on its own Lima VM.  This plan evaluates
whether DB2 and Oracle (already containerised inside their VMs) and SQL Server
(currently a bare apt install inside its VM) should all run as Podman containers
inside a single shared Lima VM, and what that means for file staging.

---

## Current Architecture

```
macOS host (Apple Silicon)
│
├── Lima VM: sqlserver22   (x86_64 Ubuntu 20.04, 4 CPU / 8 GB)
│   └── mssql-server       ← bare apt install, systemd service
│       /tmp/lima ←────────── writable shared mount (host ↔ VM)
│
├── Lima VM: db2           (x86_64 Ubuntu 22.04, 4 CPU / 8 GB)
│   └── Podman container: db2ce
│       └── DB2 CE (icr.io/db2_community/db2)
│       /tmp/lima ←────────── writable shared mount (host ↔ VM)
│       /opt/db2data ←──────── persistent volume (VM-local)
│
└── Lima VM: oracle        (x86_64 Ubuntu 22.04, 4 CPU / 8 GB)
    └── Podman container: oracle-xe
        └── Oracle XE 21c (gvenzl/oracle-xe:21-slim)
        /tmp/lima ←────────── writable shared mount (host ↔ VM)
```

**Total QEMU overhead:** 3 VMs × (boot kernel + QEMU process + Ubuntu userspace)  
**Total RAM reserved:** 3 × 8 GiB = 24 GiB (hard-committed, not swappable)

---

## Option A — Status Quo (keep three separate VMs)

No changes.

**Pros**
- Blast radius is isolated: a DB2 STMM runaway or SQL Server OOM cannot kill Oracle or SQL Server.
- Independent APFS snapshots (`state0`, `state1`, `state2`) per engine — restore one without touching others.
- Different Ubuntu versions per engine (SQL Server needs Ubuntu 20.04; DB2/Oracle prefer 22.04).
- VM-level resource caps (`memory:`, `cpus:`) are per-engine and easy to tune independently.
- `limactl stop --force sqlserver22` in 65 ms — stops only the engine under test, leaves others warm.

**Cons**
- 3 × QEMU process overhead (~100–200 MB host RAM per idle VM kernel+hypervisor).
- 3 × Ubuntu images to update and maintain.
- APFS snapshot disk space: three separate `diffdisk` files (44 GB + 100 GB + 100 GB).
- Each new developer must provision three VMs on first setup.

---

## Option B — Single shared Lima VM (all three as Podman containers)

Collapse SQL Server, DB2, and Oracle into one Lima VM.  SQL Server would become
a container (`mcr.microsoft.com/mssql/server:2022-latest`) instead of a bare apt install.

```
macOS host (Apple Silicon)
│
└── Lima VM: xdb           (x86_64 Ubuntu 22.04, 8–12 CPU / 20–24 GB)
    ├── Podman container: sqlserver22  (mcr.microsoft.com/mssql/server:2022-latest)
    ├── Podman container: db2ce        (icr.io/db2_community/db2)
    ├── Podman container: oracle-xe    (gvenzl/oracle-xe:21-slim)
    └── /tmp/lima ←─────────────────── writable shared mount (host ↔ VM ↔ all containers)
```

**Pros**
- Single VM to provision, start, stop, snapshot.
- One Ubuntu to maintain and patch.
- Single QEMU hypervisor overhead instead of three.
- With a shared `/tmp/lima` mount threaded into every container, file staging for
  DB2 `ADMIN_CMD LOAD` and SQL Server `BULK INSERT` requires zero copy steps (see §File Staging).
- Simpler `limactl` commands for developers (`limactl start xdb`, not three separate commands).

**Cons**
- Blast radius: a DB2 STMM runaway eats RAM that kills the SQL Server buffer pool and Oracle SGA.
  Each engine has its own memory manager trying to claim as much RAM as possible:
  - DB2 STMM auto-grows DATABASE_MEMORY unless hard-capped.
  - SQL Server buffer pool grows to fill available RAM unless `memorylimitmb` is set.
  - Oracle SGA is hard-capped at 2 GB by XE edition, but PGA is not.
  Memory cap config works per-container but requires careful per-container tuning.
- APFS snapshots capture the entire VM disk — one snapshot includes all three databases.
  Restoring SQL Server to `state0` also resets DB2 and Oracle.  The per-engine
  snapshot isolation that makes bench runs reproducible is lost.
- SQL Server `mcr.microsoft.com/mssql/server` container requires the same `--privileged`
  or specific capabilities as DB2, making the compose stack heavier.
- The SA password for SQL Server is generated at first container start, not first VM boot.
  The `bench_baseline.sh` password-retrieval logic (reads from cloud-init log or
  `/var/opt/mssql/.sa_password`) must be adapted for the container case.
- Ubuntu 20.04 requirement for the SQL Server apt repo is no longer relevant with the
  container image (which bundles its own glibc), but this is also a non-issue once
  moving to containers.
- Single VM = single QEMU failure kills all three engines simultaneously.

---

## Option D — Three VMs, all Lima + Podman (uniform stack)

Containerise SQL Server exactly as DB2 and Oracle already are — `mcr.microsoft.com/mssql/server:2022-latest`
inside its own Lima VM — while keeping each engine in its own VM for independent snapshots.

```
macOS host (Apple Silicon)
│
├── Lima VM: sqlserver22   (x86_64 Ubuntu 22.04, 4 CPU / 8 GB)
│   └── Podman container: sqlserver22  (mcr.microsoft.com/mssql/server:2022-latest)
│       /tmp/lima ←────────── writable shared mount (host ↔ VM ↔ container)
│       /opt/ssdata ←──────── persistent volume (VM-local)
│
├── Lima VM: db2           (x86_64 Ubuntu 22.04, 4 CPU / 8 GB)
│   └── Podman container: db2ce  (icr.io/db2_community/db2)
│       /tmp/lima ←────────── writable shared mount
│
└── Lima VM: oracle        (x86_64 Ubuntu 22.04, 4 CPU / 8 GB)
    └── Podman container: oracle-xe  (gvenzl/oracle-xe:21-slim)
        /tmp/lima ←────────── writable shared mount
```

### What changes when SQL Server becomes a container

| Aspect | Bare apt (current) | Podman container |
|--------|-------------------|------------------|
| Ubuntu version | 20.04 (required by apt repo) | 22.04 (same as DB2/Oracle) |
| Memory cap | `mssql.conf memorylimitmb` | `MSSQL_MEMORY_LIMIT_MB` env var |
| Port config | `mssql.conf tcpport` | `MSSQL_TCP_PORT` env var |
| SA password | `pwgen` random, stored in `.sa_password` | Fixed known value (`MSSQL_SA_PASSWORD`) |
| Password retrieval | `limactl shell sqlserver22 -- sudo cat /var/opt/mssql/.sa_password` | Hardcoded — no retrieval needed |
| sqlcmd location | `/opt/mssql-tools18/bin/sqlcmd` in VM | `podman exec sqlserver22 /opt/mssql-tools18/bin/sqlcmd` |
| Systemd unit | `mssql-server.service` (native) | `container-sqlserver22.service` (generated by `podman generate systemd`) |
| BULK INSERT staging | `/tmp/lima` visible in VM directly | `/tmp/lima` needs `-v /tmp/lima:/tmp/lima` mount into container |
| Privileged mode | Not needed | Not needed (unlike DB2) |
| Version pinning | apt repo (`mssql-server-2022`) | Image tag (`2022-CU14-ubuntu-22.04`) |

### What is actually lost

Nothing architecturally significant. The only real change is the SA password:
the current code generates a random password at first boot and reads it back at
runtime. With a container, `MSSQL_SA_PASSWORD` is set at launch time, so the
password is known in advance. This **simplifies** `bench_baseline.sh` — the
`dsn_sqlserver()` password-retrieval logic in `_common.sh` (two fallback paths through
`cloud-init` log and `.sa_password` file) collapses to a single env var read.

SQL Server does have password complexity requirements (≥ 8 chars, upper + lower +
digit + symbol), so the fixed password must satisfy them: e.g. `Bench1pass!`.

### What is gained

- **Uniform operational model** across all three engines: same provision pattern,
  same systemd unit style, same `podman logs` / `podman exec` debugging workflow.
- **Ubuntu 22.04 everywhere** — one OS version to track for security updates.
- **Image pinning**: lock to `2022-CU14-ubuntu-22.04` in the YAML for reproducible
  provisioning; SQL Server version is now captured in `run_env.yaml` just like DB2/Oracle.
- **`/tmp/lima` staging into container**: once `-v /tmp/lima:/tmp/lima` is added,
  `BULK INSERT` staging works the same zero-hop way as the proposed DB2 fix.
- **No apt repo maintenance**: the SQL Server Microsoft apt repo has historically
  required Ubuntu version matching; the container image bundles its own glibc.

**Pros**
- Full stack uniformity: every x86_64 engine is Lima + Ubuntu 22.04 + Podman.
- Independent APFS snapshots still preserved per engine (three separate VMs unchanged).
- SA password handling becomes simpler, not more complex.
- Sets up Option C (VM consolidation) as a future step with no additional friction —
  all three containers are already written the same way.

**Cons**
- `bench_baseline.sh` SA password code needs one edit (remove the two fallback paths,
  read from a known constant or `.env`).
- `limactl shell sqlserver22 -- sqlcmd ...` calls in scripts must become
  `limactl shell sqlserver22 -- podman exec sqlserver22 sqlcmd ...`.
- One extra Podman process per VM (negligible: ~10 MB RSS).

---

## Option C — Two VMs (SQL Server alone; DB2 + Oracle consolidated)

DB2 and Oracle are already both Lima + Podman containers running Ubuntu 22.04.
Merge them into one VM, keep SQL Server alone.

```
macOS host (Apple Silicon)
│
├── Lima VM: sqlserver22   (x86_64 Ubuntu 20.04, 4 CPU / 8 GB)   ← unchanged
│   └── mssql-server (bare apt install)
│
└── Lima VM: xdb           (x86_64 Ubuntu 22.04, 4 CPU / 12 GB)
    ├── Podman container: db2ce       (icr.io/db2_community/db2)
    ├── Podman container: oracle-xe   (gvenzl/oracle-xe:21-slim)
    └── /tmp/lima ←────────────────── writable shared mount (host ↔ VM ↔ both containers)
```

**Pros**
- Reduces QEMU overhead from 3 VMs to 2.
- DB2 and Oracle share the same Ubuntu 22.04 base, same Podman version — no divergence.
- APFS snapshots still isolate SQL Server from DB2+Oracle.  DB2 and Oracle share snapshots,
  which is acceptable because their test data is independent and the combined disk is smaller
  than separate 40 GB + 100 GB images.
- `/tmp/lima` shared mount into both containers immediately available (see §File Staging).
- SQL Server stays bare-metal in its VM — the current `BULK INSERT` path continues to work
  with the host-path `/tmp/lima` already mounted into the VM.
- No changes to `bench_baseline.sh`, `_common.sh`, or the SA password retrieval logic.
- Preserves the Ubuntu 20.04 VM for SQL Server if that proves necessary (SQL Server 2022
  container image is Ubuntu 22.04-based internally, so the apt-install path still works
  on Ubuntu 20.04).

**Cons**
- DB2 and Oracle share RAM in one VM: a DB2 STMM spike can starve Oracle.
  Mitigation: `--memory` Podman flags per-container (works reliably for OOM protection).
- Snapshots for DB2 and Oracle are not independent — restoring one resets the other.
  In practice this is fine because `bench_baseline.sh` already restores both to the
  same `state1` before a full matrix run.
- Still requires two separate `limactl start` calls.

---

## File Staging Simplification (applies to Options B and C)

### Current DB2 staging path (3 hops)

```
1. Python (host)  → tempfile in /tmp                    mkstemp()
2. Host → Lima VM  limactl copy /tmp/X.del db2:/tmp/X.del
3. Lima VM → container  limactl shell db2 -- podman cp /tmp/X.del db2ce:/tmp/X.del
4. DB2 reads /tmp/X.del via  CALL SYSPROC.ADMIN_CMD('LOAD FROM /tmp/X.del …')
```

This is implemented in `_db2_container_copy()` with `container_spec = "lima:db2:db2ce"`.

### Proposed staging path with shared volume (0 hops)

Mount `/tmp/lima` into each container when starting it:

```yaml
# In the podman run command for db2ce:
-v /tmp/lima:/tmp/lima

# In the podman run command for oracle-xe (if sqlldr staging is added):
-v /tmp/lima:/tmp/lima
```

Then in `bulk_load_db2()`, write the staging file directly to `/tmp/lima/`:

```python
fd, host_path = tempfile.mkstemp(
    prefix=f"statschema_{base}_", suffix=".del",
    dir="/tmp/lima",          # ← shared with VM and container
)
# host_path is immediately visible at the same path inside db2ce
admin_cmd_path = host_path    # no copy step needed
```

The `_db2_container_copy()` function and `DB2_CONTAINER_NAME` env var become
unnecessary for the Lima topology.  A new topology value `lima_shared` (or simply
checking that `staging_dir` points into `/tmp/lima`) would select this fast path
in `DeploymentContext`.

### SQL Server staging (already works via shared mount)

SQL Server `BULK INSERT` reads a file path relative to the **server** filesystem.
Since `/tmp/lima` is mounted into the Lima VM (but SQL Server is a bare apt install,
not a container), writing to `/tmp/lima` on the host already makes the file visible
inside the VM at `/tmp/lima`.  No further mount is needed.  Option C preserves this
without change.

### Oracle staging (not currently needed)

`OracleDirectPathLoader` uses `conn.direct_path_load()`, a client-side wire-protocol
API.  The server never reads a file.  If `sqlldr` or external tables are added later,
the same `/tmp/lima` mount trick applies.

---

## Snapshot Technology Comparison

Three mechanisms exist for capturing database state in this stack.  They operate at
different layers and have very different trade-offs.

### How each one works

**APFS `cp -c` — filesystem layer (current approach)**
```bash
# Capture (VM must be stopped — QEMU process dead)
limactl stop sqlserver22
cp -c ~/.lima/sqlserver22/diffdisk  ~/.lima/sqlserver22/diffdisk.snap-state1   # <0.2s

# Restore
cp -c ~/.lima/sqlserver22/diffdisk.snap-state1  ~/.lima/sqlserver22/diffdisk   # <0.01s
limactl start sqlserver22                                                        # ~4-5 min boot
```
The snapshot is a plain macOS reflink (clonefile) — a separate file that shares
filesystem blocks with the original until they diverge.  QEMU and Podman are not
involved.  Restore requires a full Linux + database boot.

**`limactl snapshot` — VM/QEMU layer (experimental)**
```bash
# Capture (VM must be running — talks to the live QEMU monitor)
limactl snapshot create sqlserver22 --tag state1
# → QEMU pauses VM, serialises RAM + CPU registers + disk into the diffdisk qcow2 file

# Restore
limactl snapshot apply sqlserver22 --tag state1
# → QEMU loadvm: RAM + CPU + disk replayed → VM running in seconds, no Linux boot
```
The snapshot is stored **inside** the qcow2 diffdisk file as a named internal record.
Multiple named snapshots accumulate inside the same file.  Restore is near-instant
because the entire RAM image is replayed — the DB is already running when loadvm finishes.

**`podman checkpoint/restore` — container/CRIU layer**
```bash
# Inside the Lima VM — requires CRIU installed and root access
# Capture (container can keep running with --leave-running)
podman container checkpoint db2ce --export=/tmp/lima/db2-state1.tar.zst

# Restore
podman container restore --import=/tmp/lima/db2-state1.tar.zst
# → CRIU replays process memory + open FDs + network state → DB running in seconds
```
CRIU (Checkpoint/Restore in Userspace) serialises the **container process tree**
to a tar archive.  This is container-granular: checkpointing DB2 does not touch Oracle
in the same VM.  The archive can be copied over `/tmp/lima` to the host for safekeeping.

**`podman commit` — image layer (data-bake approach)**
```bash
# Best practice: stop the DB process cleanly inside the container first,
# so data files are at a clean checkpoint and restore needs no recovery.
podman exec db2ce su - db2inst1 -c "db2stop"          # DB2
# podman exec oracle-xe sqlplus / as sysdba <<< "shutdown immediate"  # Oracle

podman commit --pause db2ce  db2-state1-image:latest

# Restore
podman rm -f db2ce
podman run -d --name db2ce ... db2-state1-image:latest
# → DB starts fresh (full startup sequence), but from a clean checkpoint — no recovery
```
`podman commit` bakes the current container filesystem into a new image layer.
It captures **disk state only** (no process memory), so restore means starting the DB
process from scratch.  DB recovery on restore depends on whether the database was
shut down cleanly before the commit: clean shutdown → no recovery; DB still running
at commit time → crash recovery on next start (same rule as APFS `cp -c`).
Useful for distributing a pre-loaded image to other developers via a registry.

---

### Side-by-side comparison

| Property | APFS `cp -c` | `limactl snapshot` | `podman checkpoint` | `podman commit` |
|---|---|---|---|---|
| Layer | Filesystem | VM / QEMU | Container / CRIU | Container image |
| Target state required | VM **stopped** | VM **running** | Container **running** | Container running |
| What is captured | Disk only | Disk + RAM + CPU | Process RAM + FDs | Disk only |
| Restore speed | 4-5 min (boot) | **Seconds** (loadvm) | **Seconds** (CRIU) | Minutes (DB start) |
| Granularity | Whole VM | Whole VM | Per-container | Per-container |
| Snapshot format | Separate file | Embedded in qcow2 | Tar archive (portable) | OCI image layers |
| Storage overhead | **~Zero** (APFS reflinks) | ~RAM/snapshot (+8 GiB each) | ~RAM + dirty pages | Full image delta |
| Platform | macOS APFS only | Any Lima/QEMU host | Any Linux with CRIU | Any Podman host |
| Stability | Rock-solid | **Experimental** (Lima 1.0.6) | Stable, requires CRIU ≥ 3.11 | Stable |
| Rootless containers | Yes | Yes | **No** — requires root | Yes |
| DB2 `--privileged` | Compatible | Compatible | **Problematic** (CRIU + privileged) | Compatible |
| DB recovery on restore | None if stopped cleanly¹ | None (RAM replayed) | None (RAM replayed) | None if stopped cleanly¹ |
| Requires extra software | Nothing | Nothing | `apt install criu` inside VM | Nothing |
| Cross-machine portability | Copy file | `qemu-img` to extract | Copy tar archive | Push to registry |

¹ Recovery depends on database state at capture time, not the mechanism.  Stop the DB
process cleanly inside the container/VM before snapping (`db2stop`, `shutdown immediate`,
`net stop mssql-server`) and restore needs no recovery.  Capture with the DB still
running → crash recovery on next start.  This rule applies identically to APFS `cp -c`
and `podman commit` since both capture disk state only.

---

### What each approach is best for

| Use case | Best mechanism |
|---|---|
| state0/state1/state2 on any platform (macOS, Linux, WSL) | **`podman commit`** — primary; works everywhere, fastest container restore |
| VM-level recovery (corrupt OS, broken Podman daemon) | **APFS `cp -c`** — only option; `podman commit` cannot fix the VM |
| Zero-cost local snapshot archive (macOS only) | **APFS `cp -c`** — reflinks cost nothing; committed images cost GBs |
| Instant restore (seconds, not minutes) | **`limactl snapshot`** once stable; **`podman checkpoint`** for Oracle |
| Share pre-loaded DB state with a teammate / CI | **`podman commit`** → push to registry |
| MySQL / PostgreSQL / CockroachDB state management | **`podman commit`** — currently no snapshot mechanism exists for these |
| SQL Server (bare apt, pre-Option D) | **APFS `cp -c`** — `podman commit` does not apply |

---

### Cross-platform state workflow with `podman commit`

APFS `cp -c` only works on macOS.  MySQL, PostgreSQL, and CockroachDB run as host
Podman containers today with no snapshot mechanism at all — their state is reset by
deleting and recreating the container.  `podman commit` is the only approach that:

- Works identically on macOS, Linux, and WSL2
- Covers every engine: host Podman containers (PG, MySQL, CockroachDB) and
  QEMU-hosted containers (DB2, Oracle, SQL Server after Option D)
- Produces portable, registry-pushable images

**Unified procedure for all engines:**

```bash
# ── Build state0 (clean install, no test data) ────────────────────────────────
# Stop DB cleanly inside the container (no crash recovery on restore)
podman exec pg18   psql -U postgres -c "checkpoint"     # PostgreSQL: flush + clean
podman exec mysql8 mysqladmin -uroot -ptestpass shutdown # MySQL: clean stop, restart
podman exec crdb-single /cockroach/cockroach quit --insecure  # CockroachDB

# For QEMU-hosted containers, run these inside the Lima VM:
#   limactl shell db2 -- podman exec db2ce su - db2inst1 -c "db2stop"
#   limactl shell oracle -- podman exec oracle-xe sqlplus / as sysdba <<< "shutdown immediate"

podman commit pg18    statschema/pg18:state0
podman commit mysql8  statschema/mysql8:state0
# etc. for every engine

# ── Build state1 (after identity test data loaded) ───────────────────────────
# Run identity test data load, then stop DB cleanly and commit:
podman commit pg18    statschema/pg18:state1
podman commit mysql8  statschema/mysql8:state1

# ── Restore to state1 ─────────────────────────────────────────────────────────
podman rm -f pg18
podman run -d --name pg18 ... statschema/pg18:state1
# DB starts from clean state1 image — no recovery, just normal startup
```

**Storage cost** is the main trade-off: each committed image stores the full filesystem
delta over the base image.  For a database with significant test data (TPC-H SF=1 is
~1 GB), three states × six engines = potentially tens of GB of images.  This is
manageable locally and on a registry, but noticeably larger than the near-zero APFS
reflink overhead.

**Restore speed** is the other trade-off: the DB process starts fresh from the image,
so restore includes a full DB startup sequence (seconds for PG/MySQL, minutes for
DB2/Oracle).  APFS + QEMU boot is also minutes, so the net difference for QEMU engines
is small.  For native Podman engines (PG, MySQL), `podman commit` restore is actually
*faster* than the current "delete + recreate + re-initialize" pattern.

### How `podman commit` and APFS relate

They are **not alternatives for the same thing** — they operate at different
granularity levels and serve different concerns.

`podman commit` restores the **container filesystem** without restarting the VM.
APFS restores the **entire VM disk** (Linux OS + Podman daemon + image cache +
container data), which requires a full VM boot.

For containerised engines (DB2, Oracle, SQL Server after Option D),
`podman commit` restore is actually **faster** than APFS restore:

```
podman commit restore:  rm container + run from image  →  DB ready in ~1-2 min
APFS restore:           stop VM + swap disk + boot VM  →  DB ready in ~4-5 min
```

APFS is not an acceleration layer on top of `podman commit`.  Its distinct value is:

1. **VM-level recovery**: if the Lima VM's Linux OS, systemd, or Podman storage
   layer becomes corrupt (not just container data), only APFS can fix it.
   `podman commit` cannot repair a broken VM.
2. **Zero storage cost**: a committed DB image carrying state1 data can be several GB;
   three states × three engines = significant disk / registry usage.  APFS reflinks
   cost nothing because they share filesystem blocks with the original.
3. **SQL Server (bare apt, pre-Option D)**: `podman commit` does not apply; APFS is
   the only state mechanism available today.

```
Primary (all platforms):  podman commit → state0/state1/state2 images, fastest restore
Complementary (macOS):    APFS cp -c    → VM-level safety net, zero storage cost
Future:                   limactl snapshot  → instant VM restore (once stable)
                          podman checkpoint → instant container restore (Oracle, CRIU)
```

### Current status

`podman commit` is available today on all platforms with no additional dependencies.
APFS `cp -c` is in use now for QEMU VMs on macOS.  Neither CRIU nor stable Lima
snapshots are available yet.  MySQL and PostgreSQL have no state snapshot today.

Migration priority:
1. Add `podman commit` state0/state1/state2 for PG, MySQL, CockroachDB — fills a gap
   that currently does not exist.
2. Once Option D is complete, add `podman commit` for DB2, Oracle, SQL Server containers.
3. `bench_baseline.sh` grows a `--mode=commit` and `--mode=restore-image` that replaces
   container delete/run with commit/restore across all engines uniformly.

---

## Resource Budget

| Layout | VMs | Approx RAM reserved | Approx disk |
|--------|-----|---------------------|-------------|
| Option A (current) | 3 | 24 GiB | 244 GiB |
| Option D (uniform stack, 3 VMs) | 3 | 24 GiB | 244 GiB |
| Option C (two) | 2 | 8 + 12 = 20 GiB | ~140 GiB |
| Option B (single) | 1 | 20–24 GiB (same engines) | ~100 GiB |

RAM savings come from eliminating duplicate Linux kernel+hypervisor overhead
(~1–2 GiB per extra VM), not from the databases themselves.

---

## APFS Snapshot Impact

| Layout | Snapshot granularity | Restore scope |
|--------|----------------------|---------------|
| Option A | Per-engine | Restore sqlserver → only sqlserver VM |
| Option D (uniform stack) | Per-engine (unchanged) | Same as Option A |
| Option C | sqlserver alone + db2+oracle together | Restore sqlserver alone; or reset db2+oracle together |
| Option B | Single VM | Restore any engine → resets all three |

Options A and D have identical snapshot isolation — containerising SQL Server does not
affect the VM boundary.  Option C is still a reasonable second step once D is in place.

---

## Recommendation

**Option D + `podman commit` state management**, then Option C:

### Phase 1 — Uniform stack (Option D) + cross-platform state workflow

Containerise SQL Server inside its own Lima VM, matching DB2 and Oracle exactly.
Simultaneously adopt `podman commit` as the primary state0/state1/state2 mechanism
for all engines — QEMU-hosted and native Podman alike.

**One-time APFS safety snapshot per VM (macOS only)**

After provisioning each Lima VM with its empty containers running and confirmed
healthy, take a single APFS snapshot as a VM-level safety net:

```bash
limactl stop sqlserver22 && limactl stop db2 && limactl stop oracle

cp -c ~/.lima/sqlserver22/diffdisk  ~/.lima/sqlserver22/diffdisk.snap-empty
cp -c ~/.lima/db2/diffdisk          ~/.lima/db2/diffdisk.snap-empty
cp -c ~/.lima/oracle/diffdisk       ~/.lima/oracle/diffdisk.snap-empty

limactl start sqlserver22 && limactl start db2 && limactl start oracle
```

This `empty` snapshot is taken **once** and rarely touched.  Its purpose is VM-level
recovery: if Lima, QEMU, or the Podman daemon itself becomes corrupt, restore the
`empty` diffdisk and re-run `podman run` from the committed images rather than
reprovisioning from scratch (which takes 20-30 min for DB2).

**State0/state1/state2 via `podman commit` (all platforms)**

All state transitions are managed at the container layer, not the VM layer:

```
After provisioning (containers empty, no test data):
  → podman commit  →  statschema/sqlserver22:state0
  → podman commit  →  statschema/db2ce:state0
  → podman commit  →  statschema/oracle-xe:state0
  → podman commit  →  statschema/pg18:state0        ← first time PG has state mgmt
  → podman commit  →  statschema/mysql8:state0       ← first time MySQL has state mgmt

After identity test data loaded (clean DB stop before commit):
  → podman commit  →  statschema/*:state1

After app schemas loaded (clean DB stop before commit):
  → podman commit  →  statschema/*:state2

Restore to state1 (VM stays running, ~1-2 min vs ~4-5 min APFS + boot):
  podman rm -f db2ce
  podman run -d --name db2ce ... statschema/db2ce:state1
```

This is faster than APFS restore for containerised engines because the VM never
restarts.  It works identically on macOS, Linux, and WSL2.  MySQL and PostgreSQL
get state management for the first time.

**Concrete changes for Phase 1:**

1. Update `config/lima/sqlserver22.yaml` — Ubuntu 22.04, `podman run mcr.microsoft.com/mssql/server:2022-latest` with `-v /tmp/lima:/tmp/lima`, fixed SA password, memory cap via env var.
2. Add `-v /tmp/lima:/tmp/lima` to `db2ce` and `oracle-xe` in their YAMLs.
3. Simplify `benchmarks/_common.sh` `dsn_sqlserver()` — fixed password, no cloud-init fallback.
4. Update `bulk_load_db2()` to write staging files to `/tmp/lima/` directly.
5. Add `bench_baseline.sh --mode=commit-state` and `--mode=restore-image` to replace the current `snap`/`restore` modes with `podman commit`/`podman rm + run` across all engines.
6. Keep `--mode=snap` / `--mode=restore` for the one-time `empty` APFS snapshot and emergency VM recovery.

### Phase 2 — VM consolidation (Option C, optional)

Once Phase 1 is complete, consolidating DB2 and Oracle into a single VM (`xdb`) is a
YAML edit.  All containers and their committed images transfer unchanged.  SQL Server
stays in its own VM.

**Option B** (single VM) only if a developer has ≤ 16 GiB RAM and cannot run three
QEMU VMs simultaneously.

---

## Implementation Steps

### Phase 1: Containerise SQL Server + adopt `podman commit` state workflow (Option D)

1. **`config/lima/sqlserver22.yaml`**
   - Change `images:` to Ubuntu 22.04.
   - Remove apt install of `mssql-server` and `mssql-tools18`.
   - Add Podman install + `podman run` block:
     ```bash
     podman run -d \
       --name sqlserver22 \
       -p 14330:14330 \
       -e ACCEPT_EULA=Y \
       -e MSSQL_SA_PASSWORD=Bench1pass! \
       -e MSSQL_TCP_PORT=14330 \
       -e MSSQL_MEMORY_LIMIT_MB=5632 \
       -v /opt/ssdata:/var/opt/mssql \
       -v /tmp/lima:/tmp/lima \
       mcr.microsoft.com/mssql/server:2022-latest
     ```
   - Generate systemd unit (`podman generate systemd`) for auto-restart.

2. **`benchmarks/_common.sh` — `dsn_sqlserver()`**
   - Remove cloud-init log fallback and `.sa_password` file read.
   - SA password is `Bench1pass!` (fixed, meets SQL Server complexity rules).
   - `limactl shell sqlserver22 -- sqlcmd ...` → `limactl shell sqlserver22 -- podman exec sqlserver22 /opt/mssql-tools18/bin/sqlcmd ...`

3. **`config/lima/db2.yaml`** — add `-v /tmp/lima:/tmp/lima` to `podman run`.

4. **`config/lima/oracle.yaml`** — add `-v /tmp/lima:/tmp/lima` to `podman run`.

5. **`src/statschema/dialects/db2/loader.py`**
   - Default `staging_dir` to `/tmp/lima`; set `admin_cmd_path = host_path` (skip `_db2_container_copy()`).
   - Add `lima_shared` topology to `DeploymentContext`.

6. **`benchmarks/bench_baseline.sh`** — add `podman commit`-based state management:
   - `--mode=commit-state --tag=state1` — stop DB cleanly inside each container, commit, restart.
   - `--mode=restore-image --tag=state1` — `podman rm -f + podman run` from committed image.
   - `--mode=snap --tag=empty` — one-time APFS snapshot of empty provisioned VM (safety net).
   - Keep existing `--mode=restore` for emergency VM-level recovery from `empty` APFS snap.

7. **`docs/local-databases.md`** — update SQL Server row; document `podman commit` state workflow
   as the cross-platform replacement for APFS snap/restore for state management.

### Phase 2: Consolidate DB2 + Oracle (Option C, future)

8. **New `config/lima/xdb.yaml`**
   - Ubuntu 22.04, 4 CPU, 12 GiB RAM.
   - Provision both `db2ce` and `oracle-xe` containers with `-v /tmp/lima:/tmp/lima`.
   - Port forwards: 50000, 1521, 5500.

9. **`benchmarks/_common.sh`** — update `start_db2()` / `start_oracle()` to reference `lima_vm="xdb"`.

10. **`benchmarks/bench_baseline.sh`** — `--mode=snap --tag=empty` targets `xdb` for the combined VM.

11. **Test**
    - Provision from scratch; take `empty` APFS snap for each VM.
    - Build state0/state1/state2 via `podman commit` for all six engines.
    - Run identity sweep; verify restore from committed image gives a clean run.
    - Verify DB2 staging file appears at `/tmp/lima/statschema_*.del` inside `db2ce` without `limactl copy`.
