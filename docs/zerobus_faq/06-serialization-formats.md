# What serialization formats does Zerobus support?

Zerobus supports two record formats:

| Format | Use case | Notes |
|---|---|---|
| **Protobuf (proto2)** | Production, high throughput | Requires a matching `.proto` schema; more efficient on the wire |
| **JSON** | Development, simpler integration | No `.proto` compilation needed; slightly less efficient |

## Protobuf schema rules

- Use `proto2` syntax.
- All fields must be `optional`.
- Field numbers must be sequential (1, 2, 3, …).
- The proto schema must match the Delta table schema 1:1 (column names, types, and nullability).
- Maximum 2000 columns per proto schema.

```protobuf
syntax = "proto2";

message MyTable {
  optional int32  order_id = 1;
  optional string customer = 2;
  optional double amount   = 3;
}
```

## Type mapping (Protobuf ↔ Delta)

| Delta type | Protobuf type |
|---|---|
| `INT`, `SMALLINT`, `TINYINT` | `int32` |
| `BIGINT`, `LONG` | `int64` |
| `FLOAT` | `float` |
| `DOUBLE` | `double` |
| `STRING` | `string` |
| `BOOLEAN` | `bool` |
| `TIMESTAMP` | `int64` (epoch ms) |
| `DATE` | `int32` (days since epoch) |
| `BINARY` | `bytes` |
| `ARRAY<TYPE>` | `repeated TYPE` |
| `MAP<K,V>` | `map<K,V>` |
| `STRUCT<FIELDS>` | `message Nested { FIELDS }` |
| `VARIANT` | `string` (JSON-encoded) |

---

*[← Back to FAQ index](../zerobus_faq.md)*
