"""Oracle dialect adapter."""

from __future__ import annotations

import re


_ORA_OP_MAP = {
    "TABLE ACCESS": {"FULL": "Seq Scan", "BY INDEX ROWID": "Index Scan",
                     "BY INDEX ROWID BATCHED": "Index Scan", "SAMPLE": "Seq Scan"},
    "INDEX":        {"RANGE SCAN": "Index Scan", "UNIQUE SCAN": "Index Scan",
                     "FULL SCAN": "Seq Scan", "FAST FULL SCAN": "Seq Scan"},
    "HASH JOIN":    {"": "Hash Join", "OUTER": "Hash Join", "ANTI": "Hash Join"},
    "NESTED LOOPS": {"": "Nested Loop", "OUTER": "Nested Loop"},
    "MERGE JOIN":   {"": "Merge Join", "CARTESIAN": "Merge Join"},
    "SORT":         {"GROUP BY": "Aggregate", "ORDER BY": "Sort",
                     "AGGREGATE": "Aggregate", "JOIN": "Merge Join", "UNIQUE": "Sort"},
    "FILTER":       {"": "Filter"},
    "COUNT":        {"STOPKEY": "Limit", "": "Aggregate"},
    "VIEW":         {"": "Subquery Scan"},
    "WINDOW":       {"SORT": "WindowAgg"},
}


