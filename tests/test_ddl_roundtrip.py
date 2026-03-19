"""
DDL and statistics round-trip tests.

Phase 1  Parametrized same-dialect idempotent emission.
         canonical → emit(dialect) → parse(dialect) → emit(dialect)
         Both emits must be byte-for-byte identical.
         Covers every canonical type × every constraint variant × every
         parseable dialect: mysql, postgres, sqlserver, databricks-via-mysql.
         oracle is emit-only (no open-source parser exists yet).

Phase 2  Random canonical → round-trip (50+ tables per parseable dialect,
         seeded for determinism).  Varies column count, type mix, constraint
         combos, nullable/NOT NULL, DEFAULT, AUTO_INCREMENT, UNIQUE, PK.

Phase 3  Statistics YAML round-trip.
         DatabaseStats (all fields filled) → to_dict → from_dict → to_dict.
         dump_stats → load_stats file path.
         make_default_stats generates non-empty, consistent stats.

Phase 4  Override application.
         OverrideSpec yaml round-trip.
         Column/table/schema/catalog renames, type mapping, row-count scaling,
         reserved-word prefixing.

Phase 8  Temporal types — date/time with time zones and fractional-seconds precision.
         Covers: timestamptz, time, timetz, fsp (0–9), TIMESTAMP(n) WITH TIME ZONE
         for MySQL, PostgreSQL, SQL Server, Oracle (emit-only), and Databricks.

Phase 9  Common migration issues — binary types, MONEY, ROWVERSION, UNSIGNED widening,
         Oracle NUMBER(p), SQL Server special types.

Phase 10 Full pipeline: DDL → canonical YAML → DDL and DDL → canonical YAML → data.
         Tests the complete ``many DDL → one YAML → many DDL + data`` flow.
"""

from __future__ import annotations

import copy
import random
import tempfile
from pathlib import Path
from typing import Any, Optional

import pytest
import yaml

from src.statschema import (
    CanonicalColumn,
    CanonicalForeignKey,
    CanonicalTableSchema,
    GenerationRule,
    emit_ddl,
    parse_ddl,
    dump_schema,
    load_canonical,
)
from src.statschema.dbldatagen_builder import to_dbldatagen_specs
from src.statschema.ddl_parser import (
    MYSQL_TYPE_TO_CANONICAL,
    POSTGRES_TYPE_TO_CANONICAL,
    SQLSERVER_TYPE_TO_CANONICAL,
)
from src.statschema.stats_model import (
    ColumnStats,
    CompositeColumnStats,
    DatabaseStats,
    ForeignKeyStats,
    IndexStats,
    MCVCombination,
    MostCommonValue,
    TableStats,
)
from src.statschema.override_model import ColumnOverride, OverrideSpec, TableOverride
from src.statschema.stats_io import (
    apply_overrides,
    apply_overrides_all,
    dump_stats,
    load_stats,
    make_default_stats,
)
from tests.helpers import (
    assert_ddl_roundtrip,
    assert_parse_first_roundtrip,
    ddl_roundtrip,
    make_col,
    make_pk_table,
    make_table,
    parse_col_type,
    parse_first_roundtrip,
)

# ---------------------------------------------------------------------------
# Private shorthands — thin wrappers kept for backward compat with test bodies
# ---------------------------------------------------------------------------
_col = make_col
_table = make_table
assert_roundtrip = assert_ddl_roundtrip


def _roundtrip(table, emit_dialect, parse_dialect):
    return ddl_roundtrip(table, emit_dialect, parse_dialect)


# ============================================================================
# Phase 1 — Parametrized same-dialect idempotent emission
# ============================================================================
#
# Parameter format: (dialect, emit_dialect, table_factory)
# For Databricks we emit with "databricks" but parse with "mysql" (same quoting;
# MySQL parser extended with STRING/BOOLEAN fallback).
# For Oracle we only test emit structure (no parser).

def _cases_mysql() -> list[tuple[str, str, CanonicalTableSchema]]:
    """All canonical type × constraint variants for MySQL."""
    cases = []

    # ── integer ──────────────────────────────────────────────────────────────
    cases += [
        ("mysql", "mysql", _table("t", _col("id", "integer", primary_key=True, not_null=True, auto_increment=True))),
        ("mysql", "mysql", _table("t", _col("id", "integer", primary_key=True, not_null=True, auto_increment=True), _col("v", "integer"))),
        ("mysql", "mysql", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("qty", "integer", not_null=True, default="0"))),
        ("mysql", "mysql", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("code", "integer", unique=True))),
        ("mysql", "mysql", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("n", "integer"))),
    ]

    # ── long ──────────────────────────────────────────────────────────────────
    cases += [
        ("mysql", "mysql", _table("t", _col("id", "long", primary_key=True, not_null=True, auto_increment=True))),
        ("mysql", "mysql", _table("t", _col("id", "long", primary_key=True, not_null=True), _col("big", "long"))),
    ]

    # ── string ────────────────────────────────────────────────────────────────
    cases += [
        ("mysql", "mysql", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("s", "string"))),
        ("mysql", "mysql", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("s", "string", length=50))),
        ("mysql", "mysql", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("s", "string", length=255, not_null=True))),
        ("mysql", "mysql", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("s", "string", length=100, default="'active'", not_null=True))),
        ("mysql", "mysql", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("s", "string", length=200, unique=True))),
    ]

    # ── float / double ───────────────────────────────────────────────────────
    cases += [
        ("mysql", "mysql", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("f", "float"))),
        ("mysql", "mysql", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("d", "double"))),
        ("mysql", "mysql", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("f", "float", not_null=True))),
        ("mysql", "mysql", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("d", "double", not_null=True))),
    ]

    # ── boolean ───────────────────────────────────────────────────────────────
    cases += [
        ("mysql", "mysql", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("active", "boolean"))),
        ("mysql", "mysql", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("flag", "boolean", default="1", not_null=True))),
        ("mysql", "mysql", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("flag", "boolean", not_null=True))),
    ]

    # ── timestamp / date ─────────────────────────────────────────────────────
    cases += [
        ("mysql", "mysql", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("ts", "timestamp"))),
        ("mysql", "mysql", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("dt", "date"))),
        ("mysql", "mysql", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("ts", "timestamp", not_null=True))),
    ]

    # ── decimal ───────────────────────────────────────────────────────────────
    for p, s in [(10, 2), (18, 4), (10, 0), (5, 2), (38, 10)]:
        cases.append(
            ("mysql", "mysql", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("amt", "decimal", precision=p, scale=s)))
        )
    cases.append(
        ("mysql", "mysql", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("amt", "decimal", precision=10, scale=2, not_null=True, default="0.00")))
    )

    # ── multi-column table ───────────────────────────────────────────────────
    cases.append(("mysql", "mysql", _table("orders",
        _col("order_id", "long", primary_key=True, not_null=True, auto_increment=True),
        _col("customer_id", "long", not_null=True),
        _col("status", "string", length=20, not_null=True, default="'pending'"),
        _col("total", "decimal", precision=12, scale=2, not_null=True),
        _col("notes", "string"),
        _col("created_at", "timestamp", not_null=True),
        _col("is_paid", "boolean", default="0"),
    )))

    return cases


def _cases_postgres() -> list[tuple[str, str, CanonicalTableSchema]]:
    cases = []

    # integer (plain SERIAL for auto-inc PK)
    cases += [
        ("postgres", "postgres", _table("t", _col("id", "integer", primary_key=True, not_null=True, auto_increment=True))),
        ("postgres", "postgres", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("qty", "integer", default="0"))),
        ("postgres", "postgres", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("code", "integer", unique=True))),
    ]

    # long (BIGSERIAL for auto-inc)
    cases += [
        ("postgres", "postgres", _table("t", _col("id", "long", primary_key=True, not_null=True, auto_increment=True))),
        ("postgres", "postgres", _table("t", _col("id", "long", primary_key=True, not_null=True), _col("big", "long"))),
    ]

    # string
    cases += [
        ("postgres", "postgres", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("s", "string"))),
        ("postgres", "postgres", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("s", "string", length=50))),
        ("postgres", "postgres", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("s", "string", length=255, not_null=True))),
        ("postgres", "postgres", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("s", "string", length=100, default="'active'"))),
        ("postgres", "postgres", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("s", "string", unique=True, not_null=True))),
    ]

    # float / double
    cases += [
        ("postgres", "postgres", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("f", "float"))),
        ("postgres", "postgres", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("d", "double"))),
    ]

    # boolean
    cases += [
        ("postgres", "postgres", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("active", "boolean"))),
        ("postgres", "postgres", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("flag", "boolean", default="true"))),
        ("postgres", "postgres", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("flag", "boolean", not_null=True, default="false"))),
    ]

    # timestamp / date
    cases += [
        ("postgres", "postgres", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("ts", "timestamp"))),
        ("postgres", "postgres", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("dt", "date"))),
        ("postgres", "postgres", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("ts", "timestamp", not_null=True))),
    ]

    # decimal
    for p, s in [(10, 2), (18, 4), (10, 0), (5, 2), (38, 10)]:
        cases.append(
            ("postgres", "postgres", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("amt", "decimal", precision=p, scale=s)))
        )

    # multi-column table
    cases.append(("postgres", "postgres", _table("orders",
        _col("order_id", "long", primary_key=True, not_null=True, auto_increment=True),
        _col("customer_id", "long", not_null=True),
        _col("status", "string", length=20, not_null=True, default="'pending'"),
        _col("total", "decimal", precision=12, scale=2, not_null=True),
        _col("notes", "string"),
        _col("created_at", "timestamp", not_null=True),
        _col("is_paid", "boolean", default="false"),
    )))

    return cases


def _cases_sqlserver() -> list[tuple[str, str, CanonicalTableSchema]]:
    cases = []

    # integer
    cases += [
        ("sqlserver", "sqlserver", _table("t", _col("id", "integer", primary_key=True, not_null=True, auto_increment=True))),
        ("sqlserver", "sqlserver", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("qty", "integer", default="0"))),
        ("sqlserver", "sqlserver", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("code", "integer", unique=True))),
    ]

    # long
    cases += [
        ("sqlserver", "sqlserver", _table("t", _col("id", "long", primary_key=True, not_null=True, auto_increment=True))),
        ("sqlserver", "sqlserver", _table("t", _col("id", "long", primary_key=True, not_null=True), _col("big", "long"))),
    ]

    # string without length → NVARCHAR(MAX)
    cases += [
        ("sqlserver", "sqlserver", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("s", "string"))),
        ("sqlserver", "sqlserver", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("s", "string", not_null=True))),
    ]

    # string with length → NVARCHAR(n)
    cases += [
        ("sqlserver", "sqlserver", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("s", "string", length=50))),
        ("sqlserver", "sqlserver", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("s", "string", length=255, not_null=True))),
        ("sqlserver", "sqlserver", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("s", "string", length=100, unique=True))),
    ]

    # float / double (both emit as FLOAT in sqlserver; sqlglot can't distinguish REAL from FLOAT)
    cases += [
        ("sqlserver", "sqlserver", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("f", "float"))),
        ("sqlserver", "sqlserver", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("d", "double"))),
        ("sqlserver", "sqlserver", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("f", "float", not_null=True))),
    ]

    # boolean → BIT
    cases += [
        ("sqlserver", "sqlserver", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("active", "boolean"))),
        ("sqlserver", "sqlserver", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("flag", "boolean", not_null=True, default="0"))),
    ]

    # timestamp → DATETIME2 / date → DATE
    cases += [
        ("sqlserver", "sqlserver", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("ts", "timestamp"))),
        ("sqlserver", "sqlserver", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("dt", "date"))),
    ]

    # decimal
    for p, s in [(10, 2), (18, 4), (10, 0), (5, 2)]:
        cases.append(
            ("sqlserver", "sqlserver", _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("amt", "decimal", precision=p, scale=s)))
        )

    # multi-column table
    cases.append(("sqlserver", "sqlserver", _table("orders",
        _col("order_id", "long", primary_key=True, not_null=True, auto_increment=True),
        _col("customer_id", "long", not_null=True),
        _col("status", "string", length=20, not_null=True),
        _col("total", "decimal", precision=12, scale=2, not_null=True),
        _col("notes", "string"),
        _col("created_at", "timestamp", not_null=True),
        _col("active", "boolean", default="1"),
    )))

    return cases


def _cases_databricks() -> list[tuple[str, str, CanonicalTableSchema]]:
    """Databricks: emit with 'databricks', parse with 'mysql'."""
    cases = []
    for ctype, kwargs in [
        ("integer", {}),
        ("integer", {"not_null": True, "default": "0"}),
        ("long",    {}),
        ("float",   {}),
        ("double",  {}),
        ("boolean", {}),
        ("boolean", {"not_null": True}),
        ("string",  {}),
        ("string",  {"length": 100}),  # length dropped by Databricks STRING
        ("string",  {"not_null": True}),
        ("timestamp", {}),
        ("date",    {}),
        ("decimal", {"precision": 10, "scale": 2}),
        ("decimal", {"precision": 18, "scale": 4}),
        ("decimal", {"precision": 10, "scale": 0}),
    ]:
        cases.append(("databricks", "mysql", _table("t",
            _col("id", "integer", primary_key=True, not_null=True),
            _col("val", ctype, **kwargs),
        )))

    # auto-increment variants
    cases.append(("databricks", "mysql", _table("t",
        _col("id", "integer", primary_key=True, not_null=True, auto_increment=True),
    )))
    cases.append(("databricks", "mysql", _table("t",
        _col("id", "long", primary_key=True, not_null=True, auto_increment=True),
    )))

    # multi-column
    cases.append(("databricks", "mysql", _table("events",
        _col("event_id", "long", primary_key=True, not_null=True, auto_increment=True),
        _col("name", "string", not_null=True),
        _col("score", "decimal", precision=8, scale=3),
        _col("active", "boolean", default="true"),
        _col("created_at", "timestamp", not_null=True),
    )))

    return cases


def _cases_oracle_emit_only() -> list[CanonicalTableSchema]:
    """Oracle emit-only: all canonical types should produce non-empty DDL."""
    return [
        _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("v", ctype))
        for ctype in ("integer", "long", "string", "float", "double", "boolean", "timestamp", "date")
    ] + [
        _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("v", "string", length=200)),
        _table("t", _col("id", "integer", primary_key=True, not_null=True), _col("v", "decimal", precision=10, scale=2)),
        _table("t", _col("id", "integer", primary_key=True, not_null=True, auto_increment=True), _col("v", "string")),
    ]


# Collect all parametrised cases
_ALL_PARSE_CASES = (
    _cases_mysql()
    + _cases_postgres()
    + _cases_sqlserver()
    + _cases_databricks()
)

_CASE_IDS = [
    f"{emit_d}-{tbl.name}-{','.join(c.type for c in tbl.columns)}"
    for emit_d, _parse_d, tbl in _ALL_PARSE_CASES
]


@pytest.mark.parametrize("emit_dialect,parse_dialect,table", _ALL_PARSE_CASES, ids=_CASE_IDS)
class TestPhase1SameDialectRoundtrip:
    """Phase 1 — idempotent emit → parse → emit for all types × constraints."""

    def test_roundtrip_idempotent(
        self,
        emit_dialect: str,
        parse_dialect: str,
        table: CanonicalTableSchema,
    ):
        assert_roundtrip(table, emit_dialect, parse_dialect)

    def test_ddl_non_empty(
        self,
        emit_dialect: str,
        parse_dialect: str,
        table: CanonicalTableSchema,
    ):
        ddl = emit_ddl(table, emit_dialect, if_not_exists=False)
        assert ddl.strip(), f"emit_ddl returned empty for {emit_dialect}"
        assert "CREATE TABLE" in ddl.upper()

    def test_column_count_preserved(
        self,
        emit_dialect: str,
        parse_dialect: str,
        table: CanonicalTableSchema,
    ):
        ddl = emit_ddl(table, emit_dialect, if_not_exists=False)
        parsed = parse_ddl(ddl, parse_dialect)
        assert parsed
        # Column count must be preserved (PK constraint line not counted as column)
        assert len(parsed[0].columns) == len(table.columns), (
            f"Column count changed: {[c.name for c in table.columns]} → "
            f"{[c.name for c in parsed[0].columns]}\nDDL:\n{ddl}"
        )

    def test_pk_preserved(
        self,
        emit_dialect: str,
        parse_dialect: str,
        table: CanonicalTableSchema,
    ):
        """Primary-key flag survives the round-trip."""
        ddl = emit_ddl(table, emit_dialect, if_not_exists=False)
        parsed = parse_ddl(ddl, parse_dialect)
        assert parsed
        src_pks = {c.name for c in table.columns if c.primary_key}
        dst_pks = {c.name for c in parsed[0].columns if c.primary_key}
        assert src_pks == dst_pks, (
            f"PK columns changed: {src_pks} → {dst_pks}\nDDL:\n{ddl}"
        )

    def test_not_null_preserved(
        self,
        emit_dialect: str,
        parse_dialect: str,
        table: CanonicalTableSchema,
    ):
        """NOT NULL flag survives the round-trip (PK cols are implicitly NOT NULL)."""
        ddl = emit_ddl(table, emit_dialect, if_not_exists=False)
        parsed = parse_ddl(ddl, parse_dialect)
        assert parsed
        src = {c.name: c.not_null or c.primary_key for c in table.columns}
        dst = {c.name: c.not_null or c.primary_key for c in parsed[0].columns}
        # Subset check: NOT NULL columns must stay NOT NULL
        for name, nn in src.items():
            if nn:
                assert dst.get(name, False), (
                    f"NOT NULL lost for column {name!r} [{emit_dialect}]\nDDL:\n{ddl}"
                )

    def test_precision_preserved(
        self,
        emit_dialect: str,
        parse_dialect: str,
        table: CanonicalTableSchema,
    ):
        """Decimal precision/scale and string length survive the round-trip."""
        ddl = emit_ddl(table, emit_dialect, if_not_exists=False)
        parsed = parse_ddl(ddl, parse_dialect)
        assert parsed
        src_cols = {c.name: c for c in table.columns}
        dst_cols = {c.name: c for c in parsed[0].columns}
        for name, src_col in src_cols.items():
            if name not in dst_cols:
                continue
            dst_col = dst_cols[name]
            if src_col.precision is not None:
                assert dst_col.precision == src_col.precision, (
                    f"Precision lost for {name!r} [{emit_dialect}]: "
                    f"{src_col.precision} → {dst_col.precision}\nDDL:\n{ddl}"
                )
                if src_col.scale is not None:
                    assert dst_col.scale == src_col.scale, (
                        f"Scale lost for {name!r} [{emit_dialect}]: "
                        f"{src_col.scale} → {dst_col.scale}\nDDL:\n{ddl}"
                    )
            # String length: Databricks drops length (STRING has no length param)
            if src_col.length is not None and emit_dialect != "databricks":
                assert dst_col.length == src_col.length, (
                    f"Length lost for {name!r} [{emit_dialect}]: "
                    f"{src_col.length} → {dst_col.length}\nDDL:\n{ddl}"
                )


