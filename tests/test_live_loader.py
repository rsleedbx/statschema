"""
Live bulk-load integration tests — OracleDirectPathLoader and DB2AdminCmdLoader.

Exercises the Phase 1 topology-aware registry: load_dataframe(..., ctx=ctx)
dispatches to the correct loader class instead of the old if-elif chain.

Oracle
------
OracleDirectPathLoader uses conn.direct_path_load() — a client-side bulk API
that works in all topologies (no server-side file required).
ctx = DeploymentContext(topology="remote") is sufficient.

DB2
---
DB2AdminCmdLoader stages a DEL file into the shared filesystem
(STATSCHEMA__SERVER_STAGING_DIR / STATSCHEMA__CLIENT_STAGING_DIR).
For the shared-fs path, construct DeploymentContext(topology="shared_fs", server_staging_dir=...).
Fallback to DB2MultiRowLoader is tested explicitly with topology="remote".

Prerequisites
-------------
  Oracle XE:  limactl start --name=oracle config/lima/oracle.yaml
  DB2 CE:     limactl start --name=db2    config/lima/db2.yaml
  .env:       STATSCHEMA__SERVER_STAGING_DIR=/tmp/lima/statschema
              STATSCHEMA__CLIENT_STAGING_DIR=/tmp/lima/statschema

Skip behaviour
--------------
Each engine's tests skip when the DB is unreachable or driver is absent.
"""

from __future__ import annotations

import os

import pandas as pd
import pytest

from src.statschema.data_loader import load_dataframe
from src.statschema.loader_context import DeploymentContext

# ---------------------------------------------------------------------------
# Oracle connection helpers
# ---------------------------------------------------------------------------

_ORA_HOST    = os.environ.get("ORACLE_HOST",    "127.0.0.1")
_ORA_PORT    = int(os.environ.get("ORACLE_PORT", "1521"))
_ORA_USER    = os.environ.get("ORACLE_USER",    "system")
_ORA_PASS    = os.environ.get("ORACLE_PASS",    "oracle")
_ORA_SERVICE = os.environ.get("ORACLE_SERVICE", "XE")
_ORA_SCHEMA  = "LOADER_TEST"
_ORA_SCHEMA_PASS = "LoaderTest1"


def _ora_conn(user=_ORA_USER, password=_ORA_PASS):
    oracledb = pytest.importorskip("oracledb")
    try:
        return oracledb.connect(
            user=user, password=password,
            dsn=f"{_ORA_HOST}:{_ORA_PORT}/{_ORA_SERVICE}",
        )
    except Exception as exc:
        pytest.skip(f"Cannot connect to Oracle: {exc}")


def _ora_exec(conn, sql: str):
    cur = conn.cursor()
    cur.execute(sql.rstrip(";").strip())
    conn.commit()


def _ora_exec_ignore(conn, sql: str, *ora_codes: int):
    try:
        _ora_exec(conn, sql)
    except Exception as exc:
        if any(f"ORA-{c:05d}" in str(exc) for c in ora_codes):
            return
        raise


# ---------------------------------------------------------------------------
# DB2 connection helpers
# ---------------------------------------------------------------------------

_DB2_HOST = os.environ.get("DB2_HOST",     "127.0.0.1")
_DB2_PORT = int(os.environ.get("DB2_PORT", "50000"))
_DB2_USER = os.environ.get("DB2_USER",     "db2inst1")
_DB2_PASS = os.environ.get("DB2_PASS",     "testpass")
_DB2_DB   = os.environ.get("DB2_DATABASE", "testdb")
_DB2_SCHEMA = _DB2_USER.upper()


def _db2_conn():
    ibm_db_dbi = pytest.importorskip("ibm_db_dbi")
    conn_str = (
        f"DATABASE={_DB2_DB};HOSTNAME={_DB2_HOST};PORT={_DB2_PORT};"
        f"PROTOCOL=TCPIP;UID={_DB2_USER};PWD={_DB2_PASS};"
    )
    try:
        return ibm_db_dbi.connect(conn_str, "", "")
    except Exception as exc:
        pytest.skip(f"Cannot connect to DB2: {exc}")


def _db2_exec(conn, sql: str):
    cur = conn.cursor()
    cur.execute(sql.rstrip(";").strip())
    conn.commit()


def _db2_exec_ignore(conn, sql: str, *sqlstates: str):
    try:
        _db2_exec(conn, sql)
    except Exception as exc:
        if any(s in str(exc) for s in sqlstates):
            return
        raise


# ---------------------------------------------------------------------------
# Sample DataFrame
# ---------------------------------------------------------------------------

