# Running Real Databases Locally on macOS (Apple Silicon)

This guide covers every database used for local integration testing on macOS with Apple Silicon (M1/M2/M3/M4).

All container commands use **Podman**.

**Adding a new database?** Read [`docs/adding-a-database.md`](adding-a-database.md) — it has the complete checklist for code, tests, and docs.

---

## Strategy overview

| Database | Page | Method | Arch | Ports |
|----------|------|--------|------|-------|
| **PostgreSQL** | [postgres.md](databases/postgres.md) | Podman (native ARM64) | arm64 | 5414 / 5416 |
| **Neon** | [neon.md](databases/neon.md) | Podman + Neon Local (cloud proxy) | any | 55433 |
| **CockroachDB** | [cockroachdb.md](databases/cockroachdb.md) | Podman (native ARM64) | arm64 | 26257 (single) / 26267–26269 (multi-region) |
| **MySQL** | [mysql.md](databases/mysql.md) | Podman (native ARM64 for 8.x) | arm64 / x86_64 | 3357 / 3384 |
| **MariaDB** | [mariadb.md](databases/mariadb.md) | Podman (native ARM64) | arm64 | 3310 / 3311 |
| **SQL Server** | [sqlserver.md](databases/sqlserver.md) | Lima VM + QEMU (x86_64) | x86_64 | 14330 |
| **Oracle XE** | [oracle.md](databases/oracle.md) | Lima VM + Podman + QEMU (x86_64) | x86_64 | 1521 |
| **IBM Db2 CE** | [db2.md](databases/db2.md) | Lima VM + Podman + QEMU (x86_64) | x86_64 | 50000 |

SQL Server, Oracle XE, and IBM Db2 CE have no linux/arm64 images.  This repo runs them in Lima VMs under QEMU (linux/amd64).

---

## Shared prerequisite: Homebrew

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
export PATH=/opt/homebrew/bin:$PATH
```

---

## Podman setup (one-time, for all Podman-based databases)

Podman Desktop is not required — the CLI alone is sufficient.

```bash
brew install podman
podman machine init          # create a lightweight Linux VM (first time only)
podman machine start         # start the VM (runs in background)
podman info                  # verify Podman is working
```

The Podman machine persists across reboots.  Start it automatically:

```bash
# Add to ~/.zshrc
podman machine start 2>/dev/null || true
```

---

## Application-level test schemas

These require the corresponding database container to be running:

| Schema | Page | Requires | Tables |
|--------|------|---------|--------|
| **Mautic** (marketing automation) | [mautic.md](databases/mautic.md) | MySQL (`mysql8`) | ~108 |
| **Gitea** (Git service) | [gitea.md](databases/gitea.md) | PostgreSQL (`pg16`) | ~112 |
| **AdventureWorks** (Microsoft sample) | [adventureworks.md](databases/adventureworks.md) | SQL Server | 12 (LT) / 71 (full) |
| **Chinook** (music store) | [chinook.md](databases/chinook.md) | SQL Server | 11 |
| **Oracle HR / CO** (Oracle sample) | [oracle-hr.md](databases/oracle-hr.md) | Oracle XE | 7 + 7 |

---

## Using these databases with the DDL tests

The DDL round-trip tests (`tests/test_ddl_roundtrip.py`) run entirely on parsed DDL strings — no live database required.  The local databases are useful for:

1. **Validating emitted DDL** — run `emit_ddl(schema, dialect=…)` output against the real engine to confirm it executes without errors.
2. **Capturing real DDL** — extract `CREATE TABLE` from a running database and feed to `parse_ddl()` to verify the parser on production schemas.
3. **End-to-end synthetic data** — generate a DataFrame with `build_dataframe_from_canonical()` and write to a real table to verify types and constraints are accepted.

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

tables = parse_ddl(ddl_string)
print(emit_ddl(tables[0], dialect="mysql"))
```

---

## Performance notes

- **PostgreSQL / MySQL / MariaDB / CockroachDB** via Podman: native ARM64.
- **SQL Server / Oracle / Db2** via Lima + QEMU x86_64: typically 3–5× slower than native x86_64.

---

## Treating containers and VMs as disposable

```bash
# Podman containers
podman rm -f pg14 pg16 mysql57 mysql8 mariadb1011 mariadb114 crdb-single

# Lima VMs
limactl delete sqlserver22 && limactl start --name=sqlserver config/lima/sqlserver.yaml
limactl delete oracle      && limactl start --name=oracle      config/lima/oracle.yaml
limactl delete db2         && limactl start --name=db2         config/lima/db2.yaml
```

---

## References

- [Lima project](https://lima-vm.io)
- [Podman](https://podman.io) / [Podman Desktop](https://podman-desktop.io)
- [SQL Server on Linux docs](https://learn.microsoft.com/en-us/sql/linux/)
- [SQL Server ARM64 feature request (open, no ETA)](https://github.com/microsoft/mssql-docker/issues/864)
- [Rosetta 2 phase-out announcement](https://arstechnica.com/gadgets/2025/06/apple-details-the-end-of-intel-mac-support-and-a-phaseout-for-rosetta-2/)
- [gvenzl/oci-oracle-xe Docker Hub image](https://hub.docker.com/r/gvenzl/oracle-xe) — community Oracle XE image; anonymous pull
- [Azure Data Studio](https://azure.microsoft.com/en-us/products/data-studio)
- [`config/lima/sqlserver.yaml`](../config/lima/sqlserver.yaml) — Lima VM config for SQL Server
- [`config/lima/oracle.yaml`](../config/lima/oracle.yaml) — Lima VM config for Oracle XE
- [`config/lima/db2.yaml`](../config/lima/db2.yaml) — Lima VM config for IBM Db2 CE
- [`docs/testing.md`](testing.md) — local Python test strategy
- [`docs/adding-a-database.md`](adding-a-database.md) — contributor guide for adding a new database
