# statschema Roadmap

Items are ordered from easiest to hardest to implement. Each item identifies the capability gap and what "done" looks like.

---

## YAML-Driven CLI

### 0. DBA-facing command-line interface ✅
- **Implemented**: `python -m statschema` with three sub-commands — `ddl`, `generate`, `load`.
  A DBA writes one YAML file and runs commands; no Python code required.
  - `ddl schema.yaml --dialect <dialect>` — emits CREATE TABLE SQL to stdout in FK dependency order.
  - `generate schema.yaml --sf <n> [--out-dir DIR] [--format csv|jsonl]` — streams synthetic rows without a database connection.
  - `load schema.yaml --dialect <dialect> --dsn <dsn> [--sf <n>]` — creates tables and loads data using the fastest available strategy per dialect.
  - Entry point: `src/statschema/__main__.py` and `src/statschema/cli.py`.
  - DBA reference: [`docs/dba-yaml-guide.md`](dba-yaml-guide.md).

---

## Data Loading

### 1. Multi-row INSERT loader ✅
- **Implemented**: `load_dataframe(df, conn, table, dialect, *, strategy, config)` in `src/statschema/data_loader.py`. Supports singleton, multi-row, and bulk-copy strategies. Oracle uses `INSERT ALL … SELECT 1 FROM DUAL`. Batch size is controlled by `BatchConfig(max_rows, max_params)` with per-dialect defaults.

### 2. Dialect-native bulk copy loader ✅
- **Implemented**: `bulk_load_postgres`, `bulk_load_mysql`, `bulk_load_sqlserver`, `bulk_load_db2` in `src/statschema/data_loader.py`. Each uses the database's fastest native path:
  - PostgreSQL / CockroachDB / Neon: `COPY … FROM STDIN WITH CSV` via `cursor.copy_expert` (no temp file)
  - MySQL / MariaDB: `LOAD DATA LOCAL INFILE` from a temp CSV
  - SQL Server: `BULK INSERT` from a staging CSV
  - IBM Db2: `LOAD FROM … OF DEL FORMAT` via `SYSPROC.ADMIN_CMD`
  - Databricks / Oracle / SQLite: falls back to `MULTI_ROW`

### 3. Batch size auto-discovery ✅
- **Implemented**: `discover_max_batch_size(conn, table, col_names, dialect, *, start_max)` in `src/statschema/data_loader.py`. Binary-searches the actual server limit using `SAVEPOINT` / `ROLLBACK TO SAVEPOINT` so no rows are committed. Enabled via `BatchConfig(auto_discover=True)`.

---

## Semantic Hints

### 1. Expand locale pattern files
- **Gap**: Only `en_US` and `de_DE` locale pattern files exist.
- **Done**: Add `fr_FR`, `ja_JP`, `zh_CN`, `pt_BR`, `es_ES` pattern files. Each file covers the same column-name patterns as `en_US.yaml` with locale-specific value generators and Faker providers.

### 2. Widen built-in pattern coverage
- **Gap**: Current patterns cover common PII fields (SSN, email, phone, address, name). Business-domain columns (product codes, order statuses, currency codes, IBAN, VIN, NPI, CUSIP) are not covered.
- **Done**: Add patterns for financial, healthcare, logistics, and e-commerce column naming conventions. Each pattern maps to an appropriate Faker provider or `format_pattern`.

### 3. LLM-based semantic inference
- **Gap**: Column names that do not match any regex pattern fall back to random data. An LLM can infer semantic meaning from column name, comment, and surrounding table context when no pattern matches.
- **Done**: Implement an optional `infer_format_pattern_llm(col, table_context)` path. Wire it as the lowest-priority fallback in `infer_format_pattern`. Support Databricks Model Serving (LogSentinel) and OpenAI-compatible endpoints. Controlled by a flag so it is opt-in.

---

## DDL Transpiler

### 4. Round-trip views and computed columns
- **Gap**: `parse_ddl` and `emit_ddl` handle `CREATE TABLE` only. `CREATE VIEW` and generated/computed columns are silently dropped.
- **Done**: Parse `CREATE VIEW` into a `CanonicalView` model. Emit dialect-correct view DDL. Preserve generated column expressions through the canonical form where the target dialect supports them.

### 5. Stored procedure and function stubs
- **Gap**: Stored procedures and functions are not parsed or emitted. Cross-dialect migrations that include procedural code produce incomplete DDL.
- **Done**: Parse procedure/function signatures into a `CanonicalRoutine` stub (name, parameters, return type, body as opaque text). Emit the signature in the target dialect. Body transpilation is out of scope — emit a placeholder comment.

---

## Stats-Driven Data Generation

statschema's native generator (`build_dataframe_from_canonical`) is intentionally lightweight: it produces column-by-column values from marginal distributions (MCVs + histogram bounds) without modeling cross-column correlations. This is sufficient for optimizer bootstrap and migration validation, which are statschema's primary use cases.

For higher-fidelity synthetic data — preserving multi-variate correlations, FK fan-out distributions, and privacy guarantees — the right path is an SDV integration (item 6 below) rather than reimplementing those algorithms inside statschema.

