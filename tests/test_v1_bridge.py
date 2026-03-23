"""
Tests for the dbldatagen.v1 bridge: CanonicalTableSchema → DataGenPlan.

Phases covered
--------------
A. Schema model sanity  — v1 Pydantic models import and work without Spark
B. Type mapping         — each canonical type maps to the correct v1 DataType
C. Generation strategy  — PK, FK, boolean, string, numeric, temporal, Faker, UUID
D. Distribution mapping — Zipf (native!), Normal, LogNormal, Exponential, Uniform
E. DDL → v1 plan        — end-to-end: MySQL / PostgreSQL / SQL Server DDL → DataGenPlan
F. YAML round-trip      — DataGenPlan serialises to YAML and back losslessly
G. Multi-table FK       — FK reference wired, topological order resolved
H. Name-mapper fallback — semantic inference from column names via v1 name_mapper
I. Row-count override   — custom row counts flow through correctly
"""

from __future__ import annotations

import json
import pytest

# ── guard: skip entire module if v1 not installed ──────────────────────────
pytest.importorskip("dbldatagen.v1", reason="dbldatagen.v1 not installed")

from dbldatagen.v1.schema import (
    ColumnSpec,
    DataGenPlan,
    DataType,
    FakerColumn,
    ForeignKeyRef,
    PatternColumn,
    RangeColumn,
    SequenceColumn,
    TableSpec,
    TimestampColumn,
    UUIDColumn,
    ValuesColumn,
    Zipf,
    Normal,
    LogNormal,
    Exponential,
)

from src.statschema import parse_ddl
from src.statschema.model import (
    CanonicalColumn,
    CanonicalForeignKey,
    CanonicalTableSchema,
    GenerationRule,
)
from src.statschema.v1_bridge import to_v1_plan


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────

def _col(name: str, type_: str, **kwargs) -> CanonicalColumn:
    return CanonicalColumn(name=name, type=type_, **kwargs)


def _table(name: str, cols: list[CanonicalColumn], **kwargs) -> CanonicalTableSchema:
    return CanonicalTableSchema(name=name, columns=cols, **kwargs)


def _find_col(plan: DataGenPlan, table_name: str, col_name: str) -> ColumnSpec:
    tbl = next(t for t in plan.tables if t.name == table_name)
    return next(c for c in tbl.columns if c.name == col_name)


# ───────────────────────────────────────────────────────────────────────────
# A. Schema model sanity
# ───────────────────────────────────────────────────────────────────────────

class TestV1SchemaSanity:
    def test_import_all_key_symbols(self):
        import dbldatagen.v1 as v1
        for sym in ["generate", "DataGenPlan", "TableSpec", "ColumnSpec",
                    "ForeignKeyRef", "PrimaryKey", "DataType",
                    "integer", "text", "fk", "pk_auto", "pk_uuid", "faker"]:
            assert hasattr(v1, sym), f"v1 missing symbol: {sym}"

    def test_datagenplan_pydantic_model(self):
        plan = DataGenPlan(
            tables=[TableSpec(name="t", columns=[
                ColumnSpec(name="id", dtype=DataType.LONG, gen=SequenceColumn())
            ], rows=10)]
        )
        assert plan.seed == 42
        assert plan.tables[0].name == "t"

    def test_datagenplan_json_serialisable(self):
        plan = DataGenPlan(
            tables=[TableSpec(name="t", columns=[
                ColumnSpec(name="id", dtype=DataType.LONG, gen=SequenceColumn())
            ], rows=100)]
        )
        raw = plan.model_dump()
        assert isinstance(raw, dict)
        assert raw["tables"][0]["name"] == "t"

    def test_datagenplan_round_trip_json(self):
        plan = DataGenPlan(tables=[
            TableSpec(name="x", columns=[
                ColumnSpec(name="id", dtype=DataType.LONG, gen=SequenceColumn()),
                ColumnSpec(name="val", dtype=DataType.INT,
                           gen=RangeColumn(min=0, max=100)),
            ], rows=50)
        ])
        json_str = plan.model_dump_json()
        plan2 = DataGenPlan.model_validate_json(json_str)
        assert plan2.tables[0].columns[1].name == "val"

    def test_zipf_is_native_not_approximation(self):
        """v1 has native Zipf — no Gamma approximation needed."""
        z = Zipf(exponent=1.5)
        assert z.type == "zipf"
        assert z.exponent == 1.5

    def test_faker_column_has_locale(self):
        fc = FakerColumn(provider="email", locale="de_DE")
        assert fc.provider == "email"
        assert fc.locale == "de_DE"


# ───────────────────────────────────────────────────────────────────────────
# B. Type mapping: canonical type → v1 DataType
# ───────────────────────────────────────────────────────────────────────────

class TestTypeMappingToV1:
    @pytest.mark.parametrize("ctype,expected_dtype", [
        ("integer",    DataType.INT),
        ("long",       DataType.LONG),
        ("float",      DataType.FLOAT),
        ("double",     DataType.DOUBLE),
        ("decimal",    DataType.DECIMAL),
        ("string",     DataType.STRING),
        ("boolean",    DataType.BOOLEAN),
        ("date",       DataType.DATE),
        ("timestamp",  DataType.TIMESTAMP),
        ("timestamptz", DataType.TIMESTAMP),
        ("time",       DataType.STRING),   # no TIME in v1 DataType
        ("binary",     DataType.STRING),   # no BinaryType in v1 DataType enum
    ])
    def test_canonical_type_maps_to_v1_dtype(self, ctype, expected_dtype):
        tbl = _table("t", [_col("c", ctype)])
        plan = to_v1_plan(tbl)
        cs = _find_col(plan, "t", "c")
        assert cs.dtype == expected_dtype, (
            f"canonical '{ctype}' should map to {expected_dtype}, got {cs.dtype}")

    def test_decimal_with_precision_scale(self):
        tbl = _table("t", [_col("price", "decimal", precision=19, scale=4)])
        plan = to_v1_plan(tbl)
        cs = _find_col(plan, "t", "price")
        assert cs.dtype == DataType.DECIMAL
        assert isinstance(cs.gen, RangeColumn)

    def test_long_range_bounds(self):
        tbl = _table("t", [_col("big", "long")])
        plan = to_v1_plan(tbl)
        cs = _find_col(plan, "t", "big")
        assert isinstance(cs.gen, RangeColumn)
        assert cs.gen.min == -(2**63)
        assert cs.gen.max == (2**63 - 1)


