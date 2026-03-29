# statschema — codebase comparison

Comparison of statschema's source size and architecture against the most
relevant open-source data generation projects on GitHub.  All counts use the
same method: code = non-blank non-comment lines; shebang and `#`-comment lines
excluded from code count.  Measured March 2026.

---

## Source code size (production code only, no tests)

| Project | Files | Code lines | Notes |
|---------|------:|-----------:|-------|
| **Faker** | 710 | 335_531 | Dominated by 682 locale provider files (268K lines of data tables) |
| **mimesis** | 57 | 50_970 | Locale data files inflate count significantly |
| **SDV** | 82 | 18_074 | Statistical modeling focus; no DB connectivity or multi-dialect support |
| **dbldatagen** | ~65 | ~15_000 | Spark-only; no stats collection, no CLI, no dialect layer |
| **statschema** | 98 | 12_725 | Multi-dialect + stats collection + CLI — see breakdown below |
| **sqlsynthgen** | 18 | 2_629 | PostgreSQL-only; no stats collection |

Faker and mimesis are not meaningful comparisons — their code is almost
entirely locale data tables.  The real peer group is SDV, dbldatagen, and
sqlsynthgen.

---

## statschema src breakdown vs. peers

| Layer | statschema | dbldatagen | SDV | sqlsynthgen |
|-------|----------:|----------:|----:|------------:|
| Interface (CLI/API) | 979 | 0 | 0 | 0 |
| Core domain logic | 6_965 | ~10_000 | ~12_000 | ~1_800 |
| Generator backends | 1_143 | ~5_000 | ~6_000 | ~800 |
| Dialect adapters | 3_638 | 0 | 0 | 0 |
| **Total src** | **12_725** | **~15_000** | **18_074** | **2_629** |

Key structural differences:

- **statschema** is the only project with a dedicated dialect layer that scales
  horizontally — adding a new database adds ~250–500 lines of dialect code
  without touching core logic.  No other project in this group supports more
  than one database engine.
- **dbldatagen** is Spark-only.  Its generator code (~5K lines) is larger than
  statschema's because it is not separated from the core; generation and schema
  modeling are interleaved throughout.
- **SDV** is pure statistical modeling (copulas, GANs, CTGANs).  It has no
  database connectivity, no CLI, and no identity testing.  Its size comes from
  the modeling layer.
- **sqlsynthgen** is the closest problem-statement match (SQL database →
  synthetic data) but is PostgreSQL-only, has no stats collection, and at
  2,629 lines is early-stage.

---

## Test coverage

| Project | Test files | Test code lines | Test : src ratio |
|---------|----------:|---------------:|-----------------|
| SDV | 129 | 47_580 | 2.6× |
| **statschema** | 40 | 19_838 | 1.6× |
| dbldatagen | ~40 | ~7_000 | ~0.5× |
| Faker | 63 | 14_696 | 0.04× |
| sqlsynthgen | 20 | 2_519 | 1.0× |
| mimesis | 42 | 4_737 | 0.09× |

statschema's 1.6× test-to-source ratio is strong for a multi-dialect system.
SDV's 2.6× is higher but covers a single-backend system.  dbldatagen's 0.5×
reflects light test coverage relative to its production code size.

---

## Architecture comparison

| Capability | statschema | dbldatagen | SDV | sqlsynthgen | Faker |
|-----------|:---------:|:---------:|:---:|:-----------:|:-----:|
| Multiple DB dialects | ✓ (6) | — | — | — (PG only) | — |
| Stats collection from live DB | ✓ | — | — | ✓ (PG) | — |
| Stats-driven generation | ✓ | ✓ | ✓ | ✓ | — |
| Multi-generator backends | ✓ (pandas, spark) | ✓ (spark) | ✓ | — | — |
| DBA CLI (no Python) | ✓ | — | — | — | — |
| Identity / fidelity testing | ✓ | — | partial | — | — |
| FK-aware generation | ✓ | ✓ | ✓ | — | — |

---

## What the numbers say about statschema's current structure

At 12,725 lines of production code, statschema is **leaner than SDV and
dbldatagen** despite covering a wider problem space (6 dialects, stats
collection, identity testing, CLI, two generator backends).  This reflects the
dialect architecture paying off: the per-dialect code (~600 lines per engine on
average) is cleanly isolated and does not inflate the core.

The generator layer (1,143 lines for pandas + spark, ~570 each) is the
clearest indicator of future growth: each new generation backend should cost
roughly the same increment.

The biggest refactoring opportunity is `identity_test.py` at 2,627 lines
(benchmarks) — larger than any single module in the src tree — which would
benefit from being split along its six test phases.

---

## Regenerating this comparison

The statschema numbers come from `scripts/loc_report.py`.  The peer numbers
were measured by cloning each repo at `--depth 1` in March 2026 and running
the same line-count logic.  Re-measure peers by running:

```bash
# From /tmp or any scratch dir
git clone --depth 1 https://github.com/sdv-dev/SDV
git clone --depth 1 https://github.com/databrickslabs/dbldatagen
git clone --depth 1 https://github.com/alan-turing-institute/sqlsynthgen
# Then count with the same logic as scripts/loc_report.py
```
