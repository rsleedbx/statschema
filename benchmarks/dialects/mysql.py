"""MySQL / MariaDB dialect adapter."""

from __future__ import annotations

import json

from benchmarks.bench_config import DEFAULT_CATALOG
from benchmarks.dialects._base import DialectBase, require_application_catalog


_MYSQL_ACCESS_MAP = {
    "ALL": "Seq Scan", "index": "Index Scan", "range": "Index Scan",
    "ref": "Index Scan", "eq_ref": "Index Scan", "const": "Index Scan",
    "system": "Index Scan", "fulltext": "Index Scan",
}


class MySQLDialect(DialectBase):
    system_database              = "mysql"
    ddl_if_not_exists            = False
    is_pg_wire                   = False
    needs_column_types_for_bulk_load = False
    sqlglot_dialect              = "mysql"
    manages_own_autocommit       = False
    supports_extended_stats      = False

    # ------------------------------------------------------------------ #
    # Connection                                                           #
    # ------------------------------------------------------------------ #

    def connect_from_profile(self, profile) -> object:
        import pymysql  # type: ignore
        database = getattr(profile, "database", None) or DEFAULT_CATALOG
        conn = pymysql.connect(
            host=getattr(profile, "host",     None) or "127.0.0.1",
            port=getattr(profile, "port",     None) or 3306,
            user=getattr(profile, "username", None) or "root",
            password=getattr(profile, "password", None) or "",
            database=database,
            local_infile=True,
            autocommit=False,
            charset="utf8mb4",
        )
        conn._statschema_db = database
        return conn

    def connect(self, dsn: str) -> object:
        p = _parse_dsn(dsn)

        class _P:
            host     = p.get("host", "127.0.0.1")
            port     = int(p.get("port", "3306"))
            database = p.get("database", p.get("db", DEFAULT_CATALOG))
            username = p.get("user", "root")
            password = p.get("password", p.get("passwd", ""))

        return self.connect_from_profile(_P())

    # ------------------------------------------------------------------ #
    # Catalog introspection                                               #
    # ------------------------------------------------------------------ #

    _SYSTEM_SCHEMAS = frozenset({
        "information_schema", "mysql", "performance_schema", "sys",
    })

    def list_schemas(self, conn) -> list[str]:
        require_application_catalog(self, conn)
        with conn.cursor() as cur:
            cur.execute("SHOW DATABASES")
            return [r[0].lower() for r in cur.fetchall()
                    if r[0].lower() not in self._SYSTEM_SCHEMAS]

    def list_tables(self, conn, schema: str) -> list[str]:
        require_application_catalog(self, conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'",
                (schema,),
            )
            return [r[0].lower() for r in cur.fetchall()]

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
        # MySQL/MariaDB has no schema concept; the catalog IS the schema.
        return {"catalog": schema}

    def create_column_statistics_sql(self, schema, stat_name, col_a, col_b, table_name) -> str:
        raise NotImplementedError("MySQL does not support pg_statistic_ext-style extended stats")

    # ------------------------------------------------------------------ #
    # Provisioning                                                         #
    # ------------------------------------------------------------------ #

    def provision(
        self,
        dba_conn,
        catalog: str,
        app_username: str,
        app_password: str,
    ) -> None:
        """Idempotently create *app_username* and *catalog*, grant access.

        *dba_conn* must be an open pymysql connection to the ``mysql``
        system database with root/DBA privileges.  All steps are idempotent
        and safe to re-run.

        SQL sequence adapted from lakeflow_connect/mysql/02_mysql_configure.sh.
        """
        import logging as _logging
        _mlog = _logging.getLogger(__name__)

        with dba_conn.cursor() as cur:
            # Step 1: create user (IF NOT EXISTS is idempotent); always sync password.
            cur.execute(
                "CREATE USER IF NOT EXISTS %s@'%%' IDENTIFIED BY %s",
                (app_username, app_password),
            )
            cur.execute(
                "ALTER USER %s@'%%' IDENTIFIED BY %s",
                (app_username, app_password),
            )
            _mlog.info("provision mysql: user %r created/updated", app_username)

            # Step 2: create database.
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{catalog}` CHARACTER SET utf8mb4"
            )
            _mlog.info("provision mysql: catalog %r created/verified", catalog)

            # Step 3: grant full access to the catalog.
            cur.execute(
                f"GRANT ALL PRIVILEGES ON `{catalog}`.* TO %s@'%%'",
                (app_username,),
            )
            cur.execute("FLUSH PRIVILEGES")

        dba_conn.commit()
        _mlog.info(
            "provision mysql: user %r granted access to catalog %r",
            app_username, catalog,
        )

        # Step 4: verify — connect as the app user to confirm provisioning succeeded.
        import pymysql as _pymysql  # type: ignore
        host = getattr(dba_conn, "host", "127.0.0.1")
        port = int(getattr(dba_conn, "port", 3306))
        try:
            test_conn = _pymysql.connect(
                host=host, port=port,
                user=app_username, password=app_password,
                database=catalog,
            )
            test_conn.close()
            _mlog.info(
                "provision mysql: login verified user=%r catalog=%r host=%s port=%s",
                app_username, catalog, host, port,
            )
        except Exception as exc:
            raise RuntimeError(
                f"provision mysql: verification login failed "
                f"(user={app_username!r} catalog={catalog!r}): {exc}"
            ) from exc

    # ------------------------------------------------------------------ #
    # Schema lifecycle                                                     #
    # ------------------------------------------------------------------ #

    def create_schema(self, conn, schema_name: str) -> None:
        if schema_name in self.list_schemas(conn):
            with conn.cursor() as cur:
                cur.execute(f"DROP DATABASE `{schema_name}`")
            conn.commit()
        with conn.cursor() as cur:
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
                cur.execute(f"ANALYZE TABLE `{schema}`.`{self.normalize_identifier(t.name)}`")
                cur.fetchall()  # consume result

    # ------------------------------------------------------------------ #
    # DDL helpers                                                          #
    # ------------------------------------------------------------------ #

    def qualify_ddl(self, ddl_text: str, schema_name: str) -> str:
        return ddl_text

    def table_ref(self, table_name: str, schema_name: str) -> str:
        return f"`{schema_name}`.`{self.normalize_identifier(table_name)}`"

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
