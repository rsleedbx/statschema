"""PostgreSQL 18+ statistics injector."""

from __future__ import annotations

import logging
from typing import Any

from ...stats_model import ColumnStats, TableStats
from .._injection_result import InjectionResult

logger = logging.getLogger(__name__)


def inject_stats_postgres(  # pragma: no cover
    conn: Any,
    table_stats: TableStats,
    schema: str = "public",
) -> InjectionResult:
    """
    Inject TableStats into a PostgreSQL 18+ database using
    pg_restore_relation_stats() and pg_restore_attribute_stats().

    Parameters
    ----------
    conn        psycopg2 connection to the PostgreSQL 18+ target database.
    table_stats TableStats collected from any source dialect.
    schema      Target schema name (default: "public").
    """
    cur = conn.cursor()

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

    # SAVEPOINTs require an open transaction block.  If the caller set
    # autocommit=True, temporarily switch to manual-commit mode.
    _orig_autocommit = getattr(conn, "autocommit", None)
    if _orig_autocommit:
        conn.autocommit = False

    cols_ok = cols_skip = 0
    try:
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
    finally:
        if _orig_autocommit is not None:
            conn.autocommit = _orig_autocommit
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
    cur = conn.cursor()

    n_distinct: float
    if col.n_distinct is not None:
        if row_count > 0 and col.n_distinct < row_count * 0.9:
            n_distinct = col.n_distinct
        else:
            n_distinct = -1.0
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

    if col.most_common_values and len(col.most_common_values) >= 2:
        vals  = [str(v.value) for v in col.most_common_values]
        freqs = [float(v.frequency) for v in col.most_common_values]
        if sum(freqs) >= 0.05:
            kwargs["most_common_vals"]  = "{" + ",".join(
                '"' + v.replace('"', '\\"') + '"' if ',' in v or ' ' in v else v
                for v in vals
            ) + "}"
            kwargs["most_common_freqs"] = freqs

    if col.histogram_bounds and len(col.histogram_bounds) >= 3:
        if "most_common_freqs" not in kwargs or sum(freqs) < 0.95:
            kwargs["histogram_bounds"] = (
                "{" + ",".join(str(b) for b in col.histogram_bounds) + "}"
            )

    _PG_CASTS: dict[str, str] = {
        "null_frac":        "::real",
        "avg_width":        "::integer",
        "n_distinct":       "::real",
        "most_common_vals": "::text",
        "histogram_bounds": "::text",
        "correlation":      "::real",
    }

    parts: list[str] = []
    params: list[Any] = []

    for k, v in kwargs.items():
        if k == "most_common_freqs" and isinstance(v, list):
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
