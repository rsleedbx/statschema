# Neon — local setup

← [All databases](../local-databases.md)

**Method:** Podman + Neon Local cloud proxy · **Port:** 55433 (maps to Postgres 5432) · **Driver:** `psycopg2-binary`

Use the **`neondatabase/neon_local`** image on [Docker Hub](https://hub.docker.com/r/neondatabase/neon_local) for integration tests.

`neondatabase/neon` exposes a pageserver port, not Postgres 5432.  Use `neondatabase/neon_local`.

## Prerequisites

Obtain a [Neon API key and project ID](https://neon.tech/docs/local/neon-local) from the Neon console.

## Start Neon Local

```bash
podman run -d --name neon-local \
  -p 55433:5432 \
  -e NEON_API_KEY=<your_api_key> \
  -e NEON_PROJECT_ID=<your_project_id> \
  docker.io/neondatabase/neon_local:latest
```

Connection: user `neon`, password `npg`, database `neondb`.

## TLS / connection issues

Live tests use `sslmode=require`.  Set `NEON_DATABASE_URL` in `.env` to override with a full connection string.  Set `NEON_SSLMODE=disable` to disable TLS.

## Run the live tests

```bash
make test-live-neon
# or directly:
.venv_test/bin/pytest tests/test_live_neon.py -v
```

## Known type normalizations

Dialect aliases `neon` and `neondb` resolve to `postgres`.

| Input type | Canonical type | Emitted as |
|------------|---------------|------------|
| `CHAR(n)` | `string` | `CHARACTER VARYING(n)` |
| `SMALLINT` | `integer` | `INTEGER` |
| `BYTEA` | `binary` | `BYTEA` |
