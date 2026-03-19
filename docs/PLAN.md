# Synthetic data pipeline plan (historical) — statschema repo

> **Scope (2026):** This repository is **statschema** — DDL/stats transpiler, canonical YAML, and statistics-driven synthetic data.  
> The sections below are **historical context** for an earlier configurable pipeline design (synthetic data → serialization → Delta).

**Purpose**: Goals, requirements, scaling dimensions, and technology. For current build steps see [implementation.md](implementation.md); for test cases see [testing.md](testing.md).

---

## Project Goal

**Demonstrate the historical pipeline and help operators understand push-to-Delta behavior** as scale increases across:

- **Number of tables** – one stream per table; observe behavior with 1 table vs many.
- **Number of columns** – wider schemas (more fields per record); impact on serialization and ingestion.
- **Column types** – more and varied types (integer, long, string, float, double, boolean, timestamp); schema complexity and validation.
- **Row volume – snapshot** – initial bulk load size; throughput and durability.
- **Row volume – CDC / incremental** – ongoing incremental load; behavior under continuous or batched updates.

The pipeline is a **configurable harness**: by changing YAML (tables, columns, types, snapshot_rows, incremental_rows), operators can run at different scales and **observe the path to Delta** (latency, throughput, stream lifecycle, success/failure, monitoring tables) without changing code.

---

## Requirements Summary

### Outcomes (Success Criteria)

- **Pipeline behavior**: Config-driven synthetic data is generated, converted to Protobuf (or JSON), and written to Delta via a push API; snapshot and incremental runs are supported.
- **Observability**: Run at different scales (tables, columns, types, rows) and observe behavior—throughput, latency, stream lifecycle, and relevant **`system.lakeflow.*`** metrics as documented for your workspace.
- **Phase 1**: One table, one integer column, 1000 snapshot rows and 100 incremental rows; unit tests assert row counts, schema, and optional protobuf round-trip.

### Inputs

| Input | Required | Description |
|-------|----------|-------------|
| **Config file** | Yes | YAML path (e.g. `config/phase1_config.yaml`) via `--config`. Defines `snapshot_rows`, `incremental_rows`, `tables` (name, columns with name/type), and `databricks` (workspace_url, workspace_id, region, catalog, schema, OAuth / service principal fields). |
| **Environment variables** | Yes (for auth) | e.g. `DATABRICKS_CLIENT_ID`, `DATABRICKS_CLIENT_SECRET`; optional workspace and table overrides depending on integration. Config may use `${VAR}` substitution. |
| **Spark session** | Yes (for generation) | `spark` from Databricks Connect or notebook; used for dbldatagen and table creation. |
| **CLI** | No | `--mode snapshot|incremental`; defaults implied by entrypoint. |

### Outputs

| Output | Description |
|--------|-------------|
| **Delta tables** | One Unity Catalog table per configured table (e.g. `catalog.schema.phase1_table`), populated with generated rows. |
| **Generated artifacts** | With Protobuf: generated `.proto` and compiled `*_pb2.py` (e.g. under `proto/`). With JSON: none. |
| **Validation** | Phase 1 tests: row counts (1000 after snapshot, 1100 after incremental), schema consistency, optional protobuf round-trip. |

### Technology Highlighted

| Technology | Role |
|------------|------|
| **dbldatagen** | Synthetic DataFrame generation from YAML-defined tables/columns/types. |
| **Protocol Buffers (proto2)** | Record serialization (optional; JSON alternative). |
| **Push API (gRPC)** | Write records into Unity Catalog Delta tables from the driver. |
| **Databricks Connect** | Local `spark` against remote workspace. |
| **Unity Catalog + Delta** | Target tables and governance. |

---

## Overview

Build a **configurable pipeline** that:
1. Generates synthetic data using **Databricks Labs Data Generator** (dbldatagen)
2. Converts output to **Protocol Buffers** (or JSON)
3. Feeds records to a **push-to-Delta** path (gRPC or equivalent)
4. Supports both **initial snapshot** and **incremental** (CDC-style) data generation

By varying config (tables, columns, types, snapshot/incremental rows), operators can **see how the pipeline behaves** as each dimension scales—without code changes.

---

## Processing Flow

