# DBA YAML Guide

A DBA writes one YAML file and runs three commands. No Python required.

```
python -m statschema ddl      schema.yaml --dialect postgres
python -m statschema generate schema.yaml --sf 1 --out-dir ./data
python -m statschema load     schema.yaml --dialect postgres --dsn "host=... dbname=..."
```

The YAML file is the single source of truth for DDL, column types, value distributions,
row counts, load order, and FK relationships.

---

## File structure

```yaml
version: "1.0"
tables:
  - name: region
    description: "..."
    row_count: 5
    columns:
      - name: r_regionkey
        type: integer
        not_null: true
        primary_key: true
        ...
  - name: order
    row_count_per_sf: 1500000
    load_after: [customer]
    fk_constraints:
      - columns: [o_custkey]
        parent_table: customer
        parent_columns: [c_custkey]
    columns:
      - ...
```

---

## Table-level keys

### `name` *(required)*
Table name. Used verbatim in DDL and INSERT statements.

### `description`
Free-text comment. Emitted as `COMMENT ON TABLE` where the dialect supports it.

### `row_count`
Fixed number of rows, independent of scale factor. Use for lookup / dimension tables
whose size is part of the spec (e.g. TPC-H `region` = 5 rows).

### `row_count_per_sf`
Rows per one unit of scale factor. At runtime the CLI multiplies this by `--sf`.

```
row_count_per_sf: 1500000   # SF=1 → 1.5 M rows, SF=10 → 15 M rows
```

Either `row_count` or `row_count_per_sf` is required on every table.

### `load_after`
List of table names that must be loaded before this table. Controls INSERT order
when FK constraints are present. The CLI resolves a topological sort automatically.

```yaml
load_after: [customer, part, supplier]
```

### `fk_constraints`
Declares foreign-key relationships used to constrain generated child FK values
to the range of existing parent primary-key values.

```yaml
fk_constraints:
  - columns: [o_custkey]          # FK column(s) in this table
    parent_table: customer         # parent table name
    parent_columns: [c_custkey]    # matching PK column(s) in the parent
    name: fk_orders_customer       # constraint name (used in DDL)
    fk_distribution: zipf          # how to distribute FK values across parents
    fk_distribution_params:
      exponent: 1.2
    fk_children_min: 1             # min child rows per parent
    fk_children_max: 30            # max child rows per parent
```

`fk_distribution` values:

| Value | Description |
|:------|:------------|
| `uniform` | Each parent is equally likely to be referenced. |
| `zipf`    | Hot-parent pattern: a small set of parents gets most children. |

---

## Column-level keys

### `name` *(required)*
Column name. Used verbatim in DDL and INSERT statements.

### `type` *(required)*
Logical type. Maps to the correct SQL type per dialect.

| YAML type | PostgreSQL | MySQL | SQL Server | Oracle | Db2 |
|:----------|:-----------|:------|:-----------|:-------|:----|
| `integer` | `INTEGER` | `INT` | `INT` | `NUMBER(10)` | `INTEGER` |
| `long`    | `BIGINT` | `BIGINT` | `BIGINT` | `NUMBER(19)` | `BIGINT` |
| `decimal(p,s)` | `NUMERIC(p,s)` | `DECIMAL(p,s)` | `DECIMAL(p,s)` | `NUMBER(p,s)` | `DECIMAL(p,s)` |
| `string`  | `VARCHAR(n)` | `VARCHAR(n)` | `NVARCHAR(n)` | `VARCHAR2(n)` | `VARCHAR(n)` |
| `boolean` | `BOOLEAN` | `TINYINT(1)` | `BIT` | `NUMBER(1)` | `SMALLINT` |
| `date`    | `DATE` | `DATE` | `DATE` | `DATE` | `DATE` |
| `timestamp` | `TIMESTAMP` | `DATETIME` | `DATETIME2` | `TIMESTAMP` | `TIMESTAMP` |
| `float` / `double` | `FLOAT` | `DOUBLE` | `FLOAT` | `FLOAT` | `DOUBLE` |

