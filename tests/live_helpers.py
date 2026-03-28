"""
tests/live_helpers.py

Dialect-aware database helper for live integration tests.
Encapsulates connection setup, query execution, and schema introspection so
that individual test files don't each need their own _get_connection / _execute
/ _fetchall / _introspect functions.

Each engine has a ``connect()`` function that:
  - imports the required driver via pytest.importorskip (skips on missing driver)
  - skips cleanly when the database is unreachable
  - returns a DBAPI-2 connection with autocommit on (unless the driver requires
    explicit commits, like Oracle)
"""

from __future__ import annotations

import os
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Per-engine connection factories
# ---------------------------------------------------------------------------

def connect_postgres(
    host: str | None = None,
    port: int | None = None,
    user: str | None = None,
    password: str | None = None,
    db: str | None = None,
    sslmode: str = "prefer",
):
    psycopg2 = pytest.importorskip("psycopg2")
    _host     = host     or os.environ.get("PG_HOST",     "127.0.0.1")
    _port     = port     or int(os.environ.get("PG18_PORT", os.environ.get("PG16_PORT", "5418")))
    _user     = user     or os.environ.get("PG_USER",     "postgres")
    _password = password or os.environ.get("PG18_PASS",   os.environ.get("PG_PASSWORD", "postgres"))
    _db       = db       or os.environ.get("PG_DB",       "postgres")
    try:
        conn = psycopg2.connect(
            host=_host, port=_port, user=_user, password=_password,
            dbname=_db, sslmode=sslmode, connect_timeout=5,
        )
        conn.autocommit = True
        return conn
    except Exception as exc:
        pytest.skip(f"Cannot connect to PostgreSQL on {_host}:{_port}: {exc}")


def connect_cockroachdb(
    host: str | None = None,
    port: int | None = None,
    user: str | None = None,
    db: str | None = None,
):
    psycopg2 = pytest.importorskip("psycopg2")
    _host = host or os.environ.get("CRDB_HOST",        "127.0.0.1")
    _port = port or int(os.environ.get("CRDB_SINGLE_PORT", "26257"))
    _user = user or os.environ.get("CRDB_USER",        "root")
    _db   = db   or os.environ.get("CRDB_DB",          "defaultdb")
    try:
        conn = psycopg2.connect(
            host=_host, port=_port, user=_user, dbname=_db,
            sslmode="disable", connect_timeout=5,
        )
        conn.autocommit = True
        return conn
    except Exception as exc:
        pytest.skip(f"Cannot connect to CockroachDB on {_host}:{_port}: {exc}")


def connect_mysql(
    host: str | None = None,
    port: int | None = None,
    user: str | None = None,
    password: str | None = None,
    db: str | None = None,
):
    pymysql = pytest.importorskip("pymysql")
    _host     = host     or os.environ.get("MYSQL_HOST",      "127.0.0.1")
    _port     = port     or int(os.environ.get("MYSQL8_PORT", "3384"))
    _user     = user     or os.environ.get("MYSQL_USER",      "root")
    _password = password or os.environ.get("MYSQL_ROOT_PASS", os.environ.get("MYSQL_PASS", "testpass"))
    _db       = db       or os.environ.get("MYSQL_DB",        "testdb")
    try:
        conn = pymysql.connect(
            host=_host, port=_port, user=_user, password=_password,
            database=_db, connect_timeout=5, autocommit=True,
        )
        return conn
    except Exception as exc:
        pytest.skip(f"Cannot connect to MySQL on {_host}:{_port}: {exc}")


def connect_mariadb(
    host: str | None = None,
    port: int | None = None,
    user: str | None = None,
    password: str | None = None,
    db: str | None = None,
):
    pymysql = pytest.importorskip("pymysql")
    _host     = host     or os.environ.get("MARIADB_HOST", "127.0.0.1")
    _port     = port     or int(os.environ.get("MARIADB_PORT", "3311"))
    _user     = user     or os.environ.get("MARIADB_USER", "root")
    _password = password or os.environ.get("MARIADB_PASS", os.environ.get("MYSQL_ROOT_PASS", "testpass"))
    _db       = db       or os.environ.get("MARIADB_DB",   "testdb")
    try:
        conn = pymysql.connect(
            host=_host, port=_port, user=_user, password=_password,
            database=_db, connect_timeout=5, autocommit=True,
        )
        return conn
    except Exception as exc:
        pytest.skip(f"Cannot connect to MariaDB on {_host}:{_port}: {exc}")


