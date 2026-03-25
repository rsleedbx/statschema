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

import base64
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .stats_model import ColumnStats, TableStats

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class InjectionResult:
    dialect: str
    table: str
    rows_injected: int           # table-level row count injected
    columns_injected: int        # number of columns whose stats were injected
    columns_skipped: int         # columns not injected (unsupported type, no stats)
    warnings: list[str]

    @property
    def success(self) -> bool:
        return self.columns_injected > 0


# ---------------------------------------------------------------------------
# MySQL 8.0+  (ANALYZE TABLE … USING DATA + innodb_table_stats n_rows)
# ---------------------------------------------------------------------------

def inject_stats_mysql(  # pragma: no cover
    conn: Any,
    table_stats: TableStats,
    database: str | None = None,
) -> InjectionResult:
    """
    Inject TableStats into a MySQL 8.0+ database.

    Two mechanisms are used:
    1.  **Column histogram injection** via
        ``ANALYZE TABLE … UPDATE HISTOGRAM ON col USING DATA 'json'``
        This writes directly to ``information_schema.COLUMN_STATISTICS`` and
        reshapes the optimizer's selectivity estimates for filtered queries.

    2.  **InnoDB table-level row count** via
        ``UPDATE mysql.innodb_table_stats SET n_rows = … FLUSH TABLE``
        This sets the row count estimate used for full-table cost calculations.

    MySQL 8.0 histogram format
    --------------------------
    - ``singleton`` histogram (low cardinality / MCV-driven):
      buckets are ``[value, cumulative_frequency]`` where string values are
      encoded as ``"base64:type254:<BASE64>"``.
    - ``equi-height`` histogram (continuous / range columns):
      buckets are ``[lower, upper, cumulative_freq, num_distinct]``.

    Parameters
    ----------
    conn        pymysql (or mysql-connector-python) connection.
    table_stats TableStats collected from any source dialect.
    database    Target database/schema name.  Defaults to the connection's
                current database.
    """
    cur = conn.cursor()

    if database is None:
        cur.execute("SELECT DATABASE()")
        database = cur.fetchone()[0]

    table     = table_stats.name
    row_count = table_stats.row_count or 0
    warnings: list[str] = []

    # ── 1. InnoDB table-level row count ──────────────────────────────────
    # Only works when InnoDB persistent stats are enabled (innodb_stats_persistent=ON,
    # which is the default in MySQL 8.0).
    try:
        cur.execute(
            "UPDATE mysql.innodb_table_stats "
            "SET n_rows = %s "
            "WHERE database_name = %s AND table_name = %s",
            (row_count, database, table),
        )
        if cur.rowcount == 0:
            # The table may not have an entry yet — insert it first by running ANALYZE
            cur.execute(f"ANALYZE TABLE `{database}`.`{table}`")
            cur.execute(
                "UPDATE mysql.innodb_table_stats "
                "SET n_rows = %s "
                "WHERE database_name = %s AND table_name = %s",
                (row_count, database, table),
            )
        # Reload the stats into the optimizer
        cur.execute(f"FLUSH TABLE `{database}`.`{table}`")
        logger.info("MySQL InnoDB n_rows injected: %s.%s rows=%d", database, table, row_count)
    except Exception as exc:
        warnings.append(f"InnoDB table stats: {exc}")

    # ── 2. Column histogram injection ────────────────────────────────────
    cols_ok = cols_skip = 0
    for col in table_stats.columns:
        try:
            _inject_mysql_column(conn, database, table, col, row_count, warnings)
            cols_ok += 1
        except Exception as exc:
            warnings.append(f"{col.name}: {exc}")
            cols_skip += 1

    return InjectionResult(
        dialect="mysql",
        table=table,
        rows_injected=row_count,
        columns_injected=cols_ok,
        columns_skipped=cols_skip,
        warnings=warnings,
    )


def _mysql_b64_str(value: str) -> str:
    """Encode a string value in MySQL's histogram singleton format."""
    return f"base64:type254:{base64.b64encode(value.encode('utf-8')).decode()}"


