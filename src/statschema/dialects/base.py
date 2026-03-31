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


class DataLoader:
    """
    Abstract base class for per-dialect bulk loaders.

    Each dialect has exactly one ``DataLoader`` subclass.  The subclass declares
    which load methods it supports via ``_supported_methods`` and implements the
    corresponding slot methods.  The ``load()`` dispatcher reads ``ctx.loader``
    (set by ``ConnectionProfile`` or the ``STATSCHEMA__LOADER`` env var) and
    validates prerequisites before calling the slot — no silent fallback.

    Slot methods (override the ones your dialect supports)
    ------------------------------------------------------
    Bulk-file paths:
      client_stream       — stream from client (COPY … FROM STDIN, MySQL LOCAL INFILE)
      server_file         — server reads a file on shared FS (BULK INSERT, LOAD DATA)
      server_file_import  — server-side import command (DB2 IMPORT)
      native_bulk         — driver-native bulk API (mssql-python bulkcopy)
      cloud_staged        — write to cloud storage, DB ingests (Databricks COPY INTO)
      external_cli        — external binary (sqlldr, bcp CLI)

    Insert paths (used for testing / small loads; fallback never applies in core):
      row_insert          — single INSERT … VALUES (row)
      multi_row_insert    — INSERT … VALUES (r1),(r2),…
      batch_insert        — executemany / JDBC batch

    Class-level metadata (set per subclass)
    ----------------------------------------
    _supported_methods        — tuple of slot names this subclass implements.
    _method_prerequisites     — {slot: [ctx_attr, …]} — ctx attrs that must be
                                non-None for the slot to be invoked.
    _method_min_server_version — {slot: "version_string"} — minimum server
                                version required (compared lexicographically).

    ``__init_subclass__`` validates all three at class-definition time so
    mismatches are caught at import, not at runtime.
    """

    _loader_attr: str = "loader"          # attribute on ctx that holds the chosen slot name
    _supported_methods: tuple[str, ...] = ()
    _method_prerequisites:      dict[str, list[str]] = {}
    _method_min_server_version: dict[str, str]       = {}

    # All valid slot names — used by __init_subclass__ for validation
    _ALL_SLOTS: tuple[str, ...] = (
        "client_stream",
        "server_file",
        "server_file_import",
        "native_bulk",
        "cloud_staged",
        "external_cli",
        "row_insert",
        "multi_row_insert",
        "batch_insert",
    )

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)

        # Every declared method must be overridden in the subclass
        for method in cls._supported_methods:
            if method not in DataLoader._ALL_SLOTS:
                raise ValueError(
                    f"{cls.__name__}._supported_methods contains unknown slot "
                    f"{method!r}.  Valid slots: {DataLoader._ALL_SLOTS}"
                )
            if getattr(cls, method, None) is getattr(DataLoader, method, None):
                raise NotImplementedError(
                    f"{cls.__name__} lists {method!r} in _supported_methods "
                    f"but does not override DataLoader.{method}()."
                )

        # Prerequisites must reference declared methods
        for method in cls._method_prerequisites:
            if method not in cls._supported_methods:
                raise ValueError(
                    f"{cls.__name__}._method_prerequisites key {method!r} "
                    f"is not in _supported_methods."
                )

        # Version constraints must reference declared methods
        for method in cls._method_min_server_version:
            if method not in cls._supported_methods:
                raise ValueError(
                    f"{cls.__name__}._method_min_server_version key {method!r} "
                    f"is not in _supported_methods."
                )

    # --- Slot stubs (raise NotImplementedError; subclasses override) ----------

    def client_stream(self, ctx: Any, conn: Any, df: Any, table: str, col_names: list[str]) -> int:
        raise NotImplementedError  # pragma: no cover

    def server_file(self, ctx: Any, conn: Any, df: Any, table: str, col_names: list[str]) -> int:
        raise NotImplementedError  # pragma: no cover

    def server_file_import(self, ctx: Any, conn: Any, df: Any, table: str, col_names: list[str]) -> int:
        raise NotImplementedError  # pragma: no cover

    def native_bulk(self, ctx: Any, conn: Any, df: Any, table: str, col_names: list[str]) -> int:
        raise NotImplementedError  # pragma: no cover

    def cloud_staged(self, ctx: Any, conn: Any, df: Any, table: str, col_names: list[str]) -> int:
        raise NotImplementedError  # pragma: no cover

    def external_cli(self, ctx: Any, conn: Any, df: Any, table: str, col_names: list[str]) -> int:
        raise NotImplementedError  # pragma: no cover

    def row_insert(self, ctx: Any, conn: Any, df: Any, table: str, col_names: list[str]) -> int:
        raise NotImplementedError  # pragma: no cover

    def multi_row_insert(self, ctx: Any, conn: Any, df: Any, table: str, col_names: list[str]) -> int:
        raise NotImplementedError  # pragma: no cover

    def batch_insert(self, ctx: Any, conn: Any, df: Any, table: str, col_names: list[str]) -> int:
        raise NotImplementedError  # pragma: no cover

    # --- Dispatcher -----------------------------------------------------------

    def load(self, ctx: Any, conn: Any, df: Any, table: str, col_names: list[str]) -> int:
        """
        Dispatch to the slot named by ``ctx.loader``.

        Fails immediately (``RuntimeError``) if:
        - ``ctx.loader`` is not set
        - ``ctx.loader`` is not in ``_supported_methods``
        - Any prerequisite ``ctx`` attribute for the chosen slot is not set
        - Server version is below the minimum required for the chosen slot
        """
        chosen = getattr(ctx, self._loader_attr, None)
        if not chosen:
            raise RuntimeError(
                f"ctx.{self._loader_attr} is not set. "
                f"Set loader in statschema.yaml or STATSCHEMA__LOADER env var. "
                f"Supported by {type(self).__name__}: {self._supported_methods}"
            )
        if chosen not in self._supported_methods:
            raise RuntimeError(
                f"ctx.loader={chosen!r} is not supported by {type(self).__name__}. "
                f"Supported: {self._supported_methods}"
            )

        # Validate prerequisites
        missing = [
            attr
            for attr in self._method_prerequisites.get(chosen, [])
            if not getattr(ctx, attr, None)
        ]
        if missing:
            raise RuntimeError(
                f"ctx.loader={chosen!r} requires: "
                + ", ".join(f"ctx.{a}" for a in missing)
                + ".  Set the missing fields in statschema.yaml or via env vars."
            )

        # Validate minimum server version (lexicographic comparison is sufficient
        # for well-formed version strings like "2022", "23c", "16.3")
        min_ver = self._method_min_server_version.get(chosen)
        if min_ver:
            server_ver = getattr(ctx, "server_version", None)
            if server_ver and server_ver < min_ver:
                raise RuntimeError(
                    f"ctx.loader={chosen!r} requires server >= {min_ver!r}; "
                    f"ctx.server_version={server_ver!r}"
                )

        return getattr(self, chosen)(ctx, conn, df, table, col_names)

    # --- Compatibility shims for existing _LOADER_REGISTRY callers ------------

    def can_use(
        self,
        ctx: Any,
        dialect: str,
        col_types: list[str] | None,
    ) -> bool:
        """Return True if this loader handles *dialect*.  Subclasses override."""
        return False  # pragma: no cover

    def bulk_load(
        self,
        ctx: Any,
        conn: Any,
        df: Any,
        table: str,
        col_names: list[str],
        wait: bool = True,
    ) -> int:
        """Called by _LOADER_REGISTRY dispatch; delegates to ``load()``."""
        return self.load(ctx, conn, df, table, col_names)


# Backwards-compatible alias — existing subclasses ``class Foo(TopologyAwareLoader)``
# continue to work unchanged.
TopologyAwareLoader = DataLoader
