"""
Protocol contracts for statschema dialect plugins.

Every database dialect implements four Protocols:

  StatsCollector  — reads per-column statistics from a live source DB
  StatsInjector   — writes collected statistics into a target DB catalog
  DDLEmitter      — renders canonical table schema as dialect DDL
  DataLoader      — bulk-loads a pandas DataFrame into a target DB table

Adding a new dialect
--------------------
1. Create ``src/statschema/dialects/<name>/`` with files that implement each
   Protocol you need.
2. Register the implementations in ``dialects/<name>/__init__.py`` via
   ``dialects.registry.register()``.
3. Add any alias mappings in ``dialects/registry.py`` (e.g. ``neon → postgres``).

No changes to core, services, CLI, or other dialect subpackages are required.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..stats_model import TableStats
from ..model import CanonicalTableSchema
from ..db_stats_collector import CollectionConfig
from ..stats_injector import InjectionResult
from ..data_loader import BatchConfig


@runtime_checkable
class StatsCollector(Protocol):
    """Reads column-level statistics from a live source database.

    Implementations query the source engine's catalog (``pg_stats``,
    ``information_schema``, ``ALL_TAB_COLUMNS``, etc.) and return a
    :class:`~statschema.stats_model.TableStats` object.

    ``conn`` is an open DBAPI-2 connection for the source engine.
    ``config`` is optional; pass ``None`` to use default collection settings.
    """

    def collect(
        self,
        conn: Any,
        table: str,
        config: CollectionConfig | None = None,
    ) -> TableStats:
        ...


@runtime_checkable
class StatsInjector(Protocol):
    """Writes collected statistics into a target database catalog.

    Implementations translate a :class:`~statschema.stats_model.TableStats`
    object into the target engine's catalog update calls (``pg_statistic``
    manipulation, ``DBMS_STATS``, ``UPDATE STATISTICS``, etc.).

    ``conn`` is an open DBAPI-2 connection for the target engine (or a Spark
    session for Databricks).  ``schema`` is the target schema name.
    """

    def inject(
        self,
        conn: Any,
        stats: TableStats,
        schema: str = "public",
    ) -> InjectionResult:
        ...


@runtime_checkable
class DDLEmitter(Protocol):
    """Renders a canonical table schema as dialect-specific DDL.

    Implementations produce a ``CREATE TABLE`` statement (and optionally
    ``COMMENT ON COLUMN`` statements) for the target dialect.
    """

    def emit_table(
        self,
        table: CanonicalTableSchema,
        if_not_exists: bool = True,
    ) -> str:
        ...


@runtime_checkable
class DataLoader(Protocol):
    """Bulk-loads a pandas DataFrame into a target database table.

    Phase 1 loaders implement both ``can_use()`` and the new ``bulk_load()``
    signature (ctx, conn, df, table, col_names).  Legacy loaders still expose
    the old signature (conn, df, table, config) — they are called directly
    from data_loader.py until fully migrated.

    Returns the number of rows loaded.
    """

    def can_use(
        self,
        ctx: Any,                    # DeploymentContext
        dialect: str,
        col_types: list[str] | None,
    ) -> bool:
        """Return True if this loader can handle the dialect + topology."""
        ...

    def bulk_load(
        self,
        ctx: Any,                    # DeploymentContext
        conn: Any,
        df: Any,                     # pandas.DataFrame
        table: str,
        col_names: list[str],
        wait: bool = True,
    ) -> int:
        ...


# ---------------------------------------------------------------------------
# Phase 1: topology-aware loader interface
# New loaders (DB2, Oracle, Spark, Lakehouse) implement these two methods.
# The old bulk_load(conn, df, table, config) signature stays on legacy loaders
# (postgres, mysql, sqlserver) until they are migrated.
# ---------------------------------------------------------------------------

class TopologyAwareLoader:
    """
    Base class (not a Protocol) for Phase 1 loaders.

    Subclass and implement ``can_use()`` and ``bulk_load()``.  Register
    instances in ``data_loader._LOADER_REGISTRY``.

    Class attribute ``loader_name`` identifies this loader for the
    ``STATSCHEMA_<DIALECT>_LOADER`` env var.  Use lower-case with underscores,
    e.g. ``"bcp"``, ``"bulk_insert"``, ``"copy_stdin"``.  An empty string means
    the loader has no stable name and cannot be selected by env var.
    """

    loader_name: str = ""

    def can_use(
        self,
        ctx: Any,                    # DeploymentContext
        dialect: str,
        col_types: list[str] | None,
    ) -> bool:
        """Return True if this loader can handle the dialect + topology."""
        return False  # pragma: no cover

    def bulk_load(
        self,
        ctx: Any,                    # DeploymentContext
        conn: Any,
        df: Any,                     # pandas.DataFrame
        table: str,
        col_names: list[str],
        wait: bool = True,
    ) -> int:
        """Load *df* into *table*. Returns row count."""
        raise NotImplementedError  # pragma: no cover
