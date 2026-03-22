# Multi-Instance Expansion

Large applications often create many physical tables that share the exact same
schema — Mautic, for example, creates hundreds of `email_stats_N` shards for
horizontal partitioning.  Rather than duplicating the column definition for
each shard, the canonical YAML lets you define the schema once and declare how
many copies are needed.

## Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `aliases` | list[str] | `[]` | Explicit additional table names that share this definition. |
| `instance_count` | int | `1` | Number of numbered copies to generate (replaces the base name when > 1). |
| `instance_suffix_format` | string | `"_{:04d}"` | Python format string for the numeric suffix: `_{:04d}` → `_0001`, `_0002`, … |

All three fields are optional.  When all are at their defaults, the table
behaves exactly as a plain single-table definition (fully backward compatible).

---

## `instance_count` — numbered shards

When `instance_count > 1`, the base table name is **replaced** by N numbered
copies.  The suffix is produced by calling
`instance_suffix_format.format(i)` for `i` in `1 .. instance_count`.

```yaml
- name: form_results
  instance_count: 1000
  columns:
    - name: id
      type: long
      primary_key: true
      not_null: true
      auto_increment: true
    - name: form_id
      type: long
      not_null: true
    - name: submitted_at
      type: timestamp
      not_null: true
```

`expand_table_instances()` resolves this to:
`form_results_0001`, `form_results_0002`, …, `form_results_1000`

The base name `form_results` does **not** appear in the expanded output when
`instance_count > 1`.

### Custom suffix format

```yaml
- name: shard
  instance_count: 256
  instance_suffix_format: "_{:03d}"   # _001 … _256
```

```yaml
- name: bucket
  instance_count: 10
  instance_suffix_format: "_{}"       # _1 … _10  (no zero-padding)
```

The format string must contain exactly one positional placeholder `{}` or
`{:Nd}`.  An invalid format raises a `ValueError` when expansion is attempted.

---

## `aliases` — explicit named copies

Use `aliases` when the tables have semantically different names but share the
same schema — for example, a live table alongside archive and staging variants.

```yaml
- name: audit_log
  aliases:
    - audit_log_archive
    - audit_log_staging
  columns:
    - name: id
      type: long
      primary_key: true
      not_null: true
      auto_increment: true
    - name: action
      type: string
      length: 100
      not_null: true
    - name: occurred_at
      type: timestamp
      not_null: true
```

Expands to: `audit_log`, `audit_log_archive`, `audit_log_staging`

Unlike `instance_count`, the base name **is always included** when only aliases
are set.

---

## Combining `instance_count` and `aliases`

Both can be specified on the same table.  Numbered instances are generated
first (base name replaced); aliases are appended as extra un-numbered copies:

```yaml
- name: events
  instance_count: 3
  aliases: [events_archive]
  columns:
    - name: id
      type: long
      primary_key: true
```

Expands to: `events_0001`, `events_0002`, `events_0003`, `events_archive`

---

## Expanding to a flat list

The compact YAML form (with `instance_count` / `aliases`) is preserved in
memory by default.  Call `expand_table_instances()` — or use
`load_canonical(path, expand=True)` — to resolve it into individual
`CanonicalTableSchema` objects before DDL emission or data generation.

### `expand_table_instances()`

```python
from statschema import CanonicalTableSchema, CanonicalColumn, expand_table_instances

table = CanonicalTableSchema(
    name="email_stats",
    instance_count=1000,
    columns=[CanonicalColumn(name="id", type="long", primary_key=True)],
)
tables = expand_table_instances(table)
# → 1000 CanonicalTableSchema objects: email_stats_0001 … email_stats_1000
```

### `load_canonical(path, expand=True)`

```python
from statschema import load_canonical, emit_ddl

# Compact form — preserves instance_count for re-serialization
tables = load_canonical("schema.yaml")

# Expanded form — one table per physical DDL target
tables = load_canonical("schema.yaml", expand=True)
for t in tables:
    print(emit_ddl(t, dialect="mysql"))
```

### Programmatic construction

```python
tables = expand_table_instances(load_canonical("schema.yaml"))
```

---

## Properties of expanded tables

- Expanded copies **share the same column list object** — mutations to columns
  affect all copies.  Clone the column list if you need independent copies.
- FK references (`fk_constraints`, `references`) are copied as-is.  If the
  parent table is also expanded (e.g. `customers` → `customers_0001`), update
  FK references manually or with `OverrideSpec`.
- After expansion, `aliases` is `[]`, `instance_count` is `1`, and
  `instance_suffix_format` is `"_{:04d}"` on every copy — making expansion
  idempotent.

---

## YAML serialization

Non-default values are written to YAML; defaults are omitted to keep the file
compact:

```yaml
# instance_count omitted (= 1), aliases omitted (= []),
# instance_suffix_format omitted (= "_{:04d}"):
- name: users
  columns: [...]

# Only instance_count written (suffix format is default):
- name: form_results
  instance_count: 500
  columns: [...]

# Custom suffix format written explicitly:
- name: shard
  instance_count: 100
  instance_suffix_format: "_{:03d}"
  columns: [...]

# Only aliases written:
- name: audit_log
  aliases: [audit_log_archive]
  columns: [...]
```

---

## Real-world patterns

### Mautic horizontal sharding

Mautic creates dozens of tables with the same schema differentiated only by a
numeric suffix (e.g. `email_stats`, `email_stats_1`, `email_stats_2`).  With
`instance_count` you define the schema once:

```yaml
- name: email_stats
  instance_count: 50
  instance_suffix_format: "_{}"    # → _1, _2, … _50  (Mautic style)
  columns:
    - name: id
      type: long
      primary_key: true
      not_null: true
      auto_increment: true
    - name: email_id
      type: long
      not_null: true
    - name: lead_id
      type: long
    - name: date_sent
      type: timestamp
```

### Multi-tenant per-tenant tables

```yaml
- name: tenant_events
  instance_count: 200
  instance_suffix_format: "_{:04d}"
  columns:
    - name: event_id
      type: long
      primary_key: true
      not_null: true
      auto_increment: true
    - name: event_type
      type: string
      length: 100
      not_null: true
    - name: occurred_at
      type: timestamp
      not_null: true
```

---

## Tests

`tests/test_table_instances.py` contains 36 tests covering all expansion
behaviours, YAML round-trips, and edge cases (invalid format strings,
idempotency, combined `instance_count` + `aliases`).

---

## Related

- [Core YAML structure](01-core-structure.md)
- [Round-trip guarantee](09-round-trip-guarantee.md)
- [`src/statschema/model.py`](../../src/statschema/model.py) — `CanonicalTableSchema`, `expand_table_instances`
- [`tests/test_table_instances.py`](../../tests/test_table_instances.py) — full test suite for this feature
