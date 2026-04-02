"""SQL Server dialect adapter."""

from __future__ import annotations

import re

from benchmarks.dialects._base import DialectBase, require_application_catalog


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


class SQLServerDialect(DialectBase):
    system_database              = "master"
    ddl_if_not_exists            = False
    is_pg_wire                   = False
    needs_column_types_for_bulk_load = False
    sqlglot_dialect              = "tsql"
    manages_own_autocommit       = True  # connect_from_profile calls setautocommit(True)
    supports_extended_stats      = False

    # ------------------------------------------------------------------ #
    # Connection                                                           #
    # ------------------------------------------------------------------ #

    def connect_from_profile(self, profile) -> object:
        import mssql_python  # type: ignore
        host     = getattr(profile, "host",     None) or "127.0.0.1"
        port     = getattr(profile, "port",     None) or 1433
        database = getattr(profile, "database", None) or "master"
        user     = getattr(profile, "username", None) or "sa"
        password = getattr(profile, "password", None) or ""
        conn_str = (
            f"SERVER={host},{port};"
            f"DATABASE={database};"
            f"UID={user};"
            f"PWD={password};"
            "ENCRYPT=no;TrustServerCertificate=yes"
        )
        conn = mssql_python.connect(conn_str)
        # DDL (CREATE SCHEMA, DROP TABLE, UPDATE STATISTICS) must run outside
        # an explicit transaction.
        conn.setautocommit(True)
        conn._statschema_db     = database  # SQL Server catalog (the DATABASE)
        conn._statschema_schema = "dbo"     # current SQL Server schema; updated by set_namespace
        return conn

    def connect(self, dsn: str) -> object:
        p = _parse_dsn(dsn)

        class _P:
            host     = p.get("server", p.get("host", "127.0.0.1"))
            port     = int(p.get("port", "1433"))
            database = p.get("database", p.get("db", "master"))
            username = p.get("user", "sa")
            password = p.get("password", "")

        return self.connect_from_profile(_P())

    # ------------------------------------------------------------------ #
    # Transaction control                                                 #
    # ------------------------------------------------------------------ #

    def commit(self, conn) -> None:
        pass  # autocommit=True: each statement commits itself

    def rollback(self, conn) -> None:
        pass  # autocommit=True: no open transaction to roll back

    # ------------------------------------------------------------------ #
    # Identifier normalisation / query rewrite / auto-stats              #
    # ------------------------------------------------------------------ #

    def normalize_identifier(self, name: str) -> str:
        return name.lower()

    def rewrite_query_sql(
        self, sql: str, schema: str, table_names: set[str], source_dialect: str
    ) -> str:
        from benchmarks.dialects._sql_rewrite import transpile_and_qualify, SQLGLOT_DIALECT
        return transpile_and_qualify(
            sql, schema, table_names, source_dialect,
            SQLGLOT_DIALECT.get(self.sqlglot_dialect, self.sqlglot_dialect),
        )

    def autovacuum_disable_sql(self) -> str | None:
        return None

    def configure_auto_stats(self, conn, enabled: bool) -> None:
        pass

    def predicate_query_store_kwargs(self, schema: str) -> dict:
        return {}

    def create_column_statistics_sql(self, schema, stat_name, col_a, col_b, table_name) -> str:
        raise NotImplementedError("SQL Server does not support pg_statistic_ext-style extended stats")

    # ------------------------------------------------------------------ #
    # Catalog introspection                                               #
    # ------------------------------------------------------------------ #

    # System schemas that ship with every SQL Server instance — never touch.
    _SYSTEM_SCHEMAS = frozenset({
        "sys", "dbo", "guest", "information_schema",
        "db_owner", "db_accessadmin", "db_securityadmin", "db_ddladmin",
        "db_backupoperator", "db_datareader", "db_datawriter", "db_denydatareader",
        "db_denydatawriter",
    })

    def list_schemas(self, conn) -> list[str]:
        require_application_catalog(self, conn)
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM sys.schemas")
            return [r[0].lower() for r in cur.fetchall()
                    if r[0].lower() not in self._SYSTEM_SCHEMAS]

    def list_tables(self, conn, schema: str) -> list[str]:
        require_application_catalog(self, conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                f"WHERE TABLE_SCHEMA = N'{schema}' AND TABLE_TYPE = 'BASE TABLE'"
            )
            return [r[0].lower() for r in cur.fetchall()]

    # ------------------------------------------------------------------ #
    # Schema lifecycle                                                     #
    # ------------------------------------------------------------------ #

    def create_schema(self, conn, schema_name: str) -> None:
        """Drop (if present) and recreate a SQL Server schema.

        Uses catalog introspection to discover existing tables, then drops
        them and the schema deterministically — no IF-EXISTS guards in DDL.
        SQL Server SCHEMAs are lightweight namespaces; creating one is free.
        """
        for tname in self.list_tables(conn, schema_name):
            with conn.cursor() as cur:
                cur.execute(f"DROP TABLE [{schema_name}].[{tname}]")
        if schema_name in self.list_schemas(conn):
            with conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA [{schema_name}]")
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA [{schema_name}]")

    def set_namespace(self, conn, schema_name: str):
        """Record the active SQL Server schema; the catalog (DATABASE) never changes."""
        conn._statschema_schema = schema_name
        return conn

    # ------------------------------------------------------------------ #
    # Statistics                                                           #
    # ------------------------------------------------------------------ #

    def analyze(self, conn, tables: list, schema: str,
                pred_col_map: dict | None = None, full_stats: bool = False,
                tablesample_pct: float | None = None) -> None:
        # UPDATE STATISTICS is a DDL command; autocommit=True handles commit.
        # mssql_python raises "Invalid cursor state" if fetchall() is called on
        # a statement that returns no rows — UPDATE STATISTICS returns no rows.
        with conn.cursor() as cur:
            for t in tables:
                cur.execute(f"UPDATE STATISTICS [{schema}].[{self.normalize_identifier(t.name)}]")

    # ------------------------------------------------------------------ #
    # DDL helpers                                                          #
    # ------------------------------------------------------------------ #

    def qualify_ddl(self, ddl_text: str, schema_name: str) -> str:
        """Prefix every CREATE TABLE with the SQL Server schema name."""
        return re.sub(
            r"CREATE TABLE \[([^\]]+)\]",
            lambda m: f"CREATE TABLE [{schema_name}].[{m.group(1)}]",
            ddl_text,
        )

    def table_ref(self, table_name: str, schema_name: str) -> str:
        return f"[{schema_name}].[{self.normalize_identifier(table_name)}]"

    # ------------------------------------------------------------------ #
    # EXPLAIN                                                              #
    # ------------------------------------------------------------------ #

    def explain(self, conn, sql: str, schema: str) -> dict:
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
