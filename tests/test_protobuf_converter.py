"""
pytest tests for src/protobuf_converter.py

Tests (no Spark required unless noted):
  - schema_to_proto_str: .proto content, type mapping, message naming
  - compile_proto:        runtime protoc compilation → Message class
  - row_to_message:       dict/Row → Message field values, timestamp conversion
  - serialize_row:        bytes round-trip

Tests requiring Spark (use _create_spark_session):
  - end-to-end: canonical schema → DataFrame → protobuf bytes
"""

import os
from datetime import datetime

import pytest

from src.statschema.model import CanonicalColumn, CanonicalTableSchema
from src.zbhelper.protobuf_converter import (
    _proto_message_name,
    schema_to_proto_str,
    compile_proto,
    row_to_message,
    serialize_row,
)


# ---------------------------------------------------------------------------
# Helper: Spark session (mirrors test_statschema.py pattern)
# ---------------------------------------------------------------------------

def _create_spark_session(app_name: str):
    try:
        from databricks.connect import DatabricksSession
        return DatabricksSession.builder.getOrCreate()
    except Exception:
        pass

    pyspark = pytest.importorskip("pyspark")
    SparkSession = pyspark.sql.SparkSession
    saved = {}
    for key in list(os.environ):
        if key == "SPARK_REMOTE" or key.startswith("DATABRICKS_CONNECT"):
            saved[key] = os.environ.pop(key)
    try:
        return (
            SparkSession.builder.master("local[1]")
            .appName(app_name)
            .getOrCreate()
        )
    except RuntimeError:
        raise
    finally:
        for key, value in saved.items():
            os.environ[key] = value


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _simple_table() -> CanonicalTableSchema:
    return CanonicalTableSchema(
        name="order",
        columns=[
            CanonicalColumn(name="order_id", type="integer"),
            CanonicalColumn(name="customer_name", type="string"),
            CanonicalColumn(name="amount", type="double"),
            CanonicalColumn(name="active", type="boolean"),
            CanonicalColumn(name="created_at", type="timestamp"),
        ],
    )


def _all_types_table() -> CanonicalTableSchema:
    return CanonicalTableSchema(
        name="all_types",
        columns=[
            CanonicalColumn(name="int_col", type="integer"),
            CanonicalColumn(name="long_col", type="long"),
            CanonicalColumn(name="float_col", type="float"),
            CanonicalColumn(name="double_col", type="double"),
            CanonicalColumn(name="str_col", type="string"),
            CanonicalColumn(name="bool_col", type="boolean"),
            CanonicalColumn(name="ts_col", type="timestamp"),
        ],
    )


# ---------------------------------------------------------------------------
# _proto_message_name
# ---------------------------------------------------------------------------

class TestProtoMessageName:
    def test_simple(self):
        assert _proto_message_name("order") == "Order"

    def test_snake_case(self):
        assert _proto_message_name("order_item") == "OrderItem"

    def test_multi_word(self):
        assert _proto_message_name("customer_order_detail") == "CustomerOrderDetail"

    def test_hyphen_treated_as_underscore(self):
        assert _proto_message_name("my-table") == "MyTable"

    def test_already_pascal(self):
        assert _proto_message_name("Order") == "Order"


# ---------------------------------------------------------------------------
# schema_to_proto_str
# ---------------------------------------------------------------------------

class TestSchemaToProtoStr:
    def test_syntax_line(self):
        proto = schema_to_proto_str(_simple_table())
        assert proto.startswith('syntax = "proto2";')

    def test_message_name(self):
        proto = schema_to_proto_str(_simple_table())
        assert "message Order {" in proto

    def test_field_count(self):
        proto = schema_to_proto_str(_simple_table())
        field_lines = [l for l in proto.splitlines() if l.strip().startswith("optional")]
        assert len(field_lines) == 5

    def test_integer_maps_to_int32(self):
        proto = schema_to_proto_str(_simple_table())
        assert "optional int32 order_id = 1;" in proto

    def test_string_maps_to_string(self):
        proto = schema_to_proto_str(_simple_table())
        assert "optional string customer_name = 2;" in proto

    def test_double_maps_to_double(self):
        proto = schema_to_proto_str(_simple_table())
        assert "optional double amount = 3;" in proto

    def test_boolean_maps_to_bool(self):
        proto = schema_to_proto_str(_simple_table())
        assert "optional bool active = 4;" in proto

    def test_timestamp_maps_to_int64(self):
        proto = schema_to_proto_str(_simple_table())
        assert "optional int64 created_at = 5;" in proto

    def test_all_canonical_types(self):
        proto = schema_to_proto_str(_all_types_table())
        assert "optional int32 int_col = 1;" in proto
        assert "optional int64 long_col = 2;" in proto
        assert "optional float float_col = 3;" in proto
        assert "optional double double_col = 4;" in proto
        assert "optional string str_col = 5;" in proto
        assert "optional bool bool_col = 6;" in proto
        assert "optional int64 ts_col = 7;" in proto

    def test_unknown_type_falls_back_to_string(self):
        table = CanonicalTableSchema(
            name="t",
            columns=[CanonicalColumn(name="x", type="jsonb")],
        )
        proto = schema_to_proto_str(table)
        assert "optional string x = 1;" in proto

    def test_field_numbers_are_sequential(self):
        table = _all_types_table()
        proto = schema_to_proto_str(table)
        for i, col in enumerate(table.columns, start=1):
            assert f"= {i};" in proto

    def test_closing_brace(self):
        proto = schema_to_proto_str(_simple_table())
        assert proto.strip().endswith("}")

    def test_empty_table_produces_valid_proto(self):
        table = CanonicalTableSchema(name="empty", columns=[])
        proto = schema_to_proto_str(table)
        assert "message Empty {" in proto
        assert proto.strip().endswith("}")


