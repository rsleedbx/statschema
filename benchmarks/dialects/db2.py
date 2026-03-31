"""IBM DB2 dialect adapter."""

from __future__ import annotations

import logging

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


class DB2Dialect:

    # ------------------------------------------------------------------ #
    # Connection                                                           #
    # ------------------------------------------------------------------ #

    def connect(self, dsn: str):
        import ibm_db_dbi  # type: ignore
        p = _parse_dsn(dsn)
        if "DATABASE" in dsn or "HOSTNAME" in dsn:
            ibm_dsn = dsn  # already a native IBM DSN
        else:
            ibm_dsn = (
                f"DATABASE={p.get('database', p.get('db', 'SAMPLE'))};"
                f"HOSTNAME={p.get('host', '127.0.0.1')};"
                f"PORT={p.get('port', '50000')};"
                f"UID={p.get('user', 'db2inst1')};"
                f"PWD={p.get('password', '')};"
                "PROTOCOL=TCPIP;"
            )
        return ibm_db_dbi.connect(ibm_dsn, "", "")

    # ------------------------------------------------------------------ #
    # Schema lifecycle                                                     #
    # ------------------------------------------------------------------ #

    def create_schema(self, conn, schema_name: str) -> None:
        with conn.cursor() as cur:
            # Ensure explain tables exist (needed for EXPLAIN ALL)
            try:
                cur.execute("SELECT 1 FROM SYSTOOLS.EXPLAIN_OPERATOR FETCH FIRST 1 ROW ONLY")
            except Exception:
                try:
                    cur.execute("CALL SYSPROC.SYSINSTALLOBJECTS('EXPLAIN', 'C', NULL, NULL)")
                    conn.commit()
                except Exception:
                    pass
            # Drop existing tables in this schema individually
            try:
                cur.execute(f"""
                    SELECT TABNAME FROM SYSCAT.TABLES
                    WHERE TABSCHEMA = '{schema_name.upper()}'
                """)
                for (tab,) in cur.fetchall():
                    try:
                        cur.execute(f"DROP TABLE {schema_name}.{tab}")
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                cur.execute(f"DROP SCHEMA {schema_name} RESTRICT")
            except Exception:
                pass
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
