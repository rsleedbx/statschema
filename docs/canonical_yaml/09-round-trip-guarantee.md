# Round-Trip Guarantee

The canonical schema YAML is designed to preserve semantic meaning through
multiple parse → emit cycles.  A "round-trip" means that parsing DDL, emitting
to a target dialect, re-parsing the emitted DDL, and emitting again produces
byte-for-byte identical output on the second and all subsequent cycles.

## What is guaranteed

```
Source DDL (any dialect)
    │
    ▼  parse_ddl(ddl, dialect)
CanonicalTableSchema                     ← dialect-independent
    │
    ▼  dump_schema(tables, "schema.yaml")
canonical_schema.yaml                    ← portable, human-editable, versionable
    │
    ├──▶  load_canonical("schema.yaml")
    │         │
    │         ▼  emit_ddl(table, dialect)   e.g. "postgres"
    │     target DDL
    │         │
    │         ▼  parse_ddl(target_ddl, dialect)
    │     CanonicalTableSchema2
    │         │
    │         ▼  emit_ddl(table2, dialect)
    │     target DDL2
    │
    │     assert target_ddl == target_ddl2   ← idempotency guarantee
    │
    └──▶  emit_ddl(table, "mysql")           ← different target from same canonical
          emit_ddl(table, "sqlserver")
          emit_ddl(table, "oracle")
          emit_ddl(table, "databricks")
```

**Key property**: `emit_ddl(parse_ddl(emit_ddl(table, d), d), d) == emit_ddl(table, d)`
for every supported dialect `d`.

## What is normalised (intentional lossy steps)

The canonical model deliberately normalises some database-specific types.
These are not bugs — they are intentional widening or unification for
cross-database portability:

| Source type | Dialect | Canonical type | Emitted as | Reason |
|------------|---------|---------------|------------|--------|
| `TINYINT` | any | `integer` | `INT` | No sub-byte canonical type |
| `SMALLINT` | any | `integer` | `INT` | Widened for portability |
| `CHAR(n)` | any | `string` | `VARCHAR(n)` | CHAR/VARCHAR unified |
| `SERIAL` | PostgreSQL | `integer` | `INTEGER` | Sequence tracked via `auto_increment` |
| `MONEY` | SQL Server | `decimal(19,4)` | `DECIMAL(19,4)` | Fixed-precision widening |
| `ROWVERSION` | SQL Server | `binary` | `VARBINARY(8)` | No cross-DB equivalent |
| `UNIQUEIDENTIFIER` | SQL Server | `uuid` | `UNIQUEIDENTIFIER` | Full lossless |
| `NUMBER(p)` (p≤9) | Oracle | `integer` | `NUMBER(10)` | Widened to canonical bucket |
| `NUMBER(p)` (p>9) | Oracle | `long` | `NUMBER(19)` | Widened to canonical bucket |
| `CLOB` | Oracle | `string` | `CLOB` | Lossless |
| `NUMERIC(p)` (no scale) | PostgreSQL | `long` | `BIGINT` | Whole-number integer |

The normalisation is symmetric: once in canonical form, re-emitting and
re-parsing never widens further.

## Python API

### Parse → dump → load → emit

```python
from schema_parser import parse_ddl, dump_schema, load_canonical, emit_ddl

# Step 1: Parse source DDL into canonical form
mysql_ddl = """
CREATE TABLE orders (
  id BIGINT NOT NULL AUTO_INCREMENT,
  customer_id BIGINT NOT NULL,
  total DECIMAL(10,2) NOT NULL,
  status VARCHAR(20) DEFAULT 'pending',
  PRIMARY KEY (id)
);
"""
tables = parse_ddl(mysql_ddl, dialect="mysql")

# Step 2: Persist to YAML (portable, versionable)
dump_schema(tables, path="schema.yaml")

# Step 3: Load from YAML and emit to any dialect
tables2 = load_canonical("schema.yaml")
print(emit_ddl(tables2[0], dialect="postgres"))
print(emit_ddl(tables2[0], dialect="sqlserver"))
print(emit_ddl(tables2[0], dialect="oracle"))
print(emit_ddl(tables2[0], dialect="databricks"))
```

### Inline YAML string (no file)

```python
yaml_str = dump_schema(tables)                  # in-memory string
tables2  = load_canonical(yaml_str)             # parse inline YAML
tables3  = load_canonical(yaml_str, expand=True) # also expand multi-instance
```

### Emit all dialects at once

```python
from schema_parser import emit_ddl_all, SUPPORTED_DIALECTS

for dialect in SUPPORTED_DIALECTS:
    print(f"--- {dialect} ---")
    print(emit_ddl_all(tables, dialect))
```

### `emit_ddl_all` vs `emit_ddl`

| Function | Input | Output |
|----------|-------|--------|
| `emit_ddl(table, dialect)` | Single `CanonicalTableSchema` | DDL string for one table |
| `emit_ddl_all(tables, dialect)` | List of `CanonicalTableSchema` | DDL string for all tables, separated by `\n` |

## Test coverage

The round-trip guarantee is enforced by `tests/test_ddl_roundtrip.py`:

- **Phase 1** (~185 parametrized cases): every canonical type × every constraint × every parseable dialect.
- **Phase 2** (60 random tables per dialect): random column mixes, seeded for determinism.
- **Live tests** (`tests/test_live_roundtrip.py`): the same cases executed against real MySQL 5.7 + 8.x, PostgreSQL 14 + 16, and SQL Server 2022 instances to confirm the emitted DDL is syntactically accepted by each engine.

## `dump_schema` / `load_canonical` round-trip

The YAML itself is also lossless — `load_canonical(dump_schema(tables))` returns
tables that are semantically identical to the originals:

```python
from schema_parser import dump_schema, load_canonical

yaml_str = dump_schema(original_tables)
restored = load_canonical(yaml_str)

for orig, rest in zip(original_tables, restored):
    assert orig.name == rest.name
    assert len(orig.columns) == len(rest.columns)
    for c1, c2 in zip(orig.columns, rest.columns):
        assert c1.name == c2.name
        assert c1.type == c2.type
```

## Supported dialects

```python
from schema_parser import SUPPORTED_DIALECTS
# → ["mysql", "postgres", "sqlserver", "oracle", "databricks"]
```

Parsing is supported for: `mysql`, `postgres`, `sqlserver` (and aliases `tsql`,
`mssql`), `oracle`.  Databricks DDL can be emitted but uses MySQL as the
parse dialect.

## Related

- [Core YAML structure](01-core-structure.md)
- [Column types](02-column-types.md)
- [Multi-instance expansion](08-multi-instance-expansion.md)
- [`tests/test_ddl_roundtrip.py`](../../tests/test_ddl_roundtrip.py) — round-trip test suite
- [`tests/test_live_roundtrip.py`](../../tests/test_live_roundtrip.py) — live database round-trip tests
- [`docs/testing.md`](../testing.md) — full testing strategy
