#!/usr/bin/env python3
"""
loc_report.py — Lines-of-code snapshot by module group.

Usage
-----
    python scripts/loc_report.py               # print markdown to stdout
    python scripts/loc_report.py --save        # write docs/loc_report.md

Counts per .py file
-------------------
  code     — non-blank, non-comment lines
  comment  — lines whose first non-space token is '#'
  blank    — empty or whitespace-only lines
  total    — code + comment + blank

Docstrings are counted as code (they live inside the AST as expressions).
Only line comments (#) are counted as comments, matching the visual weight
of deliberate annotation versus inline documentation.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SRC  = _REPO / "src" / "statschema"


# ---------------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------------

def _count(path: Path) -> tuple[int, int, int]:
    """Return (code, comment, blank) for a single .py file."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    code = comment = blank = 0
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            blank += 1
        elif stripped.startswith("#"):
            comment += 1
        else:
            code += 1
    return code, comment, blank


def _collect(root: Path, globs: tuple[str, ...] = ("*.py",)) -> list[dict]:
    rows = []
    seen: set[Path] = set()
    for pattern in globs:
        for f in sorted(root.rglob(pattern)):
            if "__pycache__" in f.parts or f in seen:
                continue
            seen.add(f)
            code, comment, blank = _count(f)
            total = code + comment + blank
            if total == 0:
                continue
            rows.append(
                dict(
                    path=f,
                    rel=str(f.relative_to(root)),
                    code=code,
                    comment=comment,
                    blank=blank,
                    total=total,
                )
            )
    return sorted(rows, key=lambda r: r["rel"])


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

_INTERFACE_FILES = {"cli.py", "__main__.py"}
# Python files that implement locale/multi-lingual support
_LOCALE_FILES    = {"semantic_hints.py"}


def _group_key(rel: str) -> str:
    """Map a relative path inside src/statschema to a display group label."""
    parts = Path(rel).parts
    if parts[0] in _INTERFACE_FILES:
        return "interface"
    if parts[0] == "dialects":
        if len(parts) == 1 or parts[1].startswith("_") or parts[1].endswith(".py"):
            return "dialects/shared"
        return f"dialects/{parts[1]}"
    if parts[0] == "spark":
        return "generators/spark"
    if parts[0] == "patterns":
        return "locale"
    if parts[0] == "core":
        if parts[-1] == "pandas_builder.py":
            return "generators/pandas"
        if parts[-1] in _LOCALE_FILES:
            return "locale"
        return "core"
    if parts[0] == "query":
        return "query"
    if parts[0] == "services":
        return "services"
    return "src root"


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _totals(rows: list[dict]) -> tuple[int, int, int, int]:
    return (
        sum(r["code"]    for r in rows),
        sum(r["comment"] for r in rows),
        sum(r["blank"]   for r in rows),
        sum(r["total"]   for r in rows),
    )


def _fmt(cell) -> str:
    """Format a cell value: integers use _ as thousands separator."""
    if isinstance(cell, int):
        return f"{cell:_}"
    return str(cell)


def _md_table(header: list[str], rows: list[list]) -> str:
    # Columns whose header is a known numeric field are right-aligned.
    numeric_headers = {"Files", "Code", "Comment", "Blank", "Total"}
    right = [h in numeric_headers for h in header]

    col_widths = [len(h) for h in header]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(_fmt(cell)))

    fmt_h = " | ".join(
        h.rjust(col_widths[i]) if right[i] else h.ljust(col_widths[i])
        for i, h in enumerate(header)
    )
    # GitHub Markdown right-aligns a column when the separator ends with ':'
    sep = " | ".join(
        ("-" * (w - 1) + ":") if right[i] else "-" * w
        for i, w in enumerate(col_widths)
    )
    lines = [fmt_h, sep]
    for row in rows:
        lines.append(" | ".join(
            _fmt(cell).rjust(col_widths[i]) if right[i] else _fmt(cell).ljust(col_widths[i])
            for i, cell in enumerate(row)
        ))
    return "\n".join(lines)


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_REPO, text=True
        ).strip()
    except subprocess.CalledProcessError:
        return "unknown"


# ---------------------------------------------------------------------------
# Report sections
# ---------------------------------------------------------------------------

