"""
tests/live_helpers.py

Dialect-aware database helper for live integration tests.
Encapsulates connection setup, query execution, and schema introspection so
that individual test files don't each need their own execute / fetchall /
introspect helpers.

``make_helper(dialect, **kwargs)`` loads a named profile from
``config/statschema.test.yaml``, applies any kwarg overrides (port, host,
user, password, db), delegates to the dialect's ``connect_from_profile``, and
returns a ``LiveDbHelper`` wrapping the connection.

Connection credentials are read from ``config/statschema.test.yaml`` via
``load_profile()``.  Do not add ``os.environ.get()`` calls here or in test
files — add the env-var reference to the YAML instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Profile loader
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
TEST_YAML  = str(_REPO_ROOT / "config" / "statschema.test.yaml")


def _tp(name: str):
    """Load a named profile from the test YAML.  Skip the test on failure."""
    from statschema.connection_profile import load_profile
    try:
        return load_profile(TEST_YAML, name)
    except Exception as exc:
        pytest.skip(f"Cannot load test profile {name!r} from {TEST_YAML}: {exc}")


# ---------------------------------------------------------------------------
# Dialect-aware LiveDbHelper
# ---------------------------------------------------------------------------

class LiveDbHelper:
    """
    Thin wrapper around a DBAPI-2 connection that adds dialect-aware helpers
    for executing SQL, fetching rows, and introspecting schemas.

    Use this in tests instead of calling the dialect-specific helpers directly.
    """

    def __init__(self, conn: Any, dialect: str):
        self.conn    = conn
        self.dialect = dialect

    # ── query execution ───────────────────────────────────────────────────────

    def execute(self, sql: str) -> Any:
        """Execute a single statement and return the cursor."""
        cur = self.conn.cursor()
        stmt = sql.rstrip(";").strip() if self.dialect in ("oracle", "db2") else sql
        cur.execute(stmt)
        if self.dialect not in ("postgres", "cockroachdb", "neon", "mysql", "mariadb"):
            self.conn.commit()
        return cur

    def execute_ignore(self, sql: str, *codes) -> None:
        """Execute SQL and silently swallow specific error codes/states."""
        try:
            self.execute(sql)
        except Exception as exc:
            msg = str(exc)
            if any(str(c) in msg for c in codes):
                return
            raise

    def fetchall(self, sql: str, params=None) -> list[tuple]:
        cur = self.conn.cursor()
        stmt = sql.rstrip(";").strip() if self.dialect in ("oracle", "db2") else sql
        cur.execute(stmt, params)
        return cur.fetchall()

    def fetchone(self, sql: str, params=None):
        rows = self.fetchall(sql, params)
        return rows[0] if rows else None

    # ── schema introspection ──────────────────────────────────────────────────

    def introspect(self, table: str, schema: str | None = None) -> dict[str, dict]:
        """
        Return a {column_name: metadata_dict} mapping for a table.
        The metadata dict is dialect-specific but always includes 'data_type'.
        """
        if self.dialect in ("postgres", "cockroachdb", "neon"):
            return self._introspect_pg(table, schema or "public")
        if self.dialect in ("mysql", "mariadb"):
            return self._introspect_mysql(table)
        if self.dialect == "sqlserver":
            return self._introspect_sqlserver(table, schema or "dbo")
        if self.dialect == "oracle":
            return self._introspect_oracle(table.upper())
        if self.dialect == "db2":
            return self._introspect_db2(table.upper())
        raise ValueError(f"introspect not implemented for dialect: {self.dialect!r}")

    def _introspect_pg(self, table: str, schema: str) -> dict[str, dict]:
        rows = self.fetchall("""
            SELECT column_name, udt_name, character_maximum_length,
                   numeric_precision, numeric_scale, is_nullable
            FROM   information_schema.columns
            WHERE  table_schema = %s AND table_name = %s
            ORDER  BY ordinal_position
        """, (schema, table))
        return {
            r[0]: {"data_type": r[1], "char_len": r[2], "num_prec": r[3],
                   "num_scale": r[4], "nullable": r[5] == "YES"}
            for r in rows
        }

    def _introspect_mysql(self, table: str) -> dict[str, dict]:
        rows = self.fetchall(f"""
            SELECT COLUMN_NAME, DATA_TYPE,
                   CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION,
                   NUMERIC_SCALE, IS_NULLABLE
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
            ORDER BY ORDINAL_POSITION
        """, (table,))
        return {
            r[0]: {"data_type": r[1], "char_len": r[2], "num_prec": r[3],
                   "num_scale": r[4], "nullable": r[5] == "YES"}
            for r in rows
        }

    def _introspect_sqlserver(self, table: str, schema: str) -> dict[str, dict]:
        rows = self.fetchall("""
            SELECT COLUMN_NAME, DATA_TYPE,
                   CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION,
                   NUMERIC_SCALE, IS_NULLABLE
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            ORDER BY ORDINAL_POSITION
        """, (schema, table))
        return {
            r[0]: {"data_type": r[1], "char_len": r[2], "num_prec": r[3],
                   "num_scale": r[4], "nullable": r[5] == "YES"}
            for r in rows
        }

    def _introspect_oracle(self, table: str) -> dict[str, dict]:
        rows = self.fetchall("""
            SELECT column_name, data_type, char_length,
                   data_precision, data_scale, nullable
            FROM   user_tab_columns
            WHERE  table_name = :1
            ORDER  BY column_id
        """, (table,))
        return {
            r[0]: {"data_type": r[1], "char_len": r[2], "num_prec": r[3],
                   "num_scale": r[4], "nullable": r[5] == "Y"}
            for r in rows
        }

    def _introspect_db2(self, table: str) -> dict[str, dict]:
        rows = self.fetchall("""
            SELECT colname, typename, length, scale, nulls
            FROM   syscat.columns
            WHERE  tabname = ?
            ORDER  BY colno
        """, (table,))
        return {
            r[0].lower(): {"data_type": r[1], "char_len": r[2],
                           "num_scale": r[3], "nullable": r[4] == "Y"}
            for r in rows
        }

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_DIALECT_PROFILE: dict[str, str] = {
    "postgres":    "test_postgres18",
    "cockroachdb": "test_cockroachdb",
    "neon":        "test_neon",
    "mysql":       "test_mysql8",
    "mariadb":     "test_mariadb_lts",
    "sqlserver":   "test_sqlserver",
    "oracle":      "test_oracle",
    "db2":         "test_db2",
}

_DIALECT_DRIVER: dict[str, str] = {
    "postgres":    "psycopg2",
    "cockroachdb": "psycopg2",
    "neon":        "psycopg2",
    "mysql":       "pymysql",
    "mariadb":     "pymysql",
    "sqlserver":   "mssql_python",
    "oracle":      "oracledb",
    "db2":         "ibm_db_dbi",
}


def make_helper(dialect: str, **kwargs) -> LiveDbHelper:
    """
    Return a LiveDbHelper for the given dialect, skipping if unreachable.

    Supported kwargs override profile fields: port, host, user, password, db.
    """
    driver = _DIALECT_DRIVER.get(dialect)
    if driver:
        pytest.importorskip(driver)
    profile_name = _DIALECT_PROFILE.get(dialect)
    if profile_name is None:
        raise ValueError(f"Unknown dialect: {dialect!r}")
    p = _tp(profile_name)
    if "port"     in kwargs: p.port     = kwargs["port"]
    if "host"     in kwargs: p.host     = kwargs["host"]
    if "user"     in kwargs: p.username = kwargs["user"]
    if "password" in kwargs: p.password = kwargs["password"]
    if "db"       in kwargs: p.database = kwargs["db"]
    from benchmarks.dialects import get as _get_dialect
    try:
        conn = _get_dialect(dialect).connect_from_profile(p)
    except Exception as exc:
        pytest.skip(f"Cannot connect to {dialect} on {p.host}:{p.port}: {exc}")
    return LiveDbHelper(conn, dialect)
