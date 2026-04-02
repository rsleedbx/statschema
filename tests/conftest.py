"""
tests/conftest.py

Session-scoped pytest fixtures that provide live database connections.
Each fixture skips gracefully when the required database is unreachable.

Usage in a test file:
    def test_something(pg_helper):
        rows = pg_helper.fetchall("SELECT version()")
        assert rows

The fixtures return a LiveDbHelper (see tests/live_helpers.py) which wraps a
DBAPI-2 connection with dialect-aware execute / fetchall / introspect helpers.
"""

from __future__ import annotations

import pytest

from tests.live_helpers import make_helper, LiveDbHelper, _tp

# ---------------------------------------------------------------------------
# PostgreSQL — parametrized by version
# ---------------------------------------------------------------------------

_PG_VERSIONS = [
    ("14", _tp("test_postgres14").port),
    ("16", _tp("test_postgres16").port),
    ("18", _tp("test_postgres18").port),
]


@pytest.fixture(
    scope="session",
    params=[pytest.param(v, id=f"pg{v[0]}") for v in _PG_VERSIONS],
)
def pg_helper(request) -> LiveDbHelper:
    _, port = request.param
    return make_helper("postgres", port=port)


@pytest.fixture(scope="session")
def pg18_helper() -> LiveDbHelper:
    """Single fixture for PostgreSQL 18 when version-parametrization is not needed."""
    return make_helper("postgres", port=_tp("test_postgres18").port)


# ---------------------------------------------------------------------------
# CockroachDB
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def crdb_helper() -> LiveDbHelper:
    return make_helper("cockroachdb")


# ---------------------------------------------------------------------------
# MySQL / MariaDB
# ---------------------------------------------------------------------------

_MYSQL_VERSIONS = [
    ("8.x", _tp("test_mysql8").port),
]
_MARIADB_VERSIONS = [
    ("10.11", _tp("test_mariadb_lts").port),
    ("11.4",  _tp("test_mariadb_new").port),
]


@pytest.fixture(
    scope="session",
    params=[pytest.param(v, id=f"mysql{v[0]}") for v in _MYSQL_VERSIONS],
)
def mysql_helper(request) -> LiveDbHelper:
    _, port = request.param
    return make_helper("mysql", port=port)


@pytest.fixture(
    scope="session",
    params=[pytest.param(v, id=f"mariadb{v[0]}") for v in _MARIADB_VERSIONS],
)
def mariadb_helper(request) -> LiveDbHelper:
    _, port = request.param
    return make_helper("mariadb", port=port)


# ---------------------------------------------------------------------------
# SQL Server
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def sqlserver_helper() -> LiveDbHelper:
    return make_helper("sqlserver")


# ---------------------------------------------------------------------------
# Oracle
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def oracle_helper() -> LiveDbHelper:
    return make_helper("oracle")


# ---------------------------------------------------------------------------
# IBM Db2
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def db2_helper() -> LiveDbHelper:
    return make_helper("db2")


# ---------------------------------------------------------------------------
# Neon
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def neon_helper() -> LiveDbHelper:
    return make_helper("neon")
