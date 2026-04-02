"""
benchmarks/cli_args.py

Shared argparse helpers for benchmark CLI scripts.

Each helper adds one logical flag (or a tightly-coupled pair) to any
ArgumentParser or sub-parser.  Scripts import only what they need so
defaults and help strings stay in one place.

Usage::

    from benchmarks import cli_args

    p = argparse.ArgumentParser(...)
    cli_args.add_profile_yaml_arg(p)
    cli_args.add_dialect_arg(p, default="postgres")
    cli_args.add_schema_arg(p, default="tpch")
    cli_args.add_sf_arg(p)
    cli_args.add_seed_arg(p)
    cli_args.add_verbose_arg(p)
    cli_args.add_skip_load_arg(p)
    cli_args.add_phases_arg(p, all_phases=ALL_PHASES)
    cli_args.add_no_extended_stats_arg(p)
    cli_args.add_full_stats_db2_ora_arg(p)
"""

from __future__ import annotations

import argparse

# ---------------------------------------------------------------------------
# Canonical constants (may be imported by scripts that need to list them)
# ---------------------------------------------------------------------------

SCHEMA_CHOICES: list[str] = ["tpch", "tpcb", "tpcc", "tpcds", "tpcdi", "tpce"]

DIALECT_CHOICES: list[str] = [
    "postgres", "lakebase", "neon", "cockroachdb",
    "mysql", "mariadb", "sqlserver", "oracle", "db2",
]

DIALECT_CHOICES_WITH_SQLITE: list[str] = ["sqlite"] + DIALECT_CHOICES


# ---------------------------------------------------------------------------
# Connection / profile
# ---------------------------------------------------------------------------

def add_profile_yaml_arg(
    parser: argparse.ArgumentParser,
    *,
    required: bool = True,
) -> None:
    """Add ``--profile-yaml FILE``."""
    parser.add_argument(
        "--profile-yaml",
        required=required,
        default="" if not required else None,
        metavar="FILE",
        help="Path to statschema.yaml containing named connection profiles.",
    )


# ---------------------------------------------------------------------------
# Dialect
# ---------------------------------------------------------------------------

def add_dialect_arg(
    parser: argparse.ArgumentParser,
    *,
    required: bool = False,
    default: str | None = None,
    include_sqlite: bool = False,
    help: str | None = None,
) -> None:
    """Add ``--dialect``."""
    choices = DIALECT_CHOICES_WITH_SQLITE if include_sqlite else DIALECT_CHOICES
    default_note = f" (default: {default})" if default else ""
    parser.add_argument(
        "--dialect",
        required=required,
        default=default,
        choices=choices,
        help=help or f"Database dialect{default_note}",
    )


# ---------------------------------------------------------------------------
# Schema / scale factor
# ---------------------------------------------------------------------------

def add_schema_arg(
    parser: argparse.ArgumentParser,
    *,
    default: str = "tpch",
) -> None:
    """Add ``--schema``."""
    parser.add_argument(
        "--schema",
        default=default,
        choices=SCHEMA_CHOICES,
        help=f"TPC schema (default: {default})",
    )


def add_sf_arg(
    parser: argparse.ArgumentParser,
    *,
    help: str = "Scale factor (default: 1)",
) -> None:
    """Add ``--sf``."""
    parser.add_argument(
        "--sf",
        type=float,
        default=1.0,
        help=help,
    )


# ---------------------------------------------------------------------------
# Reproducibility
# ---------------------------------------------------------------------------

def add_seed_arg(
    parser: argparse.ArgumentParser,
    *,
    default: int | None = 42,
) -> None:
    """Add ``--seed``."""
    note = f" (default: {default})" if default is not None else ""
    parser.add_argument(
        "--seed",
        type=int,
        default=default,
        help=f"Random seed for reproducibility{note}",
    )


# ---------------------------------------------------------------------------
# Verbosity
# ---------------------------------------------------------------------------

def add_verbose_arg(parser: argparse.ArgumentParser) -> None:
    """Add ``--verbose``."""
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )


# ---------------------------------------------------------------------------
# Identity pipeline flags (shared by identity_test.py and run_matrix.py)
# ---------------------------------------------------------------------------

def add_skip_load_arg(parser: argparse.ArgumentParser) -> None:
    """Add ``--skip-load``."""
    parser.add_argument(
        "--skip-load",
        action="store_true",
        help="Skip Phase A — reuse existing source_schema data",
    )


def add_phases_arg(
    parser: argparse.ArgumentParser,
    *,
    all_phases: list[str],
    default: str | None = None,
) -> None:
    """Add ``--phases LIST``.

    *default* falls back to all phases joined by comma when omitted.
    """
    resolved_default = default if default is not None else ",".join(all_phases)
    parser.add_argument(
        "--phases",
        metavar="LIST",
        default=resolved_default,
        help=(
            "Comma-separated pipeline phases to run. "
            f"Available: {', '.join(all_phases)}. "
            "Default: all phases."
        ),
    )


def add_no_extended_stats_arg(parser: argparse.ArgumentParser) -> None:
    """Add ``--no-extended-stats``."""
    parser.add_argument(
        "--no-extended-stats",
        action="store_true",
        help="Disable Phase A.5/D.5 extended statistics (Layer 3 query feature)",
    )


def add_full_stats_db2_ora_arg(parser: argparse.ArgumentParser) -> None:
    """Add ``--full-stats-db2-ora``."""
    parser.add_argument(
        "--full-stats-db2-ora",
        action="store_true",
        help=(
            "DB2 and Oracle only. Gather full per-column distribution stats on the "
            "target schema (DB2: WITH DISTRIBUTION AND DETAILED INDEXES ALL; Oracle: "
            "FOR ALL COLUMNS SIZE AUTO). Default is predicate-column-only stats, "
            "which is 5–10× faster. No effect on other engines. Use for "
            "major-release or audit runs."
        ),
    )
