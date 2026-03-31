# Dev Environment & CI Plan: Cross-Platform Database Setup

**Status:** In progress

Database setup differs between arm64 macOS (Lima VMs for x86\_64-only engines) and
x86\_64 Linux (Podman only, no Lima). The test code is identical on both platforms —
all connections read from environment variables. This plan adds structured setup
scripts, a live-database CI job, and a path for arm64 macOS developers to test the
x86\_64 Linux setup locally.

---

## Architecture

### Database tiers

The two tiers drive the entire script structure.

**Tier 1 — multi-arch (Podman on every platform)**

These images publish both arm64 and x86\_64 builds. They run in Podman identically
on arm64 macOS, x86\_64 Linux, Intel macOS, and GitHub Actions. No platform
branching needed.

```
postgres:18   ·   mysql:8   ·   cockroachdb/cockroach
```

**Tier 2 — x86\_64-only (no ARM64 image exists)**

These images have no ARM64 build. On an x86\_64 host they run directly in Podman.
On arm64 macOS the host cannot run them natively, so Lima provides a QEMU x86\_64
VM that can.

```
mcr.microsoft.com/mssql/server:2022-latest   (SQL Server 2022)
gvenzl/oracle-xe:21-slim                     (Oracle XE 21c)
icr.io/db2_community/db2                     (IBM DB2 CE 11.5)
```

### Scripts

`_db-common.sh` handles Tier 1. The platform scripts handle Tier 2 — differently
depending on whether the host can run x86\_64 containers natively.

```
scripts/
├── _db-common.sh          ← Tier 1 only: Podman start for PG, MySQL, CockroachDB
│                             (multi-arch images — same commands on all platforms)
│                             sourced by both platform scripts below
│
├── db-up-x86_64.sh        ← Tier 1 (via _db-common.sh)
│                           + Tier 2 directly in Podman (x86_64 host can run them)
│
├── db-up-arm64-macos.sh   ← Tier 1 (via _db-common.sh)
│                           + Tier 2 via Lima QEMU VMs (arm64 host cannot run them)
│
├── db-up.sh               ← entry point: detects arch + OS, delegates to above
├── db-down.sh             ← teardown (platform-aware)
└── test-on-linux.sh       ← arm64 macOS only: validate Tier 2 Linux path
                              via Lima devbox running db-up-x86_64.sh

.github/workflows/
├── ci.yml                 ← existing: offline tests, every push (unchanged)
└── ci-live.yml            ← new: live DB tests, calls db-up-x86_64.sh
```

### Platform routing

| Platform | arch | Tier 2 runs via | Script |
|---|---|---|---|
| Apple Silicon macOS | arm64 | Lima QEMU VMs | `db-up-arm64-macos.sh` |
| Intel macOS | x86\_64 | Podman directly | `db-up-x86_64.sh` |
| Linux (any distro) | x86\_64 | Podman directly | `db-up-x86_64.sh` |
| GitHub Actions (`ubuntu-latest`) | x86\_64 | Podman directly | `db-up-x86_64.sh` |

### No-duplication map

```
_db-common.sh           ← Tier 1 (multi-arch) written once; sourced by both platform scripts
db-up-x86_64.sh         ← used by: Linux devs, Intel Mac devs, GitHub Actions,
                           and test-on-linux.sh (arm64 Mac → Linux validation)
db-up-arm64-macos.sh    ← used by: Apple Silicon Mac devs only
ci-live.yml             ← calls db-up-x86_64.sh directly; no duplicated commands
conftest.py             ← env-var driven already; untouched
```

---

## Database mapping

### Shared Podman databases (`_db-common.sh`)

Native ARM64 images available — identical setup on all platforms.

| Container | Image | Port | Notes |
|---|---|---|---|
| `pg18` | `postgres:18` | 5418 | |
| `mysql8` | `mysql:8` | 3384 | |
| `crdb-single` | `cockroachdb/cockroach:latest` | 26257 | `--memory=2g` required |

### x86\_64-only databases

No ARM64 images. On x86\_64, run directly in Podman. On arm64 macOS, run via Lima.

| Engine | x86\_64 Podman image | arm64 macOS (Lima VM) | Port |
|---|---|---|---|
| SQL Server 2022 | `mcr.microsoft.com/mssql/server:2022-latest` | `sqlserver22` Lima VM | 14330 |
| Oracle XE 21c | `gvenzl/oracle-xe:21-slim` | `oracle` Lima VM | 1521 |
| IBM DB2 CE 11.5 | `icr.io/db2_community/db2` | `db2` Lima VM | 50000 |