# ───────────────────────────────────────────────────────────────────────────
# C. Generation strategy
# ───────────────────────────────────────────────────────────────────────────

class TestGenerationStrategy:
    def test_pk_auto_increment_becomes_sequence(self):
        tbl = _table("t", [_col("id", "long", primary_key=True, auto_increment=True)])
        plan = to_v1_plan(tbl)
        cs = _find_col(plan, "t", "id")
        assert isinstance(cs.gen, SequenceColumn)

    def test_pk_string_becomes_uuid(self):
        tbl = _table("t", [_col("id", "string", primary_key=True)])
        plan = to_v1_plan(tbl)
        cs = _find_col(plan, "t", "id")
        assert isinstance(cs.gen, UUIDColumn)

    def test_boolean_becomes_values_true_false(self):
        tbl = _table("t", [_col("active", "boolean")])
        plan = to_v1_plan(tbl)
        cs = _find_col(plan, "t", "active")
        assert isinstance(cs.gen, ValuesColumn)
        assert set(cs.gen.values) == {True, False}

    def test_explicit_values_list(self):
        gen = GenerationRule(values=["A", "B", "C"])
        tbl = _table("t", [_col("status", "string", generation=gen)])
        plan = to_v1_plan(tbl)
        cs = _find_col(plan, "t", "status")
        assert isinstance(cs.gen, ValuesColumn)
        assert cs.gen.values == ["A", "B", "C"]

    def test_timestamp_becomes_timestamp_col(self):
        tbl = _table("t", [_col("created_at", "timestamp")])
        plan = to_v1_plan(tbl)
        cs = _find_col(plan, "t", "created_at")
        assert isinstance(cs.gen, TimestampColumn)

    def test_timestamptz_becomes_timestamp_col(self):
        tbl = _table("t", [_col("updated_at", "timestamptz")])
        plan = to_v1_plan(tbl)
        cs = _find_col(plan, "t", "updated_at")
        assert isinstance(cs.gen, TimestampColumn)

    def test_date_becomes_timestamp_col(self):
        tbl = _table("t", [_col("dob", "date")])
        plan = to_v1_plan(tbl)
        cs = _find_col(plan, "t", "dob")
        assert isinstance(cs.gen, TimestampColumn)

    def test_format_pattern_email_becomes_faker(self):
        gen = GenerationRule(format_pattern="email")
        tbl = _table("t", [_col("email", "string", generation=gen)])
        plan = to_v1_plan(tbl)
        cs = _find_col(plan, "t", "email")
        assert isinstance(cs.gen, FakerColumn)
        assert cs.gen.provider == "email"

    def test_format_pattern_phone_becomes_faker(self):
        gen = GenerationRule(format_pattern="phone_us")
        tbl = _table("t", [_col("phone", "string", generation=gen)])
        plan = to_v1_plan(tbl)
        cs = _find_col(plan, "t", "phone")
        assert isinstance(cs.gen, FakerColumn)
        assert cs.gen.provider == "phone_number"

    def test_format_pattern_uuid_becomes_uuid_col(self):
        gen = GenerationRule(format_pattern="uuid")
        tbl = _table("t", [_col("ext_id", "string", generation=gen)])
        plan = to_v1_plan(tbl)
        cs = _find_col(plan, "t", "ext_id")
        assert isinstance(cs.gen, UUIDColumn)

    def test_unknown_format_pattern_passes_through_as_pattern(self):
        gen = GenerationRule(format_pattern="ORDER-{digit:4}")
        tbl = _table("t", [_col("order_ref", "string", generation=gen)])
        plan = to_v1_plan(tbl)
        cs = _find_col(plan, "t", "order_ref")
        assert isinstance(cs.gen, PatternColumn)
        assert cs.gen.template == "ORDER-{digit:4}"

    def test_nullable_column_flag(self):
        tbl = _table("t", [_col("opt", "string")])  # not_null defaults False
        plan = to_v1_plan(tbl)
        cs = _find_col(plan, "t", "opt")
        assert cs.nullable is True

    def test_not_null_column_flag(self):
        tbl = _table("t", [_col("req", "string", not_null=True)])
        plan = to_v1_plan(tbl)
        cs = _find_col(plan, "t", "req")
        assert cs.nullable is False

    def test_min_max_on_numeric(self):
        gen = GenerationRule(min_value=0, max_value=999)
        tbl = _table("t", [_col("score", "integer", generation=gen)])
        plan = to_v1_plan(tbl)
        cs = _find_col(plan, "t", "score")
        assert isinstance(cs.gen, RangeColumn)
        assert cs.gen.min == 0
        assert cs.gen.max == 999


# ───────────────────────────────────────────────────────────────────────────
# D. Distribution mapping
# ───────────────────────────────────────────────────────────────────────────

