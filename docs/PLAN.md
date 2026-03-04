# Zerobus Synthetic Data Pipeline – Plan

**Purpose**: Goals, requirements, scaling dimensions, and technology. For build steps see [implementation.md](implementation.md); for test cases see [testing.md](testing.md).

---

## Project Goal

**Demonstrate Zerobus and help customers understand Zerobus behavior** as scale increases across:

- **Number of tables** – one stream per table; observe Zerobus with 1 table vs many.
- **Number of columns** – wider schemas (more fields per record); impact on serialization and ingestion.
- **Column types** – more and varied types (integer, long, string, float, double, boolean, timestamp); schema complexity and Zerobus validation.
- **Row volume – snapshot** – initial bulk load size; throughput and durability.
- **Row volume – CDC / incremental** – ongoing incremental load; Zerobus behavior under continuous or batched updates.

The pipeline is a **configurable harness**: by changing YAML (tables, columns, types, snapshot_rows, incremental_rows), customers can run at different scales and **observe Zerobus** (latency, throughput, stream lifecycle, success/failure, monitoring tables) without changing code.

---

## Requirements Summary

### Outcomes (Success Criteria)

- **Pipeline behavior**: Config-driven synthetic data is generated, converted to Protobuf (or JSON), and ingested into Delta via Zerobus; snapshot and incremental runs are supported.
- **Zerobus observability**: Customers can run at different scales (tables, columns, types, rows) and observe Zerobus behavior—throughput, latency, stream lifecycle, and system tables (`system.lakeflow.zerobus_*`)—to understand how Zerobus performs as dimensions increase.
- **Phase 1**: One table, one integer column, 1000 snapshot rows and 100 incremental rows; unit tests assert row counts, schema, and optional protobuf round-trip.

### Inputs

| Input | Required | Description |
|-------|----------|-------------|
| **Config file** | Yes | YAML path (e.g. `config/phase1_config.yaml`) via `--config`. Defines `snapshot_rows`, `incremental_rows`, `tables` (name, columns with name/type), and `databricks` (workspace_url, workspace_id, region, catalog, schema, zerobus client_id/client_secret). |
| **Environment variables** | Yes (for Zerobus) | `DATABRICKS_CLIENT_ID`, `DATABRICKS_CLIENT_SECRET`; optionally `ZEROBUS_SERVER_ENDPOINT`, `DATABRICKS_WORKSPACE_URL`, `ZEROBUS_TABLE_NAME` if not fully specified in config. Config may use `${VAR}` substitution. |
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
| **Protocol Buffers (proto2)** | Record serialization for Zerobus (optional; JSON alternative). |
| **Databricks Zerobus Ingest** | Push API (Python SDK, gRPC) into Delta tables. |
| **Databricks Connect** | Local `spark` against remote workspace. |
| **Unity Catalog + Delta** | Target tables and governance. |

---

## Overview

Build a **configurable pipeline** that:
1. Generates synthetic data using **Databricks Labs Data Generator** (dbldatagen)
2. Converts output to **Protocol Buffers** (or JSON)
3. Feeds records to **Databricks ZeroBus Ingest**
4. Supports both **initial snapshot** and **incremental** (CDC-style) data generation

By varying config (tables, columns, types, snapshot/incremental rows), customers can **see how Zerobus behaves** as each dimension scales—without code changes.

---

## Processing Flow

```
┌─────────────────────┐     ┌──────────────────────┐     ┌─────────────────────┐
│  dbldatagen         │     │  DataFrame →          │     │  ZeroBus Ingest     │
│  (synthetic data)   │ ──► │  Protobuf conversion  │ ──► │  (Delta tables)     │
└─────────────────────┘     └──────────────────────┘     └─────────────────────┘
         │                              │
         │  Run again for incremental   │
         └──────────────────────────────┘
```

---

## Control Features (YAML Configuration)

| Feature | YAML Key | Description |
|---------|----------|-------------|
| Number of tables | `tables` (list) | Each table has its own schema and ZeroBus stream |
| Columns per table | `tables[].columns` | List of column definitions |
| Column types | `tables[].columns[].type` | `integer`, `long`, `string`, `float`, `double`, `boolean`, `timestamp`; omit for random |
| Initial snapshot size | `snapshot_rows` | Rows for initial load |
| Incremental size | `incremental_rows` | Rows for incremental run |
| Serialization format | `serialization` | `protobuf` (default) or `json` – JSON simpler for dev |
| Databricks Connect | `databricks_connect` | Use local Spark via Databricks Connect |

