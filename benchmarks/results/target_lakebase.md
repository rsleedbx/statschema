# Cross-Database → Lakebase Test Plan

The cross-database test answers a different question than the identity test: **if statschema
collects statistics from one database engine and injects them into Lakebase, do Lakebase query
plans with injected stats match Lakebase plans derived from its own ANALYZE?**

The identity test proves statschema can reproduce plans on the *same* engine.  This test measures
the additional loss introduced when the stats come from a *different* engine — MySQL, SQL Server,
Oracle, DB2, or PostgreSQL — whose catalog representations, histogram formats, and MCV lists
differ from Lakebase's.

Pipeline:
```
A  load data into Lakebase source schema
B  baseline EXPLAIN on Lakebase (ANALYZE on real data — ground truth)
C  collect stats from source DB engine  ← uses --stats-source-dsn
D  build synthetic copy on Lakebase, inject source-DB stats
C.5 compare Lakebase target stats vs source-DB stats (transfer fidelity)
E  replay EXPLAIN on Lakebase target (injected stats)
F  score E against B
```

Phase B establishes the Lakebase ground-truth plans.  Phase E tests whether injected stats can
reproduce those plans.  The gap between E and B is the cost of crossing database engine
boundaries.

---

## What source DB and Lakebase must agree on

Each invariant must hold before the next is meaningful.

| # | Invariant | How it is checked | Phase |
|---|-----------|-------------------|-------|
| 1 | **Canonical schema** — both the source DB and the Lakebase source schema are created from the same `TableDef` DDL (adapted to each dialect's syntax); the optimizer sees the same table structure | Same YAML schema used to generate DDL for each engine | A |
| 2 | **Row counts** — source DB and Lakebase source schema have the same number of rows per table; both represent the same data volume at the same scale factor | `source_row_counts` from source-DB schema vs Lakebase source schema count | A / C |
| 3 | **Source DB stats quality** — the source DB's ANALYZE has run and produced accurate statistics; columns with no stats on the source DB will transfer nothing to Lakebase | Phase C collect output: columns with `n_distinct=None` or `null_fraction=None` are gaps | C |
| 4 | **Stats transfer fidelity** — statistics collected from the source DB are reproduced on the Lakebase target after `pg_restore_attribute_stats` injection; the Lakebase optimizer is working from the same inputs the source DB described | `stats_fidelity.ndistinct_w2x`, `range_covered`, `null_match` per column | C.5 |
| 5 | **Query text** — all EXPLAIN runs on Lakebase use identical SQL (Lakebase dialect only); the optimizer answers the same question in both baseline and replay | Same query YAML, Lakebase dialect, applied to source and target schemas | B / E |
| 6 | **Query plans** — once 1–5 hold, Lakebase plans with injected stats should match Lakebase plans from native ANALYZE | `node_jaccard`, `within_2x` | F |

If invariant N fails, N+1 results are suspect.  In particular, invariant 3 is unique to the
cross-database case: if the source DB's stats catalog is sparse (e.g., DB2 does not collect MCVs
by default for most types), transfer fidelity can be high (4 passes) while the injected stats are
still less informative than Lakebase's own ANALYZE.

---

## Additional expectations by source engine

Each engine has known characteristics that affect what statistics transfer to Lakebase.

| Source engine | Expected stats quality | Known gaps |
|:---|:---|:---|
| **PostgreSQL 18** | Highest — histograms, MCVs, n_distinct, null_frac, correlation | None; same model as Lakebase |
| **CockroachDB** | High — similar to PG; n_distinct and MCVs via `SHOW STATISTICS` | No `correlation`; histogram buckets fewer than PG |
| **MySQL 8** | Medium — n_distinct, null_frac; histogram in JSON form (`information_schema.column_statistics`) | No MCV list for non-indexed columns; no `correlation` |
| **SQL Server 2022** | Medium-high — `sys.dm_db_stats_histogram` gives buckets and MCVs; very detailed for indexed columns | Unindexed columns have no histogram; no `correlation` |
| **Oracle 21c** | Medium — `ALL_TAB_COLUMNS` gives `num_distinct`, `num_nulls`, `low_value`, `high_value`; `DBMS_STATS` histograms available | Histograms are frequency or height-balanced, not equi-depth; no `correlation` |
| **DB2 LUW 11.5** | Low-medium — `SYSCAT.COLUMNS` gives `COLCARD`, `NUMNULLS`; `RUNSTATS` required for histograms | MCV and histogram availability depends on `RUNSTATS` options; no `correlation` |

