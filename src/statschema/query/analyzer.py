"""
Layer 3 query-pattern analysis — extended statistics and FK range inference.

This module provides three capabilities:

1. **Column-pair extraction** (``extract_column_pairs``): parse SQL queries and
   find which columns from the same table appear together in JOIN ON / WHERE /
   GROUP BY / HAVING.  Used to drive ``CREATE STATISTICS`` (Phase A.5/D.5).

2. **Table classification** (``classify_tables``): automatically identify which
   tables are *fact* tables (many FK columns, large row count) vs *dimension* /
   *lookup* tables.  Useful for targeting FK range overrides and display.

3. **Predicate range extraction** (``extract_column_predicates`` +
   ``resolve_fk_generation_ranges``): parse BETWEEN / = predicates from query
   WHERE clauses, then query the *source* database to find which PK values of
   the referenced dimension table satisfy those predicates.  The resulting
   ``[min_fk, max_fk]`` range is passed to ``build_rows_from_canonical`` so
   the synthetic fact table concentrates FK values in the same data window as
   the real workload.

Why column structure works with bind variables
----------------------------------------------
The column-pair and predicate-column extraction reads only *which* columns
appear together, not the predicate values themselves.  ``WHERE d.d_year = 2005``
and ``WHERE d.d_year = :b1`` both identify ``d_year`` as a predicate column.
The actual range resolution (step 3) queries the live source with the literal
value when available; placeholders produce no range override (graceful fallback).
"""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlglot
import sqlglot.expressions as exp

logger = logging.getLogger(__name__)

# Column name suffixes that suggest a FK-like column when no explicit
# ``references:`` declaration exists in the schema YAML.
_FK_SUFFIXES = ("_id", "_sk", "_key", "_no", "_symb", "_cd", "_num")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class ColumnPredicate:
    """A resolved range predicate extracted from a SQL query WHERE clause."""
    table_name: str       # resolved table name (alias stripped)
    column_name: str      # column carrying the predicate
    min_value: Any        # inclusive lower bound (None = unbounded)
    max_value: Any        # inclusive upper bound (None = unbounded)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_column_pairs(
    queries_yaml_path: Path,
    table_names: set[str],
) -> dict[str, list[tuple[str, str]]]:
    """
    Parse all SQL queries in *queries_yaml_path* and return the set of
    intra-table column pairs that appear together in non-SELECT positions
    (JOIN ON, WHERE, GROUP BY, HAVING) across all queries.

    Parameters
    ----------
    queries_yaml_path:
        Path to a benchmark queries YAML file
        (``benchmarks/queries/<schema>.yaml``).
    table_names:
        Lowercase set of table names present in the schema.  Only columns
        from these tables are included in the output.

    Returns
    -------
    dict[table_name, list[(col_a, col_b)]]
        Sorted, deduplicated pairs per table.  Empty dict when no pairs are
        found (single-table queries, no GROUP BY, etc.).
    """
    from .model import load_queries

    workload = load_queries(queries_yaml_path)
    table_names_lower = {t.lower() for t in table_names}

    all_pairs: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for query in workload.queries:
        pairs = _pairs_from_sql(query.sql, table_names_lower)
        for table, table_pairs in pairs.items():
            all_pairs[table].update(table_pairs)

    return {t: sorted(p) for t, p in all_pairs.items() if p}


def make_stat_name(table_name: str, col_a: str, col_b: str) -> str:
    """
    Build a PostgreSQL-safe (≤63 chars) name for a CREATE STATISTICS object.
    Uses MD5 truncation when the natural name would exceed the identifier limit.
    """
    raw = f"_sts_{table_name}_{col_a}_{col_b}"
    if len(raw) <= 63:
        return raw
    digest = hashlib.md5(raw.encode()).hexdigest()[:12]
    return f"_sts_{digest}"


# ---------------------------------------------------------------------------
# Table classification
# ---------------------------------------------------------------------------