class TestPhase1OracleEmitOnly:
    """Phase 1 — Oracle emit-only coverage (no parser yet)."""

    @pytest.mark.parametrize("table", _cases_oracle_emit_only(), ids=[
        f"oracle-{tbl.columns[-1].type}" for tbl in _cases_oracle_emit_only()
    ])
    def test_oracle_emit_non_empty(self, table):
        ddl = emit_ddl(table, "oracle", if_not_exists=False)
        assert "CREATE TABLE" in ddl.upper()
        assert len(ddl) > 20

    @pytest.mark.parametrize("table", _cases_oracle_emit_only(), ids=[
        f"oracle-idempotent-{tbl.columns[-1].type}" for tbl in _cases_oracle_emit_only()
    ])
    def test_oracle_emit_idempotent(self, table):
        """Emitting the same canonical twice gives the same string."""
        assert emit_ddl(table, "oracle", if_not_exists=False) == \
               emit_ddl(table, "oracle", if_not_exists=False)

    def test_oracle_type_names_in_ddl(self):
        """Oracle emitter uses the correct Oracle type vocabulary."""
        checks = [
            (_col("c", "integer"),   "NUMBER"),
            (_col("c", "long"),      "NUMBER"),
            (_col("c", "string", length=100),  "VARCHAR2"),
            (_col("c", "decimal", precision=10, scale=2), "NUMBER"),
            (_col("c", "timestamp"), "TIMESTAMP"),
            (_col("c", "boolean"),   "NUMBER"),
        ]
        for col, expected_type in checks:
            tbl = _table("t", _col("id", "integer", primary_key=True, not_null=True), col)
            ddl = emit_ddl(tbl, "oracle", if_not_exists=False)
            assert expected_type in ddl, (
                f"Expected {expected_type!r} in oracle DDL for type {col.type!r}:\n{ddl}"
            )


# ============================================================================
# Phase 2 — Random canonical round-trips
# ============================================================================

_CANONICAL_TYPES = ["integer", "long", "string", "float", "double", "boolean", "timestamp", "date", "decimal"]

_TYPE_KWARGS: dict[str, list[dict[str, Any]]] = {
    "integer": [{}, {"not_null": True}, {"default": "0"}, {"unique": True}],
    "long":    [{}, {"not_null": True}],
    "string":  [{}, {"length": 50}, {"length": 255, "not_null": True}, {"length": 100, "default": "'x'"}],
    "float":   [{}, {"not_null": True}],
    "double":  [{}, {"not_null": True}],
    "boolean": [{}, {"not_null": True}, {"default": "1"}],
    "timestamp": [{}, {"not_null": True}],
    "date":    [{}, {"not_null": True}],
    "decimal": [
        {"precision": 10, "scale": 2},
        {"precision": 18, "scale": 4},
        {"precision": 5,  "scale": 0},
        {"precision": 10, "scale": 2, "not_null": True, "default": "0.00"},
    ],
}

# For Databricks, boolean default should be "true"/"false"; postgres also uses "true"/"false"
_BOOL_DEFAULTS: dict[str, str] = {
    "mysql": "1",
    "postgres": "false",
    "sqlserver": "0",
    "databricks": "true",
    "oracle": "0",
}


def _make_random_table(
    rng: random.Random,
    table_name: str,
    emit_dialect: str,
) -> CanonicalTableSchema:
    """
    Generate a random CanonicalTableSchema.

    Rules
    -----
    - 1–7 non-PK columns + exactly 1 PK column.
    - PK is always integer auto_increment NOT NULL (safe across all dialects).
    - Other columns: random type, random constraint variant.
    """
    pk = _col("id", "integer", primary_key=True, not_null=True, auto_increment=True)

    n_extra = rng.randint(1, 7)
    extra_cols: list[CanonicalColumn] = []

    for i in range(n_extra):
        ctype = rng.choice(_CANONICAL_TYPES)
        kw_options = _TYPE_KWARGS[ctype]
        kw = rng.choice(kw_options).copy()

        # Fix boolean defaults per dialect
        if ctype == "boolean" and "default" in kw:
            kw["default"] = _BOOL_DEFAULTS.get(emit_dialect, "0")

        col_name = f"col_{i}_{ctype[:3]}"
        extra_cols.append(_col(col_name, ctype, **kw))

    return _table(table_name, pk, *extra_cols)


def _generate_random_tables(
    emit_dialect: str,
    parse_dialect: str,
    seed: int = 42,
    n: int = 60,
) -> list[tuple[CanonicalTableSchema, str, str]]:
    rng = random.Random(seed)
    return [
        (_make_random_table(rng, f"tbl_{i}", emit_dialect), emit_dialect, parse_dialect)
        for i in range(n)
    ]


# Pre-generate all random test cases (done once at module load)
_RANDOM_MYSQL       = _generate_random_tables("mysql",      "mysql",      seed=1, n=60)
_RANDOM_POSTGRES    = _generate_random_tables("postgres",   "postgres",   seed=2, n=60)
_RANDOM_SQLSERVER   = _generate_random_tables("sqlserver",  "sqlserver",  seed=3, n=60)
_RANDOM_DATABRICKS  = _generate_random_tables("databricks", "mysql",      seed=4, n=60)


class TestPhase2RandomRoundtrip:
    """Phase 2 — random canonical tables, full DDL round-trip coverage."""

    @pytest.mark.parametrize("table,emit_d,parse_d", _RANDOM_MYSQL,
        ids=[f"mysql-rnd-{i}" for i in range(len(_RANDOM_MYSQL))])
    def test_random_mysql(self, table, emit_d, parse_d):
        assert_roundtrip(table, emit_d, parse_d)

    @pytest.mark.parametrize("table,emit_d,parse_d", _RANDOM_POSTGRES,
        ids=[f"postgres-rnd-{i}" for i in range(len(_RANDOM_POSTGRES))])
    def test_random_postgres(self, table, emit_d, parse_d):
        assert_roundtrip(table, emit_d, parse_d)

    @pytest.mark.parametrize("table,emit_d,parse_d", _RANDOM_SQLSERVER,
        ids=[f"sqlserver-rnd-{i}" for i in range(len(_RANDOM_SQLSERVER))])
    def test_random_sqlserver(self, table, emit_d, parse_d):
        assert_roundtrip(table, emit_d, parse_d)

    @pytest.mark.parametrize("table,emit_d,parse_d", _RANDOM_DATABRICKS,
        ids=[f"databricks-rnd-{i}" for i in range(len(_RANDOM_DATABRICKS))])
    def test_random_databricks(self, table, emit_d, parse_d):
        assert_roundtrip(table, emit_d, parse_d)

    def test_all_canonical_types_covered_mysql(self):
        """Every canonical type appears at least once in the MySQL random set."""
        emitted_types: set[str] = set()
        for table, emit_d, _ in _RANDOM_MYSQL:
            for c in table.columns:
                emitted_types.add(c.type)
        assert emitted_types == set(_CANONICAL_TYPES), (
            f"Missing types in MySQL random set: {set(_CANONICAL_TYPES) - emitted_types}"
        )

    def test_all_canonical_types_covered_postgres(self):
        emitted_types: set[str] = set()
        for table, _, _ in _RANDOM_POSTGRES:
            for c in table.columns:
                emitted_types.add(c.type)
        assert emitted_types == set(_CANONICAL_TYPES)

    def test_all_canonical_types_covered_sqlserver(self):
        emitted_types: set[str] = set()
        for table, _, _ in _RANDOM_SQLSERVER:
            for c in table.columns:
                emitted_types.add(c.type)
        assert emitted_types == set(_CANONICAL_TYPES)


# ============================================================================
# Phase 3 — Statistics YAML round-trip
# ============================================================================

def _full_database_stats() -> DatabaseStats:
    """Build a DatabaseStats with every field populated."""
    mcv1 = [MostCommonValue("active", 0.65), MostCommonValue("closed", 0.30), MostCommonValue("pending", 0.05)]
    mcv2 = [MostCommonValue("1", 0.7), MostCommonValue("0", 0.3)]
    hist = ["1", "1000", "5000", "10000", "50000", "100000"]

    customers = TableStats(
        name="customers",
        schema="public",
        catalog="mydb",
        row_count=100_000,
        data_size_bytes=10_485_760,
        avg_row_bytes=104,
        last_analyzed="2026-03-01T00:00:00",
        columns=[
            ColumnStats(
                name="id",
                null_fraction=0.0,
                n_distinct=100_000.0,
                avg_width_bytes=4,
                min_value="1",
                max_value="100000",
                histogram_bounds=hist,
                correlation=0.99,
            ),
            ColumnStats(
                name="status",
                null_fraction=0.02,
                n_distinct=3.0,
                avg_width_bytes=8,
                min_value="active",
                max_value="pending",
                most_common_values=mcv1,
            ),
            ColumnStats(
                name="active",
                null_fraction=0.0,
                n_distinct=2.0,
                avg_width_bytes=1,
                most_common_values=mcv2,
            ),
        ],
        indexes=[
            IndexStats(name="pk_customers", columns=["id"], unique=True, cardinality=100_000, index_type="BTREE"),
            IndexStats(name="idx_status", columns=["status"], unique=False, cardinality=3, index_type="BTREE"),
        ],
        foreign_keys=[],
        composite_stats=[
            CompositeColumnStats(
                columns=["status", "active"],
                n_distinct=6,
                dependencies={"status->active": 0.12, "active->status": 0.05},
                most_common_combinations=[
                    MCVCombination(["active", "1"], 0.62),
                    MCVCombination(["closed", "0"], 0.28),
                ],
            )
        ],
    )

    orders = TableStats(
        name="orders",
        schema="public",
        row_count=1_000_000,
        data_size_bytes=104_857_600,
        avg_row_bytes=104,
        last_analyzed="2026-03-01T12:00:00",
        columns=[
            ColumnStats("order_id", null_fraction=0.0, n_distinct=1_000_000.0,
                        avg_width_bytes=8, min_value="1", max_value="1000000",
                        histogram_bounds=["1", "100000", "500000", "1000000"],
                        correlation=0.99),
            ColumnStats("customer_id", null_fraction=0.0, n_distinct=100_000.0,
                        avg_width_bytes=8),
            ColumnStats("amount", null_fraction=0.01, n_distinct=-0.5,
                        avg_width_bytes=8, min_value="0.01", max_value="99999.99"),
        ],
        indexes=[
            IndexStats("pk_orders", ["order_id"], unique=True, cardinality=1_000_000),
            IndexStats("fk_orders_customer", ["customer_id"], unique=False, cardinality=100_000),
        ],
        foreign_keys=[
            ForeignKeyStats(
                columns=["customer_id"],
                parent_table="customers",
                parent_schema="public",
                parent_columns=["id"],
                name="fk_orders_customer",
                cardinality_pattern="N:1",
                match_fraction=0.999,
                avg_children_per_parent=10.0,
            )
        ],
        composite_stats=[
            CompositeColumnStats(
                columns=["customer_id", "amount"],
                n_distinct=100_000,
                dependencies={"customer_id->amount": 0.0},
            )
        ],
    )

    return DatabaseStats(
        source_dialect="postgres",
        tables=[customers, orders],
    )


class TestPhase3StatsRoundtrip:
    """Phase 3 — statistics YAML and dict round-trip tests."""

    def test_to_dict_from_dict(self):
        """DatabaseStats → to_dict → from_dict → to_dict must be identical."""
        stats = _full_database_stats()
        d1 = stats.to_dict()
        stats2 = DatabaseStats.from_dict(d1)
        d2 = stats2.to_dict()
        assert d1 == d2, "Round-trip dict mismatch"

    def test_table_stats_roundtrip(self):
        """TableStats dict round-trip."""
        ts = _full_database_stats().tables[0]
        assert ts == TableStats.from_dict(ts.to_dict())

    def test_column_stats_roundtrip(self):
        """ColumnStats with all fields round-trips exactly."""
        cs = ColumnStats(
            name="val",
            null_fraction=0.03,
            n_distinct=1500.0,
            avg_width_bytes=12,
            min_value="0.01",
            max_value="9999.99",
            most_common_values=[MostCommonValue("100.00", 0.15), MostCommonValue("0.00", 0.08)],
            histogram_bounds=["0.01", "50.00", "200.00", "9999.99"],
            correlation=-0.05,
        )
        assert cs == ColumnStats.from_dict(cs.to_dict())

    def test_column_stats_zero_n_distinct_omitted(self):
        """n_distinct=0 (unknown) is omitted from serialised dict."""
        cs = ColumnStats(name="x", null_fraction=0.0)
        d = cs.to_dict()
        assert "n_distinct" not in d

    def test_index_stats_roundtrip(self):
        idx = IndexStats("pk", ["a", "b"], unique=True, cardinality=5000, index_type="BTREE")
        assert idx == IndexStats.from_dict(idx.to_dict())

    def test_fk_stats_roundtrip(self):
        fk = ForeignKeyStats(
            columns=["order_id", "line_no"],
            parent_table="orders",
            parent_schema="dbo",
            parent_columns=["id", "line_no"],
            name="fk_lines_order",
            cardinality_pattern="N:1",
            match_fraction=0.998,
            avg_children_per_parent=5.5,
        )
        assert fk == ForeignKeyStats.from_dict(fk.to_dict())

    def test_composite_stats_roundtrip(self):
        cs = CompositeColumnStats(
            columns=["a", "b"],
            n_distinct=500,
            dependencies={"a->b": 0.8, "b->a": 0.1},
            most_common_combinations=[MCVCombination(["x", "y"], 0.4)],
        )
        assert cs == CompositeColumnStats.from_dict(cs.to_dict())

    def test_most_common_value_roundtrip(self):
        mcv = MostCommonValue("hello world", 0.1234)
        assert mcv == MostCommonValue.from_dict(mcv.to_dict())

    def test_mcv_combination_roundtrip(self):
        combo = MCVCombination(["foo", "bar", "baz"], 0.07)
        assert combo == MCVCombination.from_dict(combo.to_dict())

    def test_file_dump_and_load(self, tmp_path):
        """dump_stats / load_stats YAML file round-trip."""
        stats = _full_database_stats()
        path = tmp_path / "stats.yaml"
        dump_stats(stats, path)
        assert path.exists()
        loaded = load_stats(path)
        assert stats.to_dict() == loaded.to_dict()

    def test_load_from_dict(self):
        stats = _full_database_stats()
        d = stats.to_dict()
        loaded = load_stats(d)
        assert stats.to_dict() == loaded.to_dict()

    def test_yaml_is_human_readable(self, tmp_path):
        """The dumped YAML file should be plain text with readable field names."""
        stats = _full_database_stats()
        path = tmp_path / "stats.yaml"
        dump_stats(stats, path)
        content = path.read_text(encoding="utf-8")
        assert "row_count:" in content
        assert "null_fraction:" in content
        assert "n_distinct:" in content
        assert "source_dialect:" in content

    def test_version_field_preserved(self):
        stats = DatabaseStats(version="2.0", tables=[])
        d = stats.to_dict()
        assert d["version"] == "2.0"
        stats2 = DatabaseStats.from_dict(d)
        assert stats2.version == "2.0"

    def test_empty_database_stats_roundtrip(self):
        stats = DatabaseStats()
        assert stats == DatabaseStats.from_dict(stats.to_dict())

    def test_table_stats_lookup(self):
        stats = _full_database_stats()
        assert stats.table_stats("orders") is not None
        assert stats.table_stats("nonexistent") is None
        assert stats.table_stats("customers").row_count == 100_000

    def test_column_stats_lookup(self):
        ts = _full_database_stats().tables[0]
        assert ts.column_stats("id") is not None
        assert ts.column_stats("id").n_distinct == 100_000.0
        assert ts.column_stats("missing") is None

    def test_negative_n_distinct_postgres_convention(self):
        """PostgreSQL uses negative n_distinct for fraction-of-rows values."""
        cs = ColumnStats(name="col", n_distinct=-1.0)
        d = cs.to_dict()
        assert d["n_distinct"] == -1.0
        cs2 = ColumnStats.from_dict(d)
        assert cs2.n_distinct == -1.0

    def test_make_default_stats_all_types(self):
        """make_default_stats generates valid stats for all canonical types."""
        schema = _table("all_types",
            _col("id", "integer", primary_key=True, not_null=True),
            _col("lg", "long"),
            _col("s", "string", length=100),
            _col("f", "float"),
            _col("d", "double"),
            _col("b", "boolean"),
            _col("ts", "timestamp"),
            _col("dt", "date"),
            _col("dec", "decimal", precision=10, scale=2),
        )
        db_stats = make_default_stats([schema], row_count=5_000)
        assert db_stats.tables
        ts = db_stats.tables[0]
        assert ts.row_count == 5_000
        # Every column should have stats
        col_names = {cs.name for cs in ts.columns}
        for col in schema.columns:
            assert col.name in col_names, f"Column {col.name!r} missing from default stats"
        # PK column should have n_distinct == row_count
        id_stats = ts.column_stats("id")
        assert id_stats is not None
        assert id_stats.n_distinct == 5_000.0
        assert id_stats.null_fraction == 0.0

    def test_make_default_stats_pk_index(self):
        """make_default_stats generates a PK index entry."""
        schema = _table("t",
            _col("id", "integer", primary_key=True, not_null=True),
            _col("name", "string"),
        )
        ts = make_default_stats([schema]).tables[0]
        pk_indexes = [i for i in ts.indexes if i.unique]
        assert pk_indexes, "No unique (PK) index in default stats"
        assert "id" in pk_indexes[0].columns

    def test_make_default_stats_yaml_roundtrip(self):
        """make_default_stats → YAML round-trip."""
        schema = _table("t",
            _col("id", "integer", primary_key=True, not_null=True),
            _col("v", "decimal", precision=8, scale=2),
        )
        stats = make_default_stats([schema])
        d1 = stats.to_dict()
        stats2 = DatabaseStats.from_dict(d1)
        assert d1 == stats2.to_dict()

    def test_make_default_stats_with_fk_constraints(self):
        """make_default_stats propagates FK constraints into ForeignKeyStats."""
        schema = CanonicalTableSchema(
            name="orders",
            columns=[
                _col("id", "integer", primary_key=True, not_null=True),
                _col("customer_id", "integer", not_null=True),
            ],
            fk_constraints=[
                CanonicalForeignKey(
                    columns=["customer_id"],
                    parent_table="customers",
                    parent_columns=["id"],
                    name="fk_orders_customer",
                )
            ],
        )
        ts = make_default_stats([schema]).tables[0]
        assert ts.foreign_keys, "FK stats missing"
        fk = ts.foreign_keys[0]
        assert fk.parent_table == "customers"
        assert fk.columns == ["customer_id"]


