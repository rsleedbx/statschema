# Column Precision & Length

Precision, scale, length, fractional-seconds precision, and the unsigned
modifier capture the full storage specification of a column for a lossless
round-trip.

## Fields

| Field | Type | Applies to | Description |
|-------|------|-----------|-------------|
| `length` | int | `string`, `binary`, `uuid` | Maximum byte/character length for variable-length types. |
| `precision` | int | `decimal` | Total number of significant digits. |
| `scale` | int | `decimal` | Number of digits after the decimal point. |
| `fsp` | int | `timestamp`, `timestamptz`, `time`, `timetz` | Fractional-seconds precision (0–9). |
| `unsigned` | bool | `integer`, `long` (MySQL only) | MySQL `UNSIGNED` modifier. |

All fields are optional and are omitted from the YAML when `null` or `false`.

---

## `length` — string and binary types

Controls the declared maximum length for `string` and `binary` columns.

```yaml
- name: code
  type: string
  length: 10        # VARCHAR(10) / NVARCHAR(10) / VARCHAR2(10)

- name: thumbnail
  type: binary
  length: 4096      # VARBINARY(4096) / RAW(4096)
```

When `length` is absent, the emitter uses the dialect's "unlimited" type:

| Type | No length | MySQL | PostgreSQL | SQL Server | Oracle |
|------|-----------|-------|------------|------------|--------|
| `string` | Unlimited | `LONGTEXT` | `TEXT` | `NVARCHAR(MAX)` | `CLOB` |
| `binary` | Unlimited | `LONGBLOB` | `BYTEA` | `VARBINARY(MAX)` | `BLOB` |

---

## `precision` and `scale` — decimal types

Both fields are required together for `decimal` columns to produce a
fully-specified `DECIMAL(p,s)` / `NUMERIC(p,s)` / `NUMBER(p,s)`.

```yaml
- name: price
  type: decimal
  precision: 10
  scale: 2          # DECIMAL(10,2) / NUMBER(10,2)

- name: tax_rate
  type: decimal
  precision: 5
  scale: 4          # DECIMAL(5,4) = 0.0000–9.9999
```

When `scale` is omitted it is treated as `0` (whole numbers only).

### Money / currency columns

Fixed-precision money types from SQL Server (`MONEY`, `SMALLMONEY`) are
normalised on parse:

| Source | Canonical | Emitted |
|--------|-----------|---------|
| `MONEY` | `decimal`, precision=19, scale=4 | `DECIMAL(19,4)` |
| `SMALLMONEY` | `decimal`, precision=10, scale=4 | `DECIMAL(10,4)` |

---

## `fsp` — fractional-seconds precision

Controls the sub-second resolution of temporal columns.

```yaml
- name: created_at
  type: timestamp
  fsp: 6            # DATETIME(6) / TIMESTAMP(6) / DATETIME2(6)

- name: measured_at
  type: timestamptz
  fsp: 3            # 3-decimal-place milliseconds
```

Database-specific limits:

| Database | Max `fsp` | Notes |
|----------|-----------|-------|
| MySQL | 6 | Microseconds |
| PostgreSQL | 6 | Microseconds |
| SQL Server | 7 | 100-nanosecond resolution (`DATETIME2(7)`) |
| Oracle | 9 | Nanoseconds (`TIMESTAMP(9)`) |
| Databricks | 6 | Not configurable in DDL; always 6 |

When `fsp` is absent the dialect's default is used (usually `0` or max
precision depending on the type).

---

## `unsigned` — MySQL unsigned integers

MySQL supports `UNSIGNED` on integer and bigint columns, roughly doubling the
positive range.  The canonical model preserves this flag so the round-trip
keeps it.

```yaml
- name: hit_count
  type: integer
  unsigned: true    # INT UNSIGNED (MySQL only)
```

On non-MySQL dialects the `unsigned` flag is silently ignored (PostgreSQL,
SQL Server, Oracle, and Databricks have no `UNSIGNED` modifier).

> **`BIGINT UNSIGNED`** (64-bit, max ~1.8×10¹⁹) has no signed equivalent in
> any dialect and is mapped to `decimal(20,0)` on parse to preserve the full
> value range.

---

## Complete example

```yaml
columns:
  - name: id
    type: long
    primary_key: true
    not_null: true
    auto_increment: true

  - name: sku
    type: string
    length: 50          # VARCHAR(50)

  - name: notes
    type: string        # no length → LONGTEXT / TEXT / NVARCHAR(MAX) / CLOB

  - name: price
    type: decimal
    precision: 12
    scale: 4            # DECIMAL(12,4)

  - name: score
    type: integer
    unsigned: true      # INT UNSIGNED (MySQL); INT elsewhere

  - name: created_at
    type: timestamp
    fsp: 6              # DATETIME(6) / TIMESTAMP(6) / DATETIME2(6)

  - name: payload
    type: binary
    length: 256         # VARBINARY(256) / RAW(256) / BYTEA
```

## Related

- [Column types](02-column-types.md)
- [Column constraints](03-column-constraints.md)
- [`src/statschema/ddl_emitter.py`](../../src/statschema/ddl_emitter.py) — length/precision/scale emission per dialect
