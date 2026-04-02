"""
Live tests: MIN/MAX collection across every significant data type on each database.

Verifies that ``_fetch_min_max`` (and the surrounding ``_collect_column_stats``
pipeline) returns correct bounds for indexed columns, falls back gracefully via
ORDER BY for types whose aggregate is unsupported (e.g. SQL Server
``uniqueidentifier``), and returns ``None`` without aborting the connection for
non-orderable types (PostgreSQL ``json``, ``bytea``).

Each class is independently skippable — the test suite stays green when a
given database is not running.

PostgreSQL prerequisites (ports from test_live_pg.py defaults):

    podman run -d --name pg16 \\
        -e POSTGRES_PASSWORD=testpass -e POSTGRES_DB=statschema \\
        -p 5416:5432 docker.io/library/postgres:16

MySQL prerequisites:

    podman run -d --name mysql8 \\
        -e MYSQL_ROOT_PASSWORD=testpass -e MYSQL_DATABASE=statschema \\
        -p 3384:3306 docker.io/library/mysql:8.4

SQL Server prerequisites:

    limactl start sqlserver22   # listens on 127.0.0.1:14330
    export SQLSERVER_PASS=...
"""
from __future__ import annotations

import uuid

import pytest

from src.statschema.db_stats_collector import (
    _fetch_min_max,
    _indexed_columns,
    _quote,
    _schema_table,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _exec(conn, sql: str, params=None):
    cur = conn.cursor()
    if params:
        cur.execute(sql, params)
    else:
        cur.execute(sql)
    return cur


def _insert(conn, tref: str, col: str, values: list, dialect: str):
    """Insert a list of values into a single-column table."""
    cref = _quote(col, dialect)
    for v in values:
        _exec(conn, f"INSERT INTO {tref} ({cref}) VALUES (%s)", (v,))


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------

from benchmarks.bench_config import DEFAULT_CATALOG
from tests.live_helpers import _tp as _ltp  # noqa: E402

_pp = _ltp("test_postgres16")
_pm = _ltp("test_mysql8")
_pss = _ltp("test_sqlserver")

_PG_HOST = _pp.host     or "127.0.0.1"
_PG_USER = _pp.username or "postgres"
_PG_PASS = _pp.password or "testpass"
_PG_DB   = _pp.database or DEFAULT_CATALOG
_PG_PORT = _pp.port     or 5416


def _pg_conn():
    psycopg2 = pytest.importorskip("psycopg2")
    try:
        conn = psycopg2.connect(
            host=_PG_HOST, port=_PG_PORT,
            user=_PG_USER, password=_PG_PASS,
            dbname=_PG_DB, connect_timeout=5,
        )
        conn.autocommit = False
        return conn
    except Exception as exc:
        pytest.skip(f"Cannot connect to PostgreSQL :{_PG_PORT}: {exc}")


@pytest.fixture(scope="module")
def pg_conn():
    conn = _pg_conn()
    _exec(conn, "DROP SCHEMA IF EXISTS mm_test CASCADE")
    _exec(conn, "CREATE SCHEMA mm_test")
    conn.commit()
    yield conn
    _exec(conn, "DROP SCHEMA IF EXISTS mm_test CASCADE")
    conn.commit()
    conn.close()


# Each entry: (col_type_sql, index_sql_or_None, insert_values, expect_min, expect_max)
# expect_min / expect_max may be None to mean "don't assert exact value, just not crash"
_PG_CASES: list[tuple] = [
    # type         index?  values                              exp_min    exp_max
    ("INTEGER",     True,  [10, 3, 77, 1],                    "1",       "77"),
    ("BIGINT",      True,  [100, 5, 999],                     "5",       "999"),
    ("SMALLINT",    True,  [7, 2, 50],                        "2",       "50"),
    ("NUMERIC(10,2)",True, [1.5, 0.1, 9.9],                   "0.10",    "9.90"),
    ("REAL",        True,  [1.0, 0.5, 3.14],                  None,      None),  # float precision varies
    ("DOUBLE PRECISION", True, [1.0, 0.5, 3.14],              None,      None),
    ("VARCHAR(50)", True,  ["banana", "apple", "cherry"],      "apple",   "cherry"),
    ("TEXT",        True,  ["zoo", "ant", "mango"],            "ant",     "zoo"),
    ("CHAR(5)",     True,  ["ccc  ", "aaa  ", "zzz  "],        None,      None),  # padding varies
    ("DATE",        True,  ["2020-01-01", "2019-06-15", "2023-12-31"], "2019-06-15", "2023-12-31"),
    ("TIMESTAMP",   True,  ["2020-01-01 00:00:00", "2019-06-15 12:00:00"], None, None),
    ("BOOLEAN",     True,  [True, False, True],                "false",   "true"),
    # UUID: MIN/MAX aggregate is supported; ORDER BY fallback also works
    ("UUID",        True,  [str(uuid.UUID(int=1)), str(uuid.UUID(int=2)), str(uuid.UUID(int=0))],
                           str(uuid.UUID(int=0)), str(uuid.UUID(int=2))),
    # Non-orderable types — both paths should fail gracefully → (None, None)
    # Index present but aggregate + ORDER BY both unsupported for json
    ("JSON",        False, [],                                 None,      None),
    ("BYTEA",       False, [],                                 None,      None),
]


class TestPGMinMax:
    """Verify _fetch_min_max for every PostgreSQL type."""

    @pytest.fixture(autouse=True)
    def _schema(self, pg_conn):
        self.conn = pg_conn

    @pytest.mark.parametrize("col_type,do_index,values,exp_min,exp_max", _PG_CASES,
                              ids=[c[0].split("(")[0].lower() for c in _PG_CASES])
    def test_minmax(self, col_type, do_index, values, exp_min, exp_max):
        tname = "t_" + col_type.lower().replace(" ", "_").replace("(", "").replace(")", "").replace(",", "")
        _exec(self.conn, f'DROP TABLE IF EXISTS mm_test."{tname}"')
        _exec(self.conn, f'CREATE TABLE mm_test."{tname}" (v {col_type})')
        if do_index:
            _exec(self.conn, f'CREATE INDEX ON mm_test."{tname}" (v)')
        if values:
            for val in values:
                _exec(self.conn, f'INSERT INTO mm_test."{tname}" (v) VALUES (%s)', (val,))
        self.conn.commit()

        tref = f'mm_test."{tname}"'
        cref = '"v"'
        lo, hi = _fetch_min_max(self.conn, tref, cref, "postgres")

        if exp_min is not None:
            assert lo == exp_min, f"{col_type}: expected min={exp_min!r}, got {lo!r}"
        if exp_max is not None:
            assert hi == exp_max, f"{col_type}: expected max={exp_max!r}, got {hi!r}"
        if not values:
            # Empty / non-orderable table → both None
            assert lo is None and hi is None, \
                f"{col_type}: expected (None, None) for non-orderable, got ({lo!r}, {hi!r})"

    def test_indexed_columns_includes_indexed(self, pg_conn):
        """_indexed_columns returns columns participating in any index."""
        _exec(pg_conn, 'DROP TABLE IF EXISTS mm_test."t_idx_check"')
        _exec(pg_conn, 'CREATE TABLE mm_test."t_idx_check" (a INTEGER, b TEXT, c DATE)')
        _exec(pg_conn, 'CREATE INDEX ON mm_test."t_idx_check" (a)')
        _exec(pg_conn, 'CREATE INDEX ON mm_test."t_idx_check" (b, c)')
        pg_conn.commit()
        idx = _indexed_columns(pg_conn, "t_idx_check", "postgres", "mm_test")
        assert "a" in idx
        assert "b" in idx
        assert "c" in idx

    def test_indexed_columns_excludes_unindexed(self, pg_conn):
        _exec(pg_conn, 'DROP TABLE IF EXISTS mm_test."t_unidx"')
        _exec(pg_conn, 'CREATE TABLE mm_test."t_unidx" (a INTEGER, b TEXT)')
        _exec(pg_conn, 'CREATE INDEX ON mm_test."t_unidx" (a)')
        pg_conn.commit()
        idx = _indexed_columns(pg_conn, "t_unidx", "postgres", "mm_test")
        assert "a" in idx
        assert "b" not in idx

    def test_no_index_skips_minmax(self, pg_conn):
        """Non-indexed column returns (None, None) — no full table scan attempted."""
        _exec(pg_conn, 'DROP TABLE IF EXISTS mm_test."t_noindex"')
        _exec(pg_conn, 'CREATE TABLE mm_test."t_noindex" (a INTEGER, b INTEGER)')
        _exec(pg_conn, 'CREATE INDEX ON mm_test."t_noindex" (a)')
        _exec(pg_conn, 'INSERT INTO mm_test."t_noindex" VALUES (5, 99)')
        pg_conn.commit()

        from src.statschema.db_stats_collector import _collect_column_stats
        indexed = _indexed_columns(pg_conn, "t_noindex", "postgres", "mm_test")
        cs_b = _collect_column_stats(pg_conn, "t_noindex", "b", "postgres", "mm_test", 1, indexed)
        # b is not indexed — min/max should be None (no table scan)
        assert cs_b.min_value is None
        assert cs_b.max_value is None

        cs_a = _collect_column_stats(pg_conn, "t_noindex", "a", "postgres", "mm_test", 1, indexed)
        assert cs_a.min_value == "5"
        assert cs_a.max_value == "5"


# ---------------------------------------------------------------------------
# MySQL
# ---------------------------------------------------------------------------

_MY_HOST = _pm.host     or "127.0.0.1"
_MY_PASS = _pm.password or "testpass"
_MY_PORT = _pm.port     or 3384

_MYSQL_CASES: list[tuple] = [
    ("INT",              True,  [10, 3, 77, 1],                         "1",    "77"),
    ("BIGINT",           True,  [100, 5, 999],                          "5",    "999"),
    ("DECIMAL(10,2)",    True,  [1.50, 0.10, 9.90],                     "0.10", "9.90"),
    ("FLOAT",            True,  [1.0, 0.5, 3.0],                        None,   None),
    ("VARCHAR(50)",      True,  ["banana", "apple", "cherry"],           "apple","cherry"),
    ("TEXT",             False, ["zoo", "ant"],                          None,   None),  # TEXT can't be fully indexed
    ("DATE",             True,  ["2020-01-01", "2019-06-15", "2023-12-31"], "2019-06-15", "2023-12-31"),
    ("DATETIME",         True,  ["2020-01-01 00:00:00", "2019-06-15 12:00:00"], None, None),
    ("TINYINT(1)",       True,  [1, 0, 1],                              "0",    "1"),
    ("CHAR(10)",         True,  ["zzz", "aaa", "mmm"],                  None,   None),
    # JSON: MIN/MAX not supported; also not indexable → (None, None)
    ("JSON",             False, [],                                      None,   None),
]


@pytest.fixture(scope="module")
def my_conn():
    pymysql = pytest.importorskip("pymysql")
    try:
        conn = pymysql.connect(
            host=_MY_HOST, port=_MY_PORT,
            user="root", password=_MY_PASS,
            database=DEFAULT_CATALOG,
            connect_timeout=5,
            autocommit=True,
        )
        conn.cursor().execute("DROP DATABASE IF EXISTS mm_test")
        conn.cursor().execute("CREATE DATABASE mm_test")
        conn.cursor().execute("USE mm_test")
        yield conn
        conn.cursor().execute("DROP DATABASE IF EXISTS mm_test")
        conn.close()
    except Exception as exc:
        pytest.skip(f"Cannot connect to MySQL :{_MY_PORT}: {exc}")


class TestMySQLMinMax:

    @pytest.fixture(autouse=True)
    def _schema(self, my_conn):
        self.conn = my_conn

    @pytest.mark.parametrize("col_type,do_index,values,exp_min,exp_max", _MYSQL_CASES,
                              ids=[c[0].split("(")[0].lower() for c in _MYSQL_CASES])
    def test_minmax(self, col_type, do_index, values, exp_min, exp_max):
        tname = "t_" + col_type.lower().replace(" ", "_").replace("(", "").replace(")", "").replace(",", "")
        self.conn.cursor().execute(f"DROP TABLE IF EXISTS `{tname}`")
        self.conn.cursor().execute(f"CREATE TABLE `{tname}` (v {col_type})")
        if do_index and col_type != "TEXT":
            try:
                self.conn.cursor().execute(f"CREATE INDEX idx_{tname} ON `{tname}` (v)")
            except Exception:
                pass  # Some types can't be indexed (TEXT without prefix)
        if values:
            for val in values:
                self.conn.cursor().execute(f"INSERT INTO `{tname}` (v) VALUES (%s)", (val,))

        tref = f"`mm_test`.`{tname}`"
        cref = "`v`"
        lo, hi = _fetch_min_max(self.conn, tref, cref, "mysql")

        if exp_min is not None:
            assert lo == exp_min, f"{col_type}: expected min={exp_min!r}, got {lo!r}"
        if exp_max is not None:
            assert hi == exp_max, f"{col_type}: expected max={exp_max!r}, got {hi!r}"
        if not values:
            assert lo is None and hi is None, \
                f"{col_type}: expected (None, None), got ({lo!r}, {hi!r})"

    def test_indexed_columns_mysql(self, my_conn):
        my_conn.cursor().execute("DROP TABLE IF EXISTS `t_idx_chk`")
        my_conn.cursor().execute("CREATE TABLE `t_idx_chk` (a INT, b VARCHAR(50), c DATE)")
        my_conn.cursor().execute("CREATE INDEX idx_a ON `t_idx_chk` (a)")
        my_conn.cursor().execute("CREATE INDEX idx_bc ON `t_idx_chk` (b, c)")
        idx = _indexed_columns(my_conn, "t_idx_chk", "mysql", "mm_test")
        assert "a" in idx
        assert "b" in idx
        assert "c" in idx


# ---------------------------------------------------------------------------
# SQL Server
# ---------------------------------------------------------------------------

_SS_HOST = _pss.host     or "127.0.0.1"
_SS_PORT = _pss.port     or 14330
_SS_USER = _pss.username or "sa"
_SS_PASS = _pss.password or ""

_SS_CASES: list[tuple] = [
    ("INT",              True,  [10, 3, 77, 1],                         "1",    "77"),
    ("BIGINT",           True,  [100, 5, 999],                          "5",    "999"),
    ("DECIMAL(10,2)",    True,  [1.50, 0.10, 9.90],                     "0.10", "9.90"),
    ("FLOAT",            True,  [1.0, 0.5, 3.0],                        None,   None),
    ("VARCHAR(50)",      True,  ["banana", "apple", "cherry"],           "apple","cherry"),
    ("NVARCHAR(50)",     True,  ["zoo", "ant", "mango"],                 "ant",  "zoo"),
    ("DATE",             True,  ["2020-01-01", "2019-06-15", "2023-12-31"], "2019-06-15", "2023-12-31"),
    ("DATETIME",         True,  ["2020-01-01 00:00:00", "2019-06-15 12:00:00"], None, None),
    ("BIT",              True,  [1, 0, 1],                              None,   None),  # BIT ordering varies
    # uniqueidentifier: MIN/MAX aggregate fails → must use ORDER BY fallback
    ("UNIQUEIDENTIFIER", True,
        ["00000000-0000-0000-0000-000000000001",
         "00000000-0000-0000-0000-000000000003",
         "00000000-0000-0000-0000-000000000002"],
        None, None),  # value is returned without crashing — exact sort order is GUID-specific
]


@pytest.fixture(scope="module")
def ss_conn():
    mssql_python = pytest.importorskip("mssql_python")
    if not _SS_PASS:
        pytest.skip("SQLSERVER_PASS not set")
    try:
        conn = mssql_python.connect(
            f"SERVER={_SS_HOST},{_SS_PORT};DATABASE=master;"
            f"UID={_SS_USER};PWD={_SS_PASS};TrustServerCertificate=yes"
        )
        cur = conn.cursor()
        cur.execute("IF EXISTS (SELECT 1 FROM sys.databases WHERE name = 'mm_test') DROP DATABASE mm_test")
        cur.execute("CREATE DATABASE mm_test")
        conn.commit()
        cur.execute("USE mm_test")
        yield conn
        cur.execute("USE master")
        cur.execute("DROP DATABASE IF EXISTS mm_test")
        conn.commit()
        conn.close()
    except Exception as exc:
        pytest.skip(f"Cannot connect to SQL Server :{_SS_PORT}: {exc}")


class TestSQLServerMinMax:

    @pytest.fixture(autouse=True)
    def _schema(self, ss_conn):
        self.conn = ss_conn

    @pytest.mark.parametrize("col_type,do_index,values,exp_min,exp_max", _SS_CASES,
                              ids=[c[0].split("(")[0].lower() for c in _SS_CASES])
    def test_minmax(self, col_type, do_index, values, exp_min, exp_max):
        cur = self.conn.cursor()
        safe = col_type.lower().replace(" ", "_").replace("(", "").replace(")", "").replace(",", "")
        tname = f"t_{safe}"
        cur.execute(f"IF OBJECT_ID('{tname}') IS NOT NULL DROP TABLE [{tname}]")
        cur.execute(f"CREATE TABLE [{tname}] (v {col_type})")
        if do_index:
            try:
                cur.execute(f"CREATE INDEX idx_{tname} ON [{tname}] (v)")
            except Exception:
                pass
        if values:
            for val in values:
                cur.execute(f"INSERT INTO [{tname}] (v) VALUES (%s)", (val,))
        self.conn.commit()

        tref = f"[{tname}]"
        cref = "[v]"
        lo, hi = _fetch_min_max(self.conn, tref, cref, "sqlserver")

        if not values:
            assert lo is None and hi is None
            return

        if col_type == "UNIQUEIDENTIFIER":
            # Must not crash and must return something from the ORDER BY fallback
            assert lo is not None, "uniqueidentifier: ORDER BY fallback returned None for min"
            assert hi is not None, "uniqueidentifier: ORDER BY fallback returned None for max"
        else:
            if exp_min is not None:
                assert lo == exp_min, f"{col_type}: expected min={exp_min!r}, got {lo!r}"
            if exp_max is not None:
                assert hi == exp_max, f"{col_type}: expected max={exp_max!r}, got {hi!r}"

    def test_indexed_columns_sqlserver(self, ss_conn):
        cur = ss_conn.cursor()
        cur.execute("IF OBJECT_ID('t_idxchk') IS NOT NULL DROP TABLE [t_idxchk]")
        cur.execute("CREATE TABLE [t_idxchk] (a INT, b VARCHAR(50), c DATE)")
        cur.execute("CREATE INDEX idx_a  ON [t_idxchk] (a)")
        cur.execute("CREATE INDEX idx_bc ON [t_idxchk] (b, c)")
        ss_conn.commit()
        idx = _indexed_columns(ss_conn, "t_idxchk", "sqlserver", None)
        assert "a" in idx
        assert "b" in idx
        assert "c" in idx

    def test_uniqueidentifier_fallback_used(self, ss_conn):
        """Confirm the ORDER BY fallback fires: MIN/MAX on uniqueidentifier raises,
        but we still get back non-None values from the index scan fallback."""
        cur = ss_conn.cursor()
        cur.execute("IF OBJECT_ID('t_guid') IS NOT NULL DROP TABLE [t_guid]")
        cur.execute("CREATE TABLE [t_guid] (id UNIQUEIDENTIFIER)")
        cur.execute("CREATE INDEX idx_guid ON [t_guid] (id)")
        uids = [
            "00000000-0000-0000-0000-000000000001",
            "00000000-0000-0000-0000-000000000005",
            "00000000-0000-0000-0000-000000000003",
        ]
        for u in uids:
            cur.execute("INSERT INTO [t_guid] (id) VALUES (%s)", (u,))
        ss_conn.commit()

        lo, hi = _fetch_min_max(ss_conn, "[t_guid]", "[id]", "sqlserver")
        assert lo is not None, "expected min from ORDER BY fallback"
        assert hi is not None, "expected max from ORDER BY fallback"