# ============================================================================
# Phase 4 — Override application
# ============================================================================

def _sample_schema() -> CanonicalTableSchema:
    return CanonicalTableSchema(
        name="orders",
        columns=[
            _col("order_id", "long", primary_key=True, not_null=True, auto_increment=True),
            _col("customer_id", "long", not_null=True),
            _col("status", "string", length=20, not_null=True, default="'pending'"),
            _col("total", "decimal", precision=10, scale=2, not_null=True),
            _col("is_paid", "boolean"),
            _col("notes", "string"),
            _col("order_date", "date", not_null=True),
        ],
        fk_constraints=[
            CanonicalForeignKey(
                columns=["customer_id"],
                parent_table="customers",
                parent_schema="public",
                parent_columns=["id"],
            )
        ],
    )


def _sample_stats() -> TableStats:
    return TableStats(
        name="orders",
        schema="public",
        row_count=500_000,
        columns=[
            ColumnStats("order_id", null_fraction=0.0, n_distinct=500_000.0),
            ColumnStats("customer_id", null_fraction=0.0, n_distinct=50_000.0),
            ColumnStats("status", null_fraction=0.0, n_distinct=4.0,
                        most_common_values=[MostCommonValue("pending", 0.5), MostCommonValue("shipped", 0.3)]),
            ColumnStats("total", null_fraction=0.0, n_distinct=-0.5,
                        min_value="0.01", max_value="9999.99"),
            ColumnStats("is_paid", null_fraction=0.0, n_distinct=2.0),
            ColumnStats("notes", null_fraction=0.4, n_distinct=-0.1),
            ColumnStats("order_date", null_fraction=0.0, n_distinct=-0.05),
        ],
    )


class TestPhase4Overrides:
    """Phase 4 — OverrideSpec application and YAML round-trip."""

    # ── YAML serialisation ───────────────────────────────────────────────────

    def test_override_spec_empty_roundtrip(self):
        spec = OverrideSpec()
        assert spec.to_dict() == {}
        spec2 = OverrideSpec.from_dict({})
        assert spec2.description is None

    def test_override_spec_dict_roundtrip(self):
        spec = OverrideSpec(
            description="test override",
            schema_mappings={"public": "app"},
            catalog_mappings={"mydb": "main"},
            uppercase_identifiers=True,
            type_mappings={"boolean": "integer"},
            row_count_scale=0.01,
            tables={
                "orders": TableOverride(
                    rename_to="ORDERS",
                    row_count=5000,
                    columns={
                        "order_date": ColumnOverride(rename_to="ORDER_DT", type="timestamp"),
                        "status": ColumnOverride(n_distinct=3, null_fraction=0.0),
                    },
                )
            },
        )
        d = spec.to_dict()
        spec2 = OverrideSpec.from_dict(d)
        assert spec2.to_dict() == d

    def test_override_spec_yaml_file_roundtrip(self, tmp_path):
        spec = OverrideSpec(
            description="yaml file test",
            schema_mappings={"dbo": "main"},
            row_count_scale=0.1,
        )
        path = tmp_path / "override.yaml"
        spec.to_yaml(path)
        spec2 = OverrideSpec.from_yaml(path)
        assert spec2.to_dict() == spec.to_dict()

    # ── Table rename ─────────────────────────────────────────────────────────

    def test_table_rename(self):
        spec = OverrideSpec(tables={"orders": TableOverride(rename_to="ORDERS")})
        schema, stats = apply_overrides(_sample_schema(), _sample_stats(), spec)
        assert schema.name == "ORDERS"
        assert stats.name == "ORDERS"

    def test_table_rename_no_stats(self):
        spec = OverrideSpec(tables={"orders": TableOverride(rename_to="tbl_orders")})
        schema, stats = apply_overrides(_sample_schema(), None, spec)
        assert schema.name == "tbl_orders"
        assert stats is None

    # ── Column rename ────────────────────────────────────────────────────────

    def test_column_rename(self):
        spec = OverrideSpec(tables={"orders": TableOverride(
            columns={"order_date": ColumnOverride(rename_to="ORDER_DT")}
        )})
        schema, stats = apply_overrides(_sample_schema(), _sample_stats(), spec)
        names = [c.name for c in schema.columns]
        assert "ORDER_DT" in names
        assert "order_date" not in names
        # Stats column renamed too
        stat_names = [cs.name for cs in stats.columns]
        assert "ORDER_DT" in stat_names
        assert "order_date" not in stat_names

    def test_column_rename_propagates_to_fk(self):
        """FK column list is updated when FK columns are renamed."""
        spec = OverrideSpec(tables={"orders": TableOverride(
            columns={"customer_id": ColumnOverride(rename_to="cust_id")}
        )})
        schema, _ = apply_overrides(_sample_schema(), None, spec)
        assert schema.fk_constraints is not None
        fk = schema.fk_constraints[0]
        assert "cust_id" in fk.columns
        assert "customer_id" not in fk.columns

    # ── Type override ────────────────────────────────────────────────────────

    def test_column_type_override(self):
        spec = OverrideSpec(tables={"orders": TableOverride(
            columns={"is_paid": ColumnOverride(type="integer")}
        )})
        schema, _ = apply_overrides(_sample_schema(), None, spec)
        col = next(c for c in schema.columns if c.name == "is_paid")
        assert col.type == "integer"

    def test_global_type_mapping(self):
        spec = OverrideSpec(type_mappings={"boolean": "integer"})
        schema, _ = apply_overrides(_sample_schema(), None, spec)
        bool_cols = [c for c in schema.columns if c.name == "is_paid"]
        assert bool_cols[0].type == "integer"

    def test_column_type_override_beats_global_mapping(self):
        """Column-level type override takes precedence over global type_mappings."""
        spec = OverrideSpec(
            type_mappings={"boolean": "integer"},
            tables={"orders": TableOverride(
                columns={"is_paid": ColumnOverride(type="long")}
            )},
        )
        schema, _ = apply_overrides(_sample_schema(), None, spec)
        col = next(c for c in schema.columns if c.name == "is_paid")
        # Column-level wins (long beats global integer mapping of boolean)
        assert col.type == "long"

    # ── Precision / length overrides ─────────────────────────────────────────

    def test_column_length_override(self):
        spec = OverrideSpec(tables={"orders": TableOverride(
            columns={"status": ColumnOverride(length=50)}
        )})
        schema, _ = apply_overrides(_sample_schema(), None, spec)
        col = next(c for c in schema.columns if c.name == "status")
        assert col.length == 50

    def test_column_precision_scale_override(self):
        spec = OverrideSpec(tables={"orders": TableOverride(
            columns={"total": ColumnOverride(precision=14, scale=4)}
        )})
        schema, _ = apply_overrides(_sample_schema(), None, spec)
        col = next(c for c in schema.columns if c.name == "total")
        assert col.precision == 14
        assert col.scale == 4

    # ── Row count scaling ────────────────────────────────────────────────────

    def test_global_row_count_scale(self):
        spec = OverrideSpec(row_count_scale=0.01)
        _, stats = apply_overrides(_sample_schema(), _sample_stats(), spec)
        assert stats.row_count == 5_000  # 500_000 * 0.01

    def test_table_level_row_count_explicit(self):
        spec = OverrideSpec(tables={"orders": TableOverride(row_count=1_000)})
        _, stats = apply_overrides(_sample_schema(), _sample_stats(), spec)
        assert stats.row_count == 1_000

    def test_table_level_row_count_scale(self):
        spec = OverrideSpec(tables={"orders": TableOverride(row_count_scale=0.1)})
        _, stats = apply_overrides(_sample_schema(), _sample_stats(), spec)
        assert stats.row_count == 50_000  # 500_000 * 0.1

    def test_table_explicit_beats_global_scale(self):
        spec = OverrideSpec(
            row_count_scale=0.001,
            tables={"orders": TableOverride(row_count=9_999)},
        )
        _, stats = apply_overrides(_sample_schema(), _sample_stats(), spec)
        assert stats.row_count == 9_999  # explicit beats global

    def test_row_count_scale_min_one(self):
        """row_count never falls below 1."""
        spec = OverrideSpec(row_count_scale=0.000001)
        _, stats = apply_overrides(_sample_schema(), _sample_stats(), spec)
        assert stats.row_count >= 1

    # ── Schema / catalog remapping ───────────────────────────────────────────

    def test_schema_mapping(self):
        spec = OverrideSpec(schema_mappings={"public": "app"})
        _, stats = apply_overrides(_sample_schema(), _sample_stats(), spec)
        assert stats.schema == "app"

    def test_catalog_mapping(self):
        stats_with_catalog = copy.deepcopy(_sample_stats())
        stats_with_catalog.catalog = "mydb"
        spec = OverrideSpec(catalog_mappings={"mydb": "main"})
        _, stats = apply_overrides(_sample_schema(), stats_with_catalog, spec)
        assert stats.catalog == "main"

    def test_schema_mapping_propagates_to_fk(self):
        """FK parent_schema is remapped through schema_mappings."""
        spec = OverrideSpec(schema_mappings={"public": "app"})
        schema, _ = apply_overrides(_sample_schema(), None, spec)
        assert schema.fk_constraints[0].parent_schema == "app"

    # ── Identifier case normalisation ────────────────────────────────────────

    def test_uppercase_identifiers(self):
        spec = OverrideSpec(uppercase_identifiers=True)
        schema, _ = apply_overrides(_sample_schema(), None, spec)
        assert schema.name == "ORDERS"
        for col in schema.columns:
            assert col.name == col.name.upper(), f"Column {col.name!r} not uppercase"

    def test_lowercase_identifiers(self):
        schema_upper = CanonicalTableSchema(
            name="ORDERS",
            columns=[_col("ORDER_ID", "integer", primary_key=True, not_null=True)],
        )
        spec = OverrideSpec(lowercase_identifiers=True)
        schema, _ = apply_overrides(schema_upper, None, spec)
        assert schema.name == "orders"
        assert schema.columns[0].name == "order_id"

    # ── Reserved word prefixing ──────────────────────────────────────────────

    def test_reserved_word_prefix_oracle(self):
        schema = CanonicalTableSchema(
            name="sessions",
            columns=[
                _col("date", "date", primary_key=True, not_null=True),
                _col("order", "integer"),
                _col("value", "decimal", precision=8, scale=2),
            ],
        )
        spec = OverrideSpec(
            reserved_word_prefix="_",
            reserved_words={"date", "order", "value"},
        )
        result, _ = apply_overrides(schema, None, spec)
        names = {c.name for c in result.columns}
        assert "_date" in names
        assert "_order" in names
        assert "_value" in names
        assert "date" not in names

    def test_oracle_builtin_reserved_words(self):
        """OverrideSpec.get_reserved_words('oracle') returns a non-empty set."""
        spec = OverrideSpec(reserved_word_prefix="_")
        rw = spec.get_reserved_words("oracle")
        assert "date" in rw
        assert "order" in rw
        assert "select" in rw

    def test_postgres_builtin_reserved_words(self):
        spec = OverrideSpec()
        rw = spec.get_reserved_words("postgres")
        assert "select" in rw
        assert "table" in rw

    def test_sqlserver_builtin_reserved_words(self):
        spec = OverrideSpec()
        rw = spec.get_reserved_words("sqlserver")
        assert "identity" in rw
        assert "select" in rw

    # ── Statistical overrides ────────────────────────────────────────────────

    def test_column_null_fraction_override(self):
        spec = OverrideSpec(tables={"orders": TableOverride(
            columns={"notes": ColumnOverride(null_fraction=0.1)}
        )})
        _, stats = apply_overrides(_sample_schema(), _sample_stats(), spec)
        assert stats.column_stats("notes").null_fraction == 0.1

    def test_column_n_distinct_override(self):
        spec = OverrideSpec(tables={"orders": TableOverride(
            columns={"status": ColumnOverride(n_distinct=3)}
        )})
        _, stats = apply_overrides(_sample_schema(), _sample_stats(), spec)
        assert stats.column_stats("status").n_distinct == 3.0

    def test_column_min_max_override(self):
        spec = OverrideSpec(tables={"orders": TableOverride(
            columns={"total": ColumnOverride(min_value="1.00", max_value="100.00")}
        )})
        _, stats = apply_overrides(_sample_schema(), _sample_stats(), spec)
        cs = stats.column_stats("total")
        assert cs.min_value == "1.00"
        assert cs.max_value == "100.00"

    def test_column_mcv_override(self):
        spec = OverrideSpec(tables={"orders": TableOverride(
            columns={"status": ColumnOverride(
                most_common_values=[
                    {"value": "active", "frequency": 0.8},
                    {"value": "closed", "frequency": 0.2},
                ]
            )}
        )})
        _, stats = apply_overrides(_sample_schema(), _sample_stats(), spec)
        mcvs = stats.column_stats("status").most_common_values
        assert len(mcvs) == 2
        assert mcvs[0].value == "active"
        assert mcvs[0].frequency == 0.8

    # ── apply_overrides_all ──────────────────────────────────────────────────

    def test_apply_overrides_all(self):
        schemas = [_sample_schema()]
        db_stats = DatabaseStats(tables=[_sample_stats()], source_dialect="postgres")
        spec = OverrideSpec(
            schema_mappings={"public": "prod"},
            row_count_scale=0.1,
        )
        new_schemas, new_db_stats = apply_overrides_all(schemas, db_stats, spec)
        assert len(new_schemas) == 1
        assert new_db_stats is not None
        assert new_db_stats.tables[0].row_count == 50_000
        assert new_db_stats.tables[0].schema == "prod"

    def test_apply_overrides_all_no_stats(self):
        schemas = [_sample_schema()]
        new_schemas, new_db_stats = apply_overrides_all(schemas, None, OverrideSpec())
        assert len(new_schemas) == 1
        assert new_db_stats is None

    # ── Idempotency of overrides ─────────────────────────────────────────────

    def test_override_does_not_mutate_input(self):
        """apply_overrides must not modify the original schema/stats objects."""
        schema = _sample_schema()
        stats = _sample_stats()
        original_name = schema.name
        original_row_count = stats.row_count

        spec = OverrideSpec(
            tables={"orders": TableOverride(rename_to="NEW_ORDERS", row_count=99)},
        )
        apply_overrides(schema, stats, spec)

        assert schema.name == original_name, "Original schema was mutated"
        assert stats.row_count == original_row_count, "Original stats were mutated"

    def test_combined_migration_scenario_oracle(self):
        """
        Simulate a complete MySQL→Oracle migration scenario:
        - Uppercase all identifiers
        - Boolean → NUMBER(1)
        - Reserve-word prefix for 'order'
        - Scale to 1% for testing
        - Schema: public → MYSCHEMA
        """
        schema = CanonicalTableSchema(
            name="order",   # reserved word in Oracle
            columns=[
                _col("id", "integer", primary_key=True, not_null=True, auto_increment=True),
                _col("status", "string", length=20, not_null=True),
                _col("active", "boolean", not_null=True),
                _col("amount", "decimal", precision=10, scale=2),
            ],
        )
        stats = TableStats(name="order", schema="public", row_count=1_000_000,
                           columns=[ColumnStats(c.name, null_fraction=0.0) for c in schema.columns])

        spec = OverrideSpec(
            description="MySQL → Oracle migration test",
            uppercase_identifiers=True,
            reserved_word_prefix="_",
            reserved_words={"order", "date", "comment", "select"},
            type_mappings={"boolean": "integer"},
            schema_mappings={"public": "MYSCHEMA"},
            row_count_scale=0.01,
        )
        new_schema, new_stats = apply_overrides(schema, stats, spec, target_dialect="oracle")

        assert new_schema.name == "_ORDER"          # reserved + uppercase
        assert new_stats.schema == "MYSCHEMA"       # schema remapped
        assert new_stats.row_count == 10_000        # 1_000_000 * 0.01
        col_types = {c.name: c.type for c in new_schema.columns}
        assert col_types["ACTIVE"] == "integer"     # boolean → integer via global map
        assert col_types["STATUS"] == "string"
        assert col_types["AMOUNT"] == "decimal"

    def test_combined_migration_scenario_databricks(self):
        """
        Simulate SQL Server → Databricks migration:
        - Lowercase identifiers
        - Schema: dbo → default
        - FLOAT stays double (already canonical; no change)
        - Row count 10%
        """
        schema = CanonicalTableSchema(
            name="SalesOrder",
            columns=[
                _col("OrderID", "long", primary_key=True, not_null=True),
                _col("Amount", "double"),
                _col("IsPaid", "boolean"),
            ],
        )
        stats = TableStats(name="SalesOrder", schema="dbo", row_count=200_000,
                           columns=[ColumnStats(c.name) for c in schema.columns])

        spec = OverrideSpec(
            lowercase_identifiers=True,
            schema_mappings={"dbo": "default"},
            row_count_scale=0.1,
        )
        new_schema, new_stats = apply_overrides(schema, stats, spec)

        assert new_schema.name == "salesorder"
        col_names = [c.name for c in new_schema.columns]
        assert "orderid" in col_names
        assert "amount" in col_names
        assert new_stats.schema == "default"
        assert new_stats.row_count == 20_000


# ============================================================================
# Phase 5 — Source type coverage
#
# For every type in MYSQL_TYPE_TO_CANONICAL, POSTGRES_TYPE_TO_CANONICAL, and
# SQLSERVER_TYPE_TO_CANONICAL, verify:
#   (a) The raw DDL type is parsed to the documented canonical type.
#   (b) emit(canonical, dialect) → parse → emit is idempotent (parse-first).
# ============================================================================

def _wrap_col_ddl(col_sql: str, dialect: str) -> str:
    """Wrap a column definition in a minimal CREATE TABLE for the given dialect."""
    if dialect == "mysql":
        return f"CREATE TABLE `t` (\n  `id` INT NOT NULL,\n  `col` {col_sql},\n  PRIMARY KEY (`id`)\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;"
    if dialect == "postgres":
        return f'CREATE TABLE "t" (\n  "id" INTEGER NOT NULL,\n  "col" {col_sql},\n  PRIMARY KEY ("id")\n);'
    if dialect == "sqlserver":
        return f"CREATE TABLE [t] (\n  [id] INT NOT NULL,\n  [col] {col_sql},\n  PRIMARY KEY ([id])\n);"
    raise ValueError(f"No DDL wrapper for dialect {dialect!r}")


# ---------------------------------------------------------------------------
# Type specs: (sql_type_str, expected_canonical)
# sql_type_str is exactly what appears in the source CREATE TABLE DDL.
# ---------------------------------------------------------------------------

