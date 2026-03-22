"""
Coverage-gap tests — exercises branches not reached by test_schema_parser.py.

Covers (sorted by impact):
  A. DDL emitter — all 6 dialects, all column types, if_not_exists guards
  B. Model serialisation — GenerationRule, CanonicalColumn, CanonicalTableSchema to_dict/from_dict
  C. Model expand_table_instances — instance_count, aliases, invalid format string
  D. Schema IO — dump_schema / load_canonical round-trips (string, file, dict, expand)
  E. Override model — ColumnOverride, TableOverride, OverrideSpec serialisation + from/to_yaml
  F. Stats model — MostCommonValue, ColumnStats, IndexStats, MCVCombination,
                   CompositeColumnStats, ForeignKeyStats, TableStats, DatabaseStats
  G. dbldatagen builder helpers — _dbldatagen_distribution, _cast_stat_value,
                                   _spark_type_and_options with stats/distribution/length
  H. Loader edge paths — alias normalisation, invalid format_hint, Oracle/unknown source
  I. SDV parser edge paths — computer_representation variants, parse_sdv_file
  J. Ydata parser edge paths — string-value fields, parse_ydata_multi_yaml
  K. Semantic hints LLM stub — _infer_format_pattern_llm raises NotImplementedError
"""

from __future__ import annotations

import io
import textwrap
import tempfile
from pathlib import Path

import pytest

