# Zerobus Synthetic Data Pipeline – Testing

**Purpose**: Test scope, cases, and execution by phase. Aligns with [plan.md](plan.md) (outcomes, scaling) and [implementation.md](implementation.md) (phases, modules).

---

## Test Strategy

| Phase | Focus | Scope |
|-------|--------|--------|
| **Phase 0** | Project setup and environment | Directories, config files, dependencies, env vars |
| **Phase 1** | Minimal pipeline (1 table, 1 column, 1000 snapshot, 100 incremental) | Config loader, data generator, protobuf converter, Zerobus ingest (mocked), end-to-end pipeline |

Tests are in `tests/`. Run with `pytest` (or the project's test runner). Integration tests that hit a real Zerobus workspace are optional and typically run manually.

---

## Running the full test suite locally (including Spark)

Some tests build Spark DataFrames with **dbldatagen** and call `SparkSession.builder.master("local[1]").getOrCreate()`. If **databricks-connect** is installed in the same environment, it can override this and allow only *remote* sessions, so those tests are **skipped** with a message like *"Local Spark not available (Databricks Connect only supports remote sessions)"*.

To run **all** tests (no skips) locally:

1. **Use a separate venv** that has **pyspark** but **not** databricks-connect:
   ```bash
   python -m venv .venv_test
   .venv_test/bin/pip install -r requirements-test.txt
   .venv_test/bin/python -m pytest tests/ -v
   ```
   `requirements-test.txt` includes pyspark and the same deps as `requirements.txt` except databricks-connect, so a local in-process Spark session is created and the Spark-dependent tests run.

2. **Keep your main venv** (e.g. `.venv` or `.venv_3_11`) for notebooks and Databricks Connect; use `.venv_test` only when you want the full test run without skips.

Summary:

| Venv / deps | Use case | Spark-dependent tests |
|-------------|----------|------------------------|
| `requirements.txt` (includes databricks-connect) | Notebooks, Connect, daily dev | Skipped (remote session only) |
| `requirements-test.txt` (pyspark, no databricks-connect) | Full pytest run | Run (local Spark) |

---

## Phase 0: Project Setup

**Goal**: Confirm the project layout, config, and dependencies are in place so Phase 1 implementation can start.

### Scope

- Repository layout and required files
- Presence and validity of Phase 1 config
- Installable dependencies and (optional) import checks

### Test Artifacts / Checks

| Check | Description | How |
|-------|--------------|-----|
| **Directories exist** | `config/`, `src/`, `tests/`, `notebooks/`, `proto/`, `docs/`, `docs/learnings/` | Script or CI step (e.g. `test_project_structure.py` or Make target) |
| **Phase 1 config exists** | `config/phase1_config.yaml` present | File exists |
| **Phase 1 config valid** | YAML loads and has required keys: `pipeline.snapshot_rows`, `pipeline.incremental_rows`, `pipeline.tables` (one table, one column `col_1`, type `integer`), `pipeline.databricks` (workspace_url, workspace_id, region, catalog, schema, zerobus) | Load with config loader or PyYAML and assert structure |
| **Dependencies install** | `requirements.txt` installs without error | `pip install -r requirements.txt` (or uv/poetry equivalent) |
| **Optional: imports** | `dbldatagen`, `zerobus`, `yaml`, `grpcio_tools` (or protobuf) importable | Optional pytest or script |

### Prerequisites

- Python 3.9+
- No Spark or Databricks required for Phase 0 checks

### Run (Phase 0)

```bash
# Example: after creating a small test script or pytest for structure/config
pytest tests/test_project_structure.py tests/test_phase0_config.py -v
# Or: python -c "from src.config_loader import load_config; load_config('config/phase1_config.yaml')"
```

### Exit Criteria (Phase 0)

- [ ] All required directories exist
- [ ] `config/phase1_config.yaml` exists and has the Phase 1 shape (1 table, 1 column integer, 1000/100 rows, databricks block)
- [ ] `pip install -r requirements.txt` succeeds

---

## Phase 1: Unit Testing (Minimal Case)

**Goal**: Validate the pipeline for the minimal case (1 table, 1 integer column, 1000 snapshot rows, 100 incremental rows) and that each module behaves as specified in [implementation.md](implementation.md).

**Target config**: 1 table `phase1_table`, 1 column `col_1` (integer), `snapshot_rows: 1000`, `incremental_rows: 100`.

### Test Files (per [implementation.md](implementation.md))

| File | Purpose |
|------|---------|
| `tests/test_config_loader.py` | Config load, env substitution, validation |
| `tests/test_data_generator.py` | dbldatagen wrapper: schema from config, row count, types |
| `tests/test_protobuf_converter.py` | Schema → .proto, row → Message, round-trip |
| `tests/test_zerobus_ingest.py` | Zerobus ingest with **mocked** SDK (or skip if no mock) |
| `tests/test_pipeline_phase1.py` | End-to-end snapshot + incremental with mocked Zerobus; row counts and schema |

### Phase 1 Test Cases

#### 1. Config Loader (`test_config_loader.py`)

| Case | Description | Assertions |
|------|--------------|------------|
| Load phase1 config | Load `config/phase1_config.yaml` | No exception; returned config is dict-like or typed object |
| Required keys | Config has required keys | `snapshot_rows`, `incremental_rows`, `tables`, `databricks` (workspace_url, workspace_id, region, catalog, schema, zerobus.client_id, zerobus.client_secret) |
| Env substitution | Placeholders like `${VAR}` are resolved when env is set | Set `TEST_VAR`; config value contains resolved value (or unchanged if not set) |
| Table shape | First table has name and one column | `tables[0].name`, `tables[0].columns` length 1, column name `col_1`, type `integer` |
| Invalid YAML / missing keys | Invalid or incomplete config raises or returns error | Appropriate exception or error result |

#### 2. Data Generator (`test_data_generator.py`)

| Case | Description | Assertions |
|------|--------------|------------|
| Generate with Phase 1 spec | Call generator with spark, 1 table, 1 column (integer), row count 1000, seed fixed | DataFrame returned; `df.count() == 1000`; single column present; type integer |
| Schema matches config | Column name and type match config | Column name `col_1`; Spark type IntegerType (or equivalent) |
| Reproducibility | Same seed produces same row count and schema | Two runs with same seed → same count and column types |
| Row count parameter | Different row counts honored | e.g. 100, 1000 → count matches |

*Prerequisite*: Spark session (e.g. `pyspark` in-process or Databricks Connect). Tests can be marked `@pytest.mark.skipif(no_spark)` if Spark is not available in CI.

#### 3. Protobuf Converter (`test_protobuf_converter.py`)

| Case | Description | Assertions |
|------|--------------|------------|
| Schema to .proto | Generate .proto from table schema (1 column integer) | Output is valid proto2; message has `optional int32 col_1 = 1` (or equivalent naming) |
| Compile generated .proto | Compile generated .proto to Python | No compile error; descriptor or `*_pb2` module usable |
| Row to Message | Convert one DataFrame row to protobuf Message | Message has correct field name and value |
| Round-trip | DataFrame row → Message → serialize → deserialize → compare | Value equals original (e.g. integer preserved) |
| Multi-column / types | Optional: 2–3 columns (e.g. int, string, long) | Generated .proto has correct types; round-trip for each type |

#### 4. Zerobus Ingest (`test_zerobus_ingest.py`)

| Case | Description | Assertions |
|------|--------------|------------|
| Stream lifecycle (mocked) | Create stream, call ingest_records_nowait(batch), flush, close | Mock SDK receives create_stream, ingest_records_nowait (or offset), flush, close; no exception |
| Batch ingestion (mocked) | Pass N records (e.g. 10); mock captures calls | N records sent (or batched); stream closed in finally |
| Table creation (optional) | If using Spark to create table before ingest | SQL run for CREATE TABLE IF NOT EXISTS with expected schema |

*Note*: Use a mock for `ZerobusSdk` / `create_stream` so CI does not require Databricks or Zerobus credentials.

#### 5. Pipeline Phase 1 End-to-End (`test_pipeline_phase1.py`)

| Case | Description | Assertions |
|------|--------------|------------|
| Snapshot run (mocked Zerobus) | Run snapshot with phase1 config; mock Zerobus | 1000 rows generated; 1000 records sent to mock Zerobus (or 1000 in batch calls); stream flushed and closed |
| Incremental run (mocked Zerobus) | Run incremental with phase1 config | 100 rows generated; 100 records sent to mock |
| Snapshot + incremental row counts | After snapshot, total = 1000; after incremental, total = 1100 | When using real Spark + mocked Zerobus: e.g. assert on counts passed to mock. With real Zerobus: `spark.table(...).count()` == 1000 then 1100 |
| Schema consistency | Table schema used for generation and proto matches config | One column `col_1`, integer type, in generated DataFrame and in generated .proto |

### Phase 1 Prerequisites

- Python 3.9+
- Dependencies from `requirements.txt` installed
- **Unit tests**: Spark session (local `pyspark` or Databricks Connect) for data generator and pipeline; Zerobus **mocked** so no workspace needed in CI
- **Integration tests (optional)**: Real Databricks workspace, Zerobus credentials, and `config/phase1_config.yaml` with real `databricks` and `zerobus` (or env vars)

### Run (Phase 1)

```bash
# All Phase 1 tests (with Spark available)
pytest tests/test_config_loader.py tests/test_data_generator.py tests/test_protobuf_converter.py tests/test_zerobus_ingest.py tests/test_pipeline_phase1.py -v

# Only tests that do not require Spark
pytest tests/test_config_loader.py tests/test_protobuf_converter.py -v

# With coverage
pytest tests/ -v --cov=src --cov-report=term-missing
```

### Exit Criteria (Phase 1)

- [ ] Config loader: load phase1 config, required keys, env substitution, table shape; invalid config handled
- [ ] Data generator: 1000 rows, 1 column integer, schema from config; reproducibility with seed
- [ ] Protobuf converter: valid .proto from schema, compile, row → Message, round-trip
- [ ] Zerobus ingest (mocked): stream lifecycle, batch ingest, flush/close
- [ ] Pipeline Phase 1: snapshot 1000 rows, incremental 100 rows; schema consistent; (optional) real Zerobus integration passes with real credentials

---

## Later Phases (Placeholder)

As the pipeline is extended for **scaling dimensions** (more tables, columns, types, larger snapshot/incremental), add:

- **Scaling tests**: Configs with 2+ tables, 5+ columns, multiple types; assert row counts and (if applicable) Zerobus call counts or system table aggregates.
- **Performance / observability**: Optional tests or scripts that log wall-clock time and query `system.lakeflow.zerobus_ingest` / `zerobus_stream` for comparison across runs (see [plan.md](plan.md) scaling progression).

These can be added to `testing.md` under **Phase 2** (or **Scaling tests**) when implemented.

---

## References

- [plan.md](plan.md) – Outcomes, Phase 1 success criteria, scaling dimensions
- [implementation.md](implementation.md) – Implementation phases, Phase 1 unit test scope, execution order
