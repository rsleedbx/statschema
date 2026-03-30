"""
benchmarks/test_registry.py

Unified test registry — loads test_catalog.yaml and expands the full list
of every available test spec, including the identity matrix (engine × schema)
generated programmatically.

Each TestSpec is tagged with categories that enable two key operations:
  - Filter to a named category:  registry.by_category("tpch", "app")
  - Random sampling:             registry.sample(n=10)

Inspired by Facebook Hydra's config-group pattern: every experiment is
self-describing and carries the metadata needed to select and reproduce it.

Usage
-----
    from benchmarks.test_registry import TestRegistry

    reg = TestRegistry()
    print(reg.list_categories())          # {"tpch": 6, "app": 5, ...}

    tpch_tests = reg.by_category("tpch")  # all tests tagged "tpch"
    quick_smoke = reg.sample(n=8)          # 8 random tests from everything
    app_sample  = reg.sample(n=3, categories=["app"])
"""

from __future__ import annotations

import random as _random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_CATALOG_PATH = Path(__file__).parent / "test_catalog.yaml"


@dataclass(frozen=True)
class TestSpec:
    """A single runnable test item with its full metadata."""

    id: str
    """Unique dot-path identifier, e.g. 'identity/sqlserver/tpch' or 'app.gitea'."""

    categories: frozenset[str]
    """Tags used for filtering: {'identity', 'tpch', 'sqlserver', 'qemu', 'live'}."""

    runner: str
    """How to execute: 'run_matrix' | 'pytest' | 'run_matrix_bench'."""

    description: str
    """One-line human summary."""

    engine: str | None = None
    """Primary engine gate — if set, the test is skipped when this engine is unreachable."""

    # Runner-specific fields
    schema: str | None = None
    """TPC schema name (identity tests only)."""

    pytest_file: str | None = None
    """Pytest test file path (pytest runner only)."""

    pytest_marks: list[str] = field(default_factory=list)
    """Pytest -m markers to pass (e.g. ['live'])."""

    def matches(self, *categories: str) -> bool:
        """Return True if this spec carries ANY of the given category tags."""
        return bool(self.categories & frozenset(categories))

    def __str__(self) -> str:
        cats = " ".join(f"[{c}]" for c in sorted(self.categories))
        return f"{self.id:<45} {cats}"


class TestRegistry:
    """
    Loads test_catalog.yaml and builds the full list of TestSpecs.

    Identity matrix (engine × schema) is expanded programmatically so the
    YAML stays readable.  All other test types are listed explicitly in the
    catalog and loaded verbatim.
    """

    def __init__(self, catalog_path: Path = _CATALOG_PATH) -> None:
        with catalog_path.open() as fh:
            self._catalog: dict[str, Any] = yaml.safe_load(fh)
        self._specs: list[TestSpec] = self._build()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def all(self) -> list[TestSpec]:
        """Return all test specs."""
        return list(self._specs)

    def by_category(self, *categories: str) -> list[TestSpec]:
        """Return specs that carry ANY of the given category tags."""
        cats = frozenset(categories)
        return [s for s in self._specs if s.categories & cats]

    def sample(
        self,
        n: int,
        categories: list[str] | None = None,
        seed: int | None = None,
        _prefiltered: list[TestSpec] | None = None,
    ) -> list[TestSpec]:
        """
        Return a random sample of *n* specs, optionally filtered by category.

        If *_prefiltered* is provided (e.g. already engine-gated), sample from
        that list directly rather than rebuilding from categories.
        If *n* ≥ pool size, returns the whole pool (no replacement).
        Set *seed* for a reproducible draw.
        """
        if _prefiltered is not None:
            pool = _prefiltered
        elif categories:
            pool = self.by_category(*categories)
        else:
            pool = list(self._specs)
        rng = _random.Random(seed)
        return rng.sample(pool, min(n, len(pool)))

    def list_categories(self) -> dict[str, int]:
        """Return {category: count} sorted by count descending."""
        counts: dict[str, int] = {}
        for spec in self._specs:
            for cat in spec.categories:
                counts[cat] = counts.get(cat, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def get(self, spec_id: str) -> TestSpec | None:
        """Look up a spec by exact id."""
        return next((s for s in self._specs if s.id == spec_id), None)

    # ------------------------------------------------------------------
    # Internal builders
    # ------------------------------------------------------------------

    def _build(self) -> list[TestSpec]:
        specs: list[TestSpec] = []
        specs.extend(self._build_identity_matrix())
        specs.extend(self._build_explicit_tests())
        return specs

    def _build_identity_matrix(self) -> list[TestSpec]:
        """Expand engine × schema Cartesian product into TestSpecs."""
        engine_cats: dict[str, list[str]] = self._catalog.get("engine_categories", {})
        engines: list[str] = self._catalog.get("identity_engines", [])
        schemas: list[str] = self._catalog.get("identity_schemas", [])

        specs = []
        for engine in engines:
            for schema in schemas:
                cats = frozenset(
                    ["identity", "live", engine, schema]
                    + engine_cats.get(engine, [])
                )
                specs.append(TestSpec(
                    id=f"identity/{engine}/{schema}",
                    categories=cats,
                    runner="run_matrix",
                    description=f"Identity test: {engine} × {schema}",
                    engine=engine,
                    schema=schema,
                ))
        return specs

    def _build_explicit_tests(self) -> list[TestSpec]:
        """Load the explicit test list from the catalog YAML."""
        specs = []
        for entry in self._catalog.get("tests", []):
            cats = frozenset(entry.get("categories", []))
            engine = entry.get("engine")
            # Auto-add engine tag if not already present
            if engine and engine not in cats:
                cats = cats | {engine}
            specs.append(TestSpec(
                id=entry["id"],
                categories=cats,
                runner=entry.get("runner", "pytest"),
                description=entry.get("description", ""),
                engine=engine,
                schema=None,
                pytest_file=entry.get("file"),
                pytest_marks=entry.get("marks", []),
            ))
        return specs
