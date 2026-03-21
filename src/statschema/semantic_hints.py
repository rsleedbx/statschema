"""Semantic data generation hints — automatic format_pattern inference.

Three layers, applied in priority order
----------------------------------------
Option A — locale-aware column-name inference (zero config):
    Matches a column name against a shipped YAML pattern file and returns a
    format_pattern. Default locale is en_US. Runs automatically inside
    to_dbldatagen_specs() and to_v1_plan().

        from statschema.semantic_hints import infer_format_pattern
        pattern = infer_format_pattern("customer_ssn")          # → "ssn"
        pattern = infer_format_pattern("vorname",               # → "name_first"
                      hints=load_builtin_patterns("de_DE"))

    Shipped locales: en_US, de_DE.
    Pattern files live in src/statschema/patterns/<locale>.yaml and use the
    same format as hints.yaml, so they can be inspected and edited without
    touching Python code.

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

Future — LLM inference (Databricks LogSentinel style):
    See _infer_format_pattern_llm() stub below.
    Intended as a fallback when Option A and B both return None.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Union

import yaml

from .model import CanonicalTableSchema, GenerationRule

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


def infer_format_pattern(
    col_name: str,
    hints: Optional[SemanticHints] = None,
) -> Optional[str]:
    """
    Infer a format_pattern from a column name.

    Uses the built-in en_US patterns by default. Pass ``hints`` to use a
    different locale or a custom SemanticHints object.

    Called automatically by to_dbldatagen_specs() and to_v1_plan() with no
    ``hints`` argument (en_US default). For locale-specific inference, pre-
    annotate tables with apply_hints() before calling the builders.

    Parameters
    ----------
    col_name  Column name to classify.
    hints     Optional SemanticHints from load_builtin_patterns() or
              load_hints(). When None, the en_US built-in patterns are used.

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
    >>> infer_format_pattern("amount")
    None
    """
    h = hints if hints is not None else _DEFAULT_HINTS
    rule = h.match(col_name)
    return rule.format_pattern if rule else None


# ---------------------------------------------------------------------------
# Option B — external hints.yaml (user-configurable overrides)
# ---------------------------------------------------------------------------

def apply_hints(
    tables: list[CanonicalTableSchema],
    hints: SemanticHints,
    *,
    override: bool = False,
) -> list[CanonicalTableSchema]:
    """
    Apply semantic hints to canonical tables.

    For each column without an existing GenerationRule, checks the hints for
    a matching pattern and attaches the corresponding GenerationRule.

    Parameters
    ----------
    tables    List of canonical tables to annotate.
    hints     SemanticHints from load_hints() or load_builtin_patterns().
    override  When True, replace existing GenerationRule with the hints match.
              When False (default), only annotate columns with no existing rule.

    Returns
    -------
    The same table objects, mutated in-place.
    """
    for table in tables:
        for col in table.columns:
            if col.generation is not None and not override:
                continue
            matched = hints.match(col.name)
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
