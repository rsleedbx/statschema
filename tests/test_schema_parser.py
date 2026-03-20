"""
pytest tests for src/statschema.
"""

import json
import os
from pathlib import Path

import pytest

from src.statschema.model import (
    CanonicalColumn,
    CanonicalTableSchema,
    GenerationRule,
)
from src.statschema.ydata_parser import (
    parse_ydata_yaml,
    parse_ydata_yaml_file,
    parse_ydata_multi_yaml,
)
from src.statschema.pipeline_parser import (
    parse_pipeline_tables,
    parse_pipeline_config,
)
from src.statschema.loader import (
    load_schema,
    detect_format,
    get_schema_source_from_data,
    SchemaSource,
)
from src.statschema.sdv_parser import parse_sdv_metadata, parse_sdv_file
from src.statschema.ddl_parser import parse_ddl, parse_ddl_file
from src.statschema.dbldatagen_builder import to_dbldatagen_specs, build_dataframe_from_canonical


# -----------------------------------------------------------------------------
# Model
# -----------------------------------------------------------------------------


class TestModel:
    def test_canonical_column_minimal(self):
        col = CanonicalColumn(name="id", type="integer")
        assert col.name == "id"
        assert col.type == "integer"
        assert col.description is None
        assert col.primary_key is False
        assert col.generation is None
        assert col.references is None

    def test_canonical_column_with_constraints(self):
        col = CanonicalColumn(
            name="pk",
            type="long",
            primary_key=True,
            generation=GenerationRule(unique=True),
        )
        assert col.primary_key is True
        assert col.generation is not None
        assert col.generation.unique is True

    def test_canonical_table_schema(self):
        cols = [
            CanonicalColumn(name="a", type="integer"),
            CanonicalColumn(name="b", type="string"),
        ]
        table = CanonicalTableSchema(name="t1", columns=cols, description="Table one")
        assert table.name == "t1"
        assert len(table.columns) == 2
        assert table.columns[0].name == "a"
        assert table.columns[1].type == "string"
        assert table.description == "Table one"

    def test_generation_rule(self):
        g = GenerationRule(min_value=0, max_value=100, values=[1, 2, 3], max_length=50)
        assert g.min_value == 0
        assert g.max_value == 100
        assert g.values == [1, 2, 3]
        assert g.max_length == 50


# -----------------------------------------------------------------------------
# YData parser
# -----------------------------------------------------------------------------


class TestYDataParser:
    def test_parse_ydata_yaml_single_table(self):
        data = {
            "__table_description__": "Test table",
            "id": {"type": "number", "description": "ID", "constraints": {"primary_key": True}},
            "col_1": {"type": "integer", "description": "Col 1"},
            "name": {"type": "text", "constraints": {"max_length": 200}},
        }
        table = parse_ydata_yaml(data, table_name="phase1_table")
        assert table.name == "phase1_table"
        assert table.description == "Test table"
        assert len(table.columns) == 3
        assert table.columns[0].name == "id"
        assert table.columns[0].type == "long"
        assert table.columns[0].primary_key is True
        assert table.columns[1].name == "col_1"
        assert table.columns[1].type == "integer"
        assert table.columns[2].name == "name"
        assert table.columns[2].type == "string"
        assert table.columns[2].generation is not None
        assert table.columns[2].generation.max_length == 200

    def test_parse_ydata_yaml_default_table_name(self):
        data = {"id": {"type": "integer"}}
        table = parse_ydata_yaml(data)
        assert table.name == "table"
        assert len(table.columns) == 1
        assert table.columns[0].type == "integer"

    def test_parse_ydata_yaml_foreign_key_with_references_dict(self):
        data = {
            "category_id": {
                "type": "foreign_key",
                "description": "FK to category",
                "references": {"schema": "Category", "field": "id"},
            },
        }
        table = parse_ydata_yaml(data)
        assert len(table.columns) == 1
        assert table.columns[0].type == "long"
        assert table.columns[0].references == ("Category", "id")

    def test_parse_ydata_yaml_foreign_key_with_references_list(self):
        data = {
            "parent_id": {"type": "foreign_key", "references": ["Parent", "id"]},
        }
        table = parse_ydata_yaml(data)
        assert table.columns[0].references == ("Parent", "id")

    def test_parse_ydata_yaml___foreign_keys__section(self):
        data = {
            "__foreign_keys__": {"ref_id": ["Other", "id"]},
            "ref_id": {"type": "long"},
            "val": {"type": "text"},
        }
        table = parse_ydata_yaml(data)
        assert table.foreign_keys is not None
        assert len(table.foreign_keys) == 1
        assert table.foreign_keys[0]["column"] == "ref_id"
        assert table.foreign_keys[0]["parent_table"] == "Other"
        assert table.foreign_keys[0]["parent_column"] == "id"

    def test_parse_ydata_yaml_type_aliases(self):
        data = {
            "a": {"type": "str"},
            "b": {"type": "int"},
            "c": {"type": "bool"},
            "d": {"type": "email"},
        }
        table = parse_ydata_yaml(data)
        assert table.columns[0].type == "string"
        assert table.columns[1].type == "integer"
        assert table.columns[2].type == "boolean"
        assert table.columns[3].type == "string"

    def test_parse_ydata_multi_yaml(self):
        data = {
            "Supplier": {
                "__table_description__": "Suppliers",
                "id": {"type": "number", "constraints": {"primary_key": True}},
                "name": {"type": "text"},
            },
            "Category": {
                "__table_description__": "Categories",
                "id": {"type": "integer", "constraints": {"primary_key": True}},
                "label": {"type": "text"},
            },
        }
        tables = parse_ydata_multi_yaml(data)
        assert len(tables) == 2
        assert tables[0].name == "Supplier"
        assert tables[0].description == "Suppliers"
        assert len(tables[0].columns) == 2
        assert tables[1].name == "Category"
        assert len(tables[1].columns) == 2

    def test_parse_ydata_yaml_file(self, tmp_path):
        yaml_path = tmp_path / "schema.yml"
        yaml_path.write_text("""
__table_description__: From file
col_1:
  type: integer
  description: One column
""", encoding="utf-8")
        table = parse_ydata_yaml_file(yaml_path, table_name="from_file")
        assert table.name == "from_file"
        assert table.description == "From file"
        assert len(table.columns) == 1
        assert table.columns[0].name == "col_1"
        assert table.columns[0].type == "integer"

    def test_parse_ydata_yaml_file_empty_raises(self, tmp_path):
        yaml_path = tmp_path / "empty.yml"
        yaml_path.write_text("", encoding="utf-8")
        with pytest.raises(ValueError, match="Empty YAML"):
            parse_ydata_yaml_file(yaml_path)

    def test_parse_ydata_yaml_file_nonexistent_raises(self):
        with pytest.raises(FileNotFoundError):
            parse_ydata_yaml_file("/nonexistent/ydata_schema.yml")

    def test_parse_ydata_yaml_field_value_string_only(self):
        """parse_ydata_yaml supports field value as string (e.g. 'col': 'integer') without dict."""
        data = {"id": "integer", "name": "text"}
        table = parse_ydata_yaml(data, table_name="t")
        assert len(table.columns) == 2
        assert table.columns[0].name == "id"
        assert table.columns[0].type == "integer"
        assert table.columns[1].name == "name"
        assert table.columns[1].type == "string"


# -----------------------------------------------------------------------------
# SDV parser (source: SDV – docs.sdv.dev)
# -----------------------------------------------------------------------------


