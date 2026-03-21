# Gitea — application-level testing (PostgreSQL)

← [All databases](../local-databases.md)

**Requires:** [PostgreSQL](postgres.md) (`pg16` container running on port 5416)

[Gitea](https://gitea.com/) (~112 PostgreSQL tables).  Types include: `BIGINT`, `BOOLEAN`, `TEXT`, `TIMESTAMP WITH TIME ZONE`, `BYTEA`, `JSONB`.

## One-time setup

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
  -e GITEA__security__SECRET_KEY=statschema_test_secret_key_32chars0 \
  -e GITEA__server__ROOT_URL=http://localhost:3000/ \
  docker.io/gitea/gitea:latest

# 4 – Wait ~30 s for schema creation, then create an admin user
sleep 30
podman exec -u git gitea gitea admin user create \
  --admin --username=gitadmin --password=Gitpass123! \
  --email=admin@example.com --must-change-password=false
```

## Run the tests

```bash
make test-live-gitea
```

Expected output: **16 passed**.
