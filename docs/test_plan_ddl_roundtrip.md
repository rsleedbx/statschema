# DDL & Statistics Round-Trip — Test Plan

**File:** `tests/test_ddl_roundtrip.py`  
**Helpers:** `tests/helpers.py` (reusable across all test files)  
**Total tests:** 1,765 (all passing)

---

## Contents

1. [Purpose and scope](#1-purpose-and-scope)
2. [Reusable helpers](#2-reusable-helpers)
3. [Phase 1 — Same-dialect idempotent emission](#3-phase-1--same-dialect-idempotent-emission)
4. [Phase 2 — Random canonical round-trips](#4-phase-2--random-canonical-round-trips)
5. [Phase 3 — Statistics YAML round-trip](#5-phase-3--statistics-yaml-round-trip)
6. [Phase 4 — Override application](#6-phase-4--override-application)
7. [Phase 5 — Source type coverage](#7-phase-5--source-type-coverage)
8. [Phase 6 — Precision, scale, and length boundaries](#8-phase-6--precision-scale-and-length-boundaries)
9. [Phase 7 — Structural edge cases](#9-phase-7--structural-edge-cases)
10. [Phase 8 — Temporal types (time zones and fractional-seconds precision)](#10-phase-8--temporal-types)
11. [Phase 9 — Common migration issues](#11-phase-9--common-migration-issues)
12. [Coverage summary](#12-coverage-summary)
13. [Known gaps and out-of-scope items](#13-known-gaps-and-out-of-scope-items)

---

## 1. Purpose and scope

The tests verify three properties:

| Property | Definition |
|---|---|
| **Idempotent emission** | `emit(canonical, d)` → `parse(d)` → `emit(canonical, d)` produces the same bytes twice |
| **Canonical type correctness** | Every source SQL type parses to its documented canonical type |
| **Precision / length fidelity** | `DECIMAL(p,s)` and `VARCHAR(n)` values survive unchanged through the canonical model |

**Dialects under test:**

| Dialect | Parse path | Emit path |
|---|---|---|
| MySQL 8+ | `parse_ddl(sql, "mysql")` | `emit_ddl(table, "mysql")` |
| PostgreSQL 13+ | `parse_ddl(sql, "postgres")` | `emit_ddl(table, "postgres")` |
| SQL Server 2019+ | `parse_ddl(sql, "sqlserver")` | `emit_ddl(table, "sqlserver")` |
| Databricks SQL | via MySQL parser (same backtick syntax) | `emit_ddl(table, "databricks")` |
| Oracle 19c | **emit-only** (no open-source parser) | `emit_ddl(table, "oracle")` |

---

## 2. Reusable helpers

`tests/helpers.py` exposes public functions usable in any test file:

```python
from tests.helpers import (
    make_col,                  # build CanonicalColumn
    make_table,                # build CanonicalTableSchema from columns
    make_pk_table,             # table with auto-increment PK + extra cols
    ddl_roundtrip,             # (table, emit_d, parse_d) → (ddl1, ddl2)
    assert_ddl_roundtrip,      # asserts ddl1 == ddl2, returns ddl1
    parse_first_roundtrip,     # raw_ddl → emit → parse → emit, returns (ddl1, ddl2)
    assert_parse_first_roundtrip,  # asserts idempotency starting from raw DDL
    parse_col_type,            # parse raw DDL → canonical type of column N
)
```

### Usage example — new test file

```python
from tests.helpers import make_col, make_pk_table, assert_ddl_roundtrip

def test_my_migration_schema():
    table = make_pk_table("customer",
        make_col("email",  "string",  length=320, not_null=True, unique=True),
        make_col("balance","decimal", precision=14, scale=2),
    )
    assert_ddl_roundtrip(table, "mysql")
    assert_ddl_roundtrip(table, "postgres")
```

---

## 3. Phase 1 — Same-dialect idempotent emission

**Class:** `TestPhase1SameDialectRoundtrip`

For every `(emit_dialect, canonical_type, constraint_variant)` combination the class runs **5 assertions**:

| Test method | What it checks |
|---|---|
| `test_roundtrip_idempotent` | DDL string is byte-identical after emit→parse→emit |
| `test_ddl_non_empty` | Emitted DDL is non-empty and contains `CREATE TABLE` |
| `test_column_count_preserved` | Column count unchanged after one round-trip |
| `test_pk_preserved` | PRIMARY KEY column set is identical |
| `test_not_null_preserved` | All NOT NULL columns remain NOT NULL |
| `test_precision_preserved` | DECIMAL(p,s) and VARCHAR(n) values unchanged |

### Canonical types × constraint variants tested

| Canonical type | Constraint variants |
|---|---|
| `integer` | plain, NOT NULL, PK+AI, DEFAULT 0, UNIQUE |
| `long` | plain, PK+AI |
| `string` | no length, length=50, length=255+NOT NULL, length+DEFAULT, length+UNIQUE |
| `float` | plain, NOT NULL |
| `double` | plain, NOT NULL |
| `boolean` | plain, NOT NULL, DEFAULT (dialect-specific value) |
| `timestamp` | plain, NOT NULL |
| `date` | plain, NOT NULL |
| `decimal` | (10,2), (18,4), (10,0), (5,2), (38,10), with NOT NULL+DEFAULT |
| multi-column (all types) | a realistic `orders` table |

### Dialects × test count

| Dialect | Cases × 6 assertions |
|---|---|
| mysql | ~35 cases × 6 = 210 |
| postgres | ~33 cases × 6 = 198 |
| sqlserver | ~30 cases × 6 = 180 |
| databricks (parsed as mysql) | ~20 cases × 6 = 120 |

**Class:** `TestPhase1OracleEmitOnly`  
Oracle emit-only: non-empty DDL, double-emit idempotency, type name spot-checks
(NUMBER, VARCHAR2, TIMESTAMP).

---

## 4. Phase 2 — Random canonical round-trips

**Class:** `TestPhase2RandomRoundtrip`

60 randomly generated tables per dialect, seeded for determinism:

| Parametrized test | Dialect | Seed |
|---|---|---|
| `test_random_mysql[mysql-rnd-0..59]` | mysql | 1 |
| `test_random_postgres[postgres-rnd-0..59]` | postgres | 2 |
| `test_random_sqlserver[sqlserver-rnd-0..59]` | sqlserver | 3 |
| `test_random_databricks[databricks-rnd-0..59]` | databricks/mysql | 4 |

Each table: 1–7 extra columns with random types and constraint combos.

**Coverage assertions:**
- `test_all_canonical_types_covered_mysql` — all 9 canonical types appear in the MySQL random set
- `test_all_canonical_types_covered_postgres` — same for postgres
- `test_all_canonical_types_covered_sqlserver` — same for sqlserver

---

## 5. Phase 3 — Statistics YAML round-trip

**Class:** `TestPhase3StatsRoundtrip`

| Test | What it verifies |
|---|---|
| `test_to_dict_from_dict` | Full `DatabaseStats` dict equals itself after to_dict→from_dict |
| `test_table_stats_roundtrip` | `TableStats` dataclass equality |
| `test_column_stats_roundtrip` | `ColumnStats` with all fields populated |
| `test_column_stats_zero_n_distinct_omitted` | n_distinct=0 (unknown) is omitted from YAML |
| `test_index_stats_roundtrip` | `IndexStats` equality |
| `test_fk_stats_roundtrip` | Composite FK stats with all fields |
| `test_composite_stats_roundtrip` | `CompositeColumnStats` with dependencies and MCVs |
| `test_most_common_value_roundtrip` | `MostCommonValue` with quoted string |
| `test_mcv_combination_roundtrip` | `MCVCombination` multi-value |
| `test_file_dump_and_load` | YAML file write + read round-trip via `dump_stats`/`load_stats` |
| `test_load_from_dict` | `load_stats(dict)` path |
| `test_yaml_is_human_readable` | YAML contains human-readable field names |
| `test_version_field_preserved` | `version` field survives round-trip |
| `test_empty_database_stats_roundtrip` | Empty stats round-trips without error |
| `test_table_stats_lookup` | `DatabaseStats.table_stats(name)` lookup |
| `test_column_stats_lookup` | `TableStats.column_stats(name)` lookup |
| `test_negative_n_distinct_postgres_convention` | Negative n_distinct (fraction encoding) |
| `test_make_default_stats_all_types` | Default stats generated for all 9 canonical types |
| `test_make_default_stats_pk_index` | PK column gets a unique index entry |
| `test_make_default_stats_yaml_roundtrip` | `make_default_stats` output round-trips |
| `test_make_default_stats_with_fk_constraints` | FK constraints become ForeignKeyStats |

---

## 6. Phase 4 — Override application

**Class:** `TestPhase4Overrides`

| Test group | Tests |
|---|---|
| YAML serialisation | `test_override_spec_empty_roundtrip`, `test_override_spec_dict_roundtrip`, `test_override_spec_yaml_file_roundtrip` |
| Table rename | `test_table_rename`, `test_table_rename_no_stats` |
| Column rename | `test_column_rename`, `test_column_rename_propagates_to_fk` |
| Type override | `test_column_type_override`, `test_global_type_mapping`, `test_column_type_override_beats_global_mapping` |
| Precision/length | `test_column_length_override`, `test_column_precision_scale_override` |
| Row count scaling | `test_global_row_count_scale`, `test_table_level_row_count_explicit`, `test_table_level_row_count_scale`, `test_table_explicit_beats_global_scale`, `test_row_count_scale_min_one` |
| Schema/catalog | `test_schema_mapping`, `test_catalog_mapping`, `test_schema_mapping_propagates_to_fk` |
| Identifier case | `test_uppercase_identifiers`, `test_lowercase_identifiers` |
| Reserved words | `test_reserved_word_prefix_oracle`, `test_oracle_builtin_reserved_words`, `test_postgres_builtin_reserved_words`, `test_sqlserver_builtin_reserved_words` |
| Statistical | `test_column_null_fraction_override`, `test_column_n_distinct_override`, `test_column_min_max_override`, `test_column_mcv_override` |
| Batch | `test_apply_overrides_all`, `test_apply_overrides_all_no_stats` |
| Immutability | `test_override_does_not_mutate_input` |
| Scenarios | `test_combined_migration_scenario_oracle`, `test_combined_migration_scenario_databricks` |

---

## 7. Phase 5 — Source type coverage

Every SQL type in each parser's type map is exercised with **2 assertions per type**:

| Assertion | What it checks |
|---|---|
| `test_canonical_type` | Raw DDL type maps to the documented canonical type |
| `test_parse_first_idempotent` | `emit(parse(raw))` → `parse` → `emit` is idempotent |

Plus one completeness assertion per dialect:
- `test_mysql_type_map_fully_covered` — every key in `MYSQL_TYPE_TO_CANONICAL` has a test
- `test_postgres_type_map_fully_covered` — same for `POSTGRES_TYPE_TO_CANONICAL`
- `test_sqlserver_type_map_fully_covered` — same for `SQLSERVER_TYPE_TO_CANONICAL`

### MySQL source types (38 types)

| SQL type | Canonical | Notes |
|---|---|---|
| `TINYINT` | integer | display-width dropped |
| `TINYINT(1)` | **boolean** | de-facto BOOLEAN alias |
| `SMALLINT` | integer | |
| `MEDIUMINT` | integer | |
| `INT` | integer | |
| `INTEGER` | integer | synonym |
| `BIGINT` | long | |
| `DECIMAL(10,2)` | decimal | |
| `DECIMAL(1,0)` | decimal | min precision boundary |
| `DECIMAL(38,0)` | decimal | max precision boundary |
| `DECIMAL(38,37)` | decimal | max scale boundary |
| `NUMERIC(5,3)` | decimal | synonym |
| `FLOAT` | float | |
| `DOUBLE` | double | |
| `REAL` | double | synonym for DOUBLE in MySQL |
| `BIT` | boolean | |
| `BOOLEAN` | boolean | TINYINT(1) alias |
| `BOOL` | boolean | synonym |
| `CHAR(10)` | string | length preserved |
| `VARCHAR(1)` | string | min length |
| `VARCHAR(255)` | string | |
| `VARCHAR(65535)` | string | MySQL theoretical max |
| `BINARY(16)` | string | |
| `VARBINARY(255)` | string | |
| `TINYTEXT` | string | |
| `TEXT` | string | |
| `MEDIUMTEXT` | string | |
| `LONGTEXT` | string | |
| `JSON` | string | |
| `JSONB` | string | Databricks fallback |
| `STRING` | string | Databricks via MySQL parser |
| `DATE` | date | |
| `DATETIME` | timestamp | |
| `TIMESTAMP` | timestamp | |
| `TIME` | string | no canonical time type |
| `YEAR` | integer | |
| `ENUM('a','b','c')` | string | values discarded |
| `SET('x','y')` | string | values discarded |

### PostgreSQL source types (35 types)

| SQL type | Canonical | Notes |
|---|---|---|
| `SMALLINT` | integer | |
| `INTEGER` | integer | |
| `INT` | integer | |
| `INT2` | integer | alias |
| `INT4` | integer | alias |
| `BIGINT` | long | |
| `INT8` | long | alias |
| `DECIMAL(10,2)` | decimal | |
| `DECIMAL(1,0)` | decimal | min precision |
| `DECIMAL(38,0)` | decimal | max precision |
| `DECIMAL(38,37)` | decimal | max scale |
| `NUMERIC(5,3)` | decimal | synonym |
| `REAL` | float | 32-bit |
| `FLOAT4` | float | alias |
| `DOUBLE PRECISION` | double | 64-bit |
| `FLOAT8` | double | alias |
| `BOOLEAN` | boolean | |
| `BOOL` | boolean | synonym |
| `CHARACTER VARYING(1)` | string | min length |
| `CHARACTER VARYING(255)` | string | |
| `CHARACTER VARYING(10485760)` | string | PG max |
| `VARCHAR(100)` | string | |
| `CHARACTER(10)` | string | |
| `CHAR(5)` | string | |
| `TEXT` | string | |
| `JSON` | string | |
| `JSONB` | string | |
| `DATE` | date | |
| `TIMESTAMP` | timestamp | |
| `TIMESTAMP WITHOUT TIME ZONE` | timestamp | |
| `TIMESTAMP WITH TIME ZONE` | timestamp | timezone discarded in canonical |
| `TIMESTAMPTZ` | timestamp | |
| `TIME` | string | |
| `INTERVAL` | string | |
| `UUID` | string | |
| `SERIAL` | integer + auto_increment | shorthand |
| `BIGSERIAL` | long + auto_increment | shorthand |

### SQL Server source types (30 types)

| SQL type | Canonical | Notes |
|---|---|---|
| `TINYINT` | integer | |
| `SMALLINT` | integer | |
| `INT` | integer | |
| `BIGINT` | long | |
| `DECIMAL(10,2)` | decimal | |
| `DECIMAL(1,0)` | decimal | min precision |
| `DECIMAL(38,0)` | decimal | max precision |
| `DECIMAL(38,37)` | decimal | max scale |
| `NUMERIC(5,3)` | decimal | synonym |
| `FLOAT` | **double** | FLOAT(53) = 64-bit |
| `REAL` | float | FLOAT(24) = 32-bit |
| `BIT` | boolean | |
| `CHAR(10)` | string | length preserved; emitted as NVARCHAR |
| `VARCHAR(1)` | string | min length; emitted as NVARCHAR |
| `VARCHAR(255)` | string | emitted as NVARCHAR |
| `NCHAR(10)` | string | length preserved |
| `NVARCHAR(1)` | string | min length |
| `NVARCHAR(100)` | string | |
| `NVARCHAR(4000)` | string | SQL Server NVARCHAR limit |
| `NVARCHAR(MAX)` | string | MAX → no stored length |
| `TEXT` | string | emitted as NVARCHAR(MAX) |
| `NTEXT` | string | emitted as NVARCHAR(MAX) |
| `DATE` | date | |
| `DATETIME` | timestamp | emitted as DATETIME2 |
| `DATETIME2` | timestamp | |
| `SMALLDATETIME` | timestamp | emitted as DATETIME2 |
| `DATETIMEOFFSET` | timestamp | timezone discarded |
| `TIME` | string | |
| `UNIQUEIDENTIFIER` | string | emitted as NVARCHAR(MAX) |

---

## 8. Phase 6 — Precision, scale, and length boundaries

### DECIMAL boundaries (tested per dialect: mysql, postgres, sqlserver)

| Precision | Scale | ID | Property tested |
|---|---|---|---|
| 1 | 0 | `min_precision` | Absolute minimum |
| 1 | 1 | `scale_equals_precision_min` | scale == precision edge |
| 5 | 5 | `scale_equals_precision_5` | scale == precision mid |
| 10 | 0 | `mid_precision_no_scale` | integer-like decimal |
| 10 | 2 | `common_money` | Money pattern |
| 18 | 4 | `common_default` | Canonical default |
| 18 | 18 | `scale_equals_precision_18` | |
| 38 | 0 | `max_precision_no_scale` | Max precision |
| 38 | 37 | `max_precision_near_max_scale` | Near-max scale |
| 38 | 38 | `max_precision_max_scale` | Max both |

Each boundary is tested with:
- Round-trip idempotency (emit → parse → emit == first emit)
- Precision value preserved exactly in parsed column
- Scale value preserved exactly in parsed column
- Oracle `NUMBER(p,s)` syntax correct
- Databricks `DECIMAL(p,s)` syntax correct

### String length boundaries (tested per dialect: mysql, postgres, sqlserver)

| Length | ID | Significance |
|---|---|---|
| 1 | `min_length` | Minimum possible |
| 10 | `small` | Short string |
| 255 | `common_max_255` | Common application max |
| 256 | `just_over_255` | One over common max |
| 4000 | `sqlserver_nvarchar_max` | SQL Server NVARCHAR limit |
| 8000 | `sqlserver_varchar_max` | SQL Server VARCHAR limit |
| 65535 | `mysql_varchar_theoretical_max` | MySQL max |

Each boundary: round-trip idempotency + length value preserved exactly.

Additional tests:
- `test_no_length_string_defaults` — no-length string produces dialect-correct default type
- `test_no_length_string_roundtrips` — TEXT/NVARCHAR(MAX) are idempotent

### DEFAULT expression boundaries

| Expression | Dialects | ID |
|---|---|---|
| `0` on integer | all | `int_zero` |
| `1` on integer | all | `int_one` |
| `42` on integer | all | `int_value` |
| `0` on long | all | `long_zero` |
| `'active'` on string | all | `string_quoted` |
| `'pending order'` on string | all | `string_quoted_space` |
| `1` on boolean | mysql, sqlserver | `bool_one_mysql` |
| `0.00` on decimal | all | `decimal_zero` |
| `1.5` on decimal | all | `decimal_value` |
| `CURRENT_TIMESTAMP` on timestamp | mysql, postgres | `ts_current` |

---

## 9. Phase 7 — Structural edge cases

**Class:** `TestPhase7StructuralEdgeCases`

| Test group | Tests |
|---|---|
| Multi-column PK | `test_multi_column_pk[mysql/postgres/sqlserver/databricks]`, idempotent emit for all three parseable dialects |
| FK parsing | `test_fk_parsed_from_mysql_ddl`, `test_fk_parsed_from_postgres_ddl`, `test_composite_fk_parsed`, `test_inline_fk_without_constraint_name`, `test_cross_schema_fk_parent` |
| Auto-increment | `test_mysql_auto_increment_parsed`, `test_postgres_serial_auto_increment_parsed`, `test_postgres_bigserial_auto_increment_parsed`, `test_sqlserver_identity_auto_increment_parsed` |
| NOT NULL / UNIQUE | `test_not_null_roundtrip[mysql/postgres/sqlserver]`, `test_unique_roundtrip[mysql/postgres/sqlserver]` |
| Single-column table | `test_single_column_pk_only[mysql/postgres/sqlserver]` |
| Wide table (all 9 types) | `test_wide_table_mysql`, `test_wide_table_postgres`, `test_wide_table_sqlserver` |
| Table naming | `test_table_name_with_underscore`, `test_table_name_starts_with_digit_like` |

---

## 10. Phase 8 — Temporal types

**Class:** `TestPhase8TemporalTypes`

### Background: temporal differences across databases

Databases differ in four ways that matter for DDL round-trips:

| Aspect | MySQL | PostgreSQL | SQL Server | Oracle | Databricks |
|---|---|---|---|---|---|
| Max fsp (fractional-seconds) | 6 (µs) | 6 (µs) | 7 (100 ns) | 9 (ns) | — (fixed, not specifiable) |
| Timezone-aware timestamp | `TIMESTAMP` (UTC) | `TIMESTAMP WITH TIME ZONE` / `TIMESTAMPTZ` | `DATETIMEOFFSET` | `TIMESTAMP WITH TIME ZONE` | `TIMESTAMP` |
| Timezone-naive timestamp | `DATETIME` | `TIMESTAMP` (no qualifier) | `DATETIME2` | `TIMESTAMP` | `TIMESTAMP_NTZ` |
| Time-of-day type | `TIME` | `TIME` / `TIME WITH TIME ZONE` / `TIMETZ` | `TIME` | ❌ (no native TIME) | ❌ (no native TIME) |

### New canonical types

Three new types were added to the canonical model:

| Canonical type | Meaning | Maps to |
|---|---|---|
| `timestamptz` | Date + time, timezone-aware | `TIMESTAMP` (MySQL), `TIMESTAMP WITH TIME ZONE` (PG/Oracle), `DATETIMEOFFSET` (SS), `TIMESTAMP` (Databricks) |
| `time` | Time-of-day, no timezone | `TIME` (MySQL/PG/SS), `TIMESTAMP` (Oracle — lossy), `STRING` (Databricks — lossy) |
| `timetz` | Time-of-day with timezone | `TIME` (MySQL/SS — degrade), `TIME WITH TIME ZONE` (PG), `TIMESTAMP WITH TIME ZONE` (Oracle), `STRING` (Databricks) |

### New model field

`CanonicalColumn.fsp: Optional[int]` — fractional-seconds precision, `None` = dialect default.
Emitted as the `(N)` argument on temporal DDL tokens; silently dropped for Databricks.

### Test coverage

| Test group | What is verified | Count |
|---|---|---|
| `test_emit_no_fsp` | 20 canonical × dialect emit combinations (no fsp) | 20 |
| `test_fsp_roundtrip` | emit(fsp) → parse → re-emit is idempotent | 28 |
| `test_fsp_preserved_in_ddl` | emitted DDL contains `(N)` where supported | 14 |
| `test_databricks_fsp_dropped` | Databricks never emits `(N)` for temporals | 1 |
| `test_parse_temporal_canonical` | type + fsp parsed correctly from raw DDL | 26 |
| `test_parse_first_idempotent` | parse-first round-trip idempotent | 18 |
| `test_oracle_emit` | Oracle temporal fragments (emit-only) | 7 |
| `test_mysql_fsp_boundary` | fsp 0/1/3/6 round-trips for MySQL | 4 |
| `test_postgres_fsp_boundary` | fsp 0/1/3/6 round-trips for PostgreSQL | 4 |
| `test_sqlserver_fsp_boundary` | fsp 0/1/3/7 round-trips for SQL Server | 4 |
| `test_oracle_fsp_emit_boundary` | fsp 0/3/6/9 Oracle emission (incl. nanoseconds) | 4 |
| `test_current_timestamp_default` | DEFAULT CURRENT_TIMESTAMP + temporal type | 3 |
| `test_fsp_with_default` | fsp=3 + DEFAULT CURRENT_TIMESTAMP coexist | 3 |
| `test_timestamptz_cross_dialect_emit` | one column emitted to all 5 dialects | 1 |
| `test_time_cross_dialect_emit` | time degradation on Oracle and Databricks | 1 |
| `test_databricks_timestamp_vs_timestamp_ntz` | TIMESTAMP vs TIMESTAMP_NTZ in one table | 1 |
| `test_mixed_temporal_table` | table with date+timestamp+timestamptz+time | 3 |

**Total Phase 8 tests: 136**

### Parsing `TIMESTAMP(n) WITH TIME ZONE`

A previous parser limitation (`(n)` would be separated from `WITH TIME ZONE` as `rest`) is now fixed by a pre-pass that merges temporal qualifiers back into the type string before `_extract_type_precision` is called.

---

## 11. Phase 9 — Common migration issues

**Class:** `TestPhase9MigrationIssues`

Derived from production migration guides: Databricks Lakeflow Connect, DBConvert Streams, Microsoft SSMA, and Oracle-to-PostgreSQL migration research (2025).

### New canonical type: `binary`

`binary` was added to represent all byte-string storage across dialects:

| Dialect | No-length default | With `length=N` |
|---|---|---|
| MySQL | `LONGBLOB` | `VARBINARY(N)` |
| PostgreSQL | `BYTEA` | `BYTEA` (length ignored) |
| SQL Server | `VARBINARY(MAX)` | `VARBINARY(N)` |
| Oracle | `BLOB` | `RAW(N)` if N ≤ 2000, else `BLOB` |
| Databricks | `BINARY` | `BINARY` (no length in Databricks DDL) |

### New `unsigned` field

`CanonicalColumn.unsigned: bool` is set when MySQL `UNSIGNED` is present, and the canonical type is widened to prevent integer overflow:

| Source | Widening | Reason |
|---|---|---|
| `INT UNSIGNED` | `integer` → `long` | Max 4,294,967,295 > INT32 max |
| `BIGINT UNSIGNED` | `long` → `decimal(20,0)` | Max 18,446,744,073,709,551,615 > INT64 max |
| `TINYINT/SMALLINT/MEDIUMINT UNSIGNED` | no change | Max values fit in INT32 |

### Oracle `NUMBER(p)` widening

When Oracle DDL is parsed via the MySQL fallback, `NUMBER(p)` without a scale argument is mapped based on digit count:

| Precision | Canonical type | Reason |
|---|---|---|
| p ≤ 9 | `integer` | Max 999,999,999 fits in INT32 |
| 10 ≤ p ≤ 18 | `long` | Max 10^18 fits in INT64 |
| p ≥ 19 | `decimal(p,0)` | Exceeds INT64 |
| `NUMBER` (no args) | `decimal` | Unbounded |

### SQL Server `TIMESTAMP` is NOT a datetime

> ⚠️ Critical gotcha: SQL Server `TIMESTAMP` is an alias for `ROWVERSION` — an 8-byte monotonic counter used for optimistic concurrency. It is NOT a date/time type.

Both `ROWVERSION` and `TIMESTAMP` now map to canonical `binary` in the SQL Server parser. A test explicitly asserts this distinction.

### MONEY precision

| Source type | Canonical | Precision | Scale | Source |
|---|---|---|---|---|
| SQL Server `MONEY` | `decimal` | 19 | 4 | SQL Server spec |
| SQL Server `SMALLMONEY` | `decimal` | 10 | 4 | SQL Server spec |
| PostgreSQL `MONEY` | `decimal` | 19 | 2 | Databricks Lakeflow Connect |

### Test coverage

| Test group | What is verified | Count |
|---|---|---|
| `test_binary_length_preserved` | BINARY(n)/VARBINARY(n)/RAW(n) preserve byte-length | 5 |
| `test_unbounded_binary_canonical` | BLOB/IMAGE/ROWVERSION/BYTEA → binary, length=None | 9 |
| `test_binary_emit` | Correct SQL token per dialect with/without length | 11 |
| `test_binary_roundtrip_with_length` | VARBINARY(255) idempotent on MySQL/PG/SS | 3 |
| `test_binary_roundtrip_no_length` | Unbounded binary idempotent on all 3 parseable dialects | 1 |
| `test_sqlserver_timestamp_is_rowversion` | SQL Server TIMESTAMP ≠ datetime | 1 |
| `test_sqlserver_rowversion_explicit` | ROWVERSION → binary | 1 |
| `test_sqlserver_money_precision` | MONEY/SMALLMONEY → correct decimal(p,s) | 2 |
| `test_money_roundtrip` | MONEY/SMALLMONEY round-trips as decimal | 3 |
| `test_pg_money_precision` | PostgreSQL MONEY → decimal(19,2) ≠ SS decimal(19,4) | 1 |
| `test_unsigned_widening` | UNSIGNED type widening for 6 MySQL types | 6 |
| `test_bigint_unsigned_precision` | BIGINT UNSIGNED → decimal(20,0) exact | 1 |
| `test_non_unsigned_columns_have_unsigned_false` | Signed columns have unsigned=False | 1 |
| `test_oracle_number_widening` | NUMBER(p) → integer/long/decimal based on precision | 8 |
| `test_oracle_number_scale_zero_for_large_p` | NUMBER(19/38) get scale=0 | 1 |
| `test_pg_bytea_roundtrip` | BYTEA parse-first round-trip | 1 |
| `test_sqlserver_string_types` | XML/SQL_VARIANT/HIERARCHYID → string | 3 |
| Cross-dialect scenarios | BLOB→BYTEA, VARBINARY→VARBINARY, MONEY migration | 4 |
| `test_migration_table_roundtrip` | Mixed binary+decimal+temporal table | 3 |

**Total Phase 9 tests: 65**

---

## 12. Coverage summary

### Canonical type × dialect coverage

| Canonical type | mysql emit | mysql parse | pg emit | pg parse | ss emit | ss parse | db emit | oracle emit |
|---|---|---|---|---|---|---|---|---|
| integer | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| long | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| string (no len) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| string (with len) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| float | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| double | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| boolean | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| timestamp (no TZ) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| timestamptz | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| time | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| timetz | ✅ | ⚠️ degrade | ✅ | ✅ | ✅ | ⚠️ degrade | ✅ | ✅ |
| date | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| decimal (with p,s) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| **binary (no len)** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| **binary (with len)** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

⚠️ degrade = timetz maps to TIME (losing timezone info) because MySQL and SQL Server have no `TIME WITH TIME ZONE` type.

### Source type map coverage

| Dialect | Types in parser map | Types with source-DDL test | Coverage |
|---|---|---|---|
| MySQL | 51 | 51 | **100%** |
| PostgreSQL | 51 | 51 | **100%** |
| SQL Server | 43 | 43 | **100%** |
| Oracle | N/A (emit-only) | N/A (key types tested via MySQL fallback) | N/A |
| Databricks | N/A (via MySQL) | via MySQL map | N/A |

### Precision boundary coverage

| Dimension | Boundaries tested | Dialects |
|---|---|---|
| DECIMAL precision | 1, 5, 10, 18, 38 | mysql, postgres, sqlserver, oracle, databricks |
| DECIMAL scale | 0, 2, 3, 4, 5, 18, 37, 38 | mysql, postgres, sqlserver, oracle, databricks |
| scale == precision | (1,1), (5,5), (18,18), (38,38) | mysql, postgres, sqlserver |
| string length | 1, 10, 255, 256, 4000, 8000, 65535 | mysql, postgres, sqlserver, oracle |
| no-length string | default type per dialect | all 5 dialects |
| **temporal fsp (MySQL)** | 0, 1, 3, 6 | mysql |
| **temporal fsp (PostgreSQL)** | 0, 1, 3, 6 | postgres |
| **temporal fsp (SQL Server)** | 0, 1, 3, 7 | sqlserver |
| **temporal fsp (Oracle)** | 0, 3, 6, 9 | oracle (emit-only) |

---

## 13. Known gaps and out-of-scope items

The following items are **not currently tested**, with the reason noted:

### Parser gaps

| Item | Status | Reason |
|---|---|---|
| Oracle DDL parsing (native) | ❌ Not tested | No open-source Oracle DDL grammar; Oracle is emit-only |
| Databricks DDL parsing (native) | ❌ Not tested | Databricks re-uses MySQL parser; no dedicated `dialect="databricks"` |
| MySQL `UNSIGNED` modifier | ✅ **Implemented** | INT/BIGINT UNSIGNED widened to long/decimal; `col.unsigned=True` stored |
| MySQL `ZEROFILL` modifier | ❌ Not tested | Dropped; zero-fill is display-only in MySQL 8+ |
| MySQL `COMMENT '...'` clause | ❌ Not tested | Column comments discarded by parser |
| MySQL `ON UPDATE CURRENT_TIMESTAMP` | ❌ Not tested | Requires trigger in PG/Oracle; not modelled |
| PostgreSQL `COLLATE` clause | ❌ Not tested | Collation discarded |
| PostgreSQL array types (`INT[]`) | ❌ Not tested | Not in type map |
| PostgreSQL `ENUM` type | ❌ Not tested | Postgres ENUMs require `CREATE TYPE` first |
| PostgreSQL `SERIAL` as non-PK | ⚠️ Partial | SERIAL PK covered; non-PK SERIAL not |
| SQL Server `MONEY` / `SMALLMONEY` | ✅ **Implemented** | Precision/scale injected from spec; fully tested |
| SQL Server `BINARY` / `VARBINARY` | ✅ **Implemented** | Maps to canonical `binary`; length preserved |
| SQL Server `XML`, `SQL_VARIANT`, `HIERARCHYID` | ✅ **Implemented** | All map to `string` |
| SQL Server `ROWVERSION` / `TIMESTAMP` | ✅ **Implemented** | Maps to `binary` (not a datetime!) |
| SQL Server `GEOGRAPHY` / `GEOMETRY` | ✅ **Implemented** | Maps to `binary` |
| SQL Server `IMAGE` | ✅ **Implemented** | Maps to `binary` |
| Oracle `NUMBER(p)` without scale | ✅ **Implemented** | Widened to integer/long/decimal(p,0) |
| Oracle `VARCHAR2`, `CLOB`, `NCLOB`, `BLOB`, `RAW` | ✅ **Implemented** | Via MySQL fallback |
| Oracle `DATE` includes time component | ⚠️ Not enforced | Maps to `date` via fallback; use OverrideSpec to change to `timestamp` |
| `DEFAULT` with `NEXTVAL(...)` | ❌ Not tested | Sequence references discarded |

### Temporal-type gaps

| Item | Status | Reason |
|---|---|---|
| Oracle `TIMESTAMP WITH LOCAL TIME ZONE` parsing | ❌ Not tested | Oracle is emit-only; maps to `timestamptz` on emit |
| Oracle `INTERVAL YEAR TO MONTH` / `INTERVAL DAY TO SECOND` | ❌ Not tested | Canonical `interval` type not yet defined |
| PostgreSQL `INTERVAL` types | ⚠️ Maps to `string` | Interval is a duration, not a point-in-time; separate canonical type needed |
| MySQL `TIMESTAMP` vs `DATETIME` as FK target | ❌ Not tested | FK type checks not tested |
| Databricks `TIMESTAMP_NTZ` as default for canonical `timestamp` | ✅ Implemented | Requires DBR 11.3+ / Delta 2.0+ |
| `timetz` round-trip on MySQL / SQL Server | ⚠️ Degrade | These dialects have no TIME WITH TIME ZONE; `timetz` emits as `TIME` (TZ info lost) |
| fsp > 6 on MySQL / PostgreSQL | ⚠️ Boundary | Max fsp is 6 for these dialects; behaviour for fsp=7 or fsp=9 is not validated |
| Nanosecond preservation (Oracle fsp=9) in cross-dialect migration | ❌ Not tested | Oracle ns → PG µs is a lossy conversion; no warning is emitted |

### Emitter gaps

| Item | Status | Reason |
|---|---|---|
| Oracle `IF NOT EXISTS` (pre-23c) | ✅ Tested | PL/SQL BEGIN/EXCEPTION block emitted |
| Oracle `CLOB` / `BLOB` → canonical `binary` emit | ✅ Implemented | Oracle `binary` emits `BLOB`; `binary(n≤2000)` emits `RAW(n)` |
| Databricks `GENERATED ALWAYS AS (expr)` | ❌ Not tested | Computed columns not in model |
| PostgreSQL `GENERATED ALWAYS AS IDENTITY` | ❌ Not tested | Only SERIAL emitted for auto_increment |
| MySQL `ENGINE=InnoDB` options | ✅ Tested (fixed) | Always appended; verified in round-trip |
| Dialect quoting of reserved-word identifiers | ⚠️ Partial | Tested via override; not auto-applied by emitter |

### Cross-dialect round-trips

| Combination | Status | Notes |
|---|---|---|
| MySQL → PostgreSQL | ❌ Not tested | Cross-dialect mapping requires explicit type coercion via OverrideSpec |
| MySQL → Databricks | ❌ Not tested | Same |
| PostgreSQL → Databricks | ❌ Not tested | Same |
| Oracle → Databricks | ❌ Not tested | Same; Oracle DDL not parseable |
| SQL Server → Databricks | ❌ Not tested | Requires type mapping (BIT→BOOLEAN, etc.) |

Cross-dialect round-trips are the responsibility of `OverrideSpec` + `apply_overrides`.
An end-to-end cross-dialect test would be: `parse(mysql_ddl) → apply_overrides(mysql→pg) → emit(pg) → parse(pg) → emit(pg) == first_pg_emit`.

### Statistics gaps

| Item | Status |
|---|---|
| Oracle `ALL_HISTOGRAMS` ENDPOINT_ACTUAL_VALUE | ❌ Not tested — collector not implemented |
| SQL Server density vector (multi-column) | ❌ Not tested — collector not implemented |
| PostgreSQL extended statistics (`pg_stats_ext`) | ❌ Not tested — collector not implemented |
| Databricks ANALYZE TABLE output | ❌ Not tested — collector not implemented |
| Stats with NULL `last_analyzed` | ✅ Implicitly (tested via `make_default_stats`) |

All stats-collection from live databases is out of scope for unit tests (requires a running database). The YAML round-trip for every stats model field is fully covered.

### Structural features not yet modelled

These DDL features are not in `CanonicalColumn` or `CanonicalTableSchema` and therefore cannot be tested:

| Feature | Impact |
|---|---|
| Column-level `CHECK` constraint | Lost on parse |
| `COLLATION` / `CHARSET` on column | Lost on parse |
| Computed / virtual columns | Not modelled |
| Table-level `UNIQUE` constraint (separate from inline) | Partially: inline UNIQUE is modelled |
| Partial indexes | Not modelled |
| Table partitioning | Not modelled |
| Storage parameters (`TABLESPACE`, `FILEGROUP`, etc.) | Not modelled |
| View DDL | Not modelled |
| Sequence DDL | Not modelled |
| Index DDL (outside CREATE TABLE) | Not modelled |

These are candidates for future canonical model extensions.
