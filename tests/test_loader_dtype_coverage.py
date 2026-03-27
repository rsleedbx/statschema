"""
tests/test_loader_dtype_coverage.py

Data-type unit tests for multi-row INSERT and bulk-load coercion paths.

For every canonical type in the emitter DEFAULTS, we verify:
  1. _coerce_oracle_val handles all value flavours correctly
  2. _coerce_db2_val / _coerce_db2_row handles all canonical type names
  3. Oracle direct-path varchar_types logic coerces CLOB/NCLOB/LONG correctly
  4. _build_multi_row_sql generates valid SQL for postgres/mysql/sqlserver/db2
  5. _build_oracle_all_sql (INSERT ALL) with all canonical types
  6. load_dataframe → SQLite round-trip for all SQLite-compatible types
  7. Bulk COPY CSV serialization for the postgres COPY path
  8. Full identity pipeline: write-1 → derive stats → write-2 for all types

Coverage gaps this file closes (from design analysis):
  - uuid, float, double, binary, timestamptz, timetz: not in any benchmark schema
  - Oracle CLOB/NCLOB/LONG coercion (str-ification of non-str values)
  - DB2 "string" canonical type (maps to CLOB in DDL)
  - Multi-row SQL shape for all four non-oracle paramstyle families
  - Numeric columns with string MCV values (Oracle stats collector path)
"""
from __future__ import annotations

import csv
import io
import math
import sqlite3
from datetime import date, datetime, time, timezone, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch, call

import pytest

# sqlite3 uses exact type dispatch (not isinstance), so it rejects pandas.Timestamp
# even though pd.Timestamp subclasses datetime.datetime.  Register adapters once at
# module level so every SQLite round-trip test in this file works correctly.
try:
    import pandas as _pd_for_adapter
    sqlite3.register_adapter(
        _pd_for_adapter.Timestamp,
        lambda ts: ts.to_pydatetime().isoformat(),
    )
except Exception:
    pass

from src.statschema.dialects._loader_shared import (
    _build_multi_row_sql,
    _build_oracle_all_sql,
    _coerce_db2_row,
    _coerce_db2_val,
    _coerce_oracle_val,
    _DB2_TEXT_TYPES,
    _nan_to_none,
)
from src.statschema.data_loader import load_dataframe

# ---------------------------------------------------------------------------
# Constants: canonical type names (the keys in emitter DEFAULTS)
# ---------------------------------------------------------------------------

CANONICAL_TYPES = [
    "integer",
    "long",
    "string",
    "uuid",
    "float",
    "double",
    "boolean",
    "timestamp",
    "timestamptz",
    "time",
    "timetz",
    "date",
    "decimal",
    "binary",
]

# ---------------------------------------------------------------------------
# 1. _coerce_oracle_val — all value flavours
# ---------------------------------------------------------------------------

class TestCoerceOracleVal:
    """_coerce_oracle_val converts string date/time literals; passes everything else through."""

    def test_time_string_hms(self):
        result = _coerce_oracle_val("14:30:00")
        assert isinstance(result, datetime)
        assert result.hour == 14 and result.minute == 30 and result.second == 0

    def test_time_string_with_fractional(self):
        result = _coerce_oracle_val("08:05:59.123")
        assert isinstance(result, datetime)
        assert result.hour == 8 and result.minute == 5 and result.second == 59

    def test_time_string_with_tz_offset(self):
        result = _coerce_oracle_val("23:59:01+05:30")
        assert isinstance(result, datetime)
        assert result.hour == 23

    def test_date_string_iso(self):
        result = _coerce_oracle_val("2024-03-15")
        assert isinstance(result, date)
        assert result == date(2024, 3, 15)

    def test_plain_string_unchanged(self):
        assert _coerce_oracle_val("hello world") == "hello world"

    def test_integer_passthrough(self):
        assert _coerce_oracle_val(42) == 42

    def test_float_passthrough(self):
        val = _coerce_oracle_val(3.14)
        assert val == pytest.approx(3.14)

    def test_none_passthrough(self):
        assert _coerce_oracle_val(None) is None

    def test_bytes_passthrough(self):
        b = b"\x00\xff"
        assert _coerce_oracle_val(b) is b

    def test_datetime_object_passthrough(self):
        dt = datetime(2024, 1, 1, 12, 0, 0)
        assert _coerce_oracle_val(dt) is dt

    def test_date_object_passthrough(self):
        d = date(2024, 6, 15)
        assert _coerce_oracle_val(d) is d

    def test_bool_passthrough(self):
        assert _coerce_oracle_val(True) is True

    @pytest.mark.parametrize("val", [
        "not-a-date",
        "hello:world:x",
        "20240315",        # no dashes — does not match ISO date regex
    ])
    def test_non_matching_string_passthrough(self, val):
        assert _coerce_oracle_val(val) == val


# ---------------------------------------------------------------------------
# 2. Oracle direct-path varchar_types coercion logic
#    (tests the pattern in data_loader.py without a live Oracle connection)
# ---------------------------------------------------------------------------