_MYSQL_SOURCE_TYPES: list[tuple[str, str]] = [
    ("TINYINT",              "integer"),
    ("TINYINT(1)",           "boolean"),    # de-facto BOOLEAN alias
    ("SMALLINT",             "integer"),
    ("MEDIUMINT",            "integer"),
    ("INT",                  "integer"),
    ("INTEGER",              "integer"),
    ("BIGINT",               "long"),
    ("DECIMAL(10,2)",        "decimal"),
    ("DECIMAL(1,0)",         "decimal"),    # minimum precision
    ("DECIMAL(38,0)",        "decimal"),    # max precision
    ("DECIMAL(38,37)",       "decimal"),    # max scale
    ("NUMERIC(5,3)",         "decimal"),
    ("FLOAT",                "float"),
    ("DOUBLE",               "double"),
    # sqlglot normalises MySQL REAL to DT.FLOAT (same as FLOAT) → "float"
    ("REAL",                 "float"),
    ("BIT",                  "boolean"),
    ("BOOLEAN",              "boolean"),
    ("BOOL",                 "boolean"),
    ("CHAR(10)",             "string"),
    ("VARCHAR(1)",           "string"),     # minimum length
    ("VARCHAR(255)",         "string"),
    ("VARCHAR(65535)",       "string"),     # MySQL theoretical max
    ("BINARY(16)",           "binary"),   # was "string" — binary bytes, not character string
    ("VARBINARY(255)",       "binary"),   # was "string"
    # MySQL blob types
    ("TINYBLOB",             "binary"),
    ("BLOB",                 "binary"),
    ("MEDIUMBLOB",           "binary"),
    ("LONGBLOB",             "binary"),
    # Oracle types via MySQL fallback
    ("NUMBER(10,2)",         "decimal"),
    ("NUMBER(5)",            "integer"),  # NUMBER(p) no scale, p≤9 → integer
    ("NUMBER(15)",           "long"),     # NUMBER(p) no scale, 10≤p≤18 → long
    ("VARCHAR2(100)",        "string"),
    ("CLOB",                 "string"),
    ("NCLOB",                "string"),
    ("BLOB",                 "binary"),   # Oracle BLOB via fallback
    ("RAW(200)",             "binary"),
    ("NVARCHAR2(50)",        "string"),
    ("TINYTEXT",             "string"),
    ("TEXT",                 "string"),
    ("MEDIUMTEXT",           "string"),
    ("LONGTEXT",             "string"),
    ("JSON",                 "string"),
    ("JSONB",                "string"),     # added for Databricks DDL fallback
    ("STRING",               "string"),     # Databricks STRING type via MySQL parser
    ("DATE",                 "date"),
    ("DATETIME",             "timestamp"),    # no timezone conversion
    ("TIMESTAMP",            "timestamptz"),  # UTC storage, session-TZ display
    ("TIMESTAMP_NTZ",        "timestamp"),    # Databricks: no timezone (Delta 2.0+)
    ("TIME",                 "time"),         # was "string" — now canonical time type
    ("YEAR",                 "integer"),
    ("ENUM('a','b','c')",    "string"),
    ("SET('x','y')",         "string"),
]

_POSTGRES_SOURCE_TYPES: list[tuple[str, str]] = [
    ("SMALLINT",                        "integer"),
    ("INTEGER",                         "integer"),
    ("INT",                             "integer"),
    ("INT2",                            "integer"),
    ("INT4",                            "integer"),
    ("BIGINT",                          "long"),
    ("INT8",                            "long"),
    ("DECIMAL(10,2)",                   "decimal"),
    ("DECIMAL(1,0)",                    "decimal"),    # minimum
    ("DECIMAL(38,0)",                   "decimal"),    # max precision
    ("DECIMAL(38,37)",                  "decimal"),    # max scale
    ("NUMERIC(5,3)",                    "decimal"),
    ("REAL",                            "float"),
    ("FLOAT4",                          "float"),
    ("DOUBLE PRECISION",                "double"),
    ("FLOAT8",                          "double"),
    ("BOOLEAN",                         "boolean"),
    ("BOOL",                            "boolean"),
    ("CHARACTER VARYING(1)",            "string"),     # minimum length
    ("CHARACTER VARYING(255)",          "string"),
    ("CHARACTER VARYING(10485760)",     "string"),     # PG max
    ("VARCHAR(100)",                    "string"),
    ("CHARACTER(10)",                   "string"),
    ("CHAR(5)",                         "string"),
    ("TEXT",                            "string"),
    ("JSON",                            "string"),
    ("JSONB",                           "string"),
    ("DATE",                            "date"),
    ("TIMESTAMP",                       "timestamp"),
    ("TIMESTAMP WITHOUT TIME ZONE",     "timestamp"),
    ("TIMESTAMP WITH TIME ZONE",        "timestamptz"),
    ("TIMESTAMPTZ",                     "timestamptz"),
    ("TIME",                            "time"),
    ("TIME WITHOUT TIME ZONE",          "time"),
    ("TIME WITH TIME ZONE",             "timetz"),
    ("TIMETZ",                          "timetz"),
    ("INTERVAL",                        "string"),
    # Binary / blob
    ("BYTEA",                           "binary"),
    ("BIT(8)",                          "binary"),
    ("BIT VARYING(8)",                  "binary"),
    ("VARBIT(8)",                       "binary"),
    # String network / misc
    ("UUID",                            "uuid"),
    ("XML",                             "string"),
    ("CIDR",                            "string"),
    ("INET",                            "string"),
    ("MACADDR",                         "string"),
    ("MACADDR8",                        "string"),
    # Money (fixed precision)
    ("MONEY",                           "decimal"),
    # Auto-increment shorthands
    ("SERIAL",                          "integer"),
    ("SMALLSERIAL",                     "integer"),
    ("BIGSERIAL",                       "long"),
]

_SQLSERVER_SOURCE_TYPES: list[tuple[str, str]] = [
    ("TINYINT",          "integer"),
    ("SMALLINT",         "integer"),
    ("INT",              "integer"),
    ("BIGINT",           "long"),
    ("DECIMAL(10,2)",    "decimal"),
    ("DECIMAL(1,0)",     "decimal"),    # minimum
    ("DECIMAL(38,0)",    "decimal"),    # max precision
    ("DECIMAL(38,37)",   "decimal"),    # max scale
    ("NUMERIC(5,3)",     "decimal"),
    # sqlglot normalises both SS FLOAT and SS REAL to DT.FLOAT → "float"
    ("FLOAT",            "float"),
    ("REAL",             "float"),
    ("BIT",              "boolean"),
    ("CHAR(10)",         "string"),
    ("VARCHAR(1)",       "string"),     # minimum
    ("VARCHAR(255)",     "string"),
    ("NCHAR(10)",        "string"),
    ("NVARCHAR(1)",      "string"),     # minimum
    ("NVARCHAR(100)",    "string"),
    ("NVARCHAR(4000)",   "string"),     # SQL Server NVARCHAR max
    ("NVARCHAR(MAX)",    "string"),     # MAX → no stored length
    ("TEXT",             "string"),
    ("NTEXT",            "string"),
    ("DATE",             "date"),
    ("DATETIME",         "timestamp"),
    ("DATETIME2",        "timestamp"),
    ("SMALLDATETIME",    "timestamp"),
    ("DATETIMEOFFSET",   "timestamptz"),
    ("TIME",             "time"),
    ("UNIQUEIDENTIFIER", "uuid"),
    # Binary types
    ("BINARY(16)",       "binary"),
    ("VARBINARY(255)",   "binary"),
    ("IMAGE",            "binary"),         # legacy LOB type
    ("ROWVERSION",       "binary"),         # 8-byte row-version counter (NOT a datetime!)
    ("TIMESTAMP",        "binary"),         # SQL Server TIMESTAMP = ROWVERSION, NOT a date!
    ("GEOGRAPHY",        "binary"),
    ("GEOMETRY",         "binary"),
    # Money (fixed precision)
    ("MONEY",            "decimal"),        # DECIMAL(19,4)
    ("SMALLMONEY",       "decimal"),        # DECIMAL(10,4)
    # String special types
    ("XML",              "string"),
    ("SQL_VARIANT",      "string"),
    ("HIERARCHYID",      "string"),
]


def _source_type_id(item: tuple[str, str]) -> str:
    """Stable pytest ID for a (sql_type, canonical) pair."""
    return item[0].replace("(", "_").replace(")", "").replace(",", "_").replace(" ", "_").lower()


@pytest.mark.parametrize("sql_type,expected_canonical", _MYSQL_SOURCE_TYPES,
    ids=[_source_type_id(x) for x in _MYSQL_SOURCE_TYPES])
class TestPhase5MySQLSourceTypes:
    """Phase 5 — every type in MYSQL_TYPE_TO_CANONICAL parses to its documented canonical."""

    def test_canonical_type(self, sql_type: str, expected_canonical: str):
        ddl = _wrap_col_ddl(sql_type, "mysql")
        tables = parse_ddl(ddl, "mysql")
        assert tables, f"No tables parsed from:\n{ddl}"
        col = tables[0].columns[1]   # col 0 = id, col 1 = data col
        assert col.type == expected_canonical, (
            f"MySQL {sql_type!r} → expected canonical {expected_canonical!r}, "
            f"got {col.type!r}"
        )

    def test_parse_first_idempotent(self, sql_type: str, expected_canonical: str):
        ddl = _wrap_col_ddl(sql_type, "mysql")
        assert_parse_first_roundtrip(ddl, "mysql")


@pytest.mark.parametrize("sql_type,expected_canonical", _POSTGRES_SOURCE_TYPES,
    ids=[_source_type_id(x) for x in _POSTGRES_SOURCE_TYPES])
class TestPhase5PostgresSourceTypes:
    """Phase 5 — every type in POSTGRES_TYPE_TO_CANONICAL parses to its documented canonical."""

    def test_canonical_type(self, sql_type: str, expected_canonical: str):
        ddl = _wrap_col_ddl(sql_type, "postgres")
        tables = parse_ddl(ddl, "postgres")
        assert tables, f"No tables parsed from:\n{ddl}"
        col = tables[0].columns[1]
        assert col.type == expected_canonical, (
            f"PostgreSQL {sql_type!r} → expected canonical {expected_canonical!r}, "
            f"got {col.type!r}"
        )

    def test_parse_first_idempotent(self, sql_type: str, expected_canonical: str):
        # SERIAL / BIGSERIAL are auto-increment shorthand; special-cased in emitter
        ddl = _wrap_col_ddl(sql_type, "postgres")
        assert_parse_first_roundtrip(ddl, "postgres")


@pytest.mark.parametrize("sql_type,expected_canonical", _SQLSERVER_SOURCE_TYPES,
    ids=[_source_type_id(x) for x in _SQLSERVER_SOURCE_TYPES])
class TestPhase5SQLServerSourceTypes:
    """Phase 5 — every type in SQLSERVER_TYPE_TO_CANONICAL parses to its documented canonical."""

    def test_canonical_type(self, sql_type: str, expected_canonical: str):
        ddl = _wrap_col_ddl(sql_type, "sqlserver")
        tables = parse_ddl(ddl, "sqlserver")
        assert tables, f"No tables parsed from:\n{ddl}"
        col = tables[0].columns[1]
        assert col.type == expected_canonical, (
            f"SQL Server {sql_type!r} → expected canonical {expected_canonical!r}, "
            f"got {col.type!r}"
        )

    def test_parse_first_idempotent(self, sql_type: str, expected_canonical: str):
        ddl = _wrap_col_ddl(sql_type, "sqlserver")
        assert_parse_first_roundtrip(ddl, "sqlserver")


class TestPhase5TypeMapCompleteness:
    """
    Verify that every key in the parser type maps is exercised by the
    source-type parametrized test lists above.
    """

    def test_mysql_type_map_fully_covered(self):
        tested = {t.split("(")[0].strip().lower() for t, _ in _MYSQL_SOURCE_TYPES}
        # ENUM/SET are listed as 'enum(...)' and 'set(...)' — normalise
        tested.add("enum")
        tested.add("set")
        untested = set(MYSQL_TYPE_TO_CANONICAL) - tested
        assert not untested, (
            f"MySQL types in MYSQL_TYPE_TO_CANONICAL but not in _MYSQL_SOURCE_TYPES: "
            f"{sorted(untested)}"
        )

    def test_postgres_type_map_fully_covered(self):
        tested = {t.split("(")[0].strip().lower() for t, _ in _POSTGRES_SOURCE_TYPES}
        # Multi-word normalise (keep full key)
        tested |= {t.lower().split("(")[0].strip() for t, _ in _POSTGRES_SOURCE_TYPES}
        untested = set(POSTGRES_TYPE_TO_CANONICAL) - tested
        assert not untested, (
            f"PG types in POSTGRES_TYPE_TO_CANONICAL but not in _POSTGRES_SOURCE_TYPES: "
            f"{sorted(untested)}"
        )

    def test_sqlserver_type_map_fully_covered(self):
        tested = {t.split("(")[0].strip().lower() for t, _ in _SQLSERVER_SOURCE_TYPES}
        untested = set(SQLSERVER_TYPE_TO_CANONICAL) - tested
        assert not untested, (
            f"SQL Server types in SQLSERVER_TYPE_TO_CANONICAL but not in "
            f"_SQLSERVER_SOURCE_TYPES: {sorted(untested)}"
        )


# ============================================================================
# Phase 6 — Precision, scale, and length boundary tests
#
# Verifies that min and max boundary values for precision, scale, and length
# survive the canonical model → emit → parse → emit round-trip exactly.
# ============================================================================

# Decimal precision/scale boundaries shared across dialects
# Format: (precision, scale, description)
_DECIMAL_BOUNDARIES: list[tuple[int, int, str]] = [
    (1,  0,  "min_precision"),
    (1,  1,  "scale_equals_precision_min"),   # edge: scale == precision (PG/Oracle allow it)
    (5,  5,  "scale_equals_precision_5"),
    (10, 0,  "mid_precision_no_scale"),
    (10, 2,  "common_money"),
    (18, 4,  "common_default"),
    (18, 18, "scale_equals_precision_18"),
    (38, 0,  "max_precision_no_scale"),
    (38, 37, "max_precision_near_max_scale"),
    (38, 38, "max_precision_max_scale"),       # edge: scale == precision at max
]

# String length boundaries: (length, description)
_STRING_LENGTH_BOUNDARIES: list[tuple[int, str]] = [
    (1,      "min_length"),
    (10,     "small"),
    (255,    "common_max_255"),
    (256,    "just_over_255"),
    (4000,   "sqlserver_nvarchar_max"),
    (8000,   "sqlserver_varchar_max"),
    (65535,  "mysql_varchar_theoretical_max"),
]


class TestPhase6DecimalBoundaries:
    """Phase 6 — DECIMAL precision/scale boundary round-trips per dialect."""

    @pytest.mark.parametrize("precision,scale,desc", _DECIMAL_BOUNDARIES,
        ids=[d for _, _, d in _DECIMAL_BOUNDARIES])
    @pytest.mark.parametrize("dialect", ["mysql", "postgres", "sqlserver"])
    def test_decimal_boundary_roundtrip(self, dialect, precision, scale, desc):
        table = make_pk_table("t", make_col("v", "decimal", precision=precision, scale=scale))
        assert_ddl_roundtrip(table, dialect)

    @pytest.mark.parametrize("precision,scale,desc", _DECIMAL_BOUNDARIES,
        ids=[d for _, _, d in _DECIMAL_BOUNDARIES])
    @pytest.mark.parametrize("dialect", ["mysql", "postgres", "sqlserver"])
    def test_decimal_precision_preserved(self, dialect, precision, scale, desc):
        table = make_pk_table("t", make_col("v", "decimal", precision=precision, scale=scale))
        ddl = emit_ddl(table, dialect, if_not_exists=False)
        tables = parse_ddl(ddl, dialect)
        assert tables
        col = tables[0].columns[1]
        assert col.precision == precision, (
            f"[{dialect}] DECIMAL({precision},{scale}) precision changed to {col.precision}\nDDL: {ddl}"
        )
        assert col.scale == scale, (
            f"[{dialect}] DECIMAL({precision},{scale}) scale changed to {col.scale}\nDDL: {ddl}"
        )

    @pytest.mark.parametrize("precision,scale,desc", _DECIMAL_BOUNDARIES,
        ids=[d for _, _, d in _DECIMAL_BOUNDARIES])
    def test_decimal_boundary_oracle_emit(self, precision, scale, desc):
        """Oracle emit-only: boundary values produce valid NUMBER(p,s) syntax."""
        table = make_pk_table("t", make_col("v", "decimal", precision=precision, scale=scale))
        ddl = emit_ddl(table, "oracle", if_not_exists=False)
        assert f"NUMBER({precision},{scale})" in ddl, (
            f"Oracle DECIMAL({precision},{scale}) not in DDL:\n{ddl}"
        )

    @pytest.mark.parametrize("precision,scale,desc", _DECIMAL_BOUNDARIES,
        ids=[d for _, _, d in _DECIMAL_BOUNDARIES])
    def test_decimal_boundary_databricks_emit(self, precision, scale, desc):
        """Databricks emit: boundary values produce DECIMAL(p,s) syntax."""
        table = make_pk_table("t", make_col("v", "decimal", precision=precision, scale=scale))
        ddl = emit_ddl(table, "databricks", if_not_exists=False)
        assert f"DECIMAL({precision},{scale})" in ddl, (
            f"Databricks DECIMAL({precision},{scale}) not in DDL:\n{ddl}"
        )


class TestPhase6StringLengthBoundaries:
    """Phase 6 — VARCHAR / string length boundary round-trips per dialect."""

    @pytest.mark.parametrize("length,desc", _STRING_LENGTH_BOUNDARIES,
        ids=[d for _, d in _STRING_LENGTH_BOUNDARIES])
    @pytest.mark.parametrize("dialect", ["mysql", "postgres", "sqlserver"])
    def test_string_length_roundtrip(self, dialect, length, desc):
        table = make_pk_table("t", make_col("v", "string", length=length))
        assert_ddl_roundtrip(table, dialect)

    @pytest.mark.parametrize("length,desc", _STRING_LENGTH_BOUNDARIES,
        ids=[d for _, d in _STRING_LENGTH_BOUNDARIES])
    @pytest.mark.parametrize("dialect", ["mysql", "postgres", "sqlserver"])
    def test_string_length_preserved(self, dialect, length, desc):
        table = make_pk_table("t", make_col("v", "string", length=length))
        ddl = emit_ddl(table, dialect, if_not_exists=False)
        tables = parse_ddl(ddl, dialect)
        assert tables
        col = tables[0].columns[1]
        assert col.length == length, (
            f"[{dialect}] VARCHAR({length}) length changed to {col.length}\nDDL: {ddl}"
        )

    @pytest.mark.parametrize("length,desc", _STRING_LENGTH_BOUNDARIES,
        ids=[d for _, d in _STRING_LENGTH_BOUNDARIES])
    def test_string_length_oracle_emit(self, length, desc):
        """Oracle: VARCHAR2(n) emitted with correct length."""
        table = make_pk_table("t", make_col("v", "string", length=length))
        ddl = emit_ddl(table, "oracle", if_not_exists=False)
        assert f"VARCHAR2({length})" in ddl, f"Oracle VARCHAR2({length}) not in DDL:\n{ddl}"

    def test_no_length_string_defaults(self):
        """No-length string uses dialect-appropriate default type."""
        table = make_pk_table("t", make_col("v", "string"))
        assert "TEXT"          in emit_ddl(table, "mysql",      if_not_exists=False)
        assert "TEXT"          in emit_ddl(table, "postgres",   if_not_exists=False)
        assert "NVARCHAR(MAX)" in emit_ddl(table, "sqlserver",  if_not_exists=False)
        assert "STRING"        in emit_ddl(table, "databricks", if_not_exists=False)
        assert "CLOB"          in emit_ddl(table, "oracle",     if_not_exists=False)

    def test_no_length_string_roundtrips(self):
        """No-length string round-trips to TEXT/NVARCHAR(MAX)/STRING idempotently."""
        table = make_pk_table("t", make_col("v", "string"))
        for dialect in ("mysql", "postgres", "sqlserver"):
            assert_ddl_roundtrip(table, dialect)


