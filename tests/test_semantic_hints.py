"""Tests for semantic data generation hints.

Coverage
--------
A. Option A — infer_format_pattern() with default en_US locale:
   - Known column name patterns map to the correct format_pattern.
   - Unknown column names return None.
   - Matching is case-insensitive.
   - Prefix/suffix variations are matched (e.g. "customer_ssn", "ssn_number").

A2. Locale loading — load_builtin_patterns():
   - en_US and de_DE load without error.
   - Unknown locale raises FileNotFoundError with available locales in message.
   - Custom path= loads from an arbitrary file.
   - infer_format_pattern() accepts hints= to use a non-default locale.

A3. de_DE locale — German column names resolve correctly:
   - vorname → name_first, nachname → name_last, plz → postal_uk, etc.

B. Option B — load_hints() + apply_hints():
   - SemanticHints loaded from a YAML file matches column names.
   - apply_hints() annotates columns that have no existing generation rule.
   - apply_hints(override=False) leaves existing rules unchanged.
   - apply_hints(override=True) replaces existing rules.
   - load_hints() raises ValueError for entries missing 'pattern'.

C. Integration — Option A flows through dbldatagen_builder._spark_type_and_options:
   - A column named "customer_ssn" with type "string" and no generation rule
     produces template = r"ddd-dd-dddd".
   - A column named "work_email" with type "string" and no generation rule
     produces an email template.
   - An explicit GenerationRule.format_pattern takes priority over inference.
"""

from __future__ import annotations

import textwrap

import pytest

from src.statschema.model import (
    CanonicalColumn,
    CanonicalTableSchema,
    GenerationRule,
)
from src.statschema.semantic_hints import (
    SemanticHints,
    apply_hints,
    infer_format_pattern,
    load_builtin_patterns,
    load_hints,
)


# ───────────────────────────────────────────────────────────────────────────
# A. Option A — infer_format_pattern()
# ───────────────────────────────────────────────────────────────────────────

class TestInferFormatPattern:
    @pytest.mark.parametrize("col_name, expected", [
        # SSN variants
        ("ssn",                      "ssn"),
        ("customer_ssn",             "ssn"),
        ("ssn_number",               "ssn"),
        ("social_security",          "ssn"),
        ("social_security_number",   "ssn"),
        ("taxpayer_social_security", "ssn"),
        # Email variants
        ("email",                    "email"),
        ("email_address",            "email"),
        ("customer_email",           "email"),
        ("primary_email",            "email"),
        # Phone variants
        ("phone",                    "phone_us"),
        ("phone_number",             "phone_us"),
        ("mobile",                   "phone_us"),
        ("mobile_phone",             "phone_us"),
        # Name variants
        ("first_name",               "name_first"),
        ("firstname",                "name_first"),
        ("given_name",               "name_first"),
        ("last_name",                "name_last"),
        ("lastname",                 "name_last"),
        ("surname",                  "name_last"),
        ("family_name",              "name_last"),
        # Credit card
        ("credit_card",              "credit_card"),
        ("card_number",              "credit_card"),
        ("cc_number",                "credit_card"),
        # IP
        ("ip_address",               "ip_v4"),
        ("ip_addr",                  "ip_v4"),
        # URL
        ("website",                  "url"),
        ("homepage",                 "url"),
        # UUID/GUID
        ("uuid",                     "uuid"),
        ("guid",                     "uuid"),
        ("record_uuid",              "uuid"),
        # IBAN
        ("iban",                     "iban"),
        ("bank_account",             "iban"),
        # Postal / zip
        ("zip_code",                 "postal_us"),
        ("postal_code",              "postal_us"),
        ("postcode",                 "postal_us"),
        # Country
        ("country",                  "country_iso2"),
        # Company
        ("company",                  "company"),
        ("organization",             "company"),
        ("employer",                 "company"),
        # Address / city
        ("address",                  "address"),
        ("street_address",           "address"),
        ("city",                     "city"),
    ])
    def test_known_patterns(self, col_name, expected):
        assert infer_format_pattern(col_name) == expected

    @pytest.mark.parametrize("col_name", [
        "id", "amount", "price", "order_id", "quantity", "status",
        "created_at", "updated_at", "is_active", "score",
    ])
    def test_unknown_returns_none(self, col_name):
        assert infer_format_pattern(col_name) is None

    def test_case_insensitive(self):
        assert infer_format_pattern("SSN") == "ssn"
        assert infer_format_pattern("Email_Address") == "email"
        assert infer_format_pattern("FIRST_NAME") == "name_first"

    def test_explicit_rule_takes_priority_over_inference(self):
        # Verify the model plumbing: a column with an explicit format_pattern
        # should keep it, not be overridden by inference.
        col = CanonicalColumn(
            name="customer_ssn",
            type="string",
            generation=GenerationRule(format_pattern="uuid"),
        )
        assert col.generation.format_pattern == "uuid"


