# Column Constraints

Column-level constraints control nullability, identity, uniqueness, primary
keys, and default values.  All are optional and default to `false` / `null`.

## Constraint fields

| Field | Type | Default | DDL keyword emitted |
|-------|------|---------|---------------------|
| `not_null` | bool | `false` | `NOT NULL` |
| `primary_key` | bool | `false` | `PRIMARY KEY` |
| `unique` | bool | `false` | `UNIQUE` |
| `auto_increment` | bool | `false` | `AUTO_INCREMENT` / `SERIAL` / `IDENTITY` / `GENERATED ALWAYS AS IDENTITY` |
| `default` | string | `null` | `DEFAULT <value>` |

Fields are omitted from the YAML when they are `false` or `null` to keep the
file compact.

## `not_null`

Adds a `NOT NULL` constraint to the column.

```yaml
- name: email
  type: string
  length: 255
  not_null: true
```

Emitted as `email VARCHAR(255) NOT NULL` (MySQL), `email NVARCHAR(255) NOT NULL` (SQL Server), etc.

> **Oracle note**: `NOT NULL` is suppressed for `IDENTITY` columns because
> Oracle's `GENERATED ALWAYS AS IDENTITY` is implicitly `NOT NULL`.

## `primary_key`

Marks the column as part of the table's primary key.  For a single-column PK,
`PRIMARY KEY` is emitted inline.

```yaml
- name: id
  type: long
  primary_key: true
  not_null: true
  auto_increment: true
```

For multi-column PKs, set `primary_key: true` on each participating column —
the emitter collects them and emits a trailing `PRIMARY KEY (col1, col2)` clause.

## `unique`

Emits a `UNIQUE` constraint on the column.

```yaml
- name: username
  type: string
  length: 100
  not_null: true
  unique: true
```

## `auto_increment`

Emits the dialect-appropriate identity/sequence mechanism:

| Dialect | Emitted syntax |
|---------|---------------|
| MySQL | `AUTO_INCREMENT` |
| PostgreSQL | `SERIAL` (for `integer`) / `BIGSERIAL` (for `long`) |
| SQL Server | `IDENTITY(1,1)` |
| Oracle | `GENERATED ALWAYS AS IDENTITY` |
| Databricks | `GENERATED ALWAYS AS IDENTITY` |

```yaml
- name: id
  type: long
  auto_increment: true
  primary_key: true
  not_null: true
```

> **Oracle**: emitting `GENERATED ALWAYS AS IDENTITY` with `NOT NULL` causes
> `ORA-00907` (Oracle treats IDENTITY as implicitly NOT NULL).  The emitter
> automatically suppresses `NOT NULL` for Oracle identity columns.

## `default`

The raw `DEFAULT` expression to embed in the DDL.  The value is written as-is
into the `DEFAULT` clause, so string literals must include their own quotes:

| Canonical `default` value | Emitted clause |
|--------------------------|----------------|
| `"'pending'"` | `DEFAULT 'pending'` |
| `"0"` | `DEFAULT 0` |
| `"CURRENT_TIMESTAMP"` | `DEFAULT CURRENT_TIMESTAMP` |
| `"true"` | `DEFAULT TRUE` (PG) / `DEFAULT 1` (MySQL/SS/Oracle) |
| `"false"` | `DEFAULT FALSE` (PG) / `DEFAULT 0` (MySQL/SS/Oracle) |

```yaml
- name: status
  type: string
  length: 20
  default: "'pending'"

- name: created_at
  type: timestamp
  not_null: true
  default: CURRENT_TIMESTAMP

- name: is_active
  type: boolean
  not_null: true
  default: "true"       # emitter translates: 1 for MySQL/SS/Oracle, TRUE for PG
```

### Boolean default translation

Boolean `true`/`false` defaults are automatically normalised per dialect.
You always write `"true"` or `"false"` in the canonical YAML; the emitter does
the right thing:

```python
# canonical:   default: "true"
# → MySQL:     DEFAULT 1
# → PG:        DEFAULT TRUE
# → SS:        DEFAULT 1
# → Oracle:    DEFAULT 1
# → Databricks: DEFAULT true
```

### Oracle `DEFAULT` ordering

Oracle requires `DEFAULT value` to come **before** `NOT NULL` in the column
definition.  The emitter handles this automatically.

## Complete example

```yaml
- name: id
  type: long
  primary_key: true
  not_null: true
  auto_increment: true

- name: email
  type: string
  length: 255
  not_null: true
  unique: true

- name: role
  type: string
  length: 50
  not_null: true
  default: "'viewer'"

- name: is_active
  type: boolean
  not_null: true
  default: "true"

- name: login_count
  type: integer
  not_null: true
  default: "0"
```

## Related

- [Column types](02-column-types.md)
- [Column precision & length](04-column-precision-length.md)
- [`src/schema_parser/ddl_emitter.py`](../../src/schema_parser/ddl_emitter.py) — `_col_ddl`, `_normalize_default`
