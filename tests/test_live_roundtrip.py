"""
Live database round-trip tests — DDL → YAML → DDL → YAML → DDL on real servers.

This is the integration layer that bridges the in-memory tests in
``test_ddl_roundtrip.py`` and the real world.  It answers:

  1. Does every emitted DDL string actually execute on a live database?
  2. Does the DDL → canonical YAML → DDL → canonical YAML → DDL chain
     produce the same schema after each emission? (double round-trip)
  3. Does a cross-dialect pipeline work end-to-end:
       MySQL DDL → canonical YAML → emit Postgres → run on PG
                                  → emit SQL Server → run on SQL Server
                                  → emit MySQL → run on MySQL
                                  → canonical YAML matches original

Test matrix
-----------
  Dialect    Live servers        Cases (Phase 1 + Phase 2)
  ─────────  ──────────────────  ───────────────────────────
  mysql      mysql57 (3357)      29 + 60 = 89 tables
             mysql8  (3384)
  postgres   pg14    (5414)      24 + 60 = 84 tables
             pg16    (5416)
  sqlserver  sqlserver22 (14330) 22 + 60 = 82 tables

  Cross-dialect pipeline runs on one representative table per dialect origin.

Prerequisites
-------------
See docs/local-databases.md for how to start the containers / VMs.

All tests auto-skip when a database is not reachable.

Environment variables
---------------------
  MYSQL_ROOT_PASS   default testpass
  MYSQL57_PORT      default 3357
  MYSQL8_PORT       default 3384
  PG_PASSWORD       default testpass
  PG14_PORT         default 5414
  PG16_PORT         default 5416
  SQLSERVER_PASS    required for SQL Server (no default — set from Lima VM log)
  SQLSERVER_PORT    default 14330
"""

from __future__ import annotations

import os
import textwrap
from typing import Any

import pytest

from src.schema_parser import (
    CanonicalTableSchema,
    dump_schema,
    emit_ddl,
    load_canonical,
    parse_ddl,
)
from tests.test_ddl_roundtrip import (
    _RANDOM_MYSQL,
    _RANDOM_POSTGRES,
    _RANDOM_SQLSERVER,
    _cases_mysql,
    _cases_postgres,
    _cases_sqlserver,
)

# ---------------------------------------------------------------------------
# Connection settings
# ---------------------------------------------------------------------------

_MYSQL_PASS   = os.environ.get("MYSQL_ROOT_PASS", "testpass")
_MYSQL57_PORT = int(os.environ.get("MYSQL57_PORT", "3357"))
_MYSQL8_PORT  = int(os.environ.get("MYSQL8_PORT",  "3384"))

_PG_PASS  = os.environ.get("PG_PASSWORD", "testpass")
_PG14_PORT = int(os.environ.get("PG14_PORT", "5414"))
_PG16_PORT = int(os.environ.get("PG16_PORT", "5416"))

_SS_PASS = os.environ.get("SQLSERVER_PASS", "")
_SS_PORT = int(os.environ.get("SQLSERVER_PORT", "14330"))

# ---------------------------------------------------------------------------
# Collect test cases from the in-memory round-trip suite
# ---------------------------------------------------------------------------

# Phase 1 tuples: (emit_dialect, parse_dialect, table)
# Phase 2 tuples: (table, emit_dialect, parse_dialect)  ← different order
_MYSQL_CASES:     list[CanonicalTableSchema] = (
    [t for _, _, t in _cases_mysql()]
    + [t for t, _, _ in _RANDOM_MYSQL]
)
_POSTGRES_CASES:  list[CanonicalTableSchema] = (
    [t for _, _, t in _cases_postgres()]
    + [t for t, _, _ in _RANDOM_POSTGRES]
)
_SQLSERVER_CASES: list[CanonicalTableSchema] = (
    [t for _, _, t in _cases_sqlserver()]
    + [t for t, _, _ in _RANDOM_SQLSERVER]
)

def _case_id(tbl: CanonicalTableSchema) -> str:
    return f"{tbl.name}-{','.join(c.type for c in tbl.columns)}"

