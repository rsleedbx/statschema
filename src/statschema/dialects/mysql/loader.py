"""MySQL / MariaDB bulk-copy loader (LOAD DATA LOCAL INFILE)."""

from __future__ import annotations

import csv
import logging
import os
import tempfile
from typing import Any, Optional

from .._loader_shared import _iter_rows, _quote_id
from ..base import TopologyAwareLoader

logger = logging.getLogger(__name__)


def bulk_load_mysql(  # pragma: no cover
    conn: Any,
    df: Any,
    table: str,
    col_names: list[str],
    staging_dir: Optional[str] = None,
) -> int:
    """Load df into MySQL / MariaDB via LOAD DATA LOCAL INFILE."""
    cur    = conn.cursor()
    qtable = _quote_id(table, "mysql")
    qcols  = ", ".join(_quote_id(c, "mysql") for c in col_names)
    base   = os.path.basename(table)
    fd, path = tempfile.mkstemp(prefix=f"statschema_{base}_", suffix=".csv", dir=staging_dir)

    count = 0
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for row in _iter_rows(df):
            writer.writerow(["\\N" if v is None else v for v in row])
            count += 1

    cur.execute(
        f"LOAD DATA LOCAL INFILE %s INTO TABLE {qtable} "
        f"FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '\"' "
        f"LINES TERMINATED BY '\\n' ({qcols})",
        (path,),
    )
    conn.commit()
    os.unlink(path)
    logger.info("bulk_load_mysql: loaded %d rows into %s", count, table)
    return count


class MySQLLocalInfileLoader(TopologyAwareLoader):
    """Topology-aware wrapper around ``bulk_load_mysql``."""


    def can_use(self, ctx: Any, dialect: str, col_types: list[str] | None) -> bool:
        return dialect in ("mysql", "mariadb")

    def bulk_load(
        self,
        ctx: Any,
        conn: Any,
        df: Any,
        table: str,
        col_names: list[str],
        wait: bool = True,
    ) -> int:
        return bulk_load_mysql(conn, df, table, col_names)
