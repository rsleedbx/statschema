# Adding a new database

Use this checklist when integrating a new engine as **source/target** for DDL, stats, or live tests.

---

## 0. Pre-checks (before writing any code)

Run these four checks before implementation.

### 0a. sqlglot dialect support

```python
import sqlglot
print("db2" in sqlglot.dialects.Dialects.__members__)  # False → custom emitter needed
```

| Result | What it means |
|--------|---------------|
| `True` | Use the standard `parse_ddl` / `emit_ddl` path. Add an entry in `SQLGLOT_DIALECT`. |
| `False` | Write a hand-written emitter in `ddl_emitter.py`. Use `""` (ANSI SQL) as the sqlglot parse dialect. See §3a. |

### 0b. Python driver ARM64 availability

Check if a `macosx_14_0_arm64` wheel exists on PyPI.  If it does, the driver runs natively on Apple Silicon:

```bash
pip index versions <driver-package>   # look for macosx_14_0_arm64 in wheel filenames
# or just try:
pip install <driver-package>
```

| Result | What it means |
|--------|---------------|
| ARM64 wheel available | Tests run from the macOS host directly; no Lima needed for the driver. |
| No ARM64 wheel | Either run tests inside the Lima VM, or use an alternative driver (JDBC via `jaydebeapi`, etc.). |

### 0c. Container ARM64 availability

```bash
docker manifest inspect <image>:<tag> | grep architecture
```

| Result | What it means |
|--------|---------------|
| `arm64` present | Use Podman directly on macOS (no Lima). |
| Only `amd64` / `x86_64` | Use Lima + QEMU. See §3b. |

### 0d. Wire protocol compatibility

Does this engine speak Postgres wire protocol?  MySQL wire protocol?  If so, aliasing may be


| Protocol | Action |
|----------|--------|
| Postgres-compatible | Add aliases → `"postgres"` in `DIALECT_ALIASES` + `SCHEMA_SOURCE_POSTGRES_ALIASES`. |
| MySQL-compatible | Add aliases → `"mysql"` in `DIALECT_ALIASES` + `SCHEMA_SOURCE_MYSQL_ALIASES`. |
| Own protocol | Add a new canonical dialect and potentially a custom emitter (see §3a). |

---

## 1. Read upstream docs

- Browse official docs and source repo.
- **DDL:** Is `CREATE TABLE` syntax Postgres-compatible (aliases suffice) or does it need a new
  sqlglot path and emitter?
- **Statistics:** Where do optimizer stats live? (`pg_catalog` / `information_schema` / vendor
  tables.)
- Capture any `parse_ddl` / `emit_ddl` round-trip breakages so tests can lock behaviour.

---

## 2. Register the dialect

All alias and normalization logic lives in **[`src/statschema/dialect_registry.py`](../src/statschema/dialect_registry.py)**.

| What to add | Where in `dialect_registry.py` |
|-------------|-------------------------------|
| Vendor names → canonical dialect | `DIALECT_ALIASES` |
| Config tags that resolve to `SchemaSource.POSTGRES` | `SCHEMA_SOURCE_POSTGRES_ALIASES` |
| Config tags that resolve to `SchemaSource.MYSQL` | `SCHEMA_SOURCE_MYSQL_ALIASES` |
| Config tags for a genuinely new canonical dialect | Add `SchemaSource.<NEW>` to `loader.py` and a new `SCHEMA_SOURCE_<NEW>_ALIASES` frozenset |
| New canonical dialect name | `CANONICAL_DIALECTS` tuple |
| New canonical dialect's sqlglot parse name | `SQLGLOT_DIALECT` — use `""` if sqlglot has no support |

**Alias-only example (CockroachDB → postgres):**

```python
# DIALECT_ALIASES
"cockroach":   "postgres",
"cockroachdb": "postgres",
"crdb":        "postgres",

# SCHEMA_SOURCE_POSTGRES_ALIASES
{"postgresql", "pg", "neon", "neondb", "cockroach", "cockroachdb", "crdb"}
```

