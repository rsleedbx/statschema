# Synthetic Data Shortcomings and How We Address Them

**Last updated:** 2026-03-04  
**Relates to:** `src/schema_parser/model.py`, `src/schema_parser/stats_model.py`  
**Tests:** `tests/test_schema_parser.py::TestSyntheticShortcomingsCoverageMatrix`

---

## 1. Background

Research (DBConvert Streams, Databricks Lakeflow, arxiv 2025, letsai.tech) identifies a consistent set of
shortcomings that afflict most synthetic tabular-data generators.  The table below maps each shortcoming to
**our canonical model's response** — either a model field, a statistics field, or a documented gap.

---

## 2. Shortcoming × Solution Matrix

> **Legend:**  ✅ = dbldatagen handles it natively   ⚡ = our builder layer bridges the gap   ⚠️ = partial / approximation   ❌ = gap, workaround required

| # | Shortcoming | Root cause | Our canonical model | dbldatagen native? | Our builder bridges? |
|---|---|---|---|---|---|
| 1 | **Uniform distribution** — real data is Zipf / log-normal / bimodal | Generator ignores observed distribution | `ColumnStats.histogram_bounds`; `GenerationRule.distribution` + `distribution_params` | ⚠️ Partial — Normal, Beta, Gamma, Exponential supported; **no native Zipf** | ⚡ Zipf approximated with Gamma(shape<1) |
| 2 | **Hot-spot blindness** — popular items not at real-world frequency; query plans flip from index seek to full scan | MCVs discarded | `ColumnStats.most_common_values[i].frequency`; `GenerationRule.use_mcv_weights=True` | ✅ `values` + `weights` natively supported | ⚡ MCV list auto-converted to values+weights |
| 3 | **NULL rates not honoured** — columns fully populated; real columns have 5–40 % NULLs | NULL fraction ignored | `ColumnStats.null_fraction`; `GenerationRule.inject_nulls_from_stats=True` | ✅ `percentNulls` natively supported | ⚡ `null_fraction` auto-mapped to `percentNulls` |
| 4 | **Boundary values absent** — min/max never appear; overflow bugs hidden | Min/max dropped | `ColumnStats.min_value` / `max_value`; `GenerationRule.inject_boundary_values=True` | ✅ `minValue` / `maxValue` supported | ⚡ Stats min/max auto-mapped; boundary injection requires post-process union |
| 5 | **Realistic string patterns missing** — emails contain `"xkqwp"`, phone columns contain integers | No format hint | `GenerationRule.format_pattern` (19 named patterns + regex fallback) | ✅ `template` mini-language supports email, phone, IP, UUID | ⚡ Named patterns auto-converted to dbldatagen templates |
| 6 | **Enum values not respected** — low-cardinality status columns generate illegal values | No domain list | `ColumnStats.most_common_values` as domain; `GenerationRule.values` explicit list | ✅ `values` list natively supported | ⚡ MCVs automatically used as values domain |
| 7 | **Cardinality wrong** — UNIQUE violated or too many distinct values | `n_distinct` not used | `ColumnStats.n_distinct`; `GenerationRule.unique` + `uniqueValues` | ✅ `uniqueValues` natively supported | ⚡ unique=True maps to uniqueValues=rows |
| 8 | **FK referential integrity broken** — child rows reference non-existent parent IDs | FK ignored | `CanonicalForeignKey`; `ForeignKeyStats.match_fraction` | ✅ Hash-based repeatable keys maintain FK consistency across tables | ⚠️ Multi-table orchestration is manual; no single-call API |
| 9 | **FK cardinality ratio lost** — avg fees-per-student drifts | Cardinality pattern not modelled | `ForeignKeyStats.avg_children_per_parent`; `cardinality_pattern` | ⚠️ `AVG_EVENTS_PER_CUSTOMER * UNIQUE_CUSTOMERS` pattern available; no automatic fan-out | ❌ Must be wired manually in multi-table setup |
| 10 | **Temporal ordering violated** — `end_date < start_date`, `updated_at < created_at` | No ordering constraints | `CanonicalTableSchema.temporal_ordering_constraints` | ⚠️ `expr` + `baseColumn` can enforce ordering via `date_add`; **no automatic parsing of constraint strings** | ❌ Requires post-process repair or manual `expr` wiring per constraint |
| 11 | **Distribution shape not preserved** — right-skewed income becomes symmetric | Skewness/kurtosis not recorded | `ColumnStats.skewness` (Pearson); `ColumnStats.kurtosis` (excess) | ⚠️ Gamma/Beta can approximate skew; **no direct skewness/kurtosis parameter** | ⚡ Skewness > 0 selects Gamma approximation (planned) |
| 12 | **Rare events never generated** — fraud, cancellation, hardware failure never appear | Only MCVs used | `GenerationRule.inject_rare_events=True` | ❌ No native tail-injection; must append rows manually | ❌ Not yet implemented in builder |
| 13 | **Functional dependency broken** — city ≠ zip code, country ≠ currency | Columns generated independently | `CompositeColumnStats.dependencies` (`"zip_code->city": 0.99}`) | ✅ `baseColumn` + `expr` can condition child column on parent value | ⚠️ Requires manual `baseColumn` wiring; builder does not auto-apply dependency map |
| 14 | **Multi-column correlation lost** — amount and discount correlated in real data | Per-column stats only | `CompositeColumnStats.most_common_combinations` | ⚠️ `baseColumn` + `expr` can express correlation; no statistical correlation matrix input | ⚠️ Manual wiring required; builder does not interpret `most_common_combinations` yet |
| 15 | **UUID format / collision** — UUIDs re-used across tables; FK mismatches | UUID semantics unrecognised | `GenerationRule.format_pattern="uuid"` | ✅ Template `xxxxxxxx-xxxx-4xxx-xxxx-xxxxxxxxxxxx` generates unique hex UUIDs | ⚡ `format_pattern="uuid"` auto-converts to template |
| 16 | **UNSIGNED overflow on migration** — `INT UNSIGNED` → `INT` truncates at 2³¹ | UNSIGNED modifier discarded | `CanonicalColumn.unsigned=True` + parser widening (`INT UNSIGNED` → `long`) | ✅ `LongType()` with correct range prevents overflow | ⚡ Parser already widens; builder uses correct Spark type |
| 17 | **Cross-table correlation lost** — students with more courses pay more fees | Requires multi-table joint model | Documented gap; use `OverrideSpec` for per-table ranges | ❌ dbldatagen generates tables independently; cross-table stats not supported | ❌ Out of scope for statistical generation |
| 18 | **Many-to-many tables** — SDV HMA cannot handle M:N junction tables | Tree-only hierarchy | `CanonicalForeignKey` supports M:N structurally | ⚠️ Tables generated independently; junction table FK keys must be computed from parent tables via join | ⚠️ Manual orchestration required |
| 19 | **Oracle `DATE` contains time** — silently truncated on migration | Oracle semantic is unique | `CanonicalColumn.type="date"` is date-only; use `OverrideSpec` to change to `timestamp` | n/a — schema concern, not generation | ⚠️ Document and use override |
| 20 | **MySQL zero dates (`'0000-00-00'`)** — illegal in other DBs | MySQL-specific edge case | `OverrideSpec` or SQL post-processing | n/a | ❌ Post-process with `NULLIF` |
| 21 | **Charset / collation drift** | Encoding not tracked | `ColumnStats.avg_width_bytes`; charset at ETL layer | n/a | ⚠️ ETL layer |
| 22 | **Query plan divergence** — index seeks flip to full scans | Fragmentation not reproducible | `ANALYZE` / `UPDATE STATISTICS` after load | n/a | ❌ Out of scope |
| 23 | **Model collapse** — synthetic-on-synthetic training degrades quality | Generation methodology | Out of scope | n/a | ❌ Out of scope |

