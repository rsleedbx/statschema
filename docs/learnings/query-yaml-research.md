# queries.yaml format — prior art research and open questions

**Status: open for review.** This document explains the design rationale for `queries.yaml`, surveys prior art, and poses open questions for other AIs and human reviewers before the format stabilizes.

If you know of a reference implementation, standard, or prior art not covered here, please open an issue or add a note to this file.

---

## Design principle: n sources → 1 canonical format → n targets

The reason `queries.yaml` exists is the same reason `schema.yaml` exists: without a canonical intermediate, testing n source databases against n target databases requires n² test paths. With one canonical format in the middle, it is n + n.

```
n source databases          queries.yaml            n target databases
──────────────────          ────────────            ──────────────────
MySQL                       SQL text                PostgreSQL / Lakebase
SQL Server           →      + source dialect   →    Databricks Lakehouse
Oracle                      + execution stats        DuckDB
PostgreSQL                  (no new syntax          CockroachDB
Databricks                   to learn)              …
```

**The SQL text IS the canonical format.** Users never write `queries.yaml`. `statschema collect --top-queries N` emits it automatically from the source database's own query statistics catalog (`pg_stat_statements`, `performance_schema`, `sys.dm_exec_query_stats`, `v$sql`, `system.query.history`). The SQL in the file is the same SQL the DBA already has in their query store — no new representation to learn.

The transpilation engine (sqlglot or, for Databricks-specific constructs, potentially Ibis internally) is an implementation detail invisible to the user. Whether we use sqlglot or Ibis affects which engine we call inside `query_transpiler.py` — it does not change the YAML format.

---

## The full validation loop

The goal is to take a query from the source database and run it — not just `EXPLAIN` it — on the target database that has a synthetic dataset already loaded:

```
1. statschema collect --top-queries 50
       ↓  queries.yaml  (source SQL + dialect + exec stats)

2. statschema ddl --dialect <target>  +  statschema load
       ↓  target has tables + realistic synthetic data

3. statschema inject --dialect <target>
       ↓  target optimizer has production-representative statistics

4. statschema replay --dialect <target>
       ↓  sqlglot transpiles each query to target dialect
       ↓  EXPLAIN → verify plans are correct
       ↓  EXECUTE (with implicit LIMIT) → verify queries run without error
```

Step 4 closes the migration validation loop: the source workload runs successfully on the target, against synthetic data that has the same statistical shape as production.

**Current implementation gap:** `replay_queries()` runs `EXPLAIN` only. Adding an `--execute` mode (transpile → run query with `LIMIT 100` → return row count and any errors) is the next step.

---

## User-supplied rewrites for non-portable queries

When sqlglot flags `manual_review: true` or produces incorrect SQL for a specific target, the user adds a `rewrites` block to the entry. The transpiler uses the rewrite verbatim instead of calling sqlglot — no code change required.

```yaml
queries:
  - id: "b2c3d4ef1234"
    source_dialect: tsql
    sql: "SELECT STUFF((SELECT ',' + name FROM categories FOR XML PATH('')), 1, 1, '')"
    tables: [categories]
    manual_review: true
    transpile_error: "Contains 'FOR XML' — no portable equivalent; manual rewrite required."
    rewrites:
      postgres:   "SELECT string_agg(name, ',') FROM categories"
      lakebase:   "SELECT string_agg(name, ',') FROM categories"
      databricks: "SELECT array_join(collect_list(name), ',') FROM categories"
```

Resolution order in `transpile_query()`:
1. `rewrites[target_dialect]` — user-supplied, used verbatim. Clears `manual_review` and `transpile_error`.
2. `rewrites[sqlglot_family]` — `lakebase` and `neon` fall back to a `postgres` rewrite if no exact key exists.
3. sqlglot transpilation from `source_dialect` to `target_dialect`.
4. If sqlglot fails or detects a non-portable construct, sets `manual_review: true`.

This is the same override pattern as `schema.yaml` column-level overrides — the YAML file is the place where corrections live, not the code.

---

## What we built

`queries.yaml` stores the top-N queries, tagged with source dialect and execution statistics. The SQL text is the normalized form emitted by the source database's own query catalog — not authored by the user.

