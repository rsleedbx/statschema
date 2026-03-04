"""
Schema parser: load schema from multiple formats (SDV, YData/Syda YAML, pipeline YAML;
MySQL/PostgreSQL/SQL Server schema-only DDL) and convert to a canonical model,
then to Databricks Labs Data Generator (dbldatagen) column specs.
"""

from .model import CanonicalColumn, CanonicalTableSchema, GenerationRule
from .ydata_parser import parse_ydata_yaml, parse_ydata_yaml_file, parse_ydata_multi_yaml
from .pipeline_parser import parse_pipeline_tables, parse_pipeline_config
from .sdv_parser import parse_sdv_metadata, parse_sdv_file
from .ddl_parser import parse_ddl, parse_ddl_file
from .dbldatagen_builder import to_dbldatagen_specs, build_dataframe_from_canonical
from .loader import (
    load_schema,
    detect_format,
    get_schema_source_from_data,
    SchemaSource,
)

__all__ = [
    "CanonicalColumn",
    "CanonicalTableSchema",
    "GenerationRule",
    "parse_ydata_yaml",
    "parse_ydata_yaml_file",
    "parse_ydata_multi_yaml",
    "parse_pipeline_tables",
    "parse_pipeline_config",
    "parse_sdv_metadata",
    "parse_sdv_file",
    "parse_ddl",
    "parse_ddl_file",
    "to_dbldatagen_specs",
    "build_dataframe_from_canonical",
    "load_schema",
    "detect_format",
    "get_schema_source_from_data",
    "SchemaSource",
]
