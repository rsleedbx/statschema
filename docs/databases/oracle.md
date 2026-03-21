# Oracle XE — local setup

← [All databases](../local-databases.md)

**Method:** Lima VM + Podman + QEMU, x86_64 · **Port:** 1521 · **Driver:** `oracledb` (thin mode, no Instant Client)

Oracle XE has no linux/arm64 image.  This repo runs it in a Lima VM under QEMU (linux/amd64).

**Image**: `docker.io/gvenzl/oracle-xe:21-slim` — community image by [Gerald Venzl](https://github.com/gvenzl/oci-oracle-xe); anonymous pull from Docker Hub.

## Prerequisites

```bash
brew install lima
```

## First-time VM creation

```bash
limactl start --name=oracle config/lima/oracle.yaml
```

Lima will:
1. Download an Ubuntu 22.04 x86_64 cloud image (~600 MB, first run only)
2. Boot the VM under QEMU (~1–2 min)
3. Install Podman inside the VM
4. Pull `gvenzl/oracle-xe:21-slim` from Docker Hub (~800 MB)
5. Start the Oracle XE container

First-time setup takes **5–10 minutes** depending on network speed.

## Wait for Oracle XE to be ready

First start: allow 3–5 minutes after the container starts.  Poll for readiness:

```bash
until limactl shell oracle -- podman logs oracle-xe 2>/dev/null \
      | grep -q "DATABASE IS READY TO USE"; do
  echo "[$(date +%H:%M:%S)] waiting…"
  sleep 15
done
echo "Oracle XE ready!"

# Confirm port 1521 is open
nc -z 127.0.0.1 1521 && echo "port 1521 open"
```

## Subsequent starts

The container is configured to restart automatically via a systemd service:

```bash
limactl start oracle
# Wait ~30 s, then verify
nc -z -w5 127.0.0.1 1521 && echo "Oracle XE ready" || echo "still starting"
```

If the container did not auto-start:
```bash
limactl shell oracle -- sudo podman start oracle-xe
```

## Connect from macOS

**Python** (`oracledb` thin mode, no Instant Client required):

```python
import oracledb
conn = oracledb.connect(user="system", password="oracle",
                        dsn="127.0.0.1:1521/XE")
```

**sqlplus** (optional, needs Oracle Instant Client):

```bash
brew tap InstantClientTap/instantclient
brew install instantclient-basic instantclient-sqlplus
sqlplus system/oracle@//127.0.0.1:1521/XE
```

GUI client (DBeaver, DataGrip, etc.):

| Field | Value |
|-------|-------|
| Host | `127.0.0.1` |
| Port | `1521` |
| Service name | `XE` |
| User | `system` |
| Password | `oracle` |

JDBC URL: `jdbc:oracle:thin:@//127.0.0.1:1521/XE`

## Run the live tests

```bash
make test-live-oracle
# or directly:
.venv_test/bin/pytest tests/test_live_oracle.py -v
```

Expected result: **20 passed**.  Tests create and tear down a dedicated `STATSCHEMA_TEST` schema.

Environment variables (defaults match `oracle.yaml`):

```bash
ORACLE_HOST=127.0.0.1
ORACLE_PORT=1521
ORACLE_USER=system
ORACLE_PASS=oracle
ORACLE_SERVICE=XE
ORACLE_SCHEMA=STATSCHEMA_TEST
```

## Common VM operations

```bash
limactl stop  oracle
limactl start oracle
limactl shell oracle
limactl delete oracle

# Recreate from scratch:
limactl delete oracle && limactl start --name=oracle config/lima/oracle.yaml
```

Check Oracle container status:

```bash
limactl shell oracle -- podman ps
limactl shell oracle -- podman logs oracle-xe | tail -20
```

## Known type normalizations

| Input type | Canonical type | Emitted as |
|------------|----------------|------------|
| `NUMBER(p)` (no scale, p ≤ 9) | `integer` | `NUMBER(10)` |
| `NUMBER(p)` (no scale, 9 < p ≤ 18) | `long` | `NUMBER(19)` |
| `CHAR(n)` | `string` | `VARCHAR2(n)` |
| `CLOB` | `string` (no length) | `CLOB` |
| `NUMBER(1)` | `integer` | `NUMBER(10)` |
| `BOOLEAN` (PL/SQL only) | `boolean` | `NUMBER(1)` |

## Troubleshooting

**`ORA-01031: insufficient privileges` on IDENTITY columns**
The test user needs `CREATE SEQUENCE` privilege.  The fixture grants it automatically; if running manually:
```sql
GRANT CREATE SEQUENCE TO myuser;
```

**Container not starting after VM restart**
```bash
limactl shell oracle -- sudo systemctl status container-oracle-xe
# If failed: sudo systemctl restart container-oracle-xe
```

**Port 1521 not open after container is running**
Oracle XE takes 3–5 min to initialise data files on first boot.  Use the polling script above.

## Sample schemas

- [Oracle HR / CO schemas](oracle-hr.md)