def _section_src() -> str:
    rows = _collect(_SRC, ("*.py", "*.yaml", "*.yml"))

    # group summary
    from collections import defaultdict
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[_group_key(r["rel"])].append(r)

    group_order = [
        "interface", "src root", "core", "query", "services",
        "locale",
        "dialects/shared",
        "dialects/cockroachdb", "dialects/databricks", "dialects/db2",
        "dialects/mysql", "dialects/oracle", "dialects/postgres",
        "dialects/sqlserver",
        "generators/pandas", "generators/spark",
    ]
    # add any groups not in the explicit order
    for g in sorted(groups):
        if g not in group_order:
            group_order.append(g)

    header = ["Group", "Files", "Code", "Comment", "Blank", "Total"]
    table_rows = []
    for g in group_order:
        g_rows = groups.get(g)
        if not g_rows:
            continue
        c, cm, bl, tot = _totals(g_rows)
        table_rows.append([g, len(g_rows), c, cm, bl, tot])

    # grand total row
    c, cm, bl, tot = _totals(rows)
    table_rows.append(["**TOTAL**", len(rows), c, cm, bl, tot])

    lines = ["## `src/statschema`", "", _md_table(header, table_rows)]

    # per-file detail for the largest groups (skipping tiny pass-through shims)
    lines += ["", "### Per-file detail (≥ 50 total lines)", ""]
    detail_header = ["File", "Code", "Comment", "Blank", "Total"]
    detail_rows = [
        [r["rel"], r["code"], r["comment"], r["blank"], r["total"]]
        for r in rows
        if r["total"] >= 50
    ]
    lines.append(_md_table(detail_header, detail_rows))
    return "\n".join(lines)


def _section_platform() -> str:
    rows   = _platform_rows()
    header = ["File", "Code", "Comment", "Blank", "Total"]
    table  = [
        [r["rel"], r["code"], r["comment"], r["blank"], r["total"]]
        for r in sorted(rows, key=lambda r: r["rel"])
    ]
    c, cm, bl, tot = _totals(rows)
    table.append(["**TOTAL**", c, cm, bl, tot])
    note = (
        "\nEach new target platform (AWS, GCloud, Azure) adds VM/container config files\n"
        "and startup scripts here — isolated from benchmark and test logic."
    )
    return "\n".join(["## `platform/` (Lima · Podman · Databricks Connect)", note, "",
                      _md_table(header, table)])


def _section_benchmarks() -> str:
    bench_root  = _REPO / "benchmarks"
    script_root = _REPO / "scripts"
    platform_rels = {r["rel"] for r in _platform_rows()}

    all_py = [r for r in _collect(bench_root, ("*.py",))
              if not r["rel"].startswith("results")]
    # Split benchmarks Python into pipeline/dialects adapters vs everything else
    dialect_py = [r for r in all_py if r["rel"].startswith("dialects/")]
    core_py    = [r for r in all_py if not r["rel"].startswith("dialects/")]

    sh_rows = [
        r for r in (
            _collect(bench_root, ("*.sh",))
            + [dict(r, rel=f"scripts/{r['rel']}")
               for r in _collect(script_root, ("*.sh",))]
        )
        if r["rel"] not in platform_rels
    ]

    # scripts/*.py — standalone dev-tooling scripts (not benchmark runners)
    tool_py = [
        dict(r, rel=f"scripts/{r['rel']}")
        for r in _collect(script_root, ("*.py",))
    ]

    header = ["File", "Code", "Comment", "Blank", "Total"]
    lines  = ["## `benchmarks/`", ""]

    for subtitle, group in [
        ("### Python (pipeline / orchestration)", core_py),
        ("### Python (dialect adapters — `benchmarks/dialects/`)", dialect_py),
        ("### Shell scripts (runners)", sh_rows),
        ("### Scripts / tools (`scripts/`)", tool_py),
    ]:
        if not group:
            continue
        table_rows = [
            [r["rel"], r["code"], r["comment"], r["blank"], r["total"]]
            for r in group
        ]
        c, cm, bl, tot = _totals(group)
        table_rows.append(["**total**", c, cm, bl, tot])
        lines += [subtitle, "", _md_table(header, table_rows), ""]

    return "\n".join(lines)


# DB engine names whose test file is classified as a dialect test.
_DIALECT_TEST_ENGINES = {
    "pg", "mysql", "cockroachdb", "db2", "oracle", "sqlserver", "mariadb", "neon",
}
# App-schema test suffixes (test_live_<app>.py).
_APP_TEST_NAMES = {
    "test_live_adventureworks.py", "test_live_chinook.py", "test_live_gitea.py",
    "test_live_mautic.py", "test_live_oracle_hr.py", "test_live_lakebase.py",
}