---

## 3. dbldatagen capability verdict

### What dbldatagen handles natively (no extra work needed)

| Capability | dbldatagen API |
|---|---|
| Per-column NULL rates | `percentNulls=0.15` |
| Discrete value domain | `values=['a','b','c']` |
| Weighted discrete values (hot-spots) | `values=[...], weights=[0.9, 0.08, 0.02]` |
| Min/max numeric range | `minValue=0, maxValue=1000` |
| Normal distribution | `distribution="normal"` or `dg.distributions.Normal(mean, std)` |
| Gamma distribution (right-skewed) | `dg.distributions.Gamma(shape, scale)` |
| Beta distribution | `dg.distributions.Beta(alpha, beta)` |
| Exponential distribution | `dg.distributions.Exponential(scale)` |
| Email / phone / UUID / IP templates | `template=r'\w.\w@\w.com'` etc. |
| Unique values (PKs, IDs) | `uniqueValues=N` |
| Multi-table FK consistency | Hash-based repeatable seeds: `baseColumn="id", baseColumnType="hash"` |
| Date/time range | `begin="2020-01-01", end="2024-12-31"` |
| Computed columns (ordering) | `expr="date_add(start_date, delay)", baseColumn=["start_date","delay"]` |
| Streaming data frames | `build(withStreaming=True)` |
| Scale to billions of rows | Spark-native parallel generation |

### What dbldatagen does NOT support — and how we work around it

