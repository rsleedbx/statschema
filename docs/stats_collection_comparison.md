# Stats collection and comparison across databases

## What statschema collects

The canonical model (`ColumnStats`) captures the following per-column fields, drawn from each engine's native catalog:

| Field | Description |
|---|---|
| `null_fraction` | Fraction of NULLs in the column |
| `n_distinct` | Estimated distinct value count (positive) or −fraction-of-rows (negative, PostgreSQL convention) |
| `avg_width_bytes` | Average byte width of a stored value |
| `min_value` | Minimum observed value (as string) |
| `max_value` | Maximum observed value (as string) |
| `most_common_values` | List of `(value, frequency)` pairs for high-frequency values |
| `histogram_bounds` | Equi-depth bucket boundaries for the value distribution |
| `correlation` | Physical sort correlation of the column to its heap order (PostgreSQL only) |
| `skewness` / `kurtosis` | Distribution shape moments (not in any native catalog; populated by the sampling enrichment path) |

Table-level fields: `row_count`, `data_size_bytes`, `avg_row_bytes`, `last_analyzed`.

Multi-column: `CompositeColumnStats` captures cross-column `n_distinct`, functional `dependencies`, and most-common value combinations (PostgreSQL `pg_stats_ext`; SQL Server density vector).

---

## Source catalog mapping per engine

| Canonical field | PostgreSQL 13+ | MySQL 8+ | SQL Server 2016+ | Oracle 19c | DB2 LUW 11.5 | CockroachDB |
|---|---|---|---|---|---|---|
| `row_count` | `pg_class.reltuples` | `INNODB_TABLESTATS.NUM_ROWS` | `dm_db_partition_stats.row_count` | `ALL_TABLES.NUM_ROWS` | `SYSCAT.TABLES.CARD` | `pg_class.reltuples` |
| `null_fraction` | `pg_stats.null_frac` | `COLUMN_STATISTICS` histogram `null-values` | `DBCC SHOW_STATISTICS` density_vector | `ALL_TAB_COL_STATISTICS.NUM_NULLS / NUM_ROWS` | `SYSCAT.COLUMNS.NUMNULLS / CARD` | `pg_stats.null_frac` |
| `n_distinct` | `pg_stats.n_distinct` | `COLUMN_STATISTICS` histogram (derived) | `RANGE_ROWS + EQ_ROWS` (derived) | `ALL_TAB_COL_STATISTICS.NUM_DISTINCT` | `SYSCAT.COLUMNS.COLCARD` | `pg_stats.n_distinct` |
| `avg_width_bytes` | `pg_stats.avg_width` | `AVG_COL_LEN` (approx) | `sys.columns.max_length` (approx) | `ALL_TAB_COL_STATISTICS.AVG_COL_LEN` | `SYSCAT.COLUMNS.AVGCOLLEN` | `pg_stats.avg_width` |
| `min_value` | `pg_stats.histogram_bounds[0]` | `COLUMN_STATISTICS` buckets[0] | `DBCC SHOW_STATISTICS` `RANGE_LOW` | `ALL_TAB_COL_STATISTICS.LOW_VALUE` (RAW decoded) | `SYSCAT.COLDIST` (V type rows) | `pg_stats.histogram_bounds[0]` |
| `max_value` | `pg_stats.histogram_bounds[-1]` | `COLUMN_STATISTICS` buckets[-1] | `DBCC SHOW_STATISTICS` `RANGE_HIGH` | `ALL_TAB_COL_STATISTICS.HIGH_VALUE` (RAW decoded) | `SYSCAT.COLDIST` (V type rows) | `pg_stats.histogram_bounds[-1]` |
| `most_common_values` | `pg_stats.most_common_vals/freqs` | `COLUMN_STATISTICS` histogram (derived) | `DBCC SHOW_STATISTICS` density_vector | `ALL_HISTOGRAMS` (FREQUENCY / TOP-FREQUENCY types) | `SYSCAT.COLDIST` (F type rows) | `pg_stats.most_common_vals/freqs` |
| `histogram_bounds` | `pg_stats.histogram_bounds` | `COLUMN_STATISTICS` histogram buckets | `dm_db_stats_histogram.range_high_key` | `ALL_HISTOGRAMS.ENDPOINT_VALUE` | `SYSCAT.COLDIST` (Q type rows) | `pg_stats.histogram_bounds` |
| `correlation` | `pg_stats.correlation` | — | — | — | — | `pg_stats.correlation` |
| `CompositeColumnStats` | `pg_stats_ext` (`CREATE STATISTICS`) | — | density_vector (cross-col density) | — | — | limited |

**`—`** means the engine does not expose this stat in its catalog.

---

## Native ANALYZE command per engine

What each engine's `_analyze_*` function issues, and what it covers:

| Engine | Default command | Coverage |
|---|---|---|
| **PostgreSQL** | `ANALYZE "schema"."table"` | All columns; samples based on `default_statistics_target` (default 100 buckets) |
| **CockroachDB** | `ANALYZE "schema"."table"` | All columns; automatic histogram creation |
| **MySQL** | `ANALYZE TABLE \`schema\`.\`table\`` | Refreshes InnoDB index cardinality + previously-created column histograms only |
| **SQL Server** | `UPDATE STATISTICS [dbo].[table]` | All statistics objects on the table (per-column + per-index); samples by default |
| **Oracle** | `DBMS_STATS.GATHER_TABLE_STATS(…)` | All columns with `method_opt => 'FOR ALL COLUMNS SIZE AUTO'` by default |
| **DB2** | `CALL SYSPROC.ADMIN_CMD('RUNSTATS ON TABLE schema.TABLE WITH DISTRIBUTION AND DETAILED INDEXES ALL')` | All columns, all indexes; most expensive variant.  **Must use `ADMIN_CMD`** — bare `cursor.execute('RUNSTATS …')` returns SQL0104N and is silently ignored by `ibm_db_dbi`. Add `TABLESAMPLE SYSTEM(n)` at the end for faster sampling (25% = ~2× faster, 10% = ~6× faster). |

### statschema default vs. `--full-stats-db2-ora`

For non-PostgreSQL engines in Phase D (target schema), statschema uses the workload predicate column map built during Phase C to restrict expensive stats to the columns that matter for the benchmark queries.

| Engine | Default (predicate-targeted) | `--full-stats-db2-ora` |
|---|---|---|
| **DB2** | `RUNSTATS … ON COLUMNS (pred_cols) WITH DISTRIBUTION AND DETAILED INDEXES ALL` for tables with predicate columns; `RUNSTATS … AND DETAILED INDEXES ALL` only for tables with no predicate columns.  `DETAILED INDEXES ALL` is always included so `SYSCAT.INDEXES` is current and the Python collector can use fast index-scan MIN/MAX. | `RUNSTATS … WITH DISTRIBUTION AND DETAILED INDEXES ALL` on every table |
| **Oracle** | `GATHER_TABLE_STATS(… method_opt='FOR ALL COLUMNS SIZE 1, FOR COLUMNS pred_col SIZE AUTO')` | `GATHER_TABLE_STATS(… method_opt='FOR ALL COLUMNS SIZE AUTO')` |
| **SQL Server** | `UPDATE STATISTICS [dbo].[table]` (no change; column targeting not available at this level) | Same — no effect |
| **MySQL** | `ANALYZE TABLE` (no change) | Same — no effect |
| **PostgreSQL / CockroachDB** | `ANALYZE "schema"."table"` (no change; stats injected directly via `pg_restore_attribute_stats`) | Same — no effect |

`--full-stats-db2-ora` is intended for major-release or audit runs where you want to verify that the full DB2 / Oracle catalog matches what statschema collected, not just the predicate-column subset.  For routine CI runs the default targeted mode is sufficient: Phase C.5 uses the same `collection_config` as Phase C (including `pred_cols` short-circuiting), so it only needs stats for predicate columns to produce correct identity scores.

Phase A (source schema) always uses full stats regardless of this flag, because Phase C reads from it as the ground truth.

---

## What the identity test compares

The test compares source vs. target in two separate dimensions.

### Phase C.5 — stats fidelity (synthetic data quality)

After loading the synthetic target rows and running ANALYZE, the test checks how well the stats-driven generator reproduced the source distributions:

| Metric | Definition | Pass threshold |
|---|---|---|
| `ndistinct_within_2x` | Fraction of `(table, column)` pairs where target `n_distinct` is within 2× of source | reported, not gated |
| `range_covered` | Fraction of numeric/date columns where target `[min, max]` covers ≥ 95% and ≤ 105% of the source range | reported, not gated |
| `null_match` | Fraction of columns where `null_fraction` differs by ≤ 0.02 | reported, not gated |

String columns are excluded from `range_covered` (randomly generated extreme values are not meaningful).

### Phase F — plan fidelity (query optimizer output)

The primary pass/fail judgment:

| Metric | Definition | Default threshold |
|---|---|---|
| `node_jaccard` | Jaccard similarity of plan node-type multisets (source vs. target) | ≥ 0.70 |
| `within_2x` | Fraction of plan nodes where the estimated row count ratio is ≤ 2× | ≥ 0.50 |
| `within_10x` | Same ratio at 10× threshold | reported only |

### Stats not compared

The following fields are collected and stored in the canonical YAML but are not evaluated in the identity test scoring (see [why these fields are not scored](stats_not_scored_rationale.md)):

- `correlation` — collected from PostgreSQL only; not injected into non-PG targets
- `avg_width_bytes` — used to guide synthetic data generation; not scored
- `data_size_bytes` / `avg_row_bytes` — informational; not scored
- `skewness` / `kurtosis` — used by the generator; not directly scored
- `CompositeColumnStats` — used for Phase A.5/D.5 extended stats on PostgreSQL; not scored on non-PG engines

---

## All-columns stats comparison (major-release audit)

To validate that statschema's collected stats match the full native catalog (not just predicate columns) across all columns and all engines, run:

```bash
# DB2 and Oracle: full RUNSTATS / GATHER_TABLE_STATS on every column
python benchmarks/run_matrix.py identity \
    --engines db2,oracle \
    --full-stats-db2-ora

# All engines (--full-stats-db2-ora has no effect on the others)
python benchmarks/run_matrix.py identity --full-stats-db2-ora
```

This expands the `ndistinct_within_2x`, `range_covered`, and `null_match` metrics to cover every column, giving a complete picture of collection accuracy at the cost of 5–10× longer ANALYZE time on DB2 and Oracle.
