# Column Types

The canonical model uses 14 semantic types that are independent of any specific
database.  Each type maps to a native SQL type when DDL is emitted for a target
dialect.

## Type reference table

| Canonical type | Meaning | MySQL | PostgreSQL | SQL Server | Oracle | Databricks |
|---------------|---------|-------|------------|------------|--------|------------|
| `integer` | 32-bit signed integer | `INT` | `INTEGER` | `INT` | `NUMBER(10)` | `INT` |
| `long` | 64-bit signed integer | `BIGINT` | `BIGINT` | `BIGINT` | `NUMBER(19)` | `BIGINT` |
| `float` | 32-bit approximate float | `FLOAT` | `REAL` | `FLOAT` | `BINARY_FLOAT` | `FLOAT` |
| `double` | 64-bit approximate float | `DOUBLE` | `DOUBLE PRECISION` | `FLOAT` | `BINARY_DOUBLE` | `DOUBLE` |
| `decimal` | Exact fixed-point (requires `precision` + `scale`) | `DECIMAL(p,s)` | `NUMERIC(p,s)` | `DECIMAL(p,s)` | `NUMBER(p,s)` | `DECIMAL(p,s)` |
| `string` | Variable-length text (uses `length` if set) | `VARCHAR(n)` / `LONGTEXT` | `VARCHAR(n)` / `TEXT` | `NVARCHAR(n)` / `NVARCHAR(MAX)` | `VARCHAR2(n)` / `CLOB` | `STRING` |
| `boolean` | True/false flag | `TINYINT(1)` | `BOOLEAN` | `BIT` | `NUMBER(1)` | `BOOLEAN` |
| `date` | Date only (no time) | `DATE` | `DATE` | `DATE` | `DATE` | `DATE` |
| `timestamp` | Date + time, no timezone | `DATETIME` | `TIMESTAMP` | `DATETIME2` | `TIMESTAMP` | `TIMESTAMP` |
| `timestamptz` | Date + time with timezone | `TIMESTAMP` | `TIMESTAMPTZ` | `DATETIMEOFFSET` | `TIMESTAMP WITH TIME ZONE` | `TIMESTAMP` |
| `time` | Time of day, no timezone | `TIME` | `TIME` | `TIME` | `INTERVAL DAY TO SECOND` | `STRING` |
| `timetz` | Time of day with timezone | `TIME` | `TIMETZ` | `TIME` | `INTERVAL DAY TO SECOND` | `STRING` |
| `binary` | Raw bytes (uses `length` if set) | `VARBINARY(n)` / `LONGBLOB` | `BYTEA` | `VARBINARY(n)` / `VARBINARY(MAX)` | `RAW(n)` / `BLOB` | `BINARY` |
| `uuid` | UUID / GUID (36-char) | `CHAR(36)` | `UUID` | `UNIQUEIDENTIFIER` | `CHAR(36)` | `STRING` |

> **`length` and `precision`/`scale` are stored alongside the type** — they are
> not part of the type string itself.  See [Column precision & length](04-column-precision-length.md).

## Type normalizations on parsing

When `parse_ddl()` reads source DDL, some database-specific types are widened
or merged into the nearest canonical type:

| Source type | Dialect | Canonical type | Reason |
|------------|---------|---------------|--------|
| `TINYINT` | MySQL, SQL Server | `integer` | No sub-byte canonical type |
| `SMALLINT` | all | `integer` | Widened for portability |
| `TINYINT(1)` | MySQL | `boolean` | MySQL boolean idiom |
| `BIT` | SQL Server | `boolean` | SQL Server boolean |
| `NUMBER(1)` | Oracle | `integer` | Single-digit NUMBER (not boolean) |
| `MONEY` | SQL Server | `decimal(19,4)` | Fixed-precision currency |
| `SMALLMONEY` | SQL Server | `decimal(10,4)` | Same |
| `REAL` | all | `float` | Maps to same DType as `FLOAT` |
| `SERIAL` | PostgreSQL | `integer` | Sequence tracked via `auto_increment` |
| `BIGSERIAL` | PostgreSQL | `long` | Same |
| `CHAR(n)` | all | `string` | CHAR and VARCHAR unified |
| `TEXT` / `CLOB` | all | `string` | No-length string |
| `BINARY(n)` | SQL Server | `binary` | BINARY → VARBINARY is safe |
| `ROWVERSION` | SQL Server | `binary` | No cross-DB equivalent |
| `UNIQUEIDENTIFIER` | SQL Server | `uuid` | Full lossless round-trip |
| `BYTEA` | PostgreSQL | `binary` | No length stored |
| `NUMERIC(p)` (no scale) | PostgreSQL | `long` | Whole-number NUMERIC → integer |

## String types in detail

The `string` type covers all text column variants.  The emitter chooses the
correct SQL type based on `length`:

| Condition | MySQL | PostgreSQL | SQL Server | Oracle |
|-----------|-------|------------|------------|--------|
| `length` set | `VARCHAR(n)` | `VARCHAR(n)` | `NVARCHAR(n)` | `VARCHAR2(n)` |
| No `length` | `LONGTEXT` | `TEXT` | `NVARCHAR(MAX)` | `CLOB` |

## UUID type in detail

`uuid` is a special canonical type that round-trips losslessly to each
dialect's native UUID representation:

```yaml
- name: row_guid
  type: uuid
  not_null: true
```

Emits as:
- MySQL: `CHAR(36)`
- PostgreSQL: `UUID`
- SQL Server: `UNIQUEIDENTIFIER`
- Oracle: `CHAR(36)`
- Databricks: `STRING`

## Boolean defaults

Default values for `boolean` columns must use `true`/`false` in the canonical
YAML.  The emitter translates them to the dialect-specific literal:

| Dialect | `true` emits as | `false` emits as |
|---------|----------------|-----------------|
| PostgreSQL | `TRUE` | `FALSE` |
| MySQL | `1` | `0` |
| SQL Server | `1` | `0` |
| Oracle | `1` | `0` |
| Databricks | `true` | `false` |

## Example

```yaml
columns:
  - name: order_id
    type: long
    primary_key: true
    not_null: true
    auto_increment: true
  - name: amount
    type: decimal
    precision: 10
    scale: 2
    not_null: true
  - name: notes
    type: string          # no length → LONGTEXT / TEXT / NVARCHAR(MAX) / CLOB
  - name: summary
    type: string
    length: 500           # VARCHAR(500) / NVARCHAR(500) / VARCHAR2(500)
  - name: is_paid
    type: boolean
    default: "false"
  - name: token
    type: uuid
  - name: payload
    type: binary          # VARBINARY(MAX) / BYTEA / BLOB
  - name: hash
    type: binary
    length: 32            # VARBINARY(32) / BYTEA / RAW(32)
```

## Related

- [Column constraints](03-column-constraints.md)
- [Column precision & length](04-column-precision-length.md)
- [`src/schema_parser/ddl_emitter.py`](../../src/schema_parser/ddl_emitter.py) — dialect-specific type emission
- [`src/schema_parser/ddl_parser.py`](../../src/schema_parser/ddl_parser.py) — type normalization on parse