_MYSQL_IDS     = [_case_id(t) for t in _MYSQL_CASES]
_POSTGRES_IDS  = [_case_id(t) for t in _POSTGRES_CASES]
_SQLSERVER_IDS = [_case_id(t) for t in _SQLSERVER_CASES]

# ---------------------------------------------------------------------------
# Low-level connection helpers (dialect-specific)
# ---------------------------------------------------------------------------

def _mysql_conn(port: int):
    pymysql = pytest.importorskip("pymysql")
    try:
        conn = pymysql.connect(
            host="127.0.0.1", port=port, user="root", password=_MYSQL_PASS,
            database="testdb", connect_timeout=5, autocommit=True,
        )
        return conn
    except Exception as exc:
        pytest.skip(f"MySQL unreachable on port {port}: {exc}")


def _pg_conn(port: int):
    psycopg2 = pytest.importorskip("psycopg2")
    try:
        conn = psycopg2.connect(
            host="127.0.0.1", port=port, user="postgres", password=_PG_PASS,
            dbname="testdb", connect_timeout=5,
        )
        conn.autocommit = True
        return conn
    except Exception as exc:
        pytest.skip(f"PostgreSQL unreachable on port {port}: {exc}")


def _ss_conn():
    if not _SS_PASS:
        pytest.skip("SQLSERVER_PASS env var not set")
    pymssql = pytest.importorskip("pymssql")
    try:
        conn = pymssql.connect(
            host="127.0.0.1", port=_SS_PORT, user="sa", password=_SS_PASS,
            database="master", as_dict=False, timeout=5, autocommit=True,
        )
        return conn
    except Exception as exc:
        pytest.skip(f"SQL Server unreachable on port {_SS_PORT}: {exc}")


# ---------------------------------------------------------------------------
# DDL execution helpers (dialect-aware)
# ---------------------------------------------------------------------------

def _exec_mysql(conn, sql: str):
    cur = conn.cursor()
    for stmt in sql.split(";"):
        s = stmt.strip()
        if s:
            cur.execute(s)


def _exec_pg(conn, sql: str):
    cur = conn.cursor()
    cur.execute(sql)


def _exec_ss(conn, sql: str):
    """Execute statements, sending BEGIN...END blocks as one batch."""
    cur = conn.cursor()
    if "BEGIN" in sql.upper() and "END" in sql.upper():
        cur.execute(sql)
        return
    for stmt in sql.split(";"):
        s = stmt.strip()
        if s:
            cur.execute(s)


# ---------------------------------------------------------------------------
# Generic "emit → execute → re-emit → verify idempotency" helper
# ---------------------------------------------------------------------------

def _live_double_roundtrip(
    exec_fn,           # callable(conn, sql)
    drop_fn,           # callable(conn, table_name) — DROP TABLE IF EXISTS
    table: CanonicalTableSchema,
    dialect: str,
    conn,
) -> None:
    """
    Core live round-trip assertion:
      1. emit DDL (ddl1)
      2. execute ddl1 on live DB  → proves DDL is valid SQL
      3. parse ddl1 → canonical2
      4. emit ddl2 from canonical2
      5. assert ddl1 == ddl2       → proves in-memory round-trip
      6. execute ddl2 on live DB  → proves round-tripped DDL also executes
      7. parse ddl2 → canonical3
      8. emit ddl3 from canonical3
      9. assert ddl2 == ddl3       → proves double round-trip stability
    """
    tname = table.name

    # ── Step 1: emit
    ddl1 = emit_ddl(table, dialect, if_not_exists=False)

    # ── Step 2: execute on live DB
    drop_fn(conn, tname)
    exec_fn(conn, ddl1)

    # ── Step 3–4: parse → re-emit
    tables2 = parse_ddl(ddl1, dialect)
    assert tables2, f"parse_ddl returned empty for dialect={dialect!r}\n{ddl1}"
    ddl2 = emit_ddl(tables2[0], dialect, if_not_exists=False)

    # ── Step 5: in-memory idempotency
    assert ddl1 == ddl2, (
        f"[{dialect}] DDL not idempotent after round-trip:\n"
        f"── Original emit ──\n{ddl1}\n── Re-emit ──\n{ddl2}"
    )

    # ── Step 6: execute round-tripped DDL
    drop_fn(conn, tname)
    exec_fn(conn, ddl2)

    # ── Steps 7–9: second round-trip
    tables3 = parse_ddl(ddl2, dialect)
    assert tables3
    ddl3 = emit_ddl(tables3[0], dialect, if_not_exists=False)
    assert ddl2 == ddl3, (
        f"[{dialect}] DDL not stable after second round-trip:\n"
        f"── ddl2 ──\n{ddl2}\n── ddl3 ──\n{ddl3}"
    )


