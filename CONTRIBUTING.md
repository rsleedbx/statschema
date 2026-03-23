# Contributing to statschema

Thank you for your interest in contributing. statschema is designed so that an AI agent or a first-time contributor can add a new dialect, a new stats field, or a new semantic pattern — and the test suite immediately confirms whether it is correct across all supported databases.

## Quick orientation

```
src/statschema/
├── model.py              CanonicalTableSchema, CanonicalColumn, GenerationRule
├── ddl_parser.py         parse_ddl() — sqlglot-based DDL → canonical model
├── ddl_emitter.py        emit_ddl() — canonical model → dialect-specific DDL
├── row_generator.py      generate_rows() — schema-driven synthetic data
├── schema_io.py          load_canonical() / dump_canonical()
├── db_stats_collector.py collect_table_stats() per dialect
├── semantic_hints.py     infer_format_pattern(), apply_hints()
└── dialect_registry.py   dialect name → emitter/collector mapping
```

Every test file follows a single `pytest` class pattern. Read one, and you understand them all.

## Set up

```bash
git clone https://github.com/rsleedbx/statschema.git
cd statschema
make venv-test          # creates .venv_test (Python 3.11, pyspark, all drivers)
cp .env.example .env    # fill in DB credentials if you need live-DB tests
```

## Run tests

There are three tiers, each requiring progressively more infrastructure:

| Target | Command | Needs Java | Needs live DB | Without `dbldatagen.v1` | With `dbldatagen.v1` |
|--------|---------|-----------|---------------|--------------------------|----------------------|
| Fast offline | `make test-fast` | No | No | 2565 passed, 1 skipped | 2670 passed, 0 skipped |
| Full offline | `make test` | **Yes** | No | 2568 passed, 1 skipped | 2673 passed, 0 skipped |
| Full suite | `make test-live-all` | **Yes** | **Yes** | + live-DB tests | same |

The single skip when `dbldatagen.v1` is absent is the entire `test_v1_bridge.py` module (see below).

### What "deselected" means

`make test-fast` uses `-k "not generate_data and not live"`. Pytest shows a count like
`2565 passed, 1 skipped, 810 deselected`. The 810 **deselected** tests were collected
but never executed — excluded entirely by the `-k` filter. They are not failures or
skips; they simply did not run. Running `make test` (which adds Spark tests) and
`make test-live-all` (which adds live-DB tests) brings those numbers down to zero.

### Spark tests need Java

`make test` runs Spark data-generation tests which require a local JVM. Java is set
automatically by `conftest.py` if `JAVA_HOME` is unset (searches Homebrew OpenJDK 17/21/11).
To install:

```bash
brew install openjdk@17
```

### Running in the Cursor agent sandbox

The Cursor agent sandbox blocks subprocess spawning and network. Run any `pytest`
invocation that includes Spark or live-DB tests with `required_permissions: ["all"]`.
See `.cursor/skills/pytest-sandbox/SKILL.md` for the complete rules.

## Optional: unlock the dbldatagen.v1 bridge tests

`tests/test_v1_bridge.py` (50 tests) exercises a next-generation data-generation API
in the `dbldatagen.v1` submodule. This API is not yet released on PyPI — it lives in
the [databrickslabs/data-generator](https://github.com/databrickslabs/data-generator)
repository. If you have that repository checked out locally:

```bash
# Install the dev version into .venv_test — unlocks the 50 v1 bridge tests
make venv-test-v1 DBLDATAGEN_DEV=~/github/dbldatagen

# Run them
make test-v1
# Expected: 105 passed, 0 skipped
```

Without this step, `test_v1_bridge.py` is skipped via `pytest.importorskip("dbldatagen.v1")`.
That is the only expected skip in the full offline suite.

## What to work on

The [roadmap](docs/ROADMAP.md) has prioritised items. The most common contribution types are:

| Contribution type | Where to start |
|---|---|
| Add a new database dialect | [`docs/adding-a-database.md`](docs/adding-a-database.md) — complete end-to-end guide |
| Add a semantic hint / pattern | [`src/statschema/patterns/`](src/statschema/patterns/) and [`tests/test_semantic_hints.py`](tests/test_semantic_hints.py) |
| Improve DDL type mapping | [`src/statschema/ddl_emitter.py`](src/statschema/ddl_emitter.py) and [`tests/test_ddl_roundtrip.py`](tests/test_ddl_roundtrip.py) |
| Add a stats field | [`src/statschema/model.py`](src/statschema/model.py) (ColumnStats) and [`tests/test_schema_parser.py`](tests/test_schema_parser.py) |
| Add a generation distribution | [`src/statschema/row_generator.py`](src/statschema/row_generator.py) and [`tests/test_canonical_generation.py`](tests/test_canonical_generation.py) |

## Pull request checklist

- [ ] Fast offline tests pass: `make test-fast` → 2565 passed, 1 skipped
- [ ] Full offline tests pass: `make test` → 2568 passed, 1 skipped (requires Java)
- [ ] New behaviour has a corresponding test in the appropriate `tests/test_*.py` file
- [ ] No skips added for infrastructure failures — skips are only for missing optional libraries (`pytest.importorskip`) or unreachable databases (port closed)
- [ ] New dialect or database has a setup guide in `docs/databases/`

## Code style

- Python 3.10+ with type hints on all public functions
- No runtime dependencies beyond what is in `requirements-test.txt`
- Match the naming and abstraction style of the surrounding code

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