For source engines with lower stats quality, plan scores (invariant 6) are expected to be lower
than the corresponding identity test scores, because Lakebase's native ANALYZE produces richer
statistics than what transferred.

---

## Stats fidelity metrics (Phase C.5)

Stats fidelity compares statistics collected from the **source DB** against statistics re-derived
by Lakebase's ANALYZE after the injected synthetic data is loaded.  A mismatch here means either
(a) the source DB had no stats for that column, or (b) the injection did not preserve the
distribution.

- **ndistinct_w2x** — fraction of columns where Lakebase target `n_distinct` is within 2× of source DB `n_distinct`.
- **range_covered** — fraction of numeric/date columns where Lakebase target `[min, max]` stays within 5% of the source DB range.  String columns excluded.
- **null_match** — fraction of columns where `null_fraction` differs by ≤ 0.02.

---

## Plan metrics

- **node_jaccard** — Jaccard similarity of plan-node-type multisets: Lakebase baseline (real data) vs Lakebase replay (injected stats).
- **within_2x** — fraction of plan nodes where the injected-stats row estimate is within 2× of the native-ANALYZE estimate.

Pass criteria: `node_jaccard ≥ 0.70` AND `within_2x ≥ 0.50`.  The same thresholds as the
identity test, but the baseline here is Lakebase-on-real-data, not source-DB plans.

---

## Test matrix

Six source engines × six TPC schemas = 36 runs.  Schema naming convention: `from_<engine>_<schema>_src`
so the origin is visible in the Lakebase catalog.

| Source engine | TPC-B | TPC-C | TPC-H | TPC-DI | TPC-DS | TPC-E |
|:---|:---:|:---:|:---:|:---:|:---:|:---:|
| PostgreSQL 18    | ○ | ○ | ○ | ○ | ○ | ○ |
| CockroachDB v23  | ○ | ○ | ○ | ○ | ○ | ○ |
| MySQL 8          | ○ | ○ | ○ | ○ | ○ | ○ |
| SQL Server 2022  | ○ | ○ | ○ | ○ | ○ | ○ |
| Oracle 21c XE    | ○ | ○ | ○ | ○ | ○ | ○ |
| DB2 LUW 11.5     | ○ | ○ | ○ | ○ | ○ | ○ |

○ = not yet run · ✓ = pass · ✗ = fail · — = skipped

Scale factors match the identity test: TPC-B SF=1, TPC-C SF=1, TPC-H SF=0.1, TPC-DI SF=5,
TPC-DS SF=0.01, TPC-E SF=0.01.

---

## Run instructions

Set DSNs for each engine and the Lakebase target, then call `identity_test.py` with
`--stats-source-dsn` pointing to the source engine.

```bash
source .venv_test/bin/activate

LAKEBASE_DSN="host=<lakebase-host> port=5432 dbname=databricks_postgres user=token password=<token>"
PG18_DSN="host=127.0.0.1 port=5418 dbname=postgres user=postgres password=postgres"
CRDB_DSN="host=127.0.0.1 port=26257 user=root dbname=defaultdb sslmode=disable"
MYSQL_DSN="host=127.0.0.1 port=3384 user=root password=testpass database=testdb"
MSSQL_DSN="server=127.0.0.1 port=1433 database=master user=sa password=TestPass123!"
ORACLE_DSN="host=127.0.0.1 port=1521 service=XE user=system password=oracle"
DB2_DSN="host=127.0.0.1 port=50000 database=testdb user=db2inst1 password=testpass"

for SCHEMA_SF in "tpcb:1" "tpcc:1" "tpch:0.1" "tpcdi:5" "tpcds:0.01" "tpce:0.01"; do
  SCHEMA=${SCHEMA_SF%%:*}
  SF=${SCHEMA_SF##*:}
  for ENGINE_DSN in "postgres:$PG18_DSN" "cockroachdb:$CRDB_DSN" "mysql:$MYSQL_DSN" \
                    "sqlserver:$MSSQL_DSN" "oracle:$ORACLE_DSN" "db2:$DB2_DSN"; do
    ENGINE=${ENGINE_DSN%%:*}
    SRC_DSN=${ENGINE_DSN#*:}
    python benchmarks/identity_test.py \
      --schema "$SCHEMA" --sf "$SF" \
      --dialect lakebase --dsn "$LAKEBASE_DSN" \
      --source-schema "from_${ENGINE}_${SCHEMA}_src" \
      --target-schema "from_${ENGINE}_${SCHEMA}_tgt" \
      --stats-source-dsn "$SRC_DSN" \
      --stats-source-dialect "$ENGINE" \
      --no-extended-stats \
      2>&1 | tee "/tmp/lakebase_${ENGINE}_${SCHEMA}.log"
  done
done
```

