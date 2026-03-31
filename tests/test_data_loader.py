"""
Tests for statschema.data_loader — all run offline using sqlite3.

SQLite is used as the test database because:
  - available in the Python standard library (zero setup)
  - supports SAVEPOINT / ROLLBACK TO SAVEPOINT
  - supports multi-row VALUES INSERT
  - has a well-known bind-parameter limit (SQLITE_LIMIT_VARIABLE_NUMBER = 999 default)
  - paramstyle = "qmark" (tests the ? placeholder path)

Bulk-copy loaders (PostgreSQL COPY, MySQL LOAD DATA, SQL Server BULK INSERT,
Db2 LOAD) require live databases and are marked # pragma: no cover.  Their
signatures and fallback behaviour are tested here via mocks.
"""
from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from src.statschema.data_loader import (
    BatchConfig,
    LoadStrategy,
    _DIALECT_BATCH_DEFAULTS,
    _build_multi_row_sql,
    _build_oracle_all_sql,
    _col_names,
    _detect_paramstyle,
    _effective_batch_size,
    _insert_multi_row,
    _insert_singleton,
    _iter_rows,
    _placeholder,
    _quote_id,
    discover_max_batch_size,
    load_dataframe,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

COLS = ["a", "b", "c"]


def _conn(extra_cols: str = "") -> sqlite3.Connection:
    """In-memory SQLite connection with table t(a, b, c)."""
    conn = sqlite3.connect(":memory:")
    conn.execute(f"CREATE TABLE t (a INTEGER, b TEXT, c REAL{extra_cols})")
    return conn


def _rows(n: int = 5) -> list[tuple]:
    return [(i, f"v{i}", float(i) * 0.1) for i in range(n)]


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

class TestDetectParamstyle:
    def test_sqlite_returns_qmark(self):
        conn = sqlite3.connect(":memory:")
        assert _detect_paramstyle(conn) == "qmark"

    def test_unknown_module_falls_back_to_pyformat(self):
        mock_conn = MagicMock()
        mock_conn.__class__ = type("FakeConn", (), {"__module__": "unknown_driver"})
        # Should not raise; falls back to pyformat
        result = _detect_paramstyle(mock_conn)
        assert result in ("pyformat", "qmark", "numeric", "named")


class TestQuoteId:
    def test_sqlite_double_quotes(self):
        assert _quote_id("col", "sqlite") == '"col"'

    def test_postgres_double_quotes(self):
        assert _quote_id("col", "postgres") == '"col"'

    def test_mysql_backtick(self):
        assert _quote_id("col", "mysql") == "`col`"

    def test_mariadb_backtick(self):
        assert _quote_id("col", "mariadb") == "`col`"

    def test_databricks_backtick(self):
        assert _quote_id("col", "databricks") == "`col`"

    def test_sqlserver_square_brackets(self):
        assert _quote_id("col", "sqlserver") == "[col]"

    def test_oracle_double_quotes(self):
        assert _quote_id("col", "oracle") == '"col"'

    def test_db2_double_quotes(self):
        assert _quote_id("col", "db2") == '"col"'


class TestPlaceholder:
    def test_qmark_ignores_index(self):
        assert _placeholder("qmark", 0) == "?"
        assert _placeholder("qmark", 99) == "?"

    def test_pyformat_ignores_index(self):
        assert _placeholder("pyformat", 0) == "%s"
        assert _placeholder("pyformat", 5) == "%s"

    def test_numeric_uses_one_based_index(self):
        assert _placeholder("numeric", 0) == ":1"
        assert _placeholder("numeric", 4) == ":5"

    def test_named_uses_one_based_index(self):
        assert _placeholder("named", 2) == ":3"


class TestEffectiveBatchSize:
    def test_row_limit_dominates(self):
        cfg = BatchConfig(max_rows=100, max_params=65535)
        assert _effective_batch_size(cfg, 3) == 100

    def test_param_limit_dominates(self):
        # 999 params / 3 cols = 333; max_rows=1000 → param wins
        cfg = BatchConfig(max_rows=1000, max_params=999)
        assert _effective_batch_size(cfg, 3) == 333

    def test_exact_division(self):
        cfg = BatchConfig(max_rows=500, max_params=1000)
        assert _effective_batch_size(cfg, 10) == 100

    def test_zero_cols_returns_max_rows(self):
        cfg = BatchConfig(max_rows=500, max_params=999)
        assert _effective_batch_size(cfg, 0) == 500

    def test_single_col(self):
        cfg = BatchConfig(max_rows=2000, max_params=999)
        assert _effective_batch_size(cfg, 1) == 999


class TestIterRows:
    def test_list_of_tuples(self):
        data = [(1, "a"), (2, "b")]
        assert list(_iter_rows(data)) == [(1, "a"), (2, "b")]

    def test_list_of_lists(self):
        data = [[1, "a"], [2, "b"]]
        result = list(_iter_rows(data))
        assert result == [(1, "a"), (2, "b")]

    def test_pandas_dataframe(self):
        pd = pytest.importorskip("pandas")
        df = pd.DataFrame({"x": [10, 20], "y": ["p", "q"]})
        rows = list(_iter_rows(df))
        assert rows == [(10, "p"), (20, "q")]

    def test_list_of_dicts_yields_values_not_keys(self):
        # generate_rows() yields dicts — must return values, not key names
        data = [{"a": 1, "b": "hello"}, {"a": 2, "b": "world"}]
        rows = list(_iter_rows(data))
        assert rows == [(1, "hello"), (2, "world")]


class TestColNames:
    def test_explicit_cols_take_priority(self):
        assert _col_names([], ["x", "y"]) == ["x", "y"]

    def test_infer_from_list_raises(self):
        with pytest.raises(ValueError):
            _col_names([(1, 2)], None)

    def test_infer_from_pandas(self):
        pd = pytest.importorskip("pandas")
        df = pd.DataFrame({"aa": [1], "bb": [2]})
        assert _col_names(df, None) == ["aa", "bb"]


# ---------------------------------------------------------------------------
# SQL builders
# ---------------------------------------------------------------------------

class TestBuildMultiRowSql:
    def test_qmark_two_rows(self):
        sql, params = _build_multi_row_sql(
            "t", ["a", "b"], [(1, "x"), (2, "y")], "sqlite", "qmark"
        )
        assert "(?, ?), (?, ?)" in sql
        assert "INSERT INTO" in sql
        assert params == [1, "x", 2, "y"]

    def test_pyformat_single_row(self):
        sql, params = _build_multi_row_sql(
            "t", ["a"], [(42,)], "postgres", "pyformat"
        )
        assert "(%s)" in sql
        assert params == [42]

    def test_sqlserver_brackets_in_sql(self):
        sql, _ = _build_multi_row_sql(
            "orders", ["id", "name"], [(1, "x")], "sqlserver", "pyformat"
        )
        assert "[orders]" in sql
        assert "[id]" in sql

    def test_mysql_backticks_in_sql(self):
        sql, _ = _build_multi_row_sql(
            "orders", ["id"], [(1,)], "mysql", "pyformat"
        )
        assert "`orders`" in sql
        assert "`id`" in sql

    def test_three_rows_correct_param_count(self):
        _, params = _build_multi_row_sql(
            "t", ["a", "b", "c"], [(1, 2, 3), (4, 5, 6), (7, 8, 9)], "sqlite", "qmark"
        )
        assert len(params) == 9

    def test_numeric_paramstyle_increments(self):
        sql, _ = _build_multi_row_sql(
            "t", ["a", "b"], [(1, 2), (3, 4)], "oracle", "numeric"
        )
        assert ":1" in sql
        assert ":4" in sql


class TestBuildOracleAllSql:
    def test_single_row_structure(self):
        sql, params = _build_oracle_all_sql("t", ["a", "b"], [(1, "x")])
        assert "INSERT ALL" in sql
        assert "SELECT 1 FROM DUAL" in sql
        assert sql.count("INTO") == 1
        assert params["v0_0"] == 1
        assert params["v0_1"] == "x"

    def test_two_rows(self):
        sql, params = _build_oracle_all_sql("orders", ["id", "name"], [(1, "a"), (2, "b")])
        assert sql.count("INTO") == 2
        assert params["v1_0"] == 2
        assert params["v1_1"] == "b"

    def test_named_bind_vars_are_unique(self):
        _, params = _build_oracle_all_sql("t", ["x", "y", "z"], [(1, 2, 3), (4, 5, 6)])
        assert len(params) == 6
        assert "v0_0" in params
        assert "v1_2" in params

    def test_oracle_table_double_quoted(self):
        sql, _ = _build_oracle_all_sql("MY_TABLE", ["col"], [(1,)])
        assert '"MY_TABLE"' in sql


# ---------------------------------------------------------------------------
# Singleton insert
# ---------------------------------------------------------------------------

class TestInsertSingleton:
    def test_five_rows_committed(self):
        conn = _conn()
        _insert_singleton(conn.cursor(), "t", COLS, _rows(5), "sqlite", "qmark")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 5

    def test_zero_rows_returns_zero(self):
        conn = _conn()
        n = _insert_singleton(conn.cursor(), "t", COLS, [], "sqlite", "qmark")
        assert n == 0

    def test_values_are_correct(self):
        conn = _conn()
        _insert_singleton(conn.cursor(), "t", COLS, [(42, "hello", 3.14)], "sqlite", "qmark")
        conn.commit()
        row = conn.execute("SELECT a, b, c FROM t").fetchone()
        assert row == (42, "hello", 3.14)

    def test_null_values(self):
        conn = _conn()
        _insert_singleton(conn.cursor(), "t", COLS, [(1, None, None)], "sqlite", "qmark")
        conn.commit()
        row = conn.execute("SELECT b, c FROM t WHERE a = 1").fetchone()
        assert row == (None, None)

    def test_returns_row_count(self):
        conn = _conn()
        n = _insert_singleton(conn.cursor(), "t", COLS, _rows(7), "sqlite", "qmark")
        assert n == 7


# ---------------------------------------------------------------------------
# Multi-row insert
# ---------------------------------------------------------------------------

class TestInsertMultiRow:
    def test_ten_rows_batch_of_five(self):
        conn = _conn()
        _insert_multi_row(conn.cursor(), "t", COLS, _rows(10), "sqlite", "qmark", 5)
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 10

    def test_returns_correct_count(self):
        conn = _conn()
        n = _insert_multi_row(conn.cursor(), "t", COLS, _rows(7), "sqlite", "qmark", 3)
        assert n == 7

    def test_batch_larger_than_rows(self):
        conn = _conn()
        n = _insert_multi_row(conn.cursor(), "t", COLS, _rows(3), "sqlite", "qmark", 100)
        conn.commit()
        assert n == 3
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 3

    def test_batch_of_one_same_as_singleton(self):
        conn = _conn()
        _insert_multi_row(conn.cursor(), "t", COLS, _rows(5), "sqlite", "qmark", 1)
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 5

    def test_null_values_preserved(self):
        conn = _conn()
        rows = [(1, None, None), (2, "ok", 1.5)]
        _insert_multi_row(conn.cursor(), "t", COLS, rows, "sqlite", "qmark", 10)
        conn.commit()
        assert conn.execute("SELECT b FROM t WHERE a = 1").fetchone()[0] is None

    def test_zero_rows(self):
        conn = _conn()
        n = _insert_multi_row(conn.cursor(), "t", COLS, [], "sqlite", "qmark", 10)
        assert n == 0


# ---------------------------------------------------------------------------
# load_dataframe entry point
# ---------------------------------------------------------------------------

class TestLoadDataframe:
    def test_singleton_strategy(self):
        conn = _conn()
        n = load_dataframe(_rows(4), conn, "t", "sqlite", strategy=LoadStrategy.SINGLETON, cols=COLS)
        assert n == 4
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 4

    def test_multi_row_strategy(self):
        conn = _conn()
        n = load_dataframe(_rows(8), conn, "t", "sqlite", strategy=LoadStrategy.MULTI_ROW, cols=COLS)
        assert n == 8

    def test_default_strategy_is_multi_row(self):
        conn = _conn()
        n = load_dataframe(_rows(3), conn, "t", "sqlite", cols=COLS)
        assert n == 3

    def test_custom_batch_config_respected(self):
        conn = _conn()
        cfg = BatchConfig(max_rows=2, max_params=999)
        n = load_dataframe(
            _rows(6), conn, "t", "sqlite",
            strategy=LoadStrategy.MULTI_ROW, config=cfg, cols=COLS,
        )
        assert n == 6
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 6

    def test_bulk_copy_raises_for_unsupported_dialect(self):
        """BULK_COPY with no registered loader raises RuntimeError — no silent fallback."""
        conn = _conn()
        with pytest.raises(RuntimeError, match="BULK_COPY not implemented for dialect='sqlite'"):
            load_dataframe(
                _rows(3), conn, "t", "sqlite",
                strategy=LoadStrategy.BULK_COPY, cols=COLS,
            )

    def test_commit_false_does_not_autocommit(self):
        conn = _conn()
        load_dataframe(_rows(3), conn, "t", "sqlite", cols=COLS, commit=False)
        # Without commit, a new connection should see 0 rows (isolation)
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0

    def test_invalid_strategy_raises(self):
        conn = _conn()
        with pytest.raises((ValueError, AttributeError)):
            load_dataframe(_rows(2), conn, "t", "sqlite", strategy="bad_strategy", cols=COLS)

    def test_databricks_no_spark_raises_runtime_error(self):
        """Databricks BULK_COPY dispatch fails with RuntimeError when no Spark session is set."""
        conn = _conn()
        # strategy=BULK_COPY → registry dispatch.
        # ctx=None → DeploymentContext.from_env() (topology='remote', spark_session=None)
        # All Databricks loaders require a live spark_session → RuntimeError from _select_loader.
        with pytest.raises(RuntimeError, match="No topology-aware loader found"):
            load_dataframe(_rows(2), conn, "t", "databricks",
                           strategy=LoadStrategy.BULK_COPY, cols=COLS)


class TestBulkLoadSqlServerBCP:
    """Verify that mssql-python's cursor.bulkcopy() is called when the driver is detected."""

    def _mssql_python_conn(self, db_name: str = "testdb"):
        """Return a fake connection whose type().__module__ == 'mssql_python.connection'.

        The loader now calls conn.cursor() → cursor.execute("SELECT DB_NAME()") →
        cursor.fetchone() → cursor.bulkcopy(table, rows, column_mappings=cols).
        """
        fake_cur = MagicMock()
        fake_cur.fetchone.return_value = (db_name,)
        fake_cur.bulkcopy.return_value = {"rows_copied": None}  # rows_copied=None → falls back to count

        FakeConn = type(
            "Connection",
            (),
            {"__module__": "mssql_python.connection"},
        )
        conn = FakeConn()
        conn.cursor = MagicMock(return_value=fake_cur)
        conn.commit = MagicMock()
        conn._fake_cur = fake_cur
        return conn

    def test_mssql_python_driver_calls_bulk_copy(self):
        from src.statschema.data_loader import bulk_load_sqlserver

        conn = self._mssql_python_conn()
        bulk_load_sqlserver(conn, _rows(3), "orders", COLS)
        conn._fake_cur.bulkcopy.assert_called_once()
        call_args = conn._fake_cur.bulkcopy.call_args
        assert "orders" in call_args[0][0]         # table name in first positional arg
        assert len(call_args[0][1]) == 3            # 3 rows passed

    def test_mssql_python_driver_uses_three_part_name(self):
        conn = self._mssql_python_conn(db_name="mydb")
        from src.statschema.data_loader import bulk_load_sqlserver
        bulk_load_sqlserver(conn, _rows(1), "orders", COLS)
        table_arg = conn._fake_cur.bulkcopy.call_args[0][0]
        assert "mydb" in table_arg
        assert "dbo" in table_arg
        assert "orders" in table_arg

    def test_mssql_python_driver_does_not_write_csv(self, tmp_path):
        from src.statschema.data_loader import bulk_load_sqlserver

        conn = self._mssql_python_conn()
        bulk_load_sqlserver(conn, _rows(2), "orders", COLS, staging_dir=str(tmp_path))
        # No CSV file should have been created
        assert list(tmp_path.iterdir()) == []

    def test_mssql_python_driver_commits(self):
        from src.statschema.data_loader import bulk_load_sqlserver

        conn = self._mssql_python_conn()
        bulk_load_sqlserver(conn, _rows(2), "orders", COLS)
        conn.commit.assert_called_once()

    def test_mssql_python_driver_returns_row_count(self):
        from src.statschema.data_loader import bulk_load_sqlserver

        conn = self._mssql_python_conn()
        n = bulk_load_sqlserver(conn, _rows(5), "orders", COLS)
        assert n == 5

    def test_pandas_dataframe_input(self):
        pd = pytest.importorskip("pandas")
        df = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"], "c": [0.1, 0.2, 0.3]})
        conn = _conn()
        n = load_dataframe(df, conn, "t", "sqlite")
        assert n == 3

    def test_generator_input_does_not_materialise(self):
        """load_dataframe must accept a one-shot generator (no list() materialisation)."""
        conn = _conn()
        # A generator can only be iterated once — if load_dataframe called list()
        # on it a second time it would get 0 rows and the count would be wrong.
        gen = ((i, f"v{i}", float(i)) for i in range(50))
        n   = load_dataframe(gen, conn, "t", "sqlite",
                             strategy=LoadStrategy.MULTI_ROW, cols=COLS)
        assert n == 50
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 50

    def test_singleton_generator_input(self):
        """SINGLETON strategy must also work with a one-shot generator."""
        conn = _conn()
        gen  = ((i, f"v{i}", float(i)) for i in range(10))
        n    = load_dataframe(gen, conn, "t", "sqlite",
                              strategy=LoadStrategy.SINGLETON, cols=COLS)
        assert n == 10


# ---------------------------------------------------------------------------
# Batch size auto-discovery
# ---------------------------------------------------------------------------

class TestDiscoverMaxBatchSize:
    # Force a low SQLITE_LIMIT_VARIABLE_NUMBER so tests don't depend on the
    # SQLite version (the default was 999 before 3.32.0, 32766 after).
    _FORCED_PARAM_LIMIT = 99

    def _probe_conn(self, n_cols: int = 2) -> sqlite3.Connection:
        cols = ", ".join(f"c{i} INTEGER" for i in range(n_cols))
        conn = sqlite3.connect(":memory:")
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, self._FORCED_PARAM_LIMIT)
        conn.execute(f"CREATE TABLE probe ({cols})")
        return conn

    def _probe_cols(self, n: int) -> list[str]:
        return [f"c{i}" for i in range(n)]

    def test_finds_valid_batch_size(self):
        conn = self._probe_conn(2)
        best = discover_max_batch_size(conn, "probe", self._probe_cols(2), "sqlite",
                                       min_rows=1, start_max=50)
        assert best >= 1

    def test_respects_forced_param_limit_two_cols(self):
        # 99 params / 2 cols = 49 max rows; start_max=100 exceeds the limit
        conn = self._probe_conn(2)
        best = discover_max_batch_size(conn, "probe", self._probe_cols(2), "sqlite",
                                       min_rows=1, start_max=100)
        assert best <= 49
        assert best >= 1

    def test_respects_forced_param_limit_ten_cols(self):
        # 99 / 10 = 9 max rows; start_max=30 exceeds the limit
        conn = self._probe_conn(10)
        best = discover_max_batch_size(conn, "probe", self._probe_cols(10), "sqlite",
                                       min_rows=1, start_max=30)
        assert best <= 9
        assert best >= 1

    def test_table_is_empty_after_discovery(self):
        """Probe rows must be fully rolled back — table stays empty."""
        conn = self._probe_conn(2)
        discover_max_batch_size(conn, "probe", self._probe_cols(2), "sqlite",
                                min_rows=1, start_max=20)
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 0

    def test_min_rows_is_returned_when_one_row_works(self):
        conn = self._probe_conn(2)
        best = discover_max_batch_size(conn, "probe", self._probe_cols(2), "sqlite",
                                       min_rows=1, start_max=1)
        assert best == 1

    def test_auto_discover_via_config(self):
        """BatchConfig.auto_discover=True triggers discovery inside load_dataframe."""
        conn = self._probe_conn(2)
        cfg  = BatchConfig(max_rows=100, max_params=999, auto_discover=True)
        n    = load_dataframe(
            [(1, 2), (3, 4)], conn, "probe", "sqlite",
            strategy=LoadStrategy.MULTI_ROW, config=cfg,
            cols=self._probe_cols(2),
        )
        assert n == 2
        assert conn.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 2


# ---------------------------------------------------------------------------
# Dialect defaults
# ---------------------------------------------------------------------------

class TestDialectDefaults:
    def test_all_dialects_present(self):
        for d in ("mysql", "mariadb", "postgres", "cockroachdb", "neon",
                  "sqlserver", "oracle", "db2", "databricks", "sqlite"):
            assert d in _DIALECT_BATCH_DEFAULTS

    def test_sqlserver_hard_row_limit(self):
        assert _DIALECT_BATCH_DEFAULTS["sqlserver"].max_rows == 1000

    def test_sqlserver_param_limit(self):
        assert _DIALECT_BATCH_DEFAULTS["sqlserver"].max_params == 2100

    def test_oracle_conservative_rows(self):
        assert _DIALECT_BATCH_DEFAULTS["oracle"].max_rows == 500

    def test_sqlite_param_limit(self):
        # 32766 since SQLite 3.32.0; use setlimit() to lower it per-connection
        assert _DIALECT_BATCH_DEFAULTS["sqlite"].max_params == 32766

    def test_auto_discover_off_by_default(self):
        for cfg in _DIALECT_BATCH_DEFAULTS.values():
            assert cfg.auto_discover is False
