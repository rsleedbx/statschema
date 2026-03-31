"""SQL Server dialect adapter."""

from __future__ import annotations

import re


_MSSQL_OP_MAP = {
    "Clustered Index Scan": "Seq Scan",
    "Clustered Index Seek": "Index Scan",
    "Index Scan":           "Seq Scan",
    "Index Seek":           "Index Scan",
    "Table Scan":           "Seq Scan",
    "Hash Match":           "Hash Join",
    "Nested Loops":         "Nested Loop",
    "Merge Join":           "Merge Join",
    "Sort":                 "Sort",
    "Top":                  "Limit",
    "Aggregate":            "Aggregate",
    "Stream Aggregate":     "Aggregate",
    "Compute Scalar":       "Result",
    "Filter":               "Filter",
    "Bitmap":               "Hash",
    "Parallelism":          "Gather",
    "Row Count Spool":      "Materialize",
}


class SQLServerDialect:

    # ------------------------------------------------------------------ #
    # Connection                                                           #
    # ------------------------------------------------------------------ #

    def connect(self, dsn: str):
        import mssql_python  # type: ignore
        p = _parse_dsn(dsn)
        host     = p.get("server", p.get("host", "127.0.0.1"))
        port     = int(p.get("port", "1433"))
        database = p.get("database", p.get("db", "master"))
        user     = p.get("user", "sa")
        password = p.get("password", "")
        conn_str = (
            f"SERVER={host},{port};"
            f"DATABASE={database};"
            f"UID={user};"
            f"PWD={password};"
            "ENCRYPT=no;TrustServerCertificate=yes"
        )
        conn = mssql_python.connect(conn_str)
        # DDL (CREATE DATABASE, DROP DATABASE, UPDATE STATISTICS) must run outside
        # an explicit transaction.
        conn.setautocommit(True)
        # Template used by set_namespace() to reconnect to a different database
        # without USE [db] (not supported on Azure SQL Database).
        conn._mssql_conn_template = (
            f"SERVER={host},{port};"
            f"DATABASE={{db}};"
            f"UID={user};"
            f"PWD={password};"
            "ENCRYPT=no;TrustServerCertificate=yes"
        )
        # Track the current catalog on the connection so callers never need to
        # round-trip SELECT DB_NAME().
        conn._statschema_db = database
        return conn

    # ------------------------------------------------------------------ #
    # Schema lifecycle                                                     #
    # ------------------------------------------------------------------ #

    def create_schema(self, conn, schema_name: str) -> None:
        """Drop and recreate a SQL Server database used as the identity-test namespace."""
        with conn.cursor() as cur:
            try:
                cur.execute(f"ALTER DATABASE [{schema_name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
            except Exception:
                pass
            try:
                cur.execute(f"DROP DATABASE [{schema_name}]")
            except Exception:
                pass
            cur.execute(f"CREATE DATABASE [{schema_name}]")
            # SIMPLE recovery allows transaction log space to be reused after each
            # checkpoint, preventing unbounded log file growth.  It does NOT reduce
            # redo writes during bulk load (only BULK_LOGGED recovery does that);
            # the identity test bottleneck is CPU-bound UPDATE STATISTICS, not log I/O.
            # New databases inherit FULL from the model database; override for test namespaces.
            cur.execute(f"ALTER DATABASE [{schema_name}] SET RECOVERY SIMPLE")

    def set_namespace(self, conn, schema_name: str):
        """Reconnect to target database; returns the new connection.

        Does NOT use USE [db] — that statement is unsupported on Azure SQL
        Database and on any SQL Server connection with a pooled session.
        Instead we close the current connection and open a fresh one to the
        target database using the template stored at connect() time.
        """
        import mssql_python  # type: ignore
        tmpl = getattr(conn, "_mssql_conn_template", None)
        if tmpl is None:
            raise RuntimeError(
                "SQLServerDialect.set_namespace: conn._mssql_conn_template is not set. "
                "Create the connection via SQLServerDialect.connect() so the "
                "database-switch template is available."
            )
        try:
            conn.close()
        except Exception:
            pass
        new_conn = mssql_python.connect(tmpl.format(db=schema_name))
        new_conn.setautocommit(True)
        new_conn._mssql_conn_template = tmpl
        new_conn._statschema_db = schema_name
        return new_conn

    # ------------------------------------------------------------------ #
    # Statistics                                                           #
    # ------------------------------------------------------------------ #

    def analyze(self, conn, tables: list, schema: str,
                pred_col_map: dict | None = None, full_stats: bool = False,
                tablesample_pct: float | None = None) -> None:
        # SQL Server statistics are per-object; UPDATE STATISTICS refreshes all
        # statistics objects on the table.  Autocommit=True handles commit.
        # mssql_python raises "Invalid cursor state" if fetchall() is called on
        # a statement that returns no rows — UPDATE STATISTICS is a command, not a query.
        with conn.cursor() as cur:
            for t in tables:
                cur.execute(f"UPDATE STATISTICS [dbo].[{t.name}]")

    # ------------------------------------------------------------------ #
    # DDL helpers                                                          #
    # ------------------------------------------------------------------ #

    def qualify_ddl(self, ddl_text: str, schema_name: str) -> str:
        """Prefix every CREATE TABLE with the schema (= database) name."""
        return re.sub(
            r"CREATE TABLE \[([^\]]+)\]",
            lambda m: f"CREATE TABLE [{schema_name}].[{m.group(1)}]",
            ddl_text,
        )

    def table_ref(self, table_name: str, schema_name: str) -> str:
        # SQL Server maps schema_name → database; tables live under [db].[dbo].[table].
        return f"[{schema_name}].[dbo].[{table_name}]"

    # ------------------------------------------------------------------ #
    # EXPLAIN                                                              #
    # ------------------------------------------------------------------ #

    def explain(self, conn, sql: str, schema: str) -> dict:
        conn = self.set_namespace(conn, schema)
        with conn.cursor() as cur:
            cur.execute("SET SHOWPLAN_ALL ON")
            try:
                cur.execute(sql)
                rows = cur.fetchall()
                # Columns: StmtText(0), StmtId(1), NodeId(2), Parent(3),
                #          PhysicalOp(4), LogicalOp(5), ..., EstimateRows(8)
                nodes: list[tuple[int, int, str, int]] = []
                for row in rows:
                    node_id  = row[2] if row[2] is not None else 0
                    parent   = row[3] if row[3] is not None else 0
                    phys_op  = (row[4] or "").strip()
                    est_rows = int(float(row[8] or 1)) if row[8] else 1
                    if phys_op:
                        pg_op = _MSSQL_OP_MAP.get(phys_op, phys_op)
                        nodes.append((int(node_id), int(parent), pg_op, est_rows))
            finally:
                cur.execute("SET SHOWPLAN_ALL OFF")

        if not nodes:
            return {"Node Type": "Unknown", "Plan Rows": 1, "Plans": []}

        by_id: dict[int, dict] = {}
        for nid, _, op, rows_ in nodes:
            by_id[nid] = {"Node Type": op, "Plan Rows": rows_, "Plans": []}
        roots: list[dict] = []
        for nid, parent_id, _, _ in nodes:
            if parent_id in by_id and parent_id != nid:
                by_id[parent_id]["Plans"].append(by_id[nid])
            elif parent_id == 0 or parent_id not in by_id:
                roots.append(by_id[nid])

        return roots[0] if roots else {"Node Type": "Unknown", "Plan Rows": 1, "Plans": []}


def _parse_dsn(dsn: str) -> dict[str, str]:
    return dict(p.split("=", 1) for p in dsn.split() if "=" in p)
