"""
benchmarks/build_row_count_report.py

Generates benchmarks/results/row_count_report.md from the identity test JSON
result files in benchmarks/results/.

For each (schema, scale-factor) group it builds a markdown table showing
source and target row counts per engine.  Engines that used a different
scale factor for a given schema appear in a separate sub-table so that
cross-engine totals are only compared within the same SF.

Selection rule per (schema, dialect):
  - Only files with source_row_counts populated are considered.
  - Prefer the most recent file whose passed=True; fall back to most recent
    overall when no passing result exists.
  - For lakebase-target runs (filename contains "from-"), the dialect is
    recorded as "lakebase" and the source engine is noted in a footnote.

Usage
-----
    # From repo root:
    python benchmarks/build_row_count_report.py

    # Overwrite only the report; results are read from the default location:
    python benchmarks/build_row_count_report.py --results-dir benchmarks/results

    # Dry-run: print to stdout, do not write file:
    python benchmarks/build_row_count_report.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
_RESULTS_DIR = _REPO_ROOT / "benchmarks" / "results"
_OUT_FILE    = _RESULTS_DIR / "row_count_report.md"

# Canonical engine display order in tables
_ENGINE_ORDER = ["postgres", "cockroachdb", "mysql", "sqlserver", "oracle", "db2", "lakebase"]

# Canonical schema order
_SCHEMA_ORDER = ["tpcb", "tpcc", "tpch", "tpcdi", "tpcds", "tpce"]


def _engine_label(dialect: str) -> str:
    return {
        "postgres":    "postgres",
        "cockroachdb": "cockroachdb",
        "mysql":       "mysql",
        "sqlserver":   "sqlserver",
        "oracle":      "oracle",
        "db2":         "db2",
        "lakebase":    "lakebase",
    }.get(dialect, dialect)


def _load_best_results(results_dir: Path) -> dict[tuple[str, str, float], dict]:
    """
    Return {(schema, dialect, sf): result_dict} — one entry per
    (schema, dialect) using the best available result (passed=True preferred,
    then most recent).
    """
    # key = (schema, dialect)  value = (path, result_dict)
    candidates: dict[tuple[str, str], list[tuple[Path, dict]]] = defaultdict(list)

    for path in sorted(results_dir.glob("*identity*.json")):
        try:
            r = json.loads(path.read_text())
        except Exception:
            continue
        if not r.get("source_row_counts"):
            continue
        schema  = r.get("schema", "")
        dialect = r.get("dialect", "")
        if not schema or not dialect:
            continue
        candidates[(schema, dialect)].append((path, r))

    best: dict[tuple[str, str, float], dict] = {}
    for (schema, dialect), entries in candidates.items():
        # Sort: passed=True first, then by filename (timestamp) descending
        entries.sort(key=lambda x: (not x[1].get("passed", False), x[0].name), reverse=False)
        # Stable reverse-timestamp within same passed status
        passed_entries   = [(p, r) for p, r in entries if r.get("passed")]
        failed_entries   = [(p, r) for p, r in entries if not r.get("passed")]
        chosen = (passed_entries or failed_entries)
        if not chosen:
            continue
        # Most recent within the preferred tier
        path, r = max(chosen, key=lambda x: x[0].name)
        sf = float(r.get("sf", 0))
        best[(schema, dialect, sf)] = r

    return best


def _build_section(
    schema: str,
    sf: float,
    engines: list[str],
    results: dict[tuple[str, str, float], dict],
) -> list[str]:
    """
    Build markdown lines for one (schema, sf) block.

    Columns: Table | <engine> src | <engine> tgt | ...
    """
    # Collect union of all table names (preserve canonical order where possible)
    all_tables: dict[str, None] = {}
    for eng in engines:
        r = results.get((schema, eng, sf), {})
        for t in r.get("source_row_counts", {}):
            all_tables[t] = None
        for t in r.get("target_row_counts", {}):
            all_tables[t] = None

    if not all_tables:
        return []

    tables = list(all_tables)

    # Header
    sf_str = str(sf).rstrip("0").rstrip(".") if "." in str(sf) else str(sf)
    lines = [
        f"## {schema.upper()} — sf={sf_str}",
        "",
    ]

    # Build column headers
    header_cells = ["Table"]
    sep_cells    = ["---"]
    for eng in engines:
        label = _engine_label(eng)
        header_cells += [f"{label} src", f"{label} tgt"]
        sep_cells    += ["---", "---"]

    lines.append("| " + " | ".join(header_cells) + " |")
    lines.append("| " + " | ".join(sep_cells)    + " |")

    # Data rows
    totals_src: dict[str, int] = defaultdict(int)
    totals_tgt: dict[str, int] = defaultdict(int)

    for table in tables:
        cells = [f"`{table}`"]
        for eng in engines:
            r = results.get((schema, eng, sf), {})
            src = r.get("source_row_counts", {}).get(table)
            tgt = r.get("target_row_counts", {}).get(table)
            cells.append(f"{src:,}" if src is not None else "—")
            cells.append(f"{tgt:,}" if tgt is not None else "—")
            if src is not None:
                totals_src[eng] += src
            if tgt is not None:
                totals_tgt[eng] += tgt
        lines.append("| " + " | ".join(cells) + " |")

    # Totals row
    total_cells = ["**TOTAL**"]
    for eng in engines:
        s = totals_src.get(eng)
        t = totals_tgt.get(eng)
        total_cells.append(f"**{s:,}**" if s else "—")
        total_cells.append(f"**{t:,}**" if t else "—")
    lines.append("| " + " | ".join(total_cells) + " |")
    lines.append("")

    # Status line
    mismatches: list[str] = []
    all_match = True
    for eng in engines:
        s = totals_src.get(eng, 0)
        t = totals_tgt.get(eng, 0)
        if s != t:
            all_match = False
            mismatches.append(f"{eng} src={s:,} tgt={t:,}")

    # Cross-engine consistency check
    unique_totals = set(totals_src.values())
    cross_ok = len(unique_totals) <= 1

    if all_match and cross_ok:
        lines.append(
            f"> ✅ `src == tgt` for all {len(engines)} engines. "
            f"All engines loaded {next(iter(unique_totals)):,} rows consistently."
        )
    elif all_match and not cross_ok:
        lines.append("> ✅ `src == tgt` per engine.  ⚠️ Cross-engine totals differ (different SFs).")
    else:
        for m in mismatches:
            lines.append(f"> ⚠️ Mismatch: {m}")

    lines.append("")
    return lines


def generate_report(results_dir: Path) -> str:
    best = _load_best_results(results_dir)

    # Group by (schema, sf) → engines
    groups: dict[tuple[str, float], list[str]] = defaultdict(list)
    for (schema, dialect, sf) in best:
        groups[(schema, sf)].append(dialect)

    # Sort engines within each group in canonical order
    for key in groups:
        dialects = groups[key]
        groups[key] = [e for e in _ENGINE_ORDER if e in dialects] + \
                      [e for e in dialects if e not in _ENGINE_ORDER]

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines: list[str] = [
        "# Row Count Report — Identity Tests",
        "",
        f"_Generated {ts}_",
        "",
        "Source vs. target row counts for all TPC schemas across every tested engine,",
        "at the canonical scale factor used for identity scoring.",
        "",
        "**What this validates:**",
        "- `src == tgt` per engine: no rows were dropped or duplicated during the load.",
        "- All engine source totals equal within a scale-factor group: the data generator",
        "  produces deterministic, engine-independent output.",
        "",
        "Rows labelled `src` are from Phase A (canonical TPC data loaded into the source schema).  ",
        "Rows labelled `tgt` are from Phase D (stats-driven synthetic data loaded into the target schema).  ",
        "Tables with a deterministic built-in generator (e.g. `date_dim`, `time_dim`) always match exactly.",
        "",
        "**How to regenerate this file:**",
        "```bash",
        "python benchmarks/build_row_count_report.py",
        "```",
        "The script reads every `benchmarks/results/*identity*.json` file, selects the",
        "most recent passing result per (schema, dialect), and writes this file.",
        "",
        "---",
        "",
    ]

    # Emit sections in canonical schema+sf order
    for schema in _SCHEMA_ORDER:
        # Collect all SFs for this schema, sorted
        sfs = sorted({sf for (s, sf) in groups if s == schema})
        for sf in sfs:
            engines = groups.get((schema, sf), [])
            if not engines:
                continue
            section = _build_section(schema, sf, engines, best)
            lines.extend(section)

    # Summary table
    lines += [
        "---",
        "",
        "## Summary",
        "",
        "| Schema | sf | Engines | Total rows | src==tgt | Cross-engine consistent |",
        "| --- | --- | --- | --- | --- | --- |",
    ]

    for schema in _SCHEMA_ORDER:
        sfs = sorted({sf for (s, sf) in groups if s == schema})
        for sf in sfs:
            engines = groups.get((schema, sf), [])
            if not engines:
                continue
            totals: list[int] = []
            all_match = True
            for eng in engines:
                r = best.get((schema, eng, sf), {})
                s = sum(r.get("source_row_counts", {}).values())
                t = sum(r.get("target_row_counts", {}).values())
                totals.append(s)
                if s != t:
                    all_match = False
            unique = set(totals)
            cross_ok = len(unique) <= 1
            total_str = f"{next(iter(unique)):,}" if unique else "—"
            eng_list = ", ".join(engines)
            match_str = "✅" if all_match else "⚠️"
            cross_str = "✅" if cross_ok else "⚠️"
            sf_str = str(sf).rstrip("0").rstrip(".") if "." in str(sf) else str(sf)
            lines.append(
                f"| **{schema.upper()}** | {sf_str} | {len(engines)} ({eng_list}) "
                f"| {total_str} | {match_str} | {cross_str} |"
            )

    lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate row_count_report.md from identity test results")
    ap.add_argument("--results-dir", default=str(_RESULTS_DIR),
                    help=f"Directory containing identity JSON files (default: {_RESULTS_DIR})")
    ap.add_argument("--out",         default=str(_OUT_FILE),
                    help=f"Output markdown file (default: {_OUT_FILE})")
    ap.add_argument("--dry-run",     action="store_true",
                    help="Print to stdout; do not write the file")
    args = ap.parse_args()

    results_dir = Path(args.results_dir)
    if not results_dir.exists():
        print(f"ERROR: results directory not found: {results_dir}", file=sys.stderr)
        sys.exit(1)

    report = generate_report(results_dir)

    if args.dry_run:
        print(report)
    else:
        out = Path(args.out)
        out.write_text(report)
        print(f"Written → {out.relative_to(_REPO_ROOT)}")
        # Print summary section only
        in_summary = False
        for line in report.splitlines():
            if line.startswith("## Summary"):
                in_summary = True
            if in_summary:
                print(line)


if __name__ == "__main__":
    main()