def _inject_mysql_column(  # pragma: no cover
    conn: Any,
    database: str,
    table: str,
    col: ColumnStats,
    row_count: int,
    warnings: list[str],
) -> None:
    """
    Inject one column's statistics via ANALYZE TABLE … UPDATE HISTOGRAM … USING DATA.

    Strategy:
    - If the column has MCVs covering ≥ 5% of rows → build a singleton histogram.
    - If the column has histogram bounds (numeric/date range) → build equi-height.
    - Otherwise skip (no useful distribution info to inject).
    """
    cur = conn.cursor()
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")

    histogram: dict | None = None

    # ── Singleton histogram from MCVs ────────────────────────────────────
    if col.most_common_values and len(col.most_common_values) >= 2:
        vals  = [(v.value, v.frequency) for v in col.most_common_values]
        # Sort by value for binary-search optimisation (MySQL requirement)
        vals.sort(key=lambda x: str(x[0]))

        # Build cumulative frequencies
        cum = 0.0
        buckets: list[Any] = []
        for val, freq in vals[:-1]:
            cum += freq
            buckets.append([_mysql_b64_str(str(val)), round(cum, 6)])
        # Last bucket must reach 1.0
        buckets.append([_mysql_b64_str(str(vals[-1][0])), 1.0])

        histogram = {
            "buckets":                   buckets,
            "data-type":                 "string",
            "auto-update":               False,
            "null-values":               float(col.null_fraction or 0.0),
            "collation-id":              255,
            "last-updated":              now,
            "sampling-rate":             1.0,
            "histogram-type":            "singleton",
            "number-of-buckets-specified": len(buckets),
        }

    # ── Equi-height histogram from histogram_bounds (numeric columns) ────
    elif col.histogram_bounds and len(col.histogram_bounds) >= 3:
        bounds = col.histogram_bounds
        num_buckets = len(bounds) - 1
        if num_buckets < 1:
            return
        bucket_freq = 1.0 / num_buckets
        buckets = []
        for i in range(num_buckets):
            cum = round((i + 1) * bucket_freq, 6)
            try:
                lo = float(bounds[i])
                hi = float(bounds[i + 1])
            except (ValueError, TypeError):
                lo = str(bounds[i])
                hi = str(bounds[i + 1])
            # equi-height bucket: [lo, hi, cumulative_freq, num_distinct_in_bucket]
            buckets.append([lo, hi, min(cum, 1.0), 1])

        histogram = {
            "buckets":                   buckets,
            "data-type":                 "double",
            "auto-update":               False,
            "null-values":               float(col.null_fraction or 0.0),
            "collation-id":              8,
            "last-updated":              now,
            "sampling-rate":             1.0,
            "histogram-type":            "equi-height",
            "number-of-buckets-specified": len(buckets),
        }

    if histogram is None:
        return  # nothing useful to inject for this column

    histogram_str = json.dumps(histogram)
    sql = (
        f"ANALYZE TABLE `{database}`.`{table}` "
        f"UPDATE HISTOGRAM ON `{col.name}` "
        f"USING DATA '{histogram_str}'"
    )
    cur.execute(sql)
    row = cur.fetchone()
    if row and len(row) >= 4 and "Error" in str(row[3]):
        raise RuntimeError(f"MySQL histogram injection failed: {row[3]}")

    logger.debug("MySQL column histogram injected: %s.%s.%s", database, table, col.name)


# ---------------------------------------------------------------------------
# PostgreSQL 18  (pg_restore_relation_stats + pg_restore_attribute_stats)
# ---------------------------------------------------------------------------