class TestDistributionMapping:
    def test_zipf_native_not_gamma(self):
        """v1 fills gap: Zipf is native, not a Gamma approximation."""
        gen = GenerationRule(distribution="zipf",
                             distribution_params={"exponent": 1.8})
        tbl = _table("t", [_col("cat_id", "integer", generation=gen)])
        plan = to_v1_plan(tbl)
        cs = _find_col(plan, "t", "cat_id")
        assert isinstance(cs.gen, RangeColumn)
        dist = cs.gen.distribution
        assert isinstance(dist, Zipf)
        assert dist.exponent == 1.8

    def test_normal_distribution(self):
        gen = GenerationRule(distribution="normal",
                             distribution_params={"mean": 50.0, "stddev": 10.0})
        tbl = _table("t", [_col("score", "float", generation=gen)])
        plan = to_v1_plan(tbl)
        cs = _find_col(plan, "t", "score")
        dist = cs.gen.distribution
        assert isinstance(dist, Normal)
        assert dist.mean == 50.0
        assert dist.stddev == 10.0

    def test_lognormal_distribution(self):
        gen = GenerationRule(distribution="lognormal",
                             distribution_params={"mean": 1.0, "stddev": 0.5})
        tbl = _table("t", [_col("salary", "double", generation=gen)])
        plan = to_v1_plan(tbl)
        cs = _find_col(plan, "t", "salary")
        assert isinstance(cs.gen.distribution, LogNormal)

    def test_exponential_distribution(self):
        gen = GenerationRule(distribution="exponential",
                             distribution_params={"rate": 0.1})
        tbl = _table("t", [_col("wait_ms", "long", generation=gen)])
        plan = to_v1_plan(tbl)
        cs = _find_col(plan, "t", "wait_ms")
        assert isinstance(cs.gen.distribution, Exponential)
        assert cs.gen.distribution.rate == 0.1

    def test_uniform_is_default(self):
        from dbldatagen.v1.schema import Uniform
        tbl = _table("t", [_col("amount", "decimal", precision=10, scale=2)])
        plan = to_v1_plan(tbl)
        cs = _find_col(plan, "t", "amount")
        assert isinstance(cs.gen.distribution, Uniform)

    def test_power_alias_maps_to_zipf(self):
        gen = GenerationRule(distribution="power",
                             distribution_params={"exponent": 2.0})
        tbl = _table("t", [_col("rank", "integer", generation=gen)])
        plan = to_v1_plan(tbl)
        cs = _find_col(plan, "t", "rank")
        assert isinstance(cs.gen.distribution, Zipf)
        assert cs.gen.distribution.exponent == 2.0


# ───────────────────────────────────────────────────────────────────────────
# E. End-to-end: DDL → parse → to_v1_plan
# ───────────────────────────────────────────────────────────────────────────

class TestDDLToV1Plan:
    MYSQL_DDL = """
    CREATE TABLE orders (
        order_id    BIGINT        NOT NULL AUTO_INCREMENT PRIMARY KEY,
        customer_id BIGINT        NOT NULL,
        status      VARCHAR(20)   NOT NULL DEFAULT 'pending',
        amount      DECIMAL(19,4) NOT NULL,
        created_at  TIMESTAMP     NOT NULL,
        notes       TEXT
    );
    """

    PG_DDL = """
    CREATE TABLE products (
        product_id  SERIAL PRIMARY KEY,
        name        VARCHAR(200) NOT NULL,
        price       NUMERIC(10,2) NOT NULL,
        in_stock    BOOLEAN NOT NULL DEFAULT TRUE,
        created_at  TIMESTAMPTZ,
        weight_kg   DOUBLE PRECISION
    );
    """

    SQLSERVER_DDL = """
    CREATE TABLE employees (
        employee_id INT          NOT NULL IDENTITY(1,1) PRIMARY KEY,
        email       NVARCHAR(255) NOT NULL,
        salary      MONEY        NOT NULL,
        hire_date   DATE,
        is_active   BIT          NOT NULL DEFAULT 1,
        dept_code   VARCHAR(10)
    );
    """

    def test_mysql_ddl_produces_valid_plan(self):
        tables = parse_ddl(self.MYSQL_DDL, dialect="mysql")
        plan = to_v1_plan(tables[0], row_counts={"orders": 5_000})
        assert plan.tables[0].name == "orders"
        assert plan.tables[0].rows == 5_000

    def test_mysql_pk_is_sequence(self):
        tables = parse_ddl(self.MYSQL_DDL, dialect="mysql")
        plan = to_v1_plan(tables[0])
        cs = _find_col(plan, "orders", "order_id")
        assert isinstance(cs.gen, SequenceColumn)

    def test_mysql_decimal_col(self):
        tables = parse_ddl(self.MYSQL_DDL, dialect="mysql")
        plan = to_v1_plan(tables[0])
        cs = _find_col(plan, "orders", "amount")
        assert cs.dtype == DataType.DECIMAL
        assert isinstance(cs.gen, RangeColumn)

    def test_mysql_timestamp_col(self):
        tables = parse_ddl(self.MYSQL_DDL, dialect="mysql")
        plan = to_v1_plan(tables[0])
        cs = _find_col(plan, "orders", "created_at")
        assert isinstance(cs.gen, TimestampColumn)

    def test_pg_ddl_produces_valid_plan(self):
        tables = parse_ddl(self.PG_DDL, dialect="postgres")
        plan = to_v1_plan(tables[0])
        assert plan.tables[0].name == "products"

    def test_pg_boolean_col(self):
        tables = parse_ddl(self.PG_DDL, dialect="postgres")
        plan = to_v1_plan(tables[0])
        cs = _find_col(plan, "products", "in_stock")
        assert isinstance(cs.gen, ValuesColumn)
        assert set(cs.gen.values) == {True, False}

    def test_pg_double_precision_col(self):
        tables = parse_ddl(self.PG_DDL, dialect="postgres")
        plan = to_v1_plan(tables[0])
        cs = _find_col(plan, "products", "weight_kg")
        assert cs.dtype == DataType.DOUBLE

    def test_sqlserver_money_maps_to_decimal(self):
        tables = parse_ddl(self.SQLSERVER_DDL, dialect="sqlserver")
        plan = to_v1_plan(tables[0])
        cs = _find_col(plan, "employees", "salary")
        assert cs.dtype == DataType.DECIMAL

    def test_sqlserver_bit_maps_to_boolean(self):
        tables = parse_ddl(self.SQLSERVER_DDL, dialect="sqlserver")
        plan = to_v1_plan(tables[0])
        cs = _find_col(plan, "employees", "is_active")
        assert cs.dtype == DataType.BOOLEAN

    def test_sqlserver_date_maps_to_date(self):
        tables = parse_ddl(self.SQLSERVER_DDL, dialect="sqlserver")
        plan = to_v1_plan(tables[0])
        cs = _find_col(plan, "employees", "hire_date")
        assert cs.dtype == DataType.DATE
        assert isinstance(cs.gen, TimestampColumn)

    def test_plan_has_primary_key(self):
        tables = parse_ddl(self.MYSQL_DDL, dialect="mysql")
        plan = to_v1_plan(tables[0])
        assert plan.tables[0].primary_key is not None
        assert "order_id" in plan.tables[0].primary_key.columns

    def test_all_columns_preserved(self):
        tables = parse_ddl(self.MYSQL_DDL, dialect="mysql")
        plan = to_v1_plan(tables[0])
        col_names = {c.name for c in plan.tables[0].columns}
        assert col_names == {"order_id", "customer_id", "status",
                             "amount", "created_at", "notes"}