def _test_group(rel: str) -> str:
    name = Path(rel).name
    if name in _APP_TEST_NAMES:
        return "app"
    if name.startswith("test_live_"):
        engine = name[len("test_live_"):-len(".py")]
        if engine in _DIALECT_TEST_ENGINES:
            return "dialect"
        return "dialect"   # any other test_live_* is dialect-level
    return "core"


def _section_tests() -> str:
    root = _REPO / "tests"
    rows = _collect(root)

    from collections import defaultdict
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[_test_group(r["rel"])].append(r)

    lines = ["## `tests/`", ""]

    header = ["File", "Code", "Comment", "Blank", "Total"]
    for label, key in [
        ("### Core tests (unit / offline integration)", "core"),
        ("### Dialect tests (live DB engine coverage)", "dialect"),
        ("### App tests (real-world application schemas)", "app"),
    ]:
        g = groups.get(key, [])
        if not g:
            continue
        table_rows = [
            [r["rel"], r["code"], r["comment"], r["blank"], r["total"]]
            for r in g
        ]
        c, cm, bl, tot = _totals(g)
        table_rows.append([f"**{key} total**", c, cm, bl, tot])
        lines += [label, "", _md_table(header, table_rows), ""]

    c, cm, bl, tot = _totals(rows)
    lines += ["### All tests total", "",
              _md_table(["Category", "Files", "Code", "Total"], [
                  [k, len(groups[k]),
                   *[_totals(groups[k])[i] for i in (0, 3)]]
                  for k in ("core", "dialect", "app") if k in groups
              ] + [["**TOTAL**", len(rows), c, tot]])]

    return "\n".join(lines)


_CORE_GROUPS      = {"src root", "core", "query", "services"}
_GENERATOR_GROUPS = {"generators/spark", "generators/pandas"}
_INTERFACE_GROUPS = {"interface"}
_LOCALE_GROUPS    = {"locale"}

# Shell/config files that belong to the platform layer (Lima, Podman, Databricks Connect).
# Each new target platform (AWS, GCloud, Azure) will add files here.
_PLATFORM_SHELL = {
    "_common.sh",          # starts Lima VMs and Podman containers
    "run_lakebase_target.sh",  # Databricks / Lakebase target test runner
    "lakebase-up.sh",      # Databricks Connect cluster lifecycle
    "lakebase-down.sh",    # Databricks Connect cluster lifecycle
}


def _platform_rows() -> list[dict]:
    """Collect all platform-layer files: Lima configs + infra shell scripts."""
    config_root = _REPO / "config" / "lima"
    lima_rows = [
        dict(r, rel=f"config/lima/{r['rel']}")
        for r in _collect(config_root, ("*.yaml", "*.yml"))
    ]
    sh_platform = []
    for root, prefix in [
        (_REPO / "benchmarks", ""),
        (_REPO / "scripts",    "scripts/"),
    ]:
        for r in _collect(root, ("*.sh",)):
            if Path(r["rel"]).name in _PLATFORM_SHELL:
                sh_platform.append(dict(r, rel=f"{prefix}{r['rel']}"))
    return lima_rows + sh_platform