# ---------------------------------------------------------------------------
# MySQL live round-trip
# ---------------------------------------------------------------------------

_MYSQL_VERSIONS = [
    pytest.param(("5.7", _MYSQL57_PORT), id="mysql57"),
    pytest.param(("8.x", _MYSQL8_PORT),  id="mysql8"),
]


@pytest.fixture(scope="module", params=_MYSQL_VERSIONS)
def mysql_conn(request):
    _ver, port = request.param
    conn = _mysql_conn(port)
    conn.cursor().execute("DROP DATABASE IF EXISTS live_rt")
    conn.cursor().execute("CREATE DATABASE live_rt")
    conn.select_db("live_rt")
    yield conn
    conn.cursor().execute("DROP DATABASE IF EXISTS live_rt")
    conn.close()


def _mysql_drop(conn, table: str):
    conn.cursor().execute(f"DROP TABLE IF EXISTS `{table}`")


@pytest.mark.parametrize("table", _MYSQL_CASES, ids=_MYSQL_IDS)
class TestMySQLLiveRoundtrip:
    """All Phase 1 + Phase 2 MySQL cases execute and double-round-trip on live MySQL."""

    def test_execute_and_double_roundtrip(self, mysql_conn, table):
        _live_double_roundtrip(_exec_mysql, _mysql_drop, table, "mysql", mysql_conn)


# ---------------------------------------------------------------------------
# PostgreSQL live round-trip
# ---------------------------------------------------------------------------

_PG_VERSIONS = [
    pytest.param(("14", _PG14_PORT), id="pg14"),
    pytest.param(("16", _PG16_PORT), id="pg16"),
]


@pytest.fixture(scope="module", params=_PG_VERSIONS)
def pg_conn(request):
    _ver, port = request.param
    conn = _pg_conn(port)
    conn.cursor().execute("DROP SCHEMA IF EXISTS live_rt CASCADE")
    conn.cursor().execute("CREATE SCHEMA live_rt")
    yield conn
    conn.cursor().execute("DROP SCHEMA IF EXISTS live_rt CASCADE")
    conn.close()


def _pg_drop(conn, table: str):
    conn.cursor().execute(f'DROP TABLE IF EXISTS live_rt."{table}"')


def _exec_pg_schema(conn, sql: str, table: str):
    """Execute PG SQL with the table name prefixed to live_rt schema."""
    sql_s = sql.replace(f'"{table}"', f'live_rt."{table}"', 1)
    conn.cursor().execute(sql_s)


@pytest.mark.parametrize("table", _POSTGRES_CASES, ids=_POSTGRES_IDS)
class TestPostgresLiveRoundtrip:
    """All Phase 1 + Phase 2 Postgres cases execute and double-round-trip on live PG."""

    def test_execute_and_double_roundtrip(self, pg_conn, table):
        tname = table.name
        ddl1 = emit_ddl(table, "postgres", if_not_exists=False)

        _pg_drop(pg_conn, tname)
        _exec_pg_schema(pg_conn, ddl1, tname)

        tables2 = parse_ddl(ddl1, "postgres")
        assert tables2
        ddl2 = emit_ddl(tables2[0], "postgres", if_not_exists=False)
        assert ddl1 == ddl2, (
            f"[postgres] DDL not idempotent:\n── Original ──\n{ddl1}\n── Re-emit ──\n{ddl2}"
        )

        _pg_drop(pg_conn, tname)
        _exec_pg_schema(pg_conn, ddl2, tname)

        tables3 = parse_ddl(ddl2, "postgres")
        assert tables3
        ddl3 = emit_ddl(tables3[0], "postgres", if_not_exists=False)
        assert ddl2 == ddl3, (
            f"[postgres] DDL not stable at second round-trip:\n── ddl2 ──\n{ddl2}\n── ddl3 ──\n{ddl3}"
        )


