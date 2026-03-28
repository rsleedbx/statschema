# CockroachDB — local setup

← [All databases](../local-databases.md)

**Method:** Podman, native ARM64 · **Driver:** `psycopg2-binary` (Postgres wire protocol)

The live test suite (`tests/test_live_cockroachdb.py`) parametrises over two topologies:

| Topology | Containers | Ports |
|----------|-----------|-------|
| **single-node** | `crdb-single` | 26257 (SQL) / 8080 (UI) |
| **multi-region** | `crdb1` `crdb2` `crdb3` | 26267–26269 (SQL) / 8083–8085 (UI) |

Both topologies use `--insecure` (no TLS); `sslmode=disable` in psycopg2.

## Single-node setup

The Podman VM is shared with the 3-node cluster (crdb1/crdb2/crdb3), which
consumes ~5.1 GB of the ~8.3 GB VM total.  Without a hard memory ceiling,
crdb-single defaults to 25% cache + 25% SQL memory ≈ 4 GB, which causes an
OOM kill under concurrent schema loads.  The `--memory=2g` flag sets a hard
container limit; `--cache` and `--max-sql-memory` tell CockroachDB to stay
within that budget.

```bash
podman run -d --name crdb-single \
  -p 26257:26257 -p 8080:8080 \
  --memory=2g \
  cockroachdb/cockroach:latest start-single-node \
  --insecure \
  --cache=512MiB \
  --max-sql-memory=512MiB

# Wait for cluster init (~8 s), then create test database
sleep 8
podman exec crdb-single \
  cockroach sql --insecure \
  -e "CREATE DATABASE IF NOT EXISTS testdb;"
```

## Run single-node tests

```bash
make test-live-cockroachdb
# or directly:
.venv_test/bin/python -m pytest tests/test_live_cockroachdb.py -v -k single-node
```

## Connect manually (single-node)

```bash
podman exec -it crdb-single cockroach sql --insecure --host=localhost:26257 --database=testdb
# Admin UI: http://localhost:8080
```

---

## Multi-region setup (3-node cluster)

```bash
# 1 – shared Podman network
podman network create crdb_net

# 2 – Node 1 (us-east1)
podman run -d --name crdb1 --network crdb_net \
  -p 26267:26257 -p 8083:8080 \
  cockroachdb/cockroach:latest start \
  --insecure \
  --locality=region=us-east1 \
  --advertise-addr=crdb1 \
  --join=crdb1,crdb2,crdb3 \
  --listen-addr=0.0.0.0:26257 \
  --http-addr=0.0.0.0:8080

# 3 – Node 2 (us-central1)
podman run -d --name crdb2 --network crdb_net \
  -p 26268:26257 -p 8084:8080 \
  cockroachdb/cockroach:latest start \
  --insecure \
  --locality=region=us-central1 \
  --advertise-addr=crdb2 \
  --join=crdb1,crdb2,crdb3 \
  --listen-addr=0.0.0.0:26257 \
  --http-addr=0.0.0.0:8080

# 4 – Node 3 (us-west1)
podman run -d --name crdb3 --network crdb_net \
  -p 26269:26257 -p 8085:8080 \
  cockroachdb/cockroach:latest start \
  --insecure \
  --locality=region=us-west1 \
  --advertise-addr=crdb3 \
  --join=crdb1,crdb2,crdb3 \
  --listen-addr=0.0.0.0:26257 \
  --http-addr=0.0.0.0:8080

# 5 – Bootstrap the cluster (run once after all nodes are up)
sleep 5
podman exec crdb1 cockroach init --insecure --host=crdb1:26257

# 6 – Create test database with multi-region configuration
sleep 5
podman exec crdb1 cockroach sql --insecure --host=crdb1:26257 -e "
  CREATE DATABASE IF NOT EXISTS testdb PRIMARY REGION 'us-east1';
  ALTER DATABASE testdb ADD REGION 'us-central1';
  ALTER DATABASE testdb ADD REGION 'us-west1';
"
```

## Run multi-region tests

```bash
.venv_test/bin/python -m pytest tests/test_live_cockroachdb.py -v -k multi-region
```

Multi-region tests verify:
- `SHOW REGIONS FROM DATABASE` reports ≥ 2 regions.
- `LOCALITY GLOBAL` tables are created correctly.
- `LOCALITY REGIONAL BY TABLE IN <region>` pins a table to one region.
- `LOCALITY REGIONAL BY ROW` auto-injects the `crdb_region` column.
- `emit_ddl(dialect="postgres")` output executes unchanged on the multi-region cluster.

## Connect manually (multi-region)

```bash
podman exec -it crdb1 cockroach sql --insecure --host=crdb1:26257 --database=testdb
# Admin UI (node-1): http://localhost:8083
```

## Stop / remove

```bash
# Single-node
podman stop crdb-single && podman rm crdb-single

# Multi-region
podman stop crdb1 crdb2 crdb3 && podman rm crdb1 crdb2 crdb3
podman network rm crdb_net
```

## Known type normalizations

| Postgres input type | Canonical type | CockroachDB `udt_name` | Note |
|--------------------|----------------|----------------------|------|
| `INTEGER` | `integer` | `int8` | CRDB `INT` is always 64-bit |
| `BIGINT` | `long` | `int8` | |
| `BYTEA` | `binary` | `bytes` | `BYTEA` is accepted as alias |
| `TEXT` / `VARCHAR(n)` | `string` | `text` | |
| `FLOAT` / `DOUBLE PRECISION` | `float` / `double` | `float8` | |
| `DECIMAL(p,s)` / `NUMERIC(p,s)` | `decimal` | `numeric` | |
| `TIMESTAMPTZ` | `timestamptz` | `timestamptz` | |
| `UUID` | `uuid` | `uuid` | |
