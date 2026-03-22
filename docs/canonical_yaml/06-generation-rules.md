# Data Generation Rules

Each column can carry a `generation:` block that guides the synthetic data
generator (`dbldatagen`) on how to produce realistic values for that column.
All fields are optional — omitting `generation:` entirely lets the builder
apply sensible defaults based on the column's type and any collected statistics.

## `generation` fields

### Range / value pool

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `min_value` | any | `null` | Minimum value for numeric or temporal columns. |
| `max_value` | any | `null` | Maximum value for numeric or temporal columns. |
| `values` | list | `null` | Explicit list of allowed values (acts as an enum). |
| `weights` | list[float] | `null` | Probability weights for each entry in `values` (must sum to 1.0 or will be normalised). |
| `max_length` | int | `null` | Maximum string length override (overrides column `length`). |
| `unique` | bool | `false` | Force every generated value to be distinct (uses dbldatagen sequential mode). |

### Distribution control

| Field | Values | Default | Description |
|-------|--------|---------|-------------|
| `distribution` | `"auto"`, `"uniform"`, `"normal"`, `"zipf"`, `"exponential"`, `"constant"`, `"sequential"` | `"auto"` | How values are sampled. |
| `distribution_params` | dict | `{}` | Distribution-specific parameters (see below). |

#### Distribution parameter reference

| Distribution | Parameters | Example |
|-------------|------------|---------|
| `uniform` | — | — |
| `normal` | `mean`, `std` | `{mean: 50.0, std: 15.0}` |
| `zipf` | `a` (exponent; 1.0 = classic Zipf) | `{a: 1.5}` |
| `exponential` | `scale` (= 1/λ) | `{scale: 1.0}` |
| `constant` | — | (put the value in `values[0]`) |
| `sequential` | — | Used for PK columns |

### String format pattern

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `format_pattern` | string | `null` | Named pattern or Python regex for realistic string generation. |

#### Named patterns

| Pattern | Example output |
|---------|---------------|
| `"email"` | `user@domain.tld` |
| `"phone_us"` | `+1-555-867-5309` |
| `"phone_intl"` | `+44-20-1234-5678` |
| `"uuid"` | `550e8400-e29b-41d4-a716-446655440000` |
| `"ip_v4"` | `192.168.1.42` |
| `"ip_v6"` | `2001:db8::1` |
| `"url"` | `https://example.com/path` |
| `"postal_us"` | `94102` or `94102-1234` |
| `"postal_uk"` | `SW1A 1AA` |
| `"ssn"` | `123-45-6789` |
| `"credit_card"` | `4532-0151-1283-0366` |
| `"iban"` | `GB82 WEST 1234 5698 7654 32` |
| `"name_first"` | `Alice` |
| `"name_last"` | `Nguyen` |
| `"company"` | `Acme Corp` |
| `"address"` | `123 Main St` |
| `"city"` | `Springfield` |
| `"country_iso2"` | `DE` |
| `"currency_iso"` | `USD` |

Regex fallback (when the pattern is not a named one):

```yaml
format_pattern: "[A-Z]{2}\\d{6}"    # e.g. passport numbers: AB123456
```

### Injection flags

These flags default to `true` (safe for test coverage).  Set to `false` to
disable a specific injection behaviour.

| Field | Default | Description |
|-------|---------|-------------|
| `inject_boundary_values` | `true` | Guarantee at least one row at `min_value` and one at `max_value`. Essential for boundary and precision tests. |
| `inject_nulls_from_stats` | `true` | Apply `ColumnStats.null_fraction` as the NULL probability. Set `false` to always generate non-null values. |
| `use_mcv_weights` | `true` | Apply `ColumnStats.most_common_values` frequencies as value weights (hot values appear at their real-world frequency). |
| `inject_rare_events` | `false` | Add a small number of rows with tail-distribution values not in the top MCVs. Useful for error-path testing. |

### Escape hatch

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `extra` | dict | `{}` | Generator-specific options passed through verbatim to dbldatagen. |

---

## Examples

### Status column with Zipf distribution

```yaml
- name: status
  type: string
  length: 20
  default: "'pending'"
  generation:
    distribution: zipf
    distribution_params: {a: 1.5}
    values: [pending, confirmed, shipped, delivered, cancelled]
    weights: [0.30, 0.25, 0.20, 0.20, 0.05]
    use_mcv_weights: true
```

### Email column with format pattern

```yaml
- name: email
  type: string
  length: 255
  not_null: true
  unique: true
  generation:
    format_pattern: email
    unique: true
```

### Numeric range with boundary injection

```yaml
- name: score
  type: integer
  not_null: true
  generation:
    min_value: 0
    max_value: 100
    distribution: uniform
    inject_boundary_values: true
```

### Nullable column with null fraction from stats

```yaml
- name: shipped_at
  type: timestamp
  generation:
    inject_nulls_from_stats: true    # NULL ~20% of the time if stats say so
    inject_boundary_values: false    # timestamps — boundaries not useful
```

### Constant column (all rows get the same value)

```yaml
- name: data_source
  type: string
  length: 50
  generation:
    distribution: constant
    values: ["production"]
```

### Sequential primary key (always unique, always increasing)

```yaml
- name: id
  type: long
  primary_key: true
  not_null: true
  auto_increment: true
  generation:
    distribution: sequential
    unique: true
```

---

## How generation rules interact with database statistics

When `DatabaseStats` are available (from `collect_table_stats()` or
`load_stats()`), the builder merges rule fields with the real statistics:

1. Explicit `min_value` / `max_value` in the rule take precedence over stats.
2. `use_mcv_weights: true` applies stats MCVs as value weights — but only when
   the MCVs account for > 50% of the data, or when the column has ≤ 20 distinct
   values (to avoid collapsing high-cardinality columns).
3. `inject_nulls_from_stats: true` uses the measured `null_fraction` from stats.
4. Boolean columns are never driven by MCVs (avoids Spark type mismatches).

---

## Related

- [Column types](02-column-types.md)
- [Temporal ordering constraints](07-temporal-ordering.md)
- [`src/statschema/dbldatagen_builder.py`](../../src/statschema/dbldatagen_builder.py) — translates generation rules to dbldatagen specs
- [`src/statschema/model.py`](../../src/statschema/model.py) — `GenerationRule` dataclass
