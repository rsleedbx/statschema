"""
Live NeonDB integration tests — PostgreSQL wire protocol via Neon Local (Podman/Docker).

Image facts (Docker Hub)
------------------------
- **`neondatabase/neon`** ([Docker Hub](https://hub.docker.com/r/neondatabase/neon)) ships Neon
  storage/compute **binaries**; the default ``CMD`` is **pageserver** (ports 6400 / 9898), *not*
  a Postgres listener for app drivers. It is the wrong image for ``psycopg2`` DDL tests.
- **`neondatabase/neon_local`** ([Docker Hub](https://hub.docker.com/r/neondatabase/neon_local))
  is the local **proxy** that listens on **5432** and forwards to your Neon cloud project.
  Use this image for live tests. See [Neon Local docs](https://neon.tech/docs/local/neon-local).

Prerequisites
-------------
1. Neon project + API key in the Neon console.
2. Start Neon Local (Podman example — pick a free host port, e.g. 55433)::

    podman run -d --name neon-local \\
      -p 55433:5432 \\
      -e NEON_API_KEY=<your_api_key> \\
      -e NEON_PROJECT_ID=<your_project_id> \\
      docker.io/neondatabase/neon_local:latest

   Docker is equivalent; replace ``podman`` with ``docker`` if needed.

3. Default database name is often ``neondb`` (override with ``NEON_DATABASE`` if yours differs).

Environment variables (defaults)
----------------------------------
    NEON_DATABASE_URL  optional — full ``postgresql://…`` from Neon console or Neon Local
                         docs (overrides host/port/user/password/db/ssl when set)
    NEON_HOST          127.0.0.1
    NEON_LOCAL_PORT    55433
    NEON_USER          neon
    NEON_PASSWORD      npg
    NEON_DATABASE      neondb
    NEON_SSLMODE       require (default; Neon / Neon Local use TLS — ``prefer`` often fails)

Skip behaviour
--------------
Tests skip when ``psycopg2`` is missing or the endpoint is unreachable, so ``make test``
still passes without Neon.
"""

from __future__ import annotations

import textwrap

import pytest

from src.statschema import emit_ddl, parse_ddl
from tests.live_helpers import _tp

# ---------------------------------------------------------------------------
# Connection configuration
# ---------------------------------------------------------------------------

_p = _tp("test_neon")

_HOST     = _p.host     or "127.0.0.1"
_PORT     = _p.port     or 55433
_USER     = _p.username or "neon"
_PASSWORD = _p.password or "npg"
_DB       = _p.database or "neondb"
# RCA: Neon's serverless Postgres and Neon Local expect TLS; ``prefer`` often fails because
# libpq may not upgrade the session the way the proxy requires — use ``require`` by default.
_SSLMODE  = "require"
# Full DSN URL takes precedence when set (e.g. Neon cloud endpoint).
_NEON_URL = (_p.url or "").strip()


def _get_connection():
    psycopg2 = pytest.importorskip("psycopg2")
    errors: list[str] = []

    if _NEON_URL:
        try:
            conn = psycopg2.connect(_NEON_URL, connect_timeout=15)
            conn.autocommit = True
            return conn
        except Exception as exc:
            errors.append(f"NEON_DATABASE_URL: {exc}")

    ssl_modes: list[str] = []
    for mode in (_SSLMODE, "require", "prefer", "disable"):
        if mode not in ssl_modes:
            ssl_modes.append(mode)
    for sslmode in ssl_modes:
        try:
            conn = psycopg2.connect(
                host=_HOST,
                port=_PORT,
                user=_USER,
                password=_PASSWORD,
                dbname=_DB,
                connect_timeout=15,
                sslmode=sslmode,
            )
            conn.autocommit = True
            return conn
        except Exception as exc:
            errors.append(f"{_HOST}:{_PORT} db={_DB!r} sslmode={sslmode!r}: {exc}")

    pytest.skip(
        "Cannot connect to Neon (Neon Local or direct). Attempts:\n"
        + "\n".join(f"  • {e}" for e in errors)
        + "\n— Set NEON_DATABASE_URL to the full postgresql://… string from the Neon console "
        "(recommended).\n"
        f"— Or discrete vars: NEON_HOST={_HOST!r} NEON_LOCAL_PORT={_PORT} NEON_DATABASE={_DB!r}\n"
        "— Neon Local: podman run -d --name neon-local -p 55433:5432 "
        "-e NEON_API_KEY=… -e NEON_PROJECT_ID=… docker.io/neondatabase/neon_local:latest\n"
        "— RCA: Neon expects TLS; default sslmode is now ``require``. "
        "For a non-TLS proxy only, set NEON_SSLMODE=disable.\n"
        "See tests/test_live_neon.py header."
    )