Results are saved to `benchmarks/results/<timestamp>-identity-<schema>-sf<N>-lakebase-from-<engine>.json`.

---

## Summary table

*To be filled in after runs complete.*

### TPC-B (SF=1, ~200K rows)

| Source engine | ndistinct_w2x | range_covered | null_match | node_jaccard | within_2x | Status |
|:---|---:|---:|---:|---:|---:|:---:|
| PostgreSQL 18   | — | — | — | — | — | ○ |
| CockroachDB v23 | — | — | — | — | — | ○ |
| MySQL 8         | — | — | — | — | — | ○ |
| SQL Server 2022 | — | — | — | — | — | ○ |
| Oracle 21c XE   | — | — | — | — | — | ○ |
| DB2 LUW 11.5    | — | — | — | — | — | ○ |

### TPC-C (SF=1, ~569K rows)

| Source engine | ndistinct_w2x | range_covered | null_match | node_jaccard | within_2x | Status |
|:---|---:|---:|---:|---:|---:|:---:|
| PostgreSQL 18   | — | — | — | — | — | ○ |
| CockroachDB v23 | — | — | — | — | — | ○ |
| MySQL 8         | — | — | — | — | — | ○ |
| SQL Server 2022 | — | — | — | — | — | ○ |
| Oracle 21c XE   | — | — | — | — | — | ○ |
| DB2 LUW 11.5    | — | — | — | — | — | ○ |

### TPC-H (SF=0.1, ~866K rows)

| Source engine | ndistinct_w2x | range_covered | null_match | node_jaccard | within_2x | Status |
|:---|---:|---:|---:|---:|---:|:---:|
| PostgreSQL 18   | — | — | — | — | — | ○ |
| CockroachDB v23 | — | — | — | — | — | ○ |
| MySQL 8         | — | — | — | — | — | ○ |
| SQL Server 2022 | — | — | — | — | — | ○ |
| Oracle 21c XE   | — | — | — | — | — | ○ |
| DB2 LUW 11.5    | — | — | — | — | — | ○ |

### TPC-DI (SF=5, ~697K rows)

| Source engine | ndistinct_w2x | range_covered | null_match | node_jaccard | within_2x | Status |
|:---|---:|---:|---:|---:|---:|:---:|
| PostgreSQL 18   | — | — | — | — | — | ○ |
| CockroachDB v23 | — | — | — | — | — | ○ |
| MySQL 8         | — | — | — | — | — | ○ |
| SQL Server 2022 | — | — | — | — | — | ○ |
| Oracle 21c XE   | — | — | — | — | — | ○ |
| DB2 LUW 11.5    | — | — | — | — | — | ○ |

### TPC-DS (SF=0.01, ~2.18M rows)

| Source engine | ndistinct_w2x | range_covered | null_match | node_jaccard | within_2x | Status |
|:---|---:|---:|---:|---:|---:|:---:|
| PostgreSQL 18   | — | — | — | — | — | ○ |
| CockroachDB v23 | — | — | — | — | — | ○ |
| MySQL 8         | — | — | — | — | — | ○ |
| SQL Server 2022 | — | — | — | — | — | ○ |
| Oracle 21c XE   | — | — | — | — | — | ○ |
| DB2 LUW 11.5    | — | — | — | — | — | ○ |

### TPC-E (SF=0.01, ~856K rows)

| Source engine | ndistinct_w2x | range_covered | null_match | node_jaccard | within_2x | Status |
|:---|---:|---:|---:|---:|---:|:---:|
| PostgreSQL 18   | — | — | — | — | — | ○ |
| CockroachDB v23 | — | — | — | — | — | ○ |
| MySQL 8         | — | — | — | — | — | ○ |
| SQL Server 2022 | — | — | — | — | — | ○ |
| Oracle 21c XE   | — | — | — | — | — | ○ |
| DB2 LUW 11.5    | — | — | — | — | — | ○ |
