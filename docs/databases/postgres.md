# PostgreSQL — local setup

← [All databases](../local-databases.md)

**Method:** Podman, native ARM64 · **Ports:** 5414 (PG 14), 5416 (PG 16) · **Driver:** `psycopg2-binary`

The live test suite (`tests/test_live_pg.py`) runs the same test classes against both PostgreSQL 14 and 16 in one `pytest` run.

## Start both versions

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

## Run the live tests

```bash
make test-live-pg
# or directly:
.venv_test/bin/pytest tests/test_live_pg.py -v
```

The test file auto-skips each version if its port is unreachable.  Override defaults:

```bash
PG14_PORT=5414 PG16_PORT=5416 make test-live-pg
```

## Connect manually

```bash
# PostgreSQL 14
PGPASSWORD=testpass psql -h 127.0.0.1 -p 5414 -U postgres -d testdb

# PostgreSQL 16
PGPASSWORD=testpass psql -h 127.0.0.1 -p 5416 -U postgres -d testdb
```

## Stop / remove

```bash
podman stop pg14 pg16
podman rm   pg14 pg16
```

## Known type normalizations

| Input type | Canonical type | Emitted as |
|------------|---------------|------------|
| `CHAR(n)` | `string` | `VARCHAR(n)` |
| `SMALLINT` | `integer` | `INTEGER` |
| `SERIAL` | `integer` | `INTEGER` |
| `NUMERIC(p)` (no scale) | `long` | `BIGINT` |
| `BYTEA` | `binary` | `BYTEA` |

## Single PG 16 for general use

```bash
podman run -d --name postgres-test \
  -e POSTGRES_PASSWORD=testpass \
  -e POSTGRES_DB=testdb \
  -p 5432:5432 \
  --restart unless-stopped \
  docker.io/library/postgres:16
```

## Alternative: Homebrew

```bash
brew install postgresql@16
brew services start postgresql@16
psql -U $(whoami) -d postgres
```
