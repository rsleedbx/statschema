# statschema — implementation notes

**Purpose**: Project structure, schema sources, and data-generation wiring for this repo.  
For historical pipeline goals and scaling notes, see [PLAN.md](PLAN.md).

---

## Project structure (current)

```
<repo>/
├── config/                 # Lima, Podman, etc. (local DB testing)
├── docs/
├── notebooks/              # e.g. generate_data (Databricks Connect / PySpark)
├── src/
│   └── statschema/         # DDL parser/emitter, stats, canonical model, dbldatagen bridge
├── tests/
├── requirements.txt
├── requirements-test.txt
├── databricks.yml
└── README.md
```

---

## Canonical schema and schema sources

The **`src/statschema`** package supports multiple schema formats:

- **SDV** — metadata JSON/YAML with `METADATA_SPEC_VERSION`, `tables`, `relationships`.
- **YData / Syda** — per-field YAML with `type`, `constraints`, `__foreign_keys__`, etc.
- **Pipeline** — `tables[]` with `name` and `columns[]`.
- **DDL** — MySQL, PostgreSQL, SQL Server, Oracle, Databricks `CREATE TABLE` via `parse_ddl()` (sqlglot-backed).

All map to `CanonicalTableSchema` / `CanonicalColumn` and can feed `emit_ddl()`, stats collection, and `build_dataframe_from_canonical()` (dbldatagen).

### Tagging the schema source

```yaml
schema_source: sdv    # or: ydata | pipeline | mysql | postgres | sqlserver | oracle
```

### Database schema-only dumps

| Source | Tool | Option |
|--------|------|--------|
| MySQL | mysqldump | `--no-data` / `-d` |
| PostgreSQL | pg_dump | `-s` / `--schema-only` |
| SQL Server | SSMS / mssql-scripter | DDL script |

Load with `load_schema(path)` or `parse_ddl(sql, dialect=...)`.

---

## Type mappings (YAML → Spark / dbldatagen)

| Canonical | Typical Spark type |
|-----------|---------------------|
| `integer` | `IntegerType` |
| `long` | `LongType` |
| `string` | `StringType` |
| `float` | `FloatType` |
| `double` | `DoubleType` |
| `boolean` | `BooleanType` |
| `timestamp` | `TimestampType` |

See `src/statschema/dbldatagen_builder.py` for the full mapping and edge cases.

For the v0 vs v1 API comparison, assumptions, and open questions for dbldatagen developers,
see [dbldatagen.md](dbldatagen.md).

---

## Synthetic data and Databricks Connect

- **Local PySpark**: see `docs/testing.md` and `requirements-test.txt` (`.venv_test`).
- **Databricks Connect**: `requirements.txt` + `databricks-connect`; use for remote `SparkSession` in notebooks.

---

## References

- [dbldatagen](https://github.com/databrickslabs/dbldatagen)
- [sqlglot](https://github.com/tobymao/sqlglot)
- [Databricks Connect](https://docs.databricks.com/en/dev-tools/databricks-connect.html)
