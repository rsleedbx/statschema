# Adding a new database (NeonDB, CockroachDB, …)

Use this checklist when integrating a new engine as **source/target** for DDL, stats, or live tests. **NeonDB** is wired as Postgres-compatible aliases plus `tests/test_live_neon.py`; **CockroachDB** should follow the same steps.

---

## 1. Read upstream source

- Clone or browse the **official GitHub repo** and product docs.
- **DDL:** Are `CREATE TABLE` types and constraints the same as an existing dialect (e.g. Postgres wire protocol), or do they need a new sqlglot path and emitter rules?
- **Statistics:** Where do optimizer stats live? (`pg_catalog` / `information_schema` / vendor tables.) For injection, mirror patterns in `stats_injector.py` and `db_stats_collector.py`.

Capture anything that breaks `parse_ddl` or `emit_ddl` round-trips so tests can lock behavior.

---

## 2. Implement dialect support in `src/statschema`

- **Alias of existing engine:** Add entries in `ddl_emitter.py` (`_DIALECT_ALIASES`), `ddl_parser.py` (normalize before `_SG_DIALECT` lookup), `loader.py` (`get_schema_source_from_data`, `format_hint`), `db_stats_collector.py`, `override_model.py` as needed. Add unit tests in `tests/test_ddl_roundtrip.py` / `tests/test_schema_parser.py`.
- **New grammar:** Extend `ddl_parser.py` and `ddl_emitter.py`, reserved words, type maps; add parametrized cases in `test_ddl_roundtrip.py`.

---

## 3. Podman (or Lima) setup

- Add a subsection under [`docs/local-databases.md`](local-databases.md): image name (Docker Hub / ghcr.io), **ports**, **environment variables**, start/stop commands, and arch notes (ARM vs x86_64).
- Prefer images that match how developers run the DB in production; document when the image is a **proxy** (e.g. Neon Local) vs a full server.

---

## 4. Live test module

- Add `tests/test_live_<engine>.py` following `tests/test_live_pg.py`:
  - `pytest.importorskip` for the DB driver.
  - **`pytest.skip`** on connection failure so `make test` stays green without the container.
  - Parse sample DDL → `emit_ddl(dialect=...)` → execute → introspect via `information_schema` (or equivalent).
- Optional: stats collect/inject tests once collector/injector support exists.

---

## 5. Makefile and config

- Add `test-live-<engine>` in [`Makefile`](../Makefile) with any `$(or $(VAR),default)` overrides.
- Append the test file to **`test-live-all`** when the suite should run in “everything up” runs.
- Extend [`.env.example`](../.env.example) with connection variables.
- Update [`docs/testing.md`](testing.md): Makefile table, suite structure table, skip-condition table, references.

---

## 6. Run tests

```bash
make venv-test          # once
make test               # full suite; live tests skip if DBs down
make test-live-<engine> # only the new module (container running)
make test-live-all      # all live modules (all containers + .env)
```

CI: keep `make test` as the default job; run `test-live-*` only when secrets and services are available.

---

## Engine-specific notes

| Engine | Status | Notes |
|--------|--------|--------|
| **NeonDB** | Done | Aliases `neon` / `neondb` → Postgres DDL path; live tests use [`neondatabase/neon_local`](https://hub.docker.com/r/neondatabase/neon_local), not `neondatabase/neon` binaries image. Live connections default to **`sslmode=require`**; prefer **`NEON_DATABASE_URL`** from the console if discrete host/port settings fail. |
| **CockroachDB** | Planned | Postgres-wire compatible for many apps; verify DDL + `information_schema` differences in upstream docs and add `cockroach` dialect or aliases as needed. |

---

## References

- [`docs/testing.md`](testing.md) — test layout and Makefile targets
- [`docs/local-databases.md`](local-databases.md) — local container recipes