# ---------------------------------------------------------------------------
# compile_proto
# ---------------------------------------------------------------------------

class TestCompileProto:
    def test_returns_message_class(self):
        pytest.importorskip("grpc_tools")
        table = CanonicalTableSchema(
            name="phase1",
            columns=[CanonicalColumn(name="col_1", type="integer")],
        )
        proto_str = schema_to_proto_str(table)
        cls = compile_proto(proto_str, "phase1")
        assert cls is not None
        assert callable(cls)

    def test_message_class_name_matches(self):
        pytest.importorskip("grpc_tools")
        table = CanonicalTableSchema(
            name="order_item",
            columns=[CanonicalColumn(name="id", type="long")],
        )
        proto_str = schema_to_proto_str(table)
        cls = compile_proto(proto_str, "order_item")
        # compile_proto appends a unique counter suffix to avoid descriptor pool conflicts
        assert cls.__name__.startswith("OrderItem")

    def test_message_has_expected_fields(self):
        pytest.importorskip("grpc_tools")
        proto_str = schema_to_proto_str(_simple_table())
        cls = compile_proto(proto_str, "order")
        descriptor = cls.DESCRIPTOR
        field_names = {f.name for f in descriptor.fields}
        assert "order_id" in field_names
        assert "customer_name" in field_names
        assert "amount" in field_names
        assert "active" in field_names
        assert "created_at" in field_names

    def test_all_types_compiles(self):
        pytest.importorskip("grpc_tools")
        proto_str = schema_to_proto_str(_all_types_table())
        cls = compile_proto(proto_str, "all_types")
        assert len(cls.DESCRIPTOR.fields) == 7

    def test_invalid_proto_raises_runtime_error(self):
        pytest.importorskip("grpc_tools")
        with pytest.raises(RuntimeError, match="protoc compilation failed"):
            compile_proto("this is not valid proto", "bad_proto")


# ---------------------------------------------------------------------------
# row_to_message
# ---------------------------------------------------------------------------

class TestRowToMessage:
    def _get_message_class(self, table: CanonicalTableSchema):
        pytest.importorskip("grpc_tools")
        proto_str = schema_to_proto_str(table)
        return compile_proto(proto_str, table.name)

    def test_integer_field(self):
        table = CanonicalTableSchema(
            name="t", columns=[CanonicalColumn(name="n", type="integer")]
        )
        cls = self._get_message_class(table)
        msg = row_to_message({"n": 42}, cls)
        assert msg.n == 42

    def test_string_field(self):
        table = CanonicalTableSchema(
            name="t", columns=[CanonicalColumn(name="s", type="string")]
        )
        cls = self._get_message_class(table)
        msg = row_to_message({"s": "hello"}, cls)
        assert msg.s == "hello"

    def test_boolean_field(self):
        table = CanonicalTableSchema(
            name="t", columns=[CanonicalColumn(name="b", type="boolean")]
        )
        cls = self._get_message_class(table)
        msg = row_to_message({"b": True}, cls)
        assert msg.b is True

    def test_none_fields_are_skipped(self):
        table = CanonicalTableSchema(
            name="t",
            columns=[
                CanonicalColumn(name="a", type="integer"),
                CanonicalColumn(name="b", type="string"),
            ],
        )
        cls = self._get_message_class(table)
        msg = row_to_message({"a": 1, "b": None}, cls)
        assert msg.a == 1
        assert not msg.HasField("b")

    def test_timestamp_converted_to_epoch_ms(self):
        table = CanonicalTableSchema(
            name="t", columns=[CanonicalColumn(name="ts", type="timestamp")]
        )
        cls = self._get_message_class(table)
        dt = datetime(2024, 1, 15, 12, 0, 0)
        msg = row_to_message({"ts": dt}, cls)
        expected_ms = int(dt.timestamp() * 1000)
        assert msg.ts == expected_ms

    def test_dict_input(self):
        table = CanonicalTableSchema(
            name="t", columns=[CanonicalColumn(name="val", type="long")]
        )
        cls = self._get_message_class(table)
        msg = row_to_message({"val": 999_999_999_999}, cls)
        assert msg.val == 999_999_999_999