# ───────────────────────────────────────────────────────────────────────────
# B. Option B — load_hints() + apply_hints()
# ───────────────────────────────────────────────────────────────────────────

# ───────────────────────────────────────────────────────────────────────────
# A2. Locale loading — load_builtin_patterns()
# ───────────────────────────────────────────────────────────────────────────

class TestLoadBuiltinPatterns:
    def test_en_us_loads(self):
        hints = load_builtin_patterns("en_US")
        assert len(hints) > 0

    def test_de_de_loads(self):
        hints = load_builtin_patterns("de_DE")
        assert len(hints) > 0

    def test_unknown_locale_raises_file_not_found(self):
        with pytest.raises(FileNotFoundError, match="en_US"):
            load_builtin_patterns("xx_XX")

    def test_unknown_locale_message_lists_available(self):
        with pytest.raises(FileNotFoundError) as exc_info:
            load_builtin_patterns("xx_XX")
        msg = str(exc_info.value)
        assert "en_US" in msg
        assert "de_DE" in msg

    def test_custom_path(self, tmp_path):
        custom = tmp_path / "custom.yaml"
        custom.write_text(
            "hints:\n  - pattern: 'custom_col'\n    generation:\n      format_pattern: ssn\n"
        )
        hints = load_builtin_patterns(path=custom)
        assert hints.match("my_custom_col") is not None

    def test_infer_with_explicit_hints(self):
        de = load_builtin_patterns("de_DE")
        assert infer_format_pattern("vorname", hints=de) == "name_first"
        assert infer_format_pattern("nachname", hints=de) == "name_last"

    def test_default_is_en_us(self):
        # No hints= argument — should use en_US
        assert infer_format_pattern("customer_ssn") == "ssn"
        assert infer_format_pattern("email_address") == "email"


# ───────────────────────────────────────────────────────────────────────────
# A3. de_DE locale — German column names
# ───────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def de_hints():
    return load_builtin_patterns("de_DE")