---

## Tasks

### Task 1 — `scripts/_db-common.sh`

Shared Podman startup for Postgres 18, MySQL 8, CockroachDB single-node. Sourced
(not executed directly) by both `db-up-x86_64.sh` and `db-up-arm64-macos.sh`.

Defines functions: `start_postgres`, `start_mysql`, `start_crdb`.
Writes shared env vars to stdout (to be appended to `.env.local`).

**Status:** Not started

---

### Task 2 — `scripts/db-up-x86_64.sh`

Sources `_db-common.sh`, then starts SQL Server, Oracle XE, and DB2 CE directly
in Podman. Writes a `.env.local` file with all connection variables matching
`.env.example` names.

This script is the single source of truth for the x86\_64 database setup. GitHub
Actions calls it directly.

Staging directory for file-based bulk loaders (DB2, Oracle):

```
STATSCHEMA__CLIENT_STAGING_DIR=/tmp/statschema
STATSCHEMA__SERVER_STAGING_DIR=/tmp/statschema
```

Both sides use the same path because the DB server process runs on the same host
(same container network or localhost port mapping).

#### Image source by environment

`db-up-x86_64.sh` is called in two different contexts with different image sources:

| Caller | Image source for SQL Server / Oracle / DB2 |
|---|---|
| GitHub Actions (`ubuntu-latest`) | Public registries (mcr.microsoft.com, docker.io, icr.io) — no local registry |
| Lima devbox (`test-on-linux.sh`) | `host.lima.internal:5000` — local registry seeded by `bench_baseline.sh` |

The script detects which source is available. If `host.lima.internal:5000` is
reachable it pulls from there; otherwise it falls back to the public registry.
This keeps the script usable in both contexts without separate scripts.

**Status:** Not started

---

### Task 3 — `.github/workflows/ci-live.yml`

New workflow alongside the existing `ci.yml`. Calls `db-up-x86_64.sh`, waits for
databases to be ready, runs `pytest tests/ -k "live"`.

Triggers:
- Pull request to `main`
- Nightly schedule (`0 2 * * *` UTC)

`ci.yml` (offline tests, every push) is unchanged.

**Status:** Not started

---

### Task 4 — `scripts/db-up-arm64-macos.sh` + `scripts/db-up.sh`

`db-up-arm64-macos.sh`: sources `_db-common.sh`, then starts the three Lima VMs
(`sqlserver22`, `oracle`, `db2`) and the database services inside them.

`db-up.sh`: single entry point for all developers. Detects `uname -m` and
`uname -s`, delegates to the appropriate script:

```bash
arch=$(uname -m)
os=$(uname -s)

if [[ "$os" == "Darwin" && "$arch" == "arm64" ]]; then
    exec "$(dirname "$0")/db-up-arm64-macos.sh" "$@"
else
    exec "$(dirname "$0")/db-up-x86_64.sh" "$@"
fi
```

**Status:** Not started

---

### Task 5 — `scripts/db-down.sh`

Stops and removes Podman containers on all platforms. On arm64 macOS, also stops
Lima VMs. Platform detection uses the same `uname -m` + `uname -s` pattern as
`db-up.sh`.

**Status:** Not started

---

### Task 6 — `scripts/test-on-linux.sh`

arm64 macOS developers use this to validate the x86\_64 Linux path without leaving
macOS.

Creates a Lima Ubuntu devbox VM (not a database VM), mounts the workspace via
Lima virtfs, runs `db-up-x86_64.sh` inside the VM, then runs the live test suite.

```
[arm64 macOS host]
  └── Lima devbox VM (x86_64 Ubuntu)
        ├── workspace mounted at /workspace
        ├── Podman: pg18, mysql8, crdb-single
        ├── Podman: sqlserver, oracle, db2   ← pulled from local registry (see below)
        └── pytest tests/ -k "live"
```

All databases are native x86\_64 inside the VM. No Lima nesting, no emulation of
emulation.

#### Local registry integration

The existing local registry on the macOS host (port 5000, managed by
`bench_baseline.sh`) already holds the x86\_64-only images. The devbox VM
configures `host.lima.internal:5000` as an insecure registry — the same mechanism
used by the sqlserver22, oracle, and db2 Lima VMs. `db-up-x86_64.sh` inside the
devbox pulls from there when available; if the registry is not running it falls back
to the public registry (same auto-detect logic described in Task 2).