# ───────────────────────────────────────────────────────────────────────────
# F. YAML round-trip (v1 DataGenPlan → YAML → DataGenPlan)
# ───────────────────────────────────────────────────────────────────────────

class TestV1YamlRoundTrip:
    def test_plan_dumps_to_yaml(self):
        import yaml
        tables = parse_ddl(TestDDLToV1Plan.MYSQL_DDL, dialect="mysql")
        plan = to_v1_plan(tables[0])
        raw = plan.model_dump(mode="json")  # Enums as strings → safe YAML
        yaml_str = yaml.dump(raw, sort_keys=False, allow_unicode=True)
        assert "orders" in yaml_str
        assert "order_id" in yaml_str

    def test_plan_loads_from_yaml(self):
        import yaml
        tables = parse_ddl(TestDDLToV1Plan.MYSQL_DDL, dialect="mysql")
        plan = to_v1_plan(tables[0])
        # mode="json" serialises Enums as plain strings → safe for yaml.safe_load
        raw = plan.model_dump(mode="json")
        yaml_str = yaml.dump(raw, sort_keys=False, allow_unicode=True)
        loaded_raw = yaml.safe_load(yaml_str)
        plan2 = DataGenPlan.model_validate(loaded_raw)
        assert plan2.tables[0].name == "orders"

    def test_yaml_round_trip_preserves_row_count(self):
        import yaml
        tables = parse_ddl(TestDDLToV1Plan.PG_DDL, dialect="postgres")
        plan = to_v1_plan(tables[0], row_counts={"products": 7_500})
        raw = plan.model_dump(mode="json")
        yaml_str = yaml.dump(raw, sort_keys=False, allow_unicode=True)
        plan2 = DataGenPlan.model_validate(yaml.safe_load(yaml_str))
        assert plan2.tables[0].rows == 7_500

    def test_yaml_round_trip_preserves_zipf(self):
        import yaml
        gen = GenerationRule(distribution="zipf",
                             distribution_params={"exponent": 1.3})
        tbl = _table("t", [
            _col("id", "long", primary_key=True, auto_increment=True),
            _col("cat", "integer", generation=gen),
        ])
        plan = to_v1_plan(tbl)
        # mode="json" avoids !!python YAML tags on Enum values
        raw = plan.model_dump(mode="json")
        plan2 = DataGenPlan.model_validate(yaml.safe_load(
            yaml.dump(raw, sort_keys=False, allow_unicode=True)))
        col = _find_col(plan2, "t", "cat")
        assert isinstance(col.gen.distribution, Zipf)
        assert col.gen.distribution.exponent == 1.3

    def test_yaml_round_trip_preserves_faker(self):
        import yaml
        gen = GenerationRule(format_pattern="email")
        tbl = _table("t", [
            _col("id", "long", primary_key=True, auto_increment=True),
            _col("email_addr", "string", generation=gen),
        ])
        plan = to_v1_plan(tbl)
        raw = plan.model_dump(mode="json")
        plan2 = DataGenPlan.model_validate(
            yaml.safe_load(yaml.dump(raw, sort_keys=False, allow_unicode=True))
        )
        col = _find_col(plan2, "t", "email_addr")
        assert isinstance(col.gen, FakerColumn)
        assert col.gen.provider == "email"

    def test_json_round_trip(self):
        tables = parse_ddl(TestDDLToV1Plan.SQLSERVER_DDL, dialect="sqlserver")
        plan = to_v1_plan(tables[0])
        json_str = plan.model_dump_json()
        plan2 = DataGenPlan.model_validate_json(json_str)
        assert len(plan2.tables[0].columns) == len(plan.tables[0].columns)


# ───────────────────────────────────────────────────────────────────────────
# G. Multi-table FK wiring
# ───────────────────────────────────────────────────────────────────────────

