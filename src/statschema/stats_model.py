"""
Canonical statistics model — database-agnostic representation of table and column
statistics collected from any supported source system.

Statistics source mapping
-------------------------
Each field maps to the native statistics API of each database:

┌──────────────────────┬──────────────────────┬────────────────────────┬──────────────────────┬──────────────────────────┐
│ Canonical field      │ MySQL 8+             │ PostgreSQL 13+         │ SQL Server 2016+     │ Oracle 19c               │
├──────────────────────┼──────────────────────┼────────────────────────┼──────────────────────┼──────────────────────────┤
│ row_count            │ INNODB_TABLESTATS    │ pg_class.reltuples     │ dm_db_partition_stats│ ALL_TABLES.NUM_ROWS      │
│                      │ .NUM_ROWS            │ / pg_stat_user_tables  │ .row_count           │                          │
│ data_size_bytes      │ TABLES.DATA_LENGTH   │ pg_total_relation_size │ dm_db_partition_stats│ ALL_TABLES.BLOCKS * blk  │
│ avg_row_bytes        │ TABLES.AVG_ROW_LENGTH│ pg_class: computed     │ dm_db_partition_stats│ ALL_TABLES.AVG_ROW_LEN   │
├──────────────────────┼──────────────────────┼────────────────────────┼──────────────────────┼──────────────────────────┤
│ null_fraction        │ COLUMN_STATISTICS    │ pg_stats.null_frac     │ density_vector from  │ ALL_TAB_COL_STATISTICS   │
│                      │ histogram.null-values│                        │ DBCC SHOW_STATISTICS │ .NUM_NULLS / NUM_ROWS    │
│ n_distinct           │ COLUMN_STATISTICS    │ pg_stats.n_distinct    │ RANGE_ROWS +         │ ALL_TAB_COL_STATISTICS   │
│                      │ histogram (derived)  │ (neg = fraction)       │ EQ_ROWS (derived)    │ .NUM_DISTINCT            │
│ avg_width_bytes      │ AVG_COL_LEN (approx) │ pg_stats.avg_width     │ sys.columns          │ ALL_TAB_COL_STATISTICS   │
│                      │                      │                        │ .max_length (approx) │ .AVG_COL_LEN             │
│ min_value            │ COLUMN_STATISTICS    │ pg_stats               │ DBCC SHOW_STATISTICS │ ALL_TAB_COL_STATISTICS   │
│                      │ histogram.buckets[0] │ .histogram_bounds[0]   │ histogram RANGE_LOW  │ .LOW_VALUE (RAW decoded)  │
│ max_value            │ COLUMN_STATISTICS    │ pg_stats               │ DBCC SHOW_STATISTICS │ ALL_TAB_COL_STATISTICS   │
│                      │ histogram.buckets[-1]│ .histogram_bounds[-1]  │ histogram RANGE_HIGH │ .HIGH_VALUE (RAW decoded) │
│ most_common_values   │ COLUMN_STATISTICS    │ pg_stats               │ DBCC SHOW_STATISTICS │ ALL_HISTOGRAMS (FREQ/    │
│                      │ histogram (derived)  │ .most_common_vals /    │ WITH DENSITY_VECTOR  │ TOP-FREQ types)          │
│                      │                      │ .most_common_freqs     │                      │                          │
│ histogram_bounds     │ COLUMN_STATISTICS    │ pg_stats               │ dm_db_stats_histogram│ ALL_HISTOGRAMS           │
│                      │ histogram.buckets    │ .histogram_bounds      │ .range_high_key      │ .ENDPOINT_VALUE          │
│ correlation          │ N/A (not collected)  │ pg_stats.correlation   │ N/A                  │ N/A                      │
├──────────────────────┼──────────────────────┼────────────────────────┼──────────────────────┼──────────────────────────┤
│ CompositeColumnStats │ N/A                  │ pg_stats_ext           │ density_vector       │ N/A                      │
│ .n_distinct          │                      │ (CREATE STATISTICS)    │ (cross-col density)  │                          │
│ .dependencies        │ N/A                  │ pg_stats_ext kinds=f   │ N/A                  │ N/A                      │
└──────────────────────┴──────────────────────┴────────────────────────┴──────────────────────┴──────────────────────────┘

Databricks: DESCRIBE TABLE EXTENDED / ANALYZE TABLE … COMPUTE STATISTICS FOR COLUMNS
→ maps to the same canonical fields via information_schema.TABLES and column_stats.

Round-trip guarantee
--------------------
All models implement ``to_dict()`` / ``from_dict()`` so YAML round-trips are exact:
    stats → yaml.dump(stats.to_dict()) → yaml.safe_load() → DatabaseStats.from_dict()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Column-level statistics
# ---------------------------------------------------------------------------

@dataclass
class MostCommonValue:
    """A single entry in the most-common-values list."""

    value: str            # value as a string (caller converts to/from native type)
    frequency: float      # fraction of non-null rows [0.0, 1.0]

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "frequency": self.frequency}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MostCommonValue:
        return cls(value=str(d["value"]), frequency=float(d["frequency"]))


@dataclass
class ColumnStats:
    """
    Column-level statistics — equivalent to pg_stats / DBCC SHOW_STATISTICS /
    ALL_TAB_COL_STATISTICS / INFORMATION_SCHEMA.COLUMN_STATISTICS.

    ``n_distinct`` follows the PostgreSQL convention:
      > 0  →  estimated count of distinct values
      < 0  →  -(fraction of rows that are distinct), e.g. -1.0 = all rows unique
      0    →  unknown
    When coming from MySQL/Oracle/SQL Server (which always give a positive count),
    store the positive count directly.

    ``skewness`` / ``kurtosis`` — distribution shape moments (Pearson):
      skewness = 0  →  symmetric; > 0  →  right-tailed; < 0  →  left-tailed
      kurtosis = 0  →  normal (excess kurtosis); > 0  →  heavy tails; < 0  →  light tails
    These are not natively available in any database catalogue; they must be derived
    by sampling or provided in the override YAML.  When None the generator falls back
    to the histogram distribution.
    """

    name: str                       # column name

    null_fraction: float = 0.0      # fraction of NULLs [0.0, 1.0]
    n_distinct: float = 0.0         # distinct count (>0) or -fraction (<0); 0 = unknown
    avg_width_bytes: Optional[int] = None    # average byte width
    min_value: Optional[str] = None          # minimum value as string
    max_value: Optional[str] = None          # maximum value as string
    most_common_values: list[MostCommonValue] = field(default_factory=list)
    histogram_bounds: list[str] = field(default_factory=list)
    correlation: Optional[float] = None     # physical sort correlation [-1, 1]; None = N/A

    # Distribution shape — not in standard DB catalogues; set via sampling or override
    skewness: Optional[float] = None   # Pearson skewness; None = use histogram
    kurtosis: Optional[float] = None   # excess kurtosis; None = assume normal tails

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "null_fraction": self.null_fraction}
        if self.n_distinct != 0.0:
            d["n_distinct"] = self.n_distinct
        if self.avg_width_bytes is not None:
            d["avg_width_bytes"] = self.avg_width_bytes
        if self.min_value is not None:
            d["min_value"] = self.min_value
        if self.max_value is not None:
            d["max_value"] = self.max_value
        if self.most_common_values:
            d["most_common_values"] = [v.to_dict() for v in self.most_common_values]
        if self.histogram_bounds:
            d["histogram_bounds"] = self.histogram_bounds
        if self.correlation is not None:
            d["correlation"] = self.correlation
        if self.skewness is not None:
            d["skewness"] = self.skewness
        if self.kurtosis is not None:
            d["kurtosis"] = self.kurtosis
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ColumnStats:
        mcv = [MostCommonValue.from_dict(v) for v in d.get("most_common_values", [])]
        return cls(
            name=d["name"],
            null_fraction=float(d.get("null_fraction", 0.0)),
            n_distinct=float(d.get("n_distinct", 0.0)),
            avg_width_bytes=d.get("avg_width_bytes"),
            min_value=d.get("min_value") and str(d["min_value"]),
            max_value=d.get("max_value") and str(d["max_value"]),
            most_common_values=mcv,
            histogram_bounds=[str(b) for b in d.get("histogram_bounds", [])],
            correlation=d.get("correlation"),
            skewness=float(d["skewness"]) if d.get("skewness") is not None else None,
            kurtosis=float(d["kurtosis"]) if d.get("kurtosis") is not None else None,
        )


# ---------------------------------------------------------------------------
# Index statistics
# ---------------------------------------------------------------------------

@dataclass
class IndexStats:
    """
    Index-level statistics.

    Sources:
      MySQL   SHOW INDEX FROM tbl  (Cardinality column)
      PG      pg_stat_user_indexes
      MSSQL   sys.stats / dm_db_stats_histogram
      Oracle  ALL_INDEXES (DISTINCT_KEYS, BLEVEL, LEAF_BLOCKS)
      DB      DESCRIBE TABLE EXTENDED
    """

    name: str
    columns: list[str]
    unique: bool = False
    cardinality: Optional[int] = None   # distinct key count
    index_type: str = "BTREE"           # BTREE | HASH | BITMAP | CLUSTERED | etc.

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "columns": self.columns,
            "unique": self.unique,
            "index_type": self.index_type,
        }
        if self.cardinality is not None:
            d["cardinality"] = self.cardinality
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> IndexStats:
        return cls(
            name=d["name"],
            columns=list(d.get("columns", [])),
            unique=bool(d.get("unique", False)),
            cardinality=d.get("cardinality"),
            index_type=str(d.get("index_type", "BTREE")),
        )


# ---------------------------------------------------------------------------
# Composite (multi-column) statistics
# ---------------------------------------------------------------------------

@dataclass
class MCVCombination:
    """Most-common combination of values across multiple columns."""

    values: list[str]       # one entry per column, in columns order
    frequency: float        # fraction of rows

    def to_dict(self) -> dict[str, Any]:
        return {"values": self.values, "frequency": self.frequency}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MCVCombination:
        return cls(values=[str(v) for v in d["values"]], frequency=float(d["frequency"]))


@dataclass
class CompositeColumnStats:
    """
    Multi-column / extended statistics.

    Sources:
      PostgreSQL  CREATE STATISTICS … (kinds: d=ndistinct, f=dependencies, m=mcv)
                  → pg_stats_ext / pg_stats_ext_data
      SQL Server  Density vector from DBCC SHOW_STATISTICS (cross-column density)
      MySQL/Oracle  Not natively available; can be approximated by sampling
    """

    columns: list[str]          # column names this stat covers (2+)
    n_distinct: Optional[int] = None    # combined distinct count

    # Functional dependency coefficients: "col_a->col_b": 0.0-1.0
    # 1.0 = col_b is fully determined by col_a; 0.0 = independent
    dependencies: dict[str, float] = field(default_factory=dict)

    # Most common value combinations (PostgreSQL MCV lists)
    most_common_combinations: list[MCVCombination] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"columns": self.columns}
        if self.n_distinct is not None:
            d["n_distinct"] = self.n_distinct
        if self.dependencies:
            d["dependencies"] = self.dependencies
        if self.most_common_combinations:
            d["most_common_combinations"] = [c.to_dict() for c in self.most_common_combinations]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CompositeColumnStats:
        return cls(
            columns=list(d["columns"]),
            n_distinct=d.get("n_distinct"),
            dependencies={str(k): float(v) for k, v in d.get("dependencies", {}).items()},
            most_common_combinations=[
                MCVCombination.from_dict(c) for c in d.get("most_common_combinations", [])
            ],
        )


# ---------------------------------------------------------------------------
# Foreign key statistics
# ---------------------------------------------------------------------------

@dataclass
class ForeignKeyStats:
    """
    Statistics about a FK relationship — cardinality pattern, match fraction, etc.

    Complements CanonicalForeignKey (structural) with runtime data characteristics.

    cardinality_pattern
        "N:1"  most common — many child rows per parent (orders → customers)
        "1:1"  one-to-one lookup / reference table
        "N:M"  many-to-many (via junction table)
        "1:N"  parent has one or more children (same as N:1 from child perspective)

    match_fraction
        Fraction of FK column values that exist in the parent table [0.0, 1.0].
        < 1.0 indicates orphaned records.
    """

    columns: list[str]
    parent_table: str
    parent_columns: list[str]
    parent_schema: Optional[str] = None
    name: Optional[str] = None
    cardinality_pattern: str = "N:1"
    match_fraction: float = 1.0             # 1.0 = no orphans
    avg_children_per_parent: Optional[float] = None  # average fan-out

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "columns": self.columns,
            "parent_table": self.parent_table,
            "parent_columns": self.parent_columns,
            "cardinality_pattern": self.cardinality_pattern,
            "match_fraction": self.match_fraction,
        }
        if self.parent_schema:
            d["parent_schema"] = self.parent_schema
        if self.name:
            d["name"] = self.name
        if self.avg_children_per_parent is not None:
            d["avg_children_per_parent"] = self.avg_children_per_parent
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ForeignKeyStats:
        return cls(
            columns=list(d["columns"]),
            parent_table=d["parent_table"],
            parent_columns=list(d["parent_columns"]),
            parent_schema=d.get("parent_schema"),
            name=d.get("name"),
            cardinality_pattern=str(d.get("cardinality_pattern", "N:1")),
            match_fraction=float(d.get("match_fraction", 1.0)),
            avg_children_per_parent=d.get("avg_children_per_parent"),
        )


# ---------------------------------------------------------------------------
# Table statistics
# ---------------------------------------------------------------------------

@dataclass
class TableStats:
    """
    Table-level statistics container.

    Combines all column, index, FK, and composite stats for one table.
    """

    name: str
    schema: Optional[str] = None
    catalog: Optional[str] = None

    # Table-level counts / sizes
    row_count: int = 0
    data_size_bytes: Optional[int] = None
    avg_row_bytes: Optional[int] = None
    last_analyzed: Optional[str] = None    # ISO-8601 datetime string

    # Per-column statistics (keyed by column name for fast lookup)
    columns: list[ColumnStats] = field(default_factory=list)

    # Index statistics
    indexes: list[IndexStats] = field(default_factory=list)

    # Foreign key statistics (runtime characteristics; complements structural FKs)
    foreign_keys: list[ForeignKeyStats] = field(default_factory=list)

    # Multi-column / extended statistics
    composite_stats: list[CompositeColumnStats] = field(default_factory=list)

    def column_stats(self, name: str) -> Optional[ColumnStats]:
        """Return ColumnStats for *name*, or None if not present."""
        for cs in self.columns:
            if cs.name == name:
                return cs
        return None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "row_count": self.row_count}
        if self.schema:
            d["schema"] = self.schema
        if self.catalog:
            d["catalog"] = self.catalog
        if self.data_size_bytes is not None:
            d["data_size_bytes"] = self.data_size_bytes
        if self.avg_row_bytes is not None:
            d["avg_row_bytes"] = self.avg_row_bytes
        if self.last_analyzed:
            d["last_analyzed"] = self.last_analyzed
        if self.columns:
            d["columns"] = [c.to_dict() for c in self.columns]
        if self.indexes:
            d["indexes"] = [i.to_dict() for i in self.indexes]
        if self.foreign_keys:
            d["foreign_keys"] = [fk.to_dict() for fk in self.foreign_keys]
        if self.composite_stats:
            d["composite_stats"] = [cs.to_dict() for cs in self.composite_stats]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TableStats:
        return cls(
            name=d["name"],
            schema=d.get("schema"),
            catalog=d.get("catalog"),
            row_count=int(d.get("row_count", 0)),
            data_size_bytes=d.get("data_size_bytes"),
            avg_row_bytes=d.get("avg_row_bytes"),
            last_analyzed=d.get("last_analyzed") and str(d["last_analyzed"]),
            columns=[ColumnStats.from_dict(c) for c in d.get("columns", [])],
            indexes=[IndexStats.from_dict(i) for i in d.get("indexes", [])],
            foreign_keys=[ForeignKeyStats.from_dict(fk) for fk in d.get("foreign_keys", [])],
            composite_stats=[CompositeColumnStats.from_dict(cs) for cs in d.get("composite_stats", [])],
        )


# ---------------------------------------------------------------------------
# Top-level database statistics container
# ---------------------------------------------------------------------------

@dataclass
class DatabaseStats:
    """
    Top-level container for statistics of one database / schema.

    This is the YAML-serialisable root object:
        stats = DatabaseStats(source_dialect="postgres", tables=[...])
        yaml_str = yaml.dump(stats.to_dict(), sort_keys=False)
        stats2 = DatabaseStats.from_dict(yaml.safe_load(yaml_str))
        assert stats == stats2
    """

    tables: list[TableStats] = field(default_factory=list)
    version: str = "1.0"
    source_dialect: Optional[str] = None   # where stats were collected from

    def table_stats(self, name: str) -> Optional[TableStats]:
        """Return TableStats for *name* (case-sensitive), or None."""
        for ts in self.tables:
            if ts.name == name:
                return ts
        return None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"version": self.version}
        if self.source_dialect:
            d["source_dialect"] = self.source_dialect
        d["tables"] = [t.to_dict() for t in self.tables]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DatabaseStats:
        return cls(
            version=str(d.get("version", "1.0")),
            source_dialect=d.get("source_dialect"),
            tables=[TableStats.from_dict(t) for t in d.get("tables", [])],
        )