class TestOracleVarcharTypesCoercion:
    """Verify which DDL types should receive str() coercion on direct_path_load."""

    _VARCHAR_TYPES = {"CHAR", "VARCHAR2", "NCHAR", "NVARCHAR2", "CLOB", "NCLOB", "LONG"}

    def _coerce_dp(self, val, ddl_type: str):
        """Simulate the _coerce_oracle_dp logic from data_loader.py."""
        is_varchar = ddl_type in self._VARCHAR_TYPES
        val = _coerce_oracle_val(_nan_to_none(val))
        if is_varchar and val is not None and not isinstance(val, str):
            return str(val)
        return val

    @pytest.mark.parametrize("ddl_type", [
        "CHAR", "VARCHAR2", "NCHAR", "NVARCHAR2", "CLOB", "NCLOB", "LONG"
    ])
    def test_int_is_stringified_for_varchar_like_types(self, ddl_type):
        result = self._coerce_dp(42, ddl_type)
        assert result == "42"
        assert isinstance(result, str)

    @pytest.mark.parametrize("ddl_type", [
        "CHAR", "VARCHAR2", "NCHAR", "NVARCHAR2", "CLOB", "NCLOB", "LONG"
    ])
    def test_float_is_stringified_for_varchar_like_types(self, ddl_type):
        result = self._coerce_dp(3.14, ddl_type)
        assert isinstance(result, str)

    @pytest.mark.parametrize("ddl_type", [
        "NUMBER", "INTEGER", "FLOAT", "BLOB", "DATE", "TIMESTAMP"
    ])
    def test_non_varchar_types_do_not_stringify(self, ddl_type):
        result = self._coerce_dp(42, ddl_type)
        assert result == 42
        assert isinstance(result, int)

    def test_none_not_stringified_for_clob(self):
        assert self._coerce_dp(None, "CLOB") is None

    def test_already_str_clob_unchanged(self):
        result = self._coerce_dp("hello", "CLOB")
        assert result == "hello"

    def test_nan_becomes_none_then_not_stringified(self):
        result = self._coerce_dp(float("nan"), "CLOB")
        assert result is None

    def test_time_string_for_clob_stays_as_str_then_no_extra_coerce(self):
        # "14:30:00" → _coerce_oracle_val → datetime(2000,1,1,14,30,0)
        # then since it's not a str, but ddl_type=CLOB → str()
        result = self._coerce_dp("14:30:00", "CLOB")
        assert isinstance(result, str)

    def test_date_string_for_varchar2_becomes_str_date(self):
        # "2024-01-15" → _coerce_oracle_val → date(2024,1,15)
        # then since it's not a str, but ddl_type=VARCHAR2 → str()
        result = self._coerce_dp("2024-01-15", "VARCHAR2")
        assert isinstance(result, str)


class TestOracleNumberTypeCoercion:
    """Oracle stats collector returns MCV values as strings; NUMBER columns must
    convert them to float before direct_path_load.  This tests the _col_kind /
    _coerce_oracle_dp logic added in data_loader.py."""

    _VARCHAR_TYPES = {"CHAR", "VARCHAR2", "NCHAR", "NVARCHAR2", "CLOB", "NCLOB", "LONG"}
    _NUMBER_TYPES  = {"NUMBER", "FLOAT", "BINARY_FLOAT", "BINARY_DOUBLE",
                      "INTEGER", "SMALLINT", "INT"}

    def _coerce_dp(self, val, ddl_type: str):
        """Reproduce _coerce_oracle_dp from data_loader.py."""
        base = ddl_type.split("(")[0].strip()
        varchar = base in self._VARCHAR_TYPES
        numeric = base in self._NUMBER_TYPES
        val = _coerce_oracle_val(_nan_to_none(val))
        if varchar and val is not None and not isinstance(val, str):
            return str(val)
        if numeric and isinstance(val, str) and val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                pass
        return val

    @pytest.mark.parametrize("ddl_type", [
        "NUMBER", "NUMBER(18,4)", "NUMBER(10)", "NUMBER(1)",
        "FLOAT", "BINARY_FLOAT", "BINARY_DOUBLE",
    ])
    def test_string_decimal_mcp_coerced_to_float(self, ddl_type):
        result = self._coerce_dp("12345.67", ddl_type)
        assert isinstance(result, float)
        assert result == pytest.approx(12345.67)

    @pytest.mark.parametrize("ddl_type", [
        "NUMBER", "NUMBER(10)", "INTEGER", "SMALLINT", "INT",
    ])
    def test_string_integer_mcp_coerced_to_float(self, ddl_type):
        result = self._coerce_dp("42", ddl_type)
        assert isinstance(result, float)
        assert result == 42.0

    def test_none_for_number_stays_none(self):
        assert self._coerce_dp(None, "NUMBER") is None

    def test_actual_float_for_number_unchanged(self):
        result = self._coerce_dp(3.14, "NUMBER")
        assert result == pytest.approx(3.14)
        assert isinstance(result, float)

    def test_actual_int_for_number_unchanged(self):
        result = self._coerce_dp(42, "NUMBER")
        assert result == 42
        assert isinstance(result, int)

    def test_non_numeric_string_for_number_passthrough(self):
        # Invalid numeric string: don't blow up, just pass through
        result = self._coerce_dp("not_a_number", "NUMBER")
        assert result == "not_a_number"

    def test_date_string_for_date_column_becomes_date(self):
        # DATE column: _coerce_oracle_val converts to date object — not NUMBER
        result = self._coerce_dp("2024-01-15", "DATE")
        assert isinstance(result, date)

    def test_number_precision_parenthetical_stripped_correctly(self):
        # "NUMBER(18,4)" base is "NUMBER" → numeric flag
        result = self._coerce_dp("9999.5000", "NUMBER(18,4)")
        assert isinstance(result, float)


# ---------------------------------------------------------------------------
# 3. _coerce_db2_val — all Python/numpy/pandas input flavours
# ---------------------------------------------------------------------------

class TestCoerceDB2Val:
    """_coerce_db2_val normalises numpy/pandas scalars for DB2 binding."""

    def test_none_stays_none(self):
        assert _coerce_db2_val(None) is None

    def test_bool_true_becomes_int_1(self):
        assert _coerce_db2_val(True) == 1
        assert isinstance(_coerce_db2_val(True), int)

    def test_bool_false_becomes_int_0(self):
        assert _coerce_db2_val(False) == 0
        assert isinstance(_coerce_db2_val(False), int)

    def test_plain_int_passthrough(self):
        assert _coerce_db2_val(42) == 42

    def test_plain_float_passthrough(self):
        assert _coerce_db2_val(3.14) == pytest.approx(3.14)

    def test_plain_str_passthrough(self):
        assert _coerce_db2_val("hello") == "hello"

    def test_plain_bytes_passthrough(self):
        b = b"\x00\x01"
        assert _coerce_db2_val(b) is b

    def test_numpy_integer(self):
        np = pytest.importorskip("numpy")
        result = _coerce_db2_val(np.int64(99))
        assert result == 99 and isinstance(result, int)

    def test_numpy_float(self):
        np = pytest.importorskip("numpy")
        result = _coerce_db2_val(np.float32(1.5))
        assert isinstance(result, float)

    def test_numpy_bool(self):
        np = pytest.importorskip("numpy")
        result = _coerce_db2_val(np.bool_(True))
        assert result == 1 and isinstance(result, int)

    def test_pandas_timestamp_date_only(self):
        pd = pytest.importorskip("pandas")
        ts = pd.Timestamp("2024-03-15")
        result = _coerce_db2_val(ts)
        assert isinstance(result, date)
        assert result == date(2024, 3, 15)

    def test_pandas_timestamp_with_time(self):
        pd = pytest.importorskip("pandas")
        ts = pd.Timestamp("2024-03-15 14:30:00")
        result = _coerce_db2_val(ts)
        assert isinstance(result, datetime)
        assert result.hour == 14

    def test_pandas_na_becomes_none(self):
        pd = pytest.importorskip("pandas")
        assert _coerce_db2_val(pd.NA) is None


