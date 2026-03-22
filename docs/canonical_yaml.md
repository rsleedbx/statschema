# Canonical Schema YAML — Feature Reference

The canonical schema YAML is the portable, human-editable interchange format
that sits at the centre of the pipeline.  It is database-agnostic: one YAML
file can be used to emit valid DDL for MySQL, PostgreSQL, SQL Server, Oracle,
and Databricks, and to drive statistics-driven synthetic data generation.

Each feature has its own page so it can be linked and shared independently.

---

## Document structure

1. [Core YAML structure](canonical_yaml/01-core-structure.md) — `version`, `tables`, the top-level document shape
2. [Column types](canonical_yaml/02-column-types.md) — the 14 canonical types and their per-dialect mappings
3. [Column constraints](canonical_yaml/03-column-constraints.md) — `not_null`, `primary_key`, `unique`, `auto_increment`, `default`
4. [Column precision & length](canonical_yaml/04-column-precision-length.md) — `length`, `precision`, `scale`, `fsp`, `unsigned`
5. [Foreign key constraints](canonical_yaml/05-foreign-keys.md) — `fk_constraints` (single and composite FKs)
6. [Data generation rules](canonical_yaml/06-generation-rules.md) — `generation:` field, distributions, value pools, injection flags
7. [Temporal ordering constraints](canonical_yaml/07-temporal-ordering.md) — `temporal_ordering_constraints` for date/time columns
8. [Multi-instance expansion](canonical_yaml/08-multi-instance-expansion.md) — `aliases`, `instance_count`, `instance_suffix_format`
9. [Round-trip guarantee](canonical_yaml/09-round-trip-guarantee.md) — `dump_schema`, `load_canonical`, `parse_ddl`, `emit_ddl`

---

## Quick-start example

```yaml
version: "1.0"
tables:
  - name: orders
    description: "Customer orders"
    fk_constraints:
      - columns: [customer_id]
        parent_table: customers
        parent_columns: [id]
        name: fk_orders_customer
    temporal_ordering_constraints:
      - "shipped_at > created_at"
    columns:
      - name: id
        type: long
        primary_key: true
        not_null: true
        auto_increment: true
      - name: customer_id
        type: long
        not_null: true
      - name: customer_ssn
        type: string
        length: 11
        comment: "Customer social security number"   # SQL COMMENT clause → drives SSN generation
      - name: total
        type: decimal
        precision: 10
        scale: 2
        not_null: true
      - name: status
        type: string
        length: 20
        default: "'pending'"
        generation:
          distribution: zipf
          values: [pending, confirmed, shipped, delivered, cancelled]
          weights: [0.30, 0.25, 0.20, 0.20, 0.05]
      - name: created_at
        type: timestamp
        not_null: true
      - name: shipped_at
        type: timestamp
```

## Key API functions

| Function | What it does |
|----------|-------------|
| `parse_ddl(ddl, dialect)` | Source DDL → list of `CanonicalTableSchema`; populates `col.comment` from SQL `COMMENT` clauses |
| `dump_schema(tables, path)` | Canonical tables → YAML string (+ optional file write) |
| `load_canonical(path, expand=False)` | YAML → list of `CanonicalTableSchema` |
| `emit_ddl(table, dialect)` | Single `CanonicalTableSchema` → DDL string; inline `COMMENT` for MySQL/Databricks |
| `emit_ddl_all(tables, dialect)` | List of tables → DDL string |
| `emit_column_comments(table, dialect)` | List of `COMMENT ON COLUMN` statements for PostgreSQL/Oracle; empty for other dialects |
| `expand_table_instances(tables)` | Resolve `aliases` / `instance_count` into flat list |

---

*See also: [`docs/testing.md`](testing.md) · [`docs/local-databases.md`](local-databases.md) · [`src/statschema/model.py`](../src/statschema/model.py)*
