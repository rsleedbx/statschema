"""
Tests for the canonical-model data generation pipeline:
  - CanonicalTableSchema.row_count / row_count_per_sf / load_after (model + YAML round-trip)
  - resolve_load_order()     — topological sort via FK constraints and load_after
  - resolve_row_counts()     — fixed / scale-factor / default resolution
  - FK range injection in to_dbldatagen_specs() (parent_row_counts parameter)
  - generate_rows()          — pure-Python row generator driven by GenerationRule
"""

from __future__ import annotations

import math
from datetime import date, datetime

import pytest

from src.statschema.model import (
    CanonicalColumn,
    CanonicalForeignKey,
    CanonicalTableSchema,
    GenerationRule,
)
from src.statschema.schema_io import resolve_load_order, resolve_row_counts
from src.statschema.row_generator import generate_rows


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _col(name: str, ctype: str = "integer", **kwargs) -> CanonicalColumn:
    return CanonicalColumn(name=name, type=ctype, **kwargs)


def _table(name: str, cols=None, *, fk_constraints=None, load_after=None,
           row_count=None, row_count_per_sf=None) -> CanonicalTableSchema:
    return CanonicalTableSchema(
        name=name,
        columns=cols or [_col("id")],
        fk_constraints=fk_constraints,
        load_after=load_after or [],
        row_count=row_count,
        row_count_per_sf=row_count_per_sf,
    )


def _fk(columns, parent_table, parent_columns=None, *, fk_distribution="uniform"):
    return CanonicalForeignKey(
        columns=columns,
        parent_table=parent_table,
        parent_columns=parent_columns or ["id"],
        fk_distribution=fk_distribution,
    )


# ---------------------------------------------------------------------------
# Model round-trip
# ---------------------------------------------------------------------------

class TestModelRoundTrip:
    def test_row_count_fixed_serializes(self):
        t = _table("orders", row_count=1000)
        d = t.to_dict()
        assert d["row_count"] == 1000
        assert "row_count_per_sf" not in d

    def test_row_count_per_sf_serializes(self):
        t = _table("lineitem", row_count_per_sf=6_000_000.0)
        d = t.to_dict()
        assert d["row_count_per_sf"] == 6_000_000.0
        assert "row_count" not in d

    def test_load_after_serializes(self):
        t = _table("child", load_after=["parent_a", "parent_b"])
        d = t.to_dict()
        assert d["load_after"] == ["parent_a", "parent_b"]

    def test_defaults_omitted(self):
        t = _table("simple")
        d = t.to_dict()
        assert "row_count" not in d
        assert "row_count_per_sf" not in d
        assert "load_after" not in d

    def test_from_dict_round_trip(self):
        orig = CanonicalTableSchema(
            name="t",
            columns=[_col("x")],
            row_count=500,
            row_count_per_sf=1234.5,
            load_after=["a", "b"],
        )
        restored = CanonicalTableSchema.from_dict(orig.to_dict())
        assert restored.row_count == 500
        assert restored.row_count_per_sf == 1234.5
        assert restored.load_after == ["a", "b"]

    def test_from_dict_missing_fields_are_none(self):
        t = CanonicalTableSchema.from_dict({"name": "x", "columns": []})
        assert t.row_count is None
        assert t.row_count_per_sf is None
        assert t.load_after == []


# ---------------------------------------------------------------------------
# resolve_load_order
# ---------------------------------------------------------------------------

