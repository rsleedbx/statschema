# Adding a new database

Use this checklist when integrating a new engine as **source/target** for DDL, stats, or live tests.

---

## 1. Read upstream source

- Browse the official docs and source repo.
- **DDL:** Is `CREATE TABLE` syntax Postgres-compatible (aliases suffice) or does it need a new sqlglot path and emitter?
- **Statistics:** Where do optimizer stats live? (`pg_catalog` / `information_schema` / vendor tables.)
- **Container:** Does an official ARM64 image exist? If not, plan for Lima + QEMU.
- **Wire protocol:** If Postgres-compatible, use `psycopg2` with the appropriate `sslmode`.

Capture any `parse_ddl` / `emit_ddl` round-trip breakages so tests can lock behaviour.

---

## 2. Register the dialect

All alias and normalization logic lives in **[`src/statschema/dialect_registry.py`](../src/statschema/dialect_registry.py)**.
No other file needs editing for an alias-only integration.

| What to add | Where in `dialect_registry.py` |
|-------------|-------------------------------|
| Vendor names → canonical dialect | `DIALECT_ALIASES` |
| Config tags that resolve to `SchemaSource.POSTGRES` | `SCHEMA_SOURCE_POSTGRES_ALIASES` |
| New canonical dialect's sqlglot name | `SQLGLOT_DIALECT` (only for a genuinely new parser path) |

**Alias-only example (CockroachDB → postgres):**

```python
# DIALECT_ALIASES
"cockroach":   "postgres",
"cockroachdb": "postgres",
"crdb":        "postgres",

# SCHEMA_SOURCE_POSTGRES_ALIASES
{"postgresql", "pg", "neon", "neondb", "cockroach", "cockroachdb", "crdb"}
```

After updating `dialect_registry.py`, add unit tests in `tests/test_ddl_roundtrip.py` or `tests/test_schema_parser.py` to lock the alias behaviour.

---

## 3. Podman (or Lima) setup

Add a section under [`docs/local-databases.md`](local-databases.md):

- Image name (Docker Hub / ghcr.io), ports, environment variables.
- Start/stop commands.
- Arch note: does an ARM64 image exist? If not, use Lima + QEMU.
- If the engine has multiple topologies (e.g. CockroachDB single-node vs multi-region), give each topology its own sub-section with self-contained start commands and a separate port range.
- Type normalizations table (what `information_schema` reports vs what Postgres reports).

---

## 4. Live test module

Add `tests/test_live_<engine>.py`:

- `pytest.importorskip` for the DB driver at the top of each connection helper.
- `pytest.skip` on connection failure (never `pytest.fail`), so `make test` stays green.
- Parametrize over topologies where relevant (single-node / multi-region, v14 / v16, etc.).
- Parse sample DDL → `emit_ddl(dialect=...)` → execute → introspect via `information_schema`.
- Add topology-specific test classes (e.g. `TestCRDBMultiRegion`) separately from common DDL tests.
- Document type differences between the engine and standard Postgres in the module docstring.

---

## 5. Makefile and config

- Add `test-live-<engine>` target in [`Makefile`](../Makefile) with `$(or $(VAR),default)` port overrides.
- Add CRDB-style env vars in the `test-live-all` block.
- Append `tests/test_live_<engine>.py` to `test-live-all`.
- Add connection variables to [`.env.example`](../.env.example).
- Update [`docs/testing.md`](testing.md): Makefile target table, test suite structure table, skip-condition table.

---

## 6. Run tests

```bash
make venv-test                   # once per clone — always rebuild; never copy .venv across repos
make test                        # full suite; live tests skip if DBs are down
make test-live-<engine>          # only the new module (container running)
make test-live-all               # all live modules
```

`make test` uses `$(PYTHON_TEST) -m pytest` (not the venv-generated `pytest` script) — this avoids shebang path issues when the repo is cloned to a different location.

CI: keep `make test` as the default job; run `test-live-*` only when containers/secrets are available.

---

## Engine-specific notes

| Engine | Status | Notes |
|--------|--------|--------|
| **NeonDB** | Done | Aliases `neon` / `neondb` → Postgres DDL path. Live tests use [`neondatabase/neon_local`](https://hub.docker.com/r/neondatabase/neon_local) (cloud proxy), not `neondatabase/neon` (storage binaries). `sslmode=require` by default; prefer `NEON_DATABASE_URL` if discrete host/port settings fail. |
| **CockroachDB** | Done | Aliases `cockroach` / `cockroachdb` / `crdb` → Postgres DDL path. Two Podman topologies: single-node (port 26257) and 3-node multi-region (ports 26267–26269). `INT` is 64-bit (`int8`) in CRDB vs 32-bit in Postgres. `BYTEA` is accepted but `information_schema` reports `bytes`. Multi-region tests cover `LOCALITY GLOBAL`, `REGIONAL BY TABLE`, and `REGIONAL BY ROW`. |
| **CockroachDB (new grammar)** | Not needed | Standard Postgres DDL executes on CockroachDB without modification. Multi-region DDL (`LOCALITY …`) is CRDB-specific syntax that is not round-tripped through the canonical model — it is applied directly in the live tests. |
| **MariaDB** | Done | Aliases `mariadb` / `maria` / `mariadb_columnstore` → MySQL DDL path. Two Podman versions: 10.11 LTS (port 3310) and 11.4 (port 3311). `JSON` stored as `longtext` in `information_schema` on MariaDB ≤ 10.4; reported as `json` from 10.5+. Native `UUID` type added in 10.7. DDL emitted with `dialect="mysql"` executes unchanged. |

---

## References

- [`src/statschema/dialect_registry.py`](../src/statschema/dialect_registry.py) — single source of truth for dialect aliases
- [`docs/testing.md`](testing.md) — test layout and Makefile targets
- [`docs/local-databases.md`](local-databases.md) — local container recipes
