"""PostgreSQL-wire dialect adapter (postgres, neon, cockroachdb, lakebase)."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from benchmarks.bench_config import DEFAULT_CATALOG  # noqa: E402
from benchmarks.dialects._base import DialectBase, require_application_catalog  # noqa: E402


class PostgresDialect(DialectBase):
    system_database              = "postgres"
    ddl_if_not_exists            = True
    is_pg_wire                   = True
    needs_column_types_for_bulk_load = False
    sqlglot_dialect              = "postgres"
    manages_own_autocommit       = False
    supports_extended_stats      = True

    def __init__(self, lakebase: bool = False) -> None:
        self._lakebase = lakebase

    # ------------------------------------------------------------------ #
    # Connection                                                           #
    # ------------------------------------------------------------------ #

    def connect_from_profile(self, profile) -> object:
        if self._lakebase:
            from src.statschema.cli import _lakebase_connect  # type: ignore
            endpoint = getattr(profile, "endpoint", None) or ""
            host     = getattr(profile, "host",     None) or ""
            dbname   = getattr(profile, "database", None) or "databricks_postgres"
            user     = getattr(profile, "username", None) or ""
            conn = _lakebase_connect(endpoint, host, dbname, user)
            conn.autocommit = False
            return conn
        import psycopg2  # type: ignore
        return psycopg2.connect(
            host=getattr(profile, "host",     None) or "127.0.0.1",
            port=getattr(profile, "port",     None) or 5432,
            dbname=getattr(profile, "database", None) or DEFAULT_CATALOG,
            user=getattr(profile, "username", None) or "postgres",
            password=getattr(profile, "password", None) or "",
        )

    def connect(self, dsn: str) -> object:
        if self._lakebase:
            from statschema.connection_profile import load_profile  # type: ignore
            _bench_yaml = str(_REPO_ROOT / "config" / "statschema.tpcb.yaml")
            p = load_profile(_bench_yaml, "tpcb_lakebase")
            # DSN keyword overrides (e.g. from --conn-profile injection)
            for part in (dsn or "").split():
                k, _, v = part.partition("=")
                if k == "host":     p.host     = v
                if k == "dbname":   p.database = v
                if k == "user":     p.username = v
                if k == "endpoint": p.endpoint = v
            return self.connect_from_profile(p)
        import psycopg2  # type: ignore
        return psycopg2.connect(dsn)

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
        from ._sql_rewrite import transpile_and_qualify
        return transpile_and_qualify(sql, schema, table_names, source_dialect, self.sqlglot_dialect)

    def autovacuum_disable_sql(self) -> str | None:
        return "SET (autovacuum_enabled = false, toast.autovacuum_enabled = false)"

    def configure_auto_stats(self, conn, enabled: bool) -> None:
        pass  # PostgreSQL does not have a cluster-level auto-stats toggle

    def predicate_query_store_kwargs(self, schema: str) -> dict:
        return {}

    def create_column_statistics_sql(
        self, schema: str, stat_name: str, col_a: str, col_b: str, table_name: str
    ) -> str:
        return (
            f'CREATE STATISTICS IF NOT EXISTS "{schema}"."{stat_name}" '
            f'ON "{col_a}", "{col_b}" '
            f'FROM "{schema}"."{table_name}"'
        )

    # ------------------------------------------------------------------ #
    # Catalog introspection                                               #
    # ------------------------------------------------------------------ #

    _SYSTEM_SCHEMAS = frozenset({
        "pg_catalog", "pg_toast", "information_schema", "public",
    })

    def list_schemas(self, conn) -> list[str]:
        require_application_catalog(self, conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name NOT LIKE 'pg_%' AND schema_name != 'information_schema'"
            )
            return [r[0].lower() for r in cur.fetchall()
                    if r[0].lower() not in self._SYSTEM_SCHEMAS]

    def list_tables(self, conn, schema: str) -> list[str]:
        require_application_catalog(self, conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = %s AND table_type = 'BASE TABLE'",
                (schema,),
            )
            return [r[0].lower() for r in cur.fetchall()]

    # ------------------------------------------------------------------ #
    # Schema lifecycle                                                     #
    # ------------------------------------------------------------------ #

    def create_schema(self, conn, schema_name: str) -> None:
        if schema_name in self.list_schemas(conn):
            with conn.cursor() as cur:
                cur.execute(f'DROP SCHEMA "{schema_name}" CASCADE')
            conn.commit()
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema_name}"')
        conn.commit()

    def set_namespace(self, conn, schema_name: str):
        with conn.cursor() as cur:
            cur.execute(f'SET search_path = "{schema_name}", public')
        conn._statschema_db = schema_name
        return conn

    # ------------------------------------------------------------------ #
    # Catalog provisioning                                                 #
    # ------------------------------------------------------------------ #

    def provision(
        self,
        dba_conn,
        catalog: str,
        app_username: str,
        app_password: str,
    ) -> None:
        """Idempotently create *app_username* and *catalog*, grant access.

        *dba_conn* must be an open psycopg2 connection to the ``postgres``
        system catalog with superuser privileges.  All steps are idempotent
        and safe to re-run.

        SQL sequence adapted from lakeflow_connect/postgres/02_postgres_configure.sh.
        """
        import logging as _logging
        from psycopg2 import sql as _sql  # type: ignore

        _plog = _logging.getLogger(__name__)

        with dba_conn.cursor() as cur:
            # Step 1: introspect — list existing non-system users
            cur.execute(
                "SELECT usename FROM pg_user "
                "WHERE usename NOT IN ('azuresu','rdsadmin','replication')"
            )
            existing = [r[0] for r in cur.fetchall()]
            _plog.info("provision postgres: existing users: %s", existing)

            # Step 2: create user only if absent; always sync the password
            if app_username not in existing:
                cur.execute(
                    _sql.SQL("CREATE USER {} WITH PASSWORD %s").format(
                        _sql.Identifier(app_username)
                    ),
                    (app_password,),
                )
                _plog.info("provision postgres: created user %r", app_username)
            else:
                _plog.info("provision postgres: user %r already exists; syncing password", app_username)
                cur.execute(
                    _sql.SQL("ALTER USER {} WITH PASSWORD %s").format(
                        _sql.Identifier(app_username)
                    ),
                    (app_password,),
                )

        dba_conn.commit()

        # Step 3: create catalog — CREATE DATABASE must run outside a transaction
        dba_conn.autocommit = True
        try:
            with dba_conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM pg_database WHERE datname = %s", (catalog,)
                )
                if not cur.fetchone():
                    cur.execute(
                        _sql.SQL("CREATE DATABASE {} OWNER {}").format(
                            _sql.Identifier(catalog),
                            _sql.Identifier(app_username),
                        )
                    )
                    _plog.info("provision postgres: created catalog %r", catalog)
                else:
                    _plog.info("provision postgres: catalog %r already exists", catalog)
        finally:
            dba_conn.autocommit = False

        # Step 4: grant access on the catalog
        with dba_conn.cursor() as cur:
            cur.execute(
                _sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    _sql.Identifier(catalog), _sql.Identifier(app_username)
                )
            )
            cur.execute(
                _sql.SQL("GRANT ALL PRIVILEGES ON DATABASE {} TO {}").format(
                    _sql.Identifier(catalog), _sql.Identifier(app_username)
                )
            )
        dba_conn.commit()
        _plog.info(
            "provision postgres: user %r granted access to catalog %r",
            app_username, catalog,
        )

        # Step 5: verify — connect as the app user to confirm provisioning succeeded
        import psycopg2 as _psycopg2  # type: ignore
        host = dba_conn.info.host
        port = dba_conn.info.port
        try:
            test_conn = _psycopg2.connect(
                host=host, port=port,
                dbname=catalog, user=app_username, password=app_password,
            )
            test_conn.close()
            _plog.info(
                "provision postgres: login verified user=%r catalog=%r host=%s port=%s",
                app_username, catalog, host, port,
            )
        except _psycopg2.Error as exc:
            raise RuntimeError(
                f"provision postgres: login verification failed "
                f"(user={app_username!r} catalog={catalog!r} host={host} port={port}): {exc}"
            ) from exc


    # ------------------------------------------------------------------ #
    # Statistics                                                           #
    # ------------------------------------------------------------------ #

    def analyze(self, conn, tables: list, schema: str,
                pred_col_map: dict | None = None, full_stats: bool = False,
                tablesample_pct: float | None = None) -> None:
        stmts = [
            f'ANALYZE "{schema}"."{self.normalize_identifier(t.name)}"'
            for t in tables
        ]
        self.execute_statements(conn, stmts)

    # ------------------------------------------------------------------ #
    # DDL helpers                                                          #
    # ------------------------------------------------------------------ #

    def qualify_ddl(self, ddl_text: str, schema_name: str) -> str:
        return ddl_text  # PostgreSQL DDL emitter already handles schema namespacing

    def table_ref(self, table_name: str, schema_name: str) -> str:
        return f'"{schema_name}"."{self.normalize_identifier(table_name)}"'

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
    system_database         = "defaultdb"
    supports_extended_stats = False  # pg_statistic_ext not supported

    def autovacuum_disable_sql(self) -> str | None:
        # CockroachDB does not support the toast.autovacuum_enabled parameter.
        return "SET (autovacuum_enabled = false)"

    def configure_auto_stats(self, conn, enabled: bool) -> None:
        """Disable/enable CockroachDB automatic statistics via cluster setting.

        Must be bracketed around Phase D (load) and Phase E (EXPLAIN) so that
        auto-stats jobs cannot overwrite injected statistics between those phases.
        """
        flag = "true" if enabled else "false"
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    f"SET CLUSTER SETTING sql.stats.automatic_collection.enabled = {flag}"
                )
            conn.autocommit = False
        except Exception:
            pass  # non-admin role or older CRDB — proceed with possible stat flakiness

    def provision(
        self,
        dba_conn,
        catalog: str,
        app_username: str,
        app_password: str,
    ) -> None:
        """Idempotently create *app_username* and *catalog*, grant access.

        *dba_conn* must be an open psycopg2 connection to the ``defaultdb``
        system catalog as a superuser (root in --insecure mode).

        CockroachDB supports ``CREATE USER IF NOT EXISTS`` and
        ``CREATE DATABASE IF NOT EXISTS``, making this fully idempotent.
        """
        from psycopg2 import sql as _sql  # type: ignore
        import logging as _logging
        _clog = _logging.getLogger(__name__)

        # Step 1: create user (IF NOT EXISTS is idempotent).
        # CockroachDB in --insecure mode rejects ALTER USER ... WITH PASSWORD,
        # so we only create the user — no password management needed.
        with dba_conn.cursor() as cur:
            cur.execute(
                _sql.SQL("CREATE USER IF NOT EXISTS {}").format(
                    _sql.Identifier(app_username)
                )
            )
        dba_conn.commit()
        _clog.info("provision cockroachdb: user %r created/verified", app_username)

        # Step 2: create database (IF NOT EXISTS is idempotent).
        with dba_conn.cursor() as cur:
            cur.execute(
                _sql.SQL("CREATE DATABASE IF NOT EXISTS {}").format(
                    _sql.Identifier(catalog)
                )
            )
        dba_conn.commit()
        _clog.info("provision cockroachdb: catalog %r created/verified", catalog)

        # Step 3: grant full access on the catalog.
        with dba_conn.cursor() as cur:
            cur.execute(
                _sql.SQL("GRANT ALL ON DATABASE {} TO {}").format(
                    _sql.Identifier(catalog), _sql.Identifier(app_username)
                )
            )
        dba_conn.commit()
        _clog.info(
            "provision cockroachdb: user %r granted access to catalog %r",
            app_username, catalog,
        )

        # Step 4: verify — connect as the app user to confirm access works.
        import psycopg2 as _psycopg2  # type: ignore
        host = dba_conn.info.host
        port = dba_conn.info.port
        try:
            test_conn = _psycopg2.connect(
                host=host, port=port,
                dbname=catalog, user=app_username,
                sslmode="disable",
            )
            test_conn.close()
            _clog.info(
                "provision cockroachdb: login verified user=%r catalog=%r host=%s port=%s",
                app_username, catalog, host, port,
            )
        except Exception as exc:
            raise RuntimeError(
                f"provision cockroachdb: verification login failed "
                f"(user={app_username!r} catalog={catalog!r}): {exc}"
            ) from exc

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
