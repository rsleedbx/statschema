"""Abstract interface every identity-test dialect adapter must implement."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


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
    # Connection                                                           #
    # ------------------------------------------------------------------ #

    def connect(self, dsn: str):
        """Return an open PEP-249 connection for the given DSN string."""
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
