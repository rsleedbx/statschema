# Why certain collected stats are not scored

A stat is scored in the identity test only when all three conditions hold:

1. It is collected from the source catalog.
2. It is injected into the target catalog.
3. It is re-readable from the target catalog in a comparable form.

The fields listed in the "Stats not compared" section of [stats_collection_comparison.md](stats_collection_comparison.md) each fail at least one condition.

---

## `correlation`

`correlation` is a physical heap-order artifact specific to PostgreSQL's MVCC storage. No equivalent field exists in the MySQL, Oracle, DB2, or SQL Server catalogs. Scoring it would only be meaningful for PG→PG runs; it cannot be read back on any other target engine.

---

## `avg_width_bytes`

`avg_width_bytes` is an input to the synthetic data generator — it shapes string and byte value sizing. It is not a distribution property the generator is trying to reproduce. Generator output quality is evaluated indirectly through `null_match`, `range_covered`, and `ndistinct_within_2x`. Scoring `avg_width_bytes` would check value byte length, not distribution fidelity.

---

## `data_size_bytes` / `avg_row_bytes`

These are storage-layer outputs that vary with each engine's internal representation: compression, page alignment, TOAST/row chaining, and column padding. A row occupying 50 bytes in PostgreSQL can legitimately occupy 120 bytes in Oracle with identical logical data. Cross-engine differences in these values reflect storage internals, not data correctness.

---

## `skewness` / `kurtosis`

`skewness` and `kurtosis` are populated by the sampling enrichment path and are not exposed by any engine's native `ANALYZE` catalog. There is no target-side catalog field to compare them against after `ANALYZE` runs. Whether the generator applied them correctly is captured indirectly by Phase F plan fidelity: wrong skew produces wrong cardinality estimates, which Phase F detects.

---

## `CompositeColumnStats`

Cross-column statistics require explicit `CREATE STATISTICS` DDL naming specific column groups (PostgreSQL `pg_stats_ext`). Other engines either do not expose cross-column stats (Oracle, DB2, MySQL) or expose them in an incompatible form (SQL Server density vectors). Scoring cross-column stats cross-engine has no portable target-side representation to compare against.
