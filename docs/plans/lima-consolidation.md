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

## Options (A → D → C → B, decreasing VM count)

Options are ordered from least change (A) to most consolidated (B).
The recommended migration path is A → D → C.

---

## Option A — Status Quo (keep three separate VMs)

No changes.

**Pros**
- Blast radius is isolated: a DB2 STMM runaway or SQL Server OOM cannot kill Oracle or SQL Server.
- Independent per-engine VM disks — APFS safety snap and `podman commit` state images are isolated per engine.
- Different Ubuntu versions per engine (SQL Server needs Ubuntu 20.04; DB2/Oracle prefer 22.04).
- VM-level resource caps (`memory:`, `cpus:`) are per-engine and easy to tune independently.
- `limactl stop --force sqlserver22` in 65 ms — stops only the engine under test, leaves others warm.

**Cons**
- 3 × QEMU process overhead (~100–200 MB host RAM per idle VM kernel+hypervisor).
- 3 × Ubuntu images to update and maintain.
- APFS snapshot disk space: three separate `diffdisk` files (44 GB + 100 GB + 100 GB).
- Each new developer must provision three VMs on first setup.

---

## Option B — Single shared Lima VM (all three as Podman containers, 1 VM)

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
- APFS safety snap covers the entire VM — restoring the VM resets all three databases.
  With `podman commit` as primary state management this matters less, but it means the
  VM-level safety net is not per-engine.
- SQL Server `mcr.microsoft.com/mssql/server` does not require `--privileged` (unlike DB2),
  but SA password handling and sqlcmd access change from the bare-apt pattern.
- Single VM = single QEMU failure kills all three engines simultaneously.

---

## Option D — Three VMs, all Lima + Podman (uniform stack, 3 VMs)

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

## Option C — Two VMs (SQL Server alone; DB2 + Oracle consolidated, 2 VMs)

In the recommended migration path Option C follows Option D, so SQL Server is already
a Podman container on Ubuntu 22.04.  This step merges DB2 and Oracle into one VM.

```
macOS host (Apple Silicon)
│
├── Lima VM: sqlserver22   (x86_64 Ubuntu 22.04, 4 CPU / 8 GB)   ← from Option D
│   └── Podman container: sqlserver22  (mcr.microsoft.com/mssql/server:2022-latest)
│       /tmp/lima ←────────── writable shared mount (host ↔ VM ↔ container)
│
└── Lima VM: xdb           (x86_64 Ubuntu 22.04, 4 CPU / 12 GB)
    ├── Podman container: db2ce       (icr.io/db2_community/db2)
    ├── Podman container: oracle-xe   (gvenzl/oracle-xe:21-slim)
    └── /tmp/lima ←────────────────── writable shared mount (host ↔ VM ↔ both containers)
```

**Pros**
- Reduces QEMU overhead from 3 VMs to 2.
- DB2 and Oracle share the same Ubuntu 22.04 base, same Podman version — no divergence.
- SQL Server VM-level safety snap and `podman commit` images remain independent from DB2+Oracle.
- `/tmp/lima` shared mount into all containers already in place from Option D.
- `bench_baseline.sh`, `_common.sh`, and `dsn_sqlserver()` already updated in Option D — no further changes needed here.

**Cons**
- DB2 and Oracle share RAM in one VM: a DB2 STMM spike can starve Oracle.
  Mitigation: `--memory` Podman flags per-container (works reliably for OOM protection).
- `podman commit` images for DB2 and Oracle remain independent (per-container), but
  the VM-level APFS safety snap covers both — restoring the VM resets both containers.
  In practice this is fine because `bench_baseline.sh` restores from committed images,
  not from the VM-level snap.
- Still requires two separate `limactl start` calls.

---

## File Staging Simplification (applies to Options B, C, and D)

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

The `_db2_container_copy()` function and container-copy path have been removed.
`topology="shared_fs"` is selected when `STATSCHEMA_SERVER_STAGING_DIR` is set.
For the Lima dev setup both sides see `/tmp/lima/statschema` via virtfs, so no
path translation is needed (`client_staging_dir == server_staging_dir`).

### SQL Server staging

In the current bare-apt setup (Option A), `/tmp/lima` is already mounted into the VM,
so `BULK INSERT` can read files written by the host with no extra steps.

After Option D (SQL Server containerised), the container needs `-v /tmp/lima:/tmp/lima`
exactly as DB2 and Oracle do — this is item 1 in the concrete changes list and is
handled identically to the DB2 case above.

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

Two valid Phase 1 paths exist depending on whether macOS-only tooling is acceptable.

---

### Local registry

A `registry:2` Podman container runs on the macOS host at `localhost:5000`, backed by a
named Podman volume (`local-registry-data`) that persists across host reboots.  Lima VMs
reach it via `host.lima.internal:5000`.

**Data flow (seed-registry path):**

