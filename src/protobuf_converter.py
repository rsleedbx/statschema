"""
Protobuf converter: canonical schema -> .proto file, DataFrame row -> serialized Message.

Flow:
  1. schema_to_proto_str(table)       -> .proto file content (string)
  2. compile_proto(proto_str, name)   -> generated Message class
  3. row_to_message(row, msg_class)   -> protobuf Message instance
  4. serialize_row(row, msg_class)    -> bytes

Type mapping (canonical -> Proto2):
  integer   -> int32
  long      -> int64
  float     -> float
  double    -> double
  string    -> string
  boolean   -> bool
  timestamp -> int64  (epoch milliseconds)
"""

import importlib
import importlib.util
import itertools
import os
import tempfile
from typing import Any

_compile_counter = itertools.count()

from src.statschema.model import CanonicalTableSchema

_CANONICAL_TO_PROTO = {
    "integer": "int32",
    "long": "int64",
    "float": "float",
    "double": "double",
    "string": "string",
    "boolean": "bool",
    "timestamp": "int64",
}


def schema_to_proto_str(table: CanonicalTableSchema) -> str:
    """
    Generate a Proto2 .proto file string from a CanonicalTableSchema.

    Each column becomes an `optional <proto_type> <name> = <field_number>` field.
    Unknown canonical types default to `string`.
    """
    lines = [
        'syntax = "proto2";',
        "",
        f"message {_proto_message_name(table.name)} {{",
    ]
    for i, col in enumerate(table.columns, start=1):
        proto_type = _CANONICAL_TO_PROTO.get(col.type.lower(), "string")
        lines.append(f"  optional {proto_type} {col.name} = {i};")
    lines.append("}")
    return "\n".join(lines) + "\n"


def compile_proto(proto_str: str, message_name: str) -> type:
    """
    Compile a .proto string at runtime using grpc_tools.protoc and return the
    generated Message class.

    Writes the .proto to a temp directory, compiles it, imports the *_pb2 module,
    and returns the Message class for `message_name`.

    A unique counter suffix is appended to the file name to avoid "duplicate file
    name" errors in the global protobuf descriptor pool when the same logical name
    is compiled more than once in the same process (common in pytest).
    """
    from grpc_tools import protoc

    # Make both the file name and message symbol unique to avoid descriptor pool
    # conflicts when the same logical name is compiled multiple times (e.g. pytest).
    suffix = next(_compile_counter)
    orig_msg_name = _proto_message_name(message_name)
    unique_msg_name = f"{orig_msg_name}_{suffix}"
    unique_file_name = f"{message_name}_{suffix}"
    unique_proto_str = proto_str.replace(
        f"message {orig_msg_name} {{", f"message {unique_msg_name} {{"
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        proto_file = os.path.join(tmpdir, f"{unique_file_name}.proto")
        with open(proto_file, "w") as f:
            f.write(unique_proto_str)

        ret = protoc.main([
            "grpc_tools.protoc",
            f"--proto_path={tmpdir}",
            f"--python_out={tmpdir}",
            proto_file,
        ])
        if ret != 0:
            raise RuntimeError(f"protoc compilation failed (exit {ret}) for:\n{proto_str}")

        pb2_file = os.path.join(tmpdir, f"{unique_file_name}_pb2.py")
        spec = importlib.util.spec_from_file_location(f"{unique_file_name}_pb2", pb2_file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

    return getattr(module, unique_msg_name)


def row_to_message(row: Any, message_class: type) -> Any:
    """
    Convert a single Spark Row (or dict) to a protobuf Message instance.

    Only columns that exist as fields in `message_class` are mapped, so extra
    columns added by dbldatagen (e.g. the internal `id` from withIdOutput()) are
    silently ignored.  Timestamp values are converted to epoch milliseconds (int64).
    """
    valid_fields = {f.name for f in message_class.DESCRIPTOR.fields}
    kwargs = {}
    row_dict = row.asDict() if hasattr(row, "asDict") else dict(row)
    for field_name, value in row_dict.items():
        if value is None or field_name not in valid_fields:
            continue
        # Convert datetime/Timestamp to epoch ms
        if hasattr(value, "timestamp"):
            value = int(value.timestamp() * 1000)
        kwargs[field_name] = value
    return message_class(**kwargs)


def serialize_row(row: Any, message_class: type) -> bytes:
    """Convert a Spark Row to serialized protobuf bytes."""
    return row_to_message(row, message_class).SerializeToString()


def _proto_message_name(table_name: str) -> str:
    """Convert a table name to a PascalCase protobuf message name."""
    return "".join(part.capitalize() for part in table_name.replace("-", "_").split("_"))