**New canonical dialect example (Db2 — no sqlglot support):**

```python
# CANONICAL_DIALECTS
CANONICAL_DIALECTS = (..., "db2")

# DIALECT_ALIASES
"ibmdb2":  "db2",
"db2luw":  "db2",
"dashdb":  "db2",

# SQLGLOT_DIALECT — empty string = ANSI SQL fallback
"db2": "",

# loader.py — add SchemaSource.DB2 = "db2" to the enum
# SCHEMA_SOURCE_DB2_ALIASES — for config tags that aren't already canonical
{"ibmdb2", "ibm_db2", "db2luw", "dashdb"}
```

After updating `dialect_registry.py`, add unit tests in `tests/test_schema_parser.py` to lock the alias behaviour.

---

## 3a. Custom DDL emitter (when sqlglot has no dialect support)

When the pre-check at §0a returns `False`, add a hand-written emitter to
[`src/statschema/ddl_emitter.py`](../src/statschema/ddl_emitter.py).

**Steps:**

1. Add a `_<ENGINE>_DEFAULTS: dict[str, str]` mapping every canonical type to its SQL type string.
2. Add the dialect name to `_DEFAULT_MAPS`.
3. Add identifier quoting: `_BACKTICK_DIALECTS`, `_BRACKET_DIALECTS`, or `_DQUOTE_DIALECTS`.
4. Extend `_build_col_type` for length/binary/decimal cases specific to the engine.
5. Extend `_auto_increment_clause` if the engine uses non-standard identity syntax.
6. Extend `_normalize_default` if the engine's boolean literals differ from SQL-standard.
7. Suppress NOT NULL after identity syntax if the engine makes it implicit (Oracle, Db2).
8. Write `_emit_<engine>(table, if_not_exists) -> str`.
9. Register in `_EMITTERS` and update the docstring's "Supported target dialects" list.

**Template:**

```python
_MYDB_DEFAULTS: dict[str, str] = {
    "integer":   "INTEGER",
    "long":      "BIGINT",
    "string":    "TEXT",         # or CLOB / VARCHAR(MAX) / STRING — pick engine default
    ...
}

def _emit_mydb(table: CanonicalTableSchema, if_not_exists: bool) -> str:
    ine   = " IF NOT EXISTS" if if_not_exists else ""
    tname = _quote(table.name, "mydb")
    lines = [_col_ddl(c, "mydb") for c in table.columns]
    pk    = _primary_key_constraint(table, "mydb")
    if pk:
        lines.append(pk)
    body  = ",\n".join(lines)
    return f"CREATE TABLE{ine} {tname} (\n{body}\n);"
```

---

## 3b. Podman or Lima setup

Create **`docs/databases/<engine>.md`** using the existing files as templates
([`postgres.md`](databases/postgres.md), [`oracle.md`](databases/oracle.md),
[`db2.md`](databases/db2.md)), then add a row to the strategy table in
[`docs/local-databases.md`](local-databases.md).

The per-database file should contain:

- Image name (Docker Hub / ghcr.io / ICR), ports, environment variables.
- Start/stop/delete commands (self-contained — no cross-referencing required).
- **Arch note**: does an ARM64 image exist?  If not, add `config/lima/<engine>.yaml` (use
  [`config/lima/oracle.yaml`](../config/lima/oracle.yaml) or
  [`config/lima/db2.yaml`](../config/lima/db2.yaml) as templates).
- Type normalizations table (what introspection returns vs what was emitted).
- Troubleshooting section for the two or three most common startup failures.

If the engine has multiple topologies (e.g. CockroachDB single-node vs multi-region), give
each topology its own sub-section with self-contained start commands and a separate port range.

---

## 4. Live test module

Add `tests/test_live_<engine>.py`:

- `pytest.importorskip` for the DB driver at the top of each connection helper.
- `pytest.skip` on connection failure (never `pytest.fail`), so `make test` stays green.
- Parametrize over versions/topologies where relevant.
- Parse sample DDL → `emit_ddl(dialect=...)` → execute → introspect via the engine's catalog
  view (`information_schema.COLUMNS`, `SYSCAT.COLUMNS`, `ALL_TAB_COLUMNS`, etc.).
