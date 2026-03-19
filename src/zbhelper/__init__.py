"""
zbhelper — Databricks / ZeroBus helper utilities.

Submodules
----------
protobuf_converter   canonical schema → .proto + Spark Row → protobuf bytes
zerobus_ingest       serialize a Spark DataFrame and stream it into ZeroBus
workspace_region_info  retrieve workspace region, zones, and ZeroBus endpoints
"""

from .protobuf_converter import (
    schema_to_proto_str,
    compile_proto,
    row_to_message,
    serialize_row,
)
from .zerobus_ingest import (
    IngestConfig,
    IngestResult,
    ingest_dataframe,
    build_qualified_table_name,
    build_zerobus_endpoint,
    DatabricksSdkHeadersProvider,
)
from .workspace_region_info import (
    WorkspaceInfoRetriever,
    get_workspace_info,
    save_workspace_info_to_file,
)

__all__ = [
    # protobuf_converter
    "schema_to_proto_str",
    "compile_proto",
    "row_to_message",
    "serialize_row",
    # zerobus_ingest
    "IngestConfig",
    "IngestResult",
    "ingest_dataframe",
    "build_qualified_table_name",
    "build_zerobus_endpoint",
    "DatabricksSdkHeadersProvider",
    # workspace_region_info
    "WorkspaceInfoRetriever",
    "get_workspace_info",
    "save_workspace_info_to_file",
]