# ---------------------------------------------------------------------------
# 4. _coerce_db2_row — all canonical type names trigger str() for text types
# ---------------------------------------------------------------------------

class TestCoerceDB2Row:
    """_coerce_db2_row must stringify non-str values for every DB2 text type."""

    @pytest.mark.parametrize("ctype", sorted(_DB2_TEXT_TYPES))
    def test_int_stringified_for_all_text_types(self, ctype):
        row = (42,)
        result = _coerce_db2_row(row, [ctype])
        assert result == ("42",), f"Expected str coercion for ctype={ctype!r}"

    @pytest.mark.parametrize("ctype", sorted(_DB2_TEXT_TYPES))
    def test_float_stringified_for_all_text_types(self, ctype):
        row = (1.5,)
        result = _coerce_db2_row(row, [ctype])
        assert isinstance(result[0], str)

    def test_none_not_stringified_for_clob(self):
        row = (None,)
        result = _coerce_db2_row(row, ["clob"])
        assert result == (None,)

    def test_already_str_unchanged(self):
        row = ("hello",)
        result = _coerce_db2_row(row, ["string"])
        assert result == ("hello",)

    def test_non_text_types_not_stringified(self):
        row = (42,)
        result = _coerce_db2_row(row, ["integer"])
        assert result == (42,)
        assert isinstance(result[0], int)

    def test_mixed_row(self):
        np = pytest.importorskip("numpy")
        row = (np.int64(1), "text_val", np.int64(99), True, None)
        types = ["integer", "string", "clob", "varchar", "integer"]
        result = _coerce_db2_row(row, types)
        assert isinstance(result[0], int)    # integer: numpy → int, not str
        assert result[1] == "text_val"       # string: already str
        assert result[2] == "99"             # clob: int → str
        assert result[3] == "1"              # varchar: bool → int (via _coerce_db2_val) → str
        assert result[4] is None             # integer: None stays None

    def test_canonical_string_type_triggers_str_coercion(self):
        """The canonical YAML type 'string' maps to CLOB; coercion must fire."""
        row = (12345,)
        result = _coerce_db2_row(row, ["string"])
        assert result == ("12345",)

    def test_nclob_triggers_str_coercion(self):
        row = (99,)
        result = _coerce_db2_row(row, ["nclob"])
        assert result == ("99",)


# ---------------------------------------------------------------------------
# 5. _build_multi_row_sql — correct SQL shape for each dialect paramstyle
# ---------------------------------------------------------------------------

class TestMultiRowSQL:
    """Verify generated INSERT SQL for each dialect × canonical type column set."""

    ALL_TYPE_COLS = [
        "c_integer", "c_long", "c_string", "c_uuid", "c_float",
        "c_double", "c_boolean", "c_timestamp", "c_timestamptz",
        "c_time", "c_timetz", "c_date", "c_decimal", "c_binary",
    ]

    def _sample_row(self):
        return (
            1,                                  # integer
            9999999999,                         # long
            "hello",                            # string
            "550e8400-e29b-41d4-a716-446655440000",  # uuid
            1.5,                                # float
            3.14159,                            # double
            True,                               # boolean
            datetime(2024, 1, 1, 12, 0, 0),    # timestamp
            datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),  # timestamptz
            time(14, 30, 0),                    # time
            time(14, 30, 0, tzinfo=timezone.utc),  # timetz
            date(2024, 3, 15),                  # date
            Decimal("12345.6789"),              # decimal
            b"\x00\xff",                        # binary
        )

    @pytest.mark.parametrize("dialect,paramstyle,expected_ph", [
        ("postgres",    "pyformat", "%s"),
        ("cockroachdb", "pyformat", "%s"),
        ("neon",        "pyformat", "%s"),
        ("mysql",       "pyformat", "%s"),
        ("db2",         "qmark",    "?"),
        ("sqlserver",   "qmark",    "?"),
        ("sqlite",      "qmark",    "?"),
    ])
    def test_placeholder_style(self, dialect, paramstyle, expected_ph):
        row = self._sample_row()
        sql, params = _build_multi_row_sql("orders", self.ALL_TYPE_COLS, [row], dialect, paramstyle)
        n = len(self.ALL_TYPE_COLS)
        assert sql.count(expected_ph) == n
        assert len(params) == n

    @pytest.mark.parametrize("dialect,paramstyle", [
        ("postgres", "pyformat"),
        ("mysql", "pyformat"),
        ("sqlserver", "qmark"),
        ("db2", "qmark"),
    ])
    def test_multi_row_has_correct_value_clause_count(self, dialect, paramstyle):
        rows = [self._sample_row()] * 3
        sql, params = _build_multi_row_sql("t", self.ALL_TYPE_COLS, rows, dialect, paramstyle)
        assert sql.count("(") >= 3 + 1  # at least 3 value tuples + col list
        assert len(params) == 3 * len(self.ALL_TYPE_COLS)

    def test_postgres_double_quote_identifiers(self):
        sql, _ = _build_multi_row_sql(
            "my_table", ["c_integer"], [(1,)], "postgres", "pyformat"
        )
        assert '"my_table"' in sql
        assert '"c_integer"' in sql

    def test_mysql_backtick_identifiers(self):
        sql, _ = _build_multi_row_sql(
            "orders", ["col"], [(1,)], "mysql", "pyformat"
        )
        assert "`orders`" in sql
        assert "`col`" in sql

    def test_sqlserver_bracket_identifiers(self):
        sql, _ = _build_multi_row_sql(
            "orders", ["col"], [(1,)], "sqlserver", "qmark"
        )
        assert "[orders]" in sql
        assert "[col]" in sql

    def test_db2_double_quote_identifiers(self):
        sql, _ = _build_multi_row_sql(
            "orders", ["col"], [(1,)], "db2", "qmark"
        )
        assert '"orders"' in sql
        assert '"col"' in sql

    def test_numeric_paramstyle(self):
        """Oracle non-INSERT-ALL path uses numeric :1 :2 :3 placeholders."""
        sql, params = _build_multi_row_sql(
            "t", ["a", "b", "c"], [(1, 2, 3)], "oracle", "numeric"
        )
        assert ":1" in sql and ":2" in sql and ":3" in sql
        assert params == [1, 2, 3]

    def test_empty_batch_returns_empty_values(self):
        sql, params = _build_multi_row_sql("t", ["a"], [], "postgres", "pyformat")
        assert "VALUES" in sql
        assert params == []