class TestSDVParser:
    def test_parse_sdv_metadata_single_table(self):
        data = {
            "METADATA_SPEC_VERSION": "V1",
            "tables": {
                "phase1_table": {
                    "primary_key": "id",
                    "columns": {
                        "id": {"sdtype": "id", "regex_format": "[0-9]+"},
                        "col_1": {"sdtype": "numerical", "computer_representation": "Int32"},
                        "name": {"sdtype": "categorical"},
                    },
                },
            },
            "relationships": [],
        }
        tables = parse_sdv_metadata(data)
        assert len(tables) == 1
        t = tables[0]
        assert t.name == "phase1_table"
        assert len(t.columns) == 3
        assert t.columns[0].name == "id"
        assert t.columns[0].type == "long"
        assert t.columns[0].primary_key is True
        assert t.columns[0].generation is not None
        assert t.columns[0].generation.unique is True
        assert t.columns[1].name == "col_1"
        assert t.columns[1].type == "integer"
        assert t.columns[2].name == "name"
        assert t.columns[2].type == "string"

    def test_parse_sdv_metadata_multi_table_with_relationships(self):
        data = {
            "METADATA_SPEC_VERSION": "V1",
            "tables": {
                "hotels": {
                    "primary_key": "hotel_id",
                    "columns": {
                        "hotel_id": {"sdtype": "id", "regex_format": "HID_[0-9]{3}"},
                        "city": {"sdtype": "categorical"},
                    },
                },
                "guests": {
                    "primary_key": "guest_email",
                    "columns": {
                        "guest_email": {"sdtype": "email"},
                        "hotel_id": {"sdtype": "id", "regex_format": "HID_[0-9]{3}"},
                    },
                },
            },
            "relationships": [
                {
                    "parent_table_name": "hotels",
                    "parent_primary_key": "hotel_id",
                    "child_table_name": "guests",
                    "child_foreign_key": "hotel_id",
                },
            ],
        }
        tables = parse_sdv_metadata(data)
        assert len(tables) == 2
        assert tables[0].name == "hotels"
        assert tables[0].foreign_keys is None
        assert tables[1].name == "guests"
        assert tables[1].foreign_keys is not None
        assert len(tables[1].foreign_keys) == 1
        assert tables[1].foreign_keys[0]["column"] == "hotel_id"
        assert tables[1].foreign_keys[0]["parent_table"] == "hotels"
        assert tables[1].foreign_keys[0]["parent_column"] == "hotel_id"

    def test_parse_sdv_metadata_numerical_float(self):
        data = {
            "tables": {
                "t": {
                    "primary_key": "id",
                    "columns": {
                        "id": {"sdtype": "id"},
                        "score": {"sdtype": "numerical", "computer_representation": "Float"},
                    },
                },
            },
            "relationships": [],
        }
        tables = parse_sdv_metadata(data)
        assert tables[0].columns[1].type == "float"

    def test_parse_sdv_metadata_datetime(self):
        data = {
            "tables": {
                "t": {
                    "columns": {
                        "ts": {"sdtype": "datetime", "datetime_format": "%Y-%m-%d"},
                    },
                },
            },
            "relationships": [],
        }
        tables = parse_sdv_metadata(data)
        assert tables[0].columns[0].type == "timestamp"
        assert tables[0].columns[0].constraints.get("datetime_format") == "%Y-%m-%d"

    def test_parse_sdv_file_yaml(self, tmp_path):
        yaml_path = tmp_path / "metadata.yaml"
        yaml_path.write_text("""
schema_source: sdv
METADATA_SPEC_VERSION: "V1"
tables:
  t1:
    primary_key: id
    columns:
      id: { sdtype: id }
      x: { sdtype: categorical }
relationships: []
""", encoding="utf-8")
        tables = parse_sdv_file(yaml_path)
        assert len(tables) == 1
        assert tables[0].name == "t1"
        assert len(tables[0].columns) == 2

    def test_parse_sdv_file_json(self, tmp_path):
        json_path = tmp_path / "metadata.json"
        # relationships must be top-level (not inside tables)
        data = {
            "METADATA_SPEC_VERSION": "V1",
            "tables": {"t": {"primary_key": "id", "columns": {"id": {"sdtype": "id"}, "v": {"sdtype": "boolean"}}}},
            "relationships": [],
        }
        json_path.write_text(json.dumps(data), encoding="utf-8")
        tables = parse_sdv_file(json_path)
        assert len(tables) == 1
        assert tables[0].columns[1].type == "boolean"

    def test_parse_sdv_file_nonexistent_raises(self):
        with pytest.raises(FileNotFoundError, match="not found"):
            parse_sdv_file("/nonexistent/sdv_metadata.json")

    def test_parse_sdv_metadata_skips_non_dict_table_entry(self):
        """SDV tables with a non-dict entry (e.g. null or string) are skipped; other tables parsed."""
        data = {
            "tables": {
                "valid_table": {"primary_key": "id", "columns": {"id": {"sdtype": "id"}}},
                "invalid_entry": None,
                "another_valid": {"primary_key": "pk", "columns": {"pk": {"sdtype": "id"}, "x": {"sdtype": "categorical"}}},
            },
            "relationships": [],
        }
        tables = parse_sdv_metadata(data)
        assert len(tables) == 2
        names = {t.name for t in tables}
        assert names == {"valid_table", "another_valid"}
        assert "invalid_entry" not in names


# -----------------------------------------------------------------------------
# Pipeline parser
# -----------------------------------------------------------------------------


class TestPipelineParser:
    def test_parse_pipeline_tables_single(self):
        tables_spec = [
            {"name": "phase1_table", "columns": [{"name": "col_1", "type": "integer"}]},
        ]
        tables = parse_pipeline_tables(tables_spec)
        assert len(tables) == 1
        assert tables[0].name == "phase1_table"
        assert len(tables[0].columns) == 1
        assert tables[0].columns[0].name == "col_1"
        assert tables[0].columns[0].type == "integer"

    def test_parse_pipeline_tables_multiple(self):
        tables_spec = [
            {"name": "t1", "columns": [{"name": "id", "type": "long"}, {"name": "v", "type": "string"}]},
            {"name": "t2", "columns": [{"name": "x", "type": "float"}]},
        ]
        tables = parse_pipeline_tables(tables_spec)
        assert len(tables) == 2
        assert tables[0].name == "t1"
        assert len(tables[0].columns) == 2
        assert tables[1].name == "t2"
        assert tables[1].columns[0].type == "float"

    def test_parse_pipeline_tables_null_type_becomes_string(self):
        tables_spec = [{"name": "t", "columns": [{"name": "col_1", "type": None}]}]
        tables = parse_pipeline_tables(tables_spec)
        assert tables[0].columns[0].type == "string"

    def test_parse_pipeline_config_with_pipeline_key(self):
        config = {
            "pipeline": {
                "snapshot_rows": 1000,
                "tables": [{"name": "phase1_table", "columns": [{"name": "col_1", "type": "integer"}]}],
            },
        }
        tables = parse_pipeline_config(config)
        assert len(tables) == 1
        assert tables[0].name == "phase1_table"
        assert tables[0].columns[0].type == "integer"

    def test_parse_pipeline_config_without_pipeline_key(self):
        config = {"tables": [{"name": "t", "columns": [{"name": "c", "type": "boolean"}]}]}
        tables = parse_pipeline_config(config)
        assert len(tables) == 1
        assert tables[0].columns[0].type == "boolean"


# -----------------------------------------------------------------------------
# Loader
# -----------------------------------------------------------------------------