def _sample_df(n: int = 50) -> pd.DataFrame:
    """Small DataFrame with int, float, and string columns.

    Column names are uppercase so _quote_id("ID", "db2") produces "ID" which
    matches DB2's uppercase-normalised column names in ALL_CAPS tables.
    """
    return pd.DataFrame({
        "ID":    list(range(1, n + 1)),
        "SCORE": [float(i) * 1.5 for i in range(1, n + 1)],
        "LABEL": [f"row_{i:04d}" for i in range(1, n + 1)],
    })


# ---------------------------------------------------------------------------
# Oracle fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ora_conn():
    """Module-scoped Oracle connection as a dedicated test user."""
    sys_conn = _ora_conn()
    _ora_exec_ignore(sys_conn, f"DROP USER {_ORA_SCHEMA} CASCADE", 1918)
    _ora_exec(sys_conn,
        f"CREATE USER {_ORA_SCHEMA} IDENTIFIED BY \"{_ORA_SCHEMA_PASS}\" "
        f"DEFAULT TABLESPACE USERS QUOTA UNLIMITED ON USERS"
    )
    _ora_exec(sys_conn,
        f"GRANT CREATE SESSION, CREATE TABLE, CREATE SEQUENCE TO {_ORA_SCHEMA}"
    )
    sys_conn.close()

    conn = _ora_conn(user=_ORA_SCHEMA, password=_ORA_SCHEMA_PASS)
    yield conn
    conn.close()

    cleanup = _ora_conn()
    _ora_exec_ignore(cleanup, f"DROP USER {_ORA_SCHEMA} CASCADE", 1918)
    cleanup.close()


# ---------------------------------------------------------------------------
# DB2 fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def db2_conn():
    """Module-scoped DB2 connection."""
    return _db2_conn()


# ---------------------------------------------------------------------------
# Oracle loader tests
# ---------------------------------------------------------------------------

class TestOracleDirectPathLoader:
    """OracleDirectPathLoader: conn.direct_path_load() via oracledb driver."""

    _TABLE = "loader_test_ora"

    def _setup_table(self, conn):
        _ora_exec_ignore(conn,
            f'DROP TABLE "{_ORA_SCHEMA}"."{self._TABLE.upper()}"',
            942,
        )
        _ora_exec(conn, f"""
            CREATE TABLE "{self._TABLE.upper()}" (
                "ID"    NUMBER(10)   NOT NULL,
                "SCORE" NUMBER(18,4)     NULL,
                "LABEL" VARCHAR2(50)     NULL
            )
        """)

    def test_direct_path_load_remote_ctx(self, ora_conn):
        """Direct-path load with topology=remote (the default for Oracle)."""
        self._setup_table(ora_conn)
        df = _sample_df(100)
        ctx = DeploymentContext(topology="remote")

        inserted = load_dataframe(
            df, ora_conn, self._TABLE, dialect="oracle", ctx=ctx,
        )

        assert inserted == 100
        cur = ora_conn.cursor()
        cur.execute(f'SELECT COUNT(*) FROM "{self._TABLE.upper()}"')
        assert cur.fetchone()[0] == 100

    def test_direct_path_load_remote_topology(self, ora_conn):
        """topology='remote' → direct_path_load (no staging file required)."""
        self._setup_table(ora_conn)
        df = _sample_df(50)

        ctx = DeploymentContext(topology="remote")
        assert ctx.topology == "remote"
        inserted = load_dataframe(
            df, ora_conn, self._TABLE, dialect="oracle", ctx=ctx,
        )
        assert inserted == 50

    def test_direct_path_load_null_values(self, ora_conn):
        """Rows with NULL score and label are handled correctly."""
        self._setup_table(ora_conn)
        df = pd.DataFrame({
            "ID":    [1, 2, 3],
            "SCORE": [1.5, None, 3.5],
            "LABEL": ["a", None, "c"],
        })
        ctx = DeploymentContext(topology="remote")

        inserted = load_dataframe(
            df, ora_conn, self._TABLE, dialect="oracle", ctx=ctx,
        )

        assert inserted == 3
        cur = ora_conn.cursor()
        cur.execute(f'SELECT id, score, label FROM "{self._TABLE.upper()}" ORDER BY id')
        rows = cur.fetchall()
        assert rows[1][1] is None   # score NULL
        assert rows[1][2] is None   # label NULL

    def test_dispatch_without_ctx_still_works(self, ora_conn):
        """Legacy path: load_dataframe without ctx uses the old oracledb inline branch."""
        self._setup_table(ora_conn)
        df = _sample_df(10)
        # No ctx argument — falls through to the inline oracledb check
        inserted = load_dataframe(df, ora_conn, self._TABLE, dialect="oracle")
        assert inserted == 10


# ---------------------------------------------------------------------------
# DB2 loader tests
# ---------------------------------------------------------------------------