def inject_stats_postgres(  # pragma: no cover
    conn: Any,
    table_stats: TableStats,
    schema: str = "public",
) -> InjectionResult:
    """
    Inject TableStats into a PostgreSQL 18+ database using
    pg_restore_relation_stats() and pg_restore_attribute_stats().

    The target table must already exist (CREATE TABLE must have been run).
    After injection, EXPLAIN plans will reflect the injected distributions.

    Parameters
    ----------
    conn        psycopg2 connection to the PostgreSQL 18+ target database.
    table_stats TableStats collected from any source dialect.
    schema      Target schema name (default: "public").

    Raises
    ------
    RuntimeError if pg_restore_attribute_stats is not available (PG < 18).
    """
    cur = conn.cursor()

    # Verify PG18+ API is available
    cur.execute(
        "SELECT COUNT(*) FROM pg_proc WHERE proname = 'pg_restore_attribute_stats'"
    )
    if cur.fetchone()[0] == 0:
        raise RuntimeError(
            "pg_restore_attribute_stats not found. "
            "PostgreSQL 18+ is required for stats injection."
        )

    table = table_stats.name
    row_count = table_stats.row_count or 0
    warnings: list[str] = []

    # ── 1. Table-level statistics ─────────────────────────────────────────
    # Use the actual physical page count from pg_class.relpages when available.
    # If we inject a page count that differs from the physical heap size, the
    # planner applies a correction: estimated_rows = reltuples × (heap_pages /
    # relpages).  A wrong relpages (e.g. estimated from avg_row_bytes) therefore
    # multiplies all row estimates by a constant and makes every join and
    # aggregate estimate diverge from the source plan.
    # Query the actual physical size of the heap using pg_relation_size.
    # pg_class.relpages is 0 on a newly loaded table (updated only by ANALYZE /
    # VACUUM), but the planner computes estimated_rows = reltuples × (heap_pages
    # / relpages) using the live block count from the buffer manager.  Injecting
    # relpages = physical_pages therefore sets the correction factor to 1 and
    # keeps row estimates consistent with the injected reltuples.
    try:
        cur.execute(
            """
            SELECT GREATEST(1, pg_relation_size(
                (quote_ident(%s) || '.' || quote_ident(%s))::regclass
            ) / current_setting('block_size')::integer)
            """,
            (schema, table),
        )
        size_row = cur.fetchone()
        pages = int(size_row[0]) if size_row and size_row[0] else None
    except Exception:
        pages = None

    if not pages:
        avg_row_bytes = table_stats.avg_row_bytes or 44
        pages = max(1, int(row_count * avg_row_bytes / 8192))

    cur.execute(
        """
        SELECT pg_restore_relation_stats(
            'schemaname', %s,
            'relname',    %s,
            'relpages',   %s::integer,
            'reltuples',  %s::real
        )
        """,
        (schema, table, pages, float(row_count)),
    )
    conn.commit()
    logger.info("PG18 table stats injected: %s.%s rows=%d pages=%d", schema, table, row_count, pages)

    # ── 2. Column-level statistics ────────────────────────────────────────
    # Use a savepoint per column so that a single column failure does not
    # abort the entire transaction and roll back all successfully injected
    # column statistics.  Without this, psycopg2 enters InFailedSqlTransaction
    # after any error, and the final conn.commit() silently becomes ROLLBACK.
    cols_ok = cols_skip = 0

    for col in table_stats.columns:
        try:
            cur.execute("SAVEPOINT _inj_col")
            _inject_pg_column(conn, schema, table, col, row_count, warnings)
            cur.execute("RELEASE SAVEPOINT _inj_col")
            cols_ok += 1
        except Exception as exc:
            try:
                cur.execute("ROLLBACK TO SAVEPOINT _inj_col")
            except Exception:
                pass
            warnings.append(f"{col.name}: {exc}")
            cols_skip += 1

    conn.commit()
    return InjectionResult(
        dialect="postgresql",
        table=table,
        rows_injected=row_count,
        columns_injected=cols_ok,
        columns_skipped=cols_skip,
        warnings=warnings,
    )


def _inject_pg_column(  # pragma: no cover
    conn: Any,
    schema: str,
    table: str,
    col: ColumnStats,
    row_count: int,
    warnings: list[str],
) -> None:
    """Inject one column's statistics via pg_restore_attribute_stats."""
    cur = conn.cursor()

    # n_distinct: positive = absolute count, negative = fraction of rows
    n_distinct: float
    if col.n_distinct is not None:
        if row_count > 0 and col.n_distinct < row_count * 0.9:
            n_distinct = col.n_distinct  # absolute
        else:
            n_distinct = -1.0  # unique / near-unique
    else:
        n_distinct = -1.0

    null_frac = float(col.null_fraction or 0.0)
    avg_width  = int(col.avg_width_bytes or 8)

    kwargs: dict[str, Any] = {
        "schemaname": schema,
        "relname":    table,
        "attname":    col.name,
        "inherited":  False,
        "null_frac":  null_frac,
        "avg_width":  avg_width,
        "n_distinct": n_distinct,
    }

    # Most-common values — format expected by pg_restore_attribute_stats:
    #   most_common_vals  → text  (array literal as a string: '{val1,val2}')
    #   most_common_freqs → real[] (passed as Python list so psycopg2 binds real[])
    if col.most_common_values and len(col.most_common_values) >= 2:
        vals  = [str(v.value) for v in col.most_common_values]
        freqs = [float(v.frequency) for v in col.most_common_values]
        # Only inject MCVs when they cover meaningful fraction
        if sum(freqs) >= 0.05:
            # PG18 expects most_common_vals as TEXT (array literal), NOT text[]
            kwargs["most_common_vals"]  = "{" + ",".join(
                '"' + v.replace('"', '\\"') + '"' if ',' in v or ' ' in v else v
                for v in vals
            ) + "}"
            # PG18 expects most_common_freqs as real[] — pass Python list
            kwargs["most_common_freqs"] = freqs

    # Histogram bounds — TEXT literal (array of text)
    if col.histogram_bounds and len(col.histogram_bounds) >= 3:
        if "most_common_freqs" not in kwargs or sum(freqs) < 0.95:
            kwargs["histogram_bounds"] = (
                "{" + ",".join(str(b) for b in col.histogram_bounds) + "}"
            )

    # Build the variadic call.
    # pg_restore_attribute_stats is strict about types: it silently ignores values
    # of the wrong type rather than raising an error.  Python floats become float8
    # in psycopg2, but PG expects real (float4) for null_frac/n_distinct.  We must
    # emit explicit PostgreSQL type casts for every numeric parameter.
    _PG_CASTS: dict[str, str] = {
        "null_frac":        "::real",
        "avg_width":        "::integer",
        "n_distinct":       "::real",
        "most_common_vals": "::text",       # text array literal
        "histogram_bounds": "::text",       # text array literal
        "correlation":      "::real",
    }

    parts: list[str] = []
    params: list[Any] = []

    for k, v in kwargs.items():
        if k == "most_common_freqs" and isinstance(v, list):
            # Pass as a text literal with explicit ::real[] cast so PG accepts it
            freq_literal = "{" + ",".join(str(f) for f in v) + "}"
            parts.append("%s, %s::real[]")
            params.extend([k, freq_literal])
        else:
            cast = _PG_CASTS.get(k, "")
            parts.append(f"%s, %s{cast}")
            params.extend([k, v])

    sql = f"SELECT pg_restore_attribute_stats({', '.join(parts)})"
    cur.execute(sql, params)

    logger.debug("PG18 column stats injected: %s.%s.%s", schema, table, col.name)