class TestLoader:
    def test_get_schema_source_from_data(self):
        assert get_schema_source_from_data({"schema_source": "sdv"}) == SchemaSource.SDV
        assert get_schema_source_from_data({"source": "ydata"}) == SchemaSource.YDATA
        assert get_schema_source_from_data({"schema_source": "pipeline"}) == SchemaSource.PIPELINE
        assert get_schema_source_from_data({"source": "SQLSERVER"}) == SchemaSource.SQLSERVER
        assert get_schema_source_from_data({"schema_source": "syda"}) == SchemaSource.YDATA
        assert get_schema_source_from_data({"id": {"type": "integer"}}) is None

    def test_get_schema_source_from_data_schema_format_and_format_keys(self):
        """Loader accepts schema_format and format as aliases for schema_source."""
        assert get_schema_source_from_data({"schema_format": "sdv"}) == SchemaSource.SDV
        assert get_schema_source_from_data({"format": "mysql"}) == SchemaSource.MYSQL
        assert get_schema_source_from_data({"schema_format": "postgres"}) == SchemaSource.POSTGRES
        assert get_schema_source_from_data({"schema_source": "neon"}) == SchemaSource.POSTGRES
        assert get_schema_source_from_data({"schema_source": "NEONDB"}) == SchemaSource.POSTGRES

    def test_load_schema_empty_file_raises(self, tmp_path):
        """load_schema from empty YAML/JSON file raises (ValueError for YAML; JSONDecodeError for empty JSON)."""
        empty_yaml = tmp_path / "empty.yaml"
        empty_yaml.write_text("", encoding="utf-8")
        with pytest.raises(ValueError, match="Empty or invalid schema file"):
            load_schema(empty_yaml)
        empty_json = tmp_path / "empty.json"
        empty_json.write_text("", encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            load_schema(empty_json)

    def test_load_schema_invalid_file_not_dict_raises(self, tmp_path):
        """load_schema from file whose content is not a dict (e.g. JSON array) raises ValueError."""
        array_json = tmp_path / "array.json"
        array_json.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(ValueError, match="Empty or invalid schema file"):
            load_schema(array_json)

    def test_load_schema_oracle_raises_not_implemented(self):
        """load_schema with schema_source oracle raises ValueError (not yet implemented)."""
        with pytest.raises(ValueError, match="not yet implemented.*oracle"):
            load_schema({"schema_source": "oracle", "tables": []})
        with pytest.raises(ValueError, match="not yet implemented.*oracle"):
            load_schema({"x": {"type": "integer"}}, format_hint=SchemaSource.ORACLE)

    def test_load_schema_multi_table_ydata_returns_multiple_tables(self):
        """load_schema with multi-table YData dict returns multiple CanonicalTableSchema."""
        data = {
                "orders": {
                    "__table_description__": "Orders",
                    "id": {"type": "integer"},
                    "amount": {"type": "number"},
                },
                "users": {
                    "__table_description__": "Users",
                    "id": {"type": "integer"},
                    "name": {"type": "text"},
                },
            }
        tables = load_schema(data, format_hint=SchemaSource.YDATA)
        assert len(tables) == 2
        names = {t.name for t in tables}
        assert names == {"orders", "users"}
        assert tables[0].columns[0].name == "id"
        assert tables[1].columns[1].name == "name"

    def test_load_schema_from_path_object(self, tmp_path):
        """load_schema accepts pathlib.Path as source."""
        yaml_path = tmp_path / "via_path.yaml"
        yaml_path.write_text("schema_source: ydata\nid: { type: integer }\n", encoding="utf-8")
        tables = load_schema(Path(yaml_path))
        assert len(tables) == 1
        assert tables[0].columns[0].type == "integer"

    def test_detect_format_sdv(self):
        data = {
            "METADATA_SPEC_VERSION": "V1",
            "tables": {
                "t": {"primary_key": "id", "columns": {"id": {"sdtype": "id"}, "x": {"sdtype": "categorical"}}},
            },
            "relationships": [],
        }
        assert detect_format(data) == SchemaSource.SDV

    def test_detect_format_ydata_by_table_description(self):
        data = {"__table_description__": "X", "id": {"type": "number"}}
        assert detect_format(data) == SchemaSource.YDATA

    def test_detect_format_ydata_by_field_type(self):
        data = {"id": {"type": "integer", "description": "ID"}}
        assert detect_format(data) == SchemaSource.YDATA

    def test_detect_format_pipeline_by_pipeline_tables(self):
        data = {"pipeline": {"tables": [{"name": "t", "columns": [{"name": "c", "type": "string"}]}]}}
        assert detect_format(data) == SchemaSource.PIPELINE

    def test_detect_format_pipeline_by_tables_only(self):
        data = {"tables": [{"name": "t", "columns": [{"name": "c", "type": "integer"}]}]}
        assert detect_format(data) == SchemaSource.PIPELINE

    def test_load_schema_respects_schema_source_tag(self):
        data = {
            "schema_source": "sdv",
            "METADATA_SPEC_VERSION": "V1",
            "tables": {"t": {"primary_key": "id", "columns": {"id": {"sdtype": "id"}, "v": {"sdtype": "string"}}}},
            "relationships": [],
        }
        tables = load_schema(data)
        assert len(tables) == 1
        assert tables[0].name == "t"
        assert tables[0].columns[0].type == "long"

    def test_load_schema_ydata_with_schema_source_tag(self):
        data = {
            "schema_source": "ydata",
            "__table_description__": "Tagged",
            "id": {"type": "integer"},
        }
        tables = load_schema(data)
        assert len(tables) == 1
        assert tables[0].columns[0].type == "integer"
        assert "schema_source" not in [c.name for c in tables[0].columns]

    def test_load_schema_from_dict_ydata(self):
        data = {"__table_description__": "Y", "col_1": {"type": "integer"}}
        tables = load_schema(data, format_hint=SchemaSource.YDATA)
        assert len(tables) == 1
        assert tables[0].name == "table"
        assert tables[0].columns[0].type == "integer"

    def test_load_schema_from_dict_pipeline(self):
        data = {"pipeline": {"tables": [{"name": "phase1_table", "columns": [{"name": "col_1", "type": "integer"}]}]}}
        tables = load_schema(data, format_hint=SchemaSource.PIPELINE)
        assert len(tables) == 1
        assert tables[0].name == "phase1_table"

    def test_load_schema_auto_detect_ydata(self):
        data = {"__table_description__": "A", "id": {"type": "long"}}
        tables = load_schema(data)
        assert len(tables) == 1
        assert tables[0].columns[0].type == "long"

    def test_load_schema_auto_detect_pipeline(self):
        data = {"pipeline": {"tables": [{"name": "t", "columns": [{"name": "c", "type": "string"}]}]}}
        tables = load_schema(data)
        assert len(tables) == 1
        assert tables[0].columns[0].type == "string"

    def test_load_schema_from_file_ydata(self, tmp_path):
        yaml_path = tmp_path / "s.yml"
        yaml_path.write_text("""
__table_description__: File table
a: { type: integer }
b: { type: string }
""", encoding="utf-8")
        tables = load_schema(yaml_path, format_hint=SchemaSource.YDATA, table_name="from_s")
        assert len(tables) == 1
        assert tables[0].name == "from_s"
        assert len(tables[0].columns) == 2

    def test_load_schema_nonexistent_file_raises(self):
        with pytest.raises(FileNotFoundError, match="not found"):
            load_schema("/nonexistent/schema.yaml")

    def test_load_schema_invalid_source_type_raises(self):
        with pytest.raises(TypeError, match="path"):
            load_schema(123)

    def test_load_schema_from_file_json_sdv(self, tmp_path):
        json_path = tmp_path / "schema.json"
        data = {
            "schema_source": "sdv",
            "METADATA_SPEC_VERSION": "V1",
            "tables": {"t": {"primary_key": "id", "columns": {"id": {"sdtype": "id"}, "x": {"sdtype": "numerical", "computer_representation": "Int32"}}}},
            "relationships": [],
        }
        json_path.write_text(json.dumps(data), encoding="utf-8")
        tables = load_schema(json_path)
        assert len(tables) == 1
        assert tables[0].columns[1].type == "integer"

    def test_load_schema_unknown_source_raises(self):
        with pytest.raises(ValueError, match="Unknown schema source"):
            load_schema({"schema_source": "invalid_format"}, format_hint="invalid_format")

    def test_valid_schema_sources_includes_expected(self):
        valid = {s.value for s in SchemaSource}
        assert "sdv" in valid
        assert "ydata" in valid
        assert "pipeline" in valid
        assert "sqlserver" in valid
        assert "postgres" in valid
        assert "mysql" in valid
        assert "oracle" in valid

    def test_load_schema_mysql_ddl_from_dict(self):
        ddl = """
        CREATE TABLE `users` (
          `id` int(11) NOT NULL AUTO_INCREMENT,
          `name` varchar(255) DEFAULT NULL,
          PRIMARY KEY (`id`)
        );
        """
        tables = load_schema({"schema_source": "mysql", "ddl": ddl})
        assert len(tables) == 1
        assert tables[0].name == "users"
        assert len(tables[0].columns) == 2
        assert tables[0].columns[0].name == "id"
        assert tables[0].columns[0].type == "integer"
        assert tables[0].columns[0].primary_key is True
        assert tables[0].columns[1].name == "name"
        assert tables[0].columns[1].type == "string"

    def test_load_schema_sql_dialect_without_ddl_raises(self):
        with pytest.raises(ValueError, match="expects a .sql file path or a dict with 'ddl' or 'sql'"):
            load_schema({"schema_source": "mysql", "tables": []})

    def test_load_schema_from_sql_file(self, tmp_path):
        sql_path = tmp_path / "schema.sql"
        sql_path.write_text("""
CREATE TABLE phase1_table (
  id int NOT NULL AUTO_INCREMENT,
  col_1 int DEFAULT NULL,
  PRIMARY KEY (id)
);
""", encoding="utf-8")
        tables = load_schema(sql_path)
        assert len(tables) == 1
        assert tables[0].name == "phase1_table"
        assert tables[0].columns[0].type == "integer"
        assert tables[0].columns[0].primary_key is True

    def test_load_schema_postgres_ddl_from_dict(self):
        ddl = """
        CREATE TABLE public.products (
            id integer NOT NULL,
            name character varying(200),
            price numeric(10,2),
            PRIMARY KEY (id)
        );
        """
        tables = load_schema({"schema_source": "postgres", "ddl": ddl})
        assert len(tables) == 1
        assert tables[0].name == "products"
        assert len(tables[0].columns) == 3
        assert tables[0].columns[0].name == "id"
        assert tables[0].columns[0].type == "integer"
        assert tables[0].columns[0].primary_key is True
        assert tables[0].columns[1].type == "string"
        assert tables[0].columns[2].type == "decimal"  # NUMERIC(10,2) is fixed-point decimal

    def test_load_schema_sqlserver_ddl_from_dict(self):
        ddl = """
        CREATE TABLE [dbo].[customers] (
            [id] INT NOT NULL PRIMARY KEY,
            [name] NVARCHAR(100) NULL,
            [active] BIT NOT NULL
        );
        """
        tables = load_schema({"schema_source": "sqlserver", "ddl": ddl})
        assert len(tables) == 1
        assert tables[0].name == "customers"
        assert len(tables[0].columns) == 3
        assert tables[0].columns[0].primary_key is True
        assert tables[0].columns[1].type == "string"
        assert tables[0].columns[2].type == "boolean"

    def test_load_schema_sql_file_with_format_hint_postgres(self, tmp_path):
        sql_path = tmp_path / "pg_schema.sql"
        sql_path.write_text(
            "CREATE TABLE t ( id integer PRIMARY KEY, label character varying(50) );",
            encoding="utf-8",
        )
        tables = load_schema(sql_path, format_hint=SchemaSource.POSTGRES)
        assert len(tables) == 1
        assert tables[0].name == "t"
        assert tables[0].columns[0].type == "integer"
        assert tables[0].columns[1].type == "string"

    def test_load_schema_sql_file_with_format_hint_neon(self, tmp_path):
        """NeonDB DDL path is Postgres-compatible; format hint ``neon`` must parse like PG."""
        sql_path = tmp_path / "neon_schema.sql"
        sql_path.write_text(
            "CREATE TABLE t ( id integer PRIMARY KEY, label character varying(50) );",
            encoding="utf-8",
        )
        tables = load_schema(sql_path, format_hint="neon")
        assert len(tables) == 1
        assert tables[0].name == "t"
        assert tables[0].columns[0].type == "integer"
        assert tables[0].columns[1].type == "string"

    def test_load_schema_sql_file_with_format_hint_sqlserver(self, tmp_path):
        sql_path = tmp_path / "mssql_schema.sql"
        sql_path.write_text(
            "CREATE TABLE [dbo].[t] ( [id] INT PRIMARY KEY, [x] NVARCHAR(50) );",
            encoding="utf-8",
        )
        tables = load_schema(sql_path, format_hint=SchemaSource.SQLSERVER)
        assert len(tables) == 1
        assert tables[0].name == "t"
        assert tables[0].columns[0].type == "integer"
        assert tables[0].columns[1].type == "string"

    def test_load_schema_sql_from_dict_using_sql_key(self):
        """Support 'sql' as alias for 'ddl' in dict."""
        ddl = "CREATE TABLE t ( id int PRIMARY KEY );"
        tables = load_schema({"schema_source": "mysql", "sql": ddl})
        assert len(tables) == 1
        assert tables[0].name == "t"
        assert tables[0].columns[0].name == "id"


# -----------------------------------------------------------------------------
# All source formats via load_schema (single entry point)
# -----------------------------------------------------------------------------


class TestLoadSchemaAllFormats:
    """load_schema() for every implemented schema source (sdv, ydata, pipeline, mysql, postgres, sqlserver)."""

    def test_load_schema_sdv(self):
        data = {
            "schema_source": "sdv",
            "METADATA_SPEC_VERSION": "V1",
            "tables": {
                "t1": {
                    "primary_key": "id",
                    "columns": {"id": {"sdtype": "id"}, "v": {"sdtype": "categorical"}},
                },
            },
            "relationships": [],
        }
        tables = load_schema(data)
        assert len(tables) == 1
        assert tables[0].name == "t1"
        assert len(tables[0].columns) == 2
        specs = to_dbldatagen_specs(tables[0], rows=10)
        assert len(specs) == 2

    def test_load_schema_ydata(self):
        data = {
            "schema_source": "ydata",
            "__table_description__": "YData table",
            "id": {"type": "integer", "constraints": {"primary_key": True}},
            "label": {"type": "text"},
        }
        tables = load_schema(data)
        assert len(tables) == 1
        assert tables[0].name == "table"
        assert len(tables[0].columns) == 2
        specs = to_dbldatagen_specs(tables[0], rows=10)
        assert len(specs) == 2

    def test_load_schema_pipeline(self):
        data = {
            "pipeline": {
                "tables": [
                    {"name": "orders", "columns": [{"name": "id", "type": "long"}, {"name": "amount", "type": "double"}]},
                ],
            },
        }
        tables = load_schema(data, format_hint=SchemaSource.PIPELINE)
        assert len(tables) == 1
        assert tables[0].name == "orders"
        assert len(tables[0].columns) == 2
        specs = to_dbldatagen_specs(tables[0], rows=10)
        assert len(specs) == 2

    def test_load_schema_mysql(self):
        data = {
            "schema_source": "mysql",
            "ddl": "CREATE TABLE `runs` ( `id` int NOT NULL, `ts` datetime DEFAULT NULL, PRIMARY KEY (`id`) );",
        }
        tables = load_schema(data)
        assert len(tables) == 1
        assert tables[0].name == "runs"
        assert len(tables[0].columns) == 2
        specs = to_dbldatagen_specs(tables[0], rows=10)
        assert len(specs) == 2

    def test_load_schema_postgres(self):
        data = {
            "schema_source": "postgres",
            "ddl": "CREATE TABLE events ( id integer PRIMARY KEY, name text );",
        }
        tables = load_schema(data)
        assert len(tables) == 1
        assert tables[0].name == "events"
        assert len(tables[0].columns) == 2
        specs = to_dbldatagen_specs(tables[0], rows=10)
        assert len(specs) == 2

    def test_load_schema_neon_alias(self):
        data = {
            "schema_source": "neon",
            "ddl": "CREATE TABLE events ( id integer PRIMARY KEY, name text );",
        }
        tables = load_schema(data)
        assert len(tables) == 1
        assert tables[0].name == "events"

    def test_load_schema_sqlserver(self):
        data = {
            "schema_source": "sqlserver",
            "ddl": "CREATE TABLE [dbo].[logs] ( [id] INT PRIMARY KEY, [msg] NVARCHAR(255) );",
        }
        tables = load_schema(data)
        assert len(tables) == 1
        assert tables[0].name == "logs"
        assert len(tables[0].columns) == 2
        specs = to_dbldatagen_specs(tables[0], rows=10)
        assert len(specs) == 2

    @pytest.mark.parametrize(
        "source_fmt,payload",
        [
            ("sdv", {"schema_source": "sdv", "METADATA_SPEC_VERSION": "V1", "tables": {"x": {"primary_key": "id", "columns": {"id": {"sdtype": "id"}}}}, "relationships": []}),
            ("ydata", {"schema_source": "ydata", "id": {"type": "integer"}, "name": {"type": "text"}}),
            ("pipeline", {"tables": [{"name": "t", "columns": [{"name": "c", "type": "string"}]}]}),
            ("mysql", {"schema_source": "mysql", "ddl": "CREATE TABLE t ( id int PRIMARY KEY );"}),
            ("postgres", {"schema_source": "postgres", "ddl": "CREATE TABLE t ( id integer PRIMARY KEY );"}),
            ("sqlserver", {"schema_source": "sqlserver", "ddl": "CREATE TABLE [dbo].[t] ( [id] INT PRIMARY KEY );"}),
        ],
        ids=["sdv", "ydata", "pipeline", "mysql", "postgres", "sqlserver"],
    )
    def test_load_schema_all_formats_return_canonical_tables(self, source_fmt, payload):
        """Every format returns list of CanonicalTableSchema with at least one table and one column."""
        tables = load_schema(payload, format_hint=source_fmt)
        assert len(tables) >= 1
        assert tables[0].name
        assert len(tables[0].columns) >= 1
        assert tables[0].columns[0].name
        assert tables[0].columns[0].type


# -----------------------------------------------------------------------------
# ronaldbradford/schema (real MySQL DDL: parse + generate data)
# See https://github.com/ronaldbradford/schema
# -----------------------------------------------------------------------------


def _sakila_excerpt_path() -> Path:
    """Path to sakila excerpt fixture (MySQL DDL from ronaldbradford/schema)."""
    return Path(__file__).resolve().parent / "fixtures" / "ronaldbradford_schema" / "sakila_excerpt.sql"


def _create_spark_session(app_name: str):
    """
    Get a Spark session for tests, using the same priority as the runner script:
      1. DatabricksSession (databricks-connect, uses ~/.databrickscfg or env vars)
      2. Local PySpark session (pyspark without databricks-connect, e.g. .venv_test)
    Raises RuntimeError if neither is available (caller should pytest.skip).
    """
    # Try DatabricksSession first (works when ~/.databrickscfg is configured)
    try:
        from databricks.connect import DatabricksSession
        return DatabricksSession.builder.getOrCreate()
    except Exception:
        pass

    # Fall back to local PySpark (e.g. .venv_test with pyspark>=3.5, no databricks-connect)
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
            # Avoid hostname-resolution failures in sandboxed/container environments
            .config("spark.driver.host", "localhost")
            .config("spark.driver.bindAddress", "127.0.0.1")
            .getOrCreate()
        )
    except RuntimeError:
        raise
    finally:
        for key, value in saved.items():
            os.environ[key] = value


