# MariaDB — local setup

← [All databases](../local-databases.md)

**Method:** Podman, native ARM64 · **Ports:** 3310 (10.11 LTS), 3311 (11.4) · **Driver:** `pymysql`

The live test suite (`tests/test_live_mariadb.py`) runs against MariaDB 10.11 (LTS) and 11.4 in one `pytest` run.

Official linux/arm64 images.  `dialect="mariadb"` and `schema_source: mariadb` resolve to `mysql` via `dialect_registry.py`.

## Start both versions

```bash
# MariaDB 10.11 LTS
podman run -d --name mariadb1011 \
  -e MARIADB_ROOT_PASSWORD=testpass \
  -e MARIADB_DATABASE=testdb \
  -p 3310:3306 \
  docker.io/library/mariadb:10.11

# MariaDB 11.4
podman run -d --name mariadb114 \
  -e MARIADB_ROOT_PASSWORD=testpass \
  -e MARIADB_DATABASE=testdb \
  -p 3311:3306 \
  docker.io/library/mariadb:11.4
```

## Run the live tests

```bash
make test-live-mariadb
# or directly:
.venv_test/bin/pytest tests/test_live_mariadb.py -v
```

Override port defaults:

```bash
MARIADB_LTS_PORT=3310 MARIADB_NEW_PORT=3311 make test-live-mariadb
```

## Connect manually

```bash
# MariaDB 10.11
mysql -h 127.0.0.1 -P 3310 -u root -ptestpass testdb

# MariaDB 11.4
mysql -h 127.0.0.1 -P 3311 -u root -ptestpass testdb
```

## Stop / remove

```bash
podman stop mariadb1011 mariadb114
podman rm   mariadb1011 mariadb114
```

## Known type normalizations

| Input type | `information_schema` reports | Notes |
|---|---|---|
| `TINYINT(1)` / `BOOLEAN` | `tinyint` | |
| `TEXT` / `LONGTEXT` | `text` | `emit_ddl` emits `TEXT` |
| `JSON` | `text` | `emit_ddl` emits `TEXT` |
| `UUID` (10.7+) | `uuid` | |
