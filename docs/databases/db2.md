# IBM Db2 CE — local setup

← [All databases](../local-databases.md)

**Method:** Lima VM + Podman + QEMU, x86_64 · **Port:** 50000 · **Driver:** `ibm_db` (ARM64 wheels available)

IBM Db2 CE has no linux/arm64 image.  `ibm_db` has macOS arm64 wheels — tests run from the host; the Db2 server runs in the Lima VM.

sqlglot has no Db2 dialect.  DDL is emitted by a hand-written emitter in `ddl_emitter.py`.  Db2 DDL parsing uses ANSI SQL mode (empty sqlglot dialect string).

## Start the Lima VM

```bash
limactl start --name=db2 config/lima/db2.yaml
```

First boot pulls the image (~2 GB) and runs Db2 setup — **this takes 5–10 minutes**.  Watch progress:

```bash
limactl shell db2 -- podman logs -f db2ce
# Wait for: "Setup has completed"
```

Or poll:

```bash
until limactl shell db2 -- podman logs db2ce 2>/dev/null \
    | grep -q "Setup has completed"; do
  echo "waiting..."; sleep 15
done
echo "Db2 ready"
```

## Run the live tests

```bash
make test-live-db2
# or directly:
.venv_test/bin/pytest tests/test_live_db2.py -v
```

Override connection defaults:

```bash
DB2_HOST=127.0.0.1 DB2_PORT=50000 DB2_PASS=testpass make test-live-db2
```

## Connect manually

```bash
# From macOS host using db2 CLI inside the VM:
limactl shell db2 -- su - db2inst1 -c "db2 connect to testdb"

# Using Python from the macOS host:
python3 -c "
import ibm_db_dbi
conn = ibm_db_dbi.connect(
    'DATABASE=testdb;HOSTNAME=127.0.0.1;PORT=50000;PROTOCOL=TCPIP;UID=db2inst1;PWD=testpass;',
    '', '')
print(conn.cursor().execute('SELECT SERVICE_LEVEL FROM SYSIBMADM.ENV_INST_INFO').fetchone())
"
```

## Stop / restart / delete

```bash
limactl stop  db2   # stops VM (Db2 container persists via systemd)
limactl start db2   # restarts VM; Db2 container auto-starts
limactl delete db2  # destroys VM and all database data
```

## Known type normalizations

| Input type | `SYSCAT.COLUMNS TYPENAME` | Notes |
|---|---|---|
| `CLOB` | `CLOB` | |
| `BOOLEAN` | `BOOLEAN` | Native Db2 11.1+ type |
| `REAL` | `REAL` | |
| `DOUBLE` | `DOUBLE` | |
| `SMALLINT` | `INTEGER` | |
| `CHAR(n)` | `VARCHAR` | |
| `CHAR(36)` | `CHARACTER` | |
| `GENERATED ALWAYS AS IDENTITY` | `INTEGER` | |
| No `VARBINARY` | `BLOB` | |

## Troubleshooting

**Container fails to start with "Operation not permitted"**
The `--privileged=true` flag is required for Db2.  Verify:
```bash
limactl shell db2 -- podman ps
```

**"Setup has completed" never appears**
Db2 setup can take up to 10 minutes on first boot.  If the probe times out, delete and retry:
```bash
limactl delete db2 && limactl start --name=db2 config/lima/db2.yaml
```

**SQLCODE=-1031 or authentication failure**
Verify `DB2_PASS` matches the `DB2INST1_PASSWORD` set in `config/lima/db2.yaml` (default: `testpass`).

**Fatal glibc error: CPU does not support x86-64-v2**
The Db2 11.5.9 container requires SSE4.2 and POPCNT instructions.  The `config/lima/db2.yaml` already sets `cpuType: x86_64: "Haswell-noTSX-IBRS"` to satisfy this.  If you recreated the YAML manually, ensure this line is present.

**`db2ftok.C:87: Failed to generate seed` / `instance home directory is invalid because it is not owned by db2inst1`**
This occurs after a VM recreate: the Podman container's `useradd` assigns a new UID that does not match the volume's ownership.  The fix is baked into `container-db2ce.service` via `ExecStartPre` directives in `config/lima/db2.yaml`.  Trigger them with:

```bash
limactl shell db2 -- sudo systemctl restart container-db2ce.service
```

If the service fails to start, run the ownership fix manually inside the VM:

```bash
limactl shell db2 -- sudo bash -c "
  podman exec db2ce chown -R db2inst1:db2iadm1 /home/db2inst1 &&
  podman exec db2ce chown db2inst1:db2iadm1 /home/db2inst1/sqllib/security/db2ftok
"
```