# ---------------------------------------------------------------------------
# SQL Server live round-trip
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ss_conn():
    conn = _ss_conn()
    cur = conn.cursor()
    cur.execute("IF DB_ID('live_rt') IS NOT NULL DROP DATABASE live_rt")
    cur.execute("CREATE DATABASE live_rt")
    cur.execute("USE live_rt")
    yield conn
    cur2 = conn.cursor()
    cur2.execute("USE master")
    cur2.execute("IF DB_ID('live_rt') IS NOT NULL DROP DATABASE live_rt")
    conn.close()


def _ss_drop(conn, table: str):
    conn.cursor().execute(f"IF OBJECT_ID(N'[{table}]', N'U') IS NOT NULL DROP TABLE [{table}]")


@pytest.mark.parametrize("table", _SQLSERVER_CASES, ids=_SQLSERVER_IDS)
class TestSQLServerLiveRoundtrip:
    """All Phase 1 + Phase 2 SQL Server cases execute and double-round-trip on live SQL Server."""

    def test_execute_and_double_roundtrip(self, ss_conn, table):
        _live_double_roundtrip(_exec_ss, _ss_drop, table, "sqlserver", ss_conn)


# ---------------------------------------------------------------------------
# Cross-dialect live pipeline
# ---------------------------------------------------------------------------
#
# Tests the "many DDL sources → one canonical YAML → many DDL targets" path
# end-to-end, executing each target DDL on a real database.
#
# Pipeline for each source table:
#   source DDL ──parse──► canonical YAML
#                               │
#          ┌────────────────────┼─────────────────────┐
#          ▼                    ▼                      ▼
#   emit MySQL DDL       emit Postgres DDL      emit SQL Server DDL
#   run on MySQL 5.7+8   run on PG 14+16        run on SQL Server 22
#   parse back           parse back             parse back
#          └────────────────────┼─────────────────────┘
#                          canonical YAML must be
#                          semantically equal across all three

_CROSS_TABLES: list[tuple[str, str]] = [
    # (source_ddl, source_dialect)
    (
        textwrap.dedent("""\
            CREATE TABLE `orders` (
              `id`          BIGINT        NOT NULL AUTO_INCREMENT,
              `customer_id` BIGINT        NOT NULL,
              `status`      VARCHAR(20)   NOT NULL DEFAULT 'pending',
              `total`       DECIMAL(12,2)     NULL,
              `notes`       TEXT              NULL,
              `created_at`  DATETIME          NULL,
              PRIMARY KEY (`id`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """),
        "mysql",
    ),
    (
        textwrap.dedent("""\
            CREATE TABLE "users" (
              "id"       BIGSERIAL      NOT NULL PRIMARY KEY,
              "email"    VARCHAR(255)   NOT NULL,
              "score"    NUMERIC(10,3)      NULL,
              "active"   BOOLEAN        NOT NULL DEFAULT TRUE,
              "created"  TIMESTAMP          NULL
            );
        """),
        "postgres",
    ),
    (
        textwrap.dedent("""\
            CREATE TABLE [products] (
              [id]    BIGINT        NOT NULL IDENTITY(1,1),
              [sku]   NVARCHAR(50)  NOT NULL,
              [price] DECIMAL(18,4)     NULL,
              [stock] INT           NOT NULL DEFAULT 0,
              CONSTRAINT PK_products PRIMARY KEY ([id])
            );
        """),
        "sqlserver",
    ),
]