### 6. SDV adapter for high-fidelity generation
- **Gap**: statschema generates each column independently from marginal distributions. SDV (Synthetic Data Vault) can learn Gaussian copula or GAN-based joint distributions that preserve column correlations and FK relationships across tables — but requires the actual data rows to train on, which are available at the source database.
- **Done**: Implement `statschema.sdv_adapter` that converts `CanonicalTableSchema` → `sdv.metadata.Metadata` and `DatabaseStats` → SDV column distribution hints. When source data is available and can be moved (small tables, non-sensitive data), a DBA can use SDV's `SingleTableSynthesizer` or `HMASynthesizer` as the generation backend while keeping statschema's DDL and stats injection pipeline for the optimizer bootstrap step. For large or sensitive datasets where moving production data is impractical or prohibited, statschema's native generator remains the only viable path.

### 6a. External statistics adapter (Tonic / Gretel / Delphix / dbldatagen)

- **Gap**: statschema collects statistics from source DB catalogs. Oracle and DB2 catalog
  collection is sparse by default — many columns have no histogram buckets after `RUNSTATS` or
  `ANALYZE` — which limits `ndistinct_w2x` and reduces row-estimate accuracy on complex schemas
  (TPC-E, TPC-DI). Privacy and synthesis platforms (Tonic.ai, Gretel.ai, Delphix, and the
  [Databricks Labs Data Generator](https://databrickslabs.github.io/dbldatagen/public_docs/generating_from_existing_data.html))
  build rich statistical models from production rows as part of their data-generation or masking
  pipeline. Those models contain joint distributions, conditional frequencies, and column
  correlations that the source DB catalog does not expose. For Databricks / Lakebase environments,
  dbldatagen's `DataAnalyzer.summarizeToDF()` produces a statistical summary of any source
  dataframe that is a natural feed for statschema's intake path.
- **Done**: Implement `statschema.external_stats_adapter` with importers for Tonic column
  profiles, Gretel model metadata, Delphix data profiles, and dbldatagen `DataAnalyzer`
  summary dataframes. Each importer produces a `DatabaseStats` / `TableStats` object compatible
  with `inject_stats_postgres` and `inject_stats_databricks`. When a platform has already
  processed the source data, a DBA passes its statistical output to statschema instead of (or to
  supplement) `collect_table_stats`, giving the optimizer bootstrap step the benefit of row-level
  distribution modeling without requiring statschema itself to access production rows.
- **Why**: Closes the Oracle and DB2 sparse-stats gap observed in cross-engine benchmarks
  (`ndistinct_w2x = 0.366` for Oracle×TPC-E, `0.058` for DB2×TPC-DI). Organizations that already
  use one of these platforms for data generation or compliance can route its statistical output
  through statschema at no additional compliance cost.

### 7. Temporal and sequential column patterns
- **Gap**: Date and timestamp columns are generated from uniform distribution between `min_value` and `max_value`. Sequential integer IDs have no monotonic ordering.
- **Done**: Detect date/timestamp columns and apply configurable time-of-day and day-of-week weight distributions driven by MCV frequencies. Detect sequential ID columns (n_distinct ≈ row_count, integer type) and generate gapless sequences.

---

## FK-Aware Generation

### 8. Cardinality-aware FK fan-out
- **Gap**: FK child rows are generated by sampling parent PKs uniformly at random. Real tables have non-uniform fan-out (Zipfian or power-law distribution).
- **Done**: Add `fan_out_distribution` to `ForeignKeyStats` (uniform / zipfian / power-law + parameter). Use it in `build_dataframe_from_canonical` to weight parent PK assignment. Infer the distribution from `histogram_bounds` on the FK column when available.

### 9. Multi-table generation ordering and referential integrity
- **Gap**: Tables are generated independently in declaration order. If a child table is generated before its parent, FK values have nothing to reference.
- **Done**: Implement a topological sort of `CanonicalTableSchema` objects by FK dependencies. Generate parent tables first. Pass the generated parent PK column as the sampling pool for child FK columns. Handle circular FK references with a deferred constraint pass.

---

## Optimizer Bootstrap

### 11. Extended / multi-column statistics injection (PostgreSQL)
- **Gap**: `inject_stats_postgres` sets per-column statistics only. PostgreSQL `CREATE STATISTICS` extended stats (column dependencies, MCV combinations across column groups) are not injected.
- **Done**: Collect `CompositeColumnStats` from source PostgreSQL via `pg_stats_ext`. Emit `CREATE STATISTICS … ON (col_a, col_b)` for the target and inject multi-column MCV data via `pg_restore_attribute_stats` when the target is PG 19+.

### 12. MariaDB histogram injection
- **Gap**: `inject_stats_mysql` targets MySQL 8.0.31+ histogram format. MariaDB uses a different JSON histogram format in `mysql.column_stats`.
- **Done**: Detect MariaDB by server version string. Use MariaDB's `ANALYZE TABLE … PERSISTENT FOR COLUMNS` with JSON histogram injection for the MariaDB-specific format.

### 13. SYSSTAT.COLDIST histogram injection for Db2 (quantile accuracy)
- **Gap**: `inject_stats_db2` approximates `VALCOUNT` for `TYPE='Q'` histogram rows as a linear fraction of row count. Real Db2 quantile histograms are equi-depth — each bucket contains approximately the same number of rows.
- **Done**: Convert `histogram_bounds` to equi-depth quantile rows: compute cumulative row count per bucket from the histogram bucket widths rather than linear interpolation. Match Db2's `RUNSTATS WITH DISTRIBUTION` equi-depth output format.
