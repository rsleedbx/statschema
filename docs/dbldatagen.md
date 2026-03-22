# dbldatagen v0 and v1 in statschema

**Last updated:** 2026-03-19  
**Relates to:** `src/statschema/dbldatagen_builder.py` (v0), `src/statschema/v1_bridge.py` (v1)  
**Intended readers:** statschema contributors, dbldatagen developers

---

## Background

[dbldatagen](https://github.com/databrickslabs/dbldatagen) is the Databricks Labs synthetic data generator
for Apache Spark. statschema uses it as the generation engine downstream of the canonical schema model.

Two distinct APIs exist within the library:

- **v0** — the current stable release on PyPI (`pip install dbldatagen`). Imperative builder API.
- **v1** — a redesigned declarative API under active development inside the same repo, available as `dbldatagen.v1`.

statschema maintains separate bridges to each:

| | v0 | v1 |
|---|---|---|
| statschema module | `src/statschema/dbldatagen_builder.py` | `src/statschema/v1_bridge.py` |
| Entry point | `build_dataframe_from_canonical()` | `to_v1_plan()` |
| Install | `pip install dbldatagen` | `dbldatagen.v1` sub-package |

---

## API shape

### v0 — imperative `.withColumn` chain

```python
import dbldatagen as dg
from pyspark.sql.types import LongType, StringType, DecimalType

gen = (
    dg.DataGenerator(spark, name="orders", rows=100_000, partitions=8)
    .withIdOutput()
    .withColumn("order_id", LongType(), minValue=1, uniqueValues=100_000)
    .withColumn("customer_id", LongType(), minValue=1, maxValue=50_000,
                distribution=dg.distributions.Gamma(shape=0.75, scale=2.0))
    .withColumn("email", StringType(), template=r"\w.\w@\w.com")
    .withColumn("total", DecimalType(12, 2), minValue=1.0, maxValue=9999.99,
                percentNulls=0.0)
)
df = gen.build()
```

The plan is not serializable. Each `.withColumn` call returns a new `DataGenerator` instance.

### v1 — declarative Pydantic `DataGenPlan`

```python
from dbldatagen.v1.schema import (
    DataGenPlan, TableSpec, ColumnSpec, PrimaryKey,
    SequenceColumn, FakerColumn, RangeColumn, ForeignKeyRef,
    DataType, Zipf,
)

plan = DataGenPlan(
    tables=[
        TableSpec(
            name="orders",
            rows=100_000,
            primary_key=PrimaryKey(columns=["order_id"]),
            columns=[
                ColumnSpec(name="order_id",  dtype=DataType.LONG,   gen=SequenceColumn()),
                ColumnSpec(name="customer_id", dtype=DataType.LONG,
                           foreign_key=ForeignKeyRef(ref="customers.id",
                                                     distribution=Zipf(exponent=1.2))),
                ColumnSpec(name="email",  dtype=DataType.STRING, gen=FakerColumn(provider="email")),
                ColumnSpec(name="total",  dtype=DataType.DECIMAL,
                           gen=RangeColumn(min=1.0, max=9999.99)),
            ],
        ),
    ]
)

# Serialise to YAML, pass to another system, load back:
import yaml
raw = yaml.dump(plan.model_dump(mode="json"), allow_unicode=True)
plan2 = DataGenPlan.model_validate(yaml.safe_load(raw))

dbldatagen.v1.generate(spark, plan2)
```

---

## Feature comparison

| Feature | v0 | v1 |
|---|---|---|
| **String generation** | Regex template strings (`r"\w.\w@\w.com"`) | Real Faker providers (`FakerColumn(provider="email")`) |
| **Zipf distribution** | ❌ Not available — approximated with Gamma | ✅ Native `Zipf(exponent=...)` |
| **FK referential integrity** | ❌ None | ✅ `ForeignKeyRef(ref="table.col", distribution=...)` |
| **Multi-table topological order** | ❌ Tables generated independently | ✅ Engine resolves parent-before-child order |
| **Serializable plan** | ❌ In-process object only | ✅ Pydantic → JSON/YAML → Spark |
| **`TableStats` support** | ✅ null_fraction, MCVs, min/max | ✅ (wired in v1_bridge; `to_v1_plan(stats=...)`) |
| **MCV weights** | ✅ `values + weights` | ✅ `ValuesColumn + WeightedValues` |
| **Numeric min/max from stats** | ✅ | ✅ |
| **Date range from stats** | ✅ | ✅ |
| **FK distribution control** | ❌ | ✅ `CanonicalForeignKey.fk_distribution` → `ForeignKeyRef.distribution` |
| **Beta / Gamma distributions** | ✅ (`dg.distributions.Beta/Gamma`) | ❌ Not in v1 schema types (assumption — see §Assumptions) |
| **`inject_boundary_values`** | ⚠️ Post-generation union (see `postgen.py`) | ⚠️ Same |
| **`inject_rare_events`** | ❌ Not implemented | ❌ Not implemented |
| **Temporal ordering constraints** | ❌ Post-generation only | ❌ Post-generation only |

---

## How statschema maps canonical fields

### v0 (`dbldatagen_builder.py`)

| Canonical field | v0 kwarg |
|---|---|
| `GenerationRule.distribution = "normal"` | `distribution=dg.distributions.Normal(mean, std)` |
| `GenerationRule.distribution = "zipf"` | `distribution=dg.distributions.Gamma(shape=1.5/a, scale=2.0)` — approximation |
| `GenerationRule.distribution = "exponential"` | `distribution=dg.distributions.Exponential(scale)` |
| `GenerationRule.format_pattern = "email"` | `template=r"\w.\w@\w.com"` |
| `ColumnStats.null_fraction` | `percentNulls=null_fraction` |
| `ColumnStats.most_common_values` (dominant) | `values=[...], weights=[...]` |
| `ColumnStats.min_value` / `max_value` | `minValue=..., maxValue=...` |
| `col.primary_key + auto_increment` | `uniqueValues=rows` |

### v1 (`v1_bridge.py`)

| Canonical field | v1 object |
|---|---|
| `col.primary_key + auto_increment` | `SequenceColumn()` |
| `col.primary_key, type=string` | `UUIDColumn()` |
| `GenerationRule.distribution = "normal"` | `RangeColumn(distribution=Normal(mean, stddev))` |
| `GenerationRule.distribution = "zipf"` | `RangeColumn(distribution=Zipf(exponent))` — native, no approximation |
| `GenerationRule.format_pattern = "email"` | `FakerColumn(provider="email")` |
| `GenerationRule.format_pattern = "uuid"` | `UUIDColumn()` |
| `GenerationRule.format_pattern` (other named) | `FakerColumn(provider=_FORMAT_TO_FAKER[fp])` |
| `GenerationRule.format_pattern` (raw regex) | `PatternColumn(template=fp)` |
| `GenerationRule.values + weights` | `ValuesColumn(values, distribution=WeightedValues(weights))` |
| `col.type = "boolean"` | `ValuesColumn(values=[True, False])` |
| `col.type in (date, timestamp)` | `TimestampColumn(begin=..., end=...)` |
| `ColumnStats.null_fraction` | `ColumnSpec(null_fraction=...)` |
| `ColumnStats.most_common_values` (dominant) | `ValuesColumn + WeightedValues` |
| `ColumnStats.min_value` / `max_value` (numeric) | `RangeColumn(min=..., max=...)` |
| `ColumnStats.min_value` / `max_value` (date) | `TimestampColumn(begin=..., end=...)` |
| `CanonicalForeignKey` | `ForeignKeyRef(ref="table.col", distribution=_fk_v1_distribution(fk))` |

---

## Assumptions we want dbldatagen developers to validate

These are decisions made in statschema based on available documentation and code reading.
We are not certain they are correct.

### 1. Zipf → Gamma approximation (v0 only)

`dbldatagen_builder.py` has no native Zipf. We approximate it:

```python
# GenerationRule.distribution = "zipf", distribution_params = {"a": 1.5}
shape = max(0.1, 1.5 / a)          # a=1.5 → shape=1.0; a=2.0 → shape=0.75
distribution = dg.distributions.Gamma(shape=shape, scale=2.0)
```

**Open question:** Is `Gamma(shape=1.5/a, scale=2.0)` a reasonable proxy for `Zipf(a)` in practice?
What shape/scale combination produces the closest match for typical row counts (10k–100M rows)?

### 2. `ForeignKeyRef.distribution` semantics (v1)

`v1_bridge.py` passes `distribution=Zipf(exponent=1.2)` to `ForeignKeyRef`. Our understanding is
that this distribution controls how parent rows are selected when generating FK column values —
i.e., a Zipf distribution produces a power-law fan-out where a few parents accumulate most children.

**Open question:** Is that correct? Does the distribution parameter control parent-row selection
probability, or does it control something else (e.g., the number of children per parent directly)?
What is the effect of `Uniform()` vs `Zipf(exponent=1.2)` on the resulting fan-out histogram?

### 3. `WeightedValues.weights` format (v1)

`v1_bridge.py` builds `WeightedValues` from `ColumnStats.most_common_values`:

```python
# MostCommonValue.frequency is a fraction of non-null rows [0.0, 1.0]
mcv_weights = {str(m.value): float(m.frequency) for m in col_stats.most_common_values}
return WeightedValues(weights=mcv_weights)
```

**Open question:** Does `WeightedValues` treat values as probabilities (must sum to ≤ 1.0) or as
relative counts (normalised internally)? If the MCVs cover only 60% of rows, should we pass the
raw frequencies or normalise them to sum to 1.0?

### 4. `PatternColumn.template` syntax (v1)

`v1_bridge.py` passes raw patterns (e.g., from `en_US.yaml`) directly to `PatternColumn.template`.
The v0 mini-language uses `a` = lowercase alpha, `A` = uppercase, `d` = digit, `\w` = word,
`\n` = random 0–255 number, `x` = hex digit.

**Open question:** Does v1 `PatternColumn` use the same template syntax as v0 `template=...`?
If the syntax changed, patterns from `src/statschema/patterns/en_US.yaml` (e.g., `r"\n.\n.\n.\n"` for IPv4)
will produce incorrect output.

### 5. `name_mapper` column name coverage (v1)

`v1_bridge.py` falls back to `dbldatagen.v1.connectors.sql.name_mapper.map_column_name(col_name)` for
string columns that have no `format_pattern` and no semantic match from `infer_format_pattern()`.

**Open question:** What column names does `map_column_name()` recognise? Is there a published list,
or is it driven by a model? Are there column names where it produces a semantically wrong generator
(e.g., a column named `score` mapped to a name Faker provider)?

### 6. `dbldatagen.v1` availability

`v1_bridge.py` imports from `dbldatagen.v1`. All tests that require it use
`pytest.importorskip("dbldatagen.v1")`.

**Open question:** Is `dbldatagen.v1` available in the same `pip install dbldatagen` package,
or does it require a separate install or a pre-release channel? Is it safe to depend on for
production Databricks cluster usage today?

### 7. `ColumnSpec.null_fraction` + `nullable` interaction (v1)

`v1_bridge.py` sets both `nullable=True` and `null_fraction=float` on `ColumnSpec`:

```python
ColumnSpec(name="region", dtype=DataType.STRING,
           gen=FakerColumn(provider="state"),
           nullable=True,
           null_fraction=0.05)
```

**Open question:** Does `nullable=True` alone generate any NULLs, or does it only permit NULLs
when `null_fraction > 0`? If `nullable=False` and `null_fraction=0.05` are set simultaneously,
which wins?

### 8. `Beta` and `Gamma` in v1

`dbldatagen_builder.py` (v0) uses `dg.distributions.Beta` and `dg.distributions.Gamma` for columns
with high skewness or kurtosis. `v1_bridge.py` imports `Zipf, Normal, LogNormal, Exponential, Uniform,
WeightedValues` — no Beta or Gamma.

**Open question:** Are `Beta` and `Gamma` available in `dbldatagen.v1.schema`? If so, what is
the import path? If not, is there a recommended v1 substitute?

---

## Generation pipelines side by side

```
                      ┌─────────────────────────────────────┐
DDL / schema YAML ──► │   CanonicalTableSchema + TableStats  │
                      └────────────┬──────────┬──────────────┘
                                   │          │
                              v0 path    v1 path
                                   │          │
                    ┌──────────────▼┐  ┌──────▼──────────────────┐
                    │ to_dbldatagen │  │       to_v1_plan()       │
                    │   _specs()    │  │  (src/v1_bridge.py)      │
                    └──────────────┬┘  └──────┬──────────────────┘
                                   │          │
                    ┌──────────────▼┐  ┌──────▼──────────────────┐
                    │ dg.DataGener- │  │  DataGenPlan (Pydantic)  │
                    │ ator.withCo-  │  │  YAML-serializable       │
                    │ lumn(...)     │  │  FK-aware, topo-ordered  │
                    └──────────────┬┘  └──────┬──────────────────┘
                                   │          │
                    ┌──────────────▼─┐ ┌──────▼──────────────────┐
                    │  gen.build()   │ │ dbldatagen.v1.generate() │
                    └──────────────┬─┘ └──────┬──────────────────┘
                                   │          │
                    ┌──────────────▼──────────▼──────────────────┐
                    │                Spark DataFrame              │
                    └─────────────────────────────────────────────┘
                                         │
                              (optional post-generation)
                    ┌────────────────────▼──────────────────────┐
                    │  apply_boundary_rows()  (src/postgen.py)   │
                    │  inject_rare_events()   (not yet)          │
                    │  apply_temporal_ordering()  (not yet)      │
                    └───────────────────────────────────────────┘
```

---

## When to use each path

Use the v0 path when:
- `dbldatagen.v1` is not yet available on your Databricks runtime.
- The schema has no FK constraints (v0 has no `ForeignKeyRef`).
- You need Beta or Gamma distributions.

Use the v1 path when:
- The schema has FK constraints and referential integrity matters.
- You need to serialise the generation plan to YAML and replay it.
- You need accurate Zipf (not the Gamma approximation).
- You need real Faker providers instead of regex templates.

---

## Related

- [`src/statschema/dbldatagen_builder.py`](../src/statschema/dbldatagen_builder.py) — v0 bridge
- [`src/statschema/v1_bridge.py`](../src/statschema/v1_bridge.py) — v1 bridge
- [`src/statschema/postgen.py`](../src/statschema/postgen.py) — post-generation utilities
- [`src/statschema/semantic_hints.py`](../src/statschema/semantic_hints.py) — column-name inference
- [`docs/canonical_yaml/06-generation-rules.md`](canonical_yaml/06-generation-rules.md) — `generation:` YAML reference
- [`docs/synthetic_data_shortcomings.md`](synthetic_data_shortcomings.md) — full shortcoming × solution matrix