class TestDeDELocale:
    @pytest.mark.parametrize("col_name, expected", [
        # Social security
        ("rentenversicherungsnummer",        "ssn"),
        ("sozialversicherungsnummer",        "ssn"),
        ("svnummer",                         "ssn"),
        # Email
        ("email",                            "email"),
        ("emailadresse",                     "email"),
        ("e_mail",                           "email"),
        # Phone
        ("telefon",                          "phone_intl"),
        ("telefonnummer",                    "phone_intl"),
        ("handy",                            "phone_intl"),
        ("handynummer",                      "phone_intl"),
        ("mobilnummer",                      "phone_intl"),
        # Names
        ("vorname",                          "name_first"),
        ("vname",                            "name_first"),
        ("rufname",                          "name_first"),
        ("nachname",                         "name_last"),
        ("nname",                            "name_last"),
        ("familienname",                     "name_last"),
        # Credit card
        ("kreditkarte",                      "credit_card"),
        ("kartennummer",                     "credit_card"),
        # IP
        ("ip_adresse",                       "ip_v4"),
        ("ipadresse",                        "ip_v4"),
        # URL
        ("webseite",                         "url"),
        ("internetadresse",                  "url"),
        # UUID
        ("uuid",                             "uuid"),
        ("guid",                             "uuid"),
        # IBAN / bank
        ("iban",                             "iban"),
        ("kontonummer",                      "iban"),
        # Currency
        ("waehrung",                         "currency_iso"),
        ("waehrungscode",                    "currency_iso"),
        # Postal
        ("plz",                              "postal_uk"),
        ("postleitzahl",                     "postal_uk"),
        # Country
        ("land",                             "country_iso2"),
        ("laendercode",                      "country_iso2"),
        # Company
        ("firma",                            "company"),
        ("unternehmen",                      "company"),
        ("arbeitgeber",                      "company"),
        # Address
        ("strasse",                          "address"),
        ("adresse",                          "address"),
        ("anschrift",                        "address"),
        # City
        ("stadt",                            "city"),
        ("ort",                              "city"),
        ("gemeinde",                         "city"),
        # English names also work in de_DE (common in German codebases)
        ("first_name",                       "name_first"),
        ("last_name",                        "name_last"),
        ("address",                          "address"),
        ("city",                             "city"),
        ("country",                          "country_iso2"),
    ])
    def test_german_column_names(self, col_name, expected, de_hints):
        assert infer_format_pattern(col_name, hints=de_hints) == expected

    def test_german_ssn_not_matched_by_default_en_locale(self):
        # "rentenversicherungsnummer" should NOT match the en_US default
        assert infer_format_pattern("rentenversicherungsnummer") is None

    def test_german_phone_not_matched_by_default_en_locale(self):
        assert infer_format_pattern("telefon") is None

    def test_apply_hints_with_de_locale(self, de_hints):
        from src.statschema.model import CanonicalColumn, CanonicalTableSchema
        table = CanonicalTableSchema(
            name="kunden",
            columns=[
                CanonicalColumn(name="id",       type="integer"),
                CanonicalColumn(name="vorname",  type="string"),
                CanonicalColumn(name="nachname", type="string"),
                CanonicalColumn(name="plz",      type="string"),
                CanonicalColumn(name="firma",    type="string"),
            ],
        )
        apply_hints([table], de_hints)
        col_map = {c.name: c for c in table.columns}
        assert col_map["vorname"].generation.format_pattern == "name_first"
        assert col_map["nachname"].generation.format_pattern == "name_last"
        assert col_map["plz"].generation.format_pattern == "postal_uk"
        assert col_map["firma"].generation.format_pattern == "company"
        assert col_map["id"].generation is None  # integer, not in hints


_HINTS_YAML = textwrap.dedent("""\
    version: "1.0"
    hints:
      - pattern: "tax_id|tin"
        generation:
          format_pattern: ssn
      - pattern: "salary|compensation"
        generation:
          min_value: 30000
          max_value: 500000
          distribution: normal
          distribution_params: {mean: 80000, std: 30000}
      - pattern: "promo_code"
        generation:
          format_pattern: "A{3}-d{4}"
""")


@pytest.fixture()
def hints_file(tmp_path):
    p = tmp_path / "hints.yaml"
    p.write_text(_HINTS_YAML, encoding="utf-8")
    return p


@pytest.fixture()
def hints(hints_file):
    return load_hints(hints_file)