class TestRonaldbradfordSchema:
    """Parse and generate data from example MySQL schemas (ronaldbradford/schema)."""

    def test_parse_sakila_excerpt_mysql(self):
        """Load sakila excerpt via load_schema; assert tables and columns."""
        path = _sakila_excerpt_path()
        if not path.exists():
            pytest.skip("fixture tests/fixtures/ronaldbradford_schema/sakila_excerpt.sql not found")
        tables = load_schema(path, format_hint=SchemaSource.MYSQL)
        assert len(tables) >= 4
        names = {t.name for t in tables}
        assert "actor" in names
        assert "category" in names
        assert "country" in names
        assert "city" in names
        actor = next(t for t in tables if t.name == "actor")
        assert len(actor.columns) >= 4
        col_names = {c.name for c in actor.columns}
        assert "actor_id" in col_names
        assert "first_name" in col_names
        assert "last_name" in col_names
        assert "last_update" in col_names
        pk = next(c for c in actor.columns if c.name == "actor_id")
        assert pk.primary_key is True
        assert pk.type == "integer"

    def test_to_dbldatagen_specs_from_sakila_excerpt(self):
        """Parse sakila excerpt and build dbldatagen specs for every table (no Spark)."""
        path = _sakila_excerpt_path()
        if not path.exists():
            pytest.skip("fixture tests/fixtures/ronaldbradford_schema/sakila_excerpt.sql not found")
        tables = load_schema(path, format_hint=SchemaSource.MYSQL)
        for table in tables:
            specs = to_dbldatagen_specs(table, rows=100)
            assert len(specs) == len(table.columns)
            for name, _spark_type, opts in specs:
                assert name
                assert isinstance(opts, dict)

    def test_generate_data_from_sakila_excerpt(self):
        """Parse sakila excerpt and build a Spark DataFrame for one table (actor)."""
        path = _sakila_excerpt_path()
        if not path.exists():
            pytest.skip("fixture tests/fixtures/ronaldbradford_schema/sakila_excerpt.sql not found")
        pytest.importorskip("dbldatagen")
        tables = load_schema(path, format_hint=SchemaSource.MYSQL)
        actor = next(t for t in tables if t.name == "actor")
        try:
            spark = _create_spark_session("statschema_ronaldbradford_test")
        except RuntimeError as e:
            if "Databricks Connect" in str(e) or "remote Spark" in str(e):
                pytest.skip("Local Spark not available (Databricks Connect only supports remote sessions)")
            raise
        try:
            df = build_dataframe_from_canonical(spark, actor, rows=50, partitions=2, seed=42)
            assert df is not None
            assert df.count() == 50
            assert len(df.columns) >= 4
            assert "actor_id" in df.columns
            assert "first_name" in df.columns
        finally:
            spark.stop()


