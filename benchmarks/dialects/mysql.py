"""MySQL / MariaDB dialect adapter."""

from __future__ import annotations

import json


_MYSQL_ACCESS_MAP = {
    "ALL": "Seq Scan", "index": "Index Scan", "range": "Index Scan",
    "ref": "Index Scan", "eq_ref": "Index Scan", "const": "Index Scan",
    "system": "Index Scan", "fulltext": "Index Scan",
}


class MySQLDialect:

    # ------------------------------------------------------------------ #
    # Connection                                                           #
    # ------------------------------------------------------------------ #

    def connect(self, dsn: str):
        import pymysql  # type: ignore
        p = _parse_dsn(dsn)
        database = p.get("database", p.get("db", "mysql"))
        conn = pymysql.connect(
            host=p.get("host", "127.0.0.1"),
            port=int(p.get("port", "3306")),
            user=p.get("user", "root"),
            password=p.get("password", p.get("passwd", "")),
            database=database,
            local_infile=True,
            autocommit=False,
            charset="utf8mb4",
        )
        conn._statschema_db = database
        return conn

    # ------------------------------------------------------------------ #
    # Schema lifecycle                                                     #
    # ------------------------------------------------------------------ #

    def create_schema(self, conn, schema_name: str) -> None:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{schema_name}`")
            cur.execute(f"CREATE DATABASE `{schema_name}` CHARACTER SET utf8mb4")
        conn.commit()

    def set_namespace(self, conn, schema_name: str):
        with conn.cursor() as cur:
            cur.execute(f"USE `{schema_name}`")
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
                cur.execute(f"ANALYZE TABLE `{schema}`.`{t.name}`")
                cur.fetchall()  # consume result

    # ------------------------------------------------------------------ #
    # DDL helpers                                                          #
    # ------------------------------------------------------------------ #

    def qualify_ddl(self, ddl_text: str, schema_name: str) -> str:
        return ddl_text

    def table_ref(self, table_name: str, schema_name: str) -> str:
        return f"`{schema_name}`.`{table_name}`"

    # ------------------------------------------------------------------ #
    # EXPLAIN                                                              #
    # ------------------------------------------------------------------ #

    def explain(self, conn, sql: str, schema: str) -> dict:
        self.set_namespace(conn, schema)
        with conn.cursor() as cur:
            cur.execute(f"EXPLAIN FORMAT=JSON {sql}")
            raw = cur.fetchone()[0]
        plan = json.loads(raw) if isinstance(raw, str) else raw
        result = _parse_mysql_plan_node(plan.get("query_block", plan))
        return result or {"Node Type": "Unknown", "Plan Rows": 1, "Plans": []}


# ---------------------------------------------------------------------------
# MySQL plan node parser
# ---------------------------------------------------------------------------

def _parse_mysql_plan_node(node: dict) -> dict | None:
    if "nested_loop" in node:
        children = [_parse_mysql_plan_node(item)
                    for item in node["nested_loop"]]
        children = [c for c in children if c]
        join_type = node.get("hash_join_type") or node.get("join_type", "Nested Loop")
        pg_type   = "Hash Join" if "hash" in str(join_type).lower() else "Nested Loop"
        outer_rows = children[0]["Plan Rows"] if children else 1
        return {"Node Type": pg_type, "Plan Rows": outer_rows, "Plans": children}

    if "table" in node:
        tbl    = node["table"]
        access = tbl.get("access_type", "ALL")
        pg_type = _MYSQL_ACCESS_MAP.get(access, "Seq Scan")
        rows = int(float(tbl.get("rows_examined_per_scan",
                                 tbl.get("rows_produced_per_join", 1))))
        return {"Node Type": pg_type, "Plan Rows": rows, "Plans": []}

    if "grouping_operation" in node:
        inner = _parse_mysql_plan_node(node["grouping_operation"])
        rows = inner["Plan Rows"] if inner else 1
        return {"Node Type": "Aggregate", "Plan Rows": rows,
                "Plans": [inner] if inner else []}

    if "ordering_operation" in node:
        inner = _parse_mysql_plan_node(node["ordering_operation"])
        rows = inner["Plan Rows"] if inner else 1
        return {"Node Type": "Sort", "Plan Rows": rows,
                "Plans": [inner] if inner else []}

    if "duplicates_removal" in node:
        inner = _parse_mysql_plan_node(node["duplicates_removal"])
        rows = inner["Plan Rows"] if inner else 1
        return {"Node Type": "Aggregate", "Plan Rows": rows,
                "Plans": [inner] if inner else []}

    if "query_block" in node:
        return _parse_mysql_plan_node(node["query_block"])

    return None


def _parse_dsn(dsn: str) -> dict[str, str]:
    return dict(p.split("=", 1) for p in dsn.split() if "=" in p)
