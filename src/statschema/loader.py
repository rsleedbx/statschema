"""
Detect schema format and load into canonical table(s).
Supports: sdv, ydata, pipeline; mysql, postgres, sqlserver (schema-only DDL from mysqldump, pg_dump, mssql-scripter).
"""

import json
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from .model import CanonicalTableSchema
from .ydata_parser import parse_ydata_yaml, parse_ydata_multi_yaml
from .pipeline_parser import parse_pipeline_config
from .sdv_parser import parse_sdv_metadata
from .ddl_parser import parse_ddl, parse_ddl_file


class SchemaSource(str, Enum):
    """Recognized schema source / format tags (use in YAML: schema_source: sdv | ydata | pipeline | ...)."""

    YDATA = "ydata"
    SDV = "sdv"
    PIPELINE = "pipeline"
    SQLSERVER = "sqlserver"
    POSTGRES = "postgres"
    MYSQL = "mysql"
    ORACLE = "oracle"


def get_schema_source_from_data(data: dict[str, Any]) -> SchemaSource | None:
    """Read schema_source (or source/schema_format/format) from config. Returns enum member or None."""
    for key in ("schema_source", "source", "schema_format", "format"):
        val = data.get(key)
        if val is None:
            continue
        s = str(val).strip().lower()
        if s == "syda":
            return SchemaSource.YDATA
        try:
            return SchemaSource(s)
        except ValueError:
            continue
    return None


def detect_format(data: dict[str, Any]) -> SchemaSource:
    """
    Heuristic: SDV has METADATA_SPEC_VERSION and tables with columns/sdtype.
    YData has __table_description__ or field-level type/constraints/description.
    Pipeline has 'pipeline.tables' or top-level 'tables' list of { name, columns: [...] }.
    """
    # SDV: tables is a dict of table_name -> { primary_key, columns: { col_name -> { sdtype } } }
    if data.get("METADATA_SPEC_VERSION") and "tables" in data:
        tables = data["tables"]
        if isinstance(tables, dict) and tables:
            first = next(iter(tables.values()))
            if isinstance(first, dict) and "columns" in first:
                cols = first.get("columns") or {}
                if isinstance(cols, dict) and cols:
                    first_col = next(iter(cols.values()))
                    if isinstance(first_col, dict) and "sdtype" in first_col:
                        return SchemaSource.SDV

    p = data.get("pipeline") if isinstance(data.get("pipeline"), dict) else data
    if p and "tables" in p and isinstance(p["tables"], list):
        for t in p["tables"]:
            if isinstance(t, dict) and "columns" in t:
                return SchemaSource.PIPELINE
    if "tables" in data and isinstance(data["tables"], list):
        for t in data["tables"]:
            if isinstance(t, dict) and "columns" in t:
                return SchemaSource.PIPELINE

    if "__table_description__" in data or "__foreign_keys__" in data:
        return SchemaSource.YDATA
    for v in (data.values() if isinstance(data, dict) else []):
        if isinstance(v, dict) and ("type" in v or "constraints" in v or "description" in v):
            return SchemaSource.YDATA
    return SchemaSource.PIPELINE


def _load_data_from_path(path: Path) -> tuple[dict[str, Any], str | None]:
    """Load dict from JSON or YAML file. Returns (data, table_name_from_stem)."""
    if path.suffix.lower() == ".json":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    else:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    if not data or not isinstance(data, dict):
        raise ValueError(f"Empty or invalid schema file: {path}")
    table_name = path.stem if path.suffix else None
    return data, table_name


def _is_sql_dialect(fmt: SchemaSource) -> bool:
    """True if format is a database DDL dialect (schema-only dump)."""
    return fmt in (SchemaSource.MYSQL, SchemaSource.POSTGRES, SchemaSource.SQLSERVER)


def load_schema(
    source: str | Path | dict[str, Any],
    format_hint: SchemaSource | str | None = None,
    table_name: str | None = None,
) -> list[CanonicalTableSchema]:
    """
    Load schema from a file path (YAML/JSON), or dict. Returns list of CanonicalTableSchema.

    - source: path to schema file (YAML or JSON), or dict (parsed config).
    - format_hint: SchemaSource or its value (e.g. "sdv"); None to use schema_source tag or auto-detect.
    - table_name: for single-table YData file, override table name (else from filename or "table").

    Schema files may tag the source with a top-level key:
      schema_source: sdv   # or ydata, pipeline, sqlserver, postgres, mysql, oracle
    """
    path = None
    data: dict[str, Any] | None = None
    if isinstance(source, dict):
        data = source
    elif isinstance(source, (str, Path)):
        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(f"Schema file not found: {path}")
        if path.suffix.lower() == ".sql":
            dialect = None
            if format_hint is not None:
                hint = format_hint.value if isinstance(format_hint, SchemaSource) else str(format_hint).strip().lower()
                dialect = hint if hint in ("mysql", "postgres", "sqlserver") else None
            return parse_ddl_file(path, dialect=dialect)
        data, stem_name = _load_data_from_path(path)
        if table_name is None and stem_name:
            table_name = stem_name
    else:
        raise TypeError("source must be path (str | Path) or dict")

    # Normalize format_hint to SchemaSource
    if isinstance(format_hint, str):
        raw = format_hint.strip().lower()
        if raw:
            try:
                format_hint = SchemaSource(raw)
            except ValueError:
                valid = sorted(s.value for s in SchemaSource)
                raise ValueError(f"Unknown schema source: {format_hint!r}. Valid: {valid}")
        else:
            format_hint = None
    tagged = get_schema_source_from_data(data or {})
    fmt = format_hint or tagged or detect_format(data or {})

    if _is_sql_dialect(fmt):
        ddl_text = (data or {}).get("ddl") or (data or {}).get("sql")
        if isinstance(ddl_text, str):
            return parse_ddl(ddl_text, dialect=fmt.value)
        if path is not None and Path(path).suffix.lower() == ".sql":
            return parse_ddl_file(path, dialect=fmt.value)
        raise ValueError(
            f"Schema source {fmt.value!r} expects a .sql file path or a dict with 'ddl' or 'sql' key."
        )

    if fmt == SchemaSource.SDV:
        return parse_sdv_metadata(data or {})
    if fmt == SchemaSource.YDATA:
        if _is_multi_table_ydata(data or {}):
            return parse_ydata_multi_yaml(data or {})
        return [parse_ydata_yaml(data or {}, table_name=table_name or "table")]
    if fmt == SchemaSource.PIPELINE:
        return parse_pipeline_config(data or {})
    if fmt == SchemaSource.ORACLE:
        raise ValueError("Schema source not yet implemented: oracle")
    raise ValueError(f"Unknown or unsupported schema source: {fmt!r}")


def _is_multi_table_ydata(data: dict[str, Any]) -> bool:
    """True if top-level keys are table names and values are table specs (nested field dicts)."""
    reserved = {"__table_description__", "__foreign_keys__"}
    table_like_keys = 0
    for k, v in data.items():
        if k in reserved:
            continue
        if not isinstance(v, dict):
            continue
        if "__table_description__" in v or any(
            isinstance(x, dict) and ("type" in x or "constraints" in x) for x in (v.values() or [])
        ):
            table_like_keys += 1
    return table_like_keys >= 2
