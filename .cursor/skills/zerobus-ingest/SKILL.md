---
name: zerobus-ingest
description: Implement and test Databricks ZeroBus Ingest — stream Protobuf-serialized rows into Unity Catalog Delta tables via the ZeroBus gRPC SDK. Use when adding ZeroBus ingest code, configuring auth (SDK or service principal), setting the server endpoint, writing tests for ingest logic, or debugging ZeroBus connection and auth errors.
---

# ZeroBus Ingest

ZeroBus is a Databricks serverless gRPC ingestion API that writes Protobuf-encoded records directly into Unity Catalog Delta tables at high throughput.

## Key files in this repo

| File | Role |
|------|------|
| `src/zerobus_ingest.py` | `IngestConfig`, `ingest_dataframe`, `build_zerobus_endpoint` |
| `src/protobuf_converter.py` | Schema → `.proto` → compiled message class → bytes |
| `tests/test_zerobus_ingest.py` | Unit + integration tests |

---

## Server Endpoint

The ZeroBus endpoint is deterministically derived from the workspace hostname — no region or external configuration needed:

```
https://{workspace_id}.zerobus.{workspace_hostname}
```

Example:
```
workspace:  https://e2-dogfood.staging.cloud.databricks.com
zerobus:    https://6051921418418893.zerobus.e2-dogfood.staging.cloud.databricks.com
```

`from_workspace_client()` auto-constructs this endpoint using `w.get_workspace_id()` and `w.config.hostname` from `~/.databrickscfg`. No extra configuration needed.

### Endpoint resolution order in `from_workspace_client`

1. `server_endpoint=` argument (explicit override)
2. `ZEROBUS_SERVER_ENDPOINT` env var
3. Auto-constructed from workspace SDK config

### `build_zerobus_endpoint` utility

```python
from src.zerobus_ingest import build_zerobus_endpoint

endpoint = build_zerobus_endpoint()
print(endpoint)  # https://6051921418418893.zerobus.e2-dogfood.staging.cloud.databricks.com
```

---

## Table Name

The table name passed to `IngestConfig` must be fully qualified: `catalog.schema.table`.

Use `build_qualified_table_name` to construct it automatically:

```python
from src.zerobus_ingest import build_qualified_table_name

# Bare table name — derives schema from current user email (robert.lee@ -> robert_lee)
fqn = build_qualified_table_name("orders")
# -> "main.robert_lee.orders"

# Already qualified — returned unchanged
fqn = build_qualified_table_name("main.default.orders")
# -> "main.default.orders"

# Explicit overrides
fqn = build_qualified_table_name("orders", catalog="dev", schema="myschema")
# -> "dev.myschema.orders"
```

Schema derivation: `robert.lee@databricks.com` → `robert_lee` (strip domain, replace `.` and `-` with `_`). Falls back to `default` if SDK call fails.

---

## Authentication

### SDK auth (local dev — recommended)

Uses `~/.databrickscfg` — same credentials as Databricks Connect. No service principal needed. Endpoint and table name are both auto-constructed.

```python
from src.zerobus_ingest import IngestConfig, ingest_dataframe, build_qualified_table_name

table_fqn = build_qualified_table_name("orders")   # main.robert_lee.orders
config = IngestConfig.from_workspace_client(table_fqn)

# Short-lived PAT (auto-expires; useful for tests)
config = IngestConfig.from_workspace_client(table_fqn, lifetime_seconds=300)
```

### Service principal auth (CI / production)

```python
# Requires env vars:
# DATABRICKS_CLIENT_ID, DATABRICKS_CLIENT_SECRET,
# ZEROBUS_SERVER_ENDPOINT, DATABRICKS_WORKSPACE_URL, ZEROBUS_TABLE_NAME
config = IngestConfig.from_env()
config = IngestConfig.from_env(table_name="main.default.my_table")
```

---

## Ingesting a DataFrame

```python
from src.zerobus_ingest import ingest_dataframe
from src.schema_parser.model import CanonicalTableSchema, CanonicalColumn

table = CanonicalTableSchema(
    name="orders",
    columns=[
        CanonicalColumn(name="order_id", type="integer"),
        CanonicalColumn(name="amount", type="double"),
    ],
)

result = ingest_dataframe(df, table, config)
print(f"Ingested {result.rows_sent} rows in {result.batch_count} batches")
```

`ingest_dataframe` pipeline:
1. `schema_to_proto_str(table)` → `.proto` string
2. `compile_proto(proto_str, table.name)` → compiled message class
3. `serialize_row(row, msg_class)` → `bytes` per row
4. `stream.ingest_records_offset(batch)` per batch
5. `stream.flush()` then `stream.close()` (always, even on exception)

Default `batch_size` is 500. Override per call: `ingest_dataframe(df, table, config, batch_size=100)`.

---

## Prerequisites

Before calling `ingest_dataframe`:
1. Target Delta table must exist in Unity Catalog with matching column names.
2. User (or service principal) needs `MODIFY` + `SELECT` on the table.
3. `databricks-zerobus-ingest-sdk` must be installed (`requirements.txt`).

---

## Testing

### Unit tests (no credentials, mock the SDK)

ZeroBus SDK names are imported at module level in `zerobus_ingest.py` so `patch.multiple` can replace them. Also patch `build_zerobus_endpoint` to avoid real SDK calls:

```python
from unittest.mock import MagicMock, patch

mock_stream = MagicMock()
mock_stream.ingest_records_offset.side_effect = iter([1, 2, 3])
mock_sdk_instance = MagicMock()
mock_sdk_instance.create_stream_with_headers_provider.return_value = mock_stream

class MockRecordType:
    PROTO = "PROTO"

with patch("src.zerobus_ingest._host_from_workspace_client", return_value="https://ws"), \
     patch("src.zerobus_ingest.build_zerobus_endpoint", return_value="https://123.zerobus.ws.databricks.com"), \
     patch.multiple("src.zerobus_ingest",
         ZerobusSdk=MagicMock(return_value=mock_sdk_instance),
         TableProperties=MagicMock(),
         StreamConfigurationOptions=MagicMock(),
         RecordType=MockRecordType):
    config = IngestConfig.from_workspace_client("main.default.t")
    result = ingest_dataframe(fake_df, table, config)
```

### Integration tests

Set env vars; the tests skip automatically when unset:

```bash
export ZEROBUS_SERVER_ENDPOINT=https://...   # optional — auto-constructed if unset
export ZEROBUS_TABLE_NAME=main.default.my_table
.venv_3_11/bin/python -m pytest tests/test_zerobus_ingest.py -k integration -v
```

---

## Common Pitfalls

- **`HeadersProvider.__new__() takes 0 positional arguments`** — Rust Pyo3 `__new__` rejects forwarded args. Fix: `def __new__(cls, *a, **kw): return HeadersProvider.__new__(cls)`. Applied in `DatabricksSdkHeadersProvider`.

- **`AttributeError: module has no attribute 'ZerobusSdk'` in mock** — ZeroBus imports must be at module level, not inside functions. Already fixed in `zerobus_ingest.py`.

- **Rows ingested but not visible** — `flush()` must be called before `close()`. `ingest_dataframe` does this in a `finally` block.

- **`ValueError: Protocol message has no field 'id'`** — `dbldatagen.withIdOutput()` adds an `id` column not in the schema. Fixed in `serialize_row`: only descriptor fields are set.

- **Duplicate proto symbol in pytest** — multiple `compile_proto` calls collide in the descriptor pool. Fixed in `compile_proto`: unique counter suffix per compilation (`Table_0`, `Table_1`, …).