# ---------------------------------------------------------------------------
# Oracle  (DBMS_STATS.SET_TABLE_STATS + SET_COLUMN_STATS)
# ---------------------------------------------------------------------------

def inject_stats_oracle(  # pragma: no cover
    conn: Any,
    table_stats: TableStats,
    schema: str | None = None,
) -> InjectionResult:
    """
    Inject TableStats into an Oracle database using DBMS_STATS procedures.

    Parameters
    ----------
    conn        oracledb connection to the Oracle target database.
    table_stats TableStats collected from any source dialect.
    schema      Oracle schema/owner (uppercase). Defaults to the connected user.
    """
    cur = conn.cursor()

    if schema is None:
        cur.execute("SELECT USER FROM DUAL")
        schema = cur.fetchone()[0]

    # Oracle table name: unquoted names are stored uppercase; emit_ddl uses quoted
    # uppercase ("XFER_ORDERS") → stored as XFER_ORDERS in data dictionary.
    table = table_stats.name.upper()
    row_count = table_stats.row_count or 0
    avg_row   = table_stats.avg_row_bytes or 44
    blocks    = max(1, int(row_count * avg_row / 8192))
    warnings: list[str] = []

    # ── 1. Table-level statistics ─────────────────────────────────────────
    cur.execute(
        """
        BEGIN
            DBMS_STATS.SET_TABLE_STATS(
                ownname  => :owner,
                tabname  => :tab,
                numrows  => :rows,
                numblks  => :blks,
                avgrlen  => :rlen,
                no_invalidate => FALSE
            );
        END;
        """,
        {"owner": schema, "tab": table, "rows": row_count, "blks": blocks, "rlen": avg_row},
    )
    conn.commit()
    logger.info("Oracle table stats injected: %s.%s rows=%d", schema, table, row_count)

    # ── 2. Column-level statistics ────────────────────────────────────────
    cols_ok = cols_skip = 0
    for col in table_stats.columns:
        try:
            _inject_oracle_column(conn, schema, table, col, row_count, warnings)
            cols_ok += 1
        except Exception as exc:
            warnings.append(f"{col.name}: {exc}")
            cols_skip += 1

    conn.commit()
    return InjectionResult(
        dialect="oracle",
        table=table,
        rows_injected=row_count,
        columns_injected=cols_ok,
        columns_skipped=cols_skip,
        warnings=warnings,
    )


def _inject_oracle_column(  # pragma: no cover
    conn: Any,
    schema: str,
    table: str,
    col: ColumnStats,
    row_count: int,
    warnings: list[str],
) -> None:
    """Inject one column's statistics via DBMS_STATS.SET_COLUMN_STATS."""
    cur = conn.cursor()

    n_distinct = int(col.n_distinct or 1)
    null_count = int((col.null_fraction or 0.0) * row_count)
    avg_width   = int(col.avg_width_bytes or 8)

    # Oracle SET_COLUMN_STATS accepts distcnt, nullcnt, avgclen
    # Oracle column name resolution for DBMS_STATS:
    #   - Unquoted/uppercase names (standard Oracle):  pass as-is uppercase
    #   - Quoted lowercase/mixed-case names:           wrap in double quotes
    raw = col.name
    if raw == raw.upper():
        col_name = raw          # standard unquoted uppercase
    else:
        col_name = f'"{raw}"'   # quoted identifier — preserve case with ""

    cur.execute(
        """
        BEGIN
            DBMS_STATS.SET_COLUMN_STATS(
                ownname  => :owner,
                tabname  => :tab,
                colname  => :col,
                distcnt  => :ndist,
                nullcnt  => :nulls,
                avgclen  => :avglen,
                no_invalidate => FALSE
            );
        END;
        """,
        {
            "owner":  schema,
            "tab":    table,
            "col":    col_name,
            "ndist":  n_distinct,
            "nulls":  null_count,
            "avglen": avg_width,
        },
    )
    logger.debug("Oracle column stats injected: %s.%s.%s dist=%d", schema, table, col.name, n_distinct)


