# Deprecate os.environ lookups — audit and migration plan

Goal: all runtime configuration lives in `statschema.yaml` profiles.
No production code (`src/`) should read `os.environ` directly except for the
two bootstrap cases documented in **Keep as env-only** below.

---

## 1 — What `os.environ` reads exist today in `src/`

### 1a — `loader_context.py` · `_ctx_from_dict` / `from_env()`

These are the `STATSCHEMA__*` deployment/topology vars.  Every one already
has a 1-to-1 field on `ConnectionProfile`.

| Env var | `ConnectionProfile` field |
|---------|--------------------------|
| `STATSCHEMA__TOPOLOGY` | `topology` |
| `STATSCHEMA__LOADER` | `loader` |
| `STATSCHEMA__SERVER_VERSION` | `version` |
| `STATSCHEMA__SERVER_IS_EMULATED` | `server_is_emulated` |
| `STATSCHEMA__CLIENT_STAGING_DIR` | `client_staging_dir` |
| `STATSCHEMA__SERVER_STAGING_DIR` | `server_staging_dir` |
| `STATSCHEMA__ORACLE__DIRECTORY` | `oracle_directory` |
| `STATSCHEMA__ORACLE__SQLLDR_BINARY` | `oracle_sqlldr_binary` |
| `STATSCHEMA__CLOUD_STAGING_URI` | `cloud_staging_uri` |

Status: **fully covered**.  `from_env()` / `_ctx_from_dict` are the
runtime fallback when no profile is passed.  Once `--profile` is wired into
the CLI (see §3), `from_env()` becomes dead code for production use and can
be removed in a follow-up.

---

### 1b — `cli.py` · `_connect()` — per-dialect connection env vars

These are the **main gap**.  The CLI has no `--profile` option; when `--dsn`
is omitted it reads raw env vars to build a connection.

| Env var(s) | Dialect | Equivalent profile fields |
|-----------|---------|--------------------------|
| `STATSCHEMA_PG_DSN` | postgres / cockroachdb / neon | `host`, `port`, `database`, `username`, `password` |
| `STATSCHEMA_MYSQL_HOST/PORT/USER/PASS/DB` | mysql / mariadb | same 5 fields |
| `STATSCHEMA_SQLSERVER_DSN` | sqlserver | `host`, `port`, `database`, `username`, `password` |
| `STATSCHEMA_ORACLE_DSN` + `STATSCHEMA_ORACLE_USER/PASS` | oracle | `host`, `port`, `database` (service), `username`, `password` |
| `STATSCHEMA_DB2_DSN` | db2 | `host`, `port`, `database`, `username`, `password` |
| `STATSCHEMA_LAKEBASE_ENDPOINT` / `ENDPOINT_NAME` | lakebase | **no field yet** — see gap below |
| `STATSCHEMA_LAKEBASE_HOST` / `PGHOST` | lakebase | `host` |
| `STATSCHEMA_LAKEBASE_DB` / `PGDATABASE` | lakebase | `database` |
| `STATSCHEMA_LAKEBASE_USER` / `PGUSER` / `DATABRICKS_CLIENT_ID` | lakebase | `username` |
| `PGPORT` | lakebase | `port` |
| `STATSCHEMA_SQLITE_PATH` | sqlite | trivial / test only |

**Gap A — `endpoint` field missing from `ConnectionProfile`.**
Lakebase requires an endpoint resource path
(`projects/<p>/branches/<b>/endpoints/<e>`) for the OAuth token exchange in
`_lakebase_connect()`.  There is no `endpoint` field on the dataclass.

Fix: add `endpoint: Optional[str] = None` to `ConnectionProfile` and wire it
through `from_profile()` → `_connect_from_profile()`.

---

### 1c — `dialects/oracle/loader.py` · `_oracle_sqlldr_userid()`

```python
def _oracle_sqlldr_userid() -> str:
    user = os.environ.get("ORACLE_USER", "system")
    pwd  = os.environ.get("ORACLE_PASS", "oracle")
    host = os.environ.get("ORACLE_HOST", "localhost")
    port = os.environ.get("ORACLE_PORT", "1521")
    svc  = os.environ.get("ORACLE_SERVICE", "XE")
    return f"{user}/{pwd}@//{host}:{port}/{svc}"
```