_CROSS_IDS = [src_d + "-" + src_ddl.strip().split("\n")[0].strip().split()[2].strip("`\"[]")
              for src_ddl, src_d in _CROSS_TABLES]


@pytest.fixture(scope="module")
def cross_mysql(request):
    """Two MySQL connections for cross-dialect tests."""
    conns = []
    for port in [_MYSQL57_PORT, _MYSQL8_PORT]:
        try:
            c = _mysql_conn(port)
            c.cursor().execute("DROP DATABASE IF EXISTS cross_rt")
            c.cursor().execute("CREATE DATABASE cross_rt")
            c.select_db("cross_rt")
            conns.append(c)
        except Exception:
            pass
    if not conns:
        pytest.skip("No MySQL instances available for cross-dialect test")
    yield conns
    for c in conns:
        try:
            c.cursor().execute("DROP DATABASE IF EXISTS cross_rt")
            c.close()
        except Exception:
            pass


@pytest.fixture(scope="module")
def cross_pg(request):
    """Two PostgreSQL connections for cross-dialect tests."""
    conns = []
    for port in [_PG14_PORT, _PG16_PORT]:
        try:
            c = _pg_conn(port)
            c.cursor().execute("DROP SCHEMA IF EXISTS cross_rt CASCADE")
            c.cursor().execute("CREATE SCHEMA cross_rt")
            conns.append(c)
        except Exception:
            pass
    if not conns:
        pytest.skip("No PostgreSQL instances available for cross-dialect test")
    yield conns
    for c in conns:
        try:
            c.cursor().execute("DROP SCHEMA IF EXISTS cross_rt CASCADE")
            c.close()
        except Exception:
            pass


@pytest.fixture(scope="module")
def cross_ss(request):
    """SQL Server connection for cross-dialect tests."""
    conn = _ss_conn()
    cur = conn.cursor()
    cur.execute("IF DB_ID('cross_rt') IS NOT NULL DROP DATABASE cross_rt")
    cur.execute("CREATE DATABASE cross_rt")
    cur.execute("USE cross_rt")
    yield conn
    cur2 = conn.cursor()
    cur2.execute("USE master")
    cur2.execute("IF DB_ID('cross_rt') IS NOT NULL DROP DATABASE cross_rt")
    conn.close()


