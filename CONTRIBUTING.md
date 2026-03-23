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
python3.11 -m venv .venv_test
source .venv_test/bin/activate
pip install -r requirements-test.txt
```

## Run tests

```bash
# Offline tests only — no database, no Java required (~60 seconds)
pytest tests/ -k "not live" -q

# Full offline suite including Spark data-generation tests
make test-fast

# Full suite (requires local databases via Podman — see docs/local-databases.md)
make test
```

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

- [ ] Offline tests pass: `pytest tests/ -k "not live" -q`
- [ ] New behaviour has a corresponding test in the appropriate `tests/test_*.py` file
- [ ] New dialect or database has a setup guide in `docs/databases/`
- [ ] No new dependencies added to `requirements-test.txt` without discussion

## Code style

- Python 3.10+ with type hints on all public functions
- No runtime dependencies beyond what is in `requirements-test.txt`
- Match the naming and abstraction style of the surrounding code

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