class TestPhase6DefaultExpressions:
    """Phase 6 — various DEFAULT expression values survive the round-trip."""

    _DEFAULTS: list[tuple[str, str, str]] = [
        # (canonical_type, default_expr, description)
        ("integer",   "0",                  "int_zero"),
        ("integer",   "1",                  "int_one"),
        ("integer",   "42",                 "int_value"),
        ("long",      "0",                  "long_zero"),
        ("string",    "'active'",           "string_quoted"),
        ("string",    "'pending order'",    "string_quoted_space"),
        ("boolean",   "1",                  "bool_one_mysql"),
        ("decimal",   "0.00",               "decimal_zero"),
        ("decimal",   "1.5",                "decimal_value"),
        ("timestamp", "CURRENT_TIMESTAMP",  "ts_current"),
    ]

    @pytest.mark.parametrize("ctype,default,desc", _DEFAULTS, ids=[d for _, _, d in _DEFAULTS])
    def test_mysql_default_roundtrip(self, ctype, default, desc):
        kw: dict = {"default": default, "not_null": True}
        if ctype == "decimal":
            kw.update({"precision": 10, "scale": 2})
        table = make_pk_table("t", make_col("v", ctype, **kw))
        assert_ddl_roundtrip(table, "mysql")

    _PG_DEFAULTS = [(ct, dv, dc) for ct, dv, dc in _DEFAULTS if ct != "boolean"]
    _SS_DEFAULTS = [(ct, dv, dc) for ct, dv, dc in _DEFAULTS if ct not in ("boolean", "timestamp")]

    @pytest.mark.parametrize("ctype,default,desc", _PG_DEFAULTS,
        ids=[d for _, _, d in _PG_DEFAULTS])
    def test_postgres_default_roundtrip(self, ctype, default, desc):
        kw: dict = {"default": default, "not_null": True}
        if ctype == "decimal":
            kw.update({"precision": 10, "scale": 2})
        table = make_pk_table("t", make_col("v", ctype, **kw))
        assert_ddl_roundtrip(table, "postgres")

    @pytest.mark.parametrize("ctype,default,desc", _SS_DEFAULTS,
        ids=[d for _, _, d in _SS_DEFAULTS])
    def test_sqlserver_default_roundtrip(self, ctype, default, desc):
        kw: dict = {"default": default, "not_null": True}
        if ctype == "decimal":
            kw.update({"precision": 10, "scale": 2})
        table = make_pk_table("t", make_col("v", ctype, **kw))
        assert_ddl_roundtrip(table, "sqlserver")


# ============================================================================
# Phase 7 — Structural edge cases
# ============================================================================

class TestPhase7StructuralEdgeCases:
    """Phase 7 — multi-column PK, FK parsing, auto-increment variants, edge identifiers."""

    # ── Multi-column primary key ─────────────────────────────────────────────

    @pytest.mark.parametrize("dialect,parse_d", [
        ("mysql",     "mysql"),
        ("postgres",  "postgres"),
        ("sqlserver", "sqlserver"),
        ("databricks","mysql"),
    ])
    def test_multi_column_pk(self, dialect, parse_d):
        """Composite PK survives emit → parse."""
        table = CanonicalTableSchema(
            name="order_lines",
            columns=[
                make_col("order_id", "long", primary_key=True, not_null=True),
                make_col("line_no",  "integer", primary_key=True, not_null=True),
                make_col("qty",      "integer", not_null=True, default="1"),
                make_col("price",    "decimal", precision=10, scale=2),
            ],
        )
        ddl = emit_ddl(table, dialect, if_not_exists=False)
        tables = parse_ddl(ddl, parse_d)
        assert tables, f"No tables parsed [{dialect}]"
        pk_cols = {c.name for c in tables[0].columns if c.primary_key}
        assert pk_cols == {"order_id", "line_no"}, (
            f"Multi-col PK not preserved [{dialect}]: got {pk_cols}\nDDL:\n{ddl}"
        )

    def test_multi_column_pk_idempotent_mysql(self):
        table = CanonicalTableSchema(
            name="x",
            columns=[
                make_col("a", "integer", primary_key=True, not_null=True),
                make_col("b", "integer", primary_key=True, not_null=True),
                make_col("v", "string", length=50),
            ],
        )
        assert_ddl_roundtrip(table, "mysql")

    def test_multi_column_pk_idempotent_postgres(self):
        table = CanonicalTableSchema(
            name="x",
            columns=[
                make_col("a", "long", primary_key=True, not_null=True),
                make_col("b", "long", primary_key=True, not_null=True),
                make_col("v", "string"),
            ],
        )
        assert_ddl_roundtrip(table, "postgres")

    def test_multi_column_pk_idempotent_sqlserver(self):
        table = CanonicalTableSchema(
            name="x",
            columns=[
                make_col("a", "integer", primary_key=True, not_null=True),
                make_col("b", "integer", primary_key=True, not_null=True),
                make_col("v", "string", length=100),
            ],
        )
        assert_ddl_roundtrip(table, "sqlserver")

    # ── FK parsing from raw DDL ───────────────────────────────────────────────

    def test_fk_parsed_from_mysql_ddl(self):
        ddl = """
        CREATE TABLE `orders` (
          `order_id` BIGINT NOT NULL AUTO_INCREMENT,
          `customer_id` BIGINT NOT NULL,
          `status` VARCHAR(20) NOT NULL,
          PRIMARY KEY (`order_id`),
          CONSTRAINT `fk_orders_customer` FOREIGN KEY (`customer_id`)
            REFERENCES `customers` (`id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """
        tables = parse_ddl(ddl, "mysql")
        assert tables
        table = tables[0]
        assert table.fk_constraints, "No FK constraints parsed"
        fk = table.fk_constraints[0]
        assert fk.columns == ["customer_id"]
        assert fk.parent_table == "customers"
        assert fk.parent_columns == ["id"]
        assert fk.name == "fk_orders_customer"

    def test_fk_parsed_from_postgres_ddl(self):
        ddl = """
        CREATE TABLE orders (
          order_id BIGINT NOT NULL,
          customer_id BIGINT NOT NULL,
          status CHARACTER VARYING(20),
          PRIMARY KEY (order_id),
          CONSTRAINT fk_orders_cust FOREIGN KEY (customer_id) REFERENCES customers (id)
        );
        """
        tables = parse_ddl(ddl, "postgres")
        assert tables
        fk = tables[0].fk_constraints[0]
        assert fk.parent_table == "customers"
        assert fk.columns == ["customer_id"]

    def test_composite_fk_parsed(self):
        """Multi-column FOREIGN KEY constraint is parsed correctly."""
        ddl = """
        CREATE TABLE `order_lines` (
          `order_id` BIGINT NOT NULL,
          `line_no` INT NOT NULL,
          `product_id` INT NOT NULL,
          FOREIGN KEY (`order_id`, `line_no`) REFERENCES `orders` (`order_id`, `line_no`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """
        tables = parse_ddl(ddl, "mysql")
        assert tables
        fk = tables[0].fk_constraints[0]
        assert fk.columns == ["order_id", "line_no"]
        assert fk.parent_columns == ["order_id", "line_no"]

    def test_inline_fk_without_constraint_name(self):
        """FOREIGN KEY without a CONSTRAINT name is parsed correctly."""
        ddl = """
        CREATE TABLE `orders` (
          `id` INT NOT NULL,
          `user_id` INT NOT NULL,
          PRIMARY KEY (`id`),
          FOREIGN KEY (`user_id`) REFERENCES `users` (`id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """
        tables = parse_ddl(ddl, "mysql")
        assert tables
        assert tables[0].fk_constraints
        fk = tables[0].fk_constraints[0]
        assert fk.name is None
        assert fk.parent_table == "users"

    def test_cross_schema_fk_parent(self):
        """FK referencing schema.table preserves the parent schema."""
        ddl = """
        CREATE TABLE orders (
          id INTEGER NOT NULL,
          cust_id INTEGER NOT NULL,
          PRIMARY KEY (id),
          FOREIGN KEY (cust_id) REFERENCES public.customers (id)
        );
        """
        tables = parse_ddl(ddl, "postgres")
        assert tables
        fk = tables[0].fk_constraints[0]
        assert fk.parent_table == "customers"
        assert fk.parent_schema == "public"

    # ── Auto-increment variants ───────────────────────────────────────────────

    def test_mysql_auto_increment_parsed(self):
        ddl = "CREATE TABLE `t` (`id` INT NOT NULL AUTO_INCREMENT, PRIMARY KEY (`id`)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;"
        tables = parse_ddl(ddl, "mysql")
        assert tables[0].columns[0].auto_increment is True

    def test_postgres_serial_auto_increment_parsed(self):
        ddl = 'CREATE TABLE "t" ("id" SERIAL NOT NULL, PRIMARY KEY ("id"));'
        tables = parse_ddl(ddl, "postgres")
        assert tables[0].columns[0].auto_increment is True
        assert tables[0].columns[0].type == "integer"

    def test_postgres_bigserial_auto_increment_parsed(self):
        ddl = 'CREATE TABLE "t" ("id" BIGSERIAL NOT NULL, PRIMARY KEY ("id"));'
        tables = parse_ddl(ddl, "postgres")
        assert tables[0].columns[0].auto_increment is True
        assert tables[0].columns[0].type == "long"

    def test_sqlserver_identity_auto_increment_parsed(self):
        ddl = "CREATE TABLE [t] ([id] INT IDENTITY(1,1) NOT NULL, PRIMARY KEY ([id]));"
        tables = parse_ddl(ddl, "sqlserver")
        assert tables[0].columns[0].auto_increment is True

    # ── NOT NULL and UNIQUE preservation ─────────────────────────────────────

    @pytest.mark.parametrize("dialect", ["mysql", "postgres", "sqlserver"])
    def test_not_null_roundtrip(self, dialect):
        table = make_pk_table("t",
            make_col("required", "string", length=100, not_null=True),
            make_col("optional", "string", length=100),
        )
        ddl = emit_ddl(table, dialect, if_not_exists=False)
        tables = parse_ddl(ddl, dialect)
        assert tables
        cols = {c.name: c for c in tables[0].columns}
        assert cols["required"].not_null is True
        assert cols["optional"].not_null is False

    @pytest.mark.parametrize("dialect", ["mysql", "postgres", "sqlserver"])
    def test_unique_roundtrip(self, dialect):
        table = make_pk_table("t",
            make_col("email", "string", length=255, unique=True, not_null=True),
            make_col("notes", "string"),
        )
        ddl = emit_ddl(table, dialect, if_not_exists=False)
        tables = parse_ddl(ddl, dialect)
        assert tables
        email_col = next(c for c in tables[0].columns if c.name == "email")
        assert email_col.unique is True

    # ── Single-column table ───────────────────────────────────────────────────

    @pytest.mark.parametrize("dialect", ["mysql", "postgres", "sqlserver"])
    def test_single_column_pk_only(self, dialect):
        """Table with only a PK column round-trips."""
        table = make_table("ids", make_col("id", "long", primary_key=True, not_null=True))
        assert_ddl_roundtrip(table, dialect)

    # ── Wide table (many columns) ─────────────────────────────────────────────

    def test_wide_table_mysql(self):
        """Table with all 9 canonical types round-trips in MySQL."""
        table = CanonicalTableSchema(name="wide", columns=[
            make_col("id",    "integer",   primary_key=True, not_null=True, auto_increment=True),
            make_col("lng",   "long",      not_null=True),
            make_col("name",  "string",    length=200, not_null=True),
            make_col("note",  "string"),
            make_col("flt",   "float"),
            make_col("dbl",   "double"),
            make_col("flag",  "boolean",   not_null=True, default="0"),
            make_col("ts",    "timestamp", not_null=True),
            make_col("dt",    "date"),
            make_col("amt",   "decimal",   precision=12, scale=4, not_null=True),
        ])
        assert_ddl_roundtrip(table, "mysql")

    def test_wide_table_postgres(self):
        table = CanonicalTableSchema(name="wide", columns=[
            make_col("id",    "integer",   primary_key=True, not_null=True, auto_increment=True),
            make_col("lng",   "long",      not_null=True, auto_increment=False),
            make_col("name",  "string",    length=200, not_null=True),
            make_col("note",  "string"),
            make_col("flt",   "float"),
            make_col("dbl",   "double"),
            make_col("flag",  "boolean",   not_null=True, default="false"),
            make_col("ts",    "timestamp", not_null=True),
            make_col("dt",    "date"),
            make_col("amt",   "decimal",   precision=12, scale=4, not_null=True),
        ])
        assert_ddl_roundtrip(table, "postgres")

    def test_wide_table_sqlserver(self):
        table = CanonicalTableSchema(name="wide", columns=[
            make_col("id",    "integer",   primary_key=True, not_null=True, auto_increment=True),
            make_col("lng",   "long",      not_null=True),
            make_col("name",  "string",    length=200, not_null=True),
            make_col("note",  "string"),
            make_col("flt",   "float"),
            make_col("dbl",   "double"),
            make_col("flag",  "boolean",   not_null=True, default="0"),
            make_col("ts",    "timestamp", not_null=True),
            make_col("dt",    "date"),
            make_col("amt",   "decimal",   precision=12, scale=4, not_null=True),
        ])
        assert_ddl_roundtrip(table, "sqlserver")

    # ── Table name edge cases ─────────────────────────────────────────────────

    @pytest.mark.parametrize("dialect,parse_d", [
        ("mysql", "mysql"), ("postgres", "postgres"), ("sqlserver", "sqlserver")
    ])
    def test_table_name_with_underscore(self, dialect, parse_d):
        table = make_pk_table("my_long_table_name_v2", make_col("v", "integer"))
        assert_ddl_roundtrip(table, dialect, parse_d)

    @pytest.mark.parametrize("dialect,parse_d", [
        ("mysql", "mysql"), ("postgres", "postgres"), ("sqlserver", "sqlserver")
    ])
    def test_table_name_starts_with_digit_like(self, dialect, parse_d):
        """Table name starting with underscore survives round-trip."""
        table = make_pk_table("_staging_table", make_col("v", "integer"))
        assert_ddl_roundtrip(table, dialect, parse_d)


# ============================================================================
# Phase 8 — Temporal types: time zones and fractional-seconds precision
# ============================================================================