class TestMultiTableFK:
    PARENT_DDL = """
    CREATE TABLE customers (
        customer_id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        name        VARCHAR(200) NOT NULL,
        email       VARCHAR(255) NOT NULL
    );
    """
    CHILD_DDL = """
    CREATE TABLE orders (
        order_id    BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        customer_id BIGINT NOT NULL,
        total       DECIMAL(12,2) NOT NULL,
        created_at  TIMESTAMP NOT NULL
    );
    """

    def _build_parent(self):
        tables = parse_ddl(self.PARENT_DDL, dialect="mysql")
        return tables[0]

    def _build_child_with_fk(self):
        tables = parse_ddl(self.CHILD_DDL, dialect="mysql")
        child = tables[0]
        child.foreign_keys = [
            {"from_col": "customer_id",
             "to_table": "customers",
             "to_col": "customer_id"}
        ]
        return child

    def test_fk_column_gets_foreign_key_ref(self):
        child = self._build_child_with_fk()
        plan = to_v1_plan(child)
        cs = _find_col(plan, "orders", "customer_id")
        assert cs.foreign_key is not None
        assert cs.foreign_key.ref == "customers.customer_id"

    def test_fk_distribution_is_zipf(self):
        child = self._build_child_with_fk()
        plan = to_v1_plan(child)
        cs = _find_col(plan, "orders", "customer_id")
        assert isinstance(cs.foreign_key.distribution, Zipf)

    def test_multi_table_plan(self):
        parent = self._build_parent()
        child = self._build_child_with_fk()
        plan = to_v1_plan(
            [parent, child],
            row_counts={"customers": 1_000, "orders": 5_000}
        )
        assert len(plan.tables) == 2
        names = {t.name for t in plan.tables}
        assert names == {"customers", "orders"}

    def test_multi_table_row_counts(self):
        parent = self._build_parent()
        child = self._build_child_with_fk()
        plan = to_v1_plan(
            [parent, child],
            row_counts={"customers": 1_000, "orders": 5_000}
        )
        counts = {t.name: t.rows for t in plan.tables}
        assert counts["customers"] == 1_000
        assert counts["orders"] == 5_000

    def test_v1_resolve_plan_handles_fk_order(self):
        """v1 planner resolves generation order — parents before children."""
        from dbldatagen.v1.engine.planner import resolve_plan
        parent = self._build_parent()
        child = self._build_child_with_fk()
        plan = to_v1_plan([parent, child],
                          row_counts={"customers": 100, "orders": 500})
        resolved = resolve_plan(plan)
        order = resolved.generation_order
        assert order.index("customers") < order.index("orders")


# ───────────────────────────────────────────────────────────────────────────
# H. Semantic name-mapper fallback
# ───────────────────────────────────────────────────────────────────────────

class TestNameMapperFallback:
    """v1's name_mapper infers generation strategy from column names — fills gap
    in our v0 builder which required explicit GenerationRule.format_pattern."""

    @pytest.mark.parametrize("col_name,expected_gen_type", [
        ("email",       FakerColumn),
        ("first_name",  FakerColumn),
        ("last_name",   FakerColumn),
        ("phone",       FakerColumn),
        ("city",        FakerColumn),
        ("created_at",  TimestampColumn),
        ("updated_at",  TimestampColumn),
        ("is_active",   ValuesColumn),   # boolean flags
        ("status",      ValuesColumn),
        ("description", FakerColumn),    # paragraph
    ])
    def test_name_mapper_infers_strategy(self, col_name, expected_gen_type):
        from dbldatagen.v1.connectors.sql.name_mapper import map_column_name
        spec = map_column_name(col_name)
        assert isinstance(spec.gen, expected_gen_type), (
            f"'{col_name}': expected {expected_gen_type.__name__}, "
            f"got {type(spec.gen).__name__}"
        )

    def test_bridge_uses_name_mapper_for_string_without_format_pattern(self):
        """Columns without explicit format_pattern fall back to name_mapper."""
        tbl = _table("t", [
            _col("id", "long", primary_key=True, auto_increment=True),
            _col("email", "string"),
            _col("created_at", "timestamp"),
        ])
        plan = to_v1_plan(tbl)
        email_col = _find_col(plan, "t", "email")
        # Should use FakerColumn (from name_mapper) not PatternColumn
        assert isinstance(email_col.gen, FakerColumn)

    def test_unknown_column_name_gets_pattern_fallback(self):
        tbl = _table("t", [_col("xyzzy_code", "string")])
        plan = to_v1_plan(tbl)
        cs = _find_col(plan, "t", "xyzzy_code")
        # Should be PatternColumn with {digit:6} fallback from name_mapper
        assert isinstance(cs.gen, PatternColumn)


# ───────────────────────────────────────────────────────────────────────────
# I. Row-count overrides
# ───────────────────────────────────────────────────────────────────────────

class TestRowCountOverrides:
    def test_default_row_count_is_1000(self):
        tbl = _table("t", [_col("id", "long", primary_key=True)])
        plan = to_v1_plan(tbl)
        assert plan.tables[0].rows == 1_000

    def test_override_row_count(self):
        tbl = _table("t", [_col("id", "long", primary_key=True)])
        plan = to_v1_plan(tbl, row_counts={"t": 50_000})
        assert plan.tables[0].rows == 50_000

    def test_human_readable_row_count(self):
        """v1 supports '10M' style row counts."""
        plan = DataGenPlan(tables=[
            TableSpec(name="big", columns=[
                ColumnSpec(name="id", dtype=DataType.LONG, gen=SequenceColumn())
            ], rows="10M")
        ])
        assert plan.tables[0].rows == 10_000_000

    def test_per_table_row_counts_in_multi_table(self):
        t1 = _table("a", [_col("id", "long", primary_key=True)])
        t2 = _table("b", [_col("id", "long", primary_key=True)])
        plan = to_v1_plan([t1, t2],
                          row_counts={"a": 100, "b": 999})
        counts = {t.name: t.rows for t in plan.tables}
        assert counts["a"] == 100
        assert counts["b"] == 999

    def test_missing_table_gets_default(self):
        t1 = _table("a", [_col("id", "long", primary_key=True)])
        t2 = _table("b", [_col("id", "long", primary_key=True)])
        plan = to_v1_plan([t1, t2], row_counts={"a": 500})
        counts = {t.name: t.rows for t in plan.tables}
        assert counts["a"] == 500
        assert counts["b"] == 1_000  # default


