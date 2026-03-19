# Zerobus Synthetic Data Pipeline – Implementation

**Purpose**: Build steps, project structure, config schema, SDK usage, and technical details. For goals, requirements, and scaling guidance, see [plan.md](plan.md).

---

## Project Structure

```
zerobus/
├── config/
│   ├── pipeline_config.yaml      # Main config (from YAML)
│   └── pipeline_config.schema.json  # Optional: JSON schema for validation
├── docs/
│   ├── PLAN.md                   # Doc index
│   ├── plan.md
│   ├── implementation.md
│   ├── testing.md
│   └── learnings/                # Project learnings
│       └── README.md
├── src/
│   ├── __init__.py
│   ├── config_loader.py          # Load and validate YAML config
│   ├── statschema/            # Canonical schema: YData YAML, pipeline YAML → dbldatagen
│   │   ├── __init__.py
│   │   ├── model.py               # CanonicalColumn, CanonicalTableSchema, GenerationRule
│   │   ├── ydata_parser.py        # YData/Syda-style YAML → canonical
│   │   ├── pipeline_parser.py    # Pipeline tables[] YAML → canonical
│   │   ├── dbldatagen_builder.py  # Canonical → dbldatagen column specs
│   │   └── loader.py              # load_schema(), detect_format(); future: DB dumps
│   ├── data_generator.py         # dbldatagen wrapper (uses statschema or inline config)
│   ├── protobuf_converter.py     # DataFrame → Protobuf (dynamic .proto gen)
│   ├── zerobus_ingest.py         # ZeroBus SDK ingestion
│   └── pipeline.py               # Orchestrator (generate → convert → ingest)
├── proto/                        # Generated .proto files (gitignore or committed)
│   └── table_1_pb2.py
├── tests/
│   ├── __init__.py
│   ├── test_config_loader.py
│   ├── test_data_generator.py
│   ├── test_protobuf_converter.py
│   ├── test_zerobus_ingest.py
│   └── test_pipeline_phase1.py   # Phase 1: 1 table, 1 col int, 1000+100
├── notebooks/
│   └── zerobus_pipeline.ipynb     # Databricks Connect local dev
├── requirements.txt
├── pyproject.toml
├── databricks.yml
└── README.md
```

---

## Canonical schema and schema sources

The **`src/statschema`** module supports multiple schema formats. Each is attributed to its source:

- **SDV** (Synthetic Data Vault) – [docs.sdv.dev](https://docs.sdv.dev/sdv/concepts/metadata/metadata-json): JSON or YAML with `METADATA_SPEC_VERSION`, `tables` (primary_key, columns with sdtype), and `relationships`. Well-documented metadata spec.
- **YData / Syda** – [python.syda.ai](https://python.syda.ai/examples/structured_only/yaml_schemas/): per-field YAML with `type`, `description`, `constraints`, `__table_description__`, `__foreign_keys__`.
- **Pipeline** – existing pipeline config: `tables[]` with `name` and `columns[]` with `name`/`type`.
- **MySQL / PostgreSQL / SQL Server** – schema-only SQL DDL (CREATE TABLE). Parsed from `.sql` files or from a dict with `schema_source` and `ddl`/`sql` key. Dialect is auto-detected or set via `format_hint`. **Oracle** is reserved for a future parser.

All are parsed into a canonical internal model (`CanonicalTableSchema`, `CanonicalColumn`, `GenerationRule`) and then converted to dbldatagen column specs.

### Tagging the schema source

In any schema file (YAML or JSON), use a top-level key so the loader selects the right parser:

```yaml
# Optional: pick parser explicitly (otherwise auto-detect)
schema_source: sdv    # or: ydata | pipeline | sqlserver | postgres | mysql | oracle
# or use: source: sdv
```

- **sdv** – file must follow [SDV metadata JSON spec](https://docs.sdv.dev/sdv/concepts/metadata/metadata-json) (YAML with same structure is supported).
- **ydata** – Syda-style per-field YAML (single or multi-table).
- **pipeline** – pipeline config with `pipeline.tables` or top-level `tables` list.
- **mysql** | **postgres** | **sqlserver** – schema-only DDL: pass a `.sql` file path, or a dict with `schema_source` and `ddl` (or `sql`) key containing CREATE TABLE statements.

### Database schema-only dumps (MySQL, PostgreSQL, SQL Server)

Schema-only dumps from standard tools are supported as **plain `.sql` files** or as **dict with `ddl` key**:

| Source | Tool | Option | Ref |
|--------|------|--------|-----|
| **MySQL** | mysqldump | `--no-data` or `-d` | [mysqldump](https://dev.mysql.com/doc/refman/8.4/en/mysqldump.html) |
| **PostgreSQL** | pg_dump | `-s` or `--schema-only` | [pg_dump](https://www.postgresql.org/docs/current/app-pgdump.html) |
| **SQL Server** | mssql-scripter / SSMS | DDL script | [mssql-scripter](https://github.com/microsoft/mssql-scripter) |

- **From a file:** `load_schema("path/to/schema.sql")` or `load_schema("path/to/schema.sql", format_hint="mysql")`. Dialect is auto-detected from the SQL text if not specified.
- **From a dict:** `load_schema({"schema_source": "mysql", "ddl": "CREATE TABLE ..."})`. Use `ddl` or `sql` for the CREATE TABLE statements.

The DDL parser (`src/statschema/ddl_parser.py`) extracts table names and column definitions (name, type, NOT NULL, PRIMARY KEY), maps dialect-specific types to canonical (integer, long, string, float, double, boolean, timestamp), and returns `list[CanonicalTableSchema]`. Same canonical model and dbldatagen conversion as for SDV/YData/pipeline.

### SDV metadata example

```yaml
# config/schema_sdv_example.yaml
schema_source: sdv
METADATA_SPEC_VERSION: "V1"
tables:
  phase1_table:
    primary_key: id
    columns:
      id: { sdtype: id, regex_format: "[0-9]+" }
      col_1: { sdtype: numerical, computer_representation: Int32 }
      company_name: { sdtype: categorical }
      value: { sdtype: numerical, computer_representation: Float }
relationships: []
```

SDV sdtypes map to canonical types (e.g. `numerical` + `Int32` → integer, `categorical` → string). See `src/statschema/sdv_parser.py` and [SDV Metadata JSON](https://docs.sdv.dev/sdv/concepts/metadata/metadata-json).

### YData/Syda YAML example (single table)

```yaml
# config/schema_ydata_example.yaml
__table_description__: Example table for Zerobus pipeline (YData canonical format)

id:
  type: number
  description: Unique row identifier
  constraints:
    primary_key: true

col_1:
  type: integer
  description: Integer column for Phase 1 tests

company_name:
  type: text
  description: Name of the company
  constraints:
    max_length: 200
```

Supported types: `number`, `integer`, `long`, `text`, `string`, `email`, `boolean`, `date`, `datetime`, `timestamp`, `float`, `double`, `foreign_key`. Constraints: `primary_key`, `unique`, `max_length`. For `foreign_key`, use `references: { schema: TableName, field: id }` or `__foreign_keys__: { column_name: [ParentTable, parent_column] }`.

### Where this YAML format comes from

There is **no published formal grammar** (e.g. BNF or JSON Schema) for this format. The structure we support is **Syda’s** (Syda Structured Data, [python.syda.ai](https://python.syda.ai)), documented by example here:

- [YAML Schema Examples \| Syda Structured Data](https://python.syda.ai/examples/structured_only/yaml_schemas/)

The parser was written to match those examples (per-field `type`, `description`, `constraints`, `__table_description__`, `__foreign_keys__`, and field-level `references`). The **YData SDK** ([docs.sdk.ydata.ai](https://docs.sdk.ydata.ai)) uses a different YAML schema (table-level `primary_keys` and `foreign_keys` only, no per-column types); we do not implement that format.

### Using the schema parser

```python
from src.statschema import load_schema, to_dbldatagen_specs
import dbldatagen as dg

# Load from file; use schema_source in file or format_hint (sdv | ydata | pipeline | mysql | postgres | sqlserver)
tables = load_schema("config/schema_ydata_example.yaml", format_hint="ydata")
# Or SDV:
tables = load_schema("config/schema_sdv_example.yaml")  # schema_source: sdv in file
# Or schema-only SQL dump (mysqldump -d, pg_dump -s, mssql-scripter):
tables = load_schema("schema.sql")  # dialect auto-detected
tables = load_schema("schema.sql", format_hint="postgres")
table = tables[0]

# Convert to dbldatagen column specs and build DataFrame
specs = to_dbldatagen_specs(table, rows=1000)
gen = dg.DataGenerator(spark, name=table.name, rows=1000).withIdOutput()
for name, col_type, kwargs in specs:
    gen = gen.withColumn(name, col_type, **kwargs)
df = gen.build()
```

Or use the helper that builds the DataFrame in one call:

```python
from src.statschema import load_schema, build_dataframe_from_canonical
tables = load_schema("config/schema_ydata_example.yaml")
df = build_dataframe_from_canonical(spark, tables[0], rows=1000, seed=42)
```

### Future: other schema sources

The same canonical model and `to_dbldatagen_specs` will be used when adding parsers for:

- SQL Server, Postgres, MySQL, Oracle schema dumps (e.g. `CREATE TABLE` or information_schema exports).

Those parsers will produce `CanonicalTableSchema` (and optionally write YData YAML); no change to the data generator or Zerobus ingest layer.

### Databricks and schema YAML

Databricks Labs Data Generator (dbldatagen) does not define a standard YAML schema format; it uses a Python API (`.withColumn(...)`). Using YData-style YAML as the canonical format gives a portable, human-editable schema that can be converted to dbldatagen (and in the future from DB dumps).

The **existing pipeline YAML** (`tables[].columns[].name` / `type`) remains supported: `load_schema(config, format_hint="pipeline")` or auto-detect parses it into the same canonical model, so `data_generator` can use either the YData file or the pipeline config.

---

## YAML Schema (config/pipeline_config.yaml)

```yaml
# Pipeline configuration - single source of truth
pipeline:
  snapshot_rows: 1000
  incremental_rows: 100
  serialization: "protobuf"   # or "json" for simpler dev (no .proto compile)

  # Databricks / ZeroBus connection
  databricks:
    workspace_url: "https://<instance>.cloud.databricks.com"
    workspace_id: "<workspace-id>"  # From URL /o=<id>
    region: "us-west-2"             # Required for Zerobus endpoint (e.g., us-west-2)
    catalog: "main"
    schema: "default"
    # ZeroBus uses OAuth - service principal (separate from Databricks Connect)
    zerobus:
      client_id: "${ZEROBUS_CLIENT_ID}"   # Env var for secrets
      client_secret: "${ZEROBUS_CLIENT_SECRET}"

  # Tables to generate and ingest
  tables:
    - name: "table_1"
      columns:
        - name: "col_1"
          type: "integer"   # Explicit type
        # - name: "col_2"
        #   type: null      # Random type if omitted
    - name: "table_2"
      columns:
        - name: "id"
          type: "long"
        - name: "value"
          type: "string"
```

---

## Implementation Phases

### Phase 0: Project Setup

- [ ] Create `config/`, `src/`, `tests/`, `notebooks/`, `proto/`, `docs/`, `docs/learnings/`
- [ ] `requirements.txt`: `dbldatagen`, `databricks-zerobus-ingest-sdk`, `databricks-connect`, `pyyaml`, `grpcio-tools`, `protobuf`
- [ ] Phase 1 unit test config: `config/phase1_config.yaml`

### Phase 1: Unit Testing (Minimal Case)

**Target**: 1 table, 1 column (integer), 1000 snapshot rows, 100 incremental rows

1. **Config Loader** (`config_loader.py`)
   - Load YAML, resolve env vars (`${VAR}`)
   - Validate required keys
   - Return typed config object

2. **Data Generator** (`data_generator.py`)
   - Accept `spark`, config table spec, row count, seed
   - Map YAML types → Spark types: `integer`→`IntegerType`, `long`→`LongType`, etc.
   - If type is null/omitted: pick random from `[integer, long, string, float, boolean]`
   - Use `dg.DataGenerator(spark, ...).withColumn(...).build()`
   - Return DataFrame

3. **Protobuf Converter** (`protobuf_converter.py`)
   - Map Spark/Delta types → Proto2: `INT`→`int32`, `BIGINT`→`int64`, `STRING`→`string`, etc.
   - Generate `.proto` file content from table schema (string template)
   - Compile via `grpc_tools.protoc` or `google.protobuf` descriptor
   - Convert each DataFrame row → protobuf `Message`, serialize to bytes
   - Support both: (a) pre-compiled `*_pb2.py`, (b) runtime-generated descriptor

4. **ZeroBus Ingest** (`src/zerobus_ingest.py`) ✅ **Complete**
   - **CLI auth (default)**: `IngestConfig.from_databricks_cli(table_name)` — reuses `~/.databrickscfg`, no service principal needed. `DatabricksCliHeadersProvider` calls `databricks auth token` on each stream open; CLI handles token refresh.
   - **Service-principal auth (CI/prod)**: `IngestConfig.from_env()` — reads `DATABRICKS_CLIENT_ID`, `DATABRICKS_CLIENT_SECRET`, `ZEROBUS_SERVER_ENDPOINT`, `DATABRICKS_WORKSPACE_URL`, `ZEROBUS_TABLE_NAME`.
   - `ingest_dataframe(df, table, config)` → `IngestResult(table_name, rows_sent, offsets)` — compiles `.proto` from canonical schema, batches rows with `ingest_records_offset`, `flush()` then `close()` in `finally`.
   - 46 pytest unit tests (mocked SDK) + 2 integration tests (skipped without `ZEROBUS_SERVER_ENDPOINT`)

5. **Pipeline Orchestrator** (`pipeline.py`)
   - `run_snapshot(config_path)` → generate snapshot → convert → ingest
   - `run_incremental(config_path)` → generate incremental → convert → ingest
   - Support `--config`, `--mode snapshot|incremental`

6. **Databricks Connect**
   - Use `spark` from Databricks extension / `databricks-connect` when running locally
   - ZeroBus uses OAuth (service principal) – independent of Connect
   - Notebook `zerobus_pipeline.ipynb`: cells for config load, generate, convert, ingest (mirror [orders_pipeline.ipynb](https://github.com/rsleedbx/dbx_sdp_cursor/blob/master/part2_sdp/part1_autoloader/orders_pipeline.ipynb) pattern)

### Phase 1 Unit Test (`test_pipeline_phase1.py`)

- Config: 1 table `phase1_table`, 1 column `col_1` (integer), 1000 snapshot, 100 incremental
- **Mock ZeroBus** (or use integration test with real workspace)
- Assert: snapshot generates 1000 rows, incremental adds 100
- Assert: protobuf serialization round-trip matches
- Assert: schema generation produces valid `.proto`

Full test cases and exit criteria per phase: see [testing.md](testing.md).

---

## Type Mappings

### YAML → Spark (dbldatagen)

| YAML | Spark Type |
|------|------------|
| `integer` | `IntegerType` |
| `long` | `LongType` |
| `string` | `StringType` |
| `float` | `FloatType` |
| `double` | `DoubleType` |
| `boolean` | `BooleanType` |
| `timestamp` | `TimestampType` |

### Spark/Delta → Proto2 (ZeroBus)

| Delta | Proto2 |
|-------|--------|
| INT, SMALLINT, TINYINT | `int32` |
| BIGINT, LONG | `int64` |
| FLOAT | `float` |
| DOUBLE | `double` |
| STRING | `string` |
| BOOLEAN | `bool` |
| TIMESTAMP | `int64` (epoch ms) |

---

## ZeroBus Endpoint Format

- **AWS**: `https://<workspace_id>.zerobus.<region>.cloud.databricks.com`
- **Azure**: `https://<workspace_id>.zerobus.<region>.azuredatabricks.net`

**Note**: The `region` (e.g., `us-west-2`) must match your workspace deployment.

**Endpoint construction** (for config loader):
```python
server_endpoint = f"https://{workspace_id}.zerobus.{region}.cloud.databricks.com"  # AWS
# Azure: ...azuredatabricks.net
```

---

## ZeroBus SDK Usage (from [zerobus-sdk-py examples](https://github.com/databricks/zerobus-sdk-py/tree/main/examples))

### Environment Variables (Standard from Examples)

```bash
export DATABRICKS_CLIENT_ID="your-service-principal-application-id"
export DATABRICKS_CLIENT_SECRET="your-service-principal-secret"
export ZEROBUS_SERVER_ENDPOINT="https://<workspace-id>.zerobus.<region>.cloud.databricks.com"
export DATABRICKS_WORKSPACE_URL="https://<workspace>.cloud.databricks.com"
export ZEROBUS_TABLE_NAME="catalog.schema.table"
```

### Recommended Ingestion Methods (Avoid Deprecated APIs)

| Method | Use Case | Notes |
|--------|----------|-------|
| `ingest_record_offset(record)` | Single record with offset tracking | **RECOMMENDED** for single records |
| `ingest_records_offset(records)` | Batch ingestion | **RECOMMENDED** for batches; returns one offset for whole batch |
| `ingest_record_nowait(record)` | Fire-and-forget single | Maximum throughput, no ack wait |
| `ingest_records_nowait(records)` | Fire-and-forget batch | **Best for bulk** – most efficient |
| ~~`ingest_record()`~~ | — | **DEPRECATED** (2–40x slower); use `ingest_record_offset` |

### Stream Lifecycle (Required Pattern)

```python
try:
    stream = sdk.create_stream(client_id, client_secret, table_properties, options)
    # ... ingest records ...
    stream.flush()   # Required before close
finally:
    stream.close()   # Always close, even on error
```

### Optional: Durability Confirmation

```python
offset = stream.ingest_record_offset(record)
stream.wait_for_offset(offset)  # Optional: wait for server ack
```

### TableProperties (Protobuf vs JSON)

**Protobuf** – pass descriptor bytes:
```python
descriptor_bytes = record_pb2.AirQuality.DESCRIPTOR.file.serialized_pb
table_properties = TableProperties(TABLE_NAME, descriptor_bytes)
```

**JSON** – no descriptor:
```python
table_properties = TableProperties(TABLE_NAME)
options = StreamConfigurationOptions(record_type=RecordType.JSON)
```

### StreamConfigurationOptions (from Examples)

```python
options = StreamConfigurationOptions(
    record_type=RecordType.PROTO,   # or RecordType.JSON
    max_inflight_records=1000,
    recovery=True,
    recovery_timeout_ms=15000,
    recovery_backoff_ms=2000,
    recovery_retries=3,
)
```

### Proto2 Schema Format (from [record.proto](https://github.com/databricks/zerobus-sdk-py/blob/main/examples/record.proto))

```protobuf
syntax = "proto2";

package examples;

message AirQuality {
  optional string device_name = 1;
  optional int32 temp = 2;
  optional int64 humidity = 3;
}
```

- Use `proto2` syntax
- Use `optional` for all fields (required for Zerobus schema validation)
- Field numbers must be sequential (1, 2, 3, …)

---

## Network & Prerequisites

### Before You Begin (from [Zerobus docs](https://docs.databricks.com/aws/en/ingestion/zerobus-ingest))

1. **Create target Delta table** – Zerobus does not create tables; schema must exist.
2. **Create service principal** – Settings > Identity and Access > Service principals.
3. **Grant permissions** – `USE_CATALOG`, `USE_SCHEMA`, `MODIFY`, `SELECT` on table.
4. **Get Zerobus endpoint** – `https://<workspace_id>.zerobus.<region>.cloud.databricks.com`.

### Interface Choice (gRPC vs REST)

- **gRPC** (SDK default): Best for high-volume streams; persistent connections.
- **REST** (Beta): For edge devices, webhooks, or languages without gRPC support.

This pipeline uses the **Python SDK with gRPC**.

### Firewall / IP Allowlist

If using a client-side firewall, add Zerobus Ingest control plane IPs to your allowlist. See [Databricks control plane addresses](https://docs.databricks.com/aws/en/resources/ip-domain-region#control-plane-ip-addresses).

### Service Principal Permissions

```sql
GRANT USE CATALOG ON CATALOG <catalog> TO `<service-principal-id>`;
GRANT USE SCHEMA ON SCHEMA <catalog>.<schema> TO `<service-principal-id>`;
GRANT MODIFY, SELECT ON TABLE <catalog>.<schema>.<table> TO `<service-principal-id>`;
```

---

## Durable Fallback for Rejected Data

If schema changes cause Zerobus to reject data after it is durable, records are written to:

```
<table_storage_root>/_zerobus/table_rejected_parquets/
```

Data stays within the table's storage boundary and follows the same access controls. See [Zerobus overview](https://docs.databricks.com/aws/en/ingestion/zerobus-overview#durable-fallback-location).

---

## Monitoring Zerobus Ingest

| System Table | Description |
|--------------|-------------|
| `system.lakeflow.zerobus_stream` | Stream lifecycle events (open, close, errors) |
| `system.lakeflow.zerobus_ingest` | Ingest batches (commit_version, committed_bytes, committed_records) |

Example – total records ingested for a table:
```sql
SELECT SUM(ingest.committed_records) AS total_records
FROM system.lakeflow.zerobus_ingest AS ingest
WHERE ingest.table_name = 'catalog.schema.table_name'
  AND ingest.commit_time >= :start_timestamp
  AND ingest.commit_time <= :end_timestamp;
```

---

## JSON Alternative (Simpler for Phase 1)

For faster iteration, use **JSON** instead of Protobuf:

- No `.proto` compilation
- Pass `dict` or JSON string directly
- `TableProperties(table_name)` only (no descriptor)
- `StreamConfigurationOptions(record_type=RecordType.JSON)`

Use Protobuf when you need higher throughput or stricter schema control.

---

## Databricks Connect vs ZeroBus

| Aspect | Databricks Connect | ZeroBus Ingest |
|--------|--------------------|----------------|
| Purpose | Run Spark locally, connect to remote cluster | Push records to Delta via gRPC |
| Auth | User token / profile | OAuth (service principal) |
| Use in pipeline | `spark` for dbldatagen, table creation | `ZerobusSdk` for ingestion |

Both can run in the same process: use Connect for `spark`, ZeroBus SDK for ingestion.

---

## Phase 1 Config Example

```yaml
# config/phase1_config.yaml
pipeline:
  snapshot_rows: 1000
  incremental_rows: 100

  databricks:
    workspace_url: "${DATABRICKS_WORKSPACE_URL}"
    workspace_id: "${DATABRICKS_WORKSPACE_ID}"
    region: "${DATABRICKS_REGION}"  # e.g., us-west-2
    catalog: "main"
    schema: "default"
    zerobus:
      client_id: "${ZEROBUS_CLIENT_ID}"
      client_secret: "${ZEROBUS_CLIENT_SECRET}"

  tables:
    - name: "phase1_table"
      columns:
        - name: "col_1"
          type: "integer"
```

---

## Execution Order (Phase 1)

1. Load `config/phase1_config.yaml`
2. Create Delta table `main.default.phase1_table` (col_1 INT) if not exists
3. Generate 1000 rows with dbldatagen (integer column)
4. Generate `.proto` for `Phase1Table` with `optional int32 col_1 = 1`
5. Compile proto, convert each row to protobuf
6. Ingest 1000 records via ZeroBus
7. Validate: `spark.table("main.default.phase1_table").count() == 1000`
8. Run incremental: generate 100 more rows, ingest
9. Validate: count == 1100

---

## Open Questions / Decisions

1. **Proto generation**: Generate at runtime (temp `.proto` + compile) vs. pre-generate and commit? Recommendation: generate at runtime from config for flexibility.
2. **Random column types**: When type is omitted, use `random.choice([integer, long, string, ...])` – ensure Proto2 supports all.
3. **Integration test**: Phase 1 can use mocked ZeroBus for CI; optional integration test with real workspace for manual validation.

---

## References (Implementation)

- [Use the Zerobus Ingest connector](https://docs.databricks.com/aws/en/ingestion/zerobus-ingest) – Setup, endpoint, Python example
- [Zerobus Python SDK](https://github.com/databricks/zerobus-sdk-py) – `databricks-zerobus-ingest-sdk`
- [Zerobus SDK examples](https://github.com/databricks/zerobus-sdk-py/tree/main/examples) – sync/async, JSON/protobuf patterns
- [Zerobus Ingest system tables](https://docs.databricks.com/aws/en/admin/system-tables/zerobus-ingest)
- [dbldatagen](https://github.com/databrickslabs/dbldatagen) – API and column options
- [orders_pipeline.ipynb](https://github.com/rsleedbx/dbx_sdp_cursor/blob/master/part2_sdp/part1_autoloader/orders_pipeline.ipynb) – Databricks Connect + dbldatagen pattern
