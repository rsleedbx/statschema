---
name: python-best-practices
description: Apply Python best practices when writing or refactoring code: single source of truth, no duplicate code, no backward-compatibility complexity on new projects; use pytest for tests and maintain complete test coverage as code is written. Use when editing Python, adding constants, refactoring modules, or adding tests.
---

# Python Best Practices (Clean Code)

When writing or changing Python in this project, follow these rules. Assume the project is **new**—do not add backward-compatibility layers unless explicitly required.

## Single Source of Truth

- Define each concept (valid options, config keys, formats) in **one place**.
- Derive everything else from that definition instead of re-listing values.

**Good:** Enum as the single definition; derive a validation set from it.

```python
class SchemaSource(str, Enum):
    YDATA = "ydata"
    SDV = "sdv"
    MYSQL = "mysql"

# Derive; do not duplicate the list
VALID = frozenset(s.value for s in SchemaSource)
```

**Bad:** Same values defined as constants, aliases, and again in a set.

```python
SOURCE_YDATA = "ydata"
SOURCE_SDV = "sdv"
FORMAT_YDATA = SOURCE_YDATA  # duplicate
VALID = {"ydata", "sdv"}     # duplicate
```

## No Duplicate Code (DRY)

- Do not repeat the same logic or the same list of options in multiple places.
- Extract shared behavior into one function/constant/type; call or reference it.
- Adding a new option should require editing **one** place (e.g. one enum or one list).

## Prefer Enums for Fixed Sets

- For a fixed set of related string values (formats, modes, sources), use an `Enum` (or `str, Enum` when you need string comparison).
- Export and use the enum in the API; avoid parallel string constants or aliases that mirror enum values.
- Accept `Enum | str` in public APIs only if you need to normalize from config/YAML; normalize once and use the enum internally.

## No Backward-Compat Complexity on New Projects

- Do **not** introduce legacy aliases, deprecated paths, or “we support both old and new” patterns unless the user explicitly asks for compatibility.
- Prefer one clear way to do things: one type (e.g. `SchemaSource`), one name per concept, one canonical representation.
- If refactoring, update call sites and tests to the new API instead of keeping old names “for compatibility.”

## Normalize Early, Use Strong Types

- Convert string input (e.g. from YAML or CLI) to the canonical type (e.g. `SchemaSource`) at the boundary.
- Use the strong type everywhere inside the module; avoid passing raw strings through the core logic.
- Raise a clear `ValueError` with valid options when input is invalid, rather than returning None or a sentinel unless that’s the intended contract.

## Pytest and Test Coverage

- **Use pytest** for all tests. Place tests in `tests/`; mirror package layout (e.g. `tests/test_schema_parser.py` for `src/schema_parser`).
- **Write tests as you write code**, not after. For each new module, class, or public function, add or extend tests so coverage stays complete.
- **One test file per module** (or logical group). Use `class TestX:` to group tests by component; use `@pytest.mark.parametrize` for multiple inputs that exercise the same behavior.
- **Test public behavior and boundaries:** success paths, invalid input (e.g. `pytest.raises(ValueError)`), edge cases (empty input, single item, all supported formats).
- **Avoid duplicate test setup:** use fixtures (`@pytest.fixture`) for shared data; use parametrization instead of copy-pasting similar test bodies.
- **Coverage goal:** New code should have corresponding tests. When adding a feature (e.g. a new schema source or parser), add tests that validate that feature end-to-end and that the loader/dispatcher exercises it.

### Running pytest — always use `.venv_test`

**Default venv for running all tests with 0 skips** is `.venv_test` (from `requirements-test.txt`). It has `pyspark` but not `databricks-connect`, so Spark-dependent tests fall back to a local in-process session instead of skipping.

```bash
# One-time setup
python -m venv .venv_test
.venv_test/bin/pip install -r requirements-test.txt

# Run every time after a change — must be 0 skipped
.venv_test/bin/python -m pytest tests/ -q
```

- **Fix skipped tests before considering the change done.** A skip means a dependency is missing from `.venv_test` or a fixture is absent — add it.
- **Do not use the system `python` or a venv that lacks `pyspark`** — tests that build Spark DataFrames will skip silently.
- To run against a real Databricks workspace use `.venv_3_11` (has `databricks-connect`). Both venvs should produce 0 skips.

## Checklist When Refactoring

- [ ] Is there a single definition of valid options / formats? (enum or one list/dict.)
- [ ] Is the validation set or “valid values” list derived from that definition, not duplicated?
- [ ] Are there no redundant aliases (e.g. `FORMAT_X = SOURCE_X`) that could be removed?
- [ ] Are call sites and **tests** updated to use the new type/API; no “backward compat” branches?
- [ ] Are invalid inputs rejected at the boundary with a clear error and list of valid values?
- [ ] Do existing tests still pass? If behavior changed, are tests updated and new cases added so coverage remains complete?
