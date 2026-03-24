# Learnings

Research and analysis captured while building and evaluating statschema.

## Documents

### [`query-yaml-research.md`](query-yaml-research.md) — queries.yaml format: prior art and open questions

Prior art survey for the `queries.yaml` workload capture format. Covers workload capture tools (pg_stat_statements, SQL Server Query Store, Oracle AWR), SQL-as-config formats (dbt, LookML, Cube.dev), structured query IRs (Substrait, sqlglot AST, Ibis, Apache Calcite), and the Spark SQL / PySpark angle. Poses six open questions for external review before the format stabilizes.

### [`oltp-migration-analysis.md`](oltp-migration-analysis.md) — Real-world OLTP migration analysis

**The most important document in this directory.** Analyzes five documented migrations (SQL Server → PostgreSQL, MySQL → PostgreSQL, Oracle → PostgreSQL, MySQL → Aurora, SQLite → Neon) drawn from Hacker News, Reddit, AWS blogs, and migration consultants (2023–2026).

Covers four phases where migration teams lose time and statschema's concrete impact on each:

1. **Production data copy blocker** — GDPR/HIPAA compliance review takes 4–12 weeks. statschema eliminates the dependency entirely: only DDL and column statistics (no row values) cross system boundaries.
2. **Schema conversion errors found late** — type mismatches (`MONEY` → `NUMERIC`, `DATETIME` → `TIMESTAMP`, `NVARCHAR` → `VARCHAR`) are visible at DDL parse time, before any data is loaded. Includes a worked `diff` of SQL Server vs PostgreSQL DDL for the Northwind schema.
3. **Optimizer-blind period post-migration** — the target has an empty `pg_statistic` table; autovacuum fills it only after real traffic. `inject_stats_postgres` injects production-representative statistics at cutover so `EXPLAIN` plans are meaningful from day one.
4. **Performance tests at wrong scale** — testing at 10% volume missed index scan vs. hash join crossover in a 400M-row migration. `statschema load --sf 10` generates at any scale factor from one YAML.

Also documents **where statschema does not help**: stored procedure rewriting, network latency, zero-downtime cutover mechanics, ETL pipeline correctness, and ORM query generation differences.

The summary table (bottom of the file) maps every evaluation cycle phase to a statschema command or explicit "no impact."