class TestPhase8TemporalTypes:
    """
    Full temporal-type coverage:
      • timestamptz vs timestamp (timezone-aware vs naive)
      • time and timetz (time-of-day with and without zone)
      • fsp (fractional-seconds precision) 0–9 round-trips
      • TIMESTAMP(n) WITH TIME ZONE parsed correctly
      • Cross-dialect emission for all five target dialects
    """

    # ── Canonical type → emitted SQL for every dialect ───────────────────

    @pytest.mark.parametrize("canonical,dialect,expected_sql", [
        # timestamp (no timezone)
        ("timestamp",   "mysql",      "DATETIME"),
        ("timestamp",   "postgres",   "TIMESTAMP"),
        ("timestamp",   "sqlserver",  "DATETIME2"),
        ("timestamp",   "oracle",     "TIMESTAMP"),
        ("timestamp",   "databricks", "TIMESTAMP_NTZ"),
        # timestamptz (timezone-aware)
        ("timestamptz", "mysql",      "TIMESTAMP"),
        ("timestamptz", "postgres",   "TIMESTAMP WITH TIME ZONE"),
        ("timestamptz", "sqlserver",  "DATETIMEOFFSET"),
        ("timestamptz", "oracle",     "TIMESTAMP WITH TIME ZONE"),
        ("timestamptz", "databricks", "TIMESTAMP"),
        # time (time-of-day)
        ("time",        "mysql",      "TIME"),
        ("time",        "postgres",   "TIME"),
        ("time",        "sqlserver",  "TIME"),
        ("time",        "oracle",     "TIMESTAMP"),   # Oracle has no native TIME
        ("time",        "databricks", "STRING"),       # Databricks has no native TIME
        # timetz (time-of-day with timezone)
        ("timetz",      "mysql",      "TIME"),         # MySQL has no timetz; degrade
        ("timetz",      "postgres",   "TIME WITH TIME ZONE"),
        ("timetz",      "sqlserver",  "TIME"),         # SS has no timetz; degrade
        ("timetz",      "oracle",     "TIMESTAMP WITH TIME ZONE"),
        ("timetz",      "databricks", "STRING"),
    ])
    def test_emit_no_fsp(self, canonical: str, dialect: str, expected_sql: str):
        """No-fsp canonical type emits to the correct default SQL token."""
        from src.statschema import emit_ddl
        col = make_col("ts", canonical)
        table = make_pk_table("t", col)
        ddl = emit_ddl(table, dialect)
        assert expected_sql in ddl, (
            f"Expected {expected_sql!r} in {dialect} DDL for canonical {canonical!r}.\n{ddl}"
        )

    # ── FSP round-trip: emit fsp → parse → re-emit fsp ───────────────────

    @pytest.mark.parametrize("canonical,fsp,dialect,parse_d", [
        # MySQL: DATETIME(fsp) and TIMESTAMP(fsp)
        ("timestamp",   0, "mysql",     "mysql"),
        ("timestamp",   3, "mysql",     "mysql"),
        ("timestamp",   6, "mysql",     "mysql"),    # max for MySQL
        ("timestamptz", 0, "mysql",     "mysql"),
        ("timestamptz", 6, "mysql",     "mysql"),
        ("time",        0, "mysql",     "mysql"),
        ("time",        6, "mysql",     "mysql"),
        # PostgreSQL: TIMESTAMP(fsp) / TIMESTAMP(fsp) WITH TIME ZONE / TIME(fsp)
        ("timestamp",   0, "postgres",  "postgres"),
        ("timestamp",   3, "postgres",  "postgres"),
        ("timestamp",   6, "postgres",  "postgres"),
        ("timestamptz", 0, "postgres",  "postgres"),
        ("timestamptz", 3, "postgres",  "postgres"),
        ("timestamptz", 6, "postgres",  "postgres"),
        ("time",        0, "postgres",  "postgres"),
        ("time",        6, "postgres",  "postgres"),
        ("timetz",      3, "postgres",  "postgres"),
        ("timetz",      6, "postgres",  "postgres"),
        # SQL Server: DATETIME2(fsp) / DATETIMEOFFSET(fsp) / TIME(fsp)
        ("timestamp",   0, "sqlserver", "sqlserver"),
        ("timestamp",   3, "sqlserver", "sqlserver"),
        ("timestamp",   7, "sqlserver", "sqlserver"),   # SS max (100-nanosecond)
        ("timestamptz", 0, "sqlserver", "sqlserver"),
        ("timestamptz", 7, "sqlserver", "sqlserver"),
        ("time",        0, "sqlserver", "sqlserver"),
        ("time",        7, "sqlserver", "sqlserver"),
        # Oracle emit-only: TIMESTAMP(fsp) / TIMESTAMP(fsp) WITH TIME ZONE
        # (parse-first round-trip skipped — no Oracle parser)
        # Databricks: fsp is not supported in DDL; stored fsp is dropped on emit
        ("timestamp",   6, "databricks", "mysql"),
        ("timestamptz", 6, "databricks", "mysql"),
    ])
    def test_fsp_roundtrip(
        self, canonical: str, fsp: int, dialect: str, parse_d: str
    ):
        """Emit fsp → parse → re-emit must be idempotent."""
        col = make_col("ts", canonical)
        col.fsp = fsp
        table = make_pk_table("t", col)
        assert_ddl_roundtrip(table, dialect, parse_d)

    @pytest.mark.parametrize("canonical,fsp,dialect", [
        ("timestamp",   0, "mysql"),
        ("timestamp",   6, "mysql"),
        ("timestamptz", 6, "mysql"),
        ("time",        6, "mysql"),
        ("timestamp",   3, "postgres"),
        ("timestamptz", 3, "postgres"),
        ("time",        3, "postgres"),
        ("timetz",      3, "postgres"),
        ("timestamp",   7, "sqlserver"),
        ("timestamptz", 7, "sqlserver"),
        ("time",        7, "sqlserver"),
        ("timestamp",   6, "oracle"),
        ("timestamptz", 6, "oracle"),
    ])
    def test_fsp_preserved_in_ddl(self, canonical: str, fsp: int, dialect: str):
        """The emitted DDL must contain the fsp value as '(N)' when supported."""
        from src.statschema import emit_ddl
        col = make_col("ts", canonical)
        col.fsp = fsp
        table = make_pk_table("t", col)
        ddl = emit_ddl(table, dialect)
        assert f"({fsp})" in ddl, (
            f"Expected fsp=({fsp}) in {dialect} DDL for {canonical!r}.\n{ddl}"
        )

    def test_databricks_fsp_dropped(self):
        """Databricks does not support fsp; the emitted DDL must NOT contain '(6)'."""
        from src.statschema import emit_ddl
        col = make_col("ts", "timestamp")
        col.fsp = 6
        table = make_pk_table("t", col)
        ddl = emit_ddl(table, "databricks")
        assert "(6)" not in ddl, f"fsp should be dropped for Databricks:\n{ddl}"

    # ── TIMESTAMP(n) WITH TIME ZONE parsed correctly ──────────────────────

    @pytest.mark.parametrize("raw_type,dialect,expected_canonical,expected_fsp", [
        # PostgreSQL multi-word types
        ("TIMESTAMP WITH TIME ZONE",          "postgres", "timestamptz", None),
        ("TIMESTAMP(0) WITH TIME ZONE",       "postgres", "timestamptz", 0),
        ("TIMESTAMP(3) WITH TIME ZONE",       "postgres", "timestamptz", 3),
        ("TIMESTAMP(6) WITH TIME ZONE",       "postgres", "timestamptz", 6),
        ("TIMESTAMP WITHOUT TIME ZONE",       "postgres", "timestamp",   None),
        ("TIMESTAMP(3) WITHOUT TIME ZONE",    "postgres", "timestamp",   3),
        ("TIMESTAMPTZ",                       "postgres", "timestamptz", None),
        ("TIME WITH TIME ZONE",               "postgres", "timetz",      None),
        ("TIME(3) WITH TIME ZONE",            "postgres", "timetz",      3),
        ("TIME WITHOUT TIME ZONE",            "postgres", "time",        None),
        ("TIME(6) WITHOUT TIME ZONE",         "postgres", "time",        6),
        ("TIMETZ",                            "postgres", "timetz",      None),
        # MySQL: plain TIMESTAMP → timestamptz, DATETIME → timestamp
        ("TIMESTAMP",                         "mysql",    "timestamptz", None),
        ("TIMESTAMP(6)",                      "mysql",    "timestamptz", 6),
        ("DATETIME",                          "mysql",    "timestamp",   None),
        ("DATETIME(3)",                       "mysql",    "timestamp",   3),
        ("TIME(6)",                           "mysql",    "time",        6),
        # MySQL via Databricks fallback
        ("TIMESTAMP_NTZ",                     "mysql",    "timestamp",   None),
        # SQL Server
        ("DATETIMEOFFSET",                    "sqlserver", "timestamptz", None),
        ("DATETIMEOFFSET(7)",                 "sqlserver", "timestamptz", 7),
        ("DATETIME2",                         "sqlserver", "timestamp",   None),
        ("DATETIME2(7)",                      "sqlserver", "timestamp",   7),
        ("TIME(7)",                           "sqlserver", "time",        7),
    ])
    def test_parse_temporal_canonical(
        self,
        raw_type: str,
        dialect: str,
        expected_canonical: str,
        expected_fsp,
    ):
        """Raw temporal DDL parses to correct canonical type and fsp."""
        ddl = _wrap_col_ddl(raw_type, dialect)
        tables = parse_ddl(ddl, dialect)
        assert tables, f"No tables parsed from:\n{ddl}"
        col = tables[0].columns[1]
        assert col.type == expected_canonical, (
            f"{dialect} {raw_type!r} → expected canonical {expected_canonical!r}, "
            f"got {col.type!r}"
        )
        assert col.fsp == expected_fsp, (
            f"{dialect} {raw_type!r} → expected fsp={expected_fsp!r}, "
            f"got {col.fsp!r}"
        )

    # ── Parse-first round-trip for temporal types ─────────────────────────

    @pytest.mark.parametrize("raw_type,dialect", [
        ("TIMESTAMP WITH TIME ZONE",       "postgres"),
        ("TIMESTAMP(3) WITH TIME ZONE",    "postgres"),
        ("TIMESTAMP(6) WITH TIME ZONE",    "postgres"),
        ("TIMESTAMP WITHOUT TIME ZONE",    "postgres"),
        ("TIMESTAMP(3) WITHOUT TIME ZONE", "postgres"),
        ("TIMESTAMPTZ",                    "postgres"),
        ("TIME WITH TIME ZONE",            "postgres"),
        ("TIME(3) WITH TIME ZONE",         "postgres"),
        ("TIME WITHOUT TIME ZONE",         "postgres"),
        ("TIMETZ",                         "postgres"),
        ("TIMESTAMP",                      "mysql"),
        ("TIMESTAMP(6)",                   "mysql"),
        ("DATETIME(3)",                    "mysql"),
        ("TIME(6)",                        "mysql"),
        ("TIMESTAMP_NTZ",                  "mysql"),
        ("DATETIMEOFFSET(7)",              "sqlserver"),
        ("DATETIME2(7)",                   "sqlserver"),
        ("TIME(7)",                        "sqlserver"),
    ])
    def test_parse_first_idempotent(self, raw_type: str, dialect: str):
        """parse(raw_ddl) → emit → parse → emit is idempotent."""
        ddl = _wrap_col_ddl(raw_type, dialect)
        assert_parse_first_roundtrip(ddl, dialect)

    # ── Oracle emit-only: verify fsp surface for each temporal type ───────

    @pytest.mark.parametrize("canonical,fsp,expected_fragment", [
        ("timestamp",   None, "TIMESTAMP"),
        ("timestamp",   6,    "TIMESTAMP(6)"),
        ("timestamptz", None, "TIMESTAMP WITH TIME ZONE"),
        ("timestamptz", 9,    "TIMESTAMP(9) WITH TIME ZONE"),   # Oracle supports nanoseconds
        ("time",        None, "TIMESTAMP"),     # degraded to TIMESTAMP
        ("time",        6,    "TIMESTAMP(6)"),
        ("timetz",      None, "TIMESTAMP WITH TIME ZONE"),
    ])
    def test_oracle_emit(self, canonical: str, fsp, expected_fragment: str):
        """Oracle emitter produces correct temporal DDL fragments."""
        from src.statschema import emit_ddl
        col = make_col("ts", canonical)
        col.fsp = fsp
        table = make_pk_table("t", col)
        ddl = emit_ddl(table, "oracle")
        assert expected_fragment in ddl, (
            f"Oracle emit of canonical={canonical!r} fsp={fsp!r}: "
            f"expected {expected_fragment!r}.\n{ddl}"
        )

    # ── Every valid fsp value for every temporal type × dialect ─────────
    #
    # MySQL/PG: fsp 0–6 (microseconds)   SQL Server: fsp 0–7 (100-ns)
    # Covers fsp=4 and all other intermediate values not reached by
    # the targeted tests above.

    @pytest.mark.parametrize("canonical,fsp", [
        (canonical, fsp)
        for canonical in ("timestamp", "timestamptz", "time")
        for fsp in range(7)           # 0, 1, 2, 3, 4, 5, 6
    ])
    def test_mysql_all_fsp_values(self, canonical: str, fsp: int):
        """Every fsp 0–6 for every temporal canonical type round-trips on MySQL."""
        col = make_col("ts", canonical)
        col.fsp = fsp
        assert_ddl_roundtrip(make_pk_table("t", col), "mysql", "mysql")

    @pytest.mark.parametrize("canonical,fsp", [
        (canonical, fsp)
        for canonical in ("timestamp", "timestamptz", "time", "timetz")
        for fsp in range(7)           # 0, 1, 2, 3, 4, 5, 6
    ])
    def test_postgres_all_fsp_values(self, canonical: str, fsp: int):
        """Every fsp 0–6 for every temporal canonical type round-trips on PostgreSQL."""
        col = make_col("ts", canonical)
        col.fsp = fsp
        assert_ddl_roundtrip(make_pk_table("t", col), "postgres", "postgres")

    @pytest.mark.parametrize("canonical,fsp", [
        (canonical, fsp)
        for canonical in ("timestamp", "timestamptz", "time")
        for fsp in range(8)           # 0, 1, 2, 3, 4, 5, 6, 7
    ])
    def test_sqlserver_all_fsp_values(self, canonical: str, fsp: int):
        """Every fsp 0–7 (100-nanosecond resolution) for every temporal type on SQL Server."""
        col = make_col("ts", canonical)
        col.fsp = fsp
        assert_ddl_roundtrip(make_pk_table("t", col), "sqlserver", "sqlserver")

    @pytest.mark.parametrize("canonical,fsp", [
        (canonical, fsp)
        for canonical in ("timestamp", "timestamptz")
        for fsp in range(10)          # 0–9 (nanoseconds)
    ])
    def test_oracle_all_fsp_emit(self, canonical: str, fsp: int):
        """Oracle accepts fsp 0–9; every value emits the correct fragment.

        When fsp is stored (even fsp=0), the emitter always includes (N) in the
        type string: TIMESTAMP(0), TIMESTAMP(6) WITH TIME ZONE, etc.
        """
        from src.statschema import emit_ddl
        col = make_col("ts", canonical)
        col.fsp = fsp
        ddl = emit_ddl(make_pk_table("t", col), "oracle")
        expected = (
            f"TIMESTAMP({fsp}) WITH TIME ZONE"
            if canonical == "timestamptz"
            else f"TIMESTAMP({fsp})"
        )
        assert expected in ddl, (
            f"Oracle {canonical} fsp={fsp}: expected {expected!r}.\n{ddl}"
        )

    # ── Parse every fsp value from raw DDL ───────────────────────────────

    @pytest.mark.parametrize("raw_type,dialect", [
        (f"DATETIME({fsp})",   "mysql")      for fsp in range(7)
    ] + [
        (f"TIMESTAMP({fsp})",  "mysql")      for fsp in range(7)
    ] + [
        (f"TIME({fsp})",       "mysql")      for fsp in range(7)
    ] + [
        (f"TIMESTAMP({fsp})",  "postgres")   for fsp in range(7)
    ] + [
        (f"TIME({fsp})",       "postgres")   for fsp in range(7)
    ] + [
        (f"TIMETZ",            "postgres"),
        (f"TIME(0) WITH TIME ZONE",  "postgres"),
        (f"TIME(3) WITH TIME ZONE",  "postgres"),
        (f"TIME(6) WITH TIME ZONE",  "postgres"),
    ] + [
        (f"TIMESTAMP({fsp}) WITH TIME ZONE", "postgres") for fsp in range(7)
    ] + [
        (f"DATETIME2({fsp})",       "sqlserver") for fsp in range(8)
    ] + [
        (f"DATETIMEOFFSET({fsp})",  "sqlserver") for fsp in range(8)
    ] + [
        (f"TIME({fsp})",            "sqlserver") for fsp in range(8)
    ])
    def test_parse_all_fsp_roundtrip(self, raw_type: str, dialect: str):
        """parse(raw DDL with explicit fsp) → emit → parse → emit is idempotent."""
        ddl = _wrap_col_ddl(raw_type, dialect)
        assert_parse_first_roundtrip(ddl, dialect)

    @pytest.mark.parametrize("raw_type,dialect,expected_fsp", [
        # MySQL: all fsp values 0–6 on TIME
        *[(f"TIME({n})", "mysql", n) for n in range(7)],
        # MySQL: all fsp values 0–6 on DATETIME
        *[(f"DATETIME({n})", "mysql", n) for n in range(7)],
        # PostgreSQL: all fsp values 0–6 on TIME
        *[(f"TIME({n})", "postgres", n) for n in range(7)],
        # PostgreSQL: all fsp values 0–6 on TIMESTAMP WITH TIME ZONE
        *[(f"TIMESTAMP({n}) WITH TIME ZONE", "postgres", n) for n in range(7)],
        # SQL Server: all fsp values 0–7 on TIME
        *[(f"TIME({n})", "sqlserver", n) for n in range(8)],
        # SQL Server: all fsp values 0–7 on DATETIME2
        *[(f"DATETIME2({n})", "sqlserver", n) for n in range(8)],
        # SQL Server: all fsp values 0–7 on DATETIMEOFFSET
        *[(f"DATETIMEOFFSET({n})", "sqlserver", n) for n in range(8)],
    ])
    def test_parse_fsp_value_exact(
        self, raw_type: str, dialect: str, expected_fsp: int
    ):
        """Every fsp value parses to the exact integer in CanonicalColumn.fsp."""
        ddl = _wrap_col_ddl(raw_type, dialect)
        tables = parse_ddl(ddl, dialect)
        assert tables
        col = tables[0].columns[1]
        assert col.fsp == expected_fsp, (
            f"{dialect} {raw_type!r}: expected fsp={expected_fsp}, got fsp={col.fsp}"
        )

    # ── DEFAULT CURRENT_TIMESTAMP survives with and without fsp ──────────

    @pytest.mark.parametrize("dialect,parse_d", [
        ("mysql",     "mysql"),
        ("postgres",  "postgres"),
        ("sqlserver", "sqlserver"),
    ])
    def test_current_timestamp_default(self, dialect: str, parse_d: str):
        """DEFAULT CURRENT_TIMESTAMP round-trips on timestamp/timestamptz columns."""
        col = make_col("created_at", "timestamptz", not_null=True)
        col.default = "CURRENT_TIMESTAMP"
        table = make_pk_table("events", col)
        assert_ddl_roundtrip(table, dialect, parse_d)

    @pytest.mark.parametrize("dialect,parse_d", [
        ("mysql",     "mysql"),
        ("postgres",  "postgres"),
        ("sqlserver", "sqlserver"),
    ])
    def test_fsp_with_default(self, dialect: str, parse_d: str):
        """fsp and DEFAULT CURRENT_TIMESTAMP coexist without parse corruption."""
        col = make_col("created_at", "timestamptz", not_null=True)
        col.fsp = 3
        col.default = "CURRENT_TIMESTAMP"
        table = make_pk_table("events", col)
        assert_ddl_roundtrip(table, dialect, parse_d)

    # ── Cross-dialect emission (same canonical → multiple targets) ────────

    def test_timestamptz_cross_dialect_emit(self):
        """A single timestamptz column emits its dialect-native TZ-aware type."""
        from src.statschema import emit_ddl
        col = make_col("ts", "timestamptz")
        table = make_pk_table("t", col)
        assert "TIMESTAMP" in emit_ddl(table, "mysql")
        assert "TIMESTAMP WITH TIME ZONE" in emit_ddl(table, "postgres")
        assert "DATETIMEOFFSET" in emit_ddl(table, "sqlserver")
        assert "TIMESTAMP WITH TIME ZONE" in emit_ddl(table, "oracle")
        assert "TIMESTAMP" in emit_ddl(table, "databricks")

    def test_time_cross_dialect_emit(self):
        """time canonical emits TIME on MySQL/PG/SS, TIMESTAMP on Oracle, STRING on Databricks."""
        from src.statschema import emit_ddl
        col = make_col("t", "time")
        table = make_pk_table("tbl", col)
        assert "TIME" in emit_ddl(table, "mysql")
        assert "TIME" in emit_ddl(table, "postgres")
        assert "TIME" in emit_ddl(table, "sqlserver")
        assert "TIMESTAMP" in emit_ddl(table, "oracle")
        assert "STRING" in emit_ddl(table, "databricks")

    def test_databricks_timestamp_vs_timestamp_ntz(self):
        """Databricks: canonical timestamp → TIMESTAMP_NTZ, timestamptz → TIMESTAMP."""
        from src.statschema import emit_ddl
        col_ts  = make_col("ts",  "timestamp")
        col_tsz = make_col("tsz", "timestamptz")
        table = make_pk_table("t", col_ts, col_tsz)
        ddl = emit_ddl(table, "databricks")
        assert "TIMESTAMP_NTZ" in ddl, f"Expected TIMESTAMP_NTZ for timestamp:\n{ddl}"
        assert "TIMESTAMP" in ddl,     f"Expected TIMESTAMP for timestamptz:\n{ddl}"

    # ── Temporal columns in a realistic table ────────────────────────────

    @pytest.mark.parametrize("dialect,parse_d", [
        ("mysql",     "mysql"),
        ("postgres",  "postgres"),
        ("sqlserver", "sqlserver"),
    ])
    def test_mixed_temporal_table(self, dialect: str, parse_d: str):
        """A table with date, timestamp, timestamptz, and time round-trips."""
        cols = [
            make_col("event_date",   "date"),
            make_col("logged_at",    "timestamp",   not_null=True),
            make_col("published_at", "timestamptz"),
            make_col("start_time",   "time"),
        ]
        table = make_pk_table("events", *cols)
        assert_ddl_roundtrip(table, dialect, parse_d)


