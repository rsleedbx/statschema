"""
Canonical schema model: independent of source format (YData YAML, pipeline YAML, DB dumps).
Used as the intermediate representation before converting to dbldatagen.
"""

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class GenerationRule:
    """Optional data generation hints (min, max, values, etc.)."""

    min_value: Optional[Any] = None
    max_value: Optional[Any] = None
    values: Optional[list[Any]] = None
    weights: Optional[list[float]] = None
    max_length: Optional[int] = None
    unique: bool = False
    # For future: distribution, format strings, etc.
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CanonicalColumn:
    """Column in the canonical schema."""

    name: str
    type: str  # Normalized: integer, long, string, float, double, boolean, timestamp, etc.
    description: Optional[str] = None
    primary_key: bool = False
    constraints: Optional[dict[str, Any]] = None
    generation: Optional[GenerationRule] = None
    # For foreign_key from YData: reference to (table, column)
    references: Optional[tuple[str, str]] = None


@dataclass
class CanonicalTableSchema:
    """Table in the canonical schema."""

    name: str
    columns: list[CanonicalColumn]
    description: Optional[str] = None
    # For multi-table: foreign key list at table level (alternative to column.references)
    foreign_keys: Optional[list[dict[str, str]]] = None