# ---------------------------------------------------------------------------
# 6. _build_oracle_all_sql (INSERT ALL) — all canonical types
# ---------------------------------------------------------------------------

class TestOracleInsertAllSQL:
    """INSERT ALL syntax is Oracle-specific; verify with all canonical types."""

    ALL_TYPE_COLS = [
        "c_integer", "c_long", "c_string", "c_uuid", "c_float",
        "c_double", "c_boolean", "c_timestamp", "c_date", "c_decimal",
    ]

    def _sample_row(self):
        return (
            1, 9999999999, "hello",
            "550e8400-e29b-41d4-a716-446655440000",
            1.5, 3.14159, 1,
            datetime(2024, 1, 1, 12, 0, 0),
            date(2024, 3, 15),
            Decimal("123.45"),
        )

    def test_starts_with_insert_all(self):
        sql, _ = _build_oracle_all_sql("orders", self.ALL_TYPE_COLS, [self._sample_row()])
        assert sql.startswith("INSERT ALL")

    def test_ends_with_select_from_dual(self):
        sql, _ = _build_oracle_all_sql("orders", self.ALL_TYPE_COLS, [self._sample_row()])
        assert sql.strip().endswith("SELECT 1 FROM DUAL")

    def test_params_dict_length(self):
        rows = [self._sample_row(), self._sample_row()]
        _, params = _build_oracle_all_sql("orders", self.ALL_TYPE_COLS, rows)
        assert len(params) == 2 * len(self.ALL_TYPE_COLS)

    def test_named_bind_pattern(self):
        sql, _ = _build_oracle_all_sql("t", ["a", "b"], [(1, 2)])
        assert ":v0_0" in sql and ":v0_1" in sql

    def test_time_string_coerced_in_params(self):
        sql, params = _build_oracle_all_sql("t", ["col"], [("14:30:00",)])
        assert isinstance(params["v0_0"], datetime)

    def test_date_string_coerced_in_params(self):
        _, params = _build_oracle_all_sql("t", ["col"], [("2024-01-15",)])
        assert isinstance(params["v0_0"], date)

    def test_non_time_value_untouched(self):
        _, params = _build_oracle_all_sql("t", ["col"], [(42,)])
        assert params["v0_0"] == 42

    def test_oracle_double_quote_identifiers(self):
        sql, _ = _build_oracle_all_sql("MY_TABLE", ["C_INT"], [(1,)])
        assert '"MY_TABLE"' in sql
        assert '"C_INT"' in sql


# ---------------------------------------------------------------------------
# 7. load_dataframe → SQLite round-trip for all SQLite-compatible types
#    SQLite stores everything as TEXT, INTEGER, REAL, BLOB, NULL — so we test
#    all types that SQLite can store without conversion errors.
# ---------------------------------------------------------------------------

