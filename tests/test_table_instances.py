"""
Tests for CanonicalTableSchema multi-instance / dedup expansion.

Covers:
- instance_count > 1  → numbered suffix copies (base name replaced)
- aliases             → explicit named copies (base name included)
- both combined       → numbered instances + aliases
- instance_count == 1 → backward-compatible no-op
- custom suffix format
- invalid suffix format raises ValueError
- YAML round-trip: to_dict / from_dict preserve the fields
- load_canonical(expand=True) expands inline
- load_canonical(expand=False) (default) preserves compact form
- dump_schema / load_canonical round-trip preserves instance_count & aliases
- expand_table_instances is idempotent on already-expanded tables
"""
import textwrap

import pytest

from src.statschema import (
    CanonicalColumn,
    CanonicalTableSchema,
    dump_schema,
    expand_table_instances,
    load_canonical,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _simple_table(name: str = "events", **kwargs) -> CanonicalTableSchema:
    col = CanonicalColumn(name="id", type="long", primary_key=True, not_null=True)
    return CanonicalTableSchema(name=name, columns=[col], **kwargs)


# ---------------------------------------------------------------------------
# expand_table_instances — instance_count
# ---------------------------------------------------------------------------

class TestInstanceCount:
    def test_default_instance_count_is_noop(self):
        t = _simple_table()
        result = expand_table_instances(t)
        assert len(result) == 1
        assert result[0].name == "events"

    def test_instance_count_one_is_noop(self):
        t = _simple_table(instance_count=1)
        assert expand_table_instances(t) == [t]

    def test_instance_count_three_produces_three_tables(self):
        t = _simple_table(instance_count=3)
        result = expand_table_instances(t)
        assert len(result) == 3
        assert [r.name for r in result] == ["events_0001", "events_0002", "events_0003"]

    def test_instance_count_1000(self):
        t = _simple_table(instance_count=1000)
        result = expand_table_instances(t)
        assert len(result) == 1000
        assert result[0].name == "events_0001"
        assert result[-1].name == "events_1000"

    def test_base_name_not_in_numbered_output(self):
        t = _simple_table(instance_count=3)
        names = [r.name for r in expand_table_instances(t)]
        assert "events" not in names

    def test_custom_suffix_format(self):
        t = _simple_table(instance_count=3, instance_suffix_format="_{:06d}")
        result = expand_table_instances(t)
        assert [r.name for r in result] == [
            "events_000001", "events_000002", "events_000003"
        ]

    def test_plain_numeric_suffix(self):
        t = _simple_table(instance_count=3, instance_suffix_format="_{}")
        result = expand_table_instances(t)
        assert [r.name for r in result] == ["events_1", "events_2", "events_3"]

    def test_invalid_suffix_format_raises(self):
        t = _simple_table(instance_count=2, instance_suffix_format="_{missing_key}")
        with pytest.raises(ValueError, match="instance_suffix_format"):
            expand_table_instances(t)

    def test_expanded_tables_have_instance_count_reset(self):
        t = _simple_table(instance_count=3)
        for r in expand_table_instances(t):
            assert r.instance_count == 1
            assert r.aliases == []

    def test_columns_are_shared(self):
        t = _simple_table(instance_count=3)
        result = expand_table_instances(t)
        for r in result:
            assert r.columns is t.columns


# ---------------------------------------------------------------------------
# expand_table_instances — aliases
# ---------------------------------------------------------------------------

class TestAliases:
    def test_aliases_appended_after_base(self):
        t = _simple_table(aliases=["events_archive", "events_staging"])
        result = expand_table_instances(t)
        assert [r.name for r in result] == [
            "events", "events_archive", "events_staging"
        ]

    def test_single_alias(self):
        t = _simple_table(aliases=["events_old"])
        result = expand_table_instances(t)
        assert len(result) == 2
        assert result[0].name == "events"
        assert result[1].name == "events_old"

    def test_no_aliases_noop(self):
        t = _simple_table()
        assert expand_table_instances(t) == [t]

    def test_aliases_cleared_after_expansion(self):
        t = _simple_table(aliases=["events_archive"])
        for r in expand_table_instances(t):
            assert r.aliases == []

    def test_mautic_style_aliases(self):
        """Same definition shared by semantically different table names."""
        t = _simple_table(
            name="email_stats",
            aliases=[f"email_stats_{i}" for i in range(1, 6)],
        )
        result = expand_table_instances(t)
        assert len(result) == 6
        assert result[0].name == "email_stats"
        assert result[-1].name == "email_stats_5"


# ---------------------------------------------------------------------------
# expand_table_instances — combined
# ---------------------------------------------------------------------------

class TestCombined:
    def test_instances_then_aliases(self):
        t = _simple_table(
            instance_count=3,
            aliases=["events_archive"],
        )
        result = expand_table_instances(t)
        # Numbered instances first (base name replaced), then aliases
        assert [r.name for r in result] == [
            "events_0001", "events_0002", "events_0003", "events_archive",
        ]
        assert len(result) == 4

    def test_no_base_name_when_instance_count_gt1_and_aliases(self):
        t = _simple_table(instance_count=2, aliases=["backup"])
        names = [r.name for r in expand_table_instances(t)]
        assert "events" not in names
        assert "backup" in names


# ---------------------------------------------------------------------------
# expand_table_instances — idempotency and list input
# ---------------------------------------------------------------------------

class TestExpandBehavior:
    def test_list_input(self):
        tables = [_simple_table("a", instance_count=2), _simple_table("b", aliases=["b_bak"])]
        result = expand_table_instances(tables)
        assert [r.name for r in result] == ["a_0001", "a_0002", "b", "b_bak"]

    def test_idempotent_on_already_expanded(self):
        """After expansion, calling again should be a no-op."""
        t = _simple_table(instance_count=3)
        first = expand_table_instances(t)
        second = expand_table_instances(first)
        assert [r.name for r in first] == [r.name for r in second]

    def test_mixed_list_some_expanded_some_not(self):
        tables = [_simple_table("a"), _simple_table("b", instance_count=2)]
        result = expand_table_instances(tables)
        assert [r.name for r in result] == ["a", "b_0001", "b_0002"]


# ---------------------------------------------------------------------------
# YAML round-trip: to_dict / from_dict
# ---------------------------------------------------------------------------

class TestYamlRoundTrip:
    def test_instance_count_serialized(self):
        t = _simple_table(instance_count=100)
        d = t.to_dict()
        assert d["instance_count"] == 100

    def test_instance_count_1_not_serialized(self):
        t = _simple_table(instance_count=1)
        d = t.to_dict()
        assert "instance_count" not in d

    def test_aliases_serialized(self):
        t = _simple_table(aliases=["events_archive"])
        d = t.to_dict()
        assert d["aliases"] == ["events_archive"]

    def test_empty_aliases_not_serialized(self):
        t = _simple_table()
        d = t.to_dict()
        assert "aliases" not in d

    def test_default_suffix_format_not_serialized(self):
        t = _simple_table(instance_suffix_format="_{:04d}")
        d = t.to_dict()
        assert "instance_suffix_format" not in d

    def test_custom_suffix_format_serialized(self):
        t = _simple_table(instance_suffix_format="_{:06d}")
        d = t.to_dict()
        assert d["instance_suffix_format"] == "_{:06d}"

    def test_from_dict_round_trip(self):
        t = _simple_table(instance_count=50, aliases=["events_old"],
                          instance_suffix_format="_{:03d}")
        restored = CanonicalTableSchema.from_dict(t.to_dict())
        assert restored.instance_count == 50
        assert restored.aliases == ["events_old"]
        assert restored.instance_suffix_format == "_{:03d}"

    def test_from_dict_missing_fields_get_defaults(self):
        """Old YAMLs without the new fields must still load with safe defaults."""
        d = {"name": "orders", "columns": [{"name": "id", "type": "long"}]}
        t = CanonicalTableSchema.from_dict(d)
        assert t.instance_count == 1
        assert t.aliases == []
        assert t.instance_suffix_format == "_{:04d}"


# ---------------------------------------------------------------------------
# load_canonical with expand parameter
# ---------------------------------------------------------------------------

class TestLoadCanonical:
    _YAML_INSTANCES = textwrap.dedent("""\
        version: "1.0"
        tables:
          - name: form_results
            instance_count: 3
            columns:
              - name: id
                type: long
                primary_key: true
    """)

    _YAML_ALIASES = textwrap.dedent("""\
        version: "1.0"
        tables:
          - name: audit_log
            aliases:
              - audit_log_archive
              - audit_log_staging
            columns:
              - name: id
                type: long
    """)

    _YAML_NO_MULTI = textwrap.dedent("""\
        version: "1.0"
        tables:
          - name: users
            columns:
              - name: id
                type: long
    """)

    def test_expand_false_returns_compact(self):
        tables = load_canonical(self._YAML_INSTANCES, expand=False)
        assert len(tables) == 1
        assert tables[0].instance_count == 3

    def test_expand_default_is_false(self):
        tables = load_canonical(self._YAML_INSTANCES)
        assert len(tables) == 1
        assert tables[0].instance_count == 3

    def test_expand_true_instances(self):
        tables = load_canonical(self._YAML_INSTANCES, expand=True)
        assert len(tables) == 3
        assert [t.name for t in tables] == [
            "form_results_0001", "form_results_0002", "form_results_0003"
        ]

    def test_expand_true_aliases(self):
        tables = load_canonical(self._YAML_ALIASES, expand=True)
        assert [t.name for t in tables] == [
            "audit_log", "audit_log_archive", "audit_log_staging"
        ]

    def test_expand_true_noop_for_plain_table(self):
        tables = load_canonical(self._YAML_NO_MULTI, expand=True)
        assert len(tables) == 1
        assert tables[0].name == "users"


# ---------------------------------------------------------------------------
# dump_schema / load_canonical full round-trip
# ---------------------------------------------------------------------------

class TestDumpLoadRoundTrip:
    def test_instance_count_survives_dump_load(self):
        t = _simple_table(instance_count=500, instance_suffix_format="_{:05d}")
        yaml_str = dump_schema(t)
        restored = load_canonical(yaml_str)
        assert len(restored) == 1
        assert restored[0].instance_count == 500
        assert restored[0].instance_suffix_format == "_{:05d}"

    def test_aliases_survive_dump_load(self):
        t = _simple_table(aliases=["events_bak", "events_old"])
        yaml_str = dump_schema(t)
        restored = load_canonical(yaml_str)
        assert restored[0].aliases == ["events_bak", "events_old"]

    def test_dump_then_expand(self):
        tables = [
            _simple_table("orders", instance_count=3),
            _simple_table("customers", aliases=["customers_archive"]),
        ]
        yaml_str = dump_schema(tables)
        expanded = load_canonical(yaml_str, expand=True)
        names = [t.name for t in expanded]
        assert names == [
            "orders_0001", "orders_0002", "orders_0003",
            "customers", "customers_archive",
        ]