Flow when local registry is seeded:
```
macOS host (bench_baseline.sh --mode=seed-registry, run once):
  localhost:5000/mssql/server:2022-latest      ← seeded once
  localhost:5000/gvenzl/oracle-xe:21-slim      ← seeded once
  localhost:5000/db2_community/db2:latest      ← seeded once

Lima devbox VM (db-up-x86_64.sh):
  podman pull host.lima.internal:5000/mssql/server:2022-latest   ← fast, local
  podman pull host.lima.internal:5000/gvenzl/oracle-xe:21-slim   ← fast, local
  podman pull host.lima.internal:5000/db2_community/db2:latest   ← fast, local
```

**Status:** Not started

---

### Task 7 — SSL env vars for CockroachDB and SQL Server

Two engines have hardcoded SSL assumptions in `tests/live_helpers.py` and
`tests/test_live_cockroachdb.py` that block cloud database use.

**`live_helpers.py` — `connect_cockroachdb`**

`sslmode` is hardcoded to `"disable"`. CockroachDB Dedicated and Serverless require
TLS. Fix: read from `CRDB_SSLMODE` env var.

```python
# before
sslmode="disable"

# after
sslmode=os.environ.get("CRDB_SSLMODE", "disable")
```

**`test_live_cockroachdb.py`**

Contains a second independent `psycopg2.connect` call with `sslmode="disable"`.
Fix: same `CRDB_SSLMODE` env var.

**`live_helpers.py` — `connect_sqlserver`**

No TLS option passed to `pymssql.connect`. Azure SQL and RDS SQL Server with forced
encryption reject unencrypted connections. Fix: read from `SQLSERVER_ENCRYPT` env var.

```python
# add to connect_sqlserver
_encrypt = os.environ.get("SQLSERVER_ENCRYPT", "false").lower() in ("true", "1", "yes")
# pass to pymssql.connect: conn_props["ssl"] = _encrypt  (pymssql ≥ 2.2)
```

**`.env.example` additions:**

```bash
# CockroachDB SSL — set to "require" for Dedicated/Serverless
CRDB_SSLMODE=disable

# SQL Server TLS — set to "true" for Azure SQL / RDS SQL Server with forced encryption
SQLSERVER_ENCRYPT=false
```

**Status:** Not started

---

## CI trigger strategy

| Workflow | Trigger | Duration | Purpose |
|---|---|---|---|
| `ci.yml` | Every push to `main`/`dev`, every PR | ~2 min | Offline unit tests, import checks |
| `ci-live.yml` | PR to `main` + nightly 02:00 UTC | ~15–20 min | Live database integration tests |

---

## Environment variables

All tests read from environment variables. `.env.example` is the authoritative
list. Setup scripts write `.env.local` (git-ignored) with the same variable names.

Developers load `.env.local` before running tests:
```bash
set -a && source .env.local && set +a
pytest tests/ -k "live"
```

GitHub Actions sets the same variables via the workflow `env:` block, populated
from the output of `db-up-x86_64.sh`.

---

## Cloud database support

All hostnames, ports, and credentials are env-var driven with local defaults.
Changing `.env` (or `.env.local`) to point at a cloud endpoint is sufficient for
most engines. Two engines have hardcoded SSL assumptions addressed in Task 7.

### Engine readiness

| Engine | Cloud-ready | Notes |
|---|---|---|
| PostgreSQL | Yes | `sslmode="prefer"` works for RDS, Cloud SQL, Aurora, Neon |
| Neon | Yes | `NEON_SSLMODE` env var present; SSL fallback loop in place |
| MySQL | Yes | No SSL hardcoding; works for RDS MySQL, Cloud SQL MySQL |
| Oracle | Mostly | DSN is env-var driven; cloud wallet auth needs extra config |
| DB2 | Yes | Staging dirs env-var driven; skips cleanly when not set |
| CockroachDB | After Task 7 | `sslmode="disable"` hardcoded in two places |
| SQL Server | After Task 7 | No TLS/encrypt option; fails with forced encryption |

### Progression model

```
1. Local dev   →  ./scripts/db-up.sh          →  .env.local (host)   →  pytest -k live
2. Linux test  →  ./scripts/test-on-linux.sh  →  .env.local (in VM)  →  pytest -k live
3. CI          →  ci-live.yml                 →  env: block           →  pytest -k live
4. Cloud DB    →  edit .env (host/port/pass/ssl)                      →  pytest -k live
```

Steps 1–3 use local containers. Step 4 requires no code changes — only `.env`
values change. After Task 7, all seven engines support step 4.