# ───────────────────────────────────────────────────────────────────────────
# J. Full headline: DDL → YAML (our format) → v1 DataGenPlan → JSON
# ───────────────────────────────────────────────────────────────────────────

class TestFullPipeline:
    """End-to-end test: many DDL sources → canonical YAML → v1 DataGenPlan."""

    MYSQL_CUSTOMERS = """
    CREATE TABLE customers (
        customer_id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        email       VARCHAR(255) NOT NULL,
        created_at  TIMESTAMP NOT NULL,
        country     VARCHAR(2) NOT NULL DEFAULT 'US'
    );
    """
    PG_PRODUCTS = """
    CREATE TABLE products (
        product_id  SERIAL PRIMARY KEY,
        name        VARCHAR(200) NOT NULL,
        price       NUMERIC(10,2) NOT NULL,
        in_stock    BOOLEAN NOT NULL DEFAULT TRUE
    );
    """
    SS_ORDERS = """
    CREATE TABLE orders (
        order_id    INT NOT NULL IDENTITY(1,1) PRIMARY KEY,
        total       MONEY NOT NULL,
        order_date  DATE NOT NULL,
        is_shipped  BIT NOT NULL DEFAULT 0
    );
    """

    def test_multi_source_ddl_to_single_v1_plan(self):
        from src.statschema import dump_schema, load_canonical

        # 1. Parse three DDLs from different dialects
        customers = parse_ddl(self.MYSQL_CUSTOMERS, dialect="mysql")[0]
        products  = parse_ddl(self.PG_PRODUCTS,    dialect="postgres")[0]
        orders    = parse_ddl(self.SS_ORDERS,       dialect="sqlserver")[0]

        # 2. Dump to canonical YAML (our interchange format)
        yaml_str = dump_schema([customers, products, orders])
        assert "customers" in yaml_str
        assert "products" in yaml_str
        assert "orders" in yaml_str

        # 3. Load back from canonical YAML
        loaded = load_canonical(yaml_str)
        assert len(loaded) == 3

        # 4. Convert to v1 DataGenPlan
        plan = to_v1_plan(
            loaded,
            row_counts={"customers": 1_000, "products": 500, "orders": 5_000}
        )
        assert len(plan.tables) == 3

        # 5. Confirm plan is JSON/YAML serialisable
        json_str = plan.model_dump_json()
        plan2 = DataGenPlan.model_validate_json(json_str)
        names = {t.name for t in plan2.tables}
        assert names == {"customers", "products", "orders"}

    def test_precision_survives_yaml_to_v1_plan(self):
        from src.statschema import dump_schema, load_canonical

        ddl = """
        CREATE TABLE payments (
            payment_id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            amount     DECIMAL(19,4) NOT NULL,
            fee        DECIMAL(10,6) NOT NULL
        );
        """
        schema = parse_ddl(ddl, dialect="mysql")[0]
        yaml_str = dump_schema(schema)
        loaded = load_canonical(yaml_str)[0]

        plan = to_v1_plan(loaded)
        amount_col = _find_col(plan, "payments", "amount")
        fee_col    = _find_col(plan, "payments", "fee")

        assert amount_col.dtype == DataType.DECIMAL
        assert fee_col.dtype == DataType.DECIMAL
        # Bounds should reflect p=19,s=4 → max_int = 10^15 - 1
        assert amount_col.gen.max == 10 ** (19 - 4) - 1

    def test_v1_plan_seed_propagated(self):
        tbl = _table("t", [_col("id", "long", primary_key=True)])
        plan = to_v1_plan(tbl, seed=12345)
        assert plan.seed == 12345
        # Table seed derived deterministically from global seed
        assert plan.tables[0].seed == 12345  # first table: seed + 0


# ───────────────────────────────────────────────────────────────────────────
# J. FK distribution control
# ───────────────────────────────────────────────────────────────────────────