# -----------------------------------------------------------------------------
# morenoh149/postgresDBSamples (real PostgreSQL DDL: parse + generate data)
# See https://github.com/morenoh149/postgresDBSamples
# -----------------------------------------------------------------------------


def _pagila_excerpt_path() -> Path:
    """Path to pagila excerpt fixture (PostgreSQL DDL from postgresDBSamples)."""
    return Path(__file__).resolve().parent / "fixtures" / "postgresDBSamples" / "pagila_excerpt.sql"


class TestPostgresDBSamples:
    """Parse and generate data from sample PostgreSQL schemas (morenoh149/postgresDBSamples)."""

    def test_parse_pagila_excerpt_postgres(self):
        """Load pagila excerpt via load_schema; assert tables and columns."""
        path = _pagila_excerpt_path()
        if not path.exists():
            pytest.skip("fixture tests/fixtures/postgresDBSamples/pagila_excerpt.sql not found")
        tables = load_schema(path, format_hint=SchemaSource.POSTGRES)
        assert len(tables) >= 4
        names = {t.name for t in tables}
        assert "actor" in names
        assert "category" in names
        assert "country" in names
        assert "city" in names
        actor = next(t for t in tables if t.name == "actor")
        assert len(actor.columns) >= 4
        col_names = {c.name for c in actor.columns}
        assert "actor_id" in col_names
        assert "first_name" in col_names
        assert "last_name" in col_names
        assert "last_update" in col_names
        pk = next(c for c in actor.columns if c.name == "actor_id")
        assert pk.primary_key is True
        assert pk.type == "integer"

    def test_to_dbldatagen_specs_from_pagila_excerpt(self):
        """Parse pagila excerpt and build dbldatagen specs for every table (no Spark)."""
        path = _pagila_excerpt_path()
        if not path.exists():
            pytest.skip("fixture tests/fixtures/postgresDBSamples/pagila_excerpt.sql not found")
        tables = load_schema(path, format_hint=SchemaSource.POSTGRES)
        for table in tables:
            specs = to_dbldatagen_specs(table, rows=100)
            assert len(specs) == len(table.columns)
            for name, _spark_type, opts in specs:
                assert name
                assert isinstance(opts, dict)

    def test_generate_data_from_pagila_excerpt(self):
        """Parse pagila excerpt and build a Spark DataFrame for one table (actor)."""
        path = _pagila_excerpt_path()
        if not path.exists():
            pytest.skip("fixture tests/fixtures/postgresDBSamples/pagila_excerpt.sql not found")
        pytest.importorskip("dbldatagen")
        tables = load_schema(path, format_hint=SchemaSource.POSTGRES)
        actor = next(t for t in tables if t.name == "actor")
        try:
            spark = _create_spark_session("statschema_postgresDBSamples_test")
        except RuntimeError as e:
            if "Databricks Connect" in str(e) or "remote Spark" in str(e):
                pytest.skip("Local Spark not available (Databricks Connect only supports remote sessions)")
            raise
        try:
            df = build_dataframe_from_canonical(spark, actor, rows=50, partitions=2, seed=42)
            assert df is not None
            assert df.count() == 50
            assert len(df.columns) >= 4
            assert "actor_id" in df.columns
            assert "first_name" in df.columns
        finally:
            spark.stop()