class TestResolveLoadOrder:
    def test_no_dependencies_preserves_order(self):
        a, b, c = _table("a"), _table("b"), _table("c")
        result = resolve_load_order([a, b, c])
        assert [t.name for t in result] == ["a", "b", "c"]

    def test_single_fk_dependency(self):
        parent = _table("parent")
        child  = _table("child", fk_constraints=[_fk(["pid"], "parent")])
        result = resolve_load_order([child, parent])
        names  = [t.name for t in result]
        assert names.index("parent") < names.index("child")

    def test_load_after_respected(self):
        a = _table("a")
        b = _table("b", load_after=["a"])
        result = resolve_load_order([b, a])
        names  = [t.name for t in result]
        assert names.index("a") < names.index("b")

    def test_chain_a_b_c(self):
        a = _table("a")
        b = _table("b", fk_constraints=[_fk(["aid"], "a")])
        c = _table("c", fk_constraints=[_fk(["bid"], "b")])
        result = resolve_load_order([c, b, a])
        names  = [t.name for t in result]
        assert names == ["a", "b", "c"]

    def test_diamond_dependency(self):
        # a → b, a → c, d depends on b and c
        a = _table("a")
        b = _table("b", fk_constraints=[_fk(["aid"], "a")])
        c = _table("c", fk_constraints=[_fk(["aid"], "a")])
        d = _table("d", fk_constraints=[_fk(["bid"], "b"), _fk(["cid"], "c")])
        result = resolve_load_order([d, c, b, a])
        names  = [t.name for t in result]
        # a must come before b and c; b and c before d
        assert names.index("a") < names.index("b")
        assert names.index("a") < names.index("c")
        assert names.index("b") < names.index("d")
        assert names.index("c") < names.index("d")

    def test_circular_dependency_returns_original(self):
        # a → b → a (cycle) — should not raise; fall back to original order
        a = _table("a", fk_constraints=[_fk(["bid"], "b")])
        b = _table("b", fk_constraints=[_fk(["aid"], "a")])
        result = resolve_load_order([a, b])
        assert set(t.name for t in result) == {"a", "b"}

    def test_tpch_yaml_order(self):
        from src.statschema.schema_io import load_canonical
        from pathlib import Path
        yaml_path = Path("benchmarks/schemas/tpch_schema.yaml")
        if not yaml_path.exists():
            pytest.skip("tpch_schema.yaml not found")
        tables = load_canonical(yaml_path)
        ordered = resolve_load_order(tables)
        names = [t.name for t in ordered]
        # region must precede nation; nation must precede supplier and customer
        assert names.index("region") < names.index("nation")
        assert names.index("nation") < names.index("supplier")
        assert names.index("nation") < names.index("customer")
        # part and supplier must precede partsupp
        assert names.index("part")     < names.index("partsupp")
        assert names.index("supplier") < names.index("partsupp")
        # orders before lineitem
        assert names.index("orders") < names.index("lineitem")


# ---------------------------------------------------------------------------
# resolve_row_counts
# ---------------------------------------------------------------------------

class TestResolveRowCounts:
    def test_fixed_row_count(self):
        t = _table("t", row_count=250)
        counts = resolve_row_counts([t], scale_factor=1.0)
        assert counts["t"] == 250

    def test_fixed_ignores_sf(self):
        t = _table("t", row_count=250)
        counts = resolve_row_counts([t], scale_factor=10.0)
        assert counts["t"] == 250

    def test_per_sf_multiplies(self):
        t = _table("t", row_count_per_sf=10_000.0)
        assert resolve_row_counts([t], scale_factor=1.0)["t"] == 10_000
        assert resolve_row_counts([t], scale_factor=0.1)["t"] == 1_000
        assert resolve_row_counts([t], scale_factor=5.0)["t"] == 50_000

    def test_per_sf_floor_applied(self):
        t = _table("t", row_count_per_sf=3.7)
        assert resolve_row_counts([t], scale_factor=1.0)["t"] == 3

    def test_default_rows_used(self):
        t = _table("t")  # no row_count or row_count_per_sf
        counts = resolve_row_counts([t], scale_factor=1.0, default_rows=500)
        assert counts["t"] == 500

    def test_minimum_is_one(self):
        t = _table("t", row_count_per_sf=0.001)
        counts = resolve_row_counts([t], scale_factor=1.0)
        assert counts["t"] >= 1

    def test_row_count_overrides_per_sf(self):
        t = _table("t", row_count=99, row_count_per_sf=10_000.0)
        assert resolve_row_counts([t], scale_factor=1.0)["t"] == 99

    def test_tpcc_sf1_counts(self):
        from src.statschema.schema_io import load_canonical
        from pathlib import Path
        yaml_path = Path("benchmarks/schemas/tpcc_schema.yaml")
        if not yaml_path.exists():
            pytest.skip("tpcc_schema.yaml not found")
        tables = load_canonical(yaml_path)
        counts = resolve_row_counts(tables, scale_factor=1.0)
        assert counts["item"] == 100_000         # fixed, per TPC-C spec
        assert counts["warehouse"] == 1          # 1 per SF
        assert counts["district"] == 10          # 10 per warehouse
        assert counts["customer"] == 30_000      # 3 000 per district × 10


# ---------------------------------------------------------------------------
# FK range injection in to_dbldatagen_specs
# ---------------------------------------------------------------------------