---

## Scaling Dimensions and Zerobus Behavior

Each config knob directly supports the goal of **understanding Zerobus as scale increases**. Suggested way to use them:

| Dimension | Config | What to vary | What to observe (Zerobus) |
|-----------|--------|--------------|----------------------------|
| **Number of tables** | `tables` (list length) | 1 → 2 → 5 → 10+ tables | One stream per table; concurrency, stream open/close, `system.lakeflow.zerobus_stream` and `zerobus_ingest` per table. |
| **Number of columns** | `tables[].columns` (list length) | 1 → 5 → 20 → 50+ columns | Wider records (Protobuf/JSON size); ingest throughput (records/sec, bytes/sec), any schema validation or size limits. |
| **Column types** | `tables[].columns[].type` | Add types: int, long, string, float, double, boolean, timestamp | Schema complexity; Zerobus schema validation and type handling; Protobuf vs JSON behavior. |
| **Snapshot row volume** | `snapshot_rows` | 1K → 10K → 100K → 1M+ | Bulk load behavior: throughput, flush/close time, `committed_records` / `committed_bytes` in `zerobus_ingest`. |
| **CDC / incremental volume** | `incremental_rows` | 100 → 1K → 10K per run | Incremental (CDC-like) behavior; multiple runs; Zerobus under repeated smaller batches. |

**Suggested progression for customers**

1. **Baseline**: Phase 1 (1 table, 1 column, 1000 snapshot, 100 incremental)—confirm pipeline and Zerobus work.
2. **More tables**: Same schema, increase `tables` (e.g. 2, 5, 10); one stream per table.
3. **More columns**: Fix 1 table, increase columns (e.g. 5, 20); compare Protobuf vs JSON if desired.
4. **More types**: Add column types (string, float, timestamp, etc.) to see schema and validation behavior.
5. **Larger snapshot**: Increase `snapshot_rows` (e.g. 10K, 100K); observe throughput and system tables.
6. **Larger incremental**: Increase `incremental_rows` or run incremental multiple times; observe CDC-like behavior.

Document or log for each run: config (tables, columns, types, rows), wall-clock time, and Zerobus system table aggregates so customers can compare Zerobus behavior across runs.

---

## Technology Stack (What Is Highlighted)

| Technology | Role in This Project |
|------------|----------------------|
| **dbldatagen** (Databricks Labs Data Generator) | Generates synthetic Spark DataFrames from a configurable schema (table/column count, types). Used for both snapshot and incremental row counts. |
| **Protocol Buffers (proto2)** | Serialization format for records sent to Zerobus. Optional; JSON is supported for simpler dev. |
| **Databricks Zerobus Ingest** | Serverless push API to write records directly into Unity Catalog Delta tables (no Kafka). Python SDK over gRPC. |
| **Databricks Connect** | Lets you run the pipeline locally (Cursor/IDE) with `spark` and `dbutils` talking to a remote Databricks workspace. |
| **Unity Catalog + Delta** | Target storage and governance; tables must exist before Zerobus ingestion. |

---

## References

### Zerobus Ingest
- [Zerobus Ingest in Action – Stream Event Data Directly](https://community.databricks.com/t5/technical-blog/zerobus-ingest-in-action-how-to-stream-event-data-directly-into/ba-p/136589) – Community blog
- [Zerobus Ingest connector overview](https://docs.databricks.com/aws/en/ingestion/zerobus-overview)
- [Use the Zerobus Ingest connector](https://docs.databricks.com/aws/en/ingestion/zerobus-ingest)
- [Zerobus Python SDK](https://github.com/databricks/zerobus-sdk-py) – `databricks-zerobus-ingest-sdk`
- [Zerobus SDK examples](https://github.com/databricks/zerobus-sdk-py/tree/main/examples)
- [Zerobus Ingest system tables](https://docs.databricks.com/aws/en/admin/system-tables/zerobus-ingest)

### Data Generation & Connect
- [dbldatagen](https://github.com/databrickslabs/dbldatagen) – Databricks Labs Data Generator
- [orders_pipeline.ipynb](https://github.com/rsleedbx/dbx_sdp_cursor/blob/master/part2_sdp/part1_autoloader/orders_pipeline.ipynb) – Databricks Connect + dbldatagen pattern
