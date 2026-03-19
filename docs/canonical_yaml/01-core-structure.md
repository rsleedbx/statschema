# Core YAML Structure

The canonical schema YAML document has a simple top-level envelope that wraps
one or more table definitions.

## Top-level document

```yaml
version: "1.0"
tables:
  - name: <table_name>
    ...
  - name: <another_table>
    ...
```

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| `version` | Yes | string | Schema format version.  Currently always `"1.0"`. |
| `tables` | Yes | list | Ordered list of table definitions. |

## Table definition fields

Each entry in `tables` is a `CanonicalTableSchema`:

| Field | Required | Type | Default | Description |
|-------|----------|------|---------|-------------|
| `name` | Yes | string | — | Table name as it will appear in emitted DDL. |
| `columns` | Yes | list | — | Ordered list of column definitions. |
| `description` | No | string | `null` | Human-readable description (comment only; not emitted as DDL). |
| `fk_constraints` | No | list | `[]` | Foreign key constraints — see [Foreign key constraints](05-foreign-keys.md). |
| `foreign_keys` | No | list | `[]` | Legacy FK dict format (kept for backward compatibility). |
| `temporal_ordering_constraints` | No | list | `[]` | Date/time ordering rules — see [Temporal ordering](07-temporal-ordering.md). |
| `aliases` | No | list | `[]` | Additional table names sharing this definition — see [Multi-instance expansion](08-multi-instance-expansion.md). |
| `instance_count` | No | int | `1` | Number of numbered copies to generate — see [Multi-instance expansion](08-multi-instance-expansion.md). |
| `instance_suffix_format` | No | string | `"_{:04d}"` | Format string for numbered suffixes. |

## Column definition fields

Each entry in `columns` is a `CanonicalColumn`:

| Field | Required | Type | Default | Description |
|-------|----------|------|---------|-------------|
| `name` | Yes | string | — | Column identifier. |
| `type` | Yes | string | — | Canonical type — see [Column types](02-column-types.md). |
| `length` | No | int | `null` | Length for `string` / `binary` columns. |
| `precision` | No | int | `null` | Total digit count for `decimal`. |
| `scale` | No | int | `null` | Decimal places for `decimal`. |
| `fsp` | No | int | `null` | Fractional-seconds precision for temporal types (0–9). |
| `unsigned` | No | bool | `false` | MySQL `UNSIGNED` modifier. |
| `not_null` | No | bool | `false` | Emit `NOT NULL` constraint. |
| `auto_increment` | No | bool | `false` | Emit `AUTO_INCREMENT` / `SERIAL` / `IDENTITY`. |
| `default` | No | string | `null` | Raw `DEFAULT` expression, e.g. `"'pending'"`, `"0"`, `"CURRENT_TIMESTAMP"`. |
| `primary_key` | No | bool | `false` | Column is part of the primary key. |
| `unique` | No | bool | `false` | Column has a `UNIQUE` constraint. |
| `description` | No | string | `null` | Human-readable column description. |
| `constraints` | No | dict | `null` | Extra source-specific metadata (passed through). |
| `generation` | No | dict | `null` | Data generation hints — see [Generation rules](06-generation-rules.md). |
| `references` | No | list | `null` | Legacy single-column FK: `[parent_table, parent_column]`. |

## Minimal example

```yaml
version: "1.0"
tables:
  - name: users
    columns:
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
      - name: created_at
        type: timestamp
        not_null: true
        default: CURRENT_TIMESTAMP
```

## Serialization rules

- **Only non-default values are written** — boolean fields (`not_null`, `primary_key`, `unique`, `auto_increment`, `unsigned`) are omitted when `false`.  Numeric fields (`length`, `precision`, `scale`, `fsp`) are omitted when `null`.  `instance_count` is omitted when `1`.  `instance_suffix_format` is omitted when `"_{:04d}"`.  This keeps the YAML compact and human-readable.
- **Order is preserved** — `yaml.dump(sort_keys=False)` is used so the table and column order in the file matches the order you defined them.
- **Unicode-safe** — `allow_unicode=True` means column names and defaults with non-ASCII characters are written directly, not escaped.

## Python API

```python
from schema_parser import parse_ddl, dump_schema, load_canonical, emit_ddl

# DDL → canonical YAML file
tables = parse_ddl(mysql_ddl, dialect="mysql")
dump_schema(tables, path="schema.yaml")

# Canonical YAML file → DDL for any target
tables = load_canonical("schema.yaml")
print(emit_ddl(tables[0], dialect="postgres"))
print(emit_ddl(tables[0], dialect="sqlserver"))
print(emit_ddl(tables[0], dialect="oracle"))
```

## Related

- [Column types](02-column-types.md)
- [Column constraints](03-column-constraints.md)
- [Round-trip guarantee](09-round-trip-guarantee.md)
- [`src/schema_parser/model.py`](../../src/schema_parser/model.py) — Python dataclass definitions
- [`src/schema_parser/schema_io.py`](../../src/schema_parser/schema_io.py) — `dump_schema` / `load_canonical`
