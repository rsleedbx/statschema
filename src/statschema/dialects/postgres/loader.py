"""PostgreSQL / CockroachDB / Neon bulk-copy loader (COPY FROM STDIN)."""

from __future__ import annotations

import csv
import io
import logging
from typing import Any

from .._loader_shared import _iter_rows, _quote_id

logger = logging.getLogger(__name__)


def bulk_load_postgres(  # pragma: no cover
    conn: Any,
    df: Any,
    table: str,
    col_names: list[str],
) -> int:
    """Load df into PostgreSQL / CockroachDB / Neon via COPY … FROM STDIN WITH CSV."""
    cur    = conn.cursor()
    qtable = _quote_id(table, "postgres")
    qcols  = ", ".join(_quote_id(c, "postgres") for c in col_names)
    sql    = f"COPY {qtable} ({qcols}) FROM STDIN WITH (FORMAT CSV, NULL '')"

    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    count = 0
    for row in _iter_rows(df):
        writer.writerow(["" if v is None else v for v in row])
        count += 1

    buf.seek(0)
    cur.copy_expert(sql, buf)
    conn.commit()
    logger.info("bulk_load_postgres: copied %d rows into %s", count, table)
    return count