class TestFKRangeInjection:
    """Verify that FK columns get minValue/maxValue from parent_row_counts."""

    def _specs_dict(self, table, rows, parent_row_counts=None):
        """Run to_dbldatagen_specs without Spark (we only inspect the opts dict)."""
        from src.statschema.dbldatagen_builder import _spark_type_and_options
        result = {}
        fk_max_map: dict[str, int] = {}
        if parent_row_counts:
            if table.fk_constraints:
                for fk in table.fk_constraints:
                    pc = parent_row_counts.get(fk.parent_table)
                    if pc:
                        for cn in fk.columns:
                            fk_max_map[cn] = pc
            for col in table.columns:
                if col.references and col.name not in fk_max_map:
                    pt, _ = col.references
                    pc = parent_row_counts.get(pt)
                    if pc:
                        fk_max_map[col.name] = pc
        for col in table.columns:
            _, opts = _spark_type_and_options(
                col, rows=rows, fk_max=fk_max_map.get(col.name)
            )
            result[col.name] = opts
        return result

    def test_fk_column_gets_range(self):
        child = CanonicalTableSchema(
            name="child",
            columns=[_col("pid", "integer")],
            fk_constraints=[_fk(["pid"], "parent")],
        )
        specs = self._specs_dict(child, 100, parent_row_counts={"parent": 500})
        assert specs["pid"]["minValue"] == 1
        assert specs["pid"]["maxValue"] == 500

    def test_explicit_generation_overrides_fk_range(self):
        col_with_gen = CanonicalColumn(
            name="pid", type="integer",
            generation=GenerationRule(min_value=10, max_value=20),
        )
        child = CanonicalTableSchema(
            name="child",
            columns=[col_with_gen],
            fk_constraints=[_fk(["pid"], "parent")],
        )
        specs = self._specs_dict(child, 100, parent_row_counts={"parent": 500})
        # Explicit GenerationRule takes precedence over FK range
        assert specs["pid"]["minValue"] == 10
        assert specs["pid"]["maxValue"] == 20

    def test_no_parent_row_counts_no_fk_range(self):
        child = CanonicalTableSchema(
            name="child",
            columns=[_col("pid", "integer")],
            fk_constraints=[_fk(["pid"], "parent")],
        )
        specs = self._specs_dict(child, 100, parent_row_counts=None)
        # FK range not applied — default integer opts used
        assert specs["pid"].get("maxValue") != 500  # could be any default

    def test_column_references_fallback(self):
        col = CanonicalColumn(name="wid", type="integer", references=("warehouse", "w_id"))
        t = CanonicalTableSchema(name="stock", columns=[col])
        specs = self._specs_dict(t, 100, parent_row_counts={"warehouse": 10})
        assert specs["wid"]["maxValue"] == 10


# ---------------------------------------------------------------------------
# generate_rows
# ---------------------------------------------------------------------------