```
internet (mcr.microsoft.com / icr.io / docker.io)
    │  podman pull  (on macOS host)
    ▼
host Podman store  (gvenzl/oracle-xe:21-slim, etc.)
    │  podman push --tls-verify=false localhost:5000/...
    ▼
local-registry container  (localhost:5000, volume: local-registry-data)
    │  host.lima.internal:5000  (Lima VM's provision script)
    ▼
Lima VM Podman store  →  running container
```

Each Lima VM's provision script pulls exclusively from `host.lima.internal:5000`.  If the
image is not in the registry the provision script exits with an error and instructs the user
to run `seed-registry` first.  There is no fallback to the upstream registry — a missing
image is always a signal that the registry was not seeded, not a reason to pull 3 GB from
the internet silently.  A `limactl delete` + `limactl start` therefore pulls from localhost
(DB2 ≈ 3 GB, SQL Server ≈ 1.5 GB, Oracle ≈ 800 MB) as long as the registry is warm.

`commit-state` pushes the committed image to the local registry automatically, so state
images (`statschema/<engine>:state0/1/2`) also survive VM deletion.

**Two ways to seed the registry:**

| Mode | When to use |
|---|---|
| `seed-registry --engine=<vm>` | Before first provision, or after `limactl delete` with no VM running.  Pulls base image from internet onto the host, pushes to local registry, keeps host copy for fast re-seeding. |
| `push-images --engine=<vm>` | After first provision when a VM is already running.  Pushes images directly from the VM to the registry (skips the host Podman store). |

```bash
bench_baseline.sh --mode=ensure-registry   # start registry if not running
bench_baseline.sh --mode=list-images       # show registry contents
curl -sf http://localhost:5000/v2/_catalog # direct registry API
```

---

### Phase 1-A — Uniform stack + `podman commit` + APFS safety net (macOS)

Containerise SQL Server to match DB2 and Oracle.  Use `podman commit` for all
state0/state1/state2 transitions.  Take one APFS snapshot per VM after initial
provisioning as a VM-level safety net — taken once, rarely touched.

```
Provisioning (one-time, macOS):
  bench_baseline.sh --mode=ensure-registry              ← start localhost:5000 if not running
  bench_baseline.sh --mode=seed-registry --engine=all  ← pull base images on host, push to registry
  limactl start all VMs  → provision pulls from host.lima.internal:5000, not the internet
  limactl stop all VMs
  cp -c diffdisk  diffdisk.snap-empty   ← VM safety net (3 VMs, <1s each)
  limactl start all VMs

State management (daily, all platforms via podman commit):
  stop DB cleanly inside container → podman commit → statschema/<engine>:state0/1/2
  (commit-state auto-pushes committed image to local registry)
  restore: podman rm -f <ctr>  +  podman run statschema/<engine>:state1

VM recovery (macOS, if Lima/QEMU/Podman itself is corrupt):
  cp -c diffdisk.snap-empty  diffdisk  → limactl start
  (provision script pulls base image from registry — no internet pull)
  podman run statschema/<engine>:state0  ← start from last committed state image

VM rebuild from scratch (limactl delete + limactl start):
  (provision script pulls from registry automatically; no manual load step)
  bench_baseline.sh --mode=commit-state --tag=state0   ← rebuild state images if missing from registry
```

The APFS `empty` snap is not part of the daily workflow — it exists so a corrupt VM
can be reset in seconds rather than re-provisioned from scratch (20-30 min for DB2).
All actual state cycling goes through `podman commit`.

**Concrete changes:**

1. `config/lima/sqlserver22.yaml` — Ubuntu 22.04, `podman run mcr.microsoft.com/mssql/server:2022-latest` with `-v /tmp/lima:/tmp/lima`, fixed SA password, memory cap via env var.
2. Add insecure registry conf (`host.lima.internal:5000`) and local-pull-with-fallback block to all three Lima YAMLs (`sqlserver22.yaml`, `db2.yaml`, `oracle.yaml`).
3. Add `-v /tmp/lima:/tmp/lima` to `db2ce` and `oracle-xe` in their YAMLs.
4. Simplify `benchmarks/_common.sh` `dsn_sqlserver()` — fixed password, no cloud-init fallback.
5. Update `bulk_load_db2()` to write staging files to `/tmp/lima/` directly.
6. `benchmarks/bench_baseline.sh` — add `podman commit`-based state management and registry modes:
   - `--mode=ensure-registry` — start `localhost:5000` Podman container if not running.
   - `--mode=seed-registry --engine=<vm>` — pull base image on host, push to registry, keep host copy.
   - `--mode=push-images --engine=<vm>` — push images from a running VM to registry.
   - `--mode=commit-state --tag=state1` — stop DB cleanly, commit, push to registry, restart.
   - `--mode=restore-image --tag=state1` — `podman rm -f + podman run` from committed image.
7. Keep `--mode=snap --tag=empty` / `--mode=restore --tag=empty` for VM-level recovery only (macOS).

---

### Phase 1-B — Uniform stack + `podman commit` only (all platforms, no APFS)