@pytest.mark.parametrize("src_ddl,src_dialect", _CROSS_TABLES, ids=_CROSS_IDS)
class TestCrossDialectLivePipeline:
    """
    Full cross-dialect DDL → YAML → DDL pipeline on real databases.

    Each test takes source DDL in one dialect, parses it to canonical YAML,
    emits DDL for all other dialects, executes on the corresponding live server,
    then parses the executed DDL back and verifies the canonical representation
    is consistent across all dialects.
    """

    def test_yaml_round_trip(self, src_ddl: str, src_dialect: str):
        """DDL → canonical YAML → reload YAML → DDL must be identical to original emit."""
        tables = parse_ddl(src_ddl, src_dialect)
        assert tables, f"parse_ddl returned empty for {src_dialect!r}"
        yaml_str = dump_schema(tables)
        reloaded = load_canonical(yaml_str)
        assert reloaded, "load_canonical returned empty"
        ddl_from_yaml = emit_ddl(reloaded[0], src_dialect, if_not_exists=False)
        ddl_direct    = emit_ddl(tables[0],   src_dialect, if_not_exists=False)
        assert ddl_from_yaml == ddl_direct, (
            f"[{src_dialect}] DDL via YAML differs from direct emit:\n"
            f"── via YAML ──\n{ddl_from_yaml}\n── direct ──\n{ddl_direct}"
        )

    def test_mysql_target_executes(self, cross_mysql, src_ddl: str, src_dialect: str):
        """Source DDL → canonical → MySQL DDL executes on live MySQL."""
        tables = parse_ddl(src_ddl, src_dialect)
        assert tables
        mysql_ddl = emit_ddl(tables[0], "mysql", if_not_exists=False)
        tname = tables[0].name
        for conn in cross_mysql:
            conn.cursor().execute(f"DROP TABLE IF EXISTS `{tname}`")
            _exec_mysql(conn, mysql_ddl)
            # parse back and verify column count preserved
            rt = parse_ddl(mysql_ddl, "mysql")
            assert rt
            assert len(rt[0].columns) == len(tables[0].columns), (
                f"Column count changed after MySQL emit: "
                f"{[c.name for c in tables[0].columns]} → {[c.name for c in rt[0].columns]}"
            )

    def test_postgres_target_executes(self, cross_pg, src_ddl: str, src_dialect: str):
        """Source DDL → canonical → Postgres DDL executes on live PostgreSQL."""
        tables = parse_ddl(src_ddl, src_dialect)
        assert tables
        pg_ddl = emit_ddl(tables[0], "postgres", if_not_exists=False)
        tname = tables[0].name
        for conn in cross_pg:
            conn.cursor().execute(f'DROP TABLE IF EXISTS cross_rt."{tname}"')
            sql_s = pg_ddl.replace(f'"{tname}"', f'cross_rt."{tname}"', 1)
            conn.cursor().execute(sql_s)
            rt = parse_ddl(pg_ddl, "postgres")
            assert rt
            assert len(rt[0].columns) == len(tables[0].columns), (
                f"Column count changed after Postgres emit"
            )

    def test_sqlserver_target_executes(self, cross_ss, src_ddl: str, src_dialect: str):
        """Source DDL → canonical → SQL Server DDL executes on live SQL Server."""
        tables = parse_ddl(src_ddl, src_dialect)
        assert tables
        ss_ddl = emit_ddl(tables[0], "sqlserver", if_not_exists=False)
        tname = tables[0].name
        cross_ss.cursor().execute(
            f"IF OBJECT_ID(N'[{tname}]', N'U') IS NOT NULL DROP TABLE [{tname}]"
        )
        _exec_ss(cross_ss, ss_ddl)
        rt = parse_ddl(ss_ddl, "sqlserver")
        assert rt
        assert len(rt[0].columns) == len(tables[0].columns)

    def test_canonical_column_count_consistent_across_dialects(
        self, src_ddl: str, src_dialect: str
    ):
        """
        Emit for MySQL, Postgres, SQL Server, parse each back to canonical —
        all three must produce the same column count and column names.
        """
        tables = parse_ddl(src_ddl, src_dialect)
        assert tables
        original_cols = [c.name for c in tables[0].columns]

        for target in ("mysql", "postgres", "sqlserver"):
            target_ddl = emit_ddl(tables[0], target, if_not_exists=False)
            rt = parse_ddl(target_ddl, target)
            assert rt, f"parse_ddl empty for target={target!r}\nDDL: {target_ddl}"
            rt_cols = [c.name for c in rt[0].columns]
            assert rt_cols == original_cols, (
                f"Column names changed: {original_cols} → {rt_cols} "
                f"(source={src_dialect!r} → target={target!r})"
            )

    def test_double_yaml_round_trip(self, src_ddl: str, src_dialect: str):
        """
        DDL → YAML → DDL → YAML → DDL: the final DDL must equal the first DDL
        (verifies the canonical model is stable after two serialization cycles).
        """
        tables1 = parse_ddl(src_ddl, src_dialect)
        assert tables1
        yaml1   = dump_schema(tables1)
        tables2 = load_canonical(yaml1)
        ddl1    = emit_ddl(tables2[0], src_dialect, if_not_exists=False)
        yaml2   = dump_schema(tables2)
        tables3 = load_canonical(yaml2)
        ddl2    = emit_ddl(tables3[0], src_dialect, if_not_exists=False)
        assert ddl1 == ddl2, (
            f"[{src_dialect}] DDL differs after double YAML round-trip:\n"
            f"── After 1st cycle ──\n{ddl1}\n── After 2nd cycle ──\n{ddl2}"
        )
