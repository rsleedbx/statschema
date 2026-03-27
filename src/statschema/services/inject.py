"""
Service: inject

Inject collected statistics into a target database catalog so the query
optimizer sees production-representative cardinalities before ``ANALYZE``
runs.  All callers go through this module rather than calling
``stats_injector`` functions directly.
"""

from __future__ import annotations

from typing import Any

from ..stats_model import TableStats
from ..stats_injector import (
    InjectionResult,
    inject_stats_postgres,
    inject_stats_mysql,
    inject_stats_oracle,
    inject_stats_sqlserver,
    inject_stats_databricks,
    inject_stats_db2,
)
from ..dialect_registry import normalize_dialect

# Dispatch table: canonical dialect → injector function.
# inject_stats_databricks has a different signature (spark session + canonical
# schema arg) so it is handled as a special case below.
_INJECTORS = {
    "postgres":    inject_stats_postgres,
    "mysql":       inject_stats_mysql,
    "oracle":      inject_stats_oracle,
    "sqlserver":   inject_stats_sqlserver,
    "db2":         inject_stats_db2,
}


def inject(
    conn: Any,
    stats: TableStats,
    dialect: str,
    schema: str = "public",
    *,
    spark: Any = None,
    canonical_schema: Any = None,
) -> InjectionResult:
    """Inject table statistics into a target database catalog.

    Parameters
    ----------
    conn:
        Open DBAPI-2 connection to the target database, or ``None`` for
        Databricks (pass ``spark`` instead).
    stats:
        :class:`~statschema.stats_model.TableStats` collected from the source.
    dialect:
        Target engine dialect (e.g. ``"postgres"``, ``"lakebase"``,
        ``"mysql"``).
    schema:
        Target schema name (default: ``"public"``).
    spark:
        Spark session — required when ``dialect`` is ``"databricks"`` or
        ``"lakebase"`` with the Databricks injector.
    canonical_schema:
        :class:`~statschema.model.CanonicalTableSchema` — required for
        Databricks injection.

    Returns
    -------
    InjectionResult
        Summary of what was injected, skipped, and any warnings.

    Raises
    ------
    ValueError
        If the dialect is not supported for injection.
    """
    d = normalize_dialect(dialect)

    if d == "databricks":
        if spark is None or canonical_schema is None:
            raise ValueError(
                "inject() with dialect='databricks' requires spark= and canonical_schema="
            )
        return inject_stats_databricks(spark, stats, canonical_schema)

    fn = _INJECTORS.get(d)
    if fn is None:
        raise ValueError(
            f"No stats injector registered for dialect {dialect!r} "
            f"(normalised: {d!r}).  Supported: {sorted(_INJECTORS)}"
        )

    return fn(conn, stats, schema)