Identical to Phase 1-A except no APFS safety snapshot is taken.  Recovery from a
corrupt VM means re-provisioning from the Lima YAML; the provision script pulls base
images from the local registry (no internet pull), and state images are rebuilt by
re-loading test data and committing.  On a machine without APFS this is the only
recovery path.

```
Provisioning (one-time, any platform):
  bench_baseline.sh --mode=ensure-registry              ← start localhost:5000 if not running
  bench_baseline.sh --mode=seed-registry --engine=all  ← pull base images on host, push to registry
  limactl start all VMs  → provision pulls from host.lima.internal:5000, not the internet
  stop DB cleanly → podman commit → statschema/<engine>:state0
  (commit-state auto-pushes state0 to local registry)

State management (daily):
  stop DB cleanly → podman commit → statschema/<engine>:state1/2
  restore: podman rm -f <ctr>  +  podman run statschema/<engine>:state1

VM recovery (VM disk destroyed — limactl delete + limactl start):
  (provision script pulls from registry automatically — no manual step)
  bench_baseline.sh --mode=commit-state --tag=state0   ← rebuild state images if missing from registry
```

**Bulk load staging is also solved by the same YAML changes.** The three-way path
requires two mechanisms working together:

1. **Lima `mounts:` entry** (`location: /tmp/lima, writable: true` in each Lima YAML) —
   Lima uses virtfs/9p to expose the macOS host's `/tmp/lima` inside the Lima VM at
   the same path.  This is the macOS → VM bridge.  Without this entry, `/tmp/lima`
   inside the VM is the VM's own empty directory and macOS-written files never appear.
2. **Podman `-v /tmp/lima:/tmp/lima`** — bind-mounts the Lima VM's `/tmp/lima` (which is
   the macOS `/tmp/lima` via the Lima mount above) into the container.  This is the
   VM → container bridge.

Both entries are added together: the Lima `mounts:` block and the Podman `-v` flag are
both part of items 1–4 in the concrete changes list.  Once both are in place, DB2
`ADMIN_CMD LOAD`, SQL Server `BULK INSERT`, and any future Oracle `sqlldr` call can all
read staging files written by the Python loader on the macOS host at `/tmp/lima/` —
no `limactl copy`, no `podman cp`, no manual transfer.

**9p read performance is not the bottleneck.**  Lima's 9p mount delivers 50–150 MB/s
sequential reads.  SQL Server and DB2 running under QEMU x86_64 emulation on Apple
Silicon are CPU-bound at 5–15× emulation overhead; their effective CSV-parse-and-ingest
rate is roughly 10–40 MB/s — well below the 9p ceiling.  The file read sits in a kernel
buffer waiting on the database, not the other way around.  9p would only become a
bottleneck if it fell below ~10 MB/s, which does not occur for sequential reads of
staging files in normal operation.

**Concrete changes:** same items 1–5 from Phase 1-A; skip item 6 (no APFS commands).

---

### Choosing between 1-A and 1-B

| | Phase 1-A (with APFS) | Phase 1-B (without APFS) |
|---|---|---|
| Platform | macOS only | macOS, Linux, WSL2 |
| VM recovery time | Seconds (APFS restore + `podman run`) | ~20-30 min (re-provision + re-commit) |
| Daily state restore | Same (`podman commit` + local registry) | Same (`podman commit` + local registry) |
| Extra tooling | `cp -c` (built into macOS) | None |
| Bulk load staging fix | Same (Lima `mounts:` + Podman `-v /tmp/lima:/tmp/lima`) | Same |

Both paths use `podman commit` for state management and are equally fast for daily
bench cycles.  The difference is VM-level recovery speed when something goes wrong.
macOS developers should use 1-A; Linux/WSL developers use 1-B.

---

### Phase 2 — VM consolidation (Option C, optional)

Once Phase 1 is complete, consolidating DB2 and Oracle into a single VM (`xdb`) is a
YAML edit.  All containers and committed images transfer unchanged.  SQL Server stays
in its own VM.

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

6. **`benchmarks/bench_baseline.sh`** — state management and image caching:
   - `--mode=commit-state --tag=state1` — stop DB cleanly, commit, push to local registry, restart.
   - `--mode=restore-image --tag=state1` — `podman rm -f + podman run` from committed image.
   - `--mode=ensure-registry` — start `localhost:5000` registry container if not running.
   - `--mode=seed-registry --engine=<vm>` — pull base image on host, push to registry (use before first provision or after `limactl delete` when no VM is running).
   - `--mode=push-images --engine=<vm>` — push all images from a running VM to the registry.
   - `--mode=snap --tag=empty` — one-time APFS snapshot of empty provisioned VM (macOS only, safety net).
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
    - Provision from scratch; on macOS (Phase 1-A), take `empty` APFS snap for each VM as a safety net.
    - Build state0/state1/state2 via `podman commit` for all six engines.
    - Run identity sweep; verify restore from committed image gives a clean run.
    - Verify DB2 staging file appears at `/tmp/lima/statschema_*.del` inside `db2ce` without `limactl copy`.
