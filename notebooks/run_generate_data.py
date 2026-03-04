#!/usr/bin/env python3
"""
Run the generate_data notebook logic from the command line.
Usage: python notebooks/run_generate_data.py   (from repo root)
   or: python run_generate_data.py              (from notebooks/)
"""
import sys
from pathlib import Path

# Project root: walk up until we find a directory containing both "src" and "tests"
cwd = Path.cwd()
root = cwd
while root != root.parent:
    if (root / "src").is_dir() and (root / "tests").is_dir():
        break
    root = root.parent
if str(root) not in sys.path:
    sys.path.insert(0, str(root))


def _get_spark():
    """Return a Spark session or None. Tries DatabricksSession first, then local PySpark."""
    try:
        from databricks.connect import DatabricksSession
        spark = DatabricksSession.builder.getOrCreate()
        print("Spark: DatabricksSession (remote)")
        return spark
    except Exception:
        pass
    try:
        from pyspark.sql import SparkSession
        spark = SparkSession.builder.master("local[1]").appName("generate_data_cli").getOrCreate()
        print("Spark: local PySpark")
        return spark
    except Exception:
        pass
    print("Spark: not available — schema-only mode")
    return None


def main():
    print("Project root:", root)
    spark = _get_spark()

    from src.schema_parser import load_schema, SchemaSource
    from src.schema_parser.dbldatagen_builder import build_dataframe_from_canonical, to_dbldatagen_specs

    schema_path = root / "tests" / "fixtures" / "ronaldbradford_schema" / "sakila_excerpt.sql"
    format_hint = SchemaSource.MYSQL

    tables = load_schema(schema_path, format_hint=format_hint)
    print(f"Loaded {len(tables)} table(s):", [t.name for t in tables])

    if spark is not None:
        ROWS, SEED, PARTITIONS = 1000, 42, 8
        dataframes = {}
        for table in tables:
            df = build_dataframe_from_canonical(spark, table, rows=ROWS, partitions=PARTITIONS, seed=SEED)
            dataframes[table.name] = df
            print(f"{table.name}: {df.count()} rows, {len(df.columns)} columns")
        first_table = tables[0].name
        df = dataframes[first_table]
        df.printSchema()
        print("Sample:")
        df.show(5, truncate=20)
    else:
        for table in tables:
            specs = to_dbldatagen_specs(table, rows=100)
            print(f"{table.name}: {len(specs)} column specs (no Spark, skip build)")

if __name__ == "__main__":
    main()