class TestFKDistribution:
    """
    CanonicalForeignKey distribution fields → ForeignKeyRef distribution object.

    Covers:
    - Model round-trip (to_dict / from_dict)
    - Default serialisation is compact (zipf omitted)
    - All supported distributions flow through _fk_v1_distribution
    - fk_constraints wiring in to_v1_plan (preferred over legacy foreign_keys)
    - Legacy foreign_keys dict still defaults to Zipf (backward compat)
    - Composite FK aligns child→parent column by index
    """

    ORDERS_DDL = """
    CREATE TABLE orders (
        order_id    BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        customer_id BIGINT NOT NULL,
        total       DECIMAL(12,2) NOT NULL
    );
    """

    def _orders_with_fk(self, **fk_kwargs) -> CanonicalForeignKey:
        return CanonicalForeignKey(
            columns=["customer_id"],
            parent_table="customers",
            parent_columns=["customer_id"],
            **fk_kwargs,
        )

    def _orders_table(self, fk: CanonicalForeignKey):
        from src.statschema import parse_ddl
        table = parse_ddl(self.ORDERS_DDL, dialect="mysql")[0]
        table.fk_constraints = [fk]
        return table

    # ── _fk_v1_distribution → correct v1 object ───────────────────────────

    @pytest.mark.parametrize("dist,params,expected_type,check", [
        ("zipf",        {},                    Zipf,        lambda d: d.exponent == 1.2),
        ("zipf",        {"exponent": 2.5},     Zipf,        lambda d: d.exponent == 2.5),
        ("uniform",     {},                    None,        None),   # Uniform has no attrs to check
        ("exponential", {"rate": 0.5},         None,        None),
    ])
    def test_fk_v1_distribution(self, dist, params, expected_type, check):
        from src.statschema.v1_bridge import _fk_v1_distribution
        fk = self._orders_with_fk(fk_distribution=dist, fk_distribution_params=params)
        dist_obj = _fk_v1_distribution(fk)
        if expected_type is not None:
            assert isinstance(dist_obj, expected_type)
        if check:
            assert check(dist_obj)

    def test_unknown_distribution_defaults_to_zipf(self):
        from src.statschema.v1_bridge import _fk_v1_distribution
        fk = self._orders_with_fk(fk_distribution="invented_dist")
        assert isinstance(_fk_v1_distribution(fk), Zipf)

    # ── fk_constraints wired in to_v1_plan ────────────────────────────────

    def test_fk_constraints_wired(self):
        """Structured fk_constraints produce a ForeignKeyRef."""
        table = self._orders_table(self._orders_with_fk())
        plan = to_v1_plan(table)
        cs = _find_col(plan, "orders", "customer_id")
        assert cs.foreign_key is not None
        assert cs.foreign_key.ref == "customers.customer_id"

    def test_fk_constraints_uniform_distribution(self):
        table = self._orders_table(self._orders_with_fk(fk_distribution="uniform"))
        plan = to_v1_plan(table)
        cs = _find_col(plan, "orders", "customer_id")
        assert not isinstance(cs.foreign_key.distribution, Zipf)

    def test_fk_constraints_zipf_exponent(self):
        fk = self._orders_with_fk(
            fk_distribution="zipf",
            fk_distribution_params={"exponent": 2.0},
        )
        table = self._orders_table(fk)
        plan = to_v1_plan(table)
        cs = _find_col(plan, "orders", "customer_id")
        assert isinstance(cs.foreign_key.distribution, Zipf)
        assert cs.foreign_key.distribution.exponent == 2.0

    def test_fk_constraints_preferred_over_legacy(self):
        """When both fk_constraints and legacy foreign_keys are present, structured wins."""
        from src.statschema import parse_ddl
        table = parse_ddl(self.ORDERS_DDL, dialect="mysql")[0]
        table.fk_constraints = [
            self._orders_with_fk(fk_distribution="uniform")
        ]
        table.foreign_keys = [
            {"from_col": "customer_id", "to_table": "customers", "to_col": "customer_id"}
        ]
        plan = to_v1_plan(table)
        cs = _find_col(plan, "orders", "customer_id")
        assert not isinstance(cs.foreign_key.distribution, Zipf)  # structured wins

    def test_legacy_fk_dict_defaults_to_zipf(self):
        """Backward compat: legacy foreign_keys dict still gets Zipf(1.2)."""
        from src.statschema import parse_ddl
        table = parse_ddl(self.ORDERS_DDL, dialect="mysql")[0]
        table.foreign_keys = [
            {"from_col": "customer_id", "to_table": "customers", "to_col": "customer_id"}
        ]
        plan = to_v1_plan(table)
        cs = _find_col(plan, "orders", "customer_id")
        assert isinstance(cs.foreign_key.distribution, Zipf)

    def test_composite_fk_column_alignment(self):
        """Composite FK aligns child columns to parent columns by position."""
        from src.statschema import parse_ddl
        ddl = """
        CREATE TABLE order_lines (
            order_id  BIGINT NOT NULL,
            line_no   INT NOT NULL,
            qty       INT NOT NULL,
            PRIMARY KEY (order_id, line_no)
        );
        """
        table = parse_ddl(ddl, dialect="mysql")[0]
        table.fk_constraints = [
            CanonicalForeignKey(
                columns=["order_id", "line_no"],
                parent_table="orders",
                parent_columns=["order_id", "line_no"],
            )
        ]
        plan = to_v1_plan(table)
        oid = _find_col(plan, "order_lines", "order_id")
        lno = _find_col(plan, "order_lines", "line_no")
        assert oid.foreign_key.ref == "orders.order_id"
        assert lno.foreign_key.ref == "orders.line_no"


# ───────────────────────────────────────────────────────────────────────────
# K. ColumnStats wiring — null_fraction, MCV weights, numeric min/max
# ───────────────────────────────────────────────────────────────────────────

