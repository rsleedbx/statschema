---
name: databricks-connect-notebook
description: Create and test Python notebooks that use Databricks Connect (spark, dbutils from remote workspace). Use when adding or editing notebooks that run against Databricks from a local IDE (e.g. Cursor), or when the user asks how to create or test Python notebooks with Databricks Connect.
---

# Create and Test Python Notebooks with Databricks Connect

## When to Use This

- Building a `.ipynb` that uses `spark` or `dbutils` against a remote Databricks workspace from the local IDE.
- Testing notebook logic locally before running the same code in a Databricks job or workspace notebook.

## Creating a Notebook

### 1. Rely on Injected `spark` and `dbutils`

The **Spark session** is **provided by Databricks Connect itself**. Do not call `SparkSession.builder` in the notebook. With the Databricks extension or a kernel that uses `databricks-connect`, the session is injected as `spark` (and `dbutils`).

```python
# In cell 1 – no SparkSession.builder
print(spark)
spark.sql("SELECT current_catalog(), current_schema()").show()
```

### 2. Pass `spark` Into Helper Modules

Keep the notebook as the driver; pass `spark` into project code. Do **not** instantiate `SparkSession` inside `src/`; accept it as an argument.

```python
from src.schema_parser.dbldatagen_builder import build_dataframe_from_canonical
df = build_dataframe_from_canonical(spark, table, rows=1000, seed=42)
```

### 3. Cell Order

- **First cell**: Walk-up root detection + verify connection (see below).
- **Second cell**: `%load_ext autoreload` / `%autoreload 2` so edits to `src/` are picked up.
- **Remaining cells**: Config load → generate → convert → ingest / validate, in order.

### 4. Dependencies

- `databricks-connect>=14.0.0` in `requirements.txt` — the kernel uses it to inject `spark`.
- **The kernel's venv must have all deps installed.** `dbldatagen` requires `jmespath`, `pyparsing`, `numpy`, `pandas` (not always declared). Fix: install the full `requirements.txt` into the kernel's venv.

---

## Authentication and `~/.databrickscfg` Setup

One login command covers **both** Databricks Connect (Spark) and the Databricks SDK (`WorkspaceClient`) — no separate service principal needed.

### 1. Install the Databricks CLI

```bash
brew tap databricks/tap && brew install databricks
```

### 2. Log in with OAuth + serverless

```bash
# If host is already in ~/.databrickscfg:
databricks auth login --configure-serverless

# First-time or named profile:
databricks auth login --configure-serverless --host https://<your-workspace>.azuredatabricks.net
```

The resulting `~/.databrickscfg`:

```ini
[DEFAULT]
host                  = https://<your-workspace>.azuredatabricks.net
serverless_compute_id = auto
auth_type             = databricks-cli
```

`serverless_compute_id = auto` — no cluster ID needed. To use a named profile: `export DATABRICKS_CONFIG_PROFILE=myprofile`

### 3. How the same credentials are reused

| Component | How it uses `~/.databrickscfg` |
|-----------|-------------------------------|
| **Databricks Connect** (`DatabricksSession`) | Reads `host` + OAuth token automatically |
| **Databricks SDK** (`WorkspaceClient()`) | Reads the same file; `w.config.authenticate()` returns a fresh Bearer token |
| **ZeroBus ingest** (`IngestConfig.from_workspace_client()`) | Uses `WorkspaceClient` to get the token; no service principal needed |

---

## How the Agent Can Run the Notebook and See Errors

### 1. Run the runner script (primary method)

```bash
cd <repo_root>
.venv_3_11/bin/python notebooks/run_generate_data.py
```

Confirmed working output (DatabricksSession on serverless):
```
Project root: /Users/robert.lee/github/zerobus
Spark: DatabricksSession (remote)
Loaded 4 table(s): ['actor', 'category', 'country', 'city']
actor: 1000 rows, 6 columns
category: 1000 rows, 4 columns
country: 1000 rows, 4 columns
city: 1000 rows, 6 columns
```

The script tries Spark in priority order:
1. `DatabricksSession.builder.getOrCreate()` — uses `~/.databrickscfg`
2. Local `SparkSession.builder.master("local[1]")` — fallback when `databricks-connect` absent
3. Schema-only mode — prints column specs, no DataFrame built

