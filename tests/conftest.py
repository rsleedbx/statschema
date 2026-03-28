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

import os

import pytest

from tests.live_helpers import make_helper, LiveDbHelper

# ---------------------------------------------------------------------------
# PostgreSQL — parametrized by version
# ---------------------------------------------------------------------------

_PG_VERSIONS = [
    ("14", int(os.environ.get("PG14_PORT", "5414"))),
    ("16", int(os.environ.get("PG16_PORT", "5416"))),
    ("18", int(os.environ.get("PG18_PORT", "5418"))),
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
    return make_helper("postgres", port=int(os.environ.get("PG18_PORT", "5418")))


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
    ("8.x",   int(os.environ.get("MYSQL8_PORT",   "3384"))),
]
_MARIADB_VERSIONS = [
    ("10.11", int(os.environ.get("MARIADB1011_PORT", "3311"))),
    ("11.4",  int(os.environ.get("MARIADB114_PORT",  "3340"))),
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