This function is called by `OracleSqlldrLoader.bulk_load()`.  It has no access
to the connection profile.  The credentials it needs are already in the profile
(`host`, `port`, `database`, `username`, `password`).

`OracleSqlldrLoader.can_use()` also reads `ORACLE_SQLLDR_BINARY` from env as
a check: `os.environ.get("ORACLE_SQLLDR_BINARY", "")`.  The canonical source
is `ctx.oracle_sqlldr_binary` (profile field `oracle_sqlldr_binary` /
env var `STATSCHEMA__ORACLE__SQLLDR_BINARY`).

**Gap B — `_oracle_sqlldr_userid()` reads flat `ORACLE_*` env vars instead of
the profile.**  Fix: pass `host`, `port`, `database`, `username`, `password`
into `OracleSqlldrLoader.bulk_load()` from the connection object or from a
credential bag passed via `ctx`.  Remove `_oracle_sqlldr_userid()`.

**Gap C — `can_use()` reads `ORACLE_SQLLDR_BINARY` directly from env** instead
of relying solely on `ctx.oracle_sqlldr_binary`.  Fix: remove the `os.environ`
fallback in `can_use()`; the profile is the only source.

---

### 1d — `loader_context.py` · `from_env()` — secrets bootstrap (Gap F)

These three vars drive `from_env()` / `DatabricksSecretProvider`, which fetches
a JSON blob from one Databricks secret and inflates it into all credentials.
That pattern is redundant: the YAML profile system already supports per-field
Databricks secret lookups inline via the `${secrets:…}` OmegaConf resolver.

**Semantics of the resolver:**

```
${secrets:scope,key}            → fetch the secret string at scope/key
${secrets:scope,key,field}      → fetch the secret, parse as JSON, return field
${secrets:scope,key,field1,f2}  → nested JSON path (field1 → field2)
```

- `secrets` = Databricks Secrets (the storage service)
- `scope` = scope name within Databricks Secrets
- `key` = key name within that scope

**Replacement:** use inline interpolation in the YAML profile instead of the
batch-load env vars.

```yaml
# Before (env vars + batch-load pattern)
# STATSCHEMA_SECRETS_SCOPE=my_scope
# STATSCHEMA_SECRETS_KEY=mydb_credentials_json

# After (inline in statschema.yaml)
profiles:
  prod_postgres:
    host:     ${secrets:my_scope,mydb_credentials_json,host}
    database: ${secrets:my_scope,mydb_credentials_json,database}
    username: ${secrets:my_scope,mydb_credentials_json,username}
    password: ${secrets:my_scope,mydb_credentials_json,password}
    # or for a plain-string secret:
    password: ${secrets:my_scope,pg_password}
```

**Consequence:** once `--profile` is wired into the CLI (Step 2 below),
`from_env()`, `DatabricksSecretProvider`, and `EnvCredentialProvider` all
become dead code and can be deleted.  `CredentialProvider` base class and
`from_providers()` go with them.

| Env var | Status | Replacement |
|---------|--------|-------------|
| `STATSCHEMA_SECRETS_SCOPE` | Remove | Hardcode scope inline: `${secrets:my_scope,…}` |
| `STATSCHEMA_SECRETS_KEY` | Remove | Hardcode key inline: `${secrets:…,my_key,…}` |
| `STATSCHEMA_SECRETS_BACKEND` | Remove | Databricks is the only supported backend; resolver handles it |
| `DATABRICKS_HOST` | Remove | Was only read in `_ctx_from_dict` to set `is_databricks=True`. `from_profile()` now infers `is_databricks` from `dialect: databricks / lakehouse` directly — no env read needed. |

---

## 2 — Vars acceptable to keep as env-only

None.  The only candidate was `DATABRICKS_HOST`, but it turns out it was never
needed.  See below.

---

## 3 — Migration plan

