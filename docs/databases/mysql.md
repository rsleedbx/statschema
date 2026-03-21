# MySQL — local setup

← [All databases](../local-databases.md)

**Method:** Podman, native ARM64 (8.x) / x86_64 emulation (5.7) · **Ports:** 3357 (5.7), 3384 (8.4) · **Driver:** `pymysql`

The live test suite (`tests/test_live_mysql.py`) runs against both MySQL 5.7 and 8.x in one `pytest` run.

MySQL 5.7 has no linux/arm64 image; uses `--platform linux/amd64`.

## Start both versions

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

## Run the live tests

```bash
make test-live-mysql
# or directly:
.venv_test/bin/pytest tests/test_live_mysql.py -v
```

Override port defaults:

```bash
MYSQL57_PORT=3357 MYSQL8_PORT=3384 make test-live-mysql
```

## Connect manually

```bash
# MySQL 5.7
mysql -h 127.0.0.1 -P 3357 -u root -ptestpass testdb

# MySQL 8.4
mysql -h 127.0.0.1 -P 3384 -u root -ptestpass testdb
```

## Stop / remove

```bash
podman stop mysql57 mysql8
podman rm   mysql57 mysql8
```

## Known type normalizations

| Input type | Canonical type | Emitted as |
|------------|---------------|------------|
| `CHAR(n)` | `string` | `VARCHAR(n)` |
| `TINYINT` / `SMALLINT` | `integer` | `INT` |
| `TINYINT(1)` / `BOOLEAN` | `boolean` | `TINYINT(1)` |
| `BIGINT UNSIGNED` | `decimal` | `DECIMAL(20,0)` |

## Single MySQL 8.x for general use

```bash
podman run -d --name mysql-test \
  -e MYSQL_ROOT_PASSWORD=testpass \
  -e MYSQL_DATABASE=testdb \
  -p 3306:3306 \
  --restart unless-stopped \
  docker.io/library/mysql:8.4
```

## Alternative: Homebrew

```bash
brew install mysql
brew services start mysql
mysql -u root
```