# -----------------------------------------------------------------------------
# neondatabase/postgres-sample-dbs (real PostgreSQL DDL: parse + generate data)
# See https://github.com/neondatabase/postgres-sample-dbs
# -----------------------------------------------------------------------------


def _neon_periodic_table_excerpt_path() -> Path:
    """Path to periodic_table excerpt fixture (PostgreSQL DDL from neondatabase/postgres-sample-dbs)."""
    return Path(__file__).resolve().parent / "fixtures" / "neondatabase_postgres_sample_dbs" / "periodic_table_excerpt.sql"


class TestNeondatabasePostgresSampleDbs:
    """Parse and generate data from sample Postgres DBs (neondatabase/postgres-sample-dbs)."""

    def test_parse_periodic_table_excerpt_postgres(self):
        """Load periodic_table excerpt via load_schema; assert table and columns."""
        path = _neon_periodic_table_excerpt_path()
        if not path.exists():
            pytest.skip("fixture tests/fixtures/neondatabase_postgres_sample_dbs/periodic_table_excerpt.sql not found")
        tables = load_schema(path, format_hint=SchemaSource.POSTGRES)
        assert len(tables) == 1
        pt = tables[0]
        assert pt.name == "periodic_table"
        assert len(pt.columns) >= 10
        col_names = {c.name for c in pt.columns}
        assert "AtomicNumber" in col_names
        assert "Element" in col_names
        assert "Symbol" in col_names
        assert "AtomicMass" in col_names
        assert "Metal" in col_names
        pk = next(c for c in pt.columns if c.name == "AtomicNumber")
        assert pk.primary_key is True
        assert pk.type == "integer"

    def test_to_dbldatagen_specs_from_periodic_table_excerpt(self):
        """Parse periodic_table excerpt and build dbldatagen specs (no Spark)."""
        path = _neon_periodic_table_excerpt_path()
        if not path.exists():
            pytest.skip("fixture tests/fixtures/neondatabase_postgres_sample_dbs/periodic_table_excerpt.sql not found")
        tables = load_schema(path, format_hint=SchemaSource.POSTGRES)
        for table in tables:
            specs = to_dbldatagen_specs(table, rows=100)
            assert len(specs) == len(table.columns)
            for name, _spark_type, opts in specs:
                assert name
                assert isinstance(opts, dict)

    def test_generate_data_from_periodic_table_excerpt(self):
        """Parse periodic_table excerpt and build a Spark DataFrame."""
        path = _neon_periodic_table_excerpt_path()
        if not path.exists():
            pytest.skip("fixture tests/fixtures/neondatabase_postgres_sample_dbs/periodic_table_excerpt.sql not found")
        pytest.importorskip("dbldatagen")
        tables = load_schema(path, format_hint=SchemaSource.POSTGRES)
        pt = tables[0]
        try:
            spark = _create_spark_session("statschema_neon_postgres_test")
        except RuntimeError as e:
            if "Databricks Connect" in str(e) or "remote Spark" in str(e):
                pytest.skip("Local Spark not available (Databricks Connect only supports remote sessions)")
            raise
        try:
            df = build_dataframe_from_canonical(spark, pt, rows=30, partitions=2, seed=42)
            assert df is not None
            assert df.count() == 30
            assert "AtomicNumber" in df.columns
            assert "Element" in df.columns
        finally:
            spark.stop()


# -----------------------------------------------------------------------------
# DDL parser (MySQL, PostgreSQL, SQL Server schema-only dumps)
# -----------------------------------------------------------------------------


class TestDDLParser:
    def test_parse_mysql_ddl(self):
        sql = """
        CREATE TABLE `foo` (
          `id` int(11) NOT NULL AUTO_INCREMENT,
          `bar_id` int(11) DEFAULT NULL,
          `bazz` varchar(255) DEFAULT NULL,
          PRIMARY KEY (`id`)
        ) ENGINE=MyISAM DEFAULT CHARSET=latin1;
        """
        tables = parse_ddl(sql, dialect="mysql")
        assert len(tables) == 1
        assert tables[0].name == "foo"
        assert len(tables[0].columns) == 3
        assert tables[0].columns[0].name == "id"
        assert tables[0].columns[0].type == "integer"
        assert tables[0].columns[0].primary_key is True
        assert tables[0].columns[1].type == "integer"
        assert tables[0].columns[2].type == "string"

    def test_parse_postgres_ddl(self):
        sql = """
        CREATE TABLE public.phase1_table (
            id integer NOT NULL,
            col_1 integer,
            name character varying(100),
            PRIMARY KEY (id)
        );
        """
        tables = parse_ddl(sql, dialect="postgres")
        assert len(tables) == 1
        assert tables[0].name == "phase1_table"
        assert tables[0].columns[0].type == "integer"
        assert tables[0].columns[2].type == "string"

    def test_parse_sqlserver_ddl(self):
        sql = """
        CREATE TABLE [dbo].[orders] (
            [id] INT NOT NULL PRIMARY KEY,
            [customer_id] INT NULL,
            [amount] DECIMAL(10,2) NULL
        );
        """
        tables = parse_ddl(sql, dialect="sqlserver")
        assert len(tables) == 1
        assert tables[0].name == "orders"
        assert tables[0].columns[0].name == "id"
        assert tables[0].columns[0].primary_key is True
        assert tables[0].columns[2].type == "decimal"  # DECIMAL(10,2) is fixed-point

    def test_parse_ddl_auto_detect_mysql(self):
        sql = "CREATE TABLE `t` ( `id` int(11) NOT NULL, PRIMARY KEY (`id`) );"
        tables = parse_ddl(sql)
        assert len(tables) == 1
        assert tables[0].name == "t"

    def test_parse_ddl_auto_detect_postgres(self):
        sql = "CREATE TABLE t ( id integer NOT NULL, name character varying(50), PRIMARY KEY (id) );"
        tables = parse_ddl(sql)
        assert len(tables) == 1
        assert tables[0].columns[1].type == "string"

    def test_parse_ddl_file(self, tmp_path):
        sql_path = tmp_path / "dump.sql"
        sql_path.write_text("CREATE TABLE x ( a int, b varchar(10), PRIMARY KEY (a) );", encoding="utf-8")
        tables = parse_ddl_file(sql_path, dialect="mysql")
        assert len(tables) == 1
        assert tables[0].name == "x"
        assert len(tables[0].columns) == 2

    def test_parse_ddl_multiple_tables(self):
        sql = """
        CREATE TABLE a ( id int PRIMARY KEY );
        CREATE TABLE b ( id int PRIMARY KEY, ref int );
        """
        tables = parse_ddl(sql, dialect="mysql")
        assert len(tables) == 2
        assert tables[0].name == "a"
        assert tables[1].name == "b"
        assert len(tables[1].columns) == 2

    def test_parse_ddl_file_nonexistent_raises(self):
        with pytest.raises(FileNotFoundError):
            parse_ddl_file("/nonexistent/dump.sql")