def _execute(conn, sql: str):
    cur = conn.cursor()
    cur.execute(sql)
    return cur


def _fetchall(conn, sql: str, params=None) -> list[tuple]:
    cur = conn.cursor()
    cur.execute(sql, params or ())
    return cur.fetchall()


def _introspect(conn, schema: str, table: str) -> dict[str, dict]:
    rows = _fetchall(
        conn,
        """
        SELECT column_name,
               udt_name,
               character_maximum_length,
               numeric_precision,
               numeric_scale,
               is_nullable
        FROM   information_schema.columns
        WHERE  table_schema = %s
          AND  table_name   = %s
        ORDER  BY ordinal_position
    """,
        (schema, table),
    )
    return {
        row[0]: {
            "udt_name": row[1],
            "char_len": row[2],
            "num_prec": row[3],
            "num_scale": row[4],
            "nullable": row[5] == "YES",
        }
        for row in rows
    }


@pytest.fixture(scope="module")
def conn():
    """Module-scoped connection; skips if Neon Local is not reachable."""
    connection = _get_connection()
    _execute(connection, "DROP SCHEMA IF EXISTS live_test_neon CASCADE")
    _execute(connection, "CREATE SCHEMA live_test_neon")
    yield connection
    _execute(connection, "DROP SCHEMA IF EXISTS live_test_neon CASCADE")
    connection.close()


class TestNeonLocalConnection:
    def test_select_one(self, conn):
        rows = _fetchall(conn, "SELECT 1")
        assert rows[0][0] == 1

    def test_server_reports_postgres(self, conn):
        rows = _fetchall(conn, "SELECT current_setting('server_version')")
        version = rows[0][0]
        assert version
        major = int(version.split(".")[0])
        assert major >= 14, f"Unexpected server_version: {version}"


class TestNeonEmitDialectOnLiveNeon:
    """Validate ``emit_ddl(..., dialect='neon')`` + execute on Neon Local (PG-compatible)."""

    _SOURCE_DDL = textwrap.dedent("""\
        CREATE TABLE "orders" (
            "order_id"    SERIAL        NOT NULL,
            "customer_id" INTEGER       NOT NULL,
            "total"       NUMERIC(12,2)     NULL,
            "status"      VARCHAR(50)   NOT NULL DEFAULT 'pending',
            PRIMARY KEY ("order_id")
        );
    """)

    def test_parse_emit_neon_executes(self, conn):
        _execute(conn, 'DROP TABLE IF EXISTS live_test_neon."orders"')
        tables = parse_ddl(self._SOURCE_DDL, dialect="neon")
        assert tables
        sql = emit_ddl(tables[0], dialect="neon", if_not_exists=False)
        sql = sql.replace('"orders"', 'live_test_neon."orders"', 1)
        _execute(conn, sql)
        cols = _introspect(conn, "live_test_neon", "orders")
        assert "order_id" in cols
        assert cols["total"]["udt_name"] == "numeric"
        assert cols["total"]["num_prec"] == 12
        assert cols["total"]["num_scale"] == 2

    def test_neon_emit_matches_postgres_emit(self, conn):
        """Alias dialects should produce identical DDL text."""
        tables = parse_ddl(self._SOURCE_DDL, dialect="postgres")
        assert emit_ddl(tables[0], "neon", if_not_exists=False) == emit_ddl(
            tables[0], "postgres", if_not_exists=False
        )