- Include at least one test that verifies the dialect alias (`dialect='ibmdb2'` etc.) round-trips.
- Include at least one test that verifies `schema_source: <alias>` resolves via `load_schema`.
- Document type differences in the module docstring.

**Common assertion pattern:**
```python
# Allow all observed values when the round-trip normalizes the type.
# e.g. LONGTEXT and TEXT both map to canonical "string" and emit back as TEXT.
assert cols["c_text"]["data_type"] in ("text", "longtext")
```
> Run the tests against a live database before finalising assertions — do not assume what
> `information_schema` will report.  Type names frequently differ from what was declared.

---

## 5. Makefile and config

- Add `test-live-<engine>` target with `$(or $(VAR),default)` port/password overrides.
- Add `DB2`-style env vars in the `test-live-all` block.
- Append `tests/test_live_<engine>.py` to the `test-live-all` file list.
- Add connection variables to [`.env.example`](../.env.example).
- Update [`docs/testing.md`](testing.md): Makefile target table, test suite structure table,
  skip-condition table, and the file reference list at the bottom.

---

## 6. Run tests

```bash
make venv-test                   # once per clone — always rebuild; never copy .venv across repos
make test                        # full suite; live tests skip if DBs are down
make test-live-<engine>          # only the new module (container/VM running)
make test-live-all               # all live modules
```

`make test` uses `$(PYTHON_TEST) -m pytest`.

CI: keep `make test` as the default job; run `test-live-*` only when containers/secrets are available.

---

## Engine-specific notes

| Engine | Status | Notes |
|--------|--------|--------|
| **NeonDB** | Done | Aliases `neon` / `neondb` → Postgres DDL path. Live tests use [`neondatabase/neon_local`](https://hub.docker.com/r/neondatabase/neon_local) (cloud proxy), not `neondatabase/neon` (storage binaries). `sslmode=require` by default; prefer `NEON_DATABASE_URL` if discrete host/port settings fail. |
| **CockroachDB** | Done | Aliases `cockroach` / `cockroachdb` / `crdb` → Postgres DDL path. Two Podman topologies: single-node (port 26257) and 3-node multi-region (ports 26267–26269). `INT` is 64-bit (`int8`) in CRDB vs 32-bit in Postgres. `BYTEA` is accepted but `information_schema` reports `bytes`. Multi-region tests cover `LOCALITY GLOBAL`, `REGIONAL BY TABLE`, and `REGIONAL BY ROW`. |
| **MariaDB** | Done | Aliases `mariadb` / `maria` / `mariadb_columnstore` → MySQL DDL path. Two Podman versions: 10.11 LTS (port 3310) and 11.4 (port 3311). `JSON` and `LONGTEXT` both round-trip to `text` in `information_schema` (canonical model emits TEXT). |
| **IBM Db2 CE** | Done | New canonical dialect `db2`. **sqlglot has no Db2 dialect** — custom emitter in `ddl_emitter.py`; ANSI SQL parsing. **No ARM64 container image** — Lima + QEMU required. `ibm_db` Python driver has ARM64 macOS wheels so tests run from the host. `CLOB` for unbounded strings, `BLOB` for binary, `BOOLEAN` native (11.1+). Introspect via `SYSCAT.COLUMNS` (uppercase type names). |

---

## References

- [`src/statschema/dialect_registry.py`](../src/statschema/dialect_registry.py) — single source of truth for dialect aliases
- [`src/statschema/ddl_emitter.py`](../src/statschema/ddl_emitter.py) — per-dialect DDL emitters (add custom emitters here)
- [`docs/testing.md`](testing.md) — test layout and Makefile targets
- [`docs/local-databases.md`](local-databases.md) — local database index (links to per-database setup pages)
- [`docs/databases/`](databases/) — per-database setup pages (postgres, mysql, mariadb, cockroachdb, sqlserver, oracle, db2, neon)