### Step 1 — Add `endpoint` to `ConnectionProfile`  (Gap A)

```python
# connection_profile.py
endpoint: Optional[str] = None   # Lakebase OAuth endpoint resource path
```

Add to `from_profile()` / `DeploymentContext` wiring and `statschema.example.yaml`.

### Step 2 — Add `--profile` option to `collect` and `load` CLI commands

```
statschema collect --profile prod_postgres
statschema load    schema.yaml --profile dev_sqlserver_bulk_insert
```

`cmd_collect` and `cmd_load` call `load_profile(yaml_path, profile_name)` →
`DeploymentContext.from_profile(profile)` instead of `_connect(dialect, dsn)`
+ `from_env()`.  The dialect is read from the profile.

The existing `--dialect` / `--dsn` flags stay as a convenience shorthand but
are not the primary path.

### Step 3 — Fix `OracleSqlldrLoader` (Gaps B and C)

Pass Oracle credentials into `bulk_load()` via `ctx` (add a `credentials`
bag to `DeploymentContext`, or pass them as keyword args).  Remove
`_oracle_sqlldr_userid()` and the `ORACLE_*` env reads.

Remove the `ORACLE_SQLLDR_BINARY` env read from `can_use()`; gate solely on
`ctx.oracle_sqlldr_binary`.

### Step 4 — Delete `from_env()` and the credential-provider chain

Once `--profile` is the primary path and the YAML inline `${secrets:…}`
resolver handles all secret lookups, the following code is dead and should
be deleted:

- `DeploymentContext.from_env()`
- `DeploymentContext.from_providers()`
- `DeploymentContext._ctx_from_dict()`
- `CredentialProvider` base class
- `EnvCredentialProvider`
- `DatabricksSecretProvider`

The only remaining constructor paths are `from_profile()` and
`from_spark_session()`.  `_ctx_from_dict` tests in `test_connection_profile.py`
are deleted along with the class method.

### Step 5 — Remove per-dialect CLI env vars

Remove `STATSCHEMA_PG_DSN`, `STATSCHEMA_MYSQL_*`, `STATSCHEMA_SQLSERVER_DSN`,
`STATSCHEMA_ORACLE_*`, `STATSCHEMA_DB2_DSN`, and the Lakebase `STATSCHEMA_LAKEBASE_*`
reads from `cli.py` once `--profile` handles all dialects.

---

## 4 — Gap summary table

| Gap | Location | What's missing | Fix |
|-----|----------|---------------|-----|
| A | `ConnectionProfile` | No `endpoint` field for Lakebase | Add `endpoint: Optional[str]` |
| B | `oracle/loader.py` `_oracle_sqlldr_userid()` | Reads `ORACLE_USER/PASS/HOST/PORT/SERVICE` from env | Pass credentials through `ctx` |
| C | `oracle/loader.py` `can_use()` | Reads `ORACLE_SQLLDR_BINARY` from env as fallback | Remove fallback; use `ctx.oracle_sqlldr_binary` only |
| D | `cli.py` | No `--profile` option; per-dialect `STATSCHEMA_*` env vars are the primary path | Add `--profile`; deprecate env fallbacks |
| E | `cli.py` `cmd_load` | `from_env()` called unconditionally | Replace with `from_profile()` when `--profile` is present |
| F | `loader_context.py` `from_env()` | `STATSCHEMA_SECRETS_SCOPE/KEY/BACKEND` batch-load pattern | Replace with inline `${secrets:scope,key,field}` in YAML; delete `from_env()` and the provider chain |

---

## 5 — Vars deliberately NOT migrated to YAML

| Var | Reason |
|-----|--------|
| Test vars (`PG_PASSWORD`, `MYSQL_ROOT_PASS`, etc.) | Only in `tests/` and `.env`; never in `src/` |
| `JAVA_HOME`, `PATH`, `PYSPARK_*` | JVM / Spark infrastructure; not statschema config |
| `BENCH_PG_DSN`, `BENCH_SQLSERVER_DSN` | Benchmark harness only; never in `src/` |