### `length`
Character length for `string` columns.

### `precision` / `scale`
Numeric precision and scale for `decimal` columns.

### `not_null`
Emits `NOT NULL` in DDL. Default: `false`.

### `primary_key`
Marks the column as part of the table's primary key. Multiple columns can be marked
`primary_key: true` to form a composite PK.

### `unique`
Emits a `UNIQUE` constraint.

### `description`
Free-text comment. Emitted as `COMMENT ON COLUMN` where supported.

### `references`
Soft FK reference for semantic hints. Does not emit a DDL `REFERENCES` clause.

```yaml
references: [parent_table, parent_column]
```

---

## Generation rules

Every column can carry a `generation:` block. Without one the generator produces
random strings or integers within the column's type bounds.

```yaml
- name: status
  type: string
  length: 1
  generation:
    values: ["F", "O", "P"]
    weights: [0.49, 0.49, 0.02]
```

### `distribution`

| Value | Description |
|:------|:------------|
| `uniform` | Uniform random within `[min_value, max_value]`. Default for numeric/date. |
| `sequential` | Monotonically increasing integers starting from `min_value`. |
| `constant` | Always the single value in `[min_value, max_value]`. |
| `normal` | Normal distribution. Requires `distribution_params: {mean: M, std: S}`. |
| `zipf` | Zipfian distribution. Requires `distribution_params: {exponent: E}`. |

### `min_value` / `max_value`
Bounds for numeric and date columns.

```yaml
generation:
  min_value: "1992-01-01"
  max_value: "1998-12-31"
  distribution: uniform
```

### `values` and `weights`
Generates values by sampling from a fixed list. `weights` (optional) sets the
probability of each value; they must sum to 1.

```yaml
generation:
  values: ["AUTOMOBILE", "BUILDING", "FURNITURE", "HOUSEHOLD", "MACHINERY"]
  weights: [0.2, 0.2, 0.2, 0.2, 0.2]
```

### `unique`
Ensures no duplicate values are generated for this column (e.g. for a PK sequence).

```yaml
generation:
  distribution: sequential
  min_value: 1
  unique: true
```

### `format_pattern`
Generates realistic-looking strings for common column types.

| Pattern | Example output |
|:--------|:---------------|
| `phone_us` | `555-321-7890` |
| `phone_intl` | `24-319-442-7801` (TPC-H format, exactly 15 chars) |
| `postal_us` | `90210` |
| `email` | `user@example.com` |
| `name_first` | `James` |
| `name_last` | `Smith` |
| `address` | `123 Main St` |
| `url` | `https://example.com` |
| `uuid` | `550e8400-e29b-41d4-a716-446655440000` |

```yaml
generation:
  format_pattern: "phone_intl"
```

### `distribution_params`
Additional parameters for distributions that require them.

```yaml
generation:
  distribution: normal
  distribution_params:
    mean: 150000.0
    std:  80000.0
```

---

## Temporal ordering constraints

`temporal_ordering_constraints` specifies ordering relationships between date/timestamp
columns in the same table. The generator applies a post-processing fix-up to ensure
the constraint holds.

```yaml
temporal_ordering_constraints:
  - "l_shipdate <= l_receiptdate"
```

---

## Minimal working example