class TestDB2AdminCmdLoader:
    """DB2AdminCmdLoader: DEL-format LOAD via a shared filesystem staging directory."""

    _TABLE = "loader_test_db2"

    def _setup_table(self, conn):
        _db2_exec_ignore(conn,
            f'DROP TABLE "{_DB2_SCHEMA}"."{self._TABLE.upper()}"',
            "42704", "42S02",
        )
        _db2_exec(conn, f"""
            CREATE TABLE "{_DB2_SCHEMA}"."{self._TABLE.upper()}" (
                "ID"    INTEGER      NOT NULL,
                "SCORE" DECIMAL(18,4)    NULL,
                "LABEL" VARCHAR(50)      NULL
            )
        """)

    def test_admin_cmd_loader_shared_fs(self, db2_conn):
        """
        DB2AdminCmdLoader via shared filesystem (zero-copy path).

        Requires STATSCHEMA__SERVER_STAGING_DIR to be set.  The staging file
        lands in the shared directory which the DB2 server process can read at
        the same (or translated) path.
        """
        staging = os.environ.get("STATSCHEMA__SERVER_STAGING_DIR", "")
        if not staging:
            pytest.skip("STATSCHEMA__SERVER_STAGING_DIR not set — skipping shared-fs test")

        self._setup_table(db2_conn)
        df = _sample_df(200)
        ctx = DeploymentContext(
            topology="shared_fs",
            server_staging_dir=staging,
            client_staging_dir=os.environ.get("STATSCHEMA__CLIENT_STAGING_DIR") or staging,
        )

        inserted = load_dataframe(
            df, db2_conn, self._TABLE, dialect="db2", ctx=ctx,
            col_types=["integer", "decimal", "string"],
        )

        assert inserted == 200
        cur = db2_conn.cursor()
        cur.execute(
            f'SELECT COUNT(*) FROM "{_DB2_SCHEMA}"."{self._TABLE.upper()}"'
        )
        assert cur.fetchone()[0] == 200

    def test_admin_cmd_loader_shared_fs(self, db2_conn):
        """
        topology='shared_fs' with server_staging_dir → DB2AdminCmdLoader selected.
        """
        staging_dir = os.environ.get("STATSCHEMA__SERVER_STAGING_DIR", "")
        if not staging_dir:
            pytest.skip("STATSCHEMA__SERVER_STAGING_DIR not set — skipping shared-fs test")

        self._setup_table(db2_conn)
        df = _sample_df(100)
        ctx = DeploymentContext(
            topology="shared_fs",
            server_staging_dir=staging_dir,
            client_staging_dir=os.environ.get("STATSCHEMA__CLIENT_STAGING_DIR") or staging_dir,
        )

        assert ctx.topology == "shared_fs"

        inserted = load_dataframe(
            df, db2_conn, self._TABLE, dialect="db2", ctx=ctx,
        )
        assert inserted == 100

    def test_multi_row_fallback_remote(self, db2_conn):
        """
        With topology=remote (no shared filesystem), DB2MultiRowLoader is selected.
        AdminCmdLoader.can_use() returns False → falls through to DB2MultiRowLoader.
        """
        self._setup_table(db2_conn)
        df = _sample_df(30)
        ctx = DeploymentContext(topology="remote")

        inserted = load_dataframe(
            df, db2_conn, self._TABLE, dialect="db2", ctx=ctx,
        )
        assert inserted == 30

    def test_clob_columns_skip_admin_cmd(self, db2_conn):
        """
        Tables with CLOB columns bypass ADMIN_CMD (DEL format doesn't support
        inline CLOB) and fall back to DB2MultiRowLoader automatically.
        """
        _db2_exec_ignore(db2_conn,
            f'DROP TABLE "{_DB2_SCHEMA}"."LOADER_CLOB_TEST"',
            "42704", "42S02",
        )
        _db2_exec(db2_conn, f"""
            CREATE TABLE "{_DB2_SCHEMA}"."LOADER_CLOB_TEST" (
                "ID"   INTEGER NOT NULL,
                "BODY" CLOB        NULL
            )
        """)
        df = pd.DataFrame({
            "ID":   [1, 2],
            "BODY": ["hello world", "foo bar"],
        })
        staging = os.environ.get("STATSCHEMA__SERVER_STAGING_DIR", "")
        if not staging:
            pytest.skip("STATSCHEMA__SERVER_STAGING_DIR not set — skipping CLOB test")
        ctx = DeploymentContext(
            topology="shared_fs",
            server_staging_dir=staging,
            client_staging_dir=os.environ.get("STATSCHEMA__CLIENT_STAGING_DIR") or staging,
        )

        inserted = load_dataframe(
            df, db2_conn, "LOADER_CLOB_TEST", dialect="db2", ctx=ctx,
            col_types=["integer", "string"],  # "string" triggers CLOB guard
        )
        assert inserted == 2
