"""Shared InjectionResult dataclass used by all dialect injectors."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class InjectionResult:
    dialect: str
    table: str
    rows_injected: int       # table-level row count injected
    columns_injected: int    # number of columns whose stats were injected
    columns_skipped: int     # columns not injected (unsupported type, no stats)
    warnings: list[str]

    @property
    def success(self) -> bool:
        return self.columns_injected > 0
