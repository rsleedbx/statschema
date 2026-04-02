"""Abstract interface every identity-test dialect adapter must implement."""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

_log = logging.getLogger(__name__)


def require_application_catalog(dialect: "IdentityDialect", conn) -> None:
    """Raise if *conn* is pointed at the system catalog.

    ``list_schemas``, ``list_tables``, and ``create_schema`` must never run
    against a system catalog (master, postgres, mysql, …) because those
    catalogs contain infrastructure objects that must not be enumerated,
    modified, or dropped.

    For dialects where ``system_database`` is ``None`` (Oracle, DB2) there is
    only one catalog per connection so no guard is needed.
    """
    sys_db = getattr(dialect, "system_database", None)
    if sys_db is None:
        return
    current = getattr(conn, "_statschema_db", None)
    if current == sys_db:
        raise RuntimeError(
            f"{type(dialect).__name__}: introspection / schema lifecycle called "
            f"on system catalog '{sys_db}' — connect to the application catalog first"
        )


class DialectBase:
    """Concrete helpers inherited by every dialect implementation.

    Dialect classes should subclass ``DialectBase`` as well as satisfy the
    ``IdentityDialect`` protocol.  Because this is a plain class (not a
    Protocol), the concrete methods here are truly inherited and available on
    every dialect instance without per-dialect overrides.
    """

    # ------------------------------------------------------------------ #
    # Statement execution helper                                           #
    # ------------------------------------------------------------------ #

    def execute_statements(
        self,
        conn,
        statements: list[str],
        allow_fail: bool = False,
    ) -> int:
        """Execute each statement on *conn*, then commit.

        Parameters
        ----------
        allow_fail:
            False (default) — raise on the first failure; no commit is issued.
            True — on exception, print + log a warning, rollback, then
            continue to the next statement.

        Returns the count of statements that succeeded.
        """
        succeeded = 0
        with conn.cursor() as cur:
            for stmt in statements:
                if not allow_fail:
                    cur.execute(stmt)
                    succeeded += 1
                else:
                    try:
                        cur.execute(stmt)
                        succeeded += 1
                    except Exception as exc:
                        print(f"WARN execute_statements: {exc}")
                        _log.warning("execute_statements: %s", exc)
                        self.rollback(conn)
        self.commit(conn)
        return succeeded

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
        """Idempotently create the app user and catalog, then grant access.

        *dba_conn* must be an open PEP-249 connection to the system catalog
        (e.g. ``postgres`` for PostgreSQL, ``master`` for SQL Server).

        Override in dialect subclasses that support provisioning.  The default
        raises ``NotImplementedError``.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement provision()"
        )


@runtime_checkable
class IdentityDialect(Protocol):
    """
    One instance per engine family.  All methods receive an open connection
    produced by ``connect()`` from the same instance.

    Adding a new database engine means creating a new module here and
    registering it in ``benchmarks/dialects/__init__.py`` — no changes to
    the pipeline in ``identity_test.py``.
    """

    # ------------------------------------------------------------------ #
    # Dialect capabilities — read by identity_test.py, never branched on  #
    # dialect name strings                                                 #
    # ------------------------------------------------------------------ #

    #: The catalog (database) that is always present on a freshly started
    #: server and can be used to verify connectivity before the application
    #: catalog is created.  ``None`` means "use whatever is in the profile".
    system_database: str | None

    #: True → pass ``IF NOT EXISTS`` to the DDL emitter.
    ddl_if_not_exists: bool

    #: True → this dialect speaks the PostgreSQL wire protocol (psycopg2 driver,
    #: supports ``inject_stats_postgres``, autovacuum ALTER TABLE, etc.).
    is_pg_wire: bool

    #: True → pass column type hints to bulk-load so the driver can coerce
    #: non-string Python values into text columns (ibm_db_dbi / DB2).
    needs_column_types_for_bulk_load: bool

    #: sqlglot dialect token used when transpiling query SQL for EXPLAIN.
    #: ``None`` means "use the sqlglot default (ANSI-ish)".
    sqlglot_dialect: str | None

    #: True → ``connect_from_profile`` already owns the autocommit setting
    #: (e.g. SQL Server needs autocommit=True for DDL).  False → caller may
    #: set ``conn.autocommit = False`` after connecting.
    manages_own_autocommit: bool

    #: True → engine supports ``pg_statistic_ext`` / ``CREATE STATISTICS``
    #: functional-dependency stats.  PostgreSQL only (not CockroachDB).
    supports_extended_stats: bool

    def create_column_statistics_sql(
        self,
        schema: str,
        stat_name: str,
        col_a: str,
        col_b: str,
        table_name: str,
    ) -> str:
        """Return the DDL statement that creates a multi-column statistics object
        for the (*col_a*, *col_b*) pair in *table_name*.

        Only called when ``supports_extended_stats`` is True; all other dialects
        may raise ``NotImplementedError``.
        """
        ...

    # ------------------------------------------------------------------ #
    # Connection                                                           #
    # ------------------------------------------------------------------ #

    def connect_from_profile(self, profile) -> object:
        """Return an open PEP-249 connection from a ConnectionProfile.

        This is the canonical connection factory.  All dialect-specific
        setup (autocommit, connection templates, metadata attributes) lives
        here.  ``connect(dsn)`` parses the DSN and delegates to this method
        so the logic is never duplicated.
        """
        ...

    def connect(self, dsn: str) -> object:
        """Return an open PEP-249 connection for the given DSN string.

        Parses *dsn* into params and calls ``connect_from_profile``.
        """
        ...

    # ------------------------------------------------------------------ #
    # Transaction control                                                 #
    # ------------------------------------------------------------------ #

    def commit(self, conn) -> None:
        """Commit the current transaction, or no-op for autocommit connections."""
        ...

    def rollback(self, conn) -> None:
        """Roll back the current transaction, or no-op for autocommit connections."""
        ...

    # ------------------------------------------------------------------ #
    # Identifier normalisation                                            #
    # ------------------------------------------------------------------ #

    def normalize_identifier(self, name: str) -> str:
        """Return the canonical form of *name* as stored in the system catalog.

        Most engines preserve case; Oracle and DB2 fold to uppercase.
        Used wherever a table or column name must match a catalog lookup.
        """
        ...

    # ------------------------------------------------------------------ #
    # Query SQL rewriting                                                 #
    # ------------------------------------------------------------------ #

    def rewrite_query_sql(
        self,
        sql: str,
        schema: str,
        table_names: set[str],
        source_dialect: str,
    ) -> str:
        """Prepare *sql* for EXPLAIN on this engine.

        Implementations may transpile syntax (LIMIT → FETCH FIRST), strip
        quoting conventions (Oracle double-quotes), or qualify bare table
        references with *schema*.  The default is identity (return *sql*
        unchanged).
        """
        ...

    # ------------------------------------------------------------------ #
    # Autovacuum / auto-stats lifecycle                                   #
    # ------------------------------------------------------------------ #

    def autovacuum_disable_sql(self) -> str | None:
        """Return the ``ALTER TABLE … SET (…)`` fragment that disables
        autovacuum for a single table, or ``None`` if not applicable.

        PostgreSQL: ``'SET (autovacuum_enabled = false, toast.autovacuum_enabled = false)'``
        CockroachDB: ``'SET (autovacuum_enabled = false)'``
        All others: ``None``
        """
        ...

    def configure_auto_stats(self, conn, enabled: bool) -> None:
        """Enable or disable engine-level automatic statistics collection.

        No-op for all dialects except CockroachDB, which uses a cluster
        setting to prevent auto-stats jobs from overwriting injected stats
        between Phase D (load) and Phase E (EXPLAIN).
        """
        ...

    # ------------------------------------------------------------------ #
    # Predicate query-store kwargs                                        #
    # ------------------------------------------------------------------ #

    def predicate_query_store_kwargs(self, schema: str) -> dict:
        """Return extra kwargs for ``predicate_col_map_from_db``.

        Most dialects: ``{}`` (schema is implicit from the connection).
        MySQL / MariaDB: ``{"catalog": schema}`` (no schema concept; catalog == schema).
        Oracle: ``{"schema": schema}`` (schema == Oracle user / owner).
        """
        ...

    # ------------------------------------------------------------------ #
    # Catalog introspection                                               #
    # ------------------------------------------------------------------ #

    def list_schemas(self, conn) -> list[str]:
        """Return user-owned schema names in the connected catalog.

        Never includes system schemas (pg_catalog, information_schema,
        sys, dbo, SYSCAT, …).  Used by create_schema to discover what
        to drop before recreating, avoiding IF-EXISTS guards in DDL.
        """
        ...

    def list_tables(self, conn, schema: str) -> list[str]:
        """Return table names in *schema* within the connected catalog."""
        ...

    # ------------------------------------------------------------------ #
    # Schema lifecycle                                                     #
    # ------------------------------------------------------------------ #

    def create_schema(self, conn, schema_name: str) -> None:
        """Drop-and-recreate the test schema/database."""
        ...

    def set_namespace(self, conn, schema_name: str):
        """Set the active schema/database so unqualified names resolve.

        Returns the (possibly new) connection.  Always capture the return:

            conn = dialect.set_namespace(conn, schema_name)

        For SQL Server this creates a new connection to the target database
        (USE [db] is not supported on Azure SQL Database).  For all other
        dialects the session setting is applied to the existing connection and
        the same object is returned.
        """
        ...

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
        """Run the engine's native ANALYZE / RUNSTATS / GATHER_TABLE_STATS."""
        ...

    # ------------------------------------------------------------------ #
    # DDL helpers                                                          #
    # ------------------------------------------------------------------ #

    def qualify_ddl(self, ddl_text: str, schema_name: str) -> str:
        """Apply any engine-specific DDL transformations (quoting, schema prefix)."""
        ...

    def table_ref(self, table_name: str, schema_name: str) -> str:
        """Return a fully-qualified, properly-quoted table reference."""
        ...

    # ------------------------------------------------------------------ #
    # EXPLAIN                                                              #
    # ------------------------------------------------------------------ #

    def explain(self, conn, sql: str, schema: str) -> dict:
        """Run EXPLAIN and return a PG-compatible plan dict.

        The returned dict has at minimum:
            {"Node Type": str, "Plan Rows": int, "Plans": [child, ...]}
        """
        ...

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
        """Idempotently create the app user and catalog, then grant access.

        *dba_conn* must be connected to the system catalog.
        """
        ...
