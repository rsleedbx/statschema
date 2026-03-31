"""Per-engine dialect adapters for the identity test."""
from benchmarks.dialects._base import IdentityDialect
from benchmarks.dialects.postgres import PostgresDialect, CockroachDBDialect
from benchmarks.dialects.mysql import MySQLDialect
from benchmarks.dialects.sqlserver import SQLServerDialect
from benchmarks.dialects.oracle import OracleDialect
from benchmarks.dialects.db2 import DB2Dialect


class ConnWrapper:
    """
    Thin wrapper around a DBAPI-2 connection that allows arbitrary attribute
    assignment on the wrapper object itself.

    C-extension connection types (psycopg2, mysqlclient, …) have no
    ``__dict__``, so benchmark code that tags connections with metadata like
    ``conn._statschema_db`` fails at runtime.  ``ConnWrapper`` stores those
    attributes in its own ``__dict__`` while delegating every unrecognised
    attribute and method lookup to the underlying raw connection.
    """

    def __init__(self, raw):
        object.__setattr__(self, "_raw", raw)

    def __getattr__(self, name: str):
        return getattr(object.__getattribute__(self, "_raw"), name)

    def __setattr__(self, name: str, value) -> None:
        raw = object.__getattribute__(self, "_raw")
        try:
            setattr(raw, name, value)
        except AttributeError:
            object.__setattr__(self, name, value)

    def __enter__(self):
        raw = object.__getattribute__(self, "_raw")
        raw.__enter__()
        return self

    def __exit__(self, *args):
        return object.__getattribute__(self, "_raw").__exit__(*args)

#: Registry mapping dialect name → adapter instance.
REGISTRY: dict[str, IdentityDialect] = {
    "postgres":    PostgresDialect(),
    "neon":        PostgresDialect(),
    "lakebase":    PostgresDialect(lakebase=True),
    "cockroachdb": CockroachDBDialect(),
    "mysql":       MySQLDialect(),
    "mariadb":     MySQLDialect(),
    "sqlserver":   SQLServerDialect(),
    "oracle":      OracleDialect(),
    "db2":         DB2Dialect(),
}


def get(dialect: str) -> IdentityDialect:
    if dialect not in REGISTRY:
        raise NotImplementedError(f"Unsupported dialect: {dialect!r}. "
                                  f"Known: {sorted(REGISTRY)}")
    return REGISTRY[dialect]
