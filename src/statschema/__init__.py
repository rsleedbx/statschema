"""
Schema parser — canonical intermediate representation for schema-driven data generation,
multi-dialect DDL emission, and statistics-driven synthetic data testing.

Goals
-----
1. Ingest schema from any supported source format into a single canonical model
   (``CanonicalTableSchema`` + ``CanonicalForeignKey``):

   - Pipeline YAML      (``tables[].name / columns[].name / columns[].type``)
   - YData / Syda YAML  (field-level ``type``, ``constraints``, ``foreign_keys``)
   - SDV metadata JSON/YAML (``METADATA_SPEC_VERSION``, ``sdtype``, ``relationships``)
   - MySQL DDL          (``mysqldump --no-data``)
   - PostgreSQL DDL     (``pg_dump -s``)
   - SQL Server DDL     (``mssql-scripter`` / SSMS Generate Scripts)
   - Oracle DDL         (stub; planned)
   - **Canonical YAML** (``load_canonical()`` — the interchange format)

2. Capture database statistics in a canonical ``DatabaseStats`` YAML that:

   - Covers column, index, composite (multi-column), and FK statistics
   - Maps to native APIs: pg_stats, INFORMATION_SCHEMA.COLUMN_STATISTICS,
     DBCC SHOW_STATISTICS, ALL_TAB_COL_STATISTICS, Databricks DESCRIBE EXTENDED
   - Round-trips exactly through YAML serialisation
   - Provides ``make_default_stats()`` for brief heuristic stats when none are available

3. Apply schema + statistics overrides via ``OverrideSpec`` for:

   - Table / column / schema / catalog renames
   - Type substitutions (e.g. MySQL TINYINT(1) → Oracle NUMBER(1))
   - Row count scaling for subset / integration testing
   - Reserved-word prefixing for target dialect compatibility
   - Common migration scenarios: MySQL↔PG, Oracle→Databricks, MSSQL→Databricks

4. Generate synthetic data from the canonical model via
   Databricks Labs Data Generator (dbldatagen):

   canonical + stats → ``to_dbldatagen_specs()`` / ``build_dataframe_from_canonical()``
                     → Spark DataFrame of synthetic rows

5. Emit ``CREATE TABLE`` DDL from the canonical model for any target dialect,
   enabling full round-trip schema + data testing without manual SQL authoring:

   canonical → ``emit_ddl(dialect)`` → executable DDL for the target system

   Supported target dialects: mysql, postgres, sqlserver, oracle, databricks.

Full pipeline (DDL → canonical YAML → many targets + data)
-----------------------------------------------------------
::

    Source DDL (MySQL / PG / SQL Server / Oracle / Databricks)
          │                    │
          ▼  parse_ddl()       │  OR  load_canonical("schema.yaml")
    CanonicalTableSchema       ◄──────────────────────────────────
          │
          ├──→ dump_schema("schema.yaml")          ← portable interchange format
          │
          ├──→ emit_ddl("databricks")  →  CREATE TABLE on Databricks / Unity Catalog
          ├──→ emit_ddl("postgres")    →  CREATE TABLE on PostgreSQL (test DB)
          ├──→ emit_ddl("mysql")       →  CREATE TABLE on MySQL (test DB)
          ├──→ emit_ddl("sqlserver")   →  CREATE TABLE on SQL Server
          ├──→ emit_ddl("oracle")      →  CREATE TABLE on Oracle
          │
          ├──→ make_default_stats()           (when no real stats available)
          │     OR  load_stats("stats.yaml")  (from real database measurements)
          │          ↓  apply_overrides(spec)
          │     DatabaseStats  (scaled / renamed for target)
          │
          └──→ build_dataframe_from_canonical(spark, ..., stats=table_stats)
                    ↓  dbldatagen (Databricks Labs Data Generator)
               Spark DataFrame (synthetic rows matching cardinality + distributions)
                    ↓  ZeroBus ingest / Delta write
               Populated test table on Unity Catalog
"""

from .model import (
    CanonicalColumn,
    CanonicalForeignKey,
    CanonicalTableSchema,
    GenerationRule,
    expand_table_instances,
)
from .schema_io import dump_schema, load_canonical
from .ydata_parser import parse_ydata_yaml, parse_ydata_yaml_file, parse_ydata_multi_yaml
from .pipeline_parser import parse_pipeline_tables, parse_pipeline_config
from .sdv_parser import parse_sdv_metadata, parse_sdv_file
from .ddl_parser import parse_ddl, parse_ddl_file
from .ddl_emitter import emit_ddl, emit_ddl_all, SUPPORTED_DIALECTS
from .dbldatagen_builder import to_dbldatagen_specs, build_dataframe_from_canonical
from .loader import (
    load_schema,
    detect_format,
    get_schema_source_from_data,
    SchemaSource,
)
from .stats_model import (
    ColumnStats,
    CompositeColumnStats,
    DatabaseStats,
    ForeignKeyStats,
    IndexStats,
    MostCommonValue,
    MCVCombination,
    TableStats,
)
from .override_model import ColumnOverride, OverrideSpec, TableOverride
from .db_stats_collector import collect_table_stats
from .stats_injector import (
    inject_stats_databricks,
    inject_stats_mysql,
    inject_stats_oracle,
    inject_stats_postgres,
    inject_stats_sqlserver,
    InjectionResult,
)
from .stats_io import (
    apply_overrides,
    apply_overrides_all,
    dump_stats,
    load_stats,
    make_default_stats,
)

__all__ = [
    # Canonical model
    "CanonicalColumn",
    "CanonicalForeignKey",
    "CanonicalTableSchema",
    "GenerationRule",
    "expand_table_instances",
    # Canonical schema YAML I/O  ← the interchange / persistence layer
    "dump_schema",
    "load_canonical",
    # Parsers (source → canonical)
    "parse_ydata_yaml",
    "parse_ydata_yaml_file",
    "parse_ydata_multi_yaml",
    "parse_pipeline_tables",
    "parse_pipeline_config",
    "parse_sdv_metadata",
    "parse_sdv_file",
    "parse_ddl",
    "parse_ddl_file",
    # Loader (auto-detect + dispatch)
    "load_schema",
    "detect_format",
    "get_schema_source_from_data",
    "SchemaSource",
    # Emitter (canonical → target DDL)
    "emit_ddl",
    "emit_ddl_all",
    "SUPPORTED_DIALECTS",
    # Data generation (canonical → dbldatagen)
    "to_dbldatagen_specs",
    "build_dataframe_from_canonical",
    # Statistics model
    "ColumnStats",
    "CompositeColumnStats",
    "DatabaseStats",
    "ForeignKeyStats",
    "IndexStats",
    "MostCommonValue",
    "MCVCombination",
    "TableStats",
    # Override model
    "ColumnOverride",
    "OverrideSpec",
    "TableOverride",
    # Stats injector (stats transpiler → target DB optimizer)
    "inject_stats_databricks",
    "inject_stats_mysql",
    "inject_stats_postgres",
    "inject_stats_oracle",
    "inject_stats_sqlserver",
    "InjectionResult",
    # Statistics I/O + override application
    "collect_table_stats",
    "load_stats",
    "dump_stats",
    "make_default_stats",
    "apply_overrides",
    "apply_overrides_all",
]