```
┌─────────────────────┐     ┌──────────────────────┐     ┌─────────────────────┐
│  dbldatagen         │     │  DataFrame →          │     │  Push to Delta      │
│  (synthetic data)   │ ──► │  Protobuf conversion  │ ──► │  (Unity Catalog)    │
└─────────────────────┘     └──────────────────────┘     └─────────────────────┘
         │                              │
         │  Run again for incremental   │
         └──────────────────────────────┘
```

---

## Control Features (YAML Configuration)

| Feature | YAML Key | Description |
|---------|----------|-------------|
| Number of tables | `tables` (list) | Each table has its own schema and load stream |
| Columns per table | `tables[].columns` | List of column definitions |
| Column types | `tables[].columns[].type` | `integer`, `long`, `string`, `float`, `double`, `boolean`, `timestamp`; omit for random |
| Initial snapshot size | `snapshot_rows` | Rows for initial load |
| Incremental size | `incremental_rows` | Rows for incremental run |
| Serialization format | `serialization` | `protobuf` (default) or `json` – JSON simpler for dev |
| Databricks Connect | `databricks_connect` | Use local Spark via Databricks Connect |

---

## Scaling Dimensions

Each config knob supports **understanding the pipeline as scale increases**:

| Dimension | Config | What to vary | What to observe |
|-----------|--------|--------------|----------------------------|
| **Number of tables** | `tables` (list length) | 1 → 2 → 5 → 10+ tables | One stream per table; concurrency; stream lifecycle and workspace system tables for your integration. |
| **Number of columns** | `tables[].columns` (list length) | 1 → 5 → 20 → 50+ columns | Wider records (Protobuf/JSON size); throughput (records/sec, bytes/sec), schema validation or size limits. |
| **Column types** | `tables[].columns[].type` | Add types: int, long, string, float, double, boolean, timestamp | Schema complexity; validation and type handling; Protobuf vs JSON behavior. |
| **Snapshot row volume** | `snapshot_rows` | 1K → 10K → 100K → 1M+ | Bulk load behavior: throughput, flush/close time, committed bytes/rows in monitoring tables. |
| **CDC / incremental volume** | `incremental_rows` | 100 → 1K → 10K per run | Incremental (CDC-like) behavior; multiple runs; smaller repeated batches. |

**Suggested progression**

1. **Baseline**: Phase 1 (1 table, 1 column, 1000 snapshot, 100 incremental)—confirm pipeline works end-to-end.
2. **More tables**: Same schema, increase `tables` (e.g. 2, 5, 10); one stream per table.
3. **More columns**: Fix 1 table, increase columns (e.g. 5, 20); optional Protobuf vs JSON comparison.
4. **More types**: Add column types (string, float, timestamp, etc.).
5. **Larger snapshot**: Increase `snapshot_rows` (e.g. 10K, 100K); observe throughput and metrics.
6. **Larger incremental**: Increase `incremental_rows` or run incremental multiple times.

Document or log for each run: config (tables, columns, types, rows), wall-clock time, and workspace metrics so you can compare runs.

---

## Technology Stack (What Is Highlighted)

| Technology | Role in This Project |
|------------|----------------------|
| **dbldatagen** (Databricks Labs Data Generator) | Generates synthetic Spark DataFrames from a configurable schema (table/column count, types). Used for both snapshot and incremental row counts. |
| **Protocol Buffers (proto2)** | Serialization format for records before load. Optional; JSON is supported for simpler dev. |
| **Push API (gRPC)** | Serverless-style push into Unity Catalog Delta tables (no Kafka). Typical pattern: Python SDK over gRPC. |
| **Databricks Connect** | Run the pipeline locally (Cursor/IDE) with `spark` and `dbutils` talking to a remote Databricks workspace. |
| **Unity Catalog + Delta** | Target storage and governance; tables must exist before loading. |

---

## References

- [dbldatagen](https://github.com/databrickslabs/dbldatagen) – Databricks Labs Data Generator
- [orders_pipeline.ipynb](https://github.com/rsleedbx/dbx_sdp_cursor/blob/master/part2_sdp/part1_autoloader/orders_pipeline.ipynb) – Databricks Connect + dbldatagen pattern
- [Databricks Connect](https://docs.databricks.com/en/dev-tools/databricks-connect.html)