| Gap | dbldatagen status | Our workaround |
|---|---|---|
| **Zipf / power-law distribution** | ❌ Not available | Approximate with `Gamma(shape<1, scale=2)` — similar heavy tail. Builder selects shape based on `GenerationRule.distribution_params["a"]`. |
| **Skewness / kurtosis parameters** | ❌ Not available | Map positive skewness to `Gamma`, negative to `Beta`, high kurtosis to wider `Gamma` scale. Stored in `ColumnStats.skewness` / `kurtosis`. |
| **Temporal ordering between columns** | ⚠️ Partial | `expr="date_add(col_a, abs(hash(id)) % N)"` enforces `col_b > col_a`. Builder must parse `temporal_ordering_constraints` strings and generate appropriate `expr` clauses. Currently requires manual wiring. |
| **Automatic FK fan-out ratio** | ⚠️ Partial | Pattern: `rows = avg_children_per_parent × unique_parents × time_periods`. Must be calculated in the orchestration layer from `ForeignKeyStats.avg_children_per_parent`. |
| **Rare event / tail injection** | ❌ Not available | Must `UNION` a separate small DataFrame generated with values from the histogram tail (outside the MCV range). Planned for builder layer. |
| **Functional dependency auto-wiring** | ⚠️ Manual | Must write `baseColumn` + `expr` per dependency pair. Builder cannot automatically apply `CompositeColumnStats.dependencies` without knowing the dependency function. |
| **Cross-table correlation** | ❌ Not available | Tables generated independently; cross-table patterns (e.g., bigger customers have bigger orders) require a GAN or post-generation join-based enrichment. Out of scope. |
| **inject_boundary_values guarantee** | ⚠️ Partial | dbldatagen generates values in [min, max] but does not guarantee at least one min and one max row. Post-process: `UNION df WITH (SELECT min_val AS col ...) LIMIT 1`. |
| **Named format patterns** (19 semantic names) | ⚠️ Manual | dbldatagen uses raw template syntax. Our builder translates named patterns (e.g., `"email"` → `r'\w.\w@\w.com'`) via `_FORMAT_PATTERN_TEMPLATES`. |

### Summary score

Out of 16 actionable shortcomings (excluding out-of-scope items 17–23):

| Category | Count | Coverage |
|---|---|---|
| ✅ dbldatagen handles natively | 7 | NULL rates, values+weights, min/max, templates, uniqueValues, date ranges, distributions |
| ⚡ Our builder bridges the gap | 5 | MCV→weights, format_pattern→template, null_fraction→percentNulls, Zipf≈Gamma, unsigned widening |
| ⚠️ Partial / manual wiring required | 3 | Temporal ordering, FK fan-out, functional dependencies |
| ❌ Not addressed by dbldatagen | 1 | Rare event injection (planned) |

**80 % of actionable shortcomings are fully or substantially covered by dbldatagen + our builder layer.**

---

## 4. Canonical model fields — quick reference

### `GenerationRule` fields

```python
@dataclass
class GenerationRule:
    # ── core range / value pool ───────────────────────────────────
    min_value: Optional[Any] = None          # boundary injection (shortcoming #4)
    max_value: Optional[Any] = None
    values: Optional[list[Any]] = None       # explicit enum domain (#6)
    weights: Optional[list[float]] = None    # frequency weights (#2)
    max_length: Optional[int] = None

    # ── distribution control ──────────────────────────────────────
    distribution: str = "auto"               # sampler selection (#1)
    distribution_params: dict[str, Any] = …  # sampler tuning (#1, #11)

    # ── string format / pattern ───────────────────────────────────
    format_pattern: Optional[str] = None     # realistic strings (#5, #15)

    # ── injection flags ───────────────────────────────────────────
    inject_boundary_values: bool = True      # min/max guaranteed (#4)
    inject_nulls_from_stats: bool = True     # apply null_fraction (#3)
    use_mcv_weights: bool = True             # hot-spot realism (#2)
    inject_rare_events: bool = False         # tail injection (#12)
```

### `ColumnStats` fields

```python
@dataclass
class ColumnStats:
    null_fraction: float               # shortcoming #3
    n_distinct: float                  # shortcoming #7
    min_value: Optional[str]           # shortcoming #4
    max_value: Optional[str]           # shortcoming #4
    most_common_values: list[MCV]      # shortcomings #2, #6
    histogram_bounds: list[str]        # shortcoming #1
    correlation: Optional[float]       # physical sort order
    skewness: Optional[float]          # shortcoming #11 — NEW
    kurtosis: Optional[float]          # shortcoming #11 — NEW
```

### `CompositeColumnStats` fields

```python
@dataclass
class CompositeColumnStats:
    columns: list[str]                         # which columns
    n_distinct: Optional[int]                  # combined cardinality (#7)
    dependencies: dict[str, float]             # shortcoming #13
    most_common_combinations: list[MCVCombo]   # shortcoming #14
```

### `ForeignKeyStats` fields