class TestGenerateRows:
    def test_row_count(self):
        t = _table("t", cols=[_col("id")])
        rows = list(generate_rows(t, 50))
        assert len(rows) == 50

    def test_yields_dicts_with_correct_keys(self):
        t = CanonicalTableSchema(
            name="t",
            columns=[_col("a", "integer"), _col("b", "string")],
        )
        rows = list(generate_rows(t, 5))
        for row in rows:
            assert set(row.keys()) == {"a", "b"}

    def test_sequential_distribution(self):
        col = CanonicalColumn(
            name="id", type="integer",
            generation=GenerationRule(distribution="sequential", min_value=1),
        )
        t = CanonicalTableSchema(name="t", columns=[col])
        rows = list(generate_rows(t, 5))
        assert [r["id"] for r in rows] == [1, 2, 3, 4, 5]

    def test_constant_distribution(self):
        col = CanonicalColumn(
            name="v", type="integer",
            generation=GenerationRule(distribution="constant", min_value=42),
        )
        t = CanonicalTableSchema(name="t", columns=[col])
        rows = list(generate_rows(t, 10))
        assert all(r["v"] == 42 for r in rows)

    def test_values_list_respected(self):
        col = CanonicalColumn(
            name="status", type="string",
            generation=GenerationRule(values=["A", "B", "C"]),
        )
        t = CanonicalTableSchema(name="t", columns=[col])
        rows = list(generate_rows(t, 100))
        assert all(r["status"] in {"A", "B", "C"} for r in rows)

    def test_weighted_values(self):
        col = CanonicalColumn(
            name="flag", type="string",
            generation=GenerationRule(values=["X", "Y"], weights=[1.0, 0.0]),
        )
        t = CanonicalTableSchema(name="t", columns=[col])
        rows = list(generate_rows(t, 50, seed=0))
        assert all(r["flag"] == "X" for r in rows)

    def test_integer_range(self):
        col = CanonicalColumn(
            name="n", type="integer",
            generation=GenerationRule(min_value=10, max_value=20),
        )
        t = CanonicalTableSchema(name="t", columns=[col])
        rows = list(generate_rows(t, 100))
        vals = [r["n"] for r in rows]
        assert all(10 <= v <= 20 for v in vals)

    def test_decimal_range_and_scale(self):
        col = CanonicalColumn(
            name="price", type="decimal", precision=5, scale=2,
            generation=GenerationRule(min_value=1.0, max_value=99.99),
        )
        t = CanonicalTableSchema(name="t", columns=[col])
        rows = list(generate_rows(t, 50))
        for row in rows:
            v = row["price"]
            assert 1.0 <= v <= 99.99
            # scale 2 → at most 2 decimal places
            assert round(v, 2) == v

    def test_date_range(self):
        col = CanonicalColumn(
            name="d", type="date",
            generation=GenerationRule(min_value="2020-01-01", max_value="2023-12-31"),
        )
        t = CanonicalTableSchema(name="t", columns=[col])
        rows = list(generate_rows(t, 30))
        lo, hi = date(2020, 1, 1), date(2023, 12, 31)
        for row in rows:
            assert isinstance(row["d"], date)
            assert lo <= row["d"] <= hi

    def test_timestamp_fallback_type(self):
        col = CanonicalColumn(name="ts", type="timestamp")
        t = CanonicalTableSchema(name="t", columns=[col])
        rows = list(generate_rows(t, 5))
        assert all(isinstance(r["ts"], datetime) for r in rows)

    def test_fk_range_from_fk_constraints(self):
        parent_counts = {"customer": 3000}
        col = CanonicalColumn(name="c_id", type="integer")
        child = CanonicalTableSchema(
            name="orders",
            columns=[col],
            fk_constraints=[_fk(["c_id"], "customer")],
        )
        rows = list(generate_rows(child, 200, parent_row_counts=parent_counts))
        for row in rows:
            assert 1 <= row["c_id"] <= 3000

    def test_fk_range_from_column_references(self):
        parent_counts = {"warehouse": 10}
        col = CanonicalColumn(name="wid", type="integer",
                              references=("warehouse", "w_id"))
        t = CanonicalTableSchema(name="stock", columns=[col])
        rows = list(generate_rows(t, 50, parent_row_counts=parent_counts))
        assert all(1 <= r["wid"] <= 10 for r in rows)

    def test_seed_produces_same_sequence(self):
        col = CanonicalColumn(
            name="n", type="integer",
            generation=GenerationRule(min_value=1, max_value=1000),
        )
        t = CanonicalTableSchema(name="t", columns=[col])
        a = list(generate_rows(t, 20, seed=7))
        b = list(generate_rows(t, 20, seed=7))
        assert a == b

    def test_different_seeds_give_different_sequences(self):
        col = CanonicalColumn(
            name="n", type="integer",
            generation=GenerationRule(min_value=1, max_value=1_000_000),
        )
        t = CanonicalTableSchema(name="t", columns=[col])
        a = list(generate_rows(t, 20, seed=1))
        b = list(generate_rows(t, 20, seed=2))
        assert a != b

    def test_string_fallback_uses_length(self):
        col = CanonicalColumn(name="data", type="string", length=10)
        t = CanonicalTableSchema(name="t", columns=[col])
        rows = list(generate_rows(t, 5))
        for row in rows:
            assert isinstance(row["data"], str)
            assert len(row["data"]) <= 10

    def test_format_pattern_phone_us(self):
        col = CanonicalColumn(
            name="phone", type="string", length=16,
            generation=GenerationRule(format_pattern="phone_us"),
        )
        t = CanonicalTableSchema(name="t", columns=[col])
        rows = list(generate_rows(t, 5))
        # NXX-NXX-XXXX format — 12 chars, no + prefix
        assert all("-" in r["phone"] for r in rows)
        assert all(len(r["phone"]) <= 16 for r in rows)

    def test_tpcc_item_table(self):
        """Smoke-test: generate 10 item rows from the TPC-C canonical YAML."""
        from src.statschema.schema_io import load_canonical
        from pathlib import Path
        yaml_path = Path("benchmarks/schemas/tpcc_schema.yaml")
        if not yaml_path.exists():
            pytest.skip("tpcc_schema.yaml not found")
        tables = load_canonical(yaml_path)
        item_table = next(t for t in tables if t.name == "item")
        rows = list(generate_rows(item_table, 10, seed=0))
        assert len(rows) == 10
        for row in rows:
            assert 1.0 <= row["i_price"] <= 99.99
            assert isinstance(row["i_name"], str)

    def test_tpch_lineitem_dates_present(self):
        """Smoke-test: lineitem generates date columns correctly."""
        from src.statschema.schema_io import load_canonical
        from pathlib import Path
        yaml_path = Path("benchmarks/schemas/tpch_schema.yaml")
        if not yaml_path.exists():
            pytest.skip("tpch_schema.yaml not found")
        tables = load_canonical(yaml_path)
        li_table = next(t for t in tables if t.name == "lineitem")
        row_counts = {"orders": 5, "part": 10, "supplier": 3, "partsupp": 40}
        rows = list(generate_rows(li_table, 10, parent_row_counts=row_counts, seed=0))
        assert len(rows) == 10
        for row in rows:
            assert row["l_returnflag"] in {"A", "N", "R"}
            assert row["l_shipmode"] in {
                "AIR", "AIR REG", "SHIP", "TRUCK", "RAIL", "MAIL", "FOB"
            }
            assert isinstance(row["l_shipdate"], date)