# ============================================================================
# Phase 9 — Common migration issues from real-world database migrations
# ============================================================================

class TestPhase9MigrationIssues:
    """
    Tests derived from documented production migration issues across MySQL,
    PostgreSQL, SQL Server, Oracle, and Databricks.

    Sources: Databricks Lakeflow Connect reference, DBConvert Streams migration
    guide, Microsoft SSMA, Oracle-to-PostgreSQL migration research (2025).
    """

    # ── Binary / blob types ───────────────────────────────────────────────

    @pytest.mark.parametrize("raw_type,dialect,expected_len", [
        ("BINARY(16)",      "mysql",     16),
        ("VARBINARY(255)",  "mysql",     255),
        ("VARBINARY(8000)", "sqlserver", 8000),
        ("BINARY(8)",       "sqlserver", 8),
        ("RAW(200)",        "mysql",     200),   # Oracle RAW via fallback
    ])
    def test_binary_length_preserved(self, raw_type: str, dialect: str, expected_len: int):
        """BINARY(n)/VARBINARY(n)/RAW(n) preserve their byte-length in CanonicalColumn."""
        ddl = _wrap_col_ddl(raw_type, dialect)
        col = parse_ddl(ddl, dialect)[0].columns[1]
        assert col.type == "binary", f"{raw_type}: expected canonical 'binary', got {col.type!r}"
        assert col.length == expected_len, (
            f"{raw_type}: expected length={expected_len}, got {col.length!r}"
        )

    @pytest.mark.parametrize("raw_type,dialect", [
        ("BLOB",             "mysql"),
        ("TINYBLOB",         "mysql"),
        ("MEDIUMBLOB",       "mysql"),
        ("LONGBLOB",         "mysql"),
        ("IMAGE",            "sqlserver"),
        ("ROWVERSION",       "sqlserver"),
        ("GEOGRAPHY",        "sqlserver"),
        ("GEOMETRY",         "sqlserver"),
        ("BYTEA",            "postgres"),
    ])
    def test_unbounded_binary_canonical(self, raw_type: str, dialect: str):
        """Unbounded binary types parse to canonical 'binary' with length=None."""
        ddl = _wrap_col_ddl(raw_type, dialect)
        col = parse_ddl(ddl, dialect)[0].columns[1]
        assert col.type == "binary", f"{raw_type}: expected 'binary', got {col.type!r}"
        assert col.length is None

    @pytest.mark.parametrize("canonical,length,dialect,parse_d,expected_sql", [
        ("binary", 16,   "mysql",     "mysql",     "VARBINARY(16)"),
        ("binary", 255,  "mysql",     "mysql",     "VARBINARY(255)"),
        ("binary", None, "mysql",     "mysql",     "LONGBLOB"),
        ("binary", 255,  "postgres",  "postgres",  "BYTEA"),    # PG ignores length
        ("binary", None, "postgres",  "postgres",  "BYTEA"),
        ("binary", 8000, "sqlserver", "sqlserver",  "VARBINARY(8000)"),
        ("binary", None, "sqlserver", "sqlserver",  "VARBINARY(MAX)"),
        ("binary", 200,  "oracle",    "oracle",     "RAW(200)"),
        ("binary", 2001, "oracle",    "oracle",     "BLOB"),     # length > 2000 → BLOB
        ("binary", None, "oracle",    "oracle",     "BLOB"),
        ("binary", 16,   "databricks","mysql",      "BINARY"),
        ("binary", None, "databricks","mysql",      "BINARY"),
    ])
    def test_binary_emit(
        self, canonical: str, length, dialect: str, parse_d: str, expected_sql: str
    ):
        """Binary canonical emits the correct dialect-native SQL token."""
        from src.statschema import emit_ddl
        col = make_col("data", canonical)
        col.length = length
        ddl = emit_ddl(make_pk_table("t", col), dialect)
        assert expected_sql in ddl, (
            f"binary(len={length}) on {dialect}: expected {expected_sql!r}.\n{ddl}"
        )

    @pytest.mark.parametrize("dialect,parse_d", [
        ("mysql",     "mysql"),
        ("postgres",  "postgres"),
        ("sqlserver", "sqlserver"),
    ])
    def test_binary_roundtrip_with_length(self, dialect: str, parse_d: str):
        """VARBINARY(255) / BYTEA / VARBINARY(255) round-trip is idempotent."""
        col = make_col("payload", "binary")
        col.length = 255
        assert_ddl_roundtrip(make_pk_table("t", col), dialect, parse_d)

    def test_binary_roundtrip_no_length(self):
        """Unbounded binary round-trips on MySQL, PostgreSQL, SQL Server."""
        col = make_col("img", "binary")
        for dialect in ("mysql", "postgres", "sqlserver"):
            assert_ddl_roundtrip(make_pk_table("t", col), dialect, dialect)

    # ── SQL Server TIMESTAMP ≠ datetime (critical gotcha) ─────────────────

    def test_sqlserver_timestamp_is_rowversion(self):
        """SQL Server TIMESTAMP (rowversion) parses to canonical 'binary', not 'timestamp'."""
        ddl = "CREATE TABLE [t] ([id] INT, [rv] TIMESTAMP)"
        tables = parse_ddl(ddl, "sqlserver")
        rv_col = tables[0].columns[1]
        assert rv_col.type == "binary", (
            f"SQL Server TIMESTAMP should be 'binary' (rowversion), got {rv_col.type!r}"
        )
        assert rv_col.length is None

    def test_sqlserver_rowversion_explicit(self):
        """ROWVERSION (explicit alias for TIMESTAMP) also maps to 'binary'."""
        ddl = "CREATE TABLE [t] ([id] INT, [rv] ROWVERSION)"
        col = parse_ddl(ddl, "sqlserver")[0].columns[1]
        assert col.type == "binary"

    # ── SQL Server MONEY / SMALLMONEY precision ───────────────────────────

    @pytest.mark.parametrize("raw_type,expected_prec,expected_scale", [
        ("MONEY",      19, 4),
        ("SMALLMONEY", 10, 4),
    ])
    def test_sqlserver_money_precision(self, raw_type: str, expected_prec: int, expected_scale: int):
        """MONEY and SMALLMONEY parse to decimal with their documented precision and scale."""
        ddl = _wrap_col_ddl(raw_type, "sqlserver")
        col = parse_ddl(ddl, "sqlserver")[0].columns[1]
        assert col.type == "decimal"
        assert col.precision == expected_prec, f"{raw_type}: precision={col.precision!r}"
        assert col.scale == expected_scale,    f"{raw_type}: scale={col.scale!r}"

    @pytest.mark.parametrize("raw_type,dialect", [
        ("MONEY",      "sqlserver"),
        ("SMALLMONEY", "sqlserver"),
        ("MONEY",      "postgres"),
    ])
    def test_money_roundtrip(self, raw_type: str, dialect: str):
        """MONEY round-trips as DECIMAL(p,s) — no precision is lost."""
        assert_parse_first_roundtrip(_wrap_col_ddl(raw_type, dialect), dialect)

    def test_pg_money_precision(self):
        """PostgreSQL MONEY → DECIMAL(19,2) (different scale from SS MONEY=4)."""
        ddl = _wrap_col_ddl("MONEY", "postgres")
        col = parse_ddl(ddl, "postgres")[0].columns[1]
        assert col.type == "decimal"
        assert col.precision == 19
        assert col.scale == 2

    # ── MySQL UNSIGNED type widening ──────────────────────────────────────

    @pytest.mark.parametrize("raw_type,expected_canonical", [
        ("TINYINT UNSIGNED",   "integer"),  # max 255, fits in INT32
        ("SMALLINT UNSIGNED",  "integer"),  # max 65535, fits in INT32
        ("MEDIUMINT UNSIGNED", "integer"),  # max 16777215, fits in INT32
        ("INT UNSIGNED",       "long"),     # max 4294967295 > INT32 max → widened to BIGINT
        ("INTEGER UNSIGNED",   "long"),     # same as INT UNSIGNED
        ("BIGINT UNSIGNED",    "decimal"),  # max 18446744073709551615 > INT64 max
    ])
    def test_unsigned_widening(self, raw_type: str, expected_canonical: str):
        """MySQL UNSIGNED INT and BIGINT UNSIGNED are widened to prevent overflow."""
        ddl = _wrap_col_ddl(raw_type, "mysql")
        col = parse_ddl(ddl, "mysql")[0].columns[1]
        assert col.type == expected_canonical, (
            f"{raw_type}: expected {expected_canonical!r}, got {col.type!r}"
        )
        assert col.unsigned is True

    def test_bigint_unsigned_precision(self):
        """BIGINT UNSIGNED → DECIMAL(20,0) — exactly 20 digits, scale=0."""
        ddl = _wrap_col_ddl("BIGINT UNSIGNED", "mysql")
        col = parse_ddl(ddl, "mysql")[0].columns[1]
        assert col.precision == 20
        assert col.scale == 0

    def test_non_unsigned_columns_have_unsigned_false(self):
        """Regular (signed) columns leave unsigned=False."""
        ddl = _wrap_col_ddl("INT", "mysql")
        col = parse_ddl(ddl, "mysql")[0].columns[1]
        assert col.unsigned is False

    # ── Oracle NUMBER(p) without scale ────────────────────────────────────

    @pytest.mark.parametrize("raw_type,expected_canonical,expected_prec", [
        ("NUMBER(5)",   "integer",  None),   # p≤9 → integer; precision dropped
        ("NUMBER(9)",   "integer",  None),   # boundary: max p for integer
        ("NUMBER(10)",  "long",     None),   # p=10 → long; precision dropped
        ("NUMBER(18)",  "long",     None),   # boundary: max p for long
        ("NUMBER(19)",  "decimal",  19),     # p=19 → decimal(19,0)
        ("NUMBER(38)",  "decimal",  38),     # max Oracle precision
        ("NUMBER(5,2)", "decimal",  5),      # scale present → always decimal
        ("NUMBER",      "decimal",  None),   # no precision → unbounded decimal
    ])
    def test_oracle_number_widening(self, raw_type: str, expected_canonical: str, expected_prec):
        """Oracle NUMBER(p) without scale is widened to integer/long based on precision."""
        ddl = _wrap_col_ddl(raw_type, "mysql")   # Oracle DDL via MySQL fallback
        col = parse_ddl(ddl, "mysql")[0].columns[1]
        assert col.type == expected_canonical, (
            f"{raw_type}: expected {expected_canonical!r}, got {col.type!r}"
        )
        assert col.precision == expected_prec, (
            f"{raw_type}: expected precision={expected_prec!r}, got {col.precision!r}"
        )

    def test_oracle_number_scale_zero_for_large_p(self):
        """Oracle NUMBER(19) and NUMBER(38) get scale=0 (not None)."""
        for raw, expected_scale in [("NUMBER(19)", 0), ("NUMBER(38)", 0)]:
            ddl = _wrap_col_ddl(raw, "mysql")
            col = parse_ddl(ddl, "mysql")[0].columns[1]
            assert col.scale == expected_scale, f"{raw}: scale={col.scale!r}"

    # ── PostgreSQL BYTEA round-trips correctly ────────────────────────────

    def test_pg_bytea_roundtrip(self):
        """BYTEA round-trips as BYTEA (no length stored or emitted)."""
        assert_parse_first_roundtrip(_wrap_col_ddl("BYTEA", "postgres"), "postgres")

    # ── SQL Server extra string types ─────────────────────────────────────

    @pytest.mark.parametrize("raw_type", [
        "XML", "SQL_VARIANT", "HIERARCHYID"
    ])
    def test_sqlserver_string_types(self, raw_type: str):
        """SQL Server XML, SQL_VARIANT, HIERARCHYID map to canonical 'string'."""
        ddl = _wrap_col_ddl(raw_type, "sqlserver")
        col = parse_ddl(ddl, "sqlserver")[0].columns[1]
        assert col.type == "string", f"{raw_type}: expected 'string', got {col.type!r}"

    # ── Cross-dialect binary migration scenario ───────────────────────────

    def test_mysql_blob_to_postgres_bytea(self):
        """MySQL BLOB (binary, no length) emits BYTEA in PostgreSQL."""
        from src.statschema import emit_ddl
        col = make_col("img", "binary")   # parsed from MySQL BLOB
        ddl = emit_ddl(make_pk_table("t", col), "postgres")
        assert "BYTEA" in ddl

    def test_mysql_varbinary_to_sqlserver(self):
        """MySQL VARBINARY(255) migrates to SQL Server VARBINARY(255)."""
        from src.statschema import emit_ddl
        col = make_col("token", "binary")
        col.length = 255
        ddl = emit_ddl(make_pk_table("t", col), "sqlserver")
        assert "VARBINARY(255)" in ddl

    def test_sqlserver_money_to_postgres(self):
        """SQL Server MONEY migrates to PostgreSQL NUMERIC(19,4)."""
        from src.statschema import emit_ddl
        col = make_col("price", "decimal")
        col.precision = 19
        col.scale = 4
        ddl = emit_ddl(make_pk_table("t", col), "postgres")
        assert "NUMERIC(19,4)" in ddl

    def test_sqlserver_money_to_databricks(self):
        """SQL Server MONEY migrates to Databricks DECIMAL(19,4)."""
        from src.statschema import emit_ddl
        col = make_col("price", "decimal")
        col.precision = 19
        col.scale = 4
        ddl = emit_ddl(make_pk_table("t", col), "databricks")
        assert "DECIMAL(19,4)" in ddl

    def test_int_unsigned_to_postgres_bigint(self):
        """MySQL INT UNSIGNED (widened to 'long') emits BIGINT in PostgreSQL."""
        from src.statschema import emit_ddl
        col = make_col("user_id", "long")  # after widening from INT UNSIGNED
        ddl = emit_ddl(make_pk_table("t", col), "postgres")
        assert "BIGINT" in ddl

    # ── Realistic mixed-type migration table ─────────────────────────────

    @pytest.mark.parametrize("dialect,parse_d", [
        ("mysql",     "mysql"),
        ("postgres",  "postgres"),
        ("sqlserver", "sqlserver"),
    ])
    def test_migration_table_roundtrip(self, dialect: str, parse_d: str):
        """A table mixing binary, decimal, string, and temporal round-trips."""
        cols = [
            make_col("img",       "binary"),
            make_col("thumb",     "binary"),
            make_col("price",     "decimal", not_null=True),
            make_col("name",      "string",  not_null=True),
            make_col("created",   "timestamptz"),
        ]
        cols[1].length = 1024
        cols[2].precision = 10
        cols[2].scale = 2
        cols[3].length = 200
        table = make_pk_table("products", *cols)
        assert_ddl_roundtrip(table, dialect, parse_d)


# ============================================================================
# Phase 10 — Full pipeline: DDL → canonical YAML → DDL / data
# ============================================================================
#
# This is the core architectural test: many source DDLs → one canonical YAML →
# many target DDLs + synthetic data.  It validates the interchange format that
# makes the whole system work.

_ORDERS_MYSQL_DDL = """\
CREATE TABLE `orders` (
  `id`          BIGINT        NOT NULL AUTO_INCREMENT,
  `customer_id` BIGINT        NOT NULL,
  `amount`      DECIMAL(10,2) NOT NULL,
  `status`      VARCHAR(20)   NOT NULL DEFAULT 'pending',
  `ordered_at`  TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `shipped_at`  DATETIME,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB;
"""

_USERS_PG_DDL = """\
CREATE TABLE users (
  id         BIGSERIAL    NOT NULL,
  email      VARCHAR(255) NOT NULL,
  created_at TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
  is_active  BOOLEAN      NOT NULL DEFAULT TRUE,
  balance    NUMERIC(12,2),
  PRIMARY KEY (id)
);
"""

_PRODUCTS_SQLSERVER_DDL = """\
CREATE TABLE [products] (
  [id]       BIGINT         NOT NULL IDENTITY(1,1),
  [name]     NVARCHAR(200)  NOT NULL,
  [price]    MONEY          NOT NULL,
  [stock]    INT            NOT NULL DEFAULT 0,
  [sku]      UNIQUEIDENTIFIER NOT NULL,
  PRIMARY KEY ([id])
);
"""