# ---------------------------------------------------------------------------
# SQL Server  (UPDATE STATISTICS WITH ROWCOUNT + manual histogram via sp)
# ---------------------------------------------------------------------------

def inject_stats_sqlserver(  # pragma: no cover
    conn: Any,
    table_stats: TableStats,
    schema: str = "dbo",
) -> InjectionResult:
    """
    Inject table-level row count statistics into SQL Server.

    SQL Server does not expose a public column-level statistics injection API
    (unlike PostgreSQL 18 and Oracle DBMS_STATS).  This function uses
    ``UPDATE STATISTICS … WITH ROWCOUNT, PAGECOUNT`` which updates the
    table-level row/page estimates that the optimizer uses for cardinality
    estimates and join strategy decisions.

    For column-level MCVs and histograms, a workaround is to load a small
    representative sample (using ``build_dataframe_from_canonical`` with
    ``rows=10000``) and then run ``UPDATE STATISTICS`` normally.  That path
    is covered by the stats-driven data generation pipeline.

    Parameters
    ----------
    conn        pymssql connection to the SQL Server target database.
    table_stats TableStats collected from any source dialect.
    schema      Target schema (default: "dbo").
    """
    cur = conn.cursor()

    table     = table_stats.name
    row_count = table_stats.row_count or 0
    avg_row   = table_stats.avg_row_bytes or 44
    pages     = max(1, int(row_count * avg_row / 8192))
    warnings: list[str] = []
    warnings.append(
        "SQL Server does not support column-level stats injection without data. "
        "Table-level ROWCOUNT/PAGECOUNT injected only. "
        "For column MCVs load a representative sample and run UPDATE STATISTICS."
    )

    full_table = f"[{schema}].[{table}]"

    # Check if any statistics object exists; if not, create one first
    cur.execute(
        f"SELECT COUNT(*) FROM sys.stats WHERE object_id = OBJECT_ID('{full_table}')"
    )
    stat_count = cur.fetchone()[0]

    if stat_count == 0:
        # No stats object exists yet — SQL Server needs at least one index or
        # auto-created stat.  We can trigger auto-creation via a dummy query.
        try:
            cur.execute(f"SELECT TOP 1 * FROM {full_table}")
        except Exception:
            pass

    # Inject row count and page count
    try:
        cur.execute(
            f"UPDATE STATISTICS {full_table} WITH ROWCOUNT={row_count}, PAGECOUNT={pages}"
        )
        conn.autocommit(True)
        logger.info("SQL Server table stats injected: %s rows=%d", full_table, row_count)
        cols_ok = 0
    except Exception as exc:
        warnings.append(f"UPDATE STATISTICS failed: {exc}")
        cols_ok = 0

    return InjectionResult(
        dialect="sqlserver",
        table=table,
        rows_injected=row_count,
        columns_injected=cols_ok,
        columns_skipped=len(table_stats.columns),
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Databricks / Delta Lake  (synthetic sample + ANALYZE TABLE)
# ---------------------------------------------------------------------------

def inject_stats_databricks(  # pragma: no cover
    spark: Any,
    table_stats: TableStats,
    canonical_schema: Any,           # CanonicalTableSchema
    table_name: str | None = None,
    database: str = "default",
    sample_rows: int = 10_000,
    analyze_columns: list[str] | None = None,
    overwrite: bool = True,
    table_format: str = "delta",
) -> InjectionResult:
    """
    Bootstrap Databricks / Delta Lake optimizer statistics by writing a
    statistically-representative synthetic sample and running ANALYZE TABLE.

    Databricks Delta Lake does not expose a catalog-level statistics injection
    API (unlike PostgreSQL 18's pg_restore_attribute_stats or Oracle DBMS_STATS).
    The optimizer collects cardinality / MCV / histogram data by scanning actual
    Delta files during ANALYZE TABLE … COMPUTE STATISTICS FOR COLUMNS.

    This function bridges that gap via a two-step process:

    1.  Generate ``sample_rows`` synthetic rows from ``table_stats`` using
        ``build_dataframe_from_canonical``.  Because MCVs, null fractions, and
        numeric ranges are driven by the collected statistics, the distribution
        matches the source database even though the rows are synthetic.

    2.  Write the sample to ``{database}.{table_name}`` (Delta format) and run
        ``ANALYZE TABLE … COMPUTE STATISTICS FOR COLUMNS``.  The Photon / Spark
        optimizer then sees the correct distribution for the *sample* data, which
        approximates the production distribution.

    Practical value — read before use
    ----------------------------------
    This function has **limited practical benefit** for most Databricks deployments:

    * Delta already writes file-level min/max/null_count stats on every append,
      so data skipping works without any manual call.  ``COMPUTE DELTA STATISTICS``
      only back-fills stats on old files — it adds nothing after a normal ingest.

    * Unity Catalog managed tables with Predictive Optimization enabled get
      ``ANALYZE TABLE`` run automatically by Databricks.  Calling this function
      is redundant in that configuration.

    * For external / non-managed tables without Predictive Optimization, running
      ``ANALYZE TABLE … COMPUTE STATISTICS FOR COLUMNS`` **after loading production
      data** yields far more accurate stats than a synthetic sample.

    The only scenario where this function provides genuine value is when you need
    the query optimizer to produce reasonable plans on a **completely empty table**
    before any production data is loaded (e.g. for query plan regression testing in
    CI).  Even then, treat the injected stats as a temporary bootstrap that will be
    replaced by a real ``ANALYZE`` post-load.

    Unlike PostgreSQL 18 (``pg_restore_attribute_stats``) and Oracle
    (``DBMS_STATS.SET_COLUMN_STATS``), Databricks has no catalog-level stats
    injection API.  This function works around that limitation by writing a
    synthetic sample and running ``ANALYZE`` on it — with all the approximation
    trade-offs that implies.

    Other limitations
    -----------------
    - Row count seen by the optimizer reflects ``sample_rows`` (default 10 000),
      not the production row count.
    - Column correlation (multi-column statistics) is not captured.
    - Injected statistics are replaced when production data is loaded; always
      re-run ``ANALYZE TABLE … COMPUTE STATISTICS`` after the full data load.
    - Requires a SparkSession with Delta Lake support (Databricks Runtime or
      open-source delta-spark >= 3.0 configured via SparkSession extensions).
    - Also works with local PySpark + delta-spark for CI testing
      (use ``table_format="parquet"`` locally — see parameter docs).

    Parameters
    ----------
    spark            Active SparkSession (Databricks or local).
    table_stats      TableStats collected from any source dialect.
    canonical_schema CanonicalTableSchema for the target table (for data generation).
    table_name       Override target table name; defaults to ``table_stats.name``.
    database         Target database / schema (default: "default").
    sample_rows      Number of synthetic rows to generate (default: 10 000).
    analyze_columns  Subset of column names to analyze; defaults to all columns.
    overwrite        If True (default) the sample replaces any existing table data.
    table_format     Underlying table format (default: "delta").  On real Databricks
                     Runtime always use "delta".  Use "parquet" for local PySpark
                     testing — parquet-backed Hive tables support ANALYZE TABLE while
                     delta-spark 4.x with DeltaCatalog does not support it locally.

    Returns
    -------
    InjectionResult with ``rows_injected=sample_rows`` and any warnings.
    """
    from .dbldatagen_builder import build_dataframe_from_canonical

    tgt_table = table_name or table_stats.name
    full_ref   = f"{database}.{tgt_table}"

    # 1. Generate synthetic sample
    df = build_dataframe_from_canonical(
        spark,
        canonical_schema,
        rows=sample_rows,
        stats=table_stats,
    )

    # 2. Write to table
    write_mode = "overwrite" if overwrite else "append"
    if table_format.lower() == "delta":
        # PySpark 4.x + delta-spark 4.x requires writing to a path first;
        # saveAsTable(format=delta) raises EXTERNAL_METADATA_UNSUPPORTED.
        # Write to a temp path and CREATE TABLE … USING DELTA LOCATION.
        import tempfile, os as _os
        delta_dir = _os.path.join(tempfile.gettempdir(), "statschema_delta", tgt_table)
        (
            df.write
            .format("delta")
            .mode(write_mode)
            .save(delta_dir)
        )
        logger.info("Wrote %d synthetic rows to Delta path %s", sample_rows, delta_dir)
        spark.sql(f"DROP TABLE IF EXISTS {full_ref}")
        spark.sql(f"CREATE TABLE {full_ref} USING DELTA LOCATION '{delta_dir}'")
        logger.info("Registered Delta table %s → %s", full_ref, delta_dir)
    else:
        # Parquet / other format: standard saveAsTable creates a V1 Hive managed table
        # that supports ANALYZE TABLE COMPUTE STATISTICS.
        spark.sql(f"DROP TABLE IF EXISTS {full_ref}")
        (
            df.write
            .format(table_format)
            .mode(write_mode)
            .saveAsTable(full_ref)
        )
        logger.info("Wrote %d synthetic rows to %s table %s", sample_rows, table_format, full_ref)

    # 3. ANALYZE TABLE … COMPUTE STATISTICS FOR COLUMNS
    col_names = analyze_columns or [c.name for c in (table_stats.columns or [])]
    if col_names:
        quoted_cols = ", ".join(f"`{c}`" for c in col_names)
        analyze_sql = f"ANALYZE TABLE {full_ref} COMPUTE STATISTICS FOR COLUMNS {quoted_cols}"
    else:
        analyze_sql = f"ANALYZE TABLE {full_ref} COMPUTE STATISTICS"
    spark.sql(analyze_sql)
    logger.info("ANALYZE TABLE completed for %s", full_ref)

    # 4. COMPUTE DELTA STATISTICS for data-skipping (DBR 14.3+)
    delta_stats_sql = f"ANALYZE TABLE {full_ref} COMPUTE DELTA STATISTICS"
    delta_stats_ok = False
    try:
        spark.sql(delta_stats_sql)
        logger.info("COMPUTE DELTA STATISTICS completed for %s", full_ref)
        delta_stats_ok = True
    except Exception as exc:
        logger.debug("COMPUTE DELTA STATISTICS not available (requires DBR 14.3+): %s", exc)

    warnings: list[str] = [
        f"Row count seen by optimizer = {sample_rows} (synthetic sample), not production row count. "
        f"Scale sample_rows up or re-run ANALYZE after loading production data.",
    ]
    if not delta_stats_ok:
        warnings.append(
            "ANALYZE TABLE … COMPUTE DELTA STATISTICS is not available on this runtime "
            "(requires Databricks Runtime 14.3+).  Delta data-skipping statistics were not set."
        )

    return InjectionResult(
        dialect="databricks",
        table=tgt_table,
        rows_injected=sample_rows,
        columns_injected=len(col_names),
        columns_skipped=0,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# IBM Db2 LUW  (UPDATE SYSSTAT.TABLES + SYSSTAT.COLUMNS)
# ---------------------------------------------------------------------------

def inject_stats_db2(  # pragma: no cover
    conn: Any,
    table_stats: TableStats,
    schema: str | None = None,
) -> InjectionResult:
    """
    Inject TableStats into IBM Db2 LUW via the SYSSTAT updateable catalog views.

    Db2 exposes ``SYSSTAT.TABLES`` and ``SYSSTAT.COLUMNS`` as updateable views
    over the SYSCAT catalog.  Updating them directly sets the statistics that
    the Db2 query optimizer reads without requiring RUNSTATS to be run against
    real data.

    What transfers
    --------------
    ✅  Table-level row count (CARD) and estimated page count (NPAGES, FPAGES)
    ✅  Per-column distinct value count (COLCARD)
    ✅  Per-column NULL count (NUMNULLS)
    ✅  Per-column average value length (AVGCOLLEN)
    ✅  Most common values — TYPE='F' rows in SYSSTAT.COLDIST (SEQNO ranked by
        frequency; VALCOUNT = estimated occurrence count)
    ✅  Histogram quantile bounds — TYPE='Q' rows in SYSSTAT.COLDIST (SEQNO =
        bucket index; VALCOUNT/DISTCOUNT approximated from histogram_bounds)

    Requirements
    ------------
    - The table must already exist in the target Db2 database (catalog rows
      are created at CREATE TABLE time).
    - The connected user must hold SYSADM or SECADM authority, or CONTROL
      privilege on the table, to update SYSSTAT views.
    - Uses ``ibm_db_dbi``-style connections (``?`` parameter markers).

    Parameters
    ----------
    conn        ibm_db_dbi connection to the Db2 LUW target database.
    table_stats TableStats collected from any source dialect.
    schema      Db2 schema name (uppercase). Defaults to ``CURRENT SCHEMA``.
    """
    cur = conn.cursor()

    if schema is None:
        cur.execute("VALUES CURRENT SCHEMA")
        row = cur.fetchone()
        schema = (row[0] if row else "").strip().upper()

    table     = table_stats.name.upper()
    row_count = table_stats.row_count or 0
    avg_row   = table_stats.avg_row_bytes or 44
    # Db2 default page size is 4 KB (4096 bytes); use that for NPAGES estimate.
    npages    = max(1, int(row_count * avg_row / 4096))
    warnings: list[str] = []

    # ── 1. Table-level statistics ──────────────────────────────────────────
    cur.execute(
        "UPDATE SYSSTAT.TABLES SET CARD = ?, NPAGES = ?, FPAGES = ? "
        "WHERE TABSCHEMA = ? AND TABNAME = ?",
        (row_count, npages, npages, schema, table),
    )
    if cur.rowcount == 0:
        warnings.append(
            f"SYSSTAT.TABLES row not found for {schema}.{table}. "
            "Table may not exist or was never RUNSTATS'd."
        )
    conn.commit()
    logger.info("Db2 table stats injected: %s.%s rows=%d", schema, table, row_count)

    # ── 2. Column-level statistics ─────────────────────────────────────────
    cols_ok = cols_skip = 0
    for col in table_stats.columns:
        try:
            _inject_db2_column(conn, schema, table, col, row_count, warnings)
            cols_ok += 1
        except Exception as exc:
            warnings.append(f"{col.name}: {exc}")
            cols_skip += 1

    conn.commit()
    return InjectionResult(
        dialect="db2",
        table=table,
        rows_injected=row_count,
        columns_injected=cols_ok,
        columns_skipped=cols_skip,
        warnings=warnings,
    )


def _inject_db2_column(  # pragma: no cover
    conn: Any,
    schema: str,
    table: str,
    col: ColumnStats,
    row_count: int,
    warnings: list[str],
) -> None:
    """Update SYSSTAT.COLUMNS and inject SYSSTAT.COLDIST rows for one column."""
    cur = conn.cursor()

    colcard   = int(col.n_distinct or 1)
    numnulls  = int((col.null_fraction or 0.0) * row_count)
    avgcollen = int(col.avg_width_bytes or 8)
    col_name  = col.name.upper()

    cur.execute(
        "UPDATE SYSSTAT.COLUMNS SET COLCARD = ?, NUMNULLS = ?, AVGCOLLEN = ? "
        "WHERE TABSCHEMA = ? AND TABNAME = ? AND COLNAME = ?",
        (colcard, numnulls, avgcollen, schema, table, col_name),
    )
    if cur.rowcount == 0:
        warnings.append(
            f"SYSSTAT.COLUMNS row not found for {schema}.{table}.{col_name}"
        )
    logger.debug(
        "Db2 column stats injected: %s.%s.%s colcard=%d numnulls=%d",
        schema, table, col_name, colcard, numnulls,
    )

    _inject_db2_coldist(conn, schema, table, col, row_count, warnings)


def _inject_db2_coldist(  # pragma: no cover
    conn: Any,
    schema: str,
    table: str,
    col: ColumnStats,
    row_count: int,
    warnings: list[str],
) -> None:
    """
    Inject MCV and histogram rows into SYSSTAT.COLDIST for one column.

    TYPE='F' rows (most common values):
        SEQNO = 1-based rank by descending frequency
        COLVALUE = value as string (truncated to 255 chars)
        VALCOUNT = estimated occurrence count (frequency × row_count)
        DISTCOUNT = NULL

    TYPE='Q' rows (histogram quantile bounds):
        SEQNO = 1-based bucket index
        COLVALUE = upper bound of the quantile bucket
        VALCOUNT = cumulative row count up to this bound (approximated)
        DISTCOUNT = cumulative distinct count up to this bound (approximated)

    Existing COLDIST rows for the column are deleted before re-insertion so
    the function is idempotent.
    """
    cur = conn.cursor()

    cur.execute(
        "DELETE FROM SYSSTAT.COLDIST "
        "WHERE TABSCHEMA = ? AND TABNAME = ? AND COLNAME = ?",
        (schema, table, col.name.upper()),
    )

    # ── TYPE='F': Most Common Values ──────────────────────────────────────
    mcv_count = 0
    if col.most_common_values:
        mcv_sorted = sorted(col.most_common_values, key=lambda m: m.frequency, reverse=True)
        for seqno, mcv in enumerate(mcv_sorted, start=1):
            valcount = max(1, int(mcv.frequency * row_count))
            colvalue = str(mcv.value)[:255]
            try:
                cur.execute(
                    "INSERT INTO SYSSTAT.COLDIST "
                    "(TABSCHEMA, TABNAME, COLNAME, TYPE, SEQNO, COLVALUE, VALCOUNT, DISTCOUNT) "
                    "VALUES (?, ?, ?, 'F', ?, ?, ?, NULL)",
                    (schema, table, col.name.upper(), seqno, colvalue, valcount),
                )
                mcv_count += 1
            except Exception as exc:
                warnings.append(f"{col.name} MCV seqno={seqno}: {exc}")

    # ── TYPE='Q': Histogram quantile bounds ───────────────────────────────
    hist_count = 0
    if col.histogram_bounds:
        n_bounds  = len(col.histogram_bounds)
        n_dist    = max(1, int(abs(col.n_distinct) or 1))
        for seqno, bound in enumerate(col.histogram_bounds, start=1):
            frac      = seqno / n_bounds
            valcount  = max(1, int(frac * row_count))
            distcount = max(1, int(frac * n_dist))
            colvalue  = str(bound)[:255]
            try:
                cur.execute(
                    "INSERT INTO SYSSTAT.COLDIST "
                    "(TABSCHEMA, TABNAME, COLNAME, TYPE, SEQNO, COLVALUE, VALCOUNT, DISTCOUNT) "
                    "VALUES (?, ?, ?, 'Q', ?, ?, ?, ?)",
                    (schema, table, col.name.upper(), seqno, colvalue, valcount, distcount),
                )
                hist_count += 1
            except Exception as exc:
                warnings.append(f"{col.name} histogram seqno={seqno}: {exc}")

    logger.debug(
        "Db2 COLDIST injected: %s.%s.%s mcv=%d histogram=%d",
        schema, table, col.name, mcv_count, hist_count,
    )
