"""PostgreSQL-wire dialect adapter (postgres, neon, cockroachdb, lakebase)."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


class PostgresDialect:
    def __init__(self, lakebase: bool = False) -> None:
        self._lakebase = lakebase

    # ------------------------------------------------------------------ #
    # Connection                                                           #
    # ------------------------------------------------------------------ #

    def connect(self, dsn: str):
        if self._lakebase:
            from src.statschema.cli import _lakebase_connect
            import os
            endpoint = os.environ.get("STATSCHEMA_LAKEBASE_ENDPOINT", "")
            host     = os.environ.get("STATSCHEMA_LAKEBASE_HOST", "")
            dbname   = os.environ.get("STATSCHEMA_LAKEBASE_DB", "databricks_postgres")
            user     = os.environ.get("STATSCHEMA_LAKEBASE_USER", "")
            for part in (dsn or "").split():
                k, _, v = part.partition("=")
                if k == "host":     host     = v
                if k == "dbname":   dbname   = v
                if k == "user":     user     = v
                if k == "endpoint": endpoint = v
            conn = _lakebase_connect(endpoint, host, dbname, user)
            conn.autocommit = False
            return conn
        import psycopg2  # type: ignore
        return psycopg2.connect(dsn)

    # ------------------------------------------------------------------ #
    # Schema lifecycle                                                     #
    # ------------------------------------------------------------------ #

    def create_schema(self, conn, schema_name: str) -> None:
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
            cur.execute(f'CREATE SCHEMA "{schema_name}"')
        conn.commit()

    def set_namespace(self, conn, schema_name: str):
        with conn.cursor() as cur:
            cur.execute(f'SET search_path = "{schema_name}", public')
        conn._statschema_db = schema_name
        return conn

    # ------------------------------------------------------------------ #
    # Statistics                                                           #
    # ------------------------------------------------------------------ #

    def analyze(self, conn, tables: list, schema: str,
                pred_col_map: dict | None = None, full_stats: bool = False,
                tablesample_pct: float | None = None) -> None:
        with conn.cursor() as cur:
            for t in tables:
                cur.execute(f'ANALYZE "{schema}"."{t.name}"')
        conn.commit()

    # ------------------------------------------------------------------ #
    # DDL helpers                                                          #
    # ------------------------------------------------------------------ #

    def qualify_ddl(self, ddl_text: str, schema_name: str) -> str:
        return ddl_text  # PostgreSQL DDL emitter already handles schema namespacing

    def table_ref(self, table_name: str, schema_name: str) -> str:
        return f'"{schema_name}"."{table_name}"'

    # ------------------------------------------------------------------ #
    # EXPLAIN                                                              #
    # ------------------------------------------------------------------ #

    def explain(self, conn, sql: str, schema: str) -> dict:
        self.set_namespace(conn, schema)
        try:
            with conn.cursor() as cur:
                cur.execute(f"EXPLAIN (FORMAT JSON) {sql}")
                raw = cur.fetchone()[0]
            plans = json.loads(raw) if isinstance(raw, str) else raw
            return plans[0]["Plan"]
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            return _explain_crdb_text(conn, sql, schema, self)


class CockroachDBDialect(PostgresDialect):
    """CockroachDB — same wire protocol as Postgres but different EXPLAIN output."""

    def explain(self, conn, sql: str, schema: str) -> dict:
        self.set_namespace(conn, schema)
        try:
            with conn.cursor() as cur:
                cur.execute(f"EXPLAIN (FORMAT JSON) {sql}")
                raw = cur.fetchone()[0]
            plans = json.loads(raw) if isinstance(raw, str) else raw
            return plans[0]["Plan"]
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            return _explain_crdb_text(conn, sql, schema, self)


# ---------------------------------------------------------------------------
# CockroachDB text EXPLAIN parser
# ---------------------------------------------------------------------------

_CRDB_TYPE_MAP = {
    "hash-join": "Hash Join",
    "merge-join": "Merge Join",
    "lookup-join": "Nested Loop",
    "cross-join": "Nested Loop",
    "anti-join": "Hash Join",
    "semi-join": "Hash Join",
    "hash-group-by": "Aggregate",
    "stream-group-by": "Aggregate",
    "scalar-group-by": "Aggregate",
    "filter": "Filter",
    "scan": "Seq Scan",
    "index-scan": "Index Scan",
    "index-join": "Index Join",
    "sort": "Sort",
    "limit": "Limit",
    "union": "Append",
    "union-all": "Append",
    "except": "SetOp",
    "intersect": "SetOp",
    "window": "WindowAgg",
    "project": "Result",
    "distinct": "Unique",
    "values": "Result",
    "insert": "Insert",
    "update": "Update",
    "delete": "Delete",
    "render": "Result",
    "root": "Result",
}


def _pg_type(crdb_name: str) -> str:
    name = crdb_name.strip().lstrip("•").strip().lower()
    name = name.split("(")[0].strip()
    return _CRDB_TYPE_MAP.get(name, crdb_name.strip().title())


def _parse_row_count(desc: str) -> int:
    m = re.match(r"[\d,]+", desc.replace(" ", ""))
    if m:
        try:
            return int(m.group(0).replace(",", ""))
        except ValueError:
            pass
    return 1


def _explain_crdb_text(conn, sql: str, schema: str, dialect: PostgresDialect) -> dict:
    dialect.set_namespace(conn, schema)
    with conn.cursor() as cur:
        cur.execute(f"EXPLAIN {sql}")
        rows = cur.fetchall()

    if not rows:
        return {"Node Type": "Unknown", "Plan Rows": 1, "Plans": []}

    if len(rows[0]) >= 3:
        nodes_flat: list[tuple[str, int]] = []
        pending_type: str | None = None
        pending_rows = 1
        for row in rows:
            tree_col  = (row[0] or "").strip()
            field_col = (row[1] or "").strip()
            desc_col  = (row[2] or "").strip()
            if tree_col and not field_col:
                if pending_type is not None:
                    nodes_flat.append((_pg_type(pending_type), pending_rows))
                pending_type = tree_col
                pending_rows = 1
            elif field_col.lower() in ("estimated row count", "estimated rows"):
                pending_rows = _parse_row_count(desc_col)
        if pending_type is not None:
            nodes_flat.append((_pg_type(pending_type), pending_rows))
    else:
        nodes_flat = []
        pending_type = None
        pending_rows = 1
        for row in rows:
            line   = (row[0] or "").strip()
            node_m = re.search(r"•\s+([\w\s\-]+?)(?:\s*$|\s*\()", line)
            if node_m:
                if pending_type is not None:
                    nodes_flat.append((_pg_type(pending_type), pending_rows))
                pending_type = node_m.group(1).strip()
                pending_rows = 1
            row_m = re.search(r"estimated row count:\s*([\d,]+)", line, re.IGNORECASE)
            if row_m:
                pending_rows = _parse_row_count(row_m.group(1))
        if pending_type is not None:
            nodes_flat.append((_pg_type(pending_type), pending_rows))

    if not nodes_flat:
        return {"Node Type": "Unknown", "Plan Rows": 1, "Plans": []}

    root: dict = {"Node Type": nodes_flat[0][0], "Plan Rows": nodes_flat[0][1], "Plans": []}
    current = root
    for ntype, nrows in nodes_flat[1:]:
        child: dict = {"Node Type": ntype, "Plan Rows": nrows, "Plans": []}
        current["Plans"].append(child)
        current = child
    return root