```yaml
source_dialect: mysql
queries:
  - id: "a1b2c3d4ef12"       # native query hash: queryid / DIGEST / sql_id
    source_dialect: mysql
    sql: "SELECT customer_id, SUM(total) FROM orders WHERE status = ? GROUP BY customer_id ORDER BY SUM(total) DESC LIMIT 10"
    tables: [orders]
    calls: 42000
    total_elapsed_ms: 8300.5
    mean_elapsed_ms: 0.197
    rank_by: total_time
  - id: "b2c3d4ef1234"
    source_dialect: mysql
    sql: "SELECT * FROM orders FOR XML PATH('order')"
    manual_review: true
    transpile_error: "Contains 'FOR XML' — no portable equivalent; manual rewrite required."
```

`statschema collect --top-queries 50` writes this file.
`statschema replay --dialect postgres` transpiles and runs `EXPLAIN` on the target.
Source: `src/statschema/query_model.py`, `query_collector.py`, `query_transpiler.py`, `query_replayer.py`.

---

## Transpilation engine: sqlglot vs Ibis (internal choice, not a format choice)

Both sqlglot and Ibis are transpilation engines. Neither changes what is stored in `queries.yaml`. The user sees only `statschema replay --dialect <target>`.

| Engine | Input | Output | Relevant for |
|--------|-------|--------|-------------|
| **sqlglot** | SQL string (any dialect) | SQL string (any dialect) | All targets today. Already a dependency. Handles >95% of OLTP source queries. |
| **Ibis** | SQL string or Python expression | SQL string for 20+ backends including Databricks, DuckDB, BigQuery, Snowflake | Databricks Lakehouse — Ibis has a dedicated Databricks backend and generates proper Databricks SQL and PySpark DataFrame API code. |

**For Lakebase (PostgreSQL wire):** sqlglot is sufficient. No reason to add Ibis.

**For Databricks Lakehouse:** sqlglot already handles the common case (`sqlglot.transpile(..., write="databricks")`). Ibis becomes relevant only if the target is a SQL Warehouse and we want to generate PySpark notebook code as a second output alongside the SQL replay. That is a feature addition, not a format change.

**For both targets:** `spark.sql()` and psycopg2 accept plain SQL strings. The transpiled SQL from sqlglot is valid input to both. No additional IR is needed.

---

## Why Ibis should NOT be a field in queries.yaml

An earlier version of this document proposed an optional `ibis_expr` field in `QueryEntry`. That is wrong for two reasons:

1. **Users would have to learn Ibis expression syntax** to read or edit the YAML. The point of storing SQL strings is that every DBA already knows SQL.
2. **The serialization format for Ibis expressions is not stable.** Ibis can emit SQL from Python expressions, but there is no stable YAML/JSON representation of an Ibis expression that round-trips reliably across Ibis versions.

If Ibis is used, it is used internally as a transpilation engine that takes the SQL string from `queries.yaml` and emits target-dialect SQL. The YAML format does not change.

---

## Prior art we found

### Workload capture (closest to our use case)

| Tool / catalog | Format | Dialect-specific | Cross-dialect replay |
|----------------|--------|-----------------|----------------------|
| PostgreSQL `pg_stat_statements` | live DB view, no file format | yes | no |
| MySQL `performance_schema.events_statements_summary_by_digest` | live DB view | yes | no |
| SQL Server Query Store | XML export via SSMS | yes | no |
| Oracle AWR | binary dump, licensed | yes | no |
| Percona `pt-query-digest` | structured text report | MySQL only | no |
| pgBadger | HTML / JSON log analysis | PostgreSQL only | no |

None produce a portable, dialect-tagged YAML for cross-dialect replay. Our format is the first we found combining these properties.

### SQL-as-config / generative formats (different problem — define queries, don't capture them)

| Tool | Format | Relationship to SQL |
|------|--------|---------------------|
| dbt | `schema.yml` + `.sql` files | documents and tests models; SQL is in separate files |
| LookML (Looker) | YAML-like DSL | generates SQL; not a capture format |
| Cube.dev | JS / YAML | generates SQL; not capture |
| Databricks Asset Bundles | YAML | job/pipeline config; SQL is an unstructured string value |

### Structured query IRs (machine-readable — not designed for human editing)

