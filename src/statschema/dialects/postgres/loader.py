"""PostgreSQL / CockroachDB / Neon bulk-copy loader (COPY FROM STDIN)."""

from __future__ import annotations

import csv
import io
import logging
import time
from typing import Any

import psycopg2

from .._loader_shared import _iter_rows, _quote_id
from ..base import TopologyAwareLoader

logger = logging.getLogger(__name__)

_MAX_COPY_RETRIES = 3


def bulk_load_postgres(  # pragma: no cover
    conn: Any,
    df: Any,
    table: str,
    col_names: list[str],
) -> int:
    """Load df into PostgreSQL / CockroachDB / Neon via COPY … FROM STDIN WITH CSV.

    CockroachDB can abort long-running COPY transactions with SerializationFailure
    (SQLSTATE 40001 / 'liveness session expired') when its heartbeat goroutine is
    CPU-starved under heavy concurrent host load.  Per CRDB docs, the canonical fix
    is to retry the transaction on 40001.  We rewind the in-memory buffer and retry
    up to _MAX_COPY_RETRIES times with an exponential back-off, which is safe for
    PostgreSQL as well (it never produces 40001 for COPY).
    """
    qtable = _quote_id(table, "postgres")
    qcols  = ", ".join(_quote_id(c, "postgres") for c in col_names)
    sql    = f"COPY {qtable} ({qcols}) FROM STDIN WITH (FORMAT CSV, NULL '')"

    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    count = 0
    for row in _iter_rows(df):
        writer.writerow(["" if v is None else v for v in row])
        count += 1

    for attempt in range(_MAX_COPY_RETRIES):
        buf.seek(0)
        try:
            cur = conn.cursor()
            cur.copy_expert(sql, buf)
            cur.close()
            conn.commit()
            break
        except psycopg2.errors.SerializationFailure:
            conn.rollback()
            if attempt == _MAX_COPY_RETRIES - 1:
                raise
            wait = 2 ** attempt
            logger.warning(
                "bulk_load_postgres: SerializationFailure on %s (attempt %d/%d), "
                "retrying in %ds", table, attempt + 1, _MAX_COPY_RETRIES, wait,
            )
            time.sleep(wait)

    logger.info("bulk_load_postgres: copied %d rows into %s", count, table)
    return count


# Redshift is intentionally excluded — it rejects COPY FROM STDIN and requires
# an S3-staged load.  Dialect names "redshift" should never reach this loader.
_POSTGRES_WIRE_DIALECTS = frozenset({
    "postgres", "cockroachdb", "neon", "lakebase",
})


class PostgresCopyStdinLoader(TopologyAwareLoader):
    """Topology-aware wrapper around ``bulk_load_postgres``."""

    def can_use(self, ctx: Any, dialect: str, col_types: list[str] | None) -> bool:
        return dialect in _POSTGRES_WIRE_DIALECTS

    def bulk_load(
        self,
        ctx: Any,
        conn: Any,
        df: Any,
        table: str,
        col_names: list[str],
        wait: bool = True,
    ) -> int:
        return bulk_load_postgres(conn, df, table, col_names)
