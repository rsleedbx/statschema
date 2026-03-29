"""Per-engine dialect adapters for the identity test."""
from benchmarks.dialects._base import IdentityDialect
from benchmarks.dialects.postgres import PostgresDialect, CockroachDBDialect
from benchmarks.dialects.mysql import MySQLDialect
from benchmarks.dialects.sqlserver import SQLServerDialect
from benchmarks.dialects.oracle import OracleDialect
from benchmarks.dialects.db2 import DB2Dialect

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