class OracleDialect:

    # ------------------------------------------------------------------ #
    # Connection                                                           #
    # ------------------------------------------------------------------ #

    def connect(self, dsn: str):
        import oracledb  # type: ignore
        p = _parse_dsn(dsn)
        host    = p.get("host", "127.0.0.1")
        port    = p.get("port", "1521")
        service = p.get("service", "XE")
        user = p.get("user", "system")
        conn = oracledb.connect(
            user=user,
            password=p.get("password", "oracle"),
            dsn=f"{host}:{port}/{service}",
        )
        # Oracle schema == username; track it so loaders never need to query.
        conn._statschema_db = user.upper()
        return conn

    # ------------------------------------------------------------------ #
    # Schema lifecycle                                                     #
    # ------------------------------------------------------------------ #

    def create_schema(self, conn, schema_name: str) -> None:
        """Create an Oracle user (= schema). Requires DBA privileges (system user)."""
        with conn.cursor() as cur:
            try:
                cur.execute(f"DROP USER {schema_name} CASCADE")
            except Exception:
                pass  # expected if user doesn't exist yet
            cur.execute(f"CREATE USER {schema_name} IDENTIFIED BY Ident123")
            cur.execute(f"GRANT CONNECT, RESOURCE TO {schema_name}")
            cur.execute(f"GRANT CREATE SESSION TO {schema_name}")
            cur.execute(f"ALTER USER {schema_name} QUOTA UNLIMITED ON USERS")
            # Set USERS tablespace to NOLOGGING so all test tables created in it
            # inherit NOLOGGING by default.  direct_path_load then generates zero
            # redo, which meaningfully cuts load time on the emulated x86 VM.
            # Oracle XE already runs in NOARCHIVELOG mode; this removes the last
            # redo overhead for DML on test objects.  Idempotent.
            try:
                cur.execute("ALTER TABLESPACE USERS NOLOGGING")
            except Exception:
                pass
        conn.commit()

    def set_namespace(self, conn, schema_name: str):
        with conn.cursor() as cur:
            cur.execute(f"ALTER SESSION SET CURRENT_SCHEMA = {schema_name}")
        conn._statschema_db = schema_name.upper()
        return conn

    # ------------------------------------------------------------------ #
    # Statistics                                                           #
    # ------------------------------------------------------------------ #

    def analyze(self, conn, tables: list, schema: str,
                pred_col_map: dict | None = None, full_stats: bool = False,
                tablesample_pct: float | None = None) -> None:
        """Gather Oracle optimizer statistics via DBMS_STATS.

        When *pred_col_map* is provided and *full_stats* is False, only predicate
        columns get full distribution stats (SIZE AUTO); every other column gets a
        fast row-count-only sweep (SIZE 1).  With *full_stats=True* all columns
        receive SIZE AUTO — the same as Oracle's default DBMS_STATS options.
        """
        with conn.cursor() as cur:
            for t in tables:
                pred_cols = {c.lower() for c in (pred_col_map or {}).get(t.name, [])}
                if pred_cols and not full_stats:
                    # predicate_col_map may contain cross-table column names from
                    # unaliased multi-table queries (e.g. TPC-H Q3 joins customer,
                    # orders, lineitem without aliases — all bare column refs are
                    # attributed to every table in the query).  Filter to only
                    # columns that actually exist in this table before building
                    # method_opt, otherwise DBMS_STATS raises ORA-20001.
                    cur.execute(
                        "SELECT LOWER(column_name) FROM all_tab_columns "
                        "WHERE owner = :1 AND table_name = :2",
                        (schema.upper(), t.name.upper()),
                    )
                    valid_cols = {r[0] for r in cur.fetchall()}
                    pred_cols &= valid_cols
                if full_stats or not pred_cols:
                    method_opt = "FOR ALL COLUMNS SIZE AUTO"
                else:
                    col_list = ", ".join(
                        f"FOR COLUMNS {c} SIZE AUTO" for c in sorted(pred_cols)
                    )
                    method_opt = f"FOR ALL COLUMNS SIZE 1, {col_list}"
                cur.execute(
                    "BEGIN DBMS_STATS.GATHER_TABLE_STATS("
                    f"ownname => '{schema.upper()}', "
                    f"tabname => '{t.name.upper()}', "
                    f"method_opt => '{method_opt}'); END;"
                )
        conn.commit()

    # ------------------------------------------------------------------ #
    # DDL helpers                                                          #
    # ------------------------------------------------------------------ #

    def qualify_ddl(self, ddl_text: str, schema_name: str) -> str:
        """Remove double-quote delimiters from Oracle DDL identifiers.

        The Oracle DDL emitter creates column names as "bid" (lowercase, quoted,
        case-sensitive), but unquoted identifiers are uppercased by Oracle causing
        ORA-00904. Stripping quotes makes all identifiers case-insensitive.
        """
        return re.sub(r'"([^"]+)"', r'\1', ddl_text)

    def table_ref(self, table_name: str, schema_name: str) -> str:
        return f"{schema_name.upper()}.{table_name.upper()}"

    # ------------------------------------------------------------------ #
    # EXPLAIN                                                              #
    # ------------------------------------------------------------------ #

    def explain(self, conn, sql: str, schema: str) -> dict:
        self.set_namespace(conn, schema)
        with conn.cursor() as cur:
            try:
                cur.execute("DELETE FROM PLAN_TABLE WHERE STATEMENT_ID = 'IDENT'")
            except Exception:
                pass  # PLAN_TABLE may not exist yet
            cur.execute(f"EXPLAIN PLAN SET STATEMENT_ID = 'IDENT' FOR {sql}")
            cur.execute("""
                SELECT ID, PARENT_ID, OPERATION, OPTIONS, CARDINALITY
                FROM PLAN_TABLE
                WHERE STATEMENT_ID = 'IDENT'
                ORDER BY ID
            """)
            rows = cur.fetchall()

        if not rows:
            return {"Node Type": "Unknown", "Plan Rows": 1, "Plans": []}

        by_id: dict[int, dict] = {}
        for row in rows:
            nid, pid, op, opts, card = row
            pg_op = _ora_op_to_pg(op, opts)
            by_id[int(nid)] = {"Node Type": pg_op, "Plan Rows": int(card or 1),
                               "Plans": [], "_pid": pid}

        roots: list[dict] = []
        for nid, node in by_id.items():
            pid = node.pop("_pid", None)
            if pid is not None and int(pid) in by_id:
                by_id[int(pid)]["Plans"].append(node)
            else:
                roots.append(node)

        return roots[0] if roots else {"Node Type": "Unknown", "Plan Rows": 1, "Plans": []}


def _ora_op_to_pg(operation: str, options: str) -> str:
    op  = (operation or "").strip().upper()
    opt = (options   or "").strip().upper()
    sub = _ORA_OP_MAP.get(op, {})
    return sub.get(opt, sub.get("", op.title()))


def _parse_dsn(dsn: str) -> dict[str, str]:
    return dict(p.split("=", 1) for p in dsn.split() if "=" in p)