def _section_grand_total() -> str:
    src_rows      = _collect(_SRC, ("*.py", "*.yaml", "*.yml"))
    platform_rows = _platform_rows()

    # bench_rows = Python benchmarks + non-platform shell scripts + scripts/*.py tools
    all_bench_sh = _collect(_REPO / "benchmarks", ("*.sh",))
    all_scripts_sh = [
        dict(r, rel=f"scripts/{r['rel']}")
        for r in _collect(_REPO / "scripts", ("*.sh",))
    ]
    platform_rels = {r["rel"] for r in platform_rows}
    scripts_py = [
        dict(r, rel=f"scripts/{r['rel']}")
        for r in _collect(_REPO / "scripts", ("*.py",))
    ]
    bench_rows = (
        [r for r in _collect(_REPO / "benchmarks", ("*.py",))
         if not r["rel"].startswith("results")]
        + [r for r in all_bench_sh + all_scripts_sh
           if r["rel"] not in platform_rels]
        + scripts_py
    )
    test_rows = _collect(_REPO / "tests")

    # Split src into five buckets
    iface_rows   = [r for r in src_rows if _group_key(r["rel"]) in _INTERFACE_GROUPS]
    core_rows    = [r for r in src_rows if _group_key(r["rel"]) in _CORE_GROUPS]
    gen_rows     = [r for r in src_rows if _group_key(r["rel"]) in _GENERATOR_GROUPS]
    locale_rows  = [r for r in src_rows if _group_key(r["rel"]) in _LOCALE_GROUPS]
    _all_src_buckets = _CORE_GROUPS | _GENERATOR_GROUPS | _INTERFACE_GROUPS | _LOCALE_GROUPS
    dialect_rows = [r for r in src_rows if _group_key(r["rel"]) not in _all_src_buckets]

    # Split tests by category
    from collections import defaultdict
    test_groups: dict[str, list[dict]] = defaultdict(list)
    for r in test_rows:
        test_groups[_test_group(r["rel"])].append(r)

    header = ["Layer", "Files", "Code", "Comment", "Blank", "Total", "Note"]
    rows = []
    for label, group, note in [
        ("  src/interface",     iface_rows,              "CLI / API entry points"),
        ("  src/core",          core_rows,               "stable domain logic — grows with features"),
        ("  src/generators",    gen_rows,                "grows with each new generator backend"),
        ("  src/locale",        locale_rows,             "locale patterns + inference (grows per language)"),
        ("  src/dialects",      dialect_rows,            "grows with each new database added"),
        ("platform",            platform_rows,           "Lima/Podman/QEMU configs + Databricks Connect scripts (grows per cloud platform)"),
        ("benchmarks",          bench_rows,              ""),
        ("  tests/core",        test_groups["core"],     "unit + offline integration"),
        ("  tests/dialect",     test_groups["dialect"],  "live DB engine coverage"),
        ("  tests/app",         test_groups["app"],      "real-world application schemas"),
    ]:
        c, cm, bl, tot = _totals(group)
        rows.append([label, len(group), c, cm, bl, tot, note])

    all_rows = src_rows + platform_rows + bench_rows + test_rows
    c, cm, bl, tot = _totals(all_rows)
    rows.append(["**TOTAL**", len(all_rows), c, cm, bl, tot, ""])

    lines = ["## Grand total", "", _md_table(header, rows)]

    # Dialect-per-database breakdown as a sub-table
    from collections import defaultdict
    src_all = _collect(_SRC, ("*.py", "*.yaml", "*.yml"))
    per_db: dict[str, list[dict]] = defaultdict(list)
    for r in src_all:
        key = _group_key(r["rel"])
        if key not in _CORE_GROUPS and key not in _GENERATOR_GROUPS and key not in _LOCALE_GROUPS:
            per_db[key].append(r)

    if per_db:
        db_header = ["Dialect", "Files", "Code", "Total"]
        db_rows = []
        for dialect in sorted(per_db):
            g = per_db[dialect]
            c, _, _, tot = _totals(g)
            db_rows.append([dialect, len(g), c, tot])
        c, _, _, tot = _totals([r for rows in per_db.values() for r in rows])
        db_rows.append(["**dialect total**", sum(len(v) for v in per_db.values()), c, tot])
        lines += ["", "### Dialect breakdown (src/statschema)", "",
                  _md_table(db_header, db_rows)]

    # Generator backend breakdown
    gen_header = ["Generator backend", "Files", "Code", "Total"]
    gen_breakdown = []
    for key in sorted(_GENERATOR_GROUPS):
        g = [r for r in src_all if _group_key(r["rel"]) == key]
        if g:
            c, _, _, tot = _totals(g)
            gen_breakdown.append([key, len(g), c, tot])
    if gen_breakdown:
        c, _, _, tot = _totals([r for r in src_all if _group_key(r["rel"]) in _GENERATOR_GROUPS])
        gen_breakdown.append(["**generator total**", sum(r[1] for r in gen_breakdown), c, tot])
        lines += ["", "### Generator backend breakdown (src/statschema)", "",
                  _md_table(gen_header, gen_breakdown)]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_report() -> str:
    ts     = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    commit = _git_commit()
    header = f"# statschema — lines of code\n\nGenerated: {ts}  \nCommit: `{commit}`\n"
    parts  = [
        header,
        _section_grand_total(),
        _section_src(),
        _section_platform(),
        _section_benchmarks(),
        _section_tests(),
    ]
    return "\n\n---\n\n".join(parts) + "\n"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--save", action="store_true",
                   help="Write output to docs/loc_report.md")
    args = p.parse_args()

    report = build_report()
    if args.save:
        out = _REPO / "docs" / "loc_report.md"
        out.write_text(report, encoding="utf-8")
        print(f"Saved → {out.relative_to(_REPO)}", file=sys.stderr)
    else:
        print(report)


if __name__ == "__main__":
    main()