```yaml
version: "1.0"
tables:

  - name: product
    description: "Product catalogue"
    row_count: 10000
    columns:
      - name: product_id
        type: integer
        not_null: true
        primary_key: true
        generation:
          distribution: sequential
          min_value: 1
          unique: true
      - name: name
        type: string
        length: 60
        not_null: true
      - name: category
        type: string
        length: 20
        generation:
          values: ["Electronics", "Clothing", "Food", "Books"]
          weights: [0.25, 0.35, 0.25, 0.15]
      - name: price
        type: decimal
        precision: 10
        scale: 2
        generation:
          min_value: 0.99
          max_value: 999.99
          distribution: uniform

  - name: order
    description: "Customer orders"
    row_count_per_sf: 500000
    load_after: [product]
    fk_constraints:
      - columns: [product_id]
        parent_table: product
        parent_columns: [product_id]
        name: fk_order_product
        fk_distribution: zipf
        fk_distribution_params: {exponent: 1.5}
    columns:
      - name: order_id
        type: long
        not_null: true
        primary_key: true
        generation:
          distribution: sequential
          min_value: 1
          unique: true
      - name: product_id
        type: integer
        not_null: true
        description: "FK → product.product_id"
      - name: quantity
        type: integer
        not_null: true
        generation:
          min_value: 1
          max_value: 50
          distribution: uniform
      - name: order_date
        type: date
        not_null: true
        generation:
          min_value: "2022-01-01"
          max_value: "2025-12-31"
          distribution: uniform
      - name: status
        type: string
        length: 10
        generation:
          values: ["pending", "shipped", "delivered", "cancelled"]
          weights: [0.1, 0.2, 0.65, 0.05]
```

Load it into PostgreSQL:

```bash
python -m statschema ddl   myschema.yaml --dialect postgres > schema.sql
python -m statschema load  myschema.yaml --dialect postgres --sf 1 \
    --dsn "host=localhost dbname=mydb user=postgres password=secret"
```

---

## Complete key reference

| Key | Scope | Required | Description |
|:----|:------|:--------:|:------------|
| `version` | file | yes | Schema file version. Always `"1.0"`. |
| `tables[].name` | table | yes | Table name. |
| `tables[].description` | table | | Free-text comment. |
| `tables[].row_count` | table | one of | Fixed row count (ignores SF). |
| `tables[].row_count_per_sf` | table | one of | Rows per SF unit. |
| `tables[].load_after` | table | | Tables to load first. |
| `tables[].fk_constraints[].columns` | table | | FK column list in this table. |
| `tables[].fk_constraints[].parent_table` | table | | Parent table name. |
| `tables[].fk_constraints[].parent_columns` | table | | Matching PK columns. |
| `tables[].fk_constraints[].name` | table | | Constraint name (DDL). |
| `tables[].fk_constraints[].fk_distribution` | table | | `uniform` or `zipf`. |
| `tables[].fk_constraints[].fk_distribution_params` | table | | e.g. `{exponent: 1.2}` |
| `tables[].fk_constraints[].fk_children_min` | table | | Min child rows per parent. |
| `tables[].fk_constraints[].fk_children_max` | table | | Max child rows per parent. |
| `tables[].temporal_ordering_constraints` | table | | e.g. `["a <= b"]` |
| `columns[].name` | column | yes | Column name. |
| `columns[].type` | column | yes | Logical type (see type table above). |
| `columns[].length` | column | | String length. |
| `columns[].precision` / `scale` | column | | Decimal precision. |
| `columns[].not_null` | column | | Emit `NOT NULL`. |
| `columns[].primary_key` | column | | Part of PK. |
| `columns[].unique` | column | | Emit `UNIQUE`. |
| `columns[].description` | column | | Free-text comment. |
| `columns[].references` | column | | Soft FK: `[table, column]`. |
| `generation.distribution` | column | | `uniform` `sequential` `constant` `normal` `zipf` |
| `generation.min_value` | column | | Lower bound (numeric or date string). |
| `generation.max_value` | column | | Upper bound. |
| `generation.values` | column | | Fixed value list. |
| `generation.weights` | column | | Sampling weights for `values`. |
| `generation.unique` | column | | No duplicate values. |
| `generation.format_pattern` | column | | Named realistic-string generator. |
| `generation.distribution_params` | column | | Extra params (`mean`, `std`, `exponent`). |
