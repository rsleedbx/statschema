# Foreign Key Constraints

Foreign key constraints capture referential integrity relationships between
tables.  The canonical model supports both single-column and composite
(multi-column) foreign keys.

## `fk_constraints` list

Foreign keys are declared at the table level under `fk_constraints`.  Each
entry is a `CanonicalForeignKey`:

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| `columns` | Yes | list[str] | Column(s) in **this** table that form the FK. |
| `parent_table` | Yes | string | Name of the referenced (parent) table. |
| `parent_columns` | Yes | list[str] | Column(s) in the parent table being referenced. |
| `name` | No | string | Constraint name (e.g. `fk_orders_customer`). |
| `parent_schema` | No | string | Schema of the parent table for cross-schema FKs. |

## Single-column FK

```yaml
tables:
  - name: orders
    fk_constraints:
      - columns: [customer_id]
        parent_table: customers
        parent_columns: [id]
        name: fk_orders_customer
    columns:
      - name: id
        type: long
        primary_key: true
        not_null: true
        auto_increment: true
      - name: customer_id
        type: long
        not_null: true
```

Emits (MySQL example):

```sql
CREATE TABLE orders (
  id BIGINT NOT NULL AUTO_INCREMENT,
  customer_id BIGINT NOT NULL,
  PRIMARY KEY (id),
  CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES customers (id)
);
```

## Composite (multi-column) FK

```yaml
tables:
  - name: order_line_items
    fk_constraints:
      - columns: [order_id, line_no]
        parent_table: order_lines
        parent_columns: [order_id, line_no]
        name: fk_line_items_order
    columns:
      - name: order_id
        type: long
        not_null: true
      - name: line_no
        type: integer
        not_null: true
      - name: quantity
        type: integer
        not_null: true
```

## Cross-schema FK

For databases that support schemas / namespaces:

```yaml
fk_constraints:
  - columns: [tenant_id]
    parent_table: tenants
    parent_columns: [id]
    parent_schema: shared
    name: fk_users_tenant
```

Emits `REFERENCES shared.tenants (id)` on dialects that support schema-qualified references.

## Legacy single-column reference (column-level)

For backward compatibility, a single-column FK can be recorded directly on the
column using the `references` field (a two-element list):

```yaml
- name: customer_id
  type: long
  not_null: true
  references: [customers, id]    # [parent_table, parent_column]
```

> **Prefer `fk_constraints`** at the table level.  The `references` field is
> kept for compatibility with older parsers and does not support composite FKs
> or named constraints.

## Statistics for FK columns

When collecting database statistics, FK relationships are captured in
`ForeignKeyStats` (part of `DatabaseStats`).  These are stored in the
statistics YAML separately from the schema YAML.

## Related

- [Core YAML structure](01-core-structure.md)
- [Round-trip guarantee](09-round-trip-guarantee.md)
- [`src/statschema/model.py`](../../src/statschema/model.py) — `CanonicalForeignKey` dataclass
