# Running Real Databases Locally on macOS (Apple Silicon)

This guide shows how to run **SQL Server, MySQL, PostgreSQL, and Oracle** locally
on macOS with Apple Silicon (M1/M2/M3/M4) for development and testing.

> **Podman, not Docker.**  Docker Desktop requires a paid commercial licence for
> company use.  All container commands in this guide use **Podman**, which is
> free, open-source, and daemonless.

---

## Strategy overview

| Database | Method | Arch | Port | Why |
|----------|--------|------|------|-----|
| **PostgreSQL** | Podman (native ARM) | arm64 | 5432 | Official ARM64 image |
| **MySQL** | Podman (native ARM) | arm64 | 3306 | Official ARM64 image |
| **SQL Server** | Lima VM + QEMU (x86_64) | x86_64 | **14330** | No ARM64 build exists — see note below |
| **Oracle XE** | Lima VM + Podman + QEMU (x86_64) | x86_64 | 1521 | No ARM64 build exists |

### Why SQL Server cannot use Podman/containers on Apple Silicon

**There is no ARM64 build of SQL Server for Linux.** This is a frequently
requested but unimplemented Microsoft feature
([mssql-docker issue #864](https://github.com/microsoft/mssql-docker/issues/864),
open since 2023, no committed roadmap).

- **Azure SQL Edge** was the only ARM64 SQL Server variant — Microsoft retired it in
  September 2023.
- The `mcr.microsoft.com/mssql/server` container image is x86_64-only. Running it
  via Rosetta 2 emulation under Podman or Docker Desktop works today but is becoming
  unreliable: recent CU builds crash on Apple Silicon, and **Apple is phasing out
  Rosetta 2 in macOS 28 (~2027)**.
- **Lima + QEMU** is the more durable choice: QEMU translates x86_64 instructions
  independently of Rosetta 2 and will continue working after macOS 28.

---

## Shared prerequisite: Homebrew

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
export PATH=/opt/homebrew/bin:$PATH
```

---

## Podman setup (one-time, for PostgreSQL and MySQL)

Podman Desktop is not required — the CLI alone is sufficient.

```bash
brew install podman
podman machine init          # create a lightweight Linux VM (first time only)
podman machine start         # start the VM (runs in background)
podman info                  # verify Podman is working
```

The Podman machine persists across reboots.  Start it automatically:

```bash
# Add to ~/.zshrc so the machine starts with each new shell if not already running
podman machine start 2>/dev/null || true
```

---

## PostgreSQL (Podman, native ARM64)

The live test suite (`tests/test_live_pg.py`) runs the same test classes against
**both PostgreSQL 14 and PostgreSQL 16** in one `pytest` run.  Both containers
must be running before executing the tests.

### Start both versions

```bash
# PostgreSQL 14 (older LTS) — native ARM64
podman run -d --name pg14 \
  -e POSTGRES_PASSWORD=testpass \
  -e POSTGRES_DB=testdb \
  -p 5414:5432 \
  docker.io/library/postgres:14

# PostgreSQL 16 (current LTS) — native ARM64
podman run -d --name pg16 \
  -e POSTGRES_PASSWORD=testpass \
  -e POSTGRES_DB=testdb \
  -p 5416:5432 \
  docker.io/library/postgres:16
```

### Run the live tests

```bash
make test-live-pg
# or directly:
.venv_test/bin/pytest tests/test_live_pg.py -v
```

The test file auto-skips each version if its port is unreachable.  Override the
defaults with environment variables:

```bash
PG14_PORT=5414 PG16_PORT=5416 make test-live-pg
```

### Connect manually

```bash
# PostgreSQL 14
PGPASSWORD=testpass psql -h 127.0.0.1 -p 5414 -U postgres -d testdb

# PostgreSQL 16
PGPASSWORD=testpass psql -h 127.0.0.1 -p 5416 -U postgres -d testdb
```

### Install the Python driver (if not already done)

```bash
.venv_test/bin/pip install psycopg2-binary
# (already listed in requirements-test.txt after first run)
```

### Stop / remove

```bash
podman stop pg14 pg16
podman rm   pg14 pg16
```

### Known type normalizations (PostgreSQL)

| Input type | Canonical type | Emitted as | Reason |
|------------|---------------|------------|--------|
| `CHAR(n)` | `string` | `VARCHAR(n)` | Canonical model normalises CHAR → VARCHAR |
| `SMALLINT` | `integer` | `INTEGER` | Widened for cross-DB portability |
| `SERIAL` | `integer` | `INTEGER` | Auto-increment sequence dropped; canonical model tracks only type |
| `NUMERIC(p)` (no scale) | `long` | `BIGINT` | Whole-number NUMERIC is semantically an integer |
| `BYTEA` | `binary` | `BYTEA` | PostgreSQL-only; no length stored |

### Single PostgreSQL 16 for general use (non-version-testing)

```bash
podman run -d --name postgres-test \
  -e POSTGRES_PASSWORD=testpass \
  -e POSTGRES_DB=testdb \
  -p 5432:5432 \
  --restart unless-stopped \
  docker.io/library/postgres:16
```

### Alternative: Homebrew (also fine, no container overhead)

```bash
brew install postgresql@16
brew services start postgresql@16
psql -U $(whoami) -d postgres
```

---

## MySQL (Podman, native ARM64)

The live test suite (`tests/test_live_mysql.py`) runs the same test classes against
**both MySQL 5.7 and MySQL 8.x** in one `pytest` run.  Both containers must be
running before executing the tests.

> **MySQL 5.7 note:** MySQL 5.7 reached end-of-life in October 2023 and has no
> official ARM64 Linux image.  The container runs via x86_64 emulation (Rosetta 2)
> inside the Podman VM.  This is adequate for compatibility testing.  MySQL 8.x has
> a native ARM64 image and runs at full speed.

### Start both versions

```bash
# MySQL 5.7 — x86_64 emulation (EOL, no ARM64 image)
podman run -d --name mysql57 \
  --platform linux/amd64 \
  -e MYSQL_ROOT_PASSWORD=testpass \
  -e MYSQL_DATABASE=testdb \
  -p 3357:3306 \
  docker.io/library/mysql:5.7

# MySQL 8.4 (latest 8.x) — native ARM64
podman run -d --name mysql8 \
  -e MYSQL_ROOT_PASSWORD=testpass \
  -e MYSQL_DATABASE=testdb \
  -p 3384:3306 \
  docker.io/library/mysql:8.4
```

### Run the live tests

```bash
make test-live-mysql
# or directly:
.venv_test/bin/pytest tests/test_live_mysql.py -v
```

The test file auto-skips each version if its port is unreachable.  Override the
defaults with environment variables:

```bash
MYSQL57_PORT=3357 MYSQL8_PORT=3384 make test-live-mysql
```

### Connect manually

```bash
# MySQL 5.7
mysql -h 127.0.0.1 -P 3357 -u root -ptestpass testdb

# MySQL 8.4
mysql -h 127.0.0.1 -P 3384 -u root -ptestpass testdb
```

### Install the Python driver (if not already done)

```bash
.venv_test/bin/pip install pymysql
# (already listed in requirements-test.txt after first run)
```

### Stop / remove

```bash
podman stop mysql57 mysql8
podman rm   mysql57 mysql8
```

### Known type normalizations (MySQL)

| Input type | Canonical type | Emitted as | Reason |
|------------|---------------|------------|--------|
| `CHAR(n)` | `string` | `VARCHAR(n)` | Canonical model does not distinguish CHAR from VARCHAR |
| `TINYINT` / `SMALLINT` | `integer` | `INT` | Widened to avoid cross-DB portability issues |
| `TINYINT(1)` / `BOOLEAN` | `boolean` | `TINYINT(1)` | MySQL boolean idiom preserved |
| `BIGINT UNSIGNED` | `decimal` | `DECIMAL(20,0)` | Unsigned 64-bit has no signed equivalent |

### Single MySQL 8.x for general use (non-version-testing)

```bash
podman run -d --name mysql-test \
  -e MYSQL_ROOT_PASSWORD=testpass \
  -e MYSQL_DATABASE=testdb \
  -p 3306:3306 \
  --restart unless-stopped \
  docker.io/library/mysql:8.4
```

### Alternative: Homebrew (also fine)

```bash
brew install mysql
brew services start mysql
mysql -u root
```

---

## SQL Server (Lima VM + QEMU, x86_64)

SQL Server for Linux has no ARM64 build.  We run it inside a Lima VM using QEMU
x86_64 emulation — this is more durable than Rosetta 2-based container emulation
because QEMU is independent of Apple's translation layer.

### Install prerequisites

```bash
brew tap microsoft/mssql-release
brew install lima pwgen mssql-tools
```

`mssql-tools` provides `sqlcmd` for command-line access.
Optionally install **Azure Data Studio** (download from Microsoft) for a GUI client.

Install the Python driver for the automated test suite:

```bash
.venv_test/bin/pip install pymssql
# (already in requirements-test.txt — only needed if skipping make venv-test)
```

### Existing VMs vs. creating a new one

If you already have a Lima VM named `sqlserver22` or `sqlserver19`, use it:

```bash
limactl list                 # show existing VMs and status
limactl start sqlserver22
```

To create a fresh VM from the checked-in config:

```bash
limactl start --name=sqlserver config/lima/sqlserver.yaml
```

Lima will:
1. Download an Ubuntu 20.04 x86_64 cloud image (first run only, ~500 MB)
2. Boot the VM under QEMU (~1–2 min)
3. Install SQL Server 2022
4. Generate a random SA password and log it to cloud-init output
5. Wait until SQL Server is listening on port 14330

> **Port**: the config uses **guest port 14330** (not the standard 1433) to avoid
> conflicts with any locally-installed SQL Server instance.

### Start the mssql-server service

SQL Server does **not** auto-start when the Lima VM boots.  Each time you start
the VM, also start the service:

```bash
limactl shell sqlserver22 -- sudo systemctl start mssql-server
```

Verify it is listening:

```bash
nc -zv 127.0.0.1 14330
```

If SQL Server is still initialising (upgrading system databases), wait ~30 s and retry.

### Find the SA password

```bash
limactl shell sqlserver22 -- grep "SQL Server sa password is" \
    /var/log/cloud-init-output.log | tail -1
```

Export for the session:

```bash
export SQLSERVER_PASS="<password from above>"
export SQLSERVER_PORT=14330
```

### Connect from macOS

```bash
sqlcmd -S 127.0.0.1,14330 -U sa -P "$SQLSERVER_PASS" -C
```

```sql
SELECT @@VERSION;
GO
```

**Azure Data Studio / any SQL client:**

| Field | Value |
|-------|-------|
| Server | `127.0.0.1` |
| Port | `14330` |
| Authentication | SQL Login |
| User | `sa` |
| Password | _(from cloud-init-output.log)_ |

### Run the automated live tests

```bash
SQLSERVER_PASS="$SQLSERVER_PASS" \
SQLSERVER_PORT=14330 \
.venv_test/bin/python -m pytest tests/test_live_sqlserver.py -v
```

Expected result: **14 passed**.  Tests auto-skip when `SQLSERVER_PASS` is not set.

### Common VM operations

```bash
limactl stop  sqlserver22    # shut down (preserves data)
limactl start sqlserver22    # restart (then systemctl start mssql-server)
limactl shell sqlserver22    # shell inside the VM
limactl delete sqlserver22   # destroy completely
```

Check SQL Server logs:

```bash
limactl shell sqlserver22 -- sudo tail -f /var/opt/mssql/log/errorlog
```

### Troubleshooting

**Port 14330 closed (Connection refused)**
```bash
limactl shell sqlserver22 -- sudo systemctl status mssql-server
# If inactive: sudo systemctl start mssql-server
```

**`Login failed for user 'sa'`**  
Multiple provision runs create multiple log entries.  Use `tail -1` to get the
most recent password (shown in the command above).

**Lima stuck at "Waiting for the optional requirement"**  
SQL Server is still initialising.  Check progress:
```bash
limactl shell sqlserver22 -- sudo tail -20 /var/opt/mssql/log/errorlog
# Ready when you see: Recovery is complete.
```

---

## Oracle XE (Lima VM + Podman + QEMU, x86_64)

Oracle Database 21c Express Edition (XE) is free for development.  It has no
ARM64 image, so it runs inside a Lima VM via Podman under QEMU x86_64 emulation.

### Prerequisites

```bash
brew install lima
```

You need a free Oracle account to pull from `container-registry.oracle.com`.
Sign up at https://login.oracle.com and accept the Oracle Database licence in
the Container Registry before the first pull.

### Start the VM

```bash
limactl start --name=oracle config/lima/oracle.yaml
```

First boot takes **3–5 minutes** while Oracle initialises its data files.
Monitor progress:

```bash
limactl shell oracle -- podman logs -f oracle-xe
```

Wait for:
```
DATABASE IS READY TO USE!
```

### Connect from macOS

Install the Oracle Instant Client:

```bash
brew tap InstantClientTap/instantclient
brew install instantclient-basic instantclient-sqlplus
```

Connect:

```bash
sqlplus system/oracle@//127.0.0.1:1521/XE
```

**Any SQL client (DBeaver, DataGrip, etc.):**

| Field | Value |
|-------|-------|
| Host | `127.0.0.1` |
| Port | `1521` |
| Service name | `XE` |
| User | `system` |
| Password | `oracle` |

JDBC URL: `jdbc:oracle:thin:@//127.0.0.1:1521/XE`

### Common VM operations

```bash
limactl stop  oracle
limactl start oracle    # then: limactl shell oracle -- podman start oracle-xe
limactl delete oracle
```

---

## Using these databases with the DDL tests

The DDL round-trip tests (`tests/test_ddl_roundtrip.py`) run entirely on parsed
DDL strings — no live database connection required.  The local databases are
useful for:

1. **Validating emitted DDL** — run `emit_ddl(schema, dialect=…)` output against
   the real engine to confirm it executes without errors.
2. **Capturing real DDL** — extract `CREATE TABLE` from a real database and feed
   to `parse_ddl()` to verify the parser on production schemas.
3. **End-to-end synthetic data** — generate a DataFrame with
   `build_dataframe_from_canonical()` and write to a real table to verify types
   and constraints are accepted.

### Extract DDL from a running database

**PostgreSQL**
```bash
pg_dump --schema-only -t my_table -d testdb | grep -A 999 "CREATE TABLE"
```

**MySQL**
```sql
SHOW CREATE TABLE my_table\G
```

**SQL Server**
```sql
SELECT OBJECT_DEFINITION(OBJECT_ID('dbo.my_table'));
```

**Oracle**
```sql
SELECT DBMS_METADATA.GET_DDL('TABLE', 'MY_TABLE', 'SYSTEM') FROM DUAL;
```

### Feed captured DDL to the parser

```python
from schema_parser import parse_ddl, emit_ddl

tables = parse_ddl(ddl_string)           # auto-detects dialect
print(emit_ddl(tables[0], dialect="mysql"))  # re-emit for any target
```

---

## Performance notes

- **PostgreSQL / MySQL** via Podman: native ARM64, near-native speed.
- **SQL Server / Oracle** via Lima + QEMU x86_64: typically **3–5× slower** than
  native x86_64 hardware.  Acceptable for DDL validation and small data volumes;
  not suitable for performance benchmarking.

---

## Treating containers and VMs as disposable

```bash
# PostgreSQL / MySQL (Podman)
podman rm -f postgres-test mysql-test
# re-run the podman run commands above

# SQL Server / Oracle (Lima)
limactl delete sqlserver22 && limactl start --name=sqlserver22 config/lima/sqlserver.yaml
limactl delete oracle      && limactl start --name=oracle      config/lima/oracle.yaml
```

---

## References

- [Lima project](https://lima-vm.io)
- [Podman](https://podman.io) / [Podman Desktop](https://podman-desktop.io)
- [SQL Server on Linux docs](https://learn.microsoft.com/en-us/sql/linux/)
- [SQL Server ARM64 feature request (open, no ETA)](https://github.com/microsoft/mssql-docker/issues/864)
- [Rosetta 2 phase-out announcement](https://arstechnica.com/gadgets/2025/06/apple-details-the-end-of-intel-mac-support-and-a-phaseout-for-rosetta-2/)
- [Oracle Container Registry](https://container-registry.oracle.com)
- [Azure Data Studio](https://azure.microsoft.com/en-us/products/data-studio)
- [`config/lima/sqlserver.yaml`](../config/lima/sqlserver.yaml) — Lima VM config for SQL Server
- [`config/lima/oracle.yaml`](../config/lima/oracle.yaml) — Lima VM config for Oracle XE
- [`docs/testing.md`](testing.md) — local Python test strategy
