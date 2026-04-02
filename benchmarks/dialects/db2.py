"""IBM DB2 dialect adapter."""

from __future__ import annotations

import logging

from benchmarks.dialects._base import DialectBase, require_application_catalog

logger = logging.getLogger(__name__)

_DB2_OP_MAP = {
    "TBSCAN":        "Seq Scan",
    "IXSCAN":        "Index Scan",
    "IXSCAN (SORT)": "Index Scan",
    "FETCH":         "Index Scan",
    "RIDSCN":        "Bitmap Index Scan",
    "HSJOIN":        "Hash Join",
    "NLJOIN":        "Nested Loop",
    "MSJOIN":        "Merge Join",
    "SORT":          "Sort",
    "GRPBY":         "Aggregate",
    "FILTER":        "Filter",
    "TBFUNC":        "Function Scan",
    "RETURN":        "Result",
    "TEMP":          "Materialize",
}


class DB2Dialect(DialectBase):
    system_database              = None   # single-instance; no separate system DB
    ddl_if_not_exists            = False
    is_pg_wire                   = False
    needs_column_types_for_bulk_load = True   # ibm_db_dbi cannot auto-coerce non-strings
    sqlglot_dialect              = "db2"
    manages_own_autocommit       = False
    supports_extended_stats      = False

    # ------------------------------------------------------------------ #
    # Connection                                                           #
    # ------------------------------------------------------------------ #

    def connect_from_profile(self, profile) -> object:
        import ibm_db_dbi  # type: ignore
        ibm_dsn = (
            f"DATABASE={getattr(profile, 'database', None) or 'SAMPLE'};"
            f"HOSTNAME={getattr(profile, 'host',     None) or '127.0.0.1'};"
            f"PORT={getattr(profile,     'port',     None) or 50000};"
            f"UID={getattr(profile,      'username', None) or 'db2inst1'};"
            f"PWD={getattr(profile,      'password', None) or ''};"
            "PROTOCOL=TCPIP;"
        )
        return ibm_db_dbi.connect(ibm_dsn, "", "")

    def connect(self, dsn: str) -> object:
        import ibm_db_dbi  # type: ignore
        # Native IBM DSN passthrough (DATABASE=…;HOSTNAME=…)
        if "DATABASE" in dsn or "HOSTNAME" in dsn:
            return ibm_db_dbi.connect(dsn, "", "")
        p = _parse_dsn(dsn)

        class _P:
            host     = p.get("host", "127.0.0.1")
            port     = int(p.get("port", "50000"))
            database = p.get("database", p.get("db", "SAMPLE"))
            username = p.get("user", "db2inst1")
            password = p.get("password", "")

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
        from benchmarks.dialects._sql_rewrite import rewrite_limit_to_fetch
        return rewrite_limit_to_fetch(sql)

    def autovacuum_disable_sql(self) -> str | None:
        return None

    def configure_auto_stats(self, conn, enabled: bool) -> None:
        pass

    def predicate_query_store_kwargs(self, schema: str) -> dict:
        return {}

    def create_column_statistics_sql(self, schema, stat_name, col_a, col_b, table_name) -> str:
        raise NotImplementedError("DB2 does not support pg_statistic_ext-style extended stats")

    # ------------------------------------------------------------------ #
    # Schema lifecycle                                                     #
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Catalog introspection                                               #
    # ------------------------------------------------------------------ #

    # Lowercase for uniform comparison — all list_* methods return lowercase.
    _SYSTEM_SCHEMAS = frozenset({
        "sysibm", "syscat", "sysstat", "sysfun", "sysproc",
        "systools", "nullid", "sqlj",
    })

    def list_schemas(self, conn) -> list[str]:
        require_application_catalog(self, conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT SCHEMANAME FROM SYSCAT.SCHEMATA "
                "WHERE DEFINER != 'SYSIBM'"
            )
            return [r[0].lower() for r in cur.fetchall()
                    if r[0].lower() not in self._SYSTEM_SCHEMAS]

    def list_tables(self, conn, schema: str) -> list[str]:
        require_application_catalog(self, conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT TABNAME FROM SYSCAT.TABLES "
                "WHERE TABSCHEMA = ? AND TYPE = 'T'",
                (schema.upper(),),
            )
            return [r[0].lower() for r in cur.fetchall()]

    def _list_objects(self, conn, schema: str, obj_type: str) -> list[str]:
        """Return object names of TYPE *obj_type* in *schema* (DB2 SYSCAT.TABLES)."""
        require_application_catalog(self, conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT TABNAME FROM SYSCAT.TABLES WHERE TABSCHEMA = ? AND TYPE = ?",
                (schema.upper(), obj_type),
            )
            return [r[0].lower() for r in cur.fetchall()]

    # ------------------------------------------------------------------ #
    # Schema lifecycle                                                     #
    # ------------------------------------------------------------------ #

    def create_schema(self, conn, schema_name: str) -> None:
        # Ensure EXPLAIN tables exist (needed for EXPLAIN ALL).
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM SYSTOOLS.EXPLAIN_OPERATOR FETCH FIRST 1 ROW ONLY")
        except Exception:
            try:
                with conn.cursor() as cur:
                    cur.execute("CALL SYSPROC.SYSINSTALLOBJECTS('EXPLAIN', 'C', NULL, NULL)")
                conn.commit()
            except Exception:
                pass

        # Drop all objects in dependency order so DROP SCHEMA RESTRICT succeeds.
        for obj_type, drop_tmpl in [
            ("T", "DROP TABLE {schema}.{name}"),
            ("S", "DROP SEQUENCE {schema}.{name}"),
            ("A", "DROP ALIAS {schema}.{name}"),
            ("V", "DROP VIEW {schema}.{name}"),
            ("N", "DROP NICKNAME {schema}.{name}"),
        ]:
            for name in self._list_objects(conn, schema_name, obj_type):
                with conn.cursor() as cur:
                    cur.execute(drop_tmpl.format(schema=schema_name, name=name))
        conn.commit()

        if schema_name.lower() in self.list_schemas(conn):
            with conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA {schema_name} RESTRICT")
            conn.commit()

        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {schema_name}")
        conn.commit()

    def set_namespace(self, conn, schema_name: str):
        with conn.cursor() as cur:
            cur.execute(f"SET SCHEMA {schema_name}")
        conn._statschema_db = schema_name.upper()
        return conn

    # ------------------------------------------------------------------ #
    # Statistics                                                           #
    # ------------------------------------------------------------------ #

    def analyze(
        self,
        conn,
        tables: list,
        schema: str,
        pred_col_map: dict | None = None,
        full_stats: bool = False,
        tablesample_pct: float | None = None,
    ) -> None:
        """Run DB2 RUNSTATS via ``CALL SYSPROC.ADMIN_CMD()``.

        ``ibm_db_dbi`` cannot execute ``RUNSTATS ON TABLE`` directly (SQL0104N);
        it must be wrapped in ``SYSPROC.ADMIN_CMD``.

        Always includes ``AND DETAILED INDEXES ALL``.  Without it SYSCAT.INDEXES
        is not refreshed and the stat collector falls back to full-table MIN/MAX
        scans for every column (2–3× slower subsequent collection).

        ``TABLESAMPLE SYSTEM(n)`` goes after ``AND DETAILED INDEXES ALL``.

        Default (fast) mode — ``full_stats=False``:
        • Tables with predicate columns:
          ``RUNSTATS ON TABLE … ON COLUMNS (c1, c2) WITH DISTRIBUTION
            AND DETAILED INDEXES ALL [TABLESAMPLE SYSTEM(n)]``
        • Tables without predicate columns:
          ``RUNSTATS ON TABLE … AND DETAILED INDEXES ALL [TABLESAMPLE SYSTEM(n)]``

        Full mode — ``full_stats=True`` (major-release / audit):
        • ``RUNSTATS … WITH DISTRIBUTION AND DETAILED INDEXES ALL [TABLESAMPLE …]``
          on every table.
        """
        sample_suffix = f" TABLESAMPLE SYSTEM({tablesample_pct})" if tablesample_pct else ""
        with conn.cursor() as cur:
            for t in tables:
                tref = f"{schema.upper()}.{t.name.upper()}"
                pred_cols_lower = {c.lower() for c in (pred_col_map or {}).get(t.name, [])}
                # col_clause_parts holds the SYSCAT-exact column names for RUNSTATS.
                # predicate_col_map has lowercase names; DB2 DDL may use mixed-case
                # double-quoted identifiers (e.g. "FirstName").  RUNSTATS ON COLUMNS
                # is case-sensitive when names are double-quoted, so we must use the
                # exact case stored in SYSCAT.COLUMNS.  We also filter cross-table
                # column attribution (unaliased multi-table JOINs attribute all bare
                # column refs to every table in the query).
                col_clause_parts: list[str] = []
                if pred_cols_lower and not full_stats:
                    cur.execute(
                        "SELECT COLNAME FROM SYSCAT.COLUMNS "
                        "WHERE TABSCHEMA = ? AND TABNAME = ?",
                        (schema.upper(), t.name.upper()),
                    )
                    # Build lowercase→original-case map; filter pred_cols to columns
                    # that actually exist in this table.
                    syscat_cols = {r[0].lower(): r[0] for r in cur.fetchall()}
                    col_clause_parts = [
                        f'"{syscat_cols[c]}"'
                        for c in sorted(pred_cols_lower)
                        if c in syscat_cols
                    ]
                if full_stats:
                    runstats = (f"RUNSTATS ON TABLE {tref} "
                                f"WITH DISTRIBUTION AND DETAILED INDEXES ALL{sample_suffix}")
                elif col_clause_parts:
                    runstats = (
                        f"RUNSTATS ON TABLE {tref} "
                        f"ON COLUMNS ({', '.join(col_clause_parts)}) WITH DISTRIBUTION "
                        f"AND DETAILED INDEXES ALL{sample_suffix}"
                    )
                else:
                    runstats = (f"RUNSTATS ON TABLE {tref} "
                                f"AND DETAILED INDEXES ALL{sample_suffix}")
                cur.execute(f"CALL SYSPROC.ADMIN_CMD('{runstats}')")
        conn.commit()

    # ------------------------------------------------------------------ #
    # DDL helpers                                                          #
    # ------------------------------------------------------------------ #

    def qualify_ddl(self, ddl_text: str, schema_name: str) -> str:
        return ddl_text

    def table_ref(self, table_name: str, schema_name: str) -> str:
        return f"{schema_name.upper()}.{table_name.upper()}"

    # ------------------------------------------------------------------ #
    # EXPLAIN                                                              #
    # ------------------------------------------------------------------ #

    def explain(self, conn, sql: str, schema: str) -> dict:
        """Use DB2 SET CURRENT EXPLAIN MODE and parse SYSTOOLS.EXPLAIN_{OPERATOR,STREAM}."""
        import datetime
        self.set_namespace(conn, schema)
        with conn.cursor() as cur:
            t_before = datetime.datetime.now(datetime.timezone.utc)
            try:
                cur.execute("SET CURRENT EXPLAIN MODE = EXPLAIN")
                try:
                    cur.execute(sql)
                except Exception:
                    pass  # expected — query is not actually executed in EXPLAIN mode
                cur.execute("SET CURRENT EXPLAIN MODE = NO")
            except Exception as e:
                logger.warning("DB2 EXPLAIN failed: %s", e)
                return {"Node Type": "Unknown", "Plan Rows": 1, "Plans": []}

            try:
                cur.execute("""
                    SELECT OPERATOR_ID, OPERATOR_TYPE
                    FROM SYSTOOLS.EXPLAIN_OPERATOR
                    WHERE EXPLAIN_TIME >= ?
                    ORDER BY OPERATOR_ID
                """, (t_before,))
                op_rows = cur.fetchall()
            except Exception:
                return {"Node Type": "Unknown", "Plan Rows": 1, "Plans": []}

            try:
                cur.execute("""
                    SELECT SOURCE_ID, TARGET_ID, STREAM_COUNT
                    FROM SYSTOOLS.EXPLAIN_STREAM
                    WHERE EXPLAIN_TIME >= ?
                      AND SOURCE_TYPE = 'Q'
                      AND TARGET_TYPE = 'Q'
                """, (t_before,))
                stream_rows = cur.fetchall()
            except Exception:
                stream_rows = []

        if not op_rows:
            return {"Node Type": "Unknown", "Plan Rows": 1, "Plans": []}

        by_id: dict[int, dict] = {}
        for op_id, op_type in op_rows:
            pg_op = _DB2_OP_MAP.get((op_type or "").strip().upper(),
                                    (op_type or "Unknown").title())
            by_id[int(op_id)] = {"Node Type": pg_op, "Plan Rows": 1,
                                  "Plans": [], "_parent": None}

        # Stream: source → target.  source_id's output cardinality = STREAM_COUNT.
        # target_id is the parent (consumer) of source_id.
        for src_id, tgt_id, card in stream_rows:
            if int(src_id) in by_id:
                by_id[int(src_id)]["Plan Rows"] = int(float(card or 1))
                by_id[int(src_id)]["_parent"] = int(tgt_id)

        roots: list[dict] = []
        for nid, node in by_id.items():
            pid = node.pop("_parent", None)
            if pid is not None and pid in by_id and pid != nid:
                by_id[pid]["Plans"].append(node)
            else:
                roots.append(node)

        return roots[0] if roots else {"Node Type": "Unknown", "Plan Rows": 1, "Plans": []}


def _parse_dsn(dsn: str) -> dict[str, str]:
    return dict(p.split("=", 1) for p in dsn.split() if "=" in p)