def connect_sqlserver(
    host: str | None = None,
    port: int | None = None,
    user: str | None = None,
    password: str | None = None,
    db: str | None = None,
):
    pymssql = pytest.importorskip("pymssql")
    _host     = host     or os.environ.get("SQLSERVER_HOST", "127.0.0.1")
    _port     = port     or int(os.environ.get("SQLSERVER_PORT", "14330"))
    _user     = user     or os.environ.get("SQLSERVER_USER", "sa")
    _password = password or os.environ.get("SQLSERVER_PASS", "")
    _db       = db       or os.environ.get("SQLSERVER_DB",   "master")
    if not _password:
        pytest.skip("SQLSERVER_PASS not set — skipping live SQL Server tests")
    try:
        conn = pymssql.connect(
            server=_host, port=_port, user=_user, password=_password,
            database=_db, login_timeout=5, tds_version="7.4",
        )
        return conn
    except Exception as exc:
        pytest.skip(f"Cannot connect to SQL Server on {_host}:{_port}: {exc}")


def connect_oracle(
    host: str | None = None,
    port: int | None = None,
    user: str | None = None,
    password: str | None = None,
    service: str | None = None,
):
    oracledb = pytest.importorskip("oracledb")
    _host     = host     or os.environ.get("ORACLE_HOST",    "127.0.0.1")
    _port     = port     or int(os.environ.get("ORACLE_PORT", "1521"))
    _user     = user     or os.environ.get("ORACLE_USER",    "system")
    _password = password or os.environ.get("ORACLE_PASS",    "")
    _service  = service  or os.environ.get("ORACLE_SERVICE", "XE")
    if not _password:
        pytest.skip("ORACLE_PASS not set — skipping live Oracle tests")
    try:
        conn = oracledb.connect(
            user=_user,
            password=_password,
            dsn=f"{_host}:{_port}/{_service}",
        )
        return conn
    except Exception as exc:
        pytest.skip(f"Cannot connect to Oracle ({_host}:{_port}/{_service}): {exc}")


def connect_db2(
    host: str | None = None,
    port: int | None = None,
    user: str | None = None,
    password: str | None = None,
    database: str | None = None,
):
    ibm_db_dbi = pytest.importorskip("ibm_db_dbi")
    _host     = host     or os.environ.get("DB2_HOST",     "127.0.0.1")
    _port     = port     or int(os.environ.get("DB2_PORT", "50000"))
    _user     = user     or os.environ.get("DB2_USER",     "db2inst1")
    _password = password or os.environ.get("DB2_PASS",     "testpass")
    _database = database or os.environ.get("DB2_DATABASE", "testdb")
    conn_str  = (
        f"DATABASE={_database};HOSTNAME={_host};PORT={_port};"
        f"PROTOCOL=TCPIP;UID={_user};PWD={_password};"
    )
    try:
        return ibm_db_dbi.connect(conn_str, "", "")
    except Exception as exc:
        pytest.skip(f"Cannot connect to Db2 on {_host}:{_port}: {exc}")


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

_CONNECT_FN = {
    "postgres":    connect_postgres,
    "cockroachdb": connect_cockroachdb,
    "neon":        connect_postgres,   # same protocol, uses different env vars
    "mysql":       connect_mysql,
    "mariadb":     connect_mariadb,
    "sqlserver":   connect_sqlserver,
    "oracle":      connect_oracle,
    "db2":         connect_db2,
}


def make_helper(dialect: str, **kwargs) -> LiveDbHelper:
    """
    Return a LiveDbHelper for the given dialect, skipping if unreachable.

    Extra kwargs are forwarded to the connection factory (e.g. port=5416).
    """
    fn = _CONNECT_FN.get(dialect)
    if fn is None:
        raise ValueError(f"Unknown dialect: {dialect!r}")
    conn = fn(**kwargs)
    return LiveDbHelper(conn, dialect)