### 2. Run pytest

**Primary (local dev with credentials):** `.venv_3_11` has `databricks-connect`; tests use `DatabricksSession` against serverless:

```bash
.venv_3_11/bin/pip install -r requirements-dev.txt  # once (adds pytest to the Connect venv)
.venv_3_11/bin/python -m pytest tests/ -q
# → all passed, 2 skipped  (2 skips = integration tests needing real ZeroBus endpoint)
# requires ~/.databrickscfg
```

**CI (no credentials):** `.venv_test` omits `databricks-connect` so Spark tests fall back to an in-process local session:

```bash
python -m venv .venv_test
.venv_test/bin/pip install -r requirements-test.txt
.venv_test/bin/python -m pytest tests/ -q
# → 176 passed, 2 skipped
```

### 3. Read the notebook file to see the user's errors

When the user reports an error, read the `.ipynb` JSON:

- `cells[i].outputs` with `"output_type": "error"` → traceback.
- First cell's `outputs` → what `root` and `spark` resolved to.

### 4. nbconvert limitations

`jupyter nbconvert --execute` runs in a plain kernel — `spark` is undefined. Use the runner script for end-to-end runs. Use nbconvert only for execution-order debugging.

**Clearing outputs:** clear only code cells (not markdown cells — nbformat forbids `execution_count`/`outputs` on them). Stream outputs must include `"name": "stdout"`.

---

## Resolving Project Root in the Notebook

`Path.cwd()` is often `notebooks/` when the kernel starts. Use the walk-up approach in the first code cell:

```python
cwd = Path.cwd()
root = cwd
while root != root.parent:
    if (root / "src").is_dir() and (root / "tests").is_dir():
        break
    root = root.parent
if str(root) not in sys.path:
    sys.path.insert(0, str(root))
print("Project root:", root)
```

---

## ZeroBus Ingest Auth (No Service Principal Needed)

Use `WorkspaceClient` from `databricks-sdk` — same `~/.databrickscfg` as Connect:

```python
from src.zerobus_ingest import IngestConfig, ingest_dataframe

# Session OAuth — WorkspaceClient handles token refresh
config = IngestConfig.from_workspace_client("main.default.my_table")

# Short-lived PAT (auto-expires in 5 min, no cleanup needed) — useful for tests
config = IngestConfig.from_workspace_client("main.default.my_table", lifetime_seconds=300)

result = ingest_dataframe(df, table, config)
print(f"Ingested {result.rows_sent} rows")
```

Only `ZEROBUS_SERVER_ENDPOINT` is required (see `docs/implementation.md § ZeroBus Endpoint Format`).

---

## Common Pitfalls

- **Creating SparkSession in notebook or `src/`** — Don't. Use the injected `spark`.
- **Kernel sees wrong project root** — Re-run the first cell or Run All. The walk-up logic fixes it.
- **`ModuleNotFoundError: No module named 'jmespath'` (or `numpy`, `pandas`)** — The kernel's venv is missing deps. Run: `.venv_3_11/bin/pip install -r requirements.txt`
- **`AssertionError: colType is not instance of DataType`** — `dbldatagen` needs instances (`IntegerType()`), not classes (`IntegerType`). Fixed in `dbldatagen_builder.py`.
- **`ValueError: time data '...' does not match format '%Y-%m-%d %H:%M:%S'`** — Timestamp `begin`/`end` must include time component: `"2024-01-01 00:00:00"`. Fixed in `dbldatagen_builder.py`.
- **`HeadersProvider.__new__() takes 0 positional arguments`** — Rust Pyo3 `__new__` rejects forwarded constructor args. Fix: define `__new__(cls, *a, **kw): return HeadersProvider.__new__(cls)` in the subclass.
- **pytest Spark tests skip** — Only happens if `~/.databrickscfg` is missing and `databricks-connect` is installed (blocks local Spark). Use `.venv_test` (CI venv without Connect) or configure credentials.

## Project Conventions (This Repo)

- Notebooks live in `notebooks/`; runner scripts mirror each notebook (e.g. `run_generate_data.py`).
- Pipeline steps are in `src/` and accept `spark` as an argument.
- Config is YAML; load in the notebook, pass to `src` functions.
- See `docs/implementation.md` for pipeline phases and `docs/testing.md` for test strategy.