class TestStatsWiring:
    """
    Verify that to_v1_plan(stats=...) correctly drives:
      - null_fraction  →  ColumnSpec.null_fraction
      - MCV weights    →  ValuesColumn + WeightedValues
      - numeric min/max →  RangeColumn.min / RangeColumn.max
      - date begin/end  →  TimestampColumn.start / .end
    """

    from dbldatagen.v1.schema import WeightedValues
    from src.statschema.stats_model import ColumnStats, MostCommonValue, TableStats

    DDL = """
    CREATE TABLE products (
        id       BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        price    DECIMAL(10,2) NOT NULL,
        region   VARCHAR(20) NOT NULL,
        created  DATE NOT NULL
    );
    """

    def _tbl(self):
        from src.statschema import parse_ddl
        return parse_ddl(self.DDL, dialect="mysql")[0]

    def _col_stats(self, name, **kwargs):
        from src.statschema.stats_model import ColumnStats
        return ColumnStats(name=name, **kwargs)

    def _mcv(self, value, frequency):
        from src.statschema.stats_model import MostCommonValue
        return MostCommonValue(value=value, frequency=frequency)

    def _make_table_stats(self, col_stats_list):
        from src.statschema.stats_model import TableStats
        return TableStats(name="products", row_count=10_000, columns=list(col_stats_list))

    # ── null_fraction ─────────────────────────────────────────────────────

    def test_null_fraction_applied(self):
        tbl = self._tbl()
        ts = self._make_table_stats([
            self._col_stats("region", null_fraction=0.05),
        ])
        plan = to_v1_plan(tbl, stats=ts)
        cs = _find_col(plan, "products", "region")
        assert cs.null_fraction == pytest.approx(0.05)

    def test_null_fraction_zero_not_applied(self):
        tbl = self._tbl()
        ts = self._make_table_stats([
            self._col_stats("region", null_fraction=0.0),
        ])
        plan = to_v1_plan(tbl, stats=ts)
        cs = _find_col(plan, "products", "region")
        assert cs.null_fraction == 0.0

    # ── MCV weights ───────────────────────────────────────────────────────

    def test_dominant_mcv_becomes_values_column(self):
        tbl = self._tbl()
        ts = self._make_table_stats([
            self._col_stats("region", n_distinct=3.0, most_common_values=[
                self._mcv("US", 0.60),
                self._mcv("EU", 0.30),
                self._mcv("APAC", 0.10),
            ]),
        ])
        plan = to_v1_plan(tbl, stats=ts)
        cs = _find_col(plan, "products", "region")
        assert isinstance(cs.gen, ValuesColumn)

    def test_mcv_weights_are_weighted_values(self):
        from dbldatagen.v1.schema import WeightedValues
        tbl = self._tbl()
        ts = self._make_table_stats([
            self._col_stats("region", n_distinct=3.0, most_common_values=[
                self._mcv("US", 0.60),
                self._mcv("EU", 0.40),
            ]),
        ])
        plan = to_v1_plan(tbl, stats=ts)
        cs = _find_col(plan, "products", "region")
        assert isinstance(cs.gen.distribution, WeightedValues)

    def test_sparse_mcv_not_applied(self):
        """Low-frequency MCVs on high-cardinality columns should NOT override generation."""
        tbl = self._tbl()
        ts = self._make_table_stats([
            self._col_stats("price", n_distinct=9000.0, most_common_values=[
                self._mcv("9.99", 0.02),
                self._mcv("19.99", 0.01),
            ]),
        ])
        plan = to_v1_plan(tbl, stats=ts)
        cs = _find_col(plan, "products", "price")
        # High-cardinality, low-coverage MCVs → should remain RangeColumn
        assert isinstance(cs.gen, RangeColumn)

    def test_low_cardinality_mcv_applied(self):
        """n_distinct ≤ 20 triggers MCV regardless of frequency."""
        tbl = self._tbl()
        ts = self._make_table_stats([
            self._col_stats("region", n_distinct=5.0, most_common_values=[
                self._mcv("US", 0.10),
                self._mcv("EU", 0.10),
            ]),
        ])
        plan = to_v1_plan(tbl, stats=ts)
        cs = _find_col(plan, "products", "region")
        assert isinstance(cs.gen, ValuesColumn)

    # ── numeric min/max from stats ────────────────────────────────────────

    def test_numeric_range_from_stats(self):
        tbl = self._tbl()
        ts = self._make_table_stats([
            self._col_stats("price", min_value="0.01", max_value="999.99"),
        ])
        plan = to_v1_plan(tbl, stats=ts)
        cs = _find_col(plan, "products", "price")
        assert isinstance(cs.gen, RangeColumn)
        assert cs.gen.min == pytest.approx(0.01)
        assert cs.gen.max == pytest.approx(999.99)

    def test_explicit_gen_overrides_stats_min_max(self):
        """GenerationRule.min_value takes priority over ColumnStats."""
        from src.statschema import parse_ddl
        from src.statschema.model import GenerationRule
        tbl = parse_ddl(self.DDL, dialect="mysql")[0]
        for col in tbl.columns:
            if col.name == "price":
                col.generation = GenerationRule(min_value=5.0, max_value=50.0)
        ts = self._make_table_stats([
            self._col_stats("price", min_value="0.01", max_value="999.99"),
        ])
        plan = to_v1_plan(tbl, stats=ts)
        cs = _find_col(plan, "products", "price")
        assert cs.gen.min == pytest.approx(5.0)
        assert cs.gen.max == pytest.approx(50.0)

    # ── date begin/end from stats ─────────────────────────────────────────

    def test_date_range_from_stats(self):
        tbl = self._tbl()
        ts = self._make_table_stats([
            self._col_stats("created", min_value="2021-03-01", max_value="2024-12-31"),
        ])
        plan = to_v1_plan(tbl, stats=ts)
        cs = _find_col(plan, "products", "created")
        assert isinstance(cs.gen, TimestampColumn)
        assert cs.gen.start == "2021-03-01"
        assert cs.gen.end == "2024-12-31"

    # ── stats dict for multi-table plans ─────────────────────────────────

    def test_stats_dict_for_multi_table(self):
        """stats={table_name: TableStats} distributes stats per table."""
        from src.statschema import parse_ddl
        ddl2 = """
        CREATE TABLE categories (
            cat_id   INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            label    VARCHAR(50) NOT NULL
        );
        """
        tbl1 = self._tbl()
        tbl2 = parse_ddl(ddl2, dialect="mysql")[0]
        ts1 = self._make_table_stats([
            self._col_stats("price", min_value="1.0", max_value="500.0"),
        ])
        plan = to_v1_plan(
            [tbl1, tbl2],
            stats={"products": ts1},
        )
        price = _find_col(plan, "products", "price")
        assert price.gen.max == pytest.approx(500.0)

    def test_no_stats_produces_valid_plan(self):
        """stats=None path still works (backward compat)."""
        tbl = self._tbl()
        plan = to_v1_plan(tbl)
        assert len(plan.tables[0].columns) > 0
