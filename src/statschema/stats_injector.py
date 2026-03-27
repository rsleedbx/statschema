"""
stats_injector — inject portable TableStats into a target database's statistics catalog.

This is the "stats transpiler" bridge: statistics collected from any source database
(via collect_table_stats) are injected directly into the target database's optimizer
statistics catalog so the query planner sees production-scale distributions before
a single row of data is loaded.

Supported targets
-----------------
MySQL 8.0+      — ANALYZE TABLE … UPDATE HISTOGRAM ON col USING DATA 'json'
                  + UPDATE mysql.innodb_table_stats SET n_rows=N
PostgreSQL 18+  — pg_restore_relation_stats() + pg_restore_attribute_stats()
Oracle          — DBMS_STATS.SET_TABLE_STATS + DBMS_STATS.SET_COLUMN_STATS
SQL Server      — UPDATE STATISTICS WITH ROWCOUNT + sp_create_stats (partial)
Databricks      — ⚠️  LIMITED VALUE — synthetic sample via build_dataframe_from_canonical
                  + ANALYZE TABLE … COMPUTE STATISTICS FOR COLUMNS.
                  Delta already writes file-level stats on every append; UC managed
                  tables with Predictive Optimization get ANALYZE automatically.
                  This function is only useful for empty tables before first data load.
                  See inject_stats_databricks() docstring and docs/stats_transpiler.md.
IBM Db2 LUW     — UPDATE SYSSTAT.TABLES (CARD, NPAGES) + UPDATE SYSSTAT.COLUMNS
                  (COLCARD, NUMNULLS, AVGCOLLEN) + INSERT SYSSTAT.COLDIST TYPE='F'
                  (MCVs) and TYPE='Q' (histogram quantile bounds).
                  Requires SYSADM/SECADM or CONTROL privilege.

What transfers correctly (summary — see docs/stats_transpiler.md for full details)
----------------------------------------------------------------------------------
PostgreSQL 18  ✅  MCVs, null fractions, n_distinct, histogram bounds (all types)
               ⚠️  Row count scaled by physical file size — empty table = ~1 row estimate
               ⚠️  Extended stats (CREATE STATISTICS) not supported (PG 19 planned)
               ⚠️  PG 18.0/18.1 has index-column bug (fixed in 18.2 Feb 2025)

MySQL 8.0.31+  ✅  MCVs via singleton histogram (≤1024 buckets), null fractions, n_distinct
               ⚠️  Equi-height histogram string equality bug #104109 — string column
                   range histograms fall back to 1/row_count for equality predicates
               ⚠️  Requires MySQL 8.0.31+; earlier MySQL 8.0 silently ignores USING DATA
               ❌  JSON column type not supported for histograms

Oracle         ✅  Row count, null count, n_distinct (distcnt)
               ❌  Histogram bounds — no portable cross-engine format; Oracle uses
                   internal binary RAW encoding that cannot be constructed externally
               ⚠️  Column name case sensitivity — must match Oracle data dictionary exactly

SQL Server     ⚠️  Row count only via UPDATE STATISTICS WITH ROWCOUNT (undocumented API)
               ❌  No column-level injection API exists in SQL Server
               ❌  Auto-update statistics may overwrite injected counts after first load

Databricks     ⚠️  Limited practical value — Delta writes file stats on every append;
                   UC managed tables with Predictive Optimization run ANALYZE automatically.
                   Only useful to bootstrap an empty table before first data load.

IBM Db2 LUW    ✅  Row count (CARD), page count (NPAGES), n_distinct (COLCARD),
                   null count (NUMNULLS), average column length (AVGCOLLEN)
               ✅  MCVs (SYSSTAT.COLDIST TYPE='F') and histogram quantile bounds
                   (SYSSTAT.COLDIST TYPE='Q')
               ⚠️  Requires SYSADM, SECADM, or CONTROL privilege on the table

What does NOT transfer (all engines)
-------------------------------------
❌  Hardware-specific cost thresholds — seq_page_cost, random_page_cost, work_mem
❌  Extended (multi-column) statistics — correlations between columns
❌  This is a bootstrap, not a permanent substitute — always run native ANALYZE
    after loading production data for best plan accuracy.

Primary use cases
-----------------
1.  Same-engine migration / upgrade  (PG→PG, Oracle→Oracle, MySQL→MySQL):
    statistics from the source are 100% compatible with the target.
    PG18 specifically ships pg_dump --statistics-only for this use case.

2.  Cross-engine migration bootstrap  (MySQL→PostgreSQL, Oracle→SQL Server):
    data distributions (MCVs, histograms) describe the data, not the engine.
    The planner sees correct selectivity ratios immediately rather than guessing
    1 row for every filter until ANALYZE finishes.

3.  CI/CD query plan regression testing:
    inject production statistics into a CI database that has no production data
    and verify that EXPLAIN plans match production shape.

References
----------
PostgreSQL 18: https://postgr.es/p/7uw  (boringSQL — production query plans without data)
MySQL 8:       https://dev.mysql.com/doc/refman/8.0/en/analyze-table.html
Oracle:        https://docs.oracle.com/en/database/oracle/oracle-database/21/arpls/DBMS_STATS.html
"""

from __future__ import annotations

import logging

# InjectionResult lives in dialects/_injection_result.py to avoid circular imports
# when dialect injectors are imported below.  Re-export from here for backward compat.
from .dialects._injection_result import InjectionResult

from .dialects.mysql.injector      import inject_stats_mysql, _mysql_b64_str
from .dialects.postgres.injector   import inject_stats_postgres
from .dialects.oracle.injector     import inject_stats_oracle
from .dialects.sqlserver.injector  import inject_stats_sqlserver
from .dialects.databricks.injector import inject_stats_databricks
from .dialects.db2.injector        import inject_stats_db2

logger = logging.getLogger(__name__)

__all__ = [
    "InjectionResult",
    "inject_stats_mysql",
    "inject_stats_postgres",
    "inject_stats_oracle",
    "inject_stats_sqlserver",
    "inject_stats_databricks",
    "inject_stats_db2",
]