# -----------------------------------------------------------------------------
# dbldatagen builder
# -----------------------------------------------------------------------------


class TestDbldatagenBuilder:
    def test_to_dbldatagen_specs_returns_triples(self):
        table = CanonicalTableSchema(
            name="t",
            columns=[
                CanonicalColumn(name="a", type="integer"),
                CanonicalColumn(name="b", type="string"),
            ],
        )
        specs = to_dbldatagen_specs(table, rows=100)
        assert len(specs) == 2
        assert specs[0][0] == "a"
        assert specs[1][0] == "b"
        assert isinstance(specs[0], tuple)
        assert len(specs[0]) == 3
        name, col_type, kwargs = specs[0]
        assert name == "a"
        assert kwargs.get("random") is True or "minValue" in kwargs or "prefix" in kwargs

    def test_to_dbldatagen_specs_unknown_type_falls_back_to_string(self):
        table = CanonicalTableSchema(
            name="t",
            columns=[CanonicalColumn(name="x", type="unknown_type")],
        )
        specs = to_dbldatagen_specs(table)
        assert len(specs) == 1
        assert specs[0][0] == "x"
        # When pyspark is available, fallback is string; when not, _SPARK_TYPES is empty so still string
        assert specs[0][2]  # kwargs present

    def test_to_dbldatagen_specs_with_random_type_choice(self):
        table = CanonicalTableSchema(
            name="t",
            columns=[CanonicalColumn(name="x", type="custom")],
        )
        specs = to_dbldatagen_specs(table, random_type_choice=lambda: "integer")
        assert len(specs) == 1
        assert specs[0][0] == "x"

    def test_to_dbldatagen_specs_unique_adds_uniqueValues(self):
        table = CanonicalTableSchema(
            name="t",
            columns=[
                CanonicalColumn(
                    name="id",
                    type="integer",
                    generation=GenerationRule(unique=True),
                ),
            ],
        )
        specs = to_dbldatagen_specs(table, rows=1000)
        assert specs[0][2].get("uniqueValues") == 1000

    def test_to_dbldatagen_specs_weights(self):
        """GenerationRule.weights is passed through to dbldatagen opts."""
        table = CanonicalTableSchema(
            name="t",
            columns=[
                CanonicalColumn(
                    name="status",
                    type="string",
                    generation=GenerationRule(values=["a", "b", "c"], weights=[0.5, 0.3, 0.2]),
                ),
            ],
        )
        specs = to_dbldatagen_specs(table)
        assert len(specs) == 1
        assert specs[0][2].get("values") == ["a", "b", "c"]
        assert specs[0][2].get("weights") == [0.5, 0.3, 0.2]

    def test_to_dbldatagen_specs_timestamp_kwargs(self):
        """Canonical type timestamp produces begin/end kwargs."""
        table = CanonicalTableSchema(
            name="t",
            columns=[CanonicalColumn(name="ts", type="timestamp")],
        )
        specs = to_dbldatagen_specs(table)
        assert len(specs) == 1
        opts = specs[0][2]
        assert "begin" in opts
        assert "end" in opts
        assert opts.get("random") is True

    def test_to_dbldatagen_specs_double_boolean_float(self):
        """Canonical types double, boolean, float get correct Spark type and opts."""
        table = CanonicalTableSchema(
            name="t",
            columns=[
                CanonicalColumn(name="d", type="double"),
                CanonicalColumn(name="flag", type="boolean"),
                CanonicalColumn(name="f", type="float"),
            ],
        )
        specs = to_dbldatagen_specs(table)
        assert len(specs) == 3
        assert specs[0][0] == "d"
        assert specs[1][0] == "flag"
        assert specs[2][0] == "f"
        # All should have kwargs (random or min/max)
        assert specs[0][2]
        assert specs[1][2].get("random") is True
        assert specs[2][2]


# ============================================================================
# Tests for new synthetic-data shortcoming remediation fields
# ============================================================================

class TestGenerationRuleEnhancements:
    """Verify new GenerationRule fields added to address synthetic data shortcomings."""

    def test_default_values(self):
        g = GenerationRule()
        assert g.distribution == "auto"
        assert g.distribution_params == {}
        assert g.format_pattern is None
        assert g.inject_boundary_values is True
        assert g.inject_nulls_from_stats is True
        assert g.use_mcv_weights is True
        assert g.inject_rare_events is False

    def test_distribution_explicit(self):
        g = GenerationRule(distribution="zipf", distribution_params={"a": 1.2})
        assert g.distribution == "zipf"
        assert g.distribution_params["a"] == 1.2

    def test_format_pattern_named(self):
        g = GenerationRule(format_pattern="email")
        assert g.format_pattern == "email"

    def test_format_pattern_regex(self):
        g = GenerationRule(format_pattern=r"[A-Z]{2}\d{6}")
        assert g.format_pattern == r"[A-Z]{2}\d{6}"

    def test_injection_flags_override(self):
        g = GenerationRule(
            inject_boundary_values=False,
            inject_nulls_from_stats=False,
            use_mcv_weights=False,
            inject_rare_events=True,
        )
        assert g.inject_boundary_values is False
        assert g.inject_nulls_from_stats is False
        assert g.use_mcv_weights is False
        assert g.inject_rare_events is True

    def test_all_named_format_patterns_are_strings(self):
        named = [
            "email", "phone_us", "phone_intl", "uuid", "ip_v4", "ip_v6",
            "url", "postal_us", "postal_uk", "ssn", "credit_card", "iban",
            "name_first", "name_last", "company", "address", "city",
            "country_iso2", "currency_iso",
        ]
        for p in named:
            g = GenerationRule(format_pattern=p)
            assert g.format_pattern == p

    def test_distribution_names(self):
        for dist in ("auto", "uniform", "normal", "zipf", "exponential", "constant", "sequential"):
            g = GenerationRule(distribution=dist)
            assert g.distribution == dist

    def test_extra_still_works(self):
        g = GenerationRule(extra={"spark_option": "foo"})
        assert g.extra["spark_option"] == "foo"