| Project | Format | Why we did not use it |
|---------|--------|-----------------------|
| **Substrait** ([substrait.io](https://substrait.io)) | Protocol Buffers | Not human-editable. OLTP databases (MySQL, Oracle, SQL Server) have no Substrait collectors. Requires JVM (spark-substrait) for Spark. Would not simplify `queries.yaml` for users. |
| sqlglot AST | Python objects, JSON-serializable | Verbose, not human-readable. Defeats the "edit between runs" goal. |
| Apache Calcite `RelNode` | Java objects, XML | JVM-only. Not portable outside Calcite ecosystem. |
| `pg_query` JSON | PostgreSQL parse tree | PostgreSQL-specific parse semantics. Not cross-dialect. |
| OpenTelemetry `db.statement` | trace span attribute | Captures SQL text + telemetry but no dialect tag, no replay intent. |

---

## Open questions for reviewers

1. **sqlglot coverage for OLTP → Databricks:** For the common case of OLTP source queries (MySQL, SQL Server, Oracle) transpiled to Databricks SQL dialect, what constructs does sqlglot miss or mistranslate? Specifically: `DECODE`, `NVL`, `STUFF`, `PIVOT`, Oracle date arithmetic (`SYSDATE - 1`), and SQL Server `ISNULL` / `DATEDIFF` with non-standard units.

2. **execute vs EXPLAIN:** When `statschema replay` runs a query against the synthetic dataset (not just `EXPLAIN` but actually executes with `LIMIT 100`), what result validation is useful? Row count? Column names? First-row type check? Or just "no error"?

3. **Parameterized queries:** `pg_stat_statements` normalizes literals to `$1`, `$2` placeholders. MySQL `DIGEST_TEXT` uses `?`. Oracle `v$sql` retains literals. When executing (not just EXPLAIN-ing) on the target, how should placeholder parameters be substituted? Heuristic (substitute type-appropriate defaults), skip execution for parameterized queries, or require the user to supply a parameters file?

4. **Missed prior art:** Is there a tool, standard, or project that combines (dialect-tagged SQL + execution statistics + cross-dialect replay intent) that we have not found? Specifically in the Databricks ecosystem, Apache ecosystem, or PostgreSQL tooling.

5. **OLTP vs OLAP boundary:** Our format is designed for OLTP migrations (MySQL / SQL Server / Oracle → PostgreSQL / Lakebase / Databricks). For OLAP migrations (Hive → Databricks, Teradata → Snowflake) Substrait is more mature. Should we document this scope boundary explicitly, or extend the format to cover OLAP sources?

---

## Design decisions

| Decision | Rationale | Revisit if |
|----------|-----------|------------|
| Store SQL strings, not AST or Substrait | Human-readable; DBAs can inspect and fix queries directly; no new syntax to learn | A stable, human-editable cross-dialect query representation emerges |
| sqlglot for transpilation | Already a dependency; handles >95% of OLTP constructs; flags the rest as `manual_review` | A gap in sqlglot's Databricks coverage is discovered; then evaluate Ibis as an internal engine replacement |
| Source dialect tag per entry | Dialect is known at collection time from the source DB type | N/A |
| `id` from native query hash | Stable across collection runs; matches `queryid` / `sql_id` / `DIGEST` in native tools | N/A |
| `manual_review: true` flag | Non-portable constructs need human attention; surface them rather than silently produce wrong SQL | N/A |
| `rank_by: total_time` default | Ranks by business impact (cumulative cost); `--rank-by calls` and `--rank-by mean_time` available | N/A |
| EXPLAIN only (current) | Safe starting point; no risk of modifying data | Add `--execute` mode with implicit `LIMIT 100` once EXPLAIN is validated |

---

## References

- Substrait specification: [substrait.io](https://substrait.io)
- Ibis project: [ibis-project.org](https://ibis-project.org)
- sqlglot: [github.com/tobymao/sqlglot](https://github.com/tobymao/sqlglot)
- pg_stat_statements: [postgresql.org/docs/current/pgstatstatements.html](https://www.postgresql.org/docs/current/pgstatstatements.html)
- SQL Server Query Store: [learn.microsoft.com/en-us/sql/relational-databases/performance/query-store-overview](https://learn.microsoft.com/en-us/sql/relational-databases/performance/query-store-overview)
- Oracle AWR: [docs.oracle.com/en/database/oracle/oracle-database/21/tgdba/automatic-workload-repository-awr.html](https://docs.oracle.com/en/database/oracle/oracle-database/21/tgdba/automatic-workload-repository-awr.html)
- Databricks `system.query.history`: [docs.databricks.com/en/administration-guide/system-tables/query-history.html](https://docs.databricks.com/en/administration-guide/system-tables/query-history.html)
