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
| **PostgreSQL** | Podman (native ARM) | arm64 | 5414 / 5416 | Official ARM64 image |
| **MySQL** | Podman (native ARM) | arm64 | 3357 / 3384 | Official ARM64 image |
| **SQL Server** | Lima VM + QEMU (x86_64) | x86_64 | **14330** | No ARM64 build exists — see note below |
| **Oracle XE** | Lima VM + Podman + QEMU (x86_64) | x86_64 | **1521** | No ARM64 build exists |

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

**Image used**: `docker.io/gvenzl/oracle-xe:21-slim` (Docker Hub, ~800 MB,
no Oracle registry account or licence acceptance required).  This community
image is maintained by [Gerald Venzl](https://github.com/gvenzl/oci-oracle-xe)
and is widely used for development and testing.

### Prerequisites

```bash
brew install lima
```

No Oracle account needed.  The image pulls from Docker Hub anonymously.

Install the Python driver for automated tests:

```bash
.venv_test/bin/pip install oracledb
# (already listed in requirements-test.txt after running make venv-test)
```

### First-time VM creation

```bash
limactl start --name=oracle config/lima/oracle.yaml
```

Lima will:
1. Download an Ubuntu 22.04 x86_64 cloud image (~600 MB, first run only)
2. Boot the VM under QEMU (~1–2 min)
3. Install Podman inside the VM
4. Pull `gvenzl/oracle-xe:21-slim` from Docker Hub (~800 MB)
5. Start the Oracle XE container

> **Note**: the Ubuntu image download and Oracle XE pull happen in the
> provisioning script (`cloud-init`).  First-time setup takes **5–10 minutes**
> depending on your network speed.

### Wait for Oracle XE to be ready

Oracle XE initialises its data files on first start, which takes an additional
**3–5 minutes** after the container starts.  Poll readiness:

```bash
# Wait until DATABASE IS READY TO USE appears in the container logs
until limactl shell oracle -- podman logs oracle-xe 2>/dev/null \
      | grep -q "DATABASE IS READY TO USE"; do
  echo "[$(date +%H:%M:%S)] waiting…"
  sleep 15
done
echo "Oracle XE ready!"

# Confirm port 1521 is open
nc -z 127.0.0.1 1521 && echo "port 1521 open"
```

### Subsequent starts (after limactl stop/start)

The container is configured to restart automatically via a systemd service.
After a `limactl stop oracle` + `limactl start oracle`, the container starts
automatically.  Verify it is up:

```bash
limactl start oracle
# Wait ~30 s, then:
nc -z -w5 127.0.0.1 1521 && echo "Oracle XE ready" || echo "still starting"
```

If the container did not start automatically:

```bash
limactl shell oracle -- sudo podman start oracle-xe
# then wait for DATABASE IS READY TO USE (as above)
```

### Connect from macOS

**Python (oracledb — no Instant Client required)**

The `oracledb` driver runs in "thin" mode by default (pure Python, no Oracle
Instant Client installation required):

```python
import oracledb
conn = oracledb.connect(user="system", password="oracle",
                        dsn="127.0.0.1:1521/XE")
```

**sqlplus (optional, needs Oracle Instant Client)**

```bash
brew tap InstantClientTap/instantclient
brew install instantclient-basic instantclient-sqlplus
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

### Run the automated live tests

```bash
make test-live-oracle
# or directly:
.venv_test/bin/pytest tests/test_live_oracle.py -v
```

All 20 tests should pass.  Typical runtime: ~10 seconds (after Oracle XE is up).

The tests create and tear down a dedicated `ZEROBUS_TEST` schema — they do not
touch any existing data in the `system` or `XE` schemas.

Environment variables (defaults match the oracle.yaml values):

```bash
ORACLE_HOST=127.0.0.1   # default
ORACLE_PORT=1521         # default
ORACLE_USER=system       # default
ORACLE_PASS=oracle       # default — set via ORACLE_PASSWORD in oracle.yaml
ORACLE_SERVICE=XE        # default
ORACLE_SCHEMA=ZEROBUS_TEST  # default — created fresh on each test run
```

All are read from `.env` automatically if set there (see `.env.example`).

### Common VM operations

```bash
limactl stop  oracle
limactl start oracle          # container restarts automatically
limactl shell oracle          # open a shell inside the VM
limactl delete oracle         # destroy completely (data is lost)

# Recreate from scratch:
limactl delete oracle && limactl start --name=oracle config/lima/oracle.yaml
```

Check Oracle container status:

```bash
limactl shell oracle -- podman ps
limactl shell oracle -- podman logs oracle-xe | tail -20
```

### Known type normalizations (Oracle)

| Input type | Canonical type | Emitted as | Reason |
|------------|----------------|------------|--------|
| `NUMBER(p)` (no scale, p ≤ 9) | `integer` | `NUMBER(10)` | Widened to canonical integer bucket |
| `NUMBER(p)` (no scale, 9 < p ≤ 18) | `long` | `NUMBER(19)` | Widened to canonical long bucket |
| `CHAR(n)` | `string` | `VARCHAR2(n)` | Canonical model normalises CHAR → string |
| `CLOB` | `string` (no length) | `CLOB` | No-length string round-trips as CLOB |
| `NUMBER(1)` | `integer` | `NUMBER(10)` | Single-digit NUMBER widens to integer |
| `BOOLEAN` (PL/SQL only) | `boolean` | `NUMBER(1)` | Oracle has no table-level BOOLEAN |

### Troubleshooting

**`ORA-01031: insufficient privileges` on IDENTITY columns**
The test user needs `CREATE SEQUENCE` privilege.  The fixture grants it
automatically; if running manually, grant it from `system`:
```sql
GRANT CREATE SEQUENCE TO myuser;
```

**Container not starting after VM restart**
```bash
limactl shell oracle -- sudo systemctl status container-oracle-xe
# If failed: sudo systemctl restart container-oracle-xe
```

**Port 1521 not open after container is running**
Oracle XE takes 3–5 min to initialise data files on first boot.
Use the polling script in "Wait for Oracle XE to be ready" above.

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
from statschema import parse_ddl, emit_ddl

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
- [gvenzl/oci-oracle-xe Docker Hub image](https://hub.docker.com/r/gvenzl/oracle-xe) — community Oracle XE image (no registry auth needed)
- [Oracle Container Registry](https://container-registry.oracle.com) — official image (requires account + licence acceptance)
- [Azure Data Studio](https://azure.microsoft.com/en-us/products/data-studio)
- [`config/lima/sqlserver.yaml`](../config/lima/sqlserver.yaml) — Lima VM config for SQL Server
- [`config/lima/oracle.yaml`](../config/lima/oracle.yaml) — Lima VM config for Oracle XE
- [`tests/test_live_oracle.py`](../tests/test_live_oracle.py) — Oracle live test suite (20 tests)
- [`tests/test_live_mautic.py`](../tests/test_live_mautic.py) — Mautic application live test suite (19 tests)
- [`docs/testing.md`](testing.md) — local Python test strategy

---

## Mautic (application-level testing)

[Mautic](https://www.mautic.org/) is an open-source marketing automation platform with
**~108 MySQL tables**.  It is an ideal application-level test target because:

- It exercises a broad range of MySQL types (`BIGINT UNSIGNED`, `TINYINT(1)`, `LONGTEXT`,
  `DATETIME`, `VARCHAR`, `DOUBLE`, `INT`, etc.).
- It contains virtual/generated columns (`GENERATED ALWAYS AS … VIRTUAL`).
- High-volume log and stats tables (`email_stats`, `campaign_lead_event_log`, `page_hits`)
  are natural shard candidates for the `instance_count` / `aliases` multi-instance feature.

### One-time setup

```bash
# 1 – Create the mautic database on the existing mysql8 container
podman exec mysql8 mysql -uroot -ptestpass -e "
  CREATE DATABASE IF NOT EXISTS mautic CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
  CREATE USER IF NOT EXISTS 'mautic'@'%' IDENTIFIED BY 'mauticpass';
  GRANT ALL PRIVILEGES ON mautic.* TO 'mautic'@'%';
  FLUSH PRIVILEGES;
"

# 2 – Create a network so Mautic can reach mysql8 by container name
podman network create mautic_net 2>/dev/null || true
podman network connect mautic_net mysql8 2>/dev/null || true

# 3 – Pull and start Mautic 5
podman pull docker.io/mautic/mautic:5-apache
podman run -d \
  --name mautic \
  --network mautic_net \
  -p 8080:80 \
  -e MAUTIC_DB_HOST=mysql8 \
  -e MAUTIC_DB_PORT=3306 \
  -e MAUTIC_DB_NAME=mautic \
  -e MAUTIC_DB_USER=mautic \
  -e MAUTIC_DB_PASSWORD=mauticpass \
  docker.io/mautic/mautic:5-apache

# 4 – Install Mautic via CLI (skips the web wizard, ~4 s)
podman exec mautic php /var/www/html/bin/console mautic:install \
  --force \
  --db_driver=pdo_mysql \
  --db_host=mysql8 \
  --db_port=3306 \
  --db_name=mautic \
  --db_user=mautic \
  --db_password=mauticpass \
  --admin_email=admin@example.com \
  "--admin_password=Mautic1234\!" \
  --admin_firstname=Admin \
  --admin_lastname=User \
  "http://127.0.0.1:8080"

# 5 – Fix Doctrine migration metadata (needed after CLI install)
podman exec mautic bash -c "
  cd /var/www/html
  php bin/console doctrine:migrations:sync-metadata-storage
  php bin/console doctrine:migrations:version --add --all --no-interaction
  php bin/console cache:warmup
"

# 6 – Insert 10 test contacts
podman exec mysql8 mysql -umautic -pmauticpass mautic -e "
INSERT INTO leads (is_published, date_added, date_modified, date_identified,
                   firstname, lastname, email, company, points) VALUES
  (1,NOW(),NOW(),NOW(),'Alice','Anderson','alice@example.com','Acme Corp',10),
  (1,NOW(),NOW(),NOW(),'Bob','Baker','bob@example.com','Beta Inc',20),
  (1,NOW(),NOW(),NOW(),'Carol','Clark','carol@example.com','Contoso Ltd',15),
  (1,NOW(),NOW(),NOW(),'David','Davis','david@example.com','Dunder Mifflin',5),
  (1,NOW(),NOW(),NOW(),'Eva','Evans','eva@example.com','Extensive Ent',30),
  (1,NOW(),NOW(),NOW(),'Frank','Fisher','frank@example.com','Fabco Inc',25),
  (1,NOW(),NOW(),NOW(),'Grace','Garcia','grace@example.com','Gamma Corp',8),
  (1,NOW(),NOW(),NOW(),'Henry','Harris','henry@example.com','Heroic Ltd',12),
  (1,NOW(),NOW(),NOW(),'Irene','Irving','irene@example.com','Ideal Systems',18),
  (1,NOW(),NOW(),NOW(),'James','Johnson','james@example.com','Jubilee Corp',22);
"
```

### Running the tests

```bash
make test-live-mautic
# or
.venv_test/bin/python -m pytest tests/test_live_mautic.py -v
```

Expected output: **19 passed**.

### Known Mautic-specific behaviours

| Mautic column pattern | Canonical type | Notes |
|-----------------------|----------------|-------|
| `BIGINT UNSIGNED AUTO_INCREMENT` | `long` | PK on all entity tables |
| `TINYINT(1)` | `integer` | boolean flags (is_published, is_read, …) |
| `LONGTEXT` | `string` | JSON blobs (COMMENT '(DC2Type:array)') |
| `GENERATED ALWAYS AS … VIRTUAL` | column dropped | Generated columns are skipped; only stored columns survive parse |
| `INT UNSIGNED` | `long` | Foreign key references (owner_id, stage_id, …) |

### Multi-instance / shard candidates

These tables grow large in production and are typically sharded:

| Table | Columns | Shard reason |
|-------|---------|--------------|
| `email_stats` | 21 | One row per sent email, very high volume |
| `campaign_lead_event_log` | 14 | One row per campaign action |
| `page_hits` | 26 | One row per page view |
| `audit_log` | 10 | Append-only audit trail |

Example canonical YAML (4-shard email_stats):

```yaml
version: "1.0"
tables:
  - name: email_stats
    instance_count: 4
    instance_suffix_format: "_{:04d}"
    columns:
      - {name: id,            type: long,    auto_increment: true, primary_key: true, not_null: true}
      - {name: email_id,      type: long}
      - {name: lead_id,       type: long}
      - {name: date_sent,     type: datetime, not_null: true}
      # … other columns …
```

After `load_canonical(yaml_str, expand=True)` this produces four tables named
`email_stats_0001` through `email_stats_0004`, each with identical column definitions.

### Mautic API note

The Mautic REST API requires OAuth2 by default.  HTTP Basic Auth can be enabled by
adding the following to `config/local.php` and rebuilding the cache:

```php
'api_enabled'           => true,
'api_enable_basic_auth' => true,
```

For automated testing, inserting contacts directly via MySQL (as done in step 6 above)
is more reliable than the REST API and avoids OAuth token management.

---

## Gitea (PostgreSQL application-level testing)

[Gitea](https://gitea.com/) is a lightweight self-hosted Git service (~112 PostgreSQL tables).
It exercises diverse PostgreSQL types: `BIGINT`, `BOOLEAN`, `TEXT`, `TIMESTAMP WITH TIME ZONE`,
`BYTEA`, and `JSONB`.  It also has natural multi-instance candidates for CI/CD action tables.

### One-time setup

```bash
# 1 – Create gitea user and database on pg16
podman exec pg16 psql -U postgres -c "
  CREATE USER gitea WITH PASSWORD 'gitea123';
  CREATE DATABASE gitea OWNER gitea;
"

# 2 – Create dedicated Podman network
podman network create gitea_net 2>/dev/null || true
podman network connect gitea_net pg16 2>/dev/null || true

# 3 – Pull and start Gitea (schema auto-created on first run)
podman pull docker.io/gitea/gitea:latest
podman run -d --name gitea --network gitea_net -p 3000:3000 \
  -e GITEA__database__DB_TYPE=postgres \
  -e GITEA__database__HOST=pg16:5432 \
  -e GITEA__database__NAME=gitea \
  -e GITEA__database__USER=gitea \
  -e GITEA__database__PASSWD=gitea123 \
  -e GITEA__security__INSTALL_LOCK=true \
  -e GITEA__security__SECRET_KEY=zerobus_test_secret_key_32chars0 \
  -e GITEA__server__ROOT_URL=http://localhost:3000/ \
  docker.io/gitea/gitea:latest

# 4 – Wait ~30 s for schema creation, then create an admin user
sleep 30
podman exec -u git gitea gitea admin user create \
  --admin --username=gitadmin --password=Gitpass123! \
  --email=admin@example.com --must-change-password=false
```

### Running the tests

```bash
make test-live-gitea
```

Expected output: **16 passed**.

---

## AdventureWorks (SQL Server — Microsoft canonical sample databases)

Microsoft publishes two variants of the AdventureWorks sample:

| Database | Tables | Description |
|----------|--------|-------------|
| **AdventureWorksLT2022** | 12 | Lightweight — retail customer/product/sales. Quick to load (~8 MB). |
| **AdventureWorks2022** | 71 | Full — 6 schemas: HumanResources, Person, Production, Purchasing, Sales, dbo. Exercises every major SQL Server type. |

The full database is the richest SQL Server test target available:
user-defined types (`Name`, `Flag`, `Phone`, `AccountNumber`), `MONEY`,
`UNIQUEIDENTIFIER`, `ROWVERSION`/`timestamp`, `xml`, `hierarchyid`, and
`geography`.

### One-time setup

```bash
# 1 – Download .bak files
curl -sL "https://github.com/Microsoft/sql-server-samples/releases/download/adventureworks/AdventureWorksLT2022.bak" \
     -o /tmp/AdventureWorksLT2022.bak
curl -sL "https://github.com/Microsoft/sql-server-samples/releases/download/adventureworks/AdventureWorks2022.bak" \
     -o /tmp/AdventureWorks2022.bak   # ~200 MB

# 2 – Copy into Lima VM
limactl copy /tmp/AdventureWorksLT2022.bak sqlserver22:/tmp/
limactl copy /tmp/AdventureWorks2022.bak   sqlserver22:/tmp/

# 3 – Move to SQL Server-accessible path (inside VM)
limactl shell sqlserver22 -- sudo bash -c "
  mkdir -p /var/opt/mssql/backup
  cp /tmp/AdventureWorks*.bak /var/opt/mssql/backup/
  chown -R mssql:mssql /var/opt/mssql/backup
"

# 4 – Restore both databases
source .env
sqlcmd -S "127.0.0.1,${SQLSERVER_PORT:-14330}" -U sa -P "$SQLSERVER_PASS" -C -Q "
  RESTORE DATABASE [AdventureWorksLT2022]
  FROM DISK = '/var/opt/mssql/backup/AdventureWorksLT2022.bak'
  WITH MOVE 'AdventureWorksLT2022_Data' TO '/var/opt/mssql/data/AdventureWorksLT2022.mdf',
       MOVE 'AdventureWorksLT2022_Log'  TO '/var/opt/mssql/data/AdventureWorksLT2022_log.ldf',
       REPLACE;
  RESTORE DATABASE [AdventureWorks2022]
  FROM DISK = '/var/opt/mssql/backup/AdventureWorks2022.bak'
  WITH MOVE 'AdventureWorks2022'     TO '/var/opt/mssql/data/AdventureWorks2022.mdf',
       MOVE 'AdventureWorks2022_log' TO '/var/opt/mssql/data/AdventureWorks2022_log.ldf',
       REPLACE;
"
```

### Running the tests

```bash
make test-live-adventureworks
```

Expected output: **34 passed** (14 for AWLT + 20 for full AW2022).

### Exotic type handling

SQL Server types with no direct canonical equivalent are remapped to `NVARCHAR(MAX)` during DDL extraction so the parser remains unblocked:

| SQL Server type | Canonical fallback |
|----------------|--------------------|
| `hierarchyid` | `string` (via NVARCHAR MAX) |
| `geography` | `string` |
| `xml` | `string` |
| `sql_variant` | `string` |
| `timestamp` (rowversion) | `string` |

User-defined types are resolved to their base type before parsing via `sys.types`.

---

## Chinook (SQL Server application-level testing)

[Chinook](https://github.com/lerocha/chinook-database) is the de-facto SQL Server sample database,
modelling a digital music store (11 tables, based on the iTunes schema).  It covers
`NVARCHAR`, `INTEGER`, `DECIMAL`, `DATETIME`, `NUMERIC`, composite PKs, and FK chains.

### One-time setup

```bash
# Download and load the Chinook T-SQL script into the SQL Server 22 Lima VM
curl -sL "https://raw.githubusercontent.com/lerocha/chinook-database/master/\
ChinookDatabase/DataSources/Chinook_SqlServer.sql" -o /tmp/chinook_sqlserver.sql

source .env
sqlcmd -S "127.0.0.1,${SQLSERVER_PORT:-14330}" -U sa -P "$SQLSERVER_PASS" \
       -C -i /tmp/chinook_sqlserver.sql
```

### Running the tests

```bash
make test-live-chinook
```

Expected output: **16 passed**.

---

## Oracle HR / CO sample schemas (Oracle application-level testing)

Oracle's canonical sample schemas from [oracle-samples/db-sample-schemas](https://github.com/oracle-samples/db-sample-schemas):

- **HR (Human Resources)** — 7 tables: REGIONS → COUNTRIES → LOCATIONS → DEPARTMENTS → JOBS → EMPLOYEES → JOB_HISTORY
- **CO (Customer Orders)** — 7 tables: CUSTOMERS → STORES → PRODUCTS → ORDERS → SHIPMENTS → ORDER_ITEMS → INVENTORY

Together they cover Oracle-specific types: `NUMBER(p,s)`, `VARCHAR2`, `CHAR`, `DATE`, `TIMESTAMP`,
`INTERVAL`, `CLOB`, and the FK graph required by Oracle's semantic rules.

### One-time setup

```bash
# Download the create scripts
curl -sL "https://raw.githubusercontent.com/oracle-samples/db-sample-schemas/main/human_resources/hr_create.sql" \
  -o /tmp/hr_create.sql
curl -sL "https://raw.githubusercontent.com/oracle-samples/db-sample-schemas/main/customer_orders/co_create.sql" \
  -o /tmp/co_create.sql

# Create users and schemas via Python (handles SQL*Plus directives)
.venv_test/bin/python - << 'EOF'
import oracledb, re

def run_sql_script(conn, script_path, skip_views=True):
    with open(script_path) as f:
        content = f.read()
    content = re.sub(r'^(SET|Prompt|SPOOL|HOST|COLUMN|TTITLE|PAUSE|DEFINE|ACCEPT|REMARK)\b.*$',
                     '', content, flags=re.IGNORECASE|re.MULTILINE)
    content = re.sub(r'^rem\b.*$', '', content, flags=re.IGNORECASE|re.MULTILINE)
    content = re.sub(r'--.*$', '', content, flags=re.MULTILINE)
    cur = conn.cursor()
    for stmt in [s.strip() for s in content.split(';') if len(s.strip()) > 5]:
        if skip_views and re.match(r'CREATE\s+OR\s+REPLACE\s+VIEW', stmt, re.I): continue
        if re.match(r'COMMENT\s+ON', stmt, re.I): continue
        try:
            cur.execute(stmt)
            conn.commit()
        except Exception as e:
            if 'ORA-00955' not in str(e):  # ignore "already exists"
                print(f"SKIP: {str(e)[:60]}")

sys_conn = oracledb.connect(user='system', password='oracle', dsn='127.0.0.1:1521/XE')
cur = sys_conn.cursor()
for user in ['hr', 'oe', 'co']:
    try: cur.execute(f'DROP USER {user} CASCADE')
    except: pass
for stmt in [
    "CREATE USER hr IDENTIFIED BY hr",
    "GRANT CONNECT, RESOURCE, CREATE VIEW TO hr",
    "ALTER USER hr QUOTA UNLIMITED ON USERS",
    "CREATE USER co IDENTIFIED BY co",
    "GRANT CONNECT, RESOURCE, CREATE VIEW, CREATE SEQUENCE TO co",
    "ALTER USER co QUOTA UNLIMITED ON USERS",
]:
    cur.execute(stmt)
sys_conn.commit()
sys_conn.close()

run_sql_script(oracledb.connect(user='hr', password='hr', dsn='127.0.0.1:1521/XE'), '/tmp/hr_create.sql')
run_sql_script(oracledb.connect(user='co', password='co', dsn='127.0.0.1:1521/XE'), '/tmp/co_create.sql')
print("HR and CO schemas created")
EOF
```

### Running the tests

```bash
make test-live-oracle-hr
```

Expected output: **20 passed**.