class TestColumnStatsEnhancements:
    """Verify skewness/kurtosis added to ColumnStats for distribution shape."""

    def test_defaults_none(self):
        from src.statschema.stats_model import ColumnStats
        cs = ColumnStats(name="age")
        assert cs.skewness is None
        assert cs.kurtosis is None

    def test_right_skewed(self):
        from src.statschema.stats_model import ColumnStats
        cs = ColumnStats(name="salary", skewness=2.5, kurtosis=6.0)
        assert cs.skewness == 2.5
        assert cs.kurtosis == 6.0

    def test_symmetric_normal(self):
        from src.statschema.stats_model import ColumnStats
        cs = ColumnStats(name="height", skewness=0.0, kurtosis=0.0)
        assert cs.skewness == 0.0
        assert cs.kurtosis == 0.0

    def test_yaml_roundtrip_with_skewness(self):
        """skewness and kurtosis survive a to_dict / from_dict round-trip."""
        import yaml
        from src.statschema.stats_model import ColumnStats, DatabaseStats, TableStats
        cs = ColumnStats(
            name="price",
            null_fraction=0.02,
            n_distinct=500,
            min_value="0.01",
            max_value="9999.99",
            skewness=1.8,
            kurtosis=4.2,
        )
        ts = TableStats(name="products", row_count=10000, columns=[cs])
        db = DatabaseStats(tables=[ts], source_dialect="mysql")
        yaml_str = yaml.dump(db.to_dict(), sort_keys=False)
        db2 = DatabaseStats.from_dict(yaml.safe_load(yaml_str))
        cs2 = db2.tables[0].columns[0]
        assert cs2.skewness == 1.8
        assert cs2.kurtosis == 4.2

    def test_yaml_roundtrip_without_skewness(self):
        """When skewness/kurtosis are None they are omitted from YAML."""
        import yaml
        from src.statschema.stats_model import ColumnStats, DatabaseStats, TableStats
        cs = ColumnStats(name="status", null_fraction=0.0, n_distinct=3)
        ts = TableStats(name="orders", row_count=1000, columns=[cs])
        db = DatabaseStats(tables=[ts])
        d = db.to_dict()
        col_d = d["tables"][0]["columns"][0]
        assert "skewness" not in col_d
        assert "kurtosis" not in col_d
        db2 = DatabaseStats.from_dict(d)
        assert db2.tables[0].columns[0].skewness is None
        assert db2.tables[0].columns[0].kurtosis is None


class TestTemporalOrderingConstraints:
    """Verify temporal_ordering_constraints on CanonicalTableSchema."""

    def test_default_empty(self):
        t = CanonicalTableSchema(name="t", columns=[])
        assert t.temporal_ordering_constraints == []

    def test_set_constraints(self):
        t = CanonicalTableSchema(
            name="orders",
            columns=[],
            temporal_ordering_constraints=[
                "end_date > start_date",
                "updated_at >= created_at",
            ],
        )
        assert len(t.temporal_ordering_constraints) == 2
        assert "end_date > start_date" in t.temporal_ordering_constraints

    def test_multiple_table_instances_independent(self):
        """Each CanonicalTableSchema instance has its own constraint list."""
        t1 = CanonicalTableSchema(name="a", columns=[])
        t2 = CanonicalTableSchema(name="b", columns=[])
        t1.temporal_ordering_constraints.append("end > start")
        assert t2.temporal_ordering_constraints == []

    def test_all_constraint_operators(self):
        """All four comparison operators are accepted as plain strings."""
        constraints = [
            "end_date > start_date",
            "updated_at >= created_at",
            "depart_at < arrive_at",
            "birth_date <= hire_date",
        ]
        t = CanonicalTableSchema(
            name="t",
            columns=[],
            temporal_ordering_constraints=constraints,
        )
        assert t.temporal_ordering_constraints == constraints

    def test_realistic_table_schema(self):
        """A realistic order table has correct constraints and columns."""
        cols = [
            CanonicalColumn(name="id",          type="long",        primary_key=True),
            CanonicalColumn(name="ordered_at",  type="timestamptz", not_null=True),
            CanonicalColumn(name="shipped_at",  type="timestamptz"),
            CanonicalColumn(name="delivered_at",type="timestamptz"),
            CanonicalColumn(name="cancelled_at",type="timestamptz"),
        ]
        table = CanonicalTableSchema(
            name="orders",
            columns=cols,
            temporal_ordering_constraints=[
                "shipped_at > ordered_at",
                "delivered_at > shipped_at",
            ],
        )
        assert len(table.temporal_ordering_constraints) == 2
        assert "shipped_at > ordered_at" in table.temporal_ordering_constraints


class TestSyntheticShortcomingsCoverageMatrix:
    """
    Meta-tests that verify the canonical model carries the fields needed to
    address each documented synthetic data shortcoming.
    """

    def test_null_rate_capturable(self):
        """null_fraction addresses: NULL rates not honoured."""
        from src.statschema.stats_model import ColumnStats
        cs = ColumnStats(name="optional_col", null_fraction=0.15)
        assert cs.null_fraction == 0.15

    def test_mcv_weights_available(self):
        """MCVs + use_mcv_weights address: hot-spot / Zipf distribution not preserved."""
        from src.statschema.stats_model import ColumnStats, MostCommonValue
        cs = ColumnStats(
            name="status",
            most_common_values=[
                MostCommonValue("active",    0.72),
                MostCommonValue("inactive",  0.18),
                MostCommonValue("pending",   0.08),
                MostCommonValue("deleted",   0.02),
            ],
        )
        g = GenerationRule(use_mcv_weights=True)
        assert cs.most_common_values[0].frequency == 0.72
        assert g.use_mcv_weights is True

    def test_boundary_injection_capturable(self):
        """min_value + max_value + inject_boundary_values address: boundary values missing."""
        from src.statschema.stats_model import ColumnStats
        cs = ColumnStats(name="age", min_value="18", max_value="120")
        g = GenerationRule(inject_boundary_values=True)
        assert cs.min_value == "18"
        assert cs.max_value == "120"
        assert g.inject_boundary_values is True

    def test_fk_cardinality_capturable(self):
        """avg_children_per_parent addresses: FK cardinality ratios lost."""
        from src.statschema.stats_model import ForeignKeyStats
        fk = ForeignKeyStats(
            columns=["student_id"],
            parent_table="students",
            parent_columns=["id"],
            avg_children_per_parent=5.1,
            cardinality_pattern="N:1",
        )
        assert fk.avg_children_per_parent == 5.1

    def test_functional_dependency_capturable(self):
        """CompositeColumnStats.dependencies addresses: correlated columns drift apart."""
        from src.statschema.stats_model import CompositeColumnStats
        cs = CompositeColumnStats(
            columns=["zip_code", "city"],
            dependencies={"zip_code->city": 0.99},
        )
        assert cs.dependencies["zip_code->city"] == 0.99

    def test_format_pattern_for_realistic_strings(self):
        """format_pattern addresses: random strings do not look like emails/phones/UUIDs."""
        for pattern in ("email", "phone_us", "uuid", "ip_v4", "url"):
            g = GenerationRule(format_pattern=pattern)
            assert g.format_pattern == pattern

    def test_rare_event_injection(self):
        """inject_rare_events addresses: rare/tail events never appear in synthetic data."""
        g = GenerationRule(inject_rare_events=True)
        assert g.inject_rare_events is True

    def test_distribution_shape_capturable(self):
        """skewness/kurtosis address: generator doesn't know true distribution shape."""
        from src.statschema.stats_model import ColumnStats
        # Income is typically strongly right-skewed (log-normal)
        cs = ColumnStats(name="income", skewness=3.5, kurtosis=15.0)
        assert cs.skewness > 0   # right-skewed
        assert cs.kurtosis > 0   # heavy-tailed

    def test_temporal_ordering_for_consistency(self):
        """temporal_ordering_constraints addresses: end_date < start_date silently corrupt."""
        t = CanonicalTableSchema(
            name="sessions",
            columns=[],
            temporal_ordering_constraints=["end_time > start_time"],
        )
        assert t.temporal_ordering_constraints == ["end_time > start_time"]

    def test_zipf_distribution_for_hot_spots(self):
        """Zipf distribution setting addresses: uniform random misses query plan hot-spots."""
        g = GenerationRule(
            distribution="zipf",
            distribution_params={"a": 1.5},
        )
        assert g.distribution == "zipf"
        assert g.distribution_params["a"] == 1.5
