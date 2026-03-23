"""
Tests for pandas_builder.build_rows_from_canonical — pure-Python generation path.

All tests run without Spark, Java, or any live database connection.
"""

from __future__ import annotations

import math
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from src.statschema import build_rows_from_canonical
from src.statschema.model import (
    CanonicalColumn,
    CanonicalForeignKey,
    CanonicalTableSchema,
    GenerationRule,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _table(*cols: CanonicalColumn, name: str = "t") -> CanonicalTableSchema:
    return CanonicalTableSchema(name=name, columns=list(cols))


def _col(name: str, ctype: str, **kwargs) -> CanonicalColumn:
    return CanonicalColumn(name=name, type=ctype, **kwargs)


def _gen(**kwargs) -> GenerationRule:
    return GenerationRule(**kwargs)


# ---------------------------------------------------------------------------
# Basic shape and reproducibility
# ---------------------------------------------------------------------------

class TestBasicShape:
    def test_row_count(self):
        t = _table(_col("id", "long"))
        df = build_rows_from_canonical(t, rows=500)
        assert len(df) == 500

    def test_column_names_preserved(self):
        t = _table(_col("id", "long"), _col("name", "string"), _col("active", "boolean"))
        df = build_rows_from_canonical(t, rows=10)
        assert list(df.columns) == ["id", "name", "active"]

    def test_seed_reproducibility(self):
        t = _table(_col("x", "long"), _col("y", "double"))
        df1 = build_rows_from_canonical(t, rows=100, seed=42)
        df2 = build_rows_from_canonical(t, rows=100, seed=42)
        pd.testing.assert_frame_equal(df1, df2)

    def test_different_seeds_differ(self):
        t = _table(_col("x", "long"))
        df1 = build_rows_from_canonical(t, rows=100, seed=1)
        df2 = build_rows_from_canonical(t, rows=100, seed=2)
        assert not df1["x"].equals(df2["x"])

    def test_empty_rows(self):
        t = _table(_col("id", "long"))
        df = build_rows_from_canonical(t, rows=0)
        assert len(df) == 0
        assert "id" in df.columns


# ---------------------------------------------------------------------------
# Numeric types
# ---------------------------------------------------------------------------

class TestNumericTypes:
    def test_integer_range(self):
        t = _table(_col("n", "integer", not_null=True))
        df = build_rows_from_canonical(t, rows=1000, seed=0)
        assert df["n"].notna().all()
        assert df["n"].between(0, 2**31 - 1).all()

    def test_long_range(self):
        t = _table(_col("n", "long", not_null=True))
        df = build_rows_from_canonical(t, rows=1000, seed=0)
        assert df["n"].notna().all()

    def test_integer_explicit_min_max(self):
        t = _table(_col("n", "integer", not_null=True,
                        generation=_gen(min_value=10, max_value=20)))
        df = build_rows_from_canonical(t, rows=500, seed=0)
        assert df["n"].between(10, 20).all()

    def test_decimal_precision(self):
        t = _table(_col("price", "decimal", precision=10, scale=2, not_null=True))
        df = build_rows_from_canonical(t, rows=200, seed=0)
        # np.round(v, 2) should equal v within float64 epsilon
        assert all(abs(v - round(v, 2)) < 1e-9 for v in df["price"])

    def test_float_dtype(self):
        t = _table(_col("f", "float", not_null=True))
        df = build_rows_from_canonical(t, rows=100, seed=0)
        assert df["f"].dtype == np.float32

    def test_double_dtype(self):
        t = _table(_col("d", "double", not_null=True))
        df = build_rows_from_canonical(t, rows=100, seed=0)
        assert df["d"].dtype == np.float64


# ---------------------------------------------------------------------------
# Sequential and special distributions
# ---------------------------------------------------------------------------

class TestDistributions:
    def test_sequential_starts_at_1(self):
        t = _table(_col("id", "long", not_null=True,
                        generation=_gen(distribution="sequential")))
        df = build_rows_from_canonical(t, rows=100, seed=0)
        assert list(df["id"]) == list(range(1, 101))

    def test_sequential_custom_start(self):
        t = _table(_col("id", "long", not_null=True,
                        generation=_gen(distribution="sequential", min_value=1000)))
        df = build_rows_from_canonical(t, rows=5, seed=0)
        assert list(df["id"]) == [1000, 1001, 1002, 1003, 1004]

    def test_constant(self):
        t = _table(_col("c", "integer", not_null=True,
                        generation=_gen(distribution="constant", min_value=42)))
        df = build_rows_from_canonical(t, rows=50, seed=0)
        assert (df["c"] == 42).all()

    def test_cyclic(self):
        t = _table(_col("c", "integer", not_null=True,
                        generation=_gen(distribution="cyclic", min_value=1, max_value=3)))
        df = build_rows_from_canonical(t, rows=9, seed=0)
        assert list(df["c"]) == [1, 2, 3, 1, 2, 3, 1, 2, 3]

    def test_block(self):
        t = _table(_col("b", "integer", not_null=True,
                        generation=_gen(distribution="block", min_value=1,
                                        distribution_params={"block_size": 3})))
        df = build_rows_from_canonical(t, rows=9, seed=0)
        assert list(df["b"]) == [1, 1, 1, 2, 2, 2, 3, 3, 3]

    def test_normal_distribution_clipped(self):
        t = _table(_col("n", "integer", not_null=True,
                        generation=_gen(distribution="normal", min_value=0, max_value=100,
                                        distribution_params={"mean": 50.0, "std": 15.0})))
        df = build_rows_from_canonical(t, rows=1000, seed=0)
        assert df["n"].between(0, 100).all()
        mean = df["n"].mean()
        assert 40 < mean < 60, f"Expected mean near 50, got {mean:.1f}"

    def test_zipf_heavy_tail(self):
        t = _table(_col("fk", "long", not_null=True,
                        generation=_gen(distribution="zipf", min_value=1, max_value=1000,
                                        distribution_params={"exponent": 1.5})))
        df = build_rows_from_canonical(t, rows=5000, seed=0)
        assert df["fk"].between(1, 1000).all()
        # Zipf is heavy-tailed: median should be close to the lower end
        assert df["fk"].median() < 200

    def test_values_and_weights(self):
        t = _table(_col("status", "string", not_null=True,
                        generation=_gen(values=["active", "inactive", "pending"],
                                        weights=[0.7, 0.2, 0.1])))
        df = build_rows_from_canonical(t, rows=2000, seed=0)
        counts = df["status"].value_counts(normalize=True)
        assert counts["active"] > 0.55   # should be ~0.70, allow variance
        assert "inactive" in counts.index
        assert "pending" in counts.index


# ---------------------------------------------------------------------------
# String types
# ---------------------------------------------------------------------------

class TestStringTypes:
    def test_basic_string_generated(self):
        t = _table(_col("name", "string", not_null=True))
        df = build_rows_from_canonical(t, rows=50, seed=0)
        assert df["name"].notna().all()
        assert all(isinstance(v, str) for v in df["name"])

    def test_string_length_respected(self):
        t = _table(_col("code", "string", length=5, not_null=True))
        df = build_rows_from_canonical(t, rows=200, seed=0)
        assert all(len(v) <= 5 for v in df["code"])

    def test_format_pattern_email(self):
        t = _table(_col("email", "string", not_null=True,
                        generation=_gen(format_pattern="email")))
        df = build_rows_from_canonical(t, rows=50, seed=0)
        assert all("@" in v and "." in v for v in df["email"])

    def test_format_pattern_uuid(self):
        t = _table(_col("id", "string", not_null=True,
                        generation=_gen(format_pattern="uuid")))
        df = build_rows_from_canonical(t, rows=20, seed=0)
        import re
        uuid_re = re.compile(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
        )
        assert all(uuid_re.match(v) for v in df["id"])

    def test_format_pattern_ip_v4(self):
        t = _table(_col("ip", "string", not_null=True,
                        generation=_gen(format_pattern="ip_v4")))
        df = build_rows_from_canonical(t, rows=30, seed=0)
        for v in df["ip"]:
            parts = v.split(".")
            assert len(parts) == 4
            assert all(0 <= int(p) <= 255 for p in parts)

    def test_semantic_hint_email_inferred(self):
        # column named "email_address" should auto-infer the email pattern
        t = _table(_col("email_address", "string", not_null=True))
        df = build_rows_from_canonical(t, rows=30, seed=0)
        assert all("@" in v for v in df["email_address"])


# ---------------------------------------------------------------------------
# Boolean and temporal types
# ---------------------------------------------------------------------------

class TestBooleanAndTemporal:
    def test_boolean_values(self):
        t = _table(_col("active", "boolean", not_null=True))
        df = build_rows_from_canonical(t, rows=200, seed=0)
        assert set(df["active"].unique()).issubset({True, False})
        # Both values should appear with 500 rows
        assert True in df["active"].values
        assert False in df["active"].values

    def test_date_type(self):
        t = _table(_col("birth_date", "date", not_null=True))
        df = build_rows_from_canonical(t, rows=100, seed=0)
        assert all(isinstance(v, date) for v in df["birth_date"])
        lo, hi = date(2020, 1, 1), date(2024, 12, 31)
        assert all(lo <= v <= hi for v in df["birth_date"])

    def test_timestamp_type(self):
        t = _table(_col("created_at", "timestamp", not_null=True))
        df = build_rows_from_canonical(t, rows=100, seed=0)
        assert all(isinstance(v, datetime) for v in df["created_at"])

    def test_time_type(self):
        t = _table(_col("start_time", "time", not_null=True))
        df = build_rows_from_canonical(t, rows=30, seed=0)
        import re
        time_re = re.compile(r"^\d{2}:\d{2}:\d{2}$")
        assert all(time_re.match(v) for v in df["start_time"])


# ---------------------------------------------------------------------------
# Null injection
# ---------------------------------------------------------------------------

class TestNullInjection:
    def test_not_null_never_null(self):
        t = _table(_col("id", "long", not_null=True,
                        generation=_gen(null_rate=0.9)))
        df = build_rows_from_canonical(t, rows=500, seed=0)
        assert df["id"].notna().all()

    def test_explicit_null_rate(self):
        t = _table(_col("notes", "string",
                        generation=_gen(null_rate=0.5)))
        df = build_rows_from_canonical(t, rows=2000, seed=0)
        null_frac = df["notes"].isna().mean()
        assert 0.35 < null_frac < 0.65


# ---------------------------------------------------------------------------
# FK range constraints
# ---------------------------------------------------------------------------

class TestFKConstraints:
    def test_fk_column_constrained_via_fk_constraints(self):
        table = CanonicalTableSchema(
            name="orders",
            columns=[
                _col("order_id", "long", not_null=True,
                     generation=_gen(distribution="sequential")),
                _col("customer_id", "long", not_null=True),
            ],
            fk_constraints=[
                CanonicalForeignKey(
                    columns=["customer_id"],
                    parent_table="customers",
                    parent_columns=["id"],
                )
            ],
        )
        df = build_rows_from_canonical(
            table, rows=500, seed=0, parent_row_counts={"customers": 50}
        )
        assert df["customer_id"].between(1, 50).all()

    def test_fk_column_constrained_via_references(self):
        t = _table(
            _col("order_id", "long", not_null=True,
                 generation=_gen(distribution="sequential")),
            _col("product_id", "long", not_null=True,
                 references=("products", "product_id")),
        )
        df = build_rows_from_canonical(
            t, rows=300, seed=0, parent_row_counts={"products": 20}
        )
        assert df["product_id"].between(1, 20).all()


# ---------------------------------------------------------------------------
# Temporal ordering constraints
# ---------------------------------------------------------------------------

class TestTemporalOrdering:
    def test_end_date_after_start_date(self):
        table = CanonicalTableSchema(
            name="events",
            columns=[
                _col("start_date", "date", not_null=True),
                _col("end_date",   "date", not_null=True),
            ],
            temporal_ordering_constraints=["end_date > start_date"],
        )
        df = build_rows_from_canonical(table, rows=500, seed=0)
        assert (df["end_date"] > df["start_date"]).all()

    def test_updated_at_gte_created_at(self):
        table = CanonicalTableSchema(
            name="records",
            columns=[
                _col("created_at", "timestamp", not_null=True),
                _col("updated_at", "timestamp", not_null=True),
            ],
            temporal_ordering_constraints=["updated_at >= created_at"],
        )
        df = build_rows_from_canonical(table, rows=500, seed=0)
        assert (df["updated_at"] >= df["created_at"]).all()


# ---------------------------------------------------------------------------
# Multi-column table end-to-end
# ---------------------------------------------------------------------------

class TestEndToEnd:
    def test_orders_table(self):
        table = CanonicalTableSchema(
            name="orders",
            columns=[
                _col("order_id",    "long",    not_null=True,
                     generation=_gen(distribution="sequential")),
                _col("customer_id", "long",    not_null=True,
                     references=("customers", "id")),
                _col("status",      "string",  not_null=True,
                     generation=_gen(values=["pending", "shipped", "delivered", "cancelled"],
                                     weights=[0.3, 0.2, 0.45, 0.05])),
                _col("total",       "decimal", not_null=True,
                     precision=10, scale=2,
                     generation=_gen(min_value=1.0, max_value=9999.99)),
                _col("created_at",  "timestamp", not_null=True),
                _col("notes",       "string",
                     generation=_gen(null_rate=0.4)),
            ],
            fk_constraints=[
                CanonicalForeignKey(
                    columns=["customer_id"],
                    parent_table="customers",
                    parent_columns=["id"],
                )
            ],
        )
        df = build_rows_from_canonical(
            table, rows=1000, seed=99,
            parent_row_counts={"customers": 100},
        )
        assert len(df) == 1000
        assert list(df["order_id"]) == list(range(1, 1001))
        assert df["customer_id"].between(1, 100).all()
        assert set(df["status"].unique()).issubset(
            {"pending", "shipped", "delivered", "cancelled"}
        )
        assert df["total"].between(1.0, 9999.99).all()
        assert df["notes"].isna().mean() > 0.2    # some NULLs
        assert df["notes"].notna().any()           # not all NULL

    def test_importable_from_top_level(self):
        from src.statschema import build_rows_from_canonical as brc
        t = _table(_col("x", "integer"))
        df = brc(t, rows=5, seed=0)
        assert len(df) == 5