from src.statschema.model import (
    CanonicalColumn,
    CanonicalForeignKey,
    CanonicalTableSchema,
    GenerationRule,
    expand_table_instances,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _col(name: str, col_type: str, **kwargs) -> CanonicalColumn:
    return CanonicalColumn(name=name, type=col_type, **kwargs)


def _simple_table(name: str = "t", cols=None) -> CanonicalTableSchema:
    if cols is None:
        cols = [_col("id", "long", primary_key=True, auto_increment=True)]
    return CanonicalTableSchema(name=name, columns=cols)


# ─────────────────────────────────────────────────────────────────────────────
# A. DDL emitter
# ─────────────────────────────────────────────────────────────────────────────

class TestDDLEmitter:
    from src.statschema.ddl_emitter import emit_ddl, emit_ddl_all

    ALL_TYPES_TABLE = CanonicalTableSchema(
        name="all_types",
        columns=[
            _col("id",         "long",       primary_key=True, auto_increment=True),
            _col("name",       "string",     length=100,  not_null=True),
            _col("bio",        "string"),                        # no length → TEXT / MAX
            _col("score",      "integer"),
            _col("price",      "decimal",    precision=10, scale=2),
            _col("ratio",      "float"),
            _col("weight",     "double"),
            _col("active",     "boolean"),
            _col("created_at", "timestamp",  not_null=True),
            _col("tz_ts",      "timestamptz"),
            _col("dob",        "date"),
            _col("start_time", "time"),
            _col("timetz_col", "timetz"),
            _col("payload",    "binary"),
            _col("status",     "string",     length=20, default="'pending'"),
        ],
    )

    def _emit(self, dialect: str, table=None, **kwargs) -> str:
        from src.statschema.ddl_emitter import emit_ddl
        return emit_ddl(table or self.ALL_TYPES_TABLE, dialect, **kwargs)

    # ── basic emission ────────────────────────────────────────────────────

    @pytest.mark.parametrize("dialect", ["mysql", "postgres", "sqlserver", "oracle", "databricks", "db2"])
    def test_emit_all_dialects(self, dialect):
        ddl = self._emit(dialect)
        assert "all_types" in ddl.lower() or "ALL_TYPES" in ddl
        assert "id" in ddl.lower() or "ID" in ddl.lower()

    @pytest.mark.parametrize("dialect", ["mysql", "postgres", "sqlserver", "oracle", "databricks", "db2"])
    def test_emit_if_not_exists_false(self, dialect):
        ddl = self._emit(dialect, if_not_exists=False)
        assert "CREATE TABLE" in ddl
        assert "IF NOT EXISTS" not in ddl.upper() or dialect not in ("mysql", "postgres", "databricks", "db2")

    # ── per-dialect string type mapping ──────────────────────────────────

    def test_mysql_varchar_with_length(self):
        t = _simple_table(cols=[_col("s", "string", length=50)])
        ddl = self._emit("mysql", table=t)
        assert "VARCHAR(50)" in ddl

    def test_mysql_text_without_length(self):
        t = _simple_table(cols=[_col("s", "string")])
        ddl = self._emit("mysql", table=t)
        assert "TEXT" in ddl

    def test_postgres_character_varying(self):
        t = _simple_table(cols=[_col("s", "string", length=50)])
        ddl = self._emit("postgres", table=t)
        assert "CHARACTER VARYING(50)" in ddl

    def test_sqlserver_nvarchar(self):
        t = _simple_table(cols=[_col("s", "string", length=50)])
        ddl = self._emit("sqlserver", table=t)
        assert "NVARCHAR(50)" in ddl

    def test_sqlserver_nvarchar_max_no_length(self):
        t = _simple_table(cols=[_col("s", "string")])
        ddl = self._emit("sqlserver", table=t)
        assert "NVARCHAR(MAX)" in ddl

    def test_oracle_varchar2(self):
        t = _simple_table(cols=[_col("s", "string", length=50)])
        ddl = self._emit("oracle", table=t)
        assert "VARCHAR2(50)" in ddl

    def test_databricks_string_always(self):
        t = _simple_table(cols=[_col("s", "string", length=50)])
        ddl = self._emit("databricks", table=t)
        assert "STRING" in ddl

    # ── decimal precision/scale ──────────────────────────────────────────

    def test_decimal_precision_scale(self):
        t = _simple_table(cols=[_col("p", "decimal", precision=12, scale=4)])
        for dialect in ["mysql", "sqlserver"]:
            ddl = self._emit(dialect, table=t)
            assert "DECIMAL(12" in ddl and "4" in ddl
        # Postgres maps decimal → NUMERIC
        ddl_pg = self._emit("postgres", table=t)
        assert "NUMERIC(12" in ddl_pg and "4" in ddl_pg

    # ── auto-increment per dialect ────────────────────────────────────────

    def test_mysql_auto_increment(self):
        ddl = self._emit("mysql")
        assert "AUTO_INCREMENT" in ddl

    def test_postgres_bigserial(self):
        ddl = self._emit("postgres")
        assert "BIGSERIAL" in ddl

    def test_sqlserver_identity(self):
        ddl = self._emit("sqlserver")
        assert "IDENTITY(1,1)" in ddl

    def test_oracle_generated_always(self):
        ddl = self._emit("oracle")
        assert "GENERATED ALWAYS AS IDENTITY" in ddl

    def test_databricks_generated_always(self):
        ddl = self._emit("databricks")
        assert "GENERATED ALWAYS AS IDENTITY" in ddl

    def test_db2_generated_always(self):
        ddl = self._emit("db2")
        assert "GENERATED ALWAYS AS IDENTITY" in ddl

    # ── if_not_exists guards ──────────────────────────────────────────────

    def test_sqlserver_if_not_exists_guard(self):
        ddl = self._emit("sqlserver", if_not_exists=True)
        assert "OBJECT_ID" in ddl

    def test_oracle_execute_immediate(self):
        ddl = self._emit("oracle", if_not_exists=True)
        assert "EXECUTE IMMEDIATE" in ddl or "BEGIN" in ddl

    def test_db2_if_not_exists(self):
        ddl = self._emit("db2", if_not_exists=True)
        assert "IF NOT EXISTS" in ddl

    # ── FSP (fractional seconds precision) ───────────────────────────────

    def test_fsp_on_timestamp(self):
        t = _simple_table(cols=[_col("ts", "timestamp", fsp=3)])
        for dialect in ["mysql", "postgres"]:
            ddl = self._emit(dialect, table=t)
            assert "(3)" in ddl

    def test_fsp_timetz_postgres(self):
        t = _simple_table(cols=[_col("t", "timetz", fsp=6)])
        ddl = self._emit("postgres", table=t)
        assert "WITH TIME ZONE" in ddl

    # ── primary key constraint ────────────────────────────────────────────

    def test_primary_key_constraint_included(self):
        t = _simple_table(cols=[
            _col("id", "long", primary_key=True),
            _col("val", "string"),
        ])
        ddl = self._emit("mysql", table=t)
        assert "PRIMARY KEY" in ddl

    def test_no_pk_no_constraint(self):
        t = _simple_table(cols=[_col("val", "string")])
        ddl = self._emit("mysql", table=t)
        assert "PRIMARY KEY" not in ddl

    # ── default values ────────────────────────────────────────────────────

    def test_default_value_in_ddl(self):
        t = _simple_table(cols=[_col("status", "string", length=20, default="'active'")])
        for dialect in ["mysql", "postgres", "sqlserver"]:
            ddl = self._emit(dialect, table=t)
            assert "DEFAULT" in ddl

    def test_boolean_default_mysql(self):
        t = _simple_table(cols=[_col("active", "boolean", default="true")])
        ddl = self._emit("mysql", table=t)
        assert "DEFAULT" in ddl

    # ── emit_ddl_all ──────────────────────────────────────────────────────

    def test_emit_ddl_all(self):
        from src.statschema.ddl_emitter import emit_ddl_all
        tables = [_simple_table("a"), _simple_table("b")]
        result = emit_ddl_all(tables, "mysql")
        assert "CREATE TABLE" in result
        assert result.count("CREATE TABLE") >= 2

    def test_unsupported_dialect_raises(self):
        from src.statschema.ddl_emitter import emit_ddl
        with pytest.raises(ValueError, match="Unsupported"):
            emit_ddl(_simple_table(), "cobol")

    # ── binary type with explicit length ──────────────────────────────────

    @pytest.mark.parametrize("dialect,expected_fragment", [
        ("mysql",       "VARBINARY(64)"),
        ("postgres",    "BYTEA"),
        ("sqlserver",   "VARBINARY(64)"),
        ("oracle",      "RAW(64)"),
        ("db2",         "BLOB(64)"),
        ("databricks",  "BINARY"),
    ])
    def test_binary_with_length(self, dialect, expected_fragment):
        t = _simple_table(cols=[_col("blob_col", "binary", length=64)])
        ddl = self._emit(dialect, table=t)
        assert expected_fragment in ddl

    def test_oracle_raw_large_becomes_blob(self):
        t = _simple_table(cols=[_col("doc", "binary", length=3000)])
        ddl = self._emit("oracle", table=t)
        assert "BLOB" in ddl

    # ── decimal oracle NUMBER / postgres NUMERIC ──────────────────────────

    def test_oracle_decimal_maps_to_number(self):
        t = _simple_table(cols=[_col("price", "decimal", precision=10, scale=2)])
        ddl = self._emit("oracle", table=t)
        assert "NUMBER(10" in ddl

    def test_postgres_decimal_maps_to_numeric(self):
        t = _simple_table(cols=[_col("price", "decimal", precision=10, scale=2)])
        ddl = self._emit("postgres", table=t)
        assert "NUMERIC(10" in ddl

    # ── timestamptz FSP on mysql/sqlserver ────────────────────────────────

    def test_timestamptz_fsp_mysql(self):
        t = _simple_table(cols=[_col("ts", "timestamptz", fsp=3)])
        ddl = self._emit("mysql", table=t)
        assert "(3)" in ddl

    def test_timestamptz_fsp_oracle(self):
        t = _simple_table(cols=[_col("ts", "timestamptz", fsp=6)])
        ddl = self._emit("oracle", table=t)
        assert "WITH TIME ZONE" in ddl and "(6)" in ddl

    # ── boolean default normalisation ─────────────────────────────────────

    @pytest.mark.parametrize("dialect,default_val,expected", [
        ("mysql",     "true",  "1"),
        ("sqlserver", "1",     "1"),
        ("oracle",    "false", "0"),
        ("db2",       "true",  "TRUE"),
        ("postgres",  "1",     "TRUE"),
    ])
    def test_boolean_default_normalisation(self, dialect, default_val, expected):
        t = _simple_table(cols=[_col("active", "boolean", default=default_val)])
        ddl = self._emit(dialect, table=t)
        assert f"DEFAULT {expected}" in ddl

    # ── unique constraint clause ──────────────────────────────────────────

    def test_unique_clause_non_pk_column(self):
        t = _simple_table(cols=[_col("email", "string", length=255, unique=True)])
        ddl = self._emit("mysql", table=t)
        assert "UNIQUE" in ddl

    def test_no_unique_clause_when_pk(self):
        t = _simple_table(cols=[_col("id", "long", primary_key=True, unique=True)])
        ddl = self._emit("mysql", table=t)
        # PK columns don't get an extra UNIQUE clause
        assert ddl.count("UNIQUE") == 0

    # ── source_type_comment (cross-dialect annotation) ────────────────────

    def test_source_type_comment_different_dialect(self):
        col = _col("email", "string", length=100,
                   constraints={"source_type": "NVARCHAR2", "source_dialect": "oracle"})
        t = _simple_table(cols=[col])
        ddl = self._emit("mysql", table=t)
        assert "originally" in ddl.lower() or "NVARCHAR2" in ddl

    def test_source_type_comment_same_dialect_suppressed(self):
        col = _col("email", "string", length=100,
                   constraints={"source_type": "VARCHAR(100)", "source_dialect": "mysql"})
        t = _simple_table(cols=[col])
        ddl = self._emit("mysql", table=t)
        assert "originally" not in ddl

    def test_source_type_comment_equivalent_types_suppressed(self):
        col = _col("ts", "timestamptz",
                   constraints={"source_type": "DATETIMEOFFSET", "source_dialect": "sqlserver"})
        t = _simple_table(cols=[col])
        ddl = self._emit("postgres", table=t)
        assert "originally" not in ddl

    def test_serial_type_comment(self):
        col = _col("id", "long", primary_key=True,
                   constraints={"serial_type": "bigserial", "source_dialect": "postgres"})
        t = _simple_table(cols=[col])
        ddl = self._emit("mysql", table=t)
        assert "BIGSERIAL" in ddl or "originally" in ddl.lower()

    def test_source_type_comment_no_type_no_serial(self):
        """constraints dict with no source_type and no serial_type → empty comment."""
        col = _col("x", "string", constraints={"pii": True})
        t = _simple_table(cols=[col])
        ddl = self._emit("mysql", table=t)
        assert "originally" not in ddl

    def test_source_type_comment_emitted_matches_source(self):
        """Emitted type matches source_type exactly → comment suppressed."""
        col = _col("qty", "long",
                   constraints={"source_type": "BIGINT", "source_dialect": "postgres"})
        t = _simple_table(cols=[col])
        ddl = self._emit("mysql", table=t)
        assert "originally" not in ddl

    # ── decimal no precision / integer SERIAL on postgres ────────────────

    def test_decimal_no_precision_uses_default(self):
        t = _simple_table(cols=[_col("amt", "decimal")])
        for dialect in ["mysql", "postgres", "oracle"]:
            ddl = self._emit(dialect, table=t)
            assert "DECIMAL" in ddl or "NUMERIC" in ddl or "NUMBER" in ddl

    def test_integer_auto_increment_postgres_serial(self):
        t = _simple_table(cols=[_col("id", "integer", primary_key=True, auto_increment=True)])
        ddl = self._emit("postgres", table=t)
        assert "SERIAL" in ddl and "BIGSERIAL" not in ddl

    # ── timestamptz with fsp on postgres ─────────────────────────────────

    def test_timestamptz_fsp_postgres(self):
        t = _simple_table(cols=[_col("ts", "timestamptz", fsp=3)])
        ddl = self._emit("postgres", table=t)
        assert "WITH TIME ZONE" in ddl and "(3)" in ddl

    # ── boolean default false and unknown for db2 / postgres ─────────────

    def test_boolean_default_false_db2(self):
        t = _simple_table(cols=[_col("active", "boolean", default="false")])
        ddl = self._emit("db2", table=t)
        assert "DEFAULT FALSE" in ddl

    def test_boolean_default_false_postgres(self):
        t = _simple_table(cols=[_col("active", "boolean", default="false")])
        ddl = self._emit("postgres", table=t)
        assert "DEFAULT FALSE" in ddl

    def test_boolean_unknown_literal_passes_through(self):
        """Unknown boolean literal is passed through unchanged."""
        t = _simple_table(cols=[_col("active", "boolean", default="UNKNOWN")])
        ddl = self._emit("postgres", table=t)
        assert "DEFAULT UNKNOWN" in ddl

    # ── NOT NULL and nullable columns ─────────────────────────────────────

    def test_not_null_clause(self):
        t = _simple_table(cols=[_col("x", "integer", not_null=True)])
        ddl = self._emit("mysql", table=t)
        assert "NOT NULL" in ddl

    def test_nullable_column_no_not_null(self):
        t = _simple_table(cols=[_col("x", "integer", not_null=False)])
        ddl = self._emit("mysql", table=t)
        # nullable column should not have NOT NULL
        lines = [ln.strip() for ln in ddl.split("\n") if "`x`" in ln]
        assert lines and "NOT NULL" not in lines[0]


# ─────────────────────────────────────────────────────────────────────────────
# B. Model serialisation — GenerationRule, CanonicalColumn, CanonicalTableSchema
# ─────────────────────────────────────────────────────────────────────────────

class TestModelSerialisation:

    def test_generation_rule_full_round_trip(self):
        g = GenerationRule(
            min_value=0, max_value=100,
            values=["a", "b"], weights=[0.7, 0.3],
            max_length=50, unique=True,
            distribution="zipf", distribution_params={"a": 1.5},
            format_pattern="email",
            inject_boundary_values=False,
            inject_nulls_from_stats=False,
            use_mcv_weights=False,
            inject_rare_events=True,
            extra={"custom_key": "val"},
        )
        g2 = GenerationRule.from_dict(g.to_dict())
        assert g2.min_value == 0
        assert g2.max_value == 100
        assert g2.values == ["a", "b"]
        assert g2.weights == [0.7, 0.3]
        assert g2.max_length == 50
        assert g2.unique is True
        assert g2.distribution == "zipf"
        assert g2.distribution_params == {"a": 1.5}
        assert g2.format_pattern == "email"
        assert g2.inject_boundary_values is False
        assert g2.inject_nulls_from_stats is False
        assert g2.use_mcv_weights is False
        assert g2.inject_rare_events is True
        assert g2.extra == {"custom_key": "val"}

    def test_generation_rule_defaults_omitted(self):
        """Default fields are not written to the dict (compact serialisation)."""
        g = GenerationRule()
        d = g.to_dict()
        assert "inject_boundary_values" not in d  # default True → omitted
        assert "inject_rare_events" not in d       # default False → omitted
        assert "distribution" not in d             # default "auto" → omitted

    def test_canonical_column_full_round_trip(self):
        g = GenerationRule(distribution="uniform")
        col = CanonicalColumn(
            name="email", type="string", length=255,
            not_null=True, unique=True, primary_key=False,
            auto_increment=False, default=None,
            description="user email address",
            constraints={"source_type": "VARCHAR"},
            generation=g,
            references=("users", "id"),
        )
        col2 = CanonicalColumn.from_dict(col.to_dict())
        assert col2.name == "email"
        assert col2.length == 255
        assert col2.not_null is True
        assert col2.unique is True
        assert col2.description == "user email address"
        assert col2.generation is not None
        assert col2.generation.distribution == "uniform"
        assert col2.references == ("users", "id")
        assert col2.constraints == {"source_type": "VARCHAR"}

    def test_canonical_column_minimal(self):
        col = CanonicalColumn(name="x", type="integer")
        col2 = CanonicalColumn.from_dict(col.to_dict())
        assert col2.name == "x"
        assert col2.type == "integer"
        assert col2.generation is None

    def test_canonical_table_full_round_trip(self):
        fk = CanonicalForeignKey(
            columns=["customer_id"],
            parent_table="customers",
            parent_columns=["id"],
            fk_distribution="uniform",
            fk_children_min=1,
            fk_children_max=5,
        )
        table = CanonicalTableSchema(
            name="orders",
            columns=[
                _col("id", "long", primary_key=True, auto_increment=True),
                _col("customer_id", "long", not_null=True),
            ],
            description="Order records",
            fk_constraints=[fk],
            foreign_keys=[{"from_col": "customer_id", "to_table": "customers", "to_col": "id"}],
            temporal_ordering_constraints=["shipped_at > ordered_at"],
            aliases=["orders_backup"],
            instance_count=3,
            instance_suffix_format="_{:03d}",
        )
        d = table.to_dict()
        t2 = CanonicalTableSchema.from_dict(d)
        assert t2.name == "orders"
        assert t2.description == "Order records"
        assert len(t2.fk_constraints) == 1
        assert t2.fk_constraints[0].fk_distribution == "uniform"
        assert t2.temporal_ordering_constraints == ["shipped_at > ordered_at"]
        assert t2.aliases == ["orders_backup"]
        assert t2.instance_count == 3
        assert t2.instance_suffix_format == "_{:03d}"


# ─────────────────────────────────────────────────────────────────────────────
# C. expand_table_instances
# ─────────────────────────────────────────────────────────────────────────────

class TestExpandTableInstances:

    def test_single_instance_returns_as_is(self):
        t = _simple_table("events")
        result = expand_table_instances(t)
        assert len(result) == 1
        assert result[0].name == "events"

    def test_instance_count_expands(self):
        t = CanonicalTableSchema(
            name="shard",
            columns=[_col("id", "long")],
            instance_count=3,
        )
        result = expand_table_instances(t)
        assert len(result) == 3
        assert {r.name for r in result} == {"shard_0001", "shard_0002", "shard_0003"}

    def test_custom_suffix_format(self):
        t = CanonicalTableSchema(
            name="part",
            columns=[_col("id", "long")],
            instance_count=2,
            instance_suffix_format="_{:02d}",
        )
        result = expand_table_instances(t)
        assert {r.name for r in result} == {"part_01", "part_02"}

    def test_aliases_appended(self):
        t = CanonicalTableSchema(
            name="main",
            columns=[_col("id", "long")],
            aliases=["replica_1", "replica_2"],
        )
        result = expand_table_instances(t)
        names = [r.name for r in result]
        assert "main" in names
        assert "replica_1" in names
        assert "replica_2" in names

    def test_instance_count_with_aliases(self):
        t = CanonicalTableSchema(
            name="t",
            columns=[_col("id", "long")],
            instance_count=2,
            aliases=["t_extra"],
        )
        result = expand_table_instances(t)
        names = [r.name for r in result]
        assert "t_0001" in names
        assert "t_0002" in names
        assert "t_extra" in names

    def test_invalid_format_string_raises(self):
        t = CanonicalTableSchema(
            name="t",
            columns=[_col("id", "long")],
            instance_count=2,
            instance_suffix_format="_{bad}",
        )
        with pytest.raises(ValueError, match="invalid instance_suffix_format"):
            expand_table_instances(t)

    def test_list_input(self):
        tables = [_simple_table("a"), _simple_table("b")]
        result = expand_table_instances(tables)
        assert {r.name for r in result} == {"a", "b"}


# ─────────────────────────────────────────────────────────────────────────────
# D. Schema IO — dump_schema / load_canonical
# ─────────────────────────────────────────────────────────────────────────────

class TestSchemaIO:
    from src.statschema.schema_io import dump_schema, load_canonical

    YAML_STR = textwrap.dedent("""\
        version: '1.0'
        tables:
          - name: users
            columns:
              - name: id
                type: long
                primary_key: true
              - name: email
                type: string
                length: 255
    """)

    def test_dump_schema_returns_string(self):
        from src.statschema.schema_io import dump_schema
        tables = [_simple_table("users")]
        result = dump_schema(tables)
        assert isinstance(result, str)
        assert "users" in result

    def test_dump_schema_single_table(self):
        from src.statschema.schema_io import dump_schema
        table = _simple_table("products")
        result = dump_schema(table)          # single table, not list
        assert "products" in result

    def test_dump_schema_to_file(self, tmp_path):
        from src.statschema.schema_io import dump_schema
        p = tmp_path / "schema.yaml"
        dump_schema([_simple_table("t")], path=str(p))
        assert p.exists()
        assert "name: t" in p.read_text()

    def test_load_canonical_from_inline_yaml(self):
        from src.statschema.schema_io import load_canonical
        tables = load_canonical(self.YAML_STR)
        assert len(tables) == 1
        assert tables[0].name == "users"
        assert len(tables[0].columns) == 2

    def test_load_canonical_from_dict(self):
        from src.statschema.schema_io import load_canonical
        import yaml
        data = yaml.safe_load(self.YAML_STR)
        tables = load_canonical(data)
        assert tables[0].name == "users"

    def test_load_canonical_from_path_object(self, tmp_path):
        from src.statschema.schema_io import load_canonical
        p = tmp_path / "s.yaml"
        p.write_text(self.YAML_STR)
        tables = load_canonical(p)
        assert tables[0].name == "users"

    def test_load_canonical_from_path_string(self, tmp_path):
        from src.statschema.schema_io import load_canonical
        p = tmp_path / "s.yaml"
        p.write_text(self.YAML_STR)
        tables = load_canonical(str(p))    # no newlines → treated as file path
        assert tables[0].name == "users"

    def test_load_canonical_expand(self):
        from src.statschema.schema_io import load_canonical
        import yaml
        multi = textwrap.dedent("""\
            version: '1.0'
            tables:
              - name: shard
                instance_count: 2
                columns:
                  - name: id
                    type: long
        """)
        tables = load_canonical(multi, expand=True)
        assert len(tables) == 2
        names = {t.name for t in tables}
        assert "shard_0001" in names

    def test_dump_load_round_trip(self):
        from src.statschema.schema_io import dump_schema, load_canonical
        original = [
            CanonicalTableSchema(
                name="orders",
                columns=[
                    _col("id", "long", primary_key=True, auto_increment=True),
                    _col("total", "decimal", precision=10, scale=2),
                ],
                fk_constraints=[
                    CanonicalForeignKey(
                        columns=["customer_id"],
                        parent_table="customers",
                        parent_columns=["id"],
                    )
                ],
            )
        ]
        yaml_str = dump_schema(original)
        restored = load_canonical(yaml_str)
        assert restored[0].name == "orders"
        assert restored[0].fk_constraints[0].parent_table == "customers"


# ─────────────────────────────────────────────────────────────────────────────
# E. Override model
# ─────────────────────────────────────────────────────────────────────────────

class TestOverrideModel:

    def test_column_override_round_trip(self):
        from src.statschema.override_model import ColumnOverride
        co = ColumnOverride(
            rename_to="renamed",
            type="string",
            length=100,
            precision=10, scale=2,
            not_null=True,
            default="'x'",
            null_fraction=0.05,
            n_distinct=50,
            min_value="0",
            max_value="100",
            most_common_values=[{"value": "a", "frequency": 0.9}],
        )
        d = co.to_dict()
        co2 = ColumnOverride.from_dict(d)
        assert co2.rename_to == "renamed"
        assert co2.type == "string"
        assert co2.length == 100
        assert co2.null_fraction == pytest.approx(0.05)
        assert co2.most_common_values == [{"value": "a", "frequency": 0.9}]

    def test_column_override_empty(self):
        from src.statschema.override_model import ColumnOverride
        co = ColumnOverride()
        assert co.to_dict() == {}

    def test_table_override_round_trip(self):
        from src.statschema.override_model import TableOverride, ColumnOverride
        to = TableOverride(
            rename_to="new_name",
            rename_schema_to="new_schema",
            row_count=5000,
            row_count_scale=2.0,
            columns={"price": ColumnOverride(min_value="1", max_value="999")},
        )
        d = to.to_dict()
        to2 = TableOverride.from_dict(d)
        assert to2.rename_to == "new_name"
        assert to2.rename_schema_to == "new_schema"
        assert to2.row_count == 5000
        assert to2.row_count_scale == pytest.approx(2.0)
        assert "price" in to2.columns

    def test_override_spec_get_reserved_words_oracle(self):
        from src.statschema.override_model import OverrideSpec
        words = OverrideSpec().get_reserved_words("oracle")
        assert "select" in words or "SELECT" in {w.upper() for w in words}

    def test_override_spec_get_reserved_words_postgres(self):
        from src.statschema.override_model import OverrideSpec
        words = OverrideSpec().get_reserved_words("postgres")
        assert len(words) > 0

    def test_override_spec_get_reserved_words_sqlserver(self):
        from src.statschema.override_model import OverrideSpec
        words = OverrideSpec().get_reserved_words("sqlserver")
        assert len(words) > 0

    def test_override_spec_get_reserved_words_unknown(self):
        from src.statschema.override_model import OverrideSpec
        words = OverrideSpec().get_reserved_words("cobol")
        assert words == set()

    def test_override_spec_with_custom_reserved_words(self):
        from src.statschema.override_model import OverrideSpec
        spec = OverrideSpec(reserved_words={"myword", "otherword"})
        words = spec.get_reserved_words("mysql")
        assert "myword" in words

    def test_override_spec_lowercase_identifiers_serialised(self):
        from src.statschema.override_model import OverrideSpec
        spec = OverrideSpec(lowercase_identifiers=True)
        d = spec.to_dict()
        assert d.get("lowercase_identifiers") is True
        spec2 = OverrideSpec.from_dict(d)
        assert spec2.lowercase_identifiers is True

    def test_override_spec_full_round_trip(self):
        from src.statschema.override_model import OverrideSpec, TableOverride, ColumnOverride
        spec = OverrideSpec(
            description="Test overrides",
            schema_mappings={"dbo": "public"},
            catalog_mappings={"prod": "dev"},
            uppercase_identifiers=True,
            reserved_word_prefix="x_",
            reserved_words={"order", "table"},
            type_mappings={"datetime": "timestamp"},
            row_count_scale=0.1,
            tables={"orders": TableOverride(row_count=1000)},
        )
        d = spec.to_dict()
        spec2 = OverrideSpec.from_dict(d)
        assert spec2.description == "Test overrides"
        assert spec2.schema_mappings == {"dbo": "public"}
        assert spec2.uppercase_identifiers is True
        assert "order" in spec2.reserved_words
        assert spec2.row_count_scale == pytest.approx(0.1)
        assert "orders" in spec2.tables

    def test_override_spec_from_yaml_file(self, tmp_path):
        from src.statschema.override_model import OverrideSpec
        import yaml
        p = tmp_path / "overrides.yaml"
        p.write_text(yaml.dump({
            "description": "test",
            "row_count_scale": 0.5,
        }))
        spec = OverrideSpec.from_yaml(str(p))
        assert spec.description == "test"
        assert spec.row_count_scale == pytest.approx(0.5)

    def test_override_spec_from_yaml_missing_file(self):
        from src.statschema.override_model import OverrideSpec
        with pytest.raises(FileNotFoundError):
            OverrideSpec.from_yaml("/nonexistent/path/overrides.yaml")

    def test_override_spec_to_yaml_file(self, tmp_path):
        from src.statschema.override_model import OverrideSpec
        spec = OverrideSpec(description="written")
        p = tmp_path / "out.yaml"
        spec.to_yaml(str(p))
        assert p.exists()
        content = p.read_text()
        assert "written" in content


# ─────────────────────────────────────────────────────────────────────────────
# F. Stats model serialisation
# ─────────────────────────────────────────────────────────────────────────────

class TestStatsModelSerialisation:

    def test_most_common_value_round_trip(self):
        from src.statschema.stats_model import MostCommonValue
        mcv = MostCommonValue(value="NY", frequency=0.35)
        mcv2 = MostCommonValue.from_dict(mcv.to_dict())
        assert mcv2.value == "NY"
        assert mcv2.frequency == pytest.approx(0.35)

    def test_column_stats_full_round_trip(self):
        from src.statschema.stats_model import ColumnStats, MostCommonValue
        cs = ColumnStats(
            name="region",
            null_fraction=0.02,
            n_distinct=5.0,
            avg_width_bytes=6,
            min_value="APAC",
            max_value="US",
            most_common_values=[MostCommonValue("US", 0.6), MostCommonValue("EU", 0.3)],
            histogram_bounds=["APAC", "EU", "US"],
            correlation=0.9,
            skewness=1.5,
            kurtosis=2.0,
        )
        cs2 = ColumnStats.from_dict(cs.to_dict())
        assert cs2.name == "region"
        assert cs2.null_fraction == pytest.approx(0.02)
        assert cs2.n_distinct == 5.0
        assert cs2.avg_width_bytes == 6
        assert cs2.min_value == "APAC"
        assert cs2.max_value == "US"
        assert len(cs2.most_common_values) == 2
        assert cs2.histogram_bounds == ["APAC", "EU", "US"]
        assert cs2.correlation == pytest.approx(0.9)
        assert cs2.skewness == pytest.approx(1.5)
        assert cs2.kurtosis == pytest.approx(2.0)

    def test_column_stats_defaults_omitted(self):
        from src.statschema.stats_model import ColumnStats
        cs = ColumnStats(name="x")
        d = cs.to_dict()
        assert "n_distinct" not in d  # default 0.0 omitted
        assert "avg_width_bytes" not in d

    def test_index_stats_round_trip(self):
        from src.statschema.stats_model import IndexStats
        idx = IndexStats(
            name="idx_email",
            columns=["email"],
            unique=True,
            cardinality=10000,
            index_type="BTREE",
        )
        idx2 = IndexStats.from_dict(idx.to_dict())
        assert idx2.name == "idx_email"
        assert idx2.columns == ["email"]
        assert idx2.unique is True
        assert idx2.cardinality == 10000
        assert idx2.index_type == "BTREE"

    def test_index_stats_no_cardinality(self):
        from src.statschema.stats_model import IndexStats
        idx = IndexStats(name="i", columns=["x"], unique=False)
        d = idx.to_dict()
        assert "cardinality" not in d

    def test_mcv_combination_round_trip(self):
        from src.statschema.stats_model import MCVCombination
        c = MCVCombination(values=["US", "NY"], frequency=0.15)
        c2 = MCVCombination.from_dict(c.to_dict())
        assert c2.values == ["US", "NY"]
        assert c2.frequency == pytest.approx(0.15)

    def test_composite_column_stats_full_round_trip(self):
        from src.statschema.stats_model import CompositeColumnStats, MCVCombination
        ccs = CompositeColumnStats(
            columns=["country", "city"],
            n_distinct=500,
            dependencies={"country->city": 0.95},
            most_common_combinations=[MCVCombination(["US", "NY"], 0.10)],
        )
        ccs2 = CompositeColumnStats.from_dict(ccs.to_dict())
        assert ccs2.columns == ["country", "city"]
        assert ccs2.n_distinct == 500
        assert ccs2.dependencies == {"country->city": 0.95}
        assert len(ccs2.most_common_combinations) == 1

    def test_foreign_key_stats_full_round_trip(self):
        from src.statschema.stats_model import ForeignKeyStats
        fk = ForeignKeyStats(
            columns=["customer_id"],
            parent_table="customers",
            parent_columns=["id"],
            parent_schema="public",
            name="fk_orders_customer",
            cardinality_pattern="N:1",
            match_fraction=0.98,
            avg_children_per_parent=4.5,
        )
        fk2 = ForeignKeyStats.from_dict(fk.to_dict())
        assert fk2.columns == ["customer_id"]
        assert fk2.parent_schema == "public"
        assert fk2.name == "fk_orders_customer"
        assert fk2.match_fraction == pytest.approx(0.98)
        assert fk2.avg_children_per_parent == pytest.approx(4.5)

    def test_table_stats_column_stats_found(self):
        from src.statschema.stats_model import TableStats, ColumnStats
        cs = ColumnStats(name="email", null_fraction=0.0)
        ts = TableStats(name="users", row_count=1000, columns=[cs])
        found = ts.column_stats("email")
        assert found is not None
        assert found.name == "email"

    def test_table_stats_column_stats_not_found(self):
        from src.statschema.stats_model import TableStats
        ts = TableStats(name="users", row_count=0)
        assert ts.column_stats("nonexistent") is None

    def test_table_stats_full_round_trip(self):
        from src.statschema.stats_model import (
            TableStats, ColumnStats, IndexStats, ForeignKeyStats, CompositeColumnStats,
        )
        ts = TableStats(
            name="orders",
            schema="public",
            catalog="prod",
            row_count=50_000,
            data_size_bytes=2_000_000,
            avg_row_bytes=40,
            last_analyzed="2025-01-15T10:00:00",
            columns=[ColumnStats(name="id", null_fraction=0.0)],
            indexes=[IndexStats(name="pk_orders", columns=["id"], unique=True)],
            foreign_keys=[ForeignKeyStats(
                columns=["customer_id"],
                parent_table="customers",
                parent_columns=["id"],
            )],
            composite_stats=[CompositeColumnStats(columns=["a", "b"])],
        )
        ts2 = TableStats.from_dict(ts.to_dict())
        assert ts2.name == "orders"
        assert ts2.schema == "public"
        assert ts2.catalog == "prod"
        assert ts2.row_count == 50_000
        assert ts2.data_size_bytes == 2_000_000
        assert ts2.last_analyzed == "2025-01-15T10:00:00"
        assert len(ts2.columns) == 1
        assert len(ts2.indexes) == 1
        assert len(ts2.foreign_keys) == 1
        assert len(ts2.composite_stats) == 1

    def test_database_stats_table_stats_found(self):
        from src.statschema.stats_model import DatabaseStats, TableStats
        ts = TableStats(name="orders", row_count=100)
        db = DatabaseStats(tables=[ts], source_dialect="mysql")
        assert db.table_stats("orders") is ts
        assert db.table_stats("missing") is None

    def test_database_stats_round_trip(self):
        from src.statschema.stats_model import DatabaseStats, TableStats
        db = DatabaseStats(
            tables=[TableStats(name="t", row_count=10)],
            version="1.0",
            source_dialect="postgres",
        )
        db2 = DatabaseStats.from_dict(db.to_dict())
        assert db2.source_dialect == "postgres"
        assert len(db2.tables) == 1
        assert db2.tables[0].name == "t"


# ─────────────────────────────────────────────────────────────────────────────
# G. dbldatagen builder helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestDbldatagenBuilderHelpers:

    # ── _cast_stat_value ─────────────────────────────────────────────────

    @pytest.mark.parametrize("value,col_type,expected", [
        ("42",   "integer",   42),
        ("42.9", "long",      42),
        ("3.14", "float",     3.14),
        ("3.14", "double",    3.14),
        ("3.14", "decimal",   3.14),
        ("2024-01-01", "timestamp", "2024-01-01"),
        ("2024-01-01", "timestamptz", "2024-01-01"),
        ("2024-01-01", "date", "2024-01-01"),
        ("foo",  "string",    None),         # non-numeric type → None
        (None,   "integer",   None),         # None input → None
        ("bad",  "integer",   None),         # bad value → None
    ])
    def test_cast_stat_value(self, value, col_type, expected):
        from src.statschema.dbldatagen_builder import _cast_stat_value
        result = _cast_stat_value(value, col_type)
        if expected is None:
            assert result is None
        elif isinstance(expected, float):
            assert result == pytest.approx(expected)
        else:
            assert result == expected

    # ── _dbldatagen_distribution ─────────────────────────────────────────

    @pytest.mark.parametrize("dist,params", [
        ("auto",        {}),
        ("uniform",     {}),
        ("sequential",  {}),
        ("constant",    {}),
    ])
    def test_distribution_returns_none_for_pass_through(self, dist, params):
        from src.statschema.dbldatagen_builder import _dbldatagen_distribution
        assert _dbldatagen_distribution(dist, params) is None

    def test_distribution_normal(self):
        from src.statschema.dbldatagen_builder import _dbldatagen_distribution
        result = _dbldatagen_distribution("normal", {"mean": 5.0, "std": 2.0})
        assert result is not None

    def test_distribution_zipf_returns_gamma_approx(self):
        from src.statschema.dbldatagen_builder import _dbldatagen_distribution
        result = _dbldatagen_distribution("zipf", {"a": 1.5})
        assert result is not None

    def test_distribution_exponential(self):
        from src.statschema.dbldatagen_builder import _dbldatagen_distribution
        result = _dbldatagen_distribution("exponential", {"scale": 2.0})
        assert result is not None

    def test_distribution_beta(self):
        from src.statschema.dbldatagen_builder import _dbldatagen_distribution
        result = _dbldatagen_distribution("beta", {"alpha": 2.0, "beta": 5.0})
        assert result is not None

    def test_distribution_gamma(self):
        from src.statschema.dbldatagen_builder import _dbldatagen_distribution
        result = _dbldatagen_distribution("gamma", {"shape": 1.0, "scale": 2.0})
        assert result is not None

    def test_distribution_unknown_returns_none(self):
        from src.statschema.dbldatagen_builder import _dbldatagen_distribution
        assert _dbldatagen_distribution("invented", {}) is None

    # ── _spark_type_and_options ──────────────────────────────────────────

    def _opts(self, col, **kwargs):
        from src.statschema.dbldatagen_builder import _spark_type_and_options
        _, opts = _spark_type_and_options(col, **kwargs)
        return opts

    def test_decimal_precision_scale(self):
        from src.statschema.dbldatagen_builder import _spark_type_and_options
        from pyspark.sql.types import DecimalType
        col = _col("price", "decimal", precision=12, scale=4)
        spark_type, _ = _spark_type_and_options(col)
        assert isinstance(spark_type, DecimalType)
        assert spark_type.precision == 12
        assert spark_type.scale == 4

    def test_explicit_min_max_from_generation_rule(self):
        col = _col("age", "integer",
                   generation=GenerationRule(min_value=0, max_value=120))
        opts = self._opts(col)
        assert opts["minValue"] == 0
        assert opts["maxValue"] == 120

    def test_explicit_values_from_generation_rule(self):
        col = _col("status", "string",
                   generation=GenerationRule(values=["A", "B"], weights=[0.7, 0.3]))
        opts = self._opts(col)
        assert opts["values"] == ["A", "B"]
        assert opts["weights"] == [0.7, 0.3]

    def test_unique_sets_unique_values(self):
        col = _col("uid", "long",
                   generation=GenerationRule(unique=True))
        opts = self._opts(col, rows=500)
        assert opts.get("uniqueValues") == 500

    def test_mcv_weights_dominant(self):
        from src.statschema.stats_model import ColumnStats, MostCommonValue
        cs = ColumnStats(
            name="region",
            n_distinct=3.0,
            most_common_values=[
                MostCommonValue("US", 0.60),
                MostCommonValue("EU", 0.30),
                MostCommonValue("APAC", 0.10),
            ],
        )
        col = _col("region", "string")
        opts = self._opts(col, col_stats=cs)
        assert "values" in opts
        assert "weights" in opts

    def test_mcv_weights_low_cardinality(self):
        from src.statschema.stats_model import ColumnStats, MostCommonValue
        cs = ColumnStats(
            name="cat",
            n_distinct=5.0,
            most_common_values=[
                MostCommonValue("X", 0.10),
                MostCommonValue("Y", 0.10),
            ],
        )
        col = _col("cat", "string")
        opts = self._opts(col, col_stats=cs)
        assert "values" in opts

    def test_null_fraction_from_col_stats(self):
        from src.statschema.stats_model import ColumnStats
        cs = ColumnStats(name="x", null_fraction=0.15)
        col = _col("x", "integer")
        opts = self._opts(col, col_stats=cs)
        assert opts.get("percentNulls") == pytest.approx(0.15)

    def test_min_max_from_col_stats_does_not_override_type_default(self):
        """Stats min/max are not applied when the type default already sets minValue.
        All numeric _SPARK_TYPES include minValue/maxValue defaults, so col_stats
        values only apply to types without defaults (e.g. boolean is excluded).
        This exercises the code path and confirms the guard behaviour."""
        from src.statschema.stats_model import ColumnStats
        cs = ColumnStats(name="score", min_value="10", max_value="500")
        col = _col("score", "integer")
        opts = self._opts(col, col_stats=cs)
        # Default minValue=0 is already in opts from _SPARK_TYPES; stats don't override it
        assert "minValue" in opts

    def test_format_pattern_sets_template(self):
        col = _col("ssn_col", "string",
                   generation=GenerationRule(format_pattern="ssn"))
        opts = self._opts(col)
        assert "template" in opts

    def test_semantic_inference_sets_template(self):
        col = _col("email_address", "string")  # triggers infer_format_pattern
        opts = self._opts(col)
        assert "template" in opts

    def test_distribution_control_sets_distribution(self):
        col = _col("cat", "integer",
                   generation=GenerationRule(distribution="zipf",
                                             distribution_params={"a": 1.5}))
        opts = self._opts(col)
        assert "distribution" in opts

    def test_sequential_removes_random(self):
        col = _col("seq", "long",
                   generation=GenerationRule(distribution="sequential"))
        opts = self._opts(col)
        assert "random" not in opts

    def test_string_length_template(self):
        col = _col("name", "string", length=30)
        opts = self._opts(col)
        assert "template" in opts


# ─────────────────────────────────────────────────────────────────────────────
# H-pre. DDL parser edge cases
# ─────────────────────────────────────────────────────────────────────────────

class TestDDLParserEdgeCases:

    def _parse(self, ddl: str, dialect=None):
        from src.statschema.ddl_parser import parse_ddl
        return parse_ddl(ddl, dialect=dialect)

    # ── dialect detection ─────────────────────────────────────────────────

    def test_detect_oracle_from_varchar2(self):
        tables = self._parse("CREATE TABLE t (name VARCHAR2(50))")
        assert tables[0].columns[0].type == "string"

    def test_detect_sqlserver_from_nvarchar(self):
        tables = self._parse("CREATE TABLE t (name NVARCHAR(100))")
        assert tables[0].columns[0].type == "string"

    def test_detect_postgres_from_integer_keyword(self):
        tables = self._parse("CREATE TABLE t (id INTEGER, val BIGINT)")
        assert tables[0].columns[0].type in ("integer", "long")

    def test_detect_mysql_fallback(self):
        """Simple DDL with no dialect hints → mysql fallback."""
        tables = self._parse("CREATE TABLE t (x INT, y FLOAT)", dialect=None)
        assert len(tables) == 1

    # ── USERDEFINED / exotic types ────────────────────────────────────────

    def test_inet_type_maps_to_string(self):
        tables = self._parse("CREATE TABLE t (ip INET)", dialect="postgres")
        assert tables[0].columns[0].type == "string"

    def test_pg_bit_varying_maps_to_binary(self):
        tables = self._parse("CREATE TABLE t (bits BIT VARYING(8))", dialect="postgres")
        assert tables[0].columns[0].type == "binary"

    def test_geometry_maps_to_binary(self):
        tables = self._parse("CREATE TABLE t (geom GEOMETRY)", dialect="postgres")
        assert tables[0].columns[0].type == "binary"

    def test_cidr_maps_to_string(self):
        tables = self._parse("CREATE TABLE t (net CIDR)", dialect="postgres")
        assert tables[0].columns[0].type == "string"

    # ── PG BIT(n>1) → binary ─────────────────────────────────────────────

    def test_pg_bit_n_maps_to_binary(self):
        tables = self._parse("CREATE TABLE t (flags BIT(8))", dialect="postgres")
        assert tables[0].columns[0].type == "binary"

    # ── TINYINT(1) MySQL → boolean ────────────────────────────────────────

    def test_mysql_tinyint1_maps_to_boolean(self):
        tables = self._parse("CREATE TABLE t (active TINYINT(1))", dialect="mysql")
        assert tables[0].columns[0].type == "boolean"

    # ── UBIGINT / MONEY / SMALLMONEY / MONEY (postgres) ──────────────────

    def test_bigint_unsigned_maps_to_decimal(self):
        """BIGINT UNSIGNED (MySQL) → UBIGINT internally → decimal(20,0)."""
        tables = self._parse("CREATE TABLE t (n BIGINT UNSIGNED)", dialect="mysql")
        col = tables[0].columns[0]
        assert col.type == "decimal"
        assert col.precision == 20

    def test_money_sqlserver_maps_to_decimal(self):
        tables = self._parse("CREATE TABLE t (price MONEY)", dialect="sqlserver")
        col = tables[0].columns[0]
        assert col.type == "decimal"
        assert col.precision == 19

    def test_money_postgres_maps_to_decimal(self):
        tables = self._parse("CREATE TABLE t (price MONEY)", dialect="postgres")
        col = tables[0].columns[0]
        assert col.type == "decimal"

    def test_smallmoney_maps_to_decimal(self):
        tables = self._parse("CREATE TABLE t (price SMALLMONEY)", dialect="sqlserver")
        col = tables[0].columns[0]
        assert col.type == "decimal"

    # ── temporal FSP ─────────────────────────────────────────────────────

    def test_timestamp_fsp(self):
        tables = self._parse("CREATE TABLE t (ts TIMESTAMP(3))", dialect="mysql")
        assert tables[0].columns[0].fsp == 3

    # ── Oracle NUMBER(p) precision classification ─────────────────────────

    def test_oracle_number_small_precision_integer(self):
        tables = self._parse("CREATE TABLE t (x NUMBER(9))", dialect="oracle")
        assert tables[0].columns[0].type == "integer"

    def test_oracle_number_medium_precision_long(self):
        tables = self._parse("CREATE TABLE t (x NUMBER(15))", dialect="oracle")
        assert tables[0].columns[0].type == "long"

    def test_oracle_number_large_precision_decimal(self):
        tables = self._parse("CREATE TABLE t (x NUMBER(25))", dialect="oracle")
        assert tables[0].columns[0].type == "decimal"

    # ── serial_type recorded for SERIAL/BIGSERIAL ─────────────────────────

    def test_serial_type_constraint_recorded(self):
        tables = self._parse("CREATE TABLE t (id SERIAL PRIMARY KEY)", dialect="postgres")
        col = tables[0].columns[0]
        assert col.constraints.get("serial_type") is not None

    # ── table-level PRIMARY KEY backfills column ──────────────────────────

    def test_table_level_pk_backfills_column(self):
        tables = self._parse(
            "CREATE TABLE t (id INT, val VARCHAR(10), PRIMARY KEY (id))",
            dialect="mysql",
        )
        id_col = next(c for c in tables[0].columns if c.name == "id")
        assert id_col.primary_key is True
        assert id_col.not_null is True

    # ── boolean DEFAULT TRUE/FALSE → lowercase ────────────────────────────

    def test_boolean_default_true_lowercased(self):
        tables = self._parse(
            "CREATE TABLE t (active BOOLEAN DEFAULT TRUE)",
            dialect="postgres",
        )
        assert tables[0].columns[0].default == "true"

    # ── VARCHAR(MAX) parameter parsing ───────────────────────────────────

    def test_varchar_max_parameter(self):
        tables = self._parse(
            "CREATE TABLE t (doc NVARCHAR(MAX))", dialect="sqlserver"
        )
        assert tables[0].columns[0].type == "string"

    # ── Oracle RAW/NCLOB preprocessing → MySQL ───────────────────────────

    def test_oracle_raw_preprocessed_for_mysql(self):
        tables = self._parse(
            "CREATE TABLE t (id RAW(16), descr NCLOB)", dialect="mysql"
        )
        assert len(tables[0].columns) == 2
        col_types = {c.name: c.type for c in tables[0].columns}
        assert col_types["id"] == "binary"
        assert col_types["descr"] == "string"

    # ── unknown dialect defaults to mysql ────────────────────────────────

    def test_unknown_dialect_falls_back_to_mysql(self):
        from src.statschema.ddl_parser import parse_ddl
        tables = parse_ddl("CREATE TABLE t (x TINYTEXT)", dialect="cobol")
        assert len(tables) == 1

    # ── FK constraints parsing ────────────────────────────────────────────

    def test_fk_constraint_parsed(self):
        ddl = """
        CREATE TABLE orders (
            id INT PRIMARY KEY,
            customer_id INT,
            FOREIGN KEY (customer_id) REFERENCES customers(id)
        );
        """
        tables = self._parse(ddl, dialect="mysql")
        assert tables[0].fk_constraints is not None
        assert len(tables[0].fk_constraints) >= 1

    def test_fk_no_reference_skipped(self):
        """FK node with no Reference → skipped gracefully."""
        ddl = "CREATE TABLE t (id INT, val VARCHAR(10));"
        tables = self._parse(ddl, dialect="mysql")
        assert tables[0].fk_constraints is None or tables[0].fk_constraints == []

    def test_fk_references_table_without_columns(self):
        """FK REFERENCES with no column list → ref.this is exp.Table, not exp.Schema."""
        ddl = """
        CREATE TABLE orders (
            id INT PRIMARY KEY,
            customer_id INT,
            FOREIGN KEY (customer_id) REFERENCES customers
        );
        """
        tables = self._parse(ddl, dialect="mysql")
        fks = tables[0].fk_constraints
        assert fks and len(fks) >= 1
        assert fks[0].parent_table == "customers"
        assert fks[0].parent_columns == []

    def test_userdefined_kind_as_plain_string(self):
        """VARBIT with db2 dialect: kind_arg is a plain str (not Identifier), exercises str() branch."""
        # db2 maps to an empty sqlglot dialect which preserves kind as a raw string.
        ddl = "CREATE TABLE t (bits VARBIT(64));"
        tables = self._parse(ddl, dialect="db2")
        col = tables[0].columns[0]
        assert col.type == "binary"

    def test_column_def_without_datatype_skipped(self):
        """Computed/generated columns with no DataType node are silently skipped."""
        ddl = "CREATE TABLE t (id INT, full_name AS (first_name || last_name));"
        tables = self._parse(ddl, dialect=None)
        col_names = [c.name for c in tables[0].columns]
        assert "id" in col_names
        assert "full_name" not in col_names


# ─────────────────────────────────────────────────────────────────────────────
# H. Loader edge paths
# ─────────────────────────────────────────────────────────────────────────────

class TestLoaderEdgePaths:

    def test_syda_alias_maps_to_ydata(self):
        from src.statschema.loader import get_schema_source_from_data, SchemaSource
        data = {"schema_source": "syda", "customer_id": {"type": "id"}}
        assert get_schema_source_from_data(data) == SchemaSource.YDATA

    def test_postgres_alias(self):
        from src.statschema.loader import get_schema_source_from_data, SchemaSource
        data = {"schema_source": "pg"}
        assert get_schema_source_from_data(data) == SchemaSource.POSTGRES

    def test_mysql_alias(self):
        from src.statschema.loader import get_schema_source_from_data, SchemaSource
        data = {"schema_source": "mariadb"}
        assert get_schema_source_from_data(data) == SchemaSource.MYSQL

    def test_db2_alias(self):
        from src.statschema.loader import get_schema_source_from_data, SchemaSource
        data = {"schema_source": "ibm_db2"}
        assert get_schema_source_from_data(data) == SchemaSource.DB2

    def test_invalid_format_hint_raises(self):
        from src.statschema.loader import load_schema
        with pytest.raises(ValueError, match="Unknown schema source"):
            load_schema({"tables": []}, format_hint="invalidxyz")

    def test_oracle_source_raises_not_implemented(self):
        from src.statschema.loader import load_schema, SchemaSource
        with pytest.raises(ValueError, match="not yet implemented"):
            load_schema({}, format_hint="oracle")

    def test_load_schema_ydata_single_table(self):
        from src.statschema.loader import load_schema
        data = {
            "schema_source": "ydata",
            "customer_id": {"type": "id"},
            "email": {"type": "text"},
        }
        tables = load_schema(data)
        assert len(tables) >= 1

    def test_load_schema_ydata_multi_table(self):
        from src.statschema.loader import load_schema
        data = {
            "customers": {
                "customer_id": {"type": "id"},
                "email": {"type": "text"},
            },
            "orders": {
                "order_id": {"type": "id"},
            },
        }
        tables = load_schema(data, format_hint="ydata")
        assert len(tables) == 2

    def test_schema_source_unknown_value_is_skipped(self):
        """Unrecognised schema_source value → ValueError → continue → returns None."""
        from src.statschema.loader import get_schema_source_from_data
        result = get_schema_source_from_data({"schema_source": "completely_bogus_xyz"})
        assert result is None

    def test_schema_source_unknown_then_valid(self):
        """Loop continues past unknown value and picks up a valid key."""
        from src.statschema.loader import get_schema_source_from_data, SchemaSource
        # "source" key has valid "ydata" value; "schema_source" has bogus value
        result = get_schema_source_from_data({
            "schema_source": "bogus_value_xyz",
            "source": "ydata",
        })
        assert result == SchemaSource.YDATA

    def test_detect_format_pipeline_via_tables_list(self):
        """detect_format returns PIPELINE when data has a 'tables' list of column-bearing dicts."""
        from src.statschema.loader import load_schema
        data = {
            "tables": [
                {"name": "users", "columns": [{"name": "id", "type": "long"}]},
            ]
        }
        tables = load_schema(data)
        assert len(tables) >= 1

    def test_detect_format_pipeline_fallback(self):
        """detect_format falls back to PIPELINE when nothing matches."""
        from src.statschema.loader import detect_format, SchemaSource
        result = detect_format({"arbitrary_key": "arbitrary_value"})
        assert result == SchemaSource.PIPELINE

    def test_load_schema_mysql_alias_format_hint(self):
        """format_hint 'mariadb' resolves to mysql."""
        from src.statschema.loader import load_schema
        ddl = "CREATE TABLE t (id INT);"
        tables = load_schema({"ddl": ddl}, format_hint="mariadb")
        assert len(tables) >= 1

    def test_load_schema_db2_alias_format_hint(self):
        """format_hint 'db2luw' resolves to db2."""
        from src.statschema.loader import load_schema
        ddl = "CREATE TABLE t (id INT);"
        tables = load_schema({"ddl": ddl}, format_hint="db2luw")
        assert len(tables) >= 1

    def test_load_schema_empty_format_hint(self):
        """Empty-string format_hint is normalised to None (auto-detect)."""
        from src.statschema.loader import load_schema
        data = {"customer_id": {"type": "id"}, "email": {"type": "text"}}
        tables = load_schema(data, format_hint="")
        assert len(tables) >= 1

    def test_load_schema_inline_ddl_in_dict(self):
        """load_schema with SQL dialect format_hint + dict containing 'ddl' key."""
        from src.statschema.loader import load_schema
        tables = load_schema({"ddl": "CREATE TABLE orders (id INT, total DECIMAL(10,2));"}, format_hint="mysql")
        assert len(tables) == 1
        assert tables[0].name == "orders"

    def test_load_schema_ydata_auto_detected_multi_table(self):
        """Multi-table ydata data auto-detected and dispatched to parse_ydata_multi_yaml."""
        from src.statschema.loader import load_schema
        data = {
            "__table_description__": "multi",
            "orders": {"order_id": {"type": "id"}},
        }
        tables = load_schema(data)
        assert len(tables) >= 1

    def test_load_schema_postgres_alias_format_hint(self):
        """format_hint 'pg' normalises to postgres."""
        from src.statschema.loader import load_schema
        tables = load_schema({"ddl": "CREATE TABLE t (id INT);"}, format_hint="pg")
        assert len(tables) >= 1

    def test_detect_format_pipeline_secondary_tables_check(self):
        """Secondary 'tables' list check (lines 85-87) fires when pipeline.tables has no columns."""
        from src.statschema.loader import detect_format, SchemaSource
        data = {
            "pipeline": {
                "tables": [{"name": "t_no_cols"}],   # no 'columns' key → first loop doesn't return
            },
            "tables": [
                {"name": "users", "columns": [{"name": "id"}]},
            ],
        }
        assert detect_format(data) == SchemaSource.PIPELINE


# ─────────────────────────────────────────────────────────────────────────────
# I. SDV parser edge paths
# ─────────────────────────────────────────────────────────────────────────────

class TestSDVParserEdgePaths:

    def _parse_col(self, col_name, spec, pk=None):
        from src.statschema.sdv_parser import _parse_sdv_column
        return _parse_sdv_column(col_name, spec, pk)

    @pytest.mark.parametrize("rep,expected_type", [
        ("Int8",   "integer"),
        ("Int16",  "integer"),
        ("Int32",  "integer"),
        ("Int64",  "long"),
        ("UInt8",  "long"),
        ("UInt16", "long"),
        ("UInt32", "long"),
        ("UInt64", "long"),
        ("Float",  "float"),
    ])
    def test_numerical_computer_representation(self, rep, expected_type):
        col = self._parse_col("x", {"sdtype": "numerical", "computer_representation": rep})
        assert col.type == expected_type

    def test_sdv_with_relationships_list(self):
        from src.statschema.sdv_parser import parse_sdv_metadata
        data = {
            "METADATA_SPEC_VERSION": "V1",
            "tables": {
                "customers": {
                    "primary_key": "id",
                    "columns": {
                        "id": {"sdtype": "id"},
                        "name": {"sdtype": "text"},
                    },
                },
                "orders": {
                    "primary_key": "order_id",
                    "columns": {
                        "order_id": {"sdtype": "id"},
                        "customer_id": {"sdtype": "id"},
                    },
                },
            },
            "relationships": [
                {"parent_table_name": "customers", "parent_primary_key": "id",
                 "child_table_name": "orders", "child_foreign_key": "customer_id"},
            ],
        }
        tables = parse_sdv_metadata(data)
        assert len(tables) == 2

    def test_parse_sdv_file(self, tmp_path):
        from src.statschema.sdv_parser import parse_sdv_file
        import json
        p = tmp_path / "meta.json"
        p.write_text(json.dumps({
            "METADATA_SPEC_VERSION": "V1",
            "tables": {
                "t": {
                    "columns": {"id": {"sdtype": "id"}},
                    "primary_key": "id",
                }
            },
        }))
        tables = parse_sdv_file(str(p))
        assert len(tables) == 1

    def test_sdv_column_with_pii_flag(self):
        col = self._parse_col("ssn", {"sdtype": "text", "pii": True})
        assert col.constraints.get("pii") is True

    def test_sdv_column_with_datetime_format(self):
        col = self._parse_col("created_at", {
            "sdtype": "datetime",
            "datetime_format": "%Y-%m-%d",
        })
        assert col.type == "timestamp"
        assert col.constraints.get("datetime_format") == "%Y-%m-%d"

    def test_sdv_non_dict_column_spec_skipped(self):
        """A column_spec that is not a dict is skipped gracefully."""
        from src.statschema.sdv_parser import parse_sdv_metadata
        data = {
            "METADATA_SPEC_VERSION": "V1",
            "tables": {
                "t": {
                    "columns": {
                        "id": {"sdtype": "id"},
                        "bad_col": "not_a_dict",     # should be skipped
                    },
                    "primary_key": "id",
                }
            },
        }
        tables = parse_sdv_metadata(data)
        col_names = [c.name for c in tables[0].columns]
        assert "id" in col_names
        assert "bad_col" not in col_names

    def test_parse_sdv_file_invalid_content_raises(self, tmp_path):
        from src.statschema.sdv_parser import parse_sdv_file
        p = tmp_path / "bad.json"
        p.write_text("null")
        with pytest.raises(ValueError, match="Invalid SDV"):
            parse_sdv_file(str(p))


# ─────────────────────────────────────────────────────────────────────────────
# J. Ydata parser edge paths
# ─────────────────────────────────────────────────────────────────────────────

class TestYdataParserEdgePaths:

    def test_parse_ydata_yaml_string_value_field(self):
        """A field whose value is a bare string (not a dict) is treated as a string column."""
        from src.statschema.ydata_parser import parse_ydata_yaml
        data = {
            "customer_id": {"type": "id"},
            "plain_string_field": "some_description",  # str value, not dict
        }
        table = parse_ydata_yaml(data, table_name="t")
        col_names = [c.name for c in table.columns]
        assert "customer_id" in col_names

    def test_parse_ydata_multi_yaml(self):
        from src.statschema.ydata_parser import parse_ydata_multi_yaml
        data = {
            "customers": {
                "customer_id": {"type": "id"},
                "email": {"type": "text"},
            },
            "orders": {
                "order_id": {"type": "id"},
            },
            "__not_a_table__": {"meta": "value"},  # should be skipped
        }
        tables = parse_ydata_multi_yaml(data)
        names = {t.name for t in tables}
        assert "customers" in names
        assert "orders" in names
        assert "__not_a_table__" not in names


# ─────────────────────────────────────────────────────────────────────────────
# K. Semantic hints LLM stub
# ─────────────────────────────────────────────────────────────────────────────

class TestDBStatsCollectorHelpers:
    """Pure-Python helpers in db_stats_collector that don't need a live DB."""

    def test_quote_mysql(self):
        from src.statschema.db_stats_collector import _quote
        assert _quote("my_table", "mysql") == "`my_table`"

    def test_quote_sqlserver(self):
        from src.statschema.db_stats_collector import _quote
        assert _quote("my_table", "sqlserver") == "[my_table]"

    def test_quote_default(self):
        from src.statschema.db_stats_collector import _quote
        assert _quote("my_table", "postgres") == '"my_table"'

    def test_schema_table_with_schema(self):
        from src.statschema.db_stats_collector import _schema_table
        assert _schema_table("orders", "mysql", "sales") == "`sales`.`orders`"

    def test_schema_table_no_schema(self):
        from src.statschema.db_stats_collector import _schema_table
        assert _schema_table("orders", "postgres", None) == '"orders"'

    def test_percentile_bounds_empty(self):
        from src.statschema.db_stats_collector import _percentile_bounds
        assert _percentile_bounds([]) == []

    def test_percentile_bounds_values(self):
        from src.statschema.db_stats_collector import _percentile_bounds
        bounds = _percentile_bounds([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        assert len(bounds) == 5

    def test_parse_pg_array_mcv_valid(self):
        from src.statschema.db_stats_collector import _parse_pg_array_mcv
        result = _parse_pg_array_mcv("{foo,bar,baz}", "{0.5,0.3,0.2}")
        assert len(result) == 3
        assert result[0].value == "foo"
        assert result[0].frequency == 0.5

    def test_parse_pg_array_mcv_invalid(self):
        from src.statschema.db_stats_collector import _parse_pg_array_mcv
        result = _parse_pg_array_mcv("not_an_array", "{bad}")
        assert result == []

    def test_percentile_bounds_unsortable(self):
        """Non-comparable mixed types trigger the except → []."""
        from src.statschema.db_stats_collector import _percentile_bounds
        result = _percentile_bounds([1, "two", None])
        assert result == []


class TestStatsInjectorHelpers:
    """Pure-Python helpers in stats_injector that don't need a live DB."""

    def test_injection_result_success_true(self):
        from src.statschema.stats_injector import InjectionResult
        r = InjectionResult(dialect="mysql", table="t", rows_injected=100,
                            columns_injected=3, columns_skipped=0, warnings=[])
        assert r.success is True

    def test_injection_result_success_false(self):
        from src.statschema.stats_injector import InjectionResult
        r = InjectionResult(dialect="mysql", table="t", rows_injected=100,
                            columns_injected=0, columns_skipped=2, warnings=["err"])
        assert r.success is False

    def test_mysql_b64_str(self):
        import base64 as _b64
        from src.statschema.stats_injector import _mysql_b64_str
        result = _mysql_b64_str("hello")
        expected_b64 = _b64.b64encode(b"hello").decode()
        assert result == f"base64:type254:{expected_b64}"


# ─────────────────────────────────────────────────────────────────────────────
# K. Semantic hints LLM stub
# ─────────────────────────────────────────────────────────────────────────────

class TestSemanticHintsLLMStub:

    def test_llm_stub_raises_not_implemented(self):
        from src.statschema.semantic_hints import _infer_format_pattern_llm
        with pytest.raises(NotImplementedError):
            _infer_format_pattern_llm("customer_name")

    def test_llm_stub_raises_with_sample_values(self):
        from src.statschema.semantic_hints import _infer_format_pattern_llm
        with pytest.raises(NotImplementedError):
            _infer_format_pattern_llm("ssn_field", sample_values=["123-45-6789"])


# ─────────────────────────────────────────────────────────────────────────────
# L. Stats I/O edge paths
# ─────────────────────────────────────────────────────────────────────────────

class TestStatsIO:

    def _make_schema(self, **col_kwargs):
        from src.statschema.model import CanonicalColumn, CanonicalTableSchema
        col = CanonicalColumn(name="col", type="string", **col_kwargs)
        return CanonicalTableSchema(name="t", columns=[col])

    # ── load_stats ────────────────────────────────────────────────────────────

    def test_load_stats_missing_file_raises(self, tmp_path):
        from src.statschema.stats_io import load_stats
        with pytest.raises(FileNotFoundError):
            load_stats(str(tmp_path / "nonexistent.yaml"))

    def test_load_stats_json_path(self, tmp_path):
        import json as _json
        from src.statschema.stats_io import load_stats
        from src.statschema.stats_model import DatabaseStats
        data = DatabaseStats(tables=[]).to_dict()
        p = tmp_path / "stats.json"
        p.write_text(_json.dumps(data))
        db_stats = load_stats(str(p))
        assert isinstance(db_stats, DatabaseStats)

    def test_load_stats_invalid_content_raises(self, tmp_path):
        from src.statschema.stats_io import load_stats
        p = tmp_path / "bad.yaml"
        p.write_text("- list item\n")
        with pytest.raises(ValueError, match="YAML/JSON object"):
            load_stats(str(p))

    # ── make_default_stats ────────────────────────────────────────────────────

    def test_make_default_stats_unique_col(self):
        """Unique (non-PK) column gets n_distinct = row_count."""
        from src.statschema.stats_io import make_default_stats
        schema = self._make_schema(unique=True)
        db_stats = make_default_stats([schema], row_count=500)
        col_stat = db_stats.tables[0].columns[0]
        assert col_stat.n_distinct == 500.0

    def test_make_default_stats_fk_col(self):
        """FK-referencing column gets n_distinct = row_count * 0.1."""
        from src.statschema.stats_io import make_default_stats
        from src.statschema.model import CanonicalColumn, CanonicalTableSchema
        col = CanonicalColumn(name="parent_id", type="integer", references="parent.id")
        schema = CanonicalTableSchema(name="child", columns=[col])
        db_stats = make_default_stats([schema], row_count=1000)
        col_stat = db_stats.tables[0].columns[0]
        assert col_stat.n_distinct == 100.0

    def test_make_default_stats_legacy_fk_dict(self):
        """Foreign key expressed as legacy dict list is included in FK stats."""
        from src.statschema.stats_io import make_default_stats
        from src.statschema.model import CanonicalColumn, CanonicalTableSchema
        col = CanonicalColumn(name="ref_id", type="integer")
        schema = CanonicalTableSchema(
            name="child",
            columns=[col],
            foreign_keys=[{"column": "ref_id", "parent_table": "parent", "parent_column": "id"}],
        )
        db_stats = make_default_stats([schema], row_count=100)
        fk_stats = db_stats.tables[0].foreign_keys
        assert any(fk.columns == ["ref_id"] for fk in fk_stats)

    # ── apply_overrides ───────────────────────────────────────────────────────

    def test_apply_col_override_not_null_and_default(self):
        """ColumnOverride.not_null, .default, and spec.type_mappings are applied to schema."""
        from src.statschema.stats_io import apply_overrides
        from src.statschema.override_model import ColumnOverride, OverrideSpec, TableOverride
        schema = self._make_schema()
        spec = OverrideSpec(
            tables={"t": TableOverride(columns={"col": ColumnOverride(not_null=True, default="N/A")})},
            type_mappings={"string": "text"},
        )
        new_schema, _ = apply_overrides(schema, None, spec)
        col = new_schema.columns[0]
        assert col.not_null is True
        assert col.default == "N/A"
        assert col.type == "text"

    def test_apply_overrides_rename_schema_to(self):
        """TableOverride.rename_schema_to updates the stats schema field."""
        from src.statschema.stats_io import apply_overrides
        from src.statschema.override_model import OverrideSpec, TableOverride
        from src.statschema.stats_model import TableStats, ColumnStats
        schema = self._make_schema()
        ts = TableStats(name="t", row_count=100, columns=[
            ColumnStats(name="col", null_fraction=0.0, n_distinct=10)
        ])
        ts.schema = "old_schema"
        spec = OverrideSpec(tables={"t": TableOverride(rename_schema_to="new_schema")})
        _, new_stats = apply_overrides(schema, ts, spec)
        assert new_stats.schema == "new_schema"

    def test_apply_overrides_missing_col_in_stats_skipped(self):
        """When a renamed column has no matching ColumnStats entry, it is silently skipped."""
        from src.statschema.stats_io import apply_overrides
        from src.statschema.override_model import ColumnOverride, OverrideSpec, TableOverride
        from src.statschema.stats_model import TableStats, ColumnStats
        from src.statschema.model import CanonicalColumn, CanonicalTableSchema
        col_a = CanonicalColumn(name="col_a", type="string")
        col_b = CanonicalColumn(name="col_b", type="integer")
        schema = CanonicalTableSchema(name="t", columns=[col_a, col_b])
        ts = TableStats(name="t", row_count=100, columns=[
            ColumnStats(name="col_a", null_fraction=0.0, n_distinct=5),
            # col_b intentionally absent from stats
        ])
        # Rename col_b which is NOT in stats → stats entry should be skipped
        spec = OverrideSpec(tables={"t": TableOverride(
            columns={"col_b": ColumnOverride(rename_to="col_b_new")}
        )})
        _, new_stats = apply_overrides(schema, ts, spec)
        stat_names = [cs.name for cs in new_stats.columns]
        assert "col_b_new" not in stat_names

    def test_apply_overrides_index_fk_composite_rename(self):
        """Column renames propagate into stats.indexes, .foreign_keys, and .composite_stats."""
        from src.statschema.stats_io import apply_overrides
        from src.statschema.override_model import ColumnOverride, OverrideSpec, TableOverride
        from src.statschema.stats_model import (
            ColumnStats, CompositeColumnStats, ForeignKeyStats, IndexStats, TableStats
        )
        schema = self._make_schema()
        ts = TableStats(
            name="t",
            row_count=100,
            columns=[ColumnStats(name="col", null_fraction=0.0, n_distinct=5)],
            indexes=[IndexStats(name="idx", columns=["col"], unique=False)],
            foreign_keys=[ForeignKeyStats(name="fk", columns=["col"], parent_table="p", parent_columns=["id"])],
            composite_stats=[CompositeColumnStats(columns=["col"])],
        )
        spec = OverrideSpec(tables={"t": TableOverride(
            columns={"col": ColumnOverride(rename_to="col_renamed")}
        )})
        _, new_stats = apply_overrides(schema, ts, spec)
        assert new_stats.indexes[0].columns == ["col_renamed"]
        assert new_stats.foreign_keys[0].columns == ["col_renamed"]
        assert new_stats.composite_stats[0].columns == ["col_renamed"]