class TestPhase10CanonicalYamlPipeline:
    """
    Full pipeline tests: DDL → canonical YAML → DDL and DDL → YAML → data.

    Validates the ``many DDL sources → one canonical YAML → many DDL targets + data``
    architecture that is the central design goal of the statschema module.
    """

    # ── Part A: YAML serialization round-trip ────────────────────────────

    def test_canonical_column_roundtrip(self):
        """CanonicalColumn.to_dict() → from_dict() is lossless."""
        col = CanonicalColumn(
            name="amount", type="decimal",
            precision=10, scale=2, not_null=True,
            description="Order amount",
        )
        col2 = CanonicalColumn.from_dict(col.to_dict())
        assert col2.name == "amount"
        assert col2.type == "decimal"
        assert col2.precision == 10
        assert col2.scale == 2
        assert col2.not_null is True
        assert col2.description == "Order amount"

    def test_generation_rule_roundtrip(self):
        """GenerationRule with all new fields survives to_dict / from_dict."""
        g = GenerationRule(
            distribution="zipf",
            distribution_params={"a": 1.5},
            format_pattern="email",
            inject_boundary_values=True,
            inject_nulls_from_stats=False,
            use_mcv_weights=True,
            inject_rare_events=True,
            values=["a", "b"],
            weights=[0.8, 0.2],
        )
        g2 = GenerationRule.from_dict(g.to_dict())
        assert g2.distribution == "zipf"
        assert g2.distribution_params == {"a": 1.5}
        assert g2.format_pattern == "email"
        assert g2.inject_nulls_from_stats is False
        assert g2.inject_rare_events is True
        assert g2.values == ["a", "b"]
        assert g2.weights == [0.8, 0.2]

    def test_fk_constraint_roundtrip(self):
        """CanonicalForeignKey survives YAML round-trip."""
        fk = CanonicalForeignKey(
            columns=["customer_id"],
            parent_table="customers",
            parent_columns=["id"],
            name="fk_orders_customer",
        )
        fk2 = CanonicalForeignKey.from_dict(fk.to_dict())
        assert fk2.columns == ["customer_id"]
        assert fk2.parent_table == "customers"
        assert fk2.name == "fk_orders_customer"

    def test_table_schema_roundtrip(self):
        """CanonicalTableSchema (columns + FK + constraints) survives to_dict / from_dict."""
        cols = [
            make_col("id",         "long",       not_null=True),
            make_col("email",      "string",     not_null=True),
            make_col("created_at", "timestamptz"),
        ]
        cols[0].primary_key = True
        cols[0].auto_increment = True
        cols[1].length = 255
        fk = CanonicalForeignKey(columns=["org_id"], parent_table="orgs", parent_columns=["id"])
        table = CanonicalTableSchema(
            name="users",
            columns=cols,
            fk_constraints=[fk],
            temporal_ordering_constraints=["created_at <= updated_at"],
        )
        table2 = CanonicalTableSchema.from_dict(table.to_dict())
        assert table2.name == "users"
        assert len(table2.columns) == 3
        assert table2.columns[1].length == 255
        assert table2.fk_constraints[0].parent_table == "orgs"
        assert table2.temporal_ordering_constraints == ["created_at <= updated_at"]

    # ── Part B: dump_schema / load_canonical I/O ─────────────────────────

    def test_dump_and_load_canonical_string(self):
        """dump_schema → YAML string → load_canonical → same table."""
        tables = parse_ddl(_ORDERS_MYSQL_DDL, "mysql")
        yaml_str = dump_schema(tables)
        tables2 = load_canonical(yaml_str)
        assert len(tables2) == 1
        assert tables2[0].name == "orders"
        assert len(tables2[0].columns) == len(tables[0].columns)

    def test_dump_and_load_canonical_file(self, tmp_path):
        """dump_schema writes to file; load_canonical reads it back."""
        tables = parse_ddl(_ORDERS_MYSQL_DDL, "mysql")
        schema_path = tmp_path / "schema.yaml"
        dump_schema(tables, path=schema_path)
        assert schema_path.exists()
        tables2 = load_canonical(schema_path)
        assert tables2[0].name == tables[0].name

    def test_canonical_yaml_has_version(self):
        """Canonical YAML document includes a version field."""
        tables = parse_ddl(_USERS_PG_DDL, "postgres")
        yaml_str = dump_schema(tables)
        data = yaml.safe_load(yaml_str)
        assert "version" in data
        assert data["version"] == "1.0"

    def test_canonical_yaml_includes_all_columns(self):
        """Every column from the DDL appears in the canonical YAML."""
        tables = parse_ddl(_ORDERS_MYSQL_DDL, "mysql")
        yaml_str = dump_schema(tables)
        data = yaml.safe_load(yaml_str)
        col_names = [c["name"] for c in data["tables"][0]["columns"]]
        assert "id" in col_names
        assert "amount" in col_names
        assert "status" in col_names

    def test_canonical_yaml_preserves_precision(self):
        """DECIMAL(10,2) precision and scale survive the YAML round-trip."""
        tables = parse_ddl(_ORDERS_MYSQL_DDL, "mysql")
        yaml_str = dump_schema(tables)
        tables2 = load_canonical(yaml_str)
        amt_col = next(c for c in tables2[0].columns if c.name == "amount")
        assert amt_col.precision == 10
        assert amt_col.scale == 2

    # ── Part C: DDL → YAML → DDL (the canonical YAML as interchange) ─────

    @pytest.mark.parametrize("target_dialect", ["mysql", "postgres", "sqlserver", "databricks"])
    def test_mysql_ddl_via_yaml_to_all_targets(self, target_dialect: str):
        """MySQL DDL → canonical YAML → any target DDL is non-empty and contains table name."""
        tables = parse_ddl(_ORDERS_MYSQL_DDL, "mysql")
        yaml_str = dump_schema(tables)
        tables2 = load_canonical(yaml_str)
        ddl = emit_ddl(tables2[0], target_dialect)
        assert "orders" in ddl.lower()
        assert "CREATE TABLE" in ddl.upper()

    @pytest.mark.parametrize("target_dialect", ["mysql", "postgres", "sqlserver", "databricks"])
    def test_pg_ddl_via_yaml_to_all_targets(self, target_dialect: str):
        """PostgreSQL DDL → canonical YAML → any target DDL is valid."""
        tables = parse_ddl(_USERS_PG_DDL, "postgres")
        yaml_str = dump_schema(tables)
        tables2 = load_canonical(yaml_str)
        ddl = emit_ddl(tables2[0], target_dialect)
        assert "users" in ddl.lower()
        assert "CREATE TABLE" in ddl.upper()

    @pytest.mark.parametrize("target_dialect", ["mysql", "postgres", "sqlserver", "databricks"])
    def test_sqlserver_ddl_via_yaml_to_all_targets(self, target_dialect: str):
        """SQL Server DDL → canonical YAML → any target DDL is valid."""
        tables = parse_ddl(_PRODUCTS_SQLSERVER_DDL, "sqlserver")
        yaml_str = dump_schema(tables)
        tables2 = load_canonical(yaml_str)
        ddl = emit_ddl(tables2[0], target_dialect)
        assert "products" in ddl.lower()
        assert "CREATE TABLE" in ddl.upper()

    def test_yaml_idempotent_schema_roundtrip(self):
        """DDL → canonical → YAML → canonical → YAML: both YAML documents are identical."""
        tables = parse_ddl(_ORDERS_MYSQL_DDL, "mysql")
        yaml1 = dump_schema(tables)
        tables2 = load_canonical(yaml1)
        yaml2 = dump_schema(tables2)
        assert yaml1 == yaml2, "Canonical YAML is not idempotent (schema changed on reload)"

    def test_multi_table_schema_yaml_roundtrip(self):
        """Multiple tables serialize and deserialize correctly in one YAML document."""
        tables_orders = parse_ddl(_ORDERS_MYSQL_DDL, "mysql")
        tables_users  = parse_ddl(_USERS_PG_DDL, "postgres")
        all_tables = tables_orders + tables_users
        yaml_str = dump_schema(all_tables)
        all_tables2 = load_canonical(yaml_str)
        assert len(all_tables2) == 2
        names = {t.name for t in all_tables2}
        assert "orders" in names
        assert "users" in names

    # ── Part D: precision types survive YAML and round-trip correctly ─────

    def test_decimal_precision_via_yaml(self):
        """DECIMAL(10,2) → canonical → YAML → canonical → emit preserves p,s."""
        tables = parse_ddl("CREATE TABLE t (price DECIMAL(10,2) NOT NULL);", "mysql")
        yaml_str = dump_schema(tables)
        tables2 = load_canonical(yaml_str)
        ddl = emit_ddl(tables2[0], "postgres")
        assert "NUMERIC(10,2)" in ddl

    def test_varchar_length_via_yaml(self):
        """VARCHAR(200) length is preserved through the YAML interchange."""
        tables = parse_ddl("CREATE TABLE t (name VARCHAR(200));", "mysql")
        yaml_str = dump_schema(tables)
        tables2 = load_canonical(yaml_str)
        ddl = emit_ddl(tables2[0], "mysql")
        assert "VARCHAR(200)" in ddl

    def test_money_precision_via_yaml(self):
        """SQL Server MONEY → DECIMAL(19,4) precision preserved through YAML."""
        tables = parse_ddl("CREATE TABLE [t] ([price] MONEY NOT NULL);", "sqlserver")
        yaml_str = dump_schema(tables)
        tables2 = load_canonical(yaml_str)
        ddl = emit_ddl(tables2[0], "databricks")
        assert "DECIMAL(19,4)" in ddl

    def test_fsp_via_yaml(self):
        """TIMESTAMP(6) fsp is preserved through the YAML interchange."""
        tables = parse_ddl("CREATE TABLE t (ts TIMESTAMP(6) NOT NULL);", "mysql")
        yaml_str = dump_schema(tables)
        tables2 = load_canonical(yaml_str)
        ddl = emit_ddl(tables2[0], "mysql")
        assert "TIMESTAMP(6)" in ddl

    def test_binary_via_yaml(self):
        """VARBINARY(255) binary type and length preserved through YAML."""
        tables = parse_ddl("CREATE TABLE t (img VARBINARY(255));", "mysql")
        yaml_str = dump_schema(tables)
        tables2 = load_canonical(yaml_str)
        ddl = emit_ddl(tables2[0], "postgres")
        assert "BYTEA" in ddl          # PG always emits BYTEA (no length)
        ddl_mysql = emit_ddl(tables2[0], "mysql")
        assert "VARBINARY(255)" in ddl_mysql

    # ── Part E: DDL → YAML → dbldatagen specs (DDL → YAML → data) ────────

    def test_mysql_ddl_to_dbldatagen_specs_via_yaml(self):
        """DDL → canonical YAML → load → to_dbldatagen_specs returns one spec per column."""
        tables = parse_ddl(_ORDERS_MYSQL_DDL, "mysql")
        yaml_str = dump_schema(tables)
        tables2 = load_canonical(yaml_str)
        specs = to_dbldatagen_specs(tables2[0], rows=1000)
        col_names = [s[0] for s in specs]
        assert "amount"     in col_names
        assert "status"     in col_names
        assert "ordered_at" in col_names

    def test_pg_ddl_to_dbldatagen_specs_via_yaml(self):
        """PostgreSQL DDL → YAML → load → to_dbldatagen_specs covers all columns."""
        tables = parse_ddl(_USERS_PG_DDL, "postgres")
        yaml_str = dump_schema(tables)
        tables2 = load_canonical(yaml_str)
        specs = to_dbldatagen_specs(tables2[0], rows=500)
        col_names = {s[0] for s in specs}
        assert {"email", "is_active", "balance"}.issubset(col_names)

    def test_generation_rule_survives_yaml_to_specs(self):
        """GenerationRule (distribution, format_pattern) survives YAML and feeds specs."""
        col = make_col("email", "string")
        col.generation = GenerationRule(
            distribution="zipf",
            distribution_params={"a": 1.5},
            format_pattern="email",
        )
        table = make_pk_table("contacts", col)
        yaml_str = dump_schema([table])
        tables2 = load_canonical(yaml_str)
        # Generation rule should be preserved
        email_col = next(c for c in tables2[0].columns if c.name == "email")
        assert email_col.generation is not None
        assert email_col.generation.distribution == "zipf"
        assert email_col.generation.format_pattern == "email"
        # And it should feed into dbldatagen specs
        specs = to_dbldatagen_specs(tables2[0], rows=100)
        email_spec = next(s for s in specs if s[0] == "email")
        # template should be set from format_pattern
        assert "template" in email_spec[2], f"Expected template in spec: {email_spec[2]}"

    def test_temporal_ordering_constraints_survive_yaml(self):
        """temporal_ordering_constraints are preserved in the YAML interchange."""
        table = CanonicalTableSchema(
            name="sessions",
            columns=[
                make_col("id",         "long",       not_null=True),
                make_col("start_time", "timestamptz", not_null=True),
                make_col("end_time",   "timestamptz"),
            ],
            temporal_ordering_constraints=["end_time > start_time"],
        )
        yaml_str = dump_schema([table])
        tables2 = load_canonical(yaml_str)
        assert tables2[0].temporal_ordering_constraints == ["end_time > start_time"]

    def test_fk_constraints_survive_yaml(self):
        """FK constraints are preserved through the YAML interchange."""
        child_col = make_col("customer_id", "long", not_null=True)
        child_col.references = ("customers", "id")
        fk = CanonicalForeignKey(
            columns=["customer_id"],
            parent_table="customers",
            parent_columns=["id"],
            name="fk_orders_customer",
        )
        table = CanonicalTableSchema(
            name="orders",
            columns=[make_col("id", "long", not_null=True), child_col],
            fk_constraints=[fk],
        )
        yaml_str = dump_schema([table])
        tables2 = load_canonical(yaml_str)
        assert tables2[0].fk_constraints[0].parent_table == "customers"
        assert tables2[0].fk_constraints[0].name == "fk_orders_customer"

    # ── Part F: Multi-source → one YAML → multi-target (the headline scenario) ──

    def test_many_ddl_sources_one_yaml_many_targets(self, tmp_path):
        """
        The headline pipeline test.

        Three DDLs from different source dialects → one canonical YAML file →
        all five target dialects emit valid DDL with correct table names.
        This proves the YAML is a genuine dialect-independent interchange format.
        """
        # Step 1: parse three source DDLs from different databases
        mysql_tables    = parse_ddl(_ORDERS_MYSQL_DDL,      "mysql")
        pg_tables       = parse_ddl(_USERS_PG_DDL,          "postgres")
        ss_tables       = parse_ddl(_PRODUCTS_SQLSERVER_DDL, "sqlserver")
        all_tables = mysql_tables + pg_tables + ss_tables

        # Step 2: serialize all to a single canonical YAML file
        schema_path = tmp_path / "all_tables.yaml"
        dump_schema(all_tables, path=schema_path)

        # Step 3: reload the canonical YAML
        reloaded = load_canonical(schema_path)
        assert len(reloaded) == 3, f"Expected 3 tables, got {len(reloaded)}"
        loaded_names = {t.name for t in reloaded}
        assert loaded_names == {"orders", "users", "products"}

        # Step 4: emit to every supported target dialect
        target_dialects = ["mysql", "postgres", "sqlserver", "oracle", "databricks"]
        for table in reloaded:
            for dialect in target_dialects:
                ddl = emit_ddl(table, dialect)
                assert "CREATE TABLE" in ddl.upper(), (
                    f"{table.name} → {dialect}: missing CREATE TABLE"
                )
                assert table.name.lower() in ddl.lower(), (
                    f"{table.name} → {dialect}: table name missing from DDL"
                )

        # Step 5: build dbldatagen specs from the reloaded YAML
        for table in reloaded:
            specs = to_dbldatagen_specs(table, rows=1000)
            assert len(specs) > 0, f"{table.name}: no dbldatagen specs generated"


# ---------------------------------------------------------------------------
# Phase 11 — Oracle dialect auto-detection (no preprocessing required)
# ---------------------------------------------------------------------------

class TestPhase11OracleDialect:
    """
    Verify that Oracle DDL is auto-detected and parsed natively via sqlglot's
    oracle dialect — without any pre-processing regex substitutions.

    These tests use parse_ddl() with NO explicit dialect argument to prove that
    _detect_dialect() routes Oracle DDL correctly.
    """

    _ORACLE_DDL = """
    CREATE TABLE employees (
      emp_id       NUMBER(10)              NOT NULL,
      salary       NUMBER(19,4),
      dept_code    VARCHAR2(10)            NOT NULL,
      name         NVARCHAR2(200),
      notes        CLOB,
      attachments  NCLOB,
      fingerprint  RAW(2000),
      hired_at     DATE,
      updated_at   TIMESTAMP(6),
      created_tz   TIMESTAMP WITH TIME ZONE,
      CONSTRAINT pk_emp PRIMARY KEY (emp_id)
    )
    """

    def test_auto_detected_as_oracle(self):
        """_detect_dialect should return 'oracle' for Oracle-style DDL."""
        from src.statschema.ddl_parser import _detect_dialect
        assert _detect_dialect(self._ORACLE_DDL) == "oracle"

    def test_parses_without_explicit_dialect(self):
        """parse_ddl(oracle_ddl) — no dialect arg — should succeed."""
        tables = parse_ddl(self._ORACLE_DDL)
        assert len(tables) == 1
        assert tables[0].name == "employees"

    def test_number_types_widened_correctly(self):
        """NUMBER(10) → 'long', NUMBER(19,4) → 'decimal'."""
        tables = parse_ddl(self._ORACLE_DDL)
        cols = {c.name: c for c in tables[0].columns}
        # NUMBER(10) — no scale, p≤18 → long
        assert cols["emp_id"].type == "long"
        # NUMBER(19,4) — has scale → decimal
        assert cols["salary"].type == "decimal"
        assert cols["salary"].precision == 19
        assert cols["salary"].scale == 4

    def test_varchar2_and_nvarchar2_map_to_string(self):
        tables = parse_ddl(self._ORACLE_DDL)
        cols = {c.name: c for c in tables[0].columns}
        assert cols["dept_code"].type == "string"
        assert cols["dept_code"].length == 10
        assert cols["name"].type == "string"
        assert cols["name"].length == 200

    def test_clob_and_nclob_map_to_string(self):
        tables = parse_ddl(self._ORACLE_DDL)
        cols = {c.name: c for c in tables[0].columns}
        assert cols["notes"].type == "string"
        assert cols["attachments"].type == "string"

    def test_raw_maps_to_binary_with_length(self):
        tables = parse_ddl(self._ORACLE_DDL)
        cols = {c.name: c for c in tables[0].columns}
        assert cols["fingerprint"].type == "binary"
        assert cols["fingerprint"].length == 2000

    def test_temporal_types(self):
        tables = parse_ddl(self._ORACLE_DDL)
        cols = {c.name: c for c in tables[0].columns}
        assert cols["hired_at"].type == "date"
        assert cols["updated_at"].type == "timestamp"
        assert cols["updated_at"].fsp == 6
        assert cols["created_tz"].type == "timestamptz"

    def test_primary_key_detected(self):
        tables = parse_ddl(self._ORACLE_DDL)
        cols = {c.name: c for c in tables[0].columns}
        assert cols["emp_id"].primary_key is True
        assert cols["emp_id"].not_null is True

    def test_raw_200_via_oracle_dialect_explicit(self):
        """RAW(200) parsed with explicit dialect='oracle' (no preprocessing)."""
        ddl = "CREATE TABLE t (id NUMBER(10) NOT NULL, col RAW(200), PRIMARY KEY (id))"
        tables = parse_ddl(ddl, dialect="oracle")
        col = tables[0].columns[1]
        assert col.type == "binary"
        assert col.length == 200

    @pytest.mark.parametrize("raw_type,expected_canonical", [
        ("RAW(200)",        "binary"),
        ("RAW(2000)",       "binary"),
        ("BLOB",            "binary"),
        ("CLOB",            "string"),
        ("NCLOB",           "string"),
        ("VARCHAR2(100)",   "string"),
        ("NVARCHAR2(100)",  "string"),
        ("NUMBER(5)",       "integer"),   # p≤9 → integer
        ("NUMBER(10)",      "long"),      # 10≤p≤18 → long
        ("NUMBER(19)",      "decimal"),   # p>18 → decimal(p,0)
        ("NUMBER(10,2)",    "decimal"),
        ("TIMESTAMP",       "timestamp"),
        ("TIMESTAMP WITH TIME ZONE",  "timestamptz"),
    ])
    def test_oracle_source_type_coverage(self, raw_type: str, expected_canonical: str):
        """Every Oracle type in ORACLE_TYPE_TO_CANONICAL round-trips correctly."""
        from src.statschema.ddl_parser import ORACLE_TYPE_TO_CANONICAL
        ddl = f"CREATE TABLE t (id NUMBER(10) NOT NULL, col {raw_type}, PRIMARY KEY (id))"
        tables = parse_ddl(ddl, dialect="oracle")
        assert tables, f"No tables parsed for Oracle type {raw_type!r}"
        col = tables[0].columns[1]
        assert col.type == expected_canonical, (
            f"Oracle {raw_type!r} → expected {expected_canonical!r}, got {col.type!r}"
        )