def classify_tables(
    tables: list,
    row_counts: dict[str, int],
) -> dict[str, str]:
    """
    Classify each table as ``'fact'``, ``'dimension'``, or ``'lookup'``.

    Heuristics (in priority order):

    1. **Declared FKs**: tables with 3+ ``references:`` columns → ``'fact'``.
    2. **Implied FKs**: tables whose integer/string columns *name* like FKs
       (suffix ``_id``, ``_sk``, ``_key``, ``_symb``, …) and are not the PK
       → counts toward fact classification (3+ implied FKs → ``'fact'``).
    3. **Tiny tables** (row_count < 200 and no FK columns) → ``'lookup'``.
    4. **Large-vs-neighbour**: if a table has ≥ 5× more rows than the median
       row count of all tables it directly joins with (via declared FKs), it
       is classified as ``'fact'``.
    5. Default → ``'dimension'``.

    Parameters
    ----------
    tables:
        List of ``CanonicalTableSchema`` objects from ``load_canonical``.
    row_counts:
        ``{table_name: row_count}`` for all tables (from ``resolve_row_counts``
        or actually measured counts).

    Returns
    -------
    ``{table_name: role}`` where ``role ∈ {'fact', 'dimension', 'lookup'}``.
    """
    # --- Build FK graph ---------------------------------------------------
    # out_degree: # FK columns this table has (declared or implied)
    # ref_by: set of tables that have FKs pointing at this table
    out_degrees_declared: dict[str, int] = {}
    out_degrees_implied: dict[str, int] = {}
    ref_by: dict[str, set[str]] = defaultdict(set)

    table_by_name = {t.name: t for t in tables}

    for t in tables:
        declared = 0
        for col in t.columns:
            if getattr(col, "references", None):
                parent, _ = col.references
                ref_by[parent].add(t.name)
                declared += 1
        out_degrees_declared[t.name] = declared

        # Implied FK heuristic: integer/string columns ending with FK suffix
        # that are not the PK column (sequential unique column).
        pk_cols = {
            c.name for c in t.columns
            if getattr(c, "primary_key", False)
            or (
                getattr(c, "generation", None)
                and getattr(c.generation, "unique", False)
            )
        }
        implied = sum(
            1 for c in t.columns
            if c.name not in pk_cols
            and c.name.lower().endswith(_FK_SUFFIXES)
            and getattr(c, "references", None) is None
        )
        out_degrees_implied[t.name] = implied

    roles: dict[str, str] = {}
    for t in tables:
        n = row_counts.get(t.name, 0)
        decl = out_degrees_declared[t.name]
        impl = out_degrees_implied[t.name]
        total_fk_like = decl + impl

        # Rule 1+2: many FK columns → fact
        if total_fk_like >= 3:
            roles[t.name] = "fact"
            continue

        # Rule 3: tiny with no FKs → lookup
        if n < 200 and total_fk_like == 0:
            roles[t.name] = "lookup"
            continue

        # Rule 4: large relative to FK parents
        if decl >= 1:
            parent_row_counts = [
                row_counts.get(c.references[0], 0)
                for c in t.columns
                if getattr(c, "references", None)
            ]
            if parent_row_counts:
                median_parent = sorted(parent_row_counts)[len(parent_row_counts) // 2]
                if median_parent > 0 and n >= 5 * median_parent:
                    roles[t.name] = "fact"
                    continue

        # Default
        roles[t.name] = "dimension"

    return roles


# ---------------------------------------------------------------------------
# Predicate extraction
# ---------------------------------------------------------------------------

def extract_column_predicates(
    queries_yaml_path: Path,
    table_names: set[str],
) -> list[ColumnPredicate]:
    """
    Parse all SQL queries in *queries_yaml_path* and return range predicates
    found in WHERE clauses.

    Handles:
    - ``col BETWEEN low AND high``
    - ``col = literal``
    - ``col >= literal`` / ``col > literal``
    - ``col <= literal`` / ``col < literal``

    Placeholders (``:b1``, ``?``, ``@p``) are silently skipped — they produce
    no ``ColumnPredicate`` entry.

    Parameters
    ----------
    queries_yaml_path:
        Path to benchmark queries YAML.
    table_names:
        Lowercase set of table names to resolve against.

    Returns
    -------
    List of :class:`ColumnPredicate`.  Multiple predicates for the same
    (table, col) from different queries are all returned; callers can merge
    them into a union range if desired.
    """
    from .model import load_queries

    workload = load_queries(queries_yaml_path)
    table_names_lower = {t.lower() for t in table_names}

    results: list[ColumnPredicate] = []
    for query in workload.queries:
        results.extend(_predicates_from_sql(query.sql, table_names_lower))
    return results


def resolve_fk_generation_ranges(
    conn: Any,
    source_schema: str,
    predicates: list[ColumnPredicate],
    tables: list,
) -> dict[str, dict[str, tuple[int, int]]]:
    """
    Translate dimension-table predicates into FK generation ranges for fact tables.

    For each :class:`ColumnPredicate` on ``dim_table.filter_col``:

    1. Find every fact-table FK column that references ``dim_table``.
    2. Query the source to find ``MIN(pk_col), MAX(pk_col)`` where the filter
       condition holds.
    3. Return the resulting range as a generation override for the FK column.

    Multiple predicates for the same FK column are merged as the **union**
    (widest range) across all queries.

    Parameters
    ----------
    conn:
        Live psycopg2 connection to the *source* database.
    source_schema:
        Schema name in the source database (e.g. ``'tpch_src'``).
    predicates:
        Output of :func:`extract_column_predicates`.
    tables:
        List of ``CanonicalTableSchema`` objects (to discover FK declarations).

    Returns
    -------
    ``{fact_table: {fk_col: (min_val, max_val)}}``
    Suitable for passing as ``fk_range_overrides`` to
    ``build_rows_from_canonical``.
    """
    if not predicates:
        return {}

    # Index FK relationships: (dim_table, pk_col) → [(fact_table, fk_col)]
    fk_index: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for t in tables:
        for col in t.columns:
            ref = getattr(col, "references", None)
            if ref:
                parent_table, parent_col = ref
                fk_index[(parent_table.lower(), parent_col.lower())].append(
                    (t.name, col.name)
                )

    # Group predicates by (table, col) and merge into a single range per pair
    from collections import defaultdict as dd
    merged: dict[tuple[str, str], list[ColumnPredicate]] = dd(list)
    for p in predicates:
        merged[(p.table_name, p.column_name)].append(p)

    overrides: dict[str, dict[str, tuple[int, int]]] = defaultdict(dict)

    with conn.cursor() as cur:
        for (dim_table, filter_col), preds in merged.items():
            # Find which FK columns reference this table and which PK they use
            # We need to find the PK column of dim_table that FK columns reference.
            # A predicate on dim_table.filter_col constrains rows of dim_table;
            # we want the PK column range (the FK target) for matching rows.
            referenced_pks = set()
            for (parent_t, parent_pk), fk_list in fk_index.items():
                if parent_t == dim_table:
                    referenced_pks.add(parent_pk)

            if not referenced_pks:
                logger.debug(
                    "No FK references found for %s; skipping predicate range", dim_table
                )
                continue

            for pk_col in referenced_pks:
                # Build WHERE clause from predicates (union: OR each predicate)
                where_parts, params = [], []
                for p in preds:
                    if p.min_value is not None and p.max_value is not None:
                        where_parts.append(f'"{filter_col}" BETWEEN %s AND %s')
                        params.extend([p.min_value, p.max_value])
                    elif p.min_value is not None:
                        where_parts.append(f'"{filter_col}" >= %s')
                        params.append(p.min_value)
                    elif p.max_value is not None:
                        where_parts.append(f'"{filter_col}" <= %s')
                        params.append(p.max_value)

                if not where_parts:
                    continue

                where_sql = " OR ".join(where_parts)
                sql = (
                    f'SELECT MIN("{pk_col}"), MAX("{pk_col}") '
                    f'FROM "{source_schema}"."{dim_table}" '
                    f'WHERE {where_sql}'
                )
                try:
                    cur.execute(sql, params)
                    row = cur.fetchone()
                    if row and row[0] is not None and row[1] is not None:
                        range_min, range_max = int(row[0]), int(row[1])
                        # Apply to all FK columns that reference this pk_col
                        for fact_table, fk_col in fk_index.get(
                            (dim_table, pk_col), []
                        ):
                            if fk_col in overrides[fact_table]:
                                # Merge: take union of all query ranges
                                existing_min, existing_max = overrides[fact_table][fk_col]
                                overrides[fact_table][fk_col] = (
                                    min(existing_min, range_min),
                                    max(existing_max, range_max),
                                )
                            else:
                                overrides[fact_table][fk_col] = (range_min, range_max)
                            logger.debug(
                                "FK range override: %s.%s → [%d, %d]  "
                                "(from %s.%s predicate)",
                                fact_table, fk_col, range_min, range_max,
                                dim_table, filter_col,
                            )
                except Exception as exc:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    logger.debug("FK range query failed for %s.%s: %s", dim_table, filter_col, exc)

    return dict(overrides)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _pairs_from_sql(
    sql: str,
    table_names: set[str],
) -> dict[str, list[tuple[str, str]]]:
    """
    Extract intra-table column pairs from a single SQL query.

    Only JOIN ON, WHERE, GROUP BY, and HAVING positions are considered —
    the SELECT list (including aggregate arguments) is excluded because
    aggregate output columns do not constrain join cardinality estimates.
    """
    try:
        tree = sqlglot.parse_one(sql, dialect="ansi", error_level=sqlglot.ErrorLevel.RAISE)
    except Exception:
        try:
            tree = sqlglot.parse_one(sql, error_level=sqlglot.ErrorLevel.WARN)
        except Exception as exc:
            logger.debug("sqlglot parse failed: %s", exc)
            return {}

    # ── Build alias → resolved table name map ─────────────────────────────
    alias_to_table: dict[str, str] = {}
    from_tables: list[str] = []

    for node in tree.find_all(exp.Table):
        table_name = node.name.lower()
        alias = (node.alias or "").lower()
        if alias:
            alias_to_table[alias] = table_name
        alias_to_table.setdefault(table_name, table_name)
        from_tables.append(table_name)

    single_from_table: str | None = from_tables[0] if len(from_tables) == 1 else None

    def resolve(col_expr: exp.Column) -> tuple[str | None, str | None]:
        """Return (resolved_table_name, column_name) or (None, None)."""
        # sqlglot normalises unquoted identifiers to lowercase; quoted ones
        # preserve case.  We lowercase both here for uniform matching.
        col = (col_expr.name or "").lower()
        if not col:
            return None, None
        table_ref = (col_expr.table or "").lower()
        if table_ref:
            resolved = alias_to_table.get(table_ref)
        else:
            resolved = single_from_table
        return resolved, col

    # ── Collect column references from non-SELECT positions ────────────────
    relevant: list[exp.Column] = []

    for join_node in tree.find_all(exp.Join):
        on_clause = join_node.args.get("on")
        if on_clause:
            relevant.extend(on_clause.find_all(exp.Column))

    where_clause = tree.args.get("where")
    if where_clause:
        relevant.extend(where_clause.find_all(exp.Column))

    group_clause = tree.args.get("group")
    if group_clause:
        relevant.extend(group_clause.find_all(exp.Column))

    having_clause = tree.args.get("having")
    if having_clause:
        relevant.extend(having_clause.find_all(exp.Column))

    # ── Group columns by resolved table ───────────────────────────────────
    per_table: dict[str, set[str]] = defaultdict(set)
    for col_expr in relevant:
        tbl, col = resolve(col_expr)
        if tbl and col and tbl in table_names:
            per_table[tbl].add(col)

    # ── Generate sorted pairs ──────────────────────────────────────────────
    result: dict[str, list[tuple[str, str]]] = {}
    for tbl, cols in per_table.items():
        col_list = sorted(cols)
        if len(col_list) >= 2:
            pairs = [
                (a, b)
                for i, a in enumerate(col_list)
                for b in col_list[i + 1 :]
            ]
            result[tbl] = pairs

    return result


def _predicates_from_sql(
    sql: str,
    table_names: set[str],
) -> list[ColumnPredicate]:
    """Extract range predicates from WHERE clauses in a single SQL statement."""
    try:
        tree = sqlglot.parse_one(sql, error_level=sqlglot.ErrorLevel.WARN)
    except Exception:
        return []

    # Build alias map
    alias_to_table: dict[str, str] = {}
    from_tables: list[str] = []
    for node in tree.find_all(exp.Table):
        tname = node.name.lower()
        alias = (node.alias or "").lower()
        if alias:
            alias_to_table[alias] = tname
        alias_to_table.setdefault(tname, tname)
        from_tables.append(tname)

    single_from = from_tables[0] if len(from_tables) == 1 else None

    def resolve_col(col_expr: exp.Column) -> tuple[str | None, str | None]:
        col = (col_expr.name or "").lower()
        if not col:
            return None, None
        tref = (col_expr.table or "").lower()
        tbl = alias_to_table.get(tref) if tref else single_from
        return tbl, col

    def literal_value(node: exp.Expression) -> Any:
        """Return the Python value for a Literal node, or None for placeholders."""
        if isinstance(node, exp.Literal):
            if node.is_number:
                val = node.this
                return int(val) if "." not in val else float(val)
            return node.this
        return None  # placeholder, subquery, etc.

    results: list[ColumnPredicate] = []

    where = tree.args.get("where")
    if not where:
        return results

    for node in where.find_all(exp.Between):
        tbl, col = resolve_col(node.this) if isinstance(node.this, exp.Column) else (None, None)
        if not tbl or tbl not in table_names:
            continue
        lo = literal_value(node.args.get("low"))
        hi = literal_value(node.args.get("high"))
        if lo is not None or hi is not None:
            results.append(ColumnPredicate(tbl, col, lo, hi))

    for node in where.find_all(exp.EQ):
        left, right = node.left, node.right
        col_node, lit_node = (left, right) if isinstance(left, exp.Column) else (right, left) if isinstance(right, exp.Column) else (None, None)
        if col_node is None:
            continue
        tbl, col = resolve_col(col_node)
        if not tbl or tbl not in table_names:
            continue
        val = literal_value(lit_node)
        if val is not None:
            results.append(ColumnPredicate(tbl, col, val, val))

    for node_cls, is_ge in [(exp.GTE, True), (exp.GT, True), (exp.LTE, False), (exp.LT, False)]:
        for node in where.find_all(node_cls):
            left, right = node.left, node.right
            if isinstance(left, exp.Column) and not isinstance(right, exp.Column):
                tbl, col = resolve_col(left)
                val = literal_value(right)
            elif isinstance(right, exp.Column) and not isinstance(left, exp.Column):
                tbl, col = resolve_col(right)
                val = literal_value(left)
                is_ge = not is_ge  # flip: col <= val becomes a max bound
            else:
                continue
            if not tbl or tbl not in table_names or val is None:
                continue
            if is_ge:
                results.append(ColumnPredicate(tbl, col, val, None))
            else:
                results.append(ColumnPredicate(tbl, col, None, val))

    return results