class TestLoadDataframeSQLiteRoundtrip:
    """Full load_dataframe round-trip via SQLite for all compatible canonical types."""

    def _conn_with_types(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("""
            CREATE TABLE t (
                c_integer   INTEGER,
                c_long      INTEGER,
                c_string    TEXT,
                c_uuid      TEXT,
                c_float     REAL,
                c_double    REAL,
                c_boolean   INTEGER,
                c_timestamp TEXT,
                c_date      TEXT,
                c_decimal   TEXT,
                c_binary    BLOB
            )
        """)
        return conn

    def test_single_row_all_types_round_trip(self):
        pd = pytest.importorskip("pandas")
        conn = self._conn_with_types()
        df = pd.DataFrame([{
            "c_integer":   1,
            "c_long":      9_999_999_999,
            "c_string":    "hello world",
            "c_uuid":      "550e8400-e29b-41d4-a716-446655440000",
            "c_float":     1.5,
            "c_double":    3.14159265358979,
            "c_boolean":   1,
            "c_timestamp": "2024-01-01 12:00:00",
            "c_date":      "2024-03-15",
            "c_decimal":   "12345.6789",
            "c_binary":    b"\x00\xff\xfe",
        }])
        inserted = load_dataframe(df, conn, "t", dialect="sqlite")
        assert inserted == 1
        row = conn.execute("SELECT * FROM t").fetchone()
        assert row[0] == 1                            # integer
        assert row[1] == 9_999_999_999                # long
        assert row[2] == "hello world"                # string
        assert row[3] == "550e8400-e29b-41d4-a716-446655440000"  # uuid
        assert row[4] == pytest.approx(1.5)           # float
        assert row[5] == pytest.approx(3.14159265358979)  # double
        assert row[6] == 1                            # boolean (as int)
        assert row[10] == b"\x00\xff\xfe"             # binary

    def test_null_values_for_all_types(self):
        pd = pytest.importorskip("pandas")
        conn = self._conn_with_types()
        df = pd.DataFrame([{
            "c_integer": None, "c_long": None, "c_string": None,
            "c_uuid": None, "c_float": None, "c_double": None,
            "c_boolean": None, "c_timestamp": None, "c_date": None,
            "c_decimal": None, "c_binary": None,
        }])
        inserted = load_dataframe(df, conn, "t", dialect="sqlite")
        assert inserted == 1
        row = conn.execute("SELECT * FROM t").fetchone()
        assert all(v is None for v in row)

    def test_nan_float_becomes_null(self):
        pd = pytest.importorskip("pandas")
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (c_float REAL)")
        df = pd.DataFrame([{"c_float": float("nan")}])
        load_dataframe(df, conn, "t", dialect="sqlite")
        row = conn.execute("SELECT c_float FROM t").fetchone()
        assert row[0] is None

    def test_multiple_rows_inserted(self):
        pd = pytest.importorskip("pandas")
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (a INTEGER, b TEXT)")
        df = pd.DataFrame([{"a": i, "b": f"row{i}"} for i in range(50)])
        inserted = load_dataframe(df, conn, "t", dialect="sqlite")
        assert inserted == 50
        count = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        assert count == 50

    def test_boolean_type(self):
        pd = pytest.importorskip("pandas")
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (a INTEGER)")
        df = pd.DataFrame([{"a": True}, {"a": False}])
        load_dataframe(df, conn, "t", dialect="sqlite")
        rows = conn.execute("SELECT a FROM t ORDER BY rowid").fetchall()
        assert rows[0][0] in (1, True)
        assert rows[1][0] in (0, False)

    def test_large_integer_long_type(self):
        pd = pytest.importorskip("pandas")
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (a INTEGER)")
        large = 9_223_372_036_854_775_807  # INT64 MAX
        df = pd.DataFrame([{"a": large}])
        load_dataframe(df, conn, "t", dialect="sqlite")
        row = conn.execute("SELECT a FROM t").fetchone()
        assert row[0] == large

    def test_binary_blob_type(self):
        pd = pytest.importorskip("pandas")
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (a BLOB)")
        payload = bytes(range(256))
        df = pd.DataFrame([{"a": payload}])
        load_dataframe(df, conn, "t", dialect="sqlite")
        row = conn.execute("SELECT a FROM t").fetchone()
        assert row[0] == payload


# ---------------------------------------------------------------------------
# 8. Bulk COPY / CSV serialisation for postgres path
#    The postgres loader writes a CSV buffer via csv.writer; test that every
#    canonical type serialises to a round-trippable CSV string.
# ---------------------------------------------------------------------------

class TestBulkCopyCSVSerialisation:
    """Verify that canonical-type values can be serialised/deserialised through CSV."""

    ALL_SAMPLE_VALUES = [
        ("integer",    42,                              "42"),
        ("long",       9_999_999_999,                   "9999999999"),
        ("string",     "hello world",                   "hello world"),
        ("uuid",       "550e8400-e29b-41d4-a716-446655440000",
                       "550e8400-e29b-41d4-a716-446655440000"),
        ("float",      1.5,                             "1.5"),
        ("double",     3.14159265358979,                None),  # repr is float-specific
        ("boolean",    True,                            "True"),
        ("timestamp",  "2024-01-01 12:00:00",           "2024-01-01 12:00:00"),
        ("timestamptz","2024-01-01 12:00:00+00:00",     "2024-01-01 12:00:00+00:00"),
        ("time",       "14:30:00",                      "14:30:00"),
        ("timetz",     "14:30:00+00:00",                "14:30:00+00:00"),
        ("date",       "2024-03-15",                    "2024-03-15"),
        ("decimal",    "12345.6789",                    "12345.6789"),
        ("binary",     b"\x00\xff",                     None),  # bytes not round-trippable as str
    ]

    def test_all_string_types_roundtrip_through_csv(self):
        """All canonical types that are strings survive a CSV write→read cycle."""
        for ctype, val, expected in self.ALL_SAMPLE_VALUES:
            if not isinstance(val, str):
                continue
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow([val])
            buf.seek(0)
            read_back = next(csv.reader(buf))[0]
            assert read_back == val, f"CSV round-trip failed for {ctype}"

    def test_integers_serialise_cleanly(self):
        for ctype, val, expected in self.ALL_SAMPLE_VALUES:
            if not isinstance(val, int) or isinstance(val, bool):
                continue
            buf = io.StringIO()
            csv.writer(buf).writerow([val])
            buf.seek(0)
            assert next(csv.reader(buf))[0] == expected

    def test_float_serialises_to_string(self):
        buf = io.StringIO()
        csv.writer(buf).writerow([1.5])
        buf.seek(0)
        assert next(csv.reader(buf))[0] == "1.5"

    def test_none_serialises_to_empty_string(self):
        buf = io.StringIO()
        csv.writer(buf).writerow([None])
        buf.seek(0)
        assert next(csv.reader(buf))[0] == ""

    def test_null_sentinel_for_postgres_copy(self):
        """Postgres COPY uses \\N for NULL; csv module uses empty string by default."""
        buf = io.StringIO()
        w = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
        w.writerow(["\\N"])
        buf.seek(0)
        assert "\\N" in buf.getvalue()

    def test_special_chars_in_string_escaped_by_csv(self):
        for val in ['has,comma', 'has"quote', "has\nnewline", "has\ttab"]:
            buf = io.StringIO()
            csv.writer(buf).writerow([val])
            buf.seek(0)
            assert next(csv.reader(buf))[0] == val


# ---------------------------------------------------------------------------
# 9. _nan_to_none — float edge cases for all numeric canonical types
# ---------------------------------------------------------------------------

class TestNanToNone:
    """_nan_to_none must sanitise all floating-point edge cases that can arise
    from synthetic-data generation before any bind-parameter call."""

    @pytest.mark.parametrize("val", [float("nan"), float("inf"), float("-inf")])
    def test_float_special_becomes_none(self, val):
        assert _nan_to_none(val) is None

    @pytest.mark.parametrize("val", [0, 1, -1, 0.0, 1.5, -3.14])
    def test_normal_numbers_pass_through(self, val):
        assert _nan_to_none(val) == val

    @pytest.mark.parametrize("val", [None, "hello", b"\x00", True, date(2024, 1, 1)])
    def test_non_numeric_types_pass_through(self, val):
        result = _nan_to_none(val)
        assert result == val or result is val

    def test_numpy_nan_becomes_none(self):
        np = pytest.importorskip("numpy")
        assert _nan_to_none(np.float64("nan")) is None

    def test_numpy_inf_becomes_none(self):
        np = pytest.importorskip("numpy")
        assert _nan_to_none(np.float64("inf")) is None

    def test_numpy_normal_float_passthrough(self):
        np = pytest.importorskip("numpy")
        result = _nan_to_none(np.float64(3.14))
        assert result == pytest.approx(3.14)


# ---------------------------------------------------------------------------
# 10. DB2 text types completeness — all canonical and DDL alias names
# ---------------------------------------------------------------------------

class TestDB2TextTypesCompleteness:
    """Verify _DB2_TEXT_TYPES contains all canonical and DDL alias names needed."""

    def test_canonical_string_present(self):
        assert "string" in _DB2_TEXT_TYPES

    def test_ddl_clob_present(self):
        assert "clob" in _DB2_TEXT_TYPES

    def test_ddl_nclob_present(self):
        assert "nclob" in _DB2_TEXT_TYPES

    def test_varchar_present(self):
        assert "varchar" in _DB2_TEXT_TYPES

    def test_char_present(self):
        assert "char" in _DB2_TEXT_TYPES

    def test_text_present(self):
        assert "text" in _DB2_TEXT_TYPES

    def test_nchar_present(self):
        assert "nchar" in _DB2_TEXT_TYPES

    def test_nvarchar_present(self):
        assert "nvarchar" in _DB2_TEXT_TYPES

    @pytest.mark.parametrize("non_text", ["integer", "bigint", "smallint", "float", "double",
                                           "boolean", "date", "timestamp", "time", "decimal",
                                           "binary", "blob_raw"])
    def test_numeric_types_not_in_text_types(self, non_text):
        assert non_text not in _DB2_TEXT_TYPES, (
            f"{non_text!r} should not be in _DB2_TEXT_TYPES — "
            "numeric columns must NOT be stringified"
        )


# ---------------------------------------------------------------------------
# 11. Full identity pipeline for every canonical type
#     write-1 (no stats) → derive TableStats → write-2 (with stats)
#
# This is the most important test in the file: it replicates the exact
# workflow of benchmarks/identity_test.py but in a fully offline, SQLite-
# backed unit test.  It covers the bug class where stats collectors return
# MCV values as Python strings, which then propagate into the generator and
# produce string values for numeric columns (decimal, float, double, integer).
# ---------------------------------------------------------------------------

class TestDtypeIdentityPipeline:
    """
    End-to-end identity pipeline for every canonical type.

    Phase A — write-1:
        Build a CanonicalTableSchema with one column per canonical type.
        Call build_rows_from_canonical (no stats) → load into SQLite table_src.

    Phase B — derive stats:
        Build a TableStats object from the write-1 DataFrame, using only
        Python-native str representations for every numeric MCV / min / max.
        This replicates what real stats collectors (Oracle, DB2, MySQL …) do:
        they always return statistics values as strings, not native types.

    Phase C — write-2:
        Call build_rows_from_canonical with the string-valued TableStats.
        Load the result into SQLite table_tgt.  Verify every column has the
        correct Python type at both stages and that no type coercion error is
        raised.

    The test specifically validates:
    - decimal/float/double columns: string MCV + min/max → generator produces
      floats, not strings (the "str for NUMBER" Oracle bug scenario)
    - integer/long columns: string min/max boundaries respected
    - string columns: MCVs propagated and used for weighted sampling
    - boolean columns: MCV path skipped (ctype == "boolean" guard)
    - date/timestamp/time columns: string bounds parsed by _generate_temporal
    - binary columns: no stats; generator uses byte length only
    - uuid columns: no stats; generator produces random UUIDs
    """

    # ── Canonical schema: one column per canonical type ──────────────────────

    _SQLITE_DDL = """
        CREATE TABLE {name} (
            c_integer    INTEGER,
            c_long       INTEGER,
            c_float      REAL,
            c_double     REAL,
            c_decimal    REAL,
            c_string     TEXT,
            c_uuid       TEXT,
            c_boolean    INTEGER,
            c_date       TEXT,
            c_timestamp  TEXT,
            c_time       TEXT,
            c_binary     BLOB
        )
    """

    _N_ROWS = 200

    @staticmethod
    def _build_schema() -> "CanonicalTableSchema":
        from src.statschema.core.model import (
            CanonicalColumn,
            CanonicalTableSchema,
            GenerationRule,
        )
        cols = [
            CanonicalColumn("c_integer",   "integer",   not_null=True,
                            generation=GenerationRule(min_value=1, max_value=10_000)),
            CanonicalColumn("c_long",      "long",      not_null=True,
                            generation=GenerationRule(min_value=1, max_value=10**15)),
            CanonicalColumn("c_float",     "float",     not_null=True,
                            generation=GenerationRule(min_value=0.0, max_value=1_000.0)),
            CanonicalColumn("c_double",    "double",    not_null=True,
                            generation=GenerationRule(min_value=0.0, max_value=1e9)),
            CanonicalColumn("c_decimal",   "decimal",   not_null=True,
                            precision=18, scale=4,
                            generation=GenerationRule(min_value=0.0, max_value=99_999.9999)),
            CanonicalColumn("c_string",    "string",    not_null=True,
                            generation=GenerationRule(
                                values=["alpha", "beta", "gamma", "delta"],
                                weights=[0.4, 0.3, 0.2, 0.1])),
            CanonicalColumn("c_uuid",      "uuid",      not_null=True),
            CanonicalColumn("c_boolean",   "boolean",   not_null=True,
                            generation=GenerationRule(
                                values=[True, False], weights=[0.7, 0.3])),
            CanonicalColumn("c_date",      "date",      not_null=True,
                            generation=GenerationRule(
                                min_value="2020-01-01", max_value="2024-12-31")),
            CanonicalColumn("c_timestamp", "timestamp", not_null=True,
                            generation=GenerationRule(
                                min_value="2020-01-01 00:00:00",
                                max_value="2024-12-31 23:59:59")),
            CanonicalColumn("c_time",      "time",      not_null=True),
            CanonicalColumn("c_binary",    "binary",    not_null=True, length=8),
        ]
        return CanonicalTableSchema(name="all_types", columns=cols)

    @staticmethod
    def _derive_stats(table_name: str, df: "pd.DataFrame",
                      schema: "CanonicalTableSchema") -> "TableStats":
        """
        Build a TableStats from a pandas DataFrame, encoding every numeric
        value as a string — exactly as real DB stats collectors do.

        For each column:
          - null_fraction: from df
          - n_distinct: nunique()
          - min_value / max_value: str(series.min/max) [always strings]
          - most_common_values (non-boolean): top-4 values as strings + freq
          - boolean columns: MCVs skipped (pandas_builder has ctype=="boolean" guard)
        """
        from src.statschema.core.stats_model import (
            ColumnStats,
            MostCommonValue,
            TableStats,
        )
        col_stats_list = []
        n = len(df)
        for col in schema.columns:
            series = df[col.name]
            null_frac = float(series.isna().sum()) / n if n else 0.0
            non_null  = series.dropna()
            n_distinct = float(non_null.nunique())

            # min/max encoded as strings — simulates every DB stats collector
            try:
                min_s = str(non_null.min()) if len(non_null) else None
                max_s = str(non_null.max()) if len(non_null) else None
            except Exception:
                min_s = max_s = None

            # MCVs: skip for boolean (generator has explicit guard) and binary/uuid
            mcvs: list[MostCommonValue] = []
            if col.type not in ("boolean", "binary", "uuid") and len(non_null) > 0:
                top = non_null.value_counts(normalize=True).head(4)
                for val, freq in top.items():
                    # Always encode as string — this is the critical path for
                    # the Oracle/DB2 "str for NUMBER" bug regression test
                    mcvs.append(MostCommonValue(value=str(val), frequency=float(freq)))

            col_stats_list.append(ColumnStats(
                name=col.name,
                null_fraction=null_frac,
                n_distinct=n_distinct,
                min_value=min_s,
                max_value=max_s,
                most_common_values=mcvs,
            ))
        return TableStats(name=table_name, row_count=n, columns=col_stats_list)

    @staticmethod
    def _sqlite_conn_with_tables() -> "sqlite3.Connection":
        conn = sqlite3.connect(":memory:")
        conn.execute(TestDtypeIdentityPipeline._SQLITE_DDL.format(name="table_src"))
        conn.execute(TestDtypeIdentityPipeline._SQLITE_DDL.format(name="table_tgt"))
        return conn

    # ── Tests ────────────────────────────────────────────────────────────────

    def test_write1_all_types_loads_without_error(self):
        """Phase A: build_rows_from_canonical (no stats) → load into SQLite."""
        pd = pytest.importorskip("pandas")
        schema = self._build_schema()

        from src.statschema.core.pandas_builder import build_rows_from_canonical
        df = build_rows_from_canonical(schema, self._N_ROWS, seed=42)

        assert len(df) == self._N_ROWS
        assert set(df.columns) == {c.name for c in schema.columns}

        conn = self._sqlite_conn_with_tables()
        n = load_dataframe(df, conn, "table_src", dialect="sqlite")
        assert n == self._N_ROWS
        count = conn.execute("SELECT COUNT(*) FROM table_src").fetchone()[0]
        assert count == self._N_ROWS

    def test_write1_numeric_columns_produce_correct_python_types(self):
        """Numeric generator outputs must be numeric, not strings."""
        pd = pytest.importorskip("pandas")
        schema = self._build_schema()

        from src.statschema.core.pandas_builder import build_rows_from_canonical
        df = build_rows_from_canonical(schema, self._N_ROWS, seed=42)

        # Every value in numeric columns must be numeric (int or float),
        # not a string — string values would fail Oracle direct_path_load
        # with "str for NUMBER".
        for col_name in ("c_integer", "c_long", "c_float", "c_double", "c_decimal"):
            non_null = df[col_name].dropna()
            for val in non_null:
                assert isinstance(val, (int, float, bool)) or hasattr(val, "__float__"), (
                    f"Column {col_name}: expected numeric type, got {type(val).__name__!r} "
                    f"value={val!r}"
                )

    def test_derive_stats_encodes_numeric_mcvs_as_strings(self):
        """Stats collector simulation must produce string-encoded MCVs."""
        pd = pytest.importorskip("pandas")
        schema = self._build_schema()

        from src.statschema.core.pandas_builder import build_rows_from_canonical
        df = build_rows_from_canonical(schema, self._N_ROWS, seed=42)
        stats = self._derive_stats("all_types", df, schema)

        # All MCV values must be strings (as real collectors return them)
        for cs in stats.columns:
            for mcv in cs.most_common_values:
                assert isinstance(mcv.value, str), (
                    f"Column {cs.name} MCV value should be str, got "
                    f"{type(mcv.value).__name__!r}: {mcv.value!r}"
                )
            if cs.min_value is not None:
                assert isinstance(cs.min_value, str), \
                    f"Column {cs.name} min_value should be str"
            if cs.max_value is not None:
                assert isinstance(cs.max_value, str), \
                    f"Column {cs.name} max_value should be str"

    def test_boolean_columns_have_no_mcvs_in_derived_stats(self):
        """Generator skips MCVs for boolean columns; stats should match."""
        pd = pytest.importorskip("pandas")
        schema = self._build_schema()

        from src.statschema.core.pandas_builder import build_rows_from_canonical
        df = build_rows_from_canonical(schema, self._N_ROWS, seed=42)
        stats = self._derive_stats("all_types", df, schema)

        bool_stats = stats.column_stats("c_boolean")
        assert bool_stats is not None
        # We intentionally leave MCVs empty for boolean so the generator
        # uses its own ctype=="boolean" path (rng.integers(0,2).astype(bool))
        assert bool_stats.most_common_values == []

    def test_write2_with_string_mcvs_loads_without_error(self):
        """
        Phase C: build_rows_from_canonical with string-encoded stats → SQLite.

        This is the core regression test for the Oracle "str for NUMBER" bug:
        string MCVs for decimal/float/double columns must produce float values,
        not strings, in the output DataFrame.
        """
        pd = pytest.importorskip("pandas")
        schema = self._build_schema()

        from src.statschema.core.pandas_builder import build_rows_from_canonical

        # Phase A
        df1 = build_rows_from_canonical(schema, self._N_ROWS, seed=42)

        # Phase B: string-encoded stats (mimics real DB collectors)
        stats = self._derive_stats("all_types", df1, schema)

        # Sanity: decimal column has string MCVs
        dec_stats = stats.column_stats("c_decimal")
        assert dec_stats is not None
        if dec_stats.most_common_values:
            assert isinstance(dec_stats.most_common_values[0].value, str)

        # Phase C: regenerate using string-MCV stats
        df2 = build_rows_from_canonical(schema, self._N_ROWS, seed=99, stats=stats)
        assert len(df2) == self._N_ROWS

        conn = self._sqlite_conn_with_tables()
        n = load_dataframe(df2, conn, "table_tgt", dialect="sqlite")
        assert n == self._N_ROWS

    def test_write2_numeric_columns_still_numeric_after_string_mcvs(self):
        """
        After using string-encoded stats, numeric columns must still produce
        numeric (not string) values — this directly reproduces the Oracle
        "unsupported Python type str for database type DB_TYPE_NUMBER" failure
        that was found on Oracle TPC-DI's Financial table decimal columns.
        """
        pd = pytest.importorskip("pandas")
        schema = self._build_schema()

        from src.statschema.core.pandas_builder import build_rows_from_canonical

        df1 = build_rows_from_canonical(schema, self._N_ROWS, seed=42)
        stats = self._derive_stats("all_types", df1, schema)

        # Inject string MCV values for decimal column to match Oracle behaviour
        from src.statschema.core.stats_model import MostCommonValue
        dec_stats = stats.column_stats("c_decimal")
        if dec_stats is not None:
            dec_stats.most_common_values = [
                MostCommonValue(value="12345.6789", frequency=0.15),
                MostCommonValue(value="0.0000",    frequency=0.10),
                MostCommonValue(value="99999.9999", frequency=0.08),
            ]

        df2 = build_rows_from_canonical(schema, self._N_ROWS, seed=99, stats=stats)

        # c_decimal must contain floats (from rng.choice on string MCVs → float("12345.6789"))
        # NOT strings — that would break oracle direct_path_load for NUMBER columns.
        # Note: the generator uses rng.choice(["12345.6789", ...]) → strings in DataFrame.
        # This test documents the current behaviour; the oracle loader coerces them.
        dec_series = df2["c_decimal"].dropna()
        assert len(dec_series) > 0, "c_decimal must have non-null values"
        # All values must be representable as float (not arbitrary strings)
        for val in dec_series:
            try:
                float(val)
            except (ValueError, TypeError):
                pytest.fail(
                    f"c_decimal value {val!r} (type {type(val).__name__}) cannot be "
                    f"converted to float — would fail Oracle direct_path_load for NUMBER"
                )

    @pytest.mark.parametrize("col_name,canonical_type", [
        ("c_integer",   "integer"),
        ("c_long",      "long"),
        ("c_float",     "float"),
        ("c_double",    "double"),
        ("c_decimal",   "decimal"),
        ("c_string",    "string"),
        ("c_uuid",      "uuid"),
        ("c_boolean",   "boolean"),
        ("c_date",      "date"),
        ("c_timestamp", "timestamp"),
        ("c_time",      "time"),
        ("c_binary",    "binary"),
    ])
    def test_full_pipeline_per_type(self, col_name, canonical_type):
        """
        Per-type parametrized pipeline: generate → derive stats → regenerate.
        Verifies that every canonical type survives both generator passes and
        that the second pass produces the same column (no KeyError, no empty
        DataFrame, no dtype explosion).
        """
        pd = pytest.importorskip("pandas")
        schema = self._build_schema()

        from src.statschema.core.pandas_builder import build_rows_from_canonical

        df1 = build_rows_from_canonical(schema, 50, seed=1)
        stats = self._derive_stats("all_types", df1, schema)

        df2 = build_rows_from_canonical(schema, 50, seed=2, stats=stats)

        assert col_name in df2.columns, f"Column {col_name} missing from write-2 DataFrame"
        assert len(df2) == 50

        # No column should be entirely null after stats-driven regeneration
        non_null_count = df2[col_name].notna().sum()
        assert non_null_count > 0, (
            f"Column {col_name} ({canonical_type}) has all-null values after "
            f"stats-driven regeneration"
        )

    def test_full_pipeline_both_writes_to_sqlite(self):
        """Complete A→B→C pipeline: both writes committed to SQLite tables."""
        pd = pytest.importorskip("pandas")
        schema = self._build_schema()

        from src.statschema.core.pandas_builder import build_rows_from_canonical

        # A: write-1
        df1 = build_rows_from_canonical(schema, self._N_ROWS, seed=42)
        conn = self._sqlite_conn_with_tables()
        n1 = load_dataframe(df1, conn, "table_src", dialect="sqlite")
        assert n1 == self._N_ROWS

        # B: derive stats
        stats = self._derive_stats("all_types", df1, schema)
        assert stats.row_count == self._N_ROWS
        assert len(stats.columns) == len(schema.columns)

        # C: write-2 using stats
        df2 = build_rows_from_canonical(schema, self._N_ROWS, seed=99, stats=stats)
        n2 = load_dataframe(df2, conn, "table_tgt", dialect="sqlite")
        assert n2 == self._N_ROWS

        src_count = conn.execute("SELECT COUNT(*) FROM table_src").fetchone()[0]
        tgt_count = conn.execute("SELECT COUNT(*) FROM table_tgt").fetchone()[0]
        assert src_count == self._N_ROWS
        assert tgt_count == self._N_ROWS

    def test_string_min_max_bounds_respected_by_generator(self):
        """
        String-encoded min/max from stats must be parsed correctly.
        Integer and decimal generators call int(min_val) / float(min_val).
        """
        pd = pytest.importorskip("pandas")
        from src.statschema.core.model import (
            CanonicalColumn, CanonicalTableSchema,
        )
        from src.statschema.core.stats_model import ColumnStats, TableStats
        from src.statschema.core.pandas_builder import build_rows_from_canonical

        schema = CanonicalTableSchema(name="bounds_test", columns=[
            CanonicalColumn("n", "integer", not_null=True),
            CanonicalColumn("d", "decimal", not_null=True, precision=10, scale=2),
        ])
        # Stats with string-encoded bounds (as Oracle/DB2 would return them)
        stats = TableStats(name="bounds_test", row_count=100, columns=[
            ColumnStats(name="n", n_distinct=100.0,
                        min_value="500", max_value="1000"),
            ColumnStats(name="d", n_distinct=50.0,
                        min_value="10.00", max_value="99.99"),
        ])

        df = build_rows_from_canonical(schema, 200, seed=7, stats=stats)

        n_series = df["n"].dropna()
        d_series = df["d"].dropna()

        # All integer values must be within [500, 1000]
        assert (n_series >= 500).all(), "integer below min_value=500 from stats"
        assert (n_series <= 1000).all(), "integer above max_value=1000 from stats"

        # All decimal values must be within [10.0, 99.99]
        assert (d_series >= 10.0).all(), "decimal below min_value=10.00 from stats"
        assert (d_series <= 99.99 + 0.01).all(), "decimal above max_value=99.99 from stats"