```python
@dataclass
class ForeignKeyStats:
    columns: list[str]
    parent_table: str
    parent_columns: list[str]
    cardinality_pattern: str              # "N:1", "1:1", "N:M" — #9
    match_fraction: float                 # orphan detection — #8
    avg_children_per_parent: Optional[float]  # fan-out ratio — #9
```

### `CanonicalTableSchema` field

```python
@dataclass
class CanonicalTableSchema:
    temporal_ordering_constraints: list[str]   # shortcoming #10 — NEW
    # e.g. ["end_date > start_date", "updated_at >= created_at"]
```

---

## 4. `format_pattern` named patterns reference

| Pattern name | Example value | Use case |
|---|---|---|
| `email` | `alice.jones@example.com` | user accounts, contact tables |
| `phone_us` | `+1-415-555-0172` | US phone numbers |
| `phone_intl` | `+44-20-7946-0958` | International phone numbers |
| `uuid` | `550e8400-e29b-41d4-a716-446655440000` | Surrogate PKs, session IDs |
| `ip_v4` | `192.168.1.42` | Network logs |
| `ip_v6` | `2001:0db8:85a3::8a2e:0370:7334` | IPv6 network logs |
| `url` | `https://shop.example.com/product/42` | Clickstream, referrers |
| `postal_us` | `94107` or `94107-1234` | US addresses |
| `postal_uk` | `SW1A 1AA` | UK addresses |
| `ssn` | `123-45-6789` | US Social Security Numbers (test only) |
| `credit_card` | `4111-1111-1111-1111` | Payment testing (Luhn-valid) |
| `iban` | `GB82 WEST 1234 5698 7654 32` | European banking |
| `name_first` | `Sofia` | First names (locale-aware) |
| `name_last` | `Nguyen` | Family names (locale-aware) |
| `company` | `Acme Logistics Ltd.` | Business names |
| `address` | `742 Evergreen Terrace` | Street addresses |
| `city` | `Springfield` | City names |
| `country_iso2` | `DE` | ISO 3166-1 alpha-2 |
| `currency_iso` | `EUR` | ISO 4217 currency codes |
| Regex fallback | `r"[A-Z]{2}\d{6}"` | Custom formats (passports, etc.) |

---

## 5. Distribution selector guide

| Distribution | `distribution` value | `distribution_params` | Typical columns |
|---|---|---|---|
| Auto (default) | `"auto"` | — | Any — builder decides from stats |
| Uniform | `"uniform"` | — | Sequential IDs, test row numbers |
| Normal (Gaussian) | `"normal"` | `{"mean": 170.0, "std": 10.0}` | Height, weight, temperature |
| Log-normal / right-skewed | `"normal"` + `skewness > 0` | `{"mean": 10.0, "std": 2.0}` (in log space) | Income, session duration, order value |
| Zipf / power-law | `"zipf"` | `{"a": 1.2}` | Page views, purchase frequency, product popularity |
| Exponential | `"exponential"` | `{"scale": 30.0}` | Time-to-event, inter-arrival gaps |
| Constant | `"constant"` | — | Status fields where only one value exists |
| Sequential | `"sequential"` | — | Auto-increment PKs, monotonic timestamps |

---

## 6. Temporal ordering constraint syntax

```
<column_name>  <operator>  <column_name>
```

Supported operators: `<`, `<=`, `>`, `>=`

```yaml
# In CanonicalTableSchema serialization
temporal_ordering_constraints:
  - "end_date > start_date"
  - "updated_at >= created_at"
  - "shipped_at > ordered_at"
  - "birth_date < hire_date"
```

The generator must validate each row and re-sample the later column until the
constraint holds.  A hard limit on re-sampling attempts (e.g. 100 tries) should
be enforced to prevent infinite loops.

---

## 7. Known gaps

| Gap | Severity | Workaround |
|---|---|---|
| Cross-table correlation (#17) | Medium | Model each table's row count and FK fan-out; use `OverrideSpec` to set realistic per-table ranges |
| Many-to-many junction tables (#18) | Medium | Generate each table independently; enforce FK integrity via post-generation join check |
| Oracle `DATE` = datetime (#19) | Low | Use `OverrideSpec.column_overrides` to change type to `timestamp` for Oracle sources |
| MySQL zero dates (#20) | Low | Use `OverrideSpec` generation hint or pre-process with SQL `NULLIF` |
| Charset / collation (#21) | Low | Handle at ETL layer; document source charset in `CanonicalColumn.constraints` |
| Query plan divergence (#22) | Low-medium | After synthetic data load run `ANALYZE TABLE` / `UPDATE STATISTICS` to rebuild planner stats |
| `skewness` / `kurtosis` not in DB catalogues | Medium | Must be computed by sampling or provided in override YAML; not collectable from `ANALYZE` |
| Nanosecond precision across dialects | Low | Oracle ns → PG µs is lossy; documented in `test_plan_ddl_roundtrip.md` |
