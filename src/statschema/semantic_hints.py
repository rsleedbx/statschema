"""Semantic data generation hints — automatic format_pattern inference.

Three information sources, each stored separately in portable YAML
------------------------------------------------------------------
1. DDL structure (schema.yaml)  — col.name, col.type, col.length, …
2. DDL comment   (schema.yaml)  — col.comment  (from SQL COMMENT clause)
3. Statistics    (stats.yaml)   — ColumnStats.most_common_values, min/max, …

Semantic hints can look at all three sources. The resolution order inside
infer_format_pattern() is:

  1. Column-name match      — col.name vs name-pattern file
  2. Column-comment match   — col.comment (or col.description) vs comment-pattern file
  3. [planned] Stats match  — MCV values / histogram vs pattern file
  4. [planned] LLM inference — col.name + sample values sent to a foundation model

Three layers of hint configuration
------------------------------------
Option A — locale-aware built-in inference (zero config):
    Matches col.name against a shipped YAML pattern file. When the name gives
    no match, falls back to col.comment (SQL COMMENT clause text).

        from statschema.semantic_hints import infer_format_pattern
        pattern = infer_format_pattern("customer_ssn")             # → "ssn"
        pattern = infer_format_pattern("vorname",                  # → "name_first"
                      hints=load_builtin_patterns("de_DE"))
        pattern = infer_format_pattern("col_x",
                      col_comment="Customer social security number")  # → "ssn"

    Pass comment_hints=False to disable comment fallback.

    Shipped locales: en_US, de_DE.
    Name pattern files:    src/statschema/patterns/<locale>.yaml
    Comment pattern files: src/statschema/patterns/<locale>.comments.yaml

Option B — external hints.yaml (user-configurable overrides):
    A YAML file mapping column-name patterns to GenerationRule specifications.
    Applied before generation via apply_hints().

        from statschema.semantic_hints import load_hints, apply_hints
        hints = load_hints("hints.yaml")
        tables = apply_hints(tables, hints)

    hints.yaml format:

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

    apply_hints() accepts db_stats to pass column-level statistics alongside
    each column for future stats-based inference:

        tables = apply_hints(tables, hints, db_stats=my_db_stats)

Future — LLM inference (Databricks LogSentinel style):
    See _infer_format_pattern_llm() stub below.
    Intended as a fallback when Option A and B both return None.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Union

import yaml

from .model import CanonicalTableSchema, GenerationRule

if TYPE_CHECKING:
    from .stats_model import ColumnStats, DatabaseStats

# ---------------------------------------------------------------------------
# Shared: SemanticHints container and YAML loader
# (used by both Option A built-in patterns and Option B user hints)
# ---------------------------------------------------------------------------

class SemanticHints:
    """
    Column-name → GenerationRule mapping loaded from YAML.

    Each entry is a (compiled regex, GenerationRule) pair. Patterns are
    matched case-insensitively against the column name; first match wins.

    Used for both built-in locale patterns (load_builtin_patterns) and
    user-supplied overrides (load_hints).
    """

    def __init__(self, entries: list[tuple[re.Pattern, GenerationRule]]) -> None:
        self._entries = entries

    def match(self, col_name: str) -> Optional[GenerationRule]:
        """Return the first matching GenerationRule for the column name, or None."""
        for pattern, rule in self._entries:
            if pattern.search(col_name):
                return rule
        return None

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        return f"SemanticHints({len(self._entries)} rules)"


def load_hints(path: Union[str, Path]) -> SemanticHints:
    """
    Load a hints.yaml file and return a SemanticHints object.

    Accepts any YAML file that follows the hints format:

        hints:
          - pattern: "<regex>"
            generation:
              format_pattern: <name>   # or any GenerationRule fields

    Parameters
    ----------
    path   Path to the YAML file.

    Raises
    ------
    FileNotFoundError  If the file does not exist.
    ValueError         If an entry is missing the required 'pattern' key.
    """
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    entries: list[tuple[re.Pattern, GenerationRule]] = []
    for item in data.get("hints", []):
        pat_str = item.get("pattern")
        if not pat_str:
            raise ValueError(f"hints.yaml entry missing 'pattern': {item}")
        rule = GenerationRule.from_dict(item.get("generation") or {})
        entries.append((re.compile(pat_str, re.IGNORECASE), rule))
    return SemanticHints(entries)


# ---------------------------------------------------------------------------
# Option A — locale-aware built-in column-name inference
# ---------------------------------------------------------------------------

_PATTERNS_DIR = Path(__file__).parent / "patterns"


def load_builtin_patterns(
    locale: str = "en_US",
    *,
    path: Optional[Union[str, Path]] = None,
) -> SemanticHints:
    """
    Load the built-in column-name inference patterns for a locale.

    Parameters
    ----------
    locale  Locale tag matching a shipped YAML file in src/statschema/patterns/.
            Shipped locales: "en_US", "de_DE".
    path    Override: load from an arbitrary file path instead of the shipped
            locale file. Ignores the ``locale`` argument when given.

    Returns
    -------
    SemanticHints ready to pass to infer_format_pattern() or apply_hints().

    Examples
    --------
    ::

        # Default English/US patterns (used automatically by the builders)
        en = load_builtin_patterns("en_US")

        # German locale — covers vorname, nachname, plz, strasse, firma, …
        de = load_builtin_patterns("de_DE")
        tables = apply_hints(tables, de)

        # Custom locale file
        custom = load_builtin_patterns(path="my_patterns.yaml")

    Raises
    ------
    FileNotFoundError  If the locale file does not exist.
    """
    if path is not None:
        return load_hints(path)

    locale_path = _PATTERNS_DIR / f"{locale}.yaml"
    if not locale_path.exists():
        available = sorted(p.stem for p in _PATTERNS_DIR.glob("*.yaml"))
        raise FileNotFoundError(
            f"No built-in patterns for locale '{locale}'. "
            f"Shipped locales: {available}. "
            f"Use path= to load a custom file."
        )
    return load_hints(locale_path)


# Loaded once at module import — default locale used by infer_format_pattern().
_DEFAULT_HINTS: SemanticHints = load_builtin_patterns("en_US")


def load_builtin_comment_patterns(
    locale: str = "en_US",
    *,
    path: Optional[Union[str, Path]] = None,
) -> SemanticHints:
    """
    Load the built-in column-comment inference patterns for a locale.

    Comment patterns are matched against a column's COMMENT text
    (``col.description``) rather than its name.  They use word-boundary
    anchors and natural-language phrasing to work accurately against
    free-form prose.

    Parameters
    ----------
    locale  Locale tag matching a shipped file in src/statschema/patterns/
            named ``<locale>.comments.yaml``.
            Shipped locales: "en_US", "de_DE".
    path    Override: load from an arbitrary file path instead of the shipped
            locale file.  Ignores the ``locale`` argument when given.

    Returns
    -------
    SemanticHints ready to pass as ``comment_hints`` to
    infer_format_pattern() or apply_hints().

    Examples
    --------
    ::

        # Default English/US comment patterns (used automatically)
        en_comments = load_builtin_comment_patterns("en_US")

        # German locale comment patterns
        de_comments = load_builtin_comment_patterns("de_DE")
        tables = apply_hints(tables, hints, comment_hints=de_comments)

    Raises
    ------
    FileNotFoundError  If the locale file does not exist.
    """
    if path is not None:
        return load_hints(path)

    locale_path = _PATTERNS_DIR / f"{locale}.comments.yaml"
    if not locale_path.exists():
        available = sorted(p.stem for p in _PATTERNS_DIR.glob("*.comments.yaml"))
        raise FileNotFoundError(
            f"No built-in comment patterns for locale '{locale}'. "
            f"Shipped locales: {available}. "
            f"Use path= to load a custom file."
        )
    return load_hints(locale_path)


# Loaded once at module import — default comment patterns for infer_format_pattern().
_DEFAULT_COMMENT_HINTS: SemanticHints = load_builtin_comment_patterns("en_US")


def infer_format_pattern(
    col_name: str,
    hints: Optional[SemanticHints] = None,
    *,
    col_comment: Optional[str] = None,
    col_description: Optional[str] = None,
    comment_hints: Union[SemanticHints, bool, None] = None,
    col_stats: Optional["ColumnStats"] = None,
) -> Optional[str]:
    """
    Infer a format_pattern from a column name, with fallback to the column's
    DDL comment text.

    Resolution order:
      1. Column-name match against ``hints`` (en_US built-in by default).
      2. Comment-text match against ``comment_hints`` using ``col_comment``
         (from the SQL COMMENT clause, stored as ``CanonicalColumn.comment``).
         Falls back to ``col_description`` when ``col_comment`` is not given.
      3. [planned] Stats-based match using ``col_stats`` (MCV values, etc.).
      4. Returns ``None`` when nothing matches.

    Parameters
    ----------
    col_name        Column name to classify.
    hints           SemanticHints from load_builtin_patterns() or load_hints().
                    When None, the en_US built-in name patterns are used.
    col_comment     Verbatim SQL COMMENT clause text (``CanonicalColumn.comment``).
                    When provided and the name match returns None, this text is
                    matched against comment_hints.
    col_description Human-written column description (``CanonicalColumn.description``).
                    Used as a fallback text source when col_comment is None.
    comment_hints   SemanticHints for comment/description matching.
                    None (default) → use the en_US built-in comment patterns.
                    False          → skip comment matching entirely.
                    SemanticHints  → use the provided object.
    col_stats       ColumnStats for this column (null_fraction, n_distinct, MCVs,
                    histogram bounds, etc.).  Reserved for future stats-based
                    semantic inference; currently unused.

    Returns
    -------
    A named format_pattern string (e.g. "ssn", "email") or None.

    Examples
    --------
    >>> infer_format_pattern("customer_ssn")
    'ssn'
    >>> infer_format_pattern("email_address")
    'email'
    >>> infer_format_pattern("vorname", hints=load_builtin_patterns("de_DE"))
    'name_first'
    >>> infer_format_pattern("col_x", col_comment="Customer social security number")
    'ssn'
    >>> infer_format_pattern("amount")
    None
    """
    h = hints if hints is not None else _DEFAULT_HINTS
    rule = h.match(col_name)
    if rule and rule.format_pattern:
        return rule.format_pattern

    # Fallback: match the column comment (DDL source) or description (human/LLM)
    # col_comment takes priority; col_description is a secondary fallback.
    comment_text = col_comment or col_description
    if comment_text and comment_hints is not False:
        ch: SemanticHints = (
            comment_hints if isinstance(comment_hints, SemanticHints)
            else _DEFAULT_COMMENT_HINTS
        )
        comment_rule = ch.match(comment_text)
        if comment_rule and comment_rule.format_pattern:
            return comment_rule.format_pattern

    # Future: stats-based inference using col_stats.most_common_values, etc.
    # _ = col_stats  # reserved

    return None


# ---------------------------------------------------------------------------
# Option B — external hints.yaml (user-configurable overrides)
# ---------------------------------------------------------------------------

def apply_hints(
    tables: list[CanonicalTableSchema],
    hints: SemanticHints,
    *,
    comment_hints: Union[SemanticHints, bool, None] = None,
    db_stats: Optional["DatabaseStats"] = None,
    override: bool = False,
) -> list[CanonicalTableSchema]:
    """
    Apply semantic hints to canonical tables.

    For each column without an existing GenerationRule, checks:
      1. Name hints — col.name vs the hints patterns.
      2. Comment hints — col.comment (DDL COMMENT clause, from schema.yaml) vs
         comment_hints patterns.  Falls back to col.description (human text)
         when col.comment is absent.
      3. [planned] Stats — col_stats from db_stats for future MCV-based inference.

    Parameters
    ----------
    tables        List of canonical tables to annotate.
    hints         SemanticHints for column-name matching (from load_hints() or
                  load_builtin_patterns()).
    comment_hints SemanticHints for column-comment matching.
                  None (default) → use the en_US built-in comment patterns.
                  False          → skip comment matching entirely.
                  SemanticHints  → use the provided object.
    db_stats      DatabaseStats containing per-column statistics.  When provided,
                  each column's ColumnStats are passed to infer_format_pattern()
                  for future stats-based inference.  Currently unused by the
                  built-in patterns; reserved for the stats-match layer.
    override      When True, replace an existing GenerationRule with the
                  matched rule.  When False (default), only annotate columns
                  that have no existing rule.

    Returns
    -------
    The same table objects, mutated in-place.
    """
    from .stats_model import DatabaseStats as _DatabaseStats  # local to avoid circular import

    ch: Optional[SemanticHints] = (
        None if comment_hints is False
        else (comment_hints if isinstance(comment_hints, SemanticHints)
              else _DEFAULT_COMMENT_HINTS)
    )

    for table in tables:
        # Look up per-table stats once per table (O(tables) lookup)
        table_stats = db_stats.table_stats(table.name) if db_stats is not None else None

        for col in table.columns:
            if col.generation is not None and not override:
                continue

            # Resolve column-level stats for this column (None when unavailable)
            col_stats = table_stats.column_stats(col.name) if table_stats else None

            matched = hints.match(col.name)

            # Fallback to comment/description text when the name gives no result
            if matched is None and ch is not None:
                comment_text = col.comment or col.description
                if comment_text:
                    matched = ch.match(comment_text)

            # Future: stats-based match using col_stats.most_common_values, etc.

            if matched is not None:
                col.generation = matched

    return tables


# ---------------------------------------------------------------------------
# Future: LLM inference (Databricks LogSentinel style)
# ---------------------------------------------------------------------------

def _infer_format_pattern_llm(
    col_name: str,
    sample_values: Optional[list] = None,
    *,
    model: str = "databricks-meta-llama-3-3-70b-instruct",
) -> Optional[str]:
    """
    [NOT IMPLEMENTED] Infer a format_pattern using an LLM.

    Intended to mirror Databricks LogSentinel's approach: a hierarchical
    multi-model LLM pipeline that classifies column semantics from column name,
    sample data, and table context.

    When implemented, this would:
      1. Compose a prompt: column name + optional sample values + the list of
         known format_pattern values from GenerationRule.
      2. Call a Databricks Foundation Model API endpoint via databricks-sdk
         WorkspaceClient.serving_endpoints.query() (e.g. DBRX, Llama-3).
      3. Parse the response to extract one of the named format_pattern values,
         or None if the model signals no semantic match.
      4. Cache results per (table_name, column_name) to avoid redundant calls.

    Integration point in the pipeline:
      - Called as a fallback when infer_format_pattern() (Option A) returns None
        and no hints.yaml entry matches (Option B).
      - Enabled via hints.yaml top-level flag: llm_inference: true.

    Reference:
      https://www.databricks.com/blog/logsentinel-how-databricks-uses-databricks-for-llm-powered-pii-detection-and-governance
    """
    raise NotImplementedError(
        "_infer_format_pattern_llm is not yet implemented. "
        "Use infer_format_pattern() (Option A) or load_hints() (Option B) instead."
    )
