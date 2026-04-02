"""Oracle dialect adapter."""

from __future__ import annotations

import re

from benchmarks.dialects._base import DialectBase, require_application_catalog


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


class OracleDialect(DialectBase):
    system_database              = None   # single-instance; no separate system DB
    ddl_if_not_exists            = False
    is_pg_wire                   = False
    needs_column_types_for_bulk_load = False
    sqlglot_dialect              = "oracle"
    manages_own_autocommit       = False
    supports_extended_stats      = False

    # ------------------------------------------------------------------ #
    # Connection                                                           #
    # ------------------------------------------------------------------ #

    def connect_from_profile(self, profile) -> object:
        import oracledb  # type: ignore
        host    = getattr(profile, "host",     None) or "127.0.0.1"
        port    = getattr(profile, "port",     None) or 1521
        service = getattr(profile, "database", None) or "XE"
        user    = getattr(profile, "username", None) or "system"
        password = getattr(profile, "password", None) or "oracle"
        conn = oracledb.connect(
            user=user,
            password=password,
            dsn=f"{host}:{port}/{service}",
        )
        conn._statschema_db = user.upper()
        return conn

    def connect(self, dsn: str) -> object:
        p = _parse_dsn(dsn)

        class _P:
            host     = p.get("host", "127.0.0.1")
            port     = int(p.get("port", "1521"))
            database = p.get("service", "XE")
            username = p.get("user", "system")
            password = p.get("password", "oracle")

        return self.connect_from_profile(_P())

    # ------------------------------------------------------------------ #
    # Transaction control                                                 #
    # ------------------------------------------------------------------ #

    def commit(self, conn) -> None:
        conn.commit()

    def rollback(self, conn) -> None:
        conn.rollback()

    # ------------------------------------------------------------------ #
    # Identifier normalisation / query rewrite / auto-stats              #
    # ------------------------------------------------------------------ #

    def normalize_identifier(self, name: str) -> str:
        return name.upper()

    def rewrite_query_sql(
        self, sql: str, schema: str, table_names: set[str], source_dialect: str
    ) -> str:
        from benchmarks.dialects._sql_rewrite import strip_double_quotes, rewrite_limit_to_fetch
        return rewrite_limit_to_fetch(strip_double_quotes(sql))

    def autovacuum_disable_sql(self) -> str | None:
        return None

    def configure_auto_stats(self, conn, enabled: bool) -> None:
        pass

    def predicate_query_store_kwargs(self, schema: str) -> dict:
        return {"schema": schema}

    def create_column_statistics_sql(self, schema, stat_name, col_a, col_b, table_name) -> str:
        raise NotImplementedError("Oracle does not support pg_statistic_ext-style extended stats")

    # ------------------------------------------------------------------ #
    # Catalog introspection                                               #
    # ------------------------------------------------------------------ #

    # Oracle built-in accounts that must never be dropped (lowercase for
    # uniform comparison — all list_* methods return lowercase).
    _SYSTEM_SCHEMAS = frozenset({
        "sys", "system", "outln", "dbsnmp", "appqossys", "dbsfwuser",
        "ggsys", "anonymous", "ctxsys", "dvsys", "dvf", "gsmadmin_internal",
        "mdsys", "olapsys", "ordplugins", "ordsys", "orddata", "si_informtn_schema",
        "wmsys", "xdb", "lbacsys", "apex_public_user", "flows_files",
        "hr", "oe", "pm", "ix", "sh", "bi",
    })

    def list_schemas(self, conn) -> list[str]:
        require_application_catalog(self, conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT username FROM dba_users "
                "WHERE account_status = 'OPEN' AND oracle_maintained = 'N'"
            )
            return [r[0].lower() for r in cur.fetchall()
                    if r[0].lower() not in self._SYSTEM_SCHEMAS]

    def list_tables(self, conn, schema: str) -> list[str]:
        require_application_catalog(self, conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM all_tables WHERE owner = :1",
                (schema.upper(),),
            )
            return [r[0].lower() for r in cur.fetchall()]

    # ------------------------------------------------------------------ #
    # Schema lifecycle                                                     #
    # ------------------------------------------------------------------ #

    def create_schema(self, conn, schema_name: str) -> None:
        """Drop and recreate an Oracle user (= schema). Requires DBA privileges."""
        if schema_name.lower() in self.list_schemas(conn):
            with conn.cursor() as cur:
                cur.execute(f"DROP USER {schema_name} CASCADE")
            conn.commit()
        with conn.cursor() as cur:
            cur.execute(f"CREATE USER {schema_name} IDENTIFIED BY Ident123")
            cur.execute(f"GRANT CONNECT, RESOURCE TO {schema_name}")
            cur.execute(f"GRANT CREATE SESSION TO {schema_name}")
            cur.execute(f"ALTER USER {schema_name} QUOTA UNLIMITED ON USERS")
        # Set USERS tablespace to NOLOGGING so all test tables inherit it.
        # Oracle XE runs in NOARCHIVELOG mode; this eliminates remaining redo overhead.
        try:
            with conn.cursor() as cur:
                cur.execute("ALTER TABLESPACE USERS NOLOGGING")
        except Exception:
            pass  # may already be set or require higher privilege
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