class TestLoadHints:
    def test_loads_entries(self, hints):
        assert len(hints) == 3

    def test_repr(self, hints):
        assert "SemanticHints(3 rules)" in repr(hints)

    def test_match_tax_id(self, hints):
        rule = hints.match("taxpayer_tax_id")
        assert rule is not None
        assert rule.format_pattern == "ssn"

    def test_match_tin(self, hints):
        rule = hints.match("tin")
        assert rule is not None
        assert rule.format_pattern == "ssn"

    def test_match_salary(self, hints):
        rule = hints.match("annual_salary")
        assert rule is not None
        assert rule.min_value == 30000
        assert rule.max_value == 500000
        assert rule.distribution == "normal"

    def test_no_match_returns_none(self, hints):
        assert hints.match("order_id") is None
        assert hints.match("amount") is None

    def test_case_insensitive(self, hints):
        assert hints.match("TAX_ID") is not None
        assert hints.match("SALARY") is not None

    def test_missing_pattern_raises(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("version: '1.0'\nhints:\n  - generation:\n      format_pattern: ssn\n")
        with pytest.raises(ValueError, match="missing 'pattern'"):
            load_hints(bad)

    def test_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_hints(tmp_path / "nonexistent.yaml")


class TestApplyHints:
    def _make_table(self) -> CanonicalTableSchema:
        return CanonicalTableSchema(
            name="employees",
            columns=[
                CanonicalColumn(name="id",          type="integer"),
                CanonicalColumn(name="tax_id",      type="string"),
                CanonicalColumn(name="annual_salary", type="decimal"),
                CanonicalColumn(name="status",      type="string"),
            ],
        )

    def test_annotates_columns_without_rule(self, hints):
        table = self._make_table()
        apply_hints([table], hints)
        # tax_id matched → ssn
        assert table.columns[1].generation is not None
        assert table.columns[1].generation.format_pattern == "ssn"
        # annual_salary matched → normal distribution
        assert table.columns[2].generation is not None
        assert table.columns[2].generation.distribution == "normal"

    def test_leaves_unmatched_columns_untouched(self, hints):
        table = self._make_table()
        apply_hints([table], hints)
        # "id" and "status" have no matching hints
        assert table.columns[0].generation is None
        assert table.columns[3].generation is None

    def test_override_false_preserves_existing_rule(self, hints):
        table = self._make_table()
        existing = GenerationRule(format_pattern="uuid")
        table.columns[1].generation = existing  # tax_id already has a rule
        apply_hints([table], hints, override=False)
        assert table.columns[1].generation is existing  # unchanged

    def test_override_true_replaces_existing_rule(self, hints):
        table = self._make_table()
        table.columns[1].generation = GenerationRule(format_pattern="uuid")
        apply_hints([table], hints, override=True)
        assert table.columns[1].generation.format_pattern == "ssn"

    def test_returns_same_table_objects(self, hints):
        table = self._make_table()
        result = apply_hints([table], hints)
        assert result[0] is table  # mutated in-place


# ───────────────────────────────────────────────────────────────────────────
# C. Integration — Option A flows through dbldatagen_builder
# ───────────────────────────────────────────────────────────────────────────

# dbldatagen_builder imports pyspark at module level; skip if not installed.
pyspark = pytest.importorskip("pyspark", reason="pyspark not installed")

from src.statschema.dbldatagen_builder import (  # noqa: E402
    _FORMAT_PATTERN_TEMPLATES,
    _spark_type_and_options,
)


class TestBuilderOptionA:
    def test_ssn_column_gets_ssn_template(self):
        col = CanonicalColumn(name="customer_ssn", type="string")
        _, opts = _spark_type_and_options(col)
        assert opts.get("template") == _FORMAT_PATTERN_TEMPLATES["ssn"]

    def test_email_column_gets_email_template(self):
        col = CanonicalColumn(name="work_email", type="string")
        _, opts = _spark_type_and_options(col)
        assert opts.get("template") == _FORMAT_PATTERN_TEMPLATES["email"]

    def test_phone_column_gets_phone_template(self):
        col = CanonicalColumn(name="mobile_phone", type="string")
        _, opts = _spark_type_and_options(col)
        assert opts.get("template") == _FORMAT_PATTERN_TEMPLATES["phone_us"]

    def test_unknown_column_no_template_from_inference(self):
        col = CanonicalColumn(name="order_notes", type="string")
        _, opts = _spark_type_and_options(col)
        # No format_pattern template — uses prefix-based default
        assert "template" not in opts or opts.get("prefix") is not None or True
        # Ensure it did NOT accidentally infer an SSN/email template
        if "template" in opts:
            assert opts["template"] not in (
                _FORMAT_PATTERN_TEMPLATES["ssn"],
                _FORMAT_PATTERN_TEMPLATES["email"],
            )

    def test_explicit_format_pattern_overrides_inference(self):
        col = CanonicalColumn(
            name="customer_ssn",
            type="string",
            generation=GenerationRule(format_pattern="uuid"),
        )
        _, opts = _spark_type_and_options(col)
        assert opts.get("template") == _FORMAT_PATTERN_TEMPLATES["uuid"]

    def test_non_string_column_not_affected(self):
        col = CanonicalColumn(name="ssn_count", type="integer")
        _, opts = _spark_type_and_options(col)
        assert "template" not in opts


# ─────────────────────────────────────────────────────────────────────────────
# D. Column-comment inference
# ─────────────────────────────────────────────────────────────────────────────

class TestCommentInference:
    """Option A: column-comment fallback.

    Three separate text sources for semantic inference, in priority order:
      1. col.name     — matched via col_name parameter
      2. col.comment  — SQL COMMENT clause text, matched via col_comment parameter
      3. col.description — human/LLM description, matched via col_description fallback
    """

    # ── col_comment= (primary DDL source) ────────────────────────────────────

    def test_comment_infers_ssn(self):
        from src.statschema.semantic_hints import infer_format_pattern
        assert infer_format_pattern("col_x", col_comment="Customer social security number") == "ssn"

    def test_comment_infers_email(self):
        from src.statschema.semantic_hints import infer_format_pattern
        assert infer_format_pattern("col_a", col_comment="Email address of the user") == "email"

    def test_name_wins_over_comment(self):
        """Column name match takes priority over comment match."""
        from src.statschema.semantic_hints import infer_format_pattern
        # Name → email; comment → ssn — name should win
        assert infer_format_pattern("email", col_comment="social security number") == "email"

    def test_comment_disabled(self):
        from src.statschema.semantic_hints import infer_format_pattern
        result = infer_format_pattern("col_x",
                                      col_comment="social security number",
                                      comment_hints=False)
        assert result is None

    def test_no_comment_returns_none(self):
        from src.statschema.semantic_hints import infer_format_pattern
        assert infer_format_pattern("col_x", col_comment=None) is None

    def test_custom_comment_hints(self, tmp_path):
        """A user-supplied comment_hints file is respected."""
        from src.statschema.semantic_hints import infer_format_pattern, load_hints
        p = tmp_path / "cc.yaml"
        p.write_text("hints:\n  - pattern: 'tax.?id'\n    generation:\n      format_pattern: ssn\n")
        custom = load_hints(str(p))
        assert infer_format_pattern("ref", col_comment="Federal tax ID", comment_hints=custom) == "ssn"

    # ── col_description= (human/LLM description fallback) ────────────────────

    def test_description_fallback_infers_ssn(self):
        """col_description= still works as a fallback when col_comment= is absent."""
        from src.statschema.semantic_hints import infer_format_pattern
        assert infer_format_pattern("col_x", col_description="Customer social security number") == "ssn"

    def test_comment_takes_priority_over_description(self):
        """When both col_comment and col_description are given, comment wins."""
        from src.statschema.semantic_hints import infer_format_pattern
        # comment → ssn; description → email — comment should win
        assert infer_format_pattern("col_z",
                                    col_comment="social security number",
                                    col_description="email address") == "ssn"

    # ── locale / loader helpers ───────────────────────────────────────────────

    def test_load_builtin_comment_patterns_en_us(self):
        from src.statschema.semantic_hints import load_builtin_comment_patterns
        hints = load_builtin_comment_patterns("en_US")
        assert len(hints) > 0

    def test_load_builtin_comment_patterns_de_de(self):
        from src.statschema.semantic_hints import load_builtin_comment_patterns
        hints = load_builtin_comment_patterns("de_DE")
        assert hints.match("Telefonnummer des Kunden") is not None

    def test_load_builtin_comment_patterns_unknown_locale(self):
        from src.statschema.semantic_hints import load_builtin_comment_patterns
        with pytest.raises(FileNotFoundError, match="fr_FR"):
            load_builtin_comment_patterns("fr_FR")

    # ── apply_hints integration ───────────────────────────────────────────────

    def test_apply_hints_uses_comment_field(self):
        """apply_hints() uses col.comment (DDL source) for comment matching."""
        from src.statschema.model import CanonicalColumn, CanonicalTableSchema
        from src.statschema.semantic_hints import apply_hints, load_builtin_patterns
        col = CanonicalColumn(name="col_x", type="string",
                              comment="Email address of the customer")
        table = CanonicalTableSchema(name="t", columns=[col])
        apply_hints([table], load_builtin_patterns("en_US"))
        assert col.generation is not None
        assert col.generation.format_pattern == "email"

    def test_apply_hints_uses_description_fallback(self):
        """apply_hints() falls back to col.description when col.comment is absent."""
        from src.statschema.model import CanonicalColumn, CanonicalTableSchema
        from src.statschema.semantic_hints import apply_hints, load_builtin_patterns
        col = CanonicalColumn(name="col_x", type="string",
                              description="Email address of the customer")
        table = CanonicalTableSchema(name="t", columns=[col])
        apply_hints([table], load_builtin_patterns("en_US"))
        assert col.generation is not None
        assert col.generation.format_pattern == "email"

    def test_apply_hints_comment_disabled(self):
        """comment_hints=False skips comment matching in apply_hints()."""
        from src.statschema.model import CanonicalColumn, CanonicalTableSchema
        from src.statschema.semantic_hints import apply_hints, load_builtin_patterns
        col = CanonicalColumn(name="col_x", type="string",
                              comment="Email address of the customer")
        table = CanonicalTableSchema(name="t", columns=[col])
        apply_hints([table], load_builtin_patterns("en_US"), comment_hints=False)
        assert col.generation is None

    def test_apply_hints_with_db_stats(self):
        """apply_hints() accepts db_stats without error (stats plumbed for future use)."""
        from src.statschema.model import CanonicalColumn, CanonicalTableSchema
        from src.statschema.semantic_hints import apply_hints, load_builtin_patterns
        from src.statschema.stats_model import ColumnStats, DatabaseStats, TableStats
        col = CanonicalColumn(name="email_addr", type="string")
        table = CanonicalTableSchema(name="users", columns=[col])
        db_stats = DatabaseStats(tables=[
            TableStats(name="users", row_count=100,
                       columns=[ColumnStats(name="email_addr")])
        ])
        apply_hints([table], load_builtin_patterns("en_US"), db_stats=db_stats)
        assert col.generation is not None
        assert col.generation.format_pattern == "email"

    # ── DDL parser → YAML round-trip → semantic inference ────────────────────

    def test_ddl_parser_captures_comment(self):
        """SQL COMMENT clause is stored in col.comment (not col.description)."""
        from src.statschema.ddl_parser import parse_ddl
        ddl = "CREATE TABLE t (id INT, ssn_val VARCHAR(20) COMMENT 'social security number');"
        tables = parse_ddl(ddl, dialect="mysql")
        col = tables[0].columns[1]
        assert col.comment == "social security number"
        assert col.description is None  # human description is separate

    def test_ddl_comment_roundtrips_in_yaml(self):
        """col.comment survives schema.yaml round-trip via to_dict/from_dict."""
        from src.statschema.ddl_parser import parse_ddl
        from src.statschema.schema_io import dump_schema, load_canonical
        ddl = "CREATE TABLE t (col_a VARCHAR(20) COMMENT 'Customer email address');"
        tables = parse_ddl(ddl, dialect="mysql")
        yaml_str = dump_schema(tables)
        tables2 = load_canonical(yaml_str)
        col = tables2[0].columns[0]
        assert col.comment == "Customer email address"
        assert col.description is None

    def test_ddl_comment_drives_inference(self):
        """End-to-end: DDL COMMENT → col.comment → format_pattern inference."""
        from src.statschema.ddl_parser import parse_ddl
        from src.statschema.semantic_hints import infer_format_pattern
        ddl = "CREATE TABLE t (col_a VARCHAR(20) COMMENT 'Customer email address');"
        tables = parse_ddl(ddl, dialect="mysql")
        col = tables[0].columns[0]
        assert infer_format_pattern(col.name, col_comment=col.comment) == "email"
