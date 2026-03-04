"""
Convert canonical schema to Databricks Labs Data Generator (dbldatagen) column specs.

Produces a list of column specs that can be applied to dg.DataGenerator(spark, ...).withColumn(...).
"""

from typing import Any, Callable

from .model import CanonicalColumn, CanonicalTableSchema, GenerationRule

# Canonical type -> (Spark DataType instance, default withColumn kwargs for dbldatagen)
# dbldatagen expects colType to be an instance of DataType, not the class.
try:
    from pyspark.sql.types import (
        BooleanType,
        DoubleType,
        FloatType,
        IntegerType,
        LongType,
        StringType,
        TimestampType,
    )
    _SPARK_TYPES = {
        "integer": (IntegerType(), {"minValue": 0, "maxValue": 2**31 - 1, "random": True}),
        "long": (LongType(), {"minValue": 0, "maxValue": 2**63 - 1, "random": True}),
        "string": (StringType(), {"prefix": "v", "random": True}),
        "float": (FloatType(), {"minValue": 0.0, "maxValue": 1.0, "random": True}),
        "double": (DoubleType(), {"minValue": 0.0, "maxValue": 1.0, "random": True}),
        "boolean": (BooleanType(), {"random": True}),
        "timestamp": (TimestampType(), {"begin": "2020-01-01 00:00:00", "end": "2024-12-31 00:00:00", "random": True}),
    }
except ImportError:
    _SPARK_TYPES = {}


def _spark_type_and_options(
    col: CanonicalColumn,
    rows: int | None = None,
    random_type_choice: Callable[[], str] | None = None,
) -> tuple[Any, dict[str, Any]]:
    """
    Return (SparkType, kwargs) for dbldatagen .withColumn(name, colType, **kwargs).
    If col.type is not in _SPARK_TYPES and random_type_choice is given, use it to pick a type.
    """
    canonical_type = col.type.lower().strip()
    if canonical_type not in _SPARK_TYPES and random_type_choice:
        canonical_type = random_type_choice()
    if canonical_type not in _SPARK_TYPES:
        canonical_type = "string"
    spark_type, default_kwargs = _SPARK_TYPES[canonical_type]
    opts = dict(default_kwargs)

    if col.generation:
        g = col.generation
        if g.min_value is not None:
            opts["minValue"] = g.min_value
        if g.max_value is not None:
            opts["maxValue"] = g.max_value
        if g.values is not None:
            opts["values"] = g.values
        if g.weights is not None:
            opts["weights"] = g.weights
        # max_length: stored in canonical model for future use (dbldatagen template/expr)
        if g.unique and rows:
            opts["uniqueValues"] = rows

    return spark_type, opts


def to_dbldatagen_specs(
    table: CanonicalTableSchema,
    rows: int | None = None,
    random_type_choice: Callable[[], str] | None = None,
) -> list[tuple[str, Any, dict[str, Any]]]:
    """
    Convert a canonical table schema to a list of (column_name, SparkType, withColumn_kwargs)
    for use with dbldatagen DataGenerator.

    Example usage:
        specs = to_dbldatagen_specs(canonical_table, rows=1000)
        gen = dg.DataGenerator(spark, name="t", rows=1000, ...)
        for name, col_type, kwargs in specs:
            gen = gen.withColumn(name, col_type, **kwargs)
        df = gen.build()
    """
    result: list[tuple[str, Any, dict[str, Any]]] = []
    for col in table.columns:
        spark_type, opts = _spark_type_and_options(col, rows=rows, random_type_choice=random_type_choice)
        result.append((col.name, spark_type, opts))
    return result


def build_dataframe_from_canonical(
    spark: Any,
    table: CanonicalTableSchema,
    rows: int,
    partitions: int | None = 8,
    seed: int | None = None,
    random_type_choice: Callable[[], str] | None = None,
):
    """
    Build a Spark DataFrame from a canonical table schema using dbldatagen.
    Requires dbldatagen and pyspark to be available.
    """
    import dbldatagen as dg

    specs = to_dbldatagen_specs(table, rows=rows, random_type_choice=random_type_choice)
    gen = (
        dg.DataGenerator(
            spark,
            name=table.name,
            rows=rows,
            partitions=partitions or 8,
            randomSeed=seed if seed is not None else -1,
        )
        .withIdOutput()
    )
    for name, col_type, kwargs in specs:
        gen = gen.withColumn(name, col_type, **kwargs)
    return gen.build()