# ---------------------------------------------------------------------------
# serialize_row
# ---------------------------------------------------------------------------

class TestSerializeRow:
    def _get_message_class(self, table: CanonicalTableSchema):
        pytest.importorskip("grpc_tools")
        proto_str = schema_to_proto_str(table)
        return compile_proto(proto_str, table.name)

    def test_returns_bytes(self):
        table = CanonicalTableSchema(
            name="t", columns=[CanonicalColumn(name="n", type="integer")]
        )
        cls = self._get_message_class(table)
        result = serialize_row({"n": 7}, cls)
        assert isinstance(result, bytes)

    def test_round_trip(self):
        """Serialize then parse back, check field values are preserved."""
        table = CanonicalTableSchema(
            name="roundtrip",
            columns=[
                CanonicalColumn(name="id", type="integer"),
                CanonicalColumn(name="name", type="string"),
                CanonicalColumn(name="score", type="double"),
            ],
        )
        cls = self._get_message_class(table)
        row = {"id": 123, "name": "Alice", "score": 9.5}
        serialized = serialize_row(row, cls)
        recovered = cls()
        recovered.ParseFromString(serialized)
        assert recovered.id == 123
        assert recovered.name == "Alice"
        assert abs(recovered.score - 9.5) < 1e-6

    def test_empty_row_is_valid_bytes(self):
        table = CanonicalTableSchema(
            name="t", columns=[CanonicalColumn(name="x", type="integer")]
        )
        cls = self._get_message_class(table)
        result = serialize_row({}, cls)
        assert isinstance(result, bytes)


# ---------------------------------------------------------------------------
# End-to-end: canonical schema -> DataFrame -> protobuf bytes (requires Spark)
# ---------------------------------------------------------------------------

class TestEndToEnd:
    def test_phase1_schema_to_proto_bytes(self):
        """Phase 1 target: 1 table, 1 integer column, 1000 rows -> protobuf bytes."""
        pytest.importorskip("dbldatagen")
        pytest.importorskip("grpc_tools")

        from src.statschema.dbldatagen_builder import build_dataframe_from_canonical

        table = CanonicalTableSchema(
            name="phase1_table",
            columns=[CanonicalColumn(name="col_1", type="integer")],
        )
        try:
            spark = _create_spark_session("protobuf_converter_e2e_test")
        except RuntimeError as e:
            if "Databricks Connect" in str(e) or "remote Spark" in str(e):
                pytest.skip("Local Spark not available")
            raise

        df = build_dataframe_from_canonical(spark, table, rows=1000, partitions=2, seed=42)
        assert df.count() == 1000

        proto_str = schema_to_proto_str(table)
        msg_class = compile_proto(proto_str, table.name)

        rows = df.collect()
        serialized = [serialize_row(r, msg_class) for r in rows]
        assert len(serialized) == 1000
        assert all(isinstance(b, bytes) for b in serialized)
        assert all(len(b) > 0 for b in serialized)

        # Round-trip spot check: first row
        recovered = msg_class()
        recovered.ParseFromString(serialized[0])
        assert isinstance(recovered.col_1, int)

    def test_multi_type_schema_round_trip(self):
        """Multi-column schema with mixed types: DataFrame -> serialize -> parse -> verify."""
        pytest.importorskip("dbldatagen")
        pytest.importorskip("grpc_tools")

        from src.statschema.dbldatagen_builder import build_dataframe_from_canonical

        table = CanonicalTableSchema(
            name="multi_type",
            columns=[
                CanonicalColumn(name="id", type="integer"),
                CanonicalColumn(name="label", type="string"),
                CanonicalColumn(name="score", type="double"),
                CanonicalColumn(name="active", type="boolean"),
            ],
        )
        try:
            spark = _create_spark_session("protobuf_converter_multi_type_test")
        except RuntimeError as e:
            if "Databricks Connect" in str(e) or "remote Spark" in str(e):
                pytest.skip("Local Spark not available")
            raise

        df = build_dataframe_from_canonical(spark, table, rows=100, partitions=1, seed=7)
        assert df.count() == 100

        proto_str = schema_to_proto_str(table)
        msg_class = compile_proto(proto_str, table.name)

        rows = df.collect()
        for row in rows[:5]:
            serialized = serialize_row(row, msg_class)
            assert isinstance(serialized, bytes)
            recovered = msg_class()
            recovered.ParseFromString(serialized)
            assert isinstance(recovered.id, int)
            assert isinstance(recovered.label, str)
