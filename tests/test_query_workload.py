"""
Unit tests for the query workload pipeline:
  query_model  → query_transpiler  → query_replayer
No live database connection is required.
"""

from __future__ import annotations

import json
import textwrap
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.statschema import (
    QueryEntry,
    QueryWorkload,
    ReplayResult,
    dump_queries,
    load_queries,
    print_replay_report,
    replay_queries,
    transpile_query,
    transpile_workload,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MYSQL_SELECT = "SELECT order_id, status, total FROM orders WHERE customer_id = 42 LIMIT 10"
TSQL_TOP     = "SELECT TOP 10 order_id, status, total FROM orders WHERE customer_id = 42"
ORACLE_SELECT= "SELECT order_id FROM (SELECT order_id FROM orders WHERE customer_id = 42) WHERE ROWNUM <= 10"
NONPORTABLE  = "SELECT * FROM orders FOR XML PATH('order')"
CONNECT_BY   = "SELECT level, order_id FROM orders CONNECT BY PRIOR order_id = parent_id"


def _entry(sql: str, dialect: str = "mysql", **kw) -> QueryEntry:
    return QueryEntry(id="test01", source_dialect=dialect, sql=sql, **kw)


# ---------------------------------------------------------------------------
# QueryModel YAML round-trip
# ---------------------------------------------------------------------------

class TestQueryModelYaml:
    def test_round_trip(self, tmp_path: Path) -> None:
        entry = QueryEntry(
            id="abc123",
            source_dialect="mysql",
            sql=MYSQL_SELECT,
            tables=["orders"],
            calls=5000,
            total_elapsed_ms=12300.5,
            mean_elapsed_ms=2.46,
        )
        workload = QueryWorkload(source_dialect="mysql", queries=[entry])
        path = tmp_path / "queries.yaml"
        dump_queries(workload, path)
        loaded = load_queries(path)

        assert loaded.source_dialect == "mysql"
        assert len(loaded.queries) == 1
        e = loaded.queries[0]
        assert e.id == "abc123"
        assert e.sql == MYSQL_SELECT
        assert e.tables == ["orders"]
        assert e.calls == 5000
        assert abs(e.total_elapsed_ms - 12300.5) < 0.001
        assert abs(e.mean_elapsed_ms - 2.46) < 0.001

    def test_round_trip_from_dict(self) -> None:
        data = {
            "source_dialect": "tsql",
            "queries": [{"id": "q1", "source_dialect": "tsql", "sql": TSQL_TOP}],
        }
        workload = load_queries(data)
        assert workload.source_dialect == "tsql"
        assert workload.queries[0].sql == TSQL_TOP

    def test_manual_review_preserved(self, tmp_path: Path) -> None:
        entry = QueryEntry(
            id="q2", source_dialect="oracle", sql=CONNECT_BY,
            manual_review=True, transpile_error="CONNECT BY has no portable equivalent."
        )
        path = tmp_path / "q.yaml"
        dump_queries(QueryWorkload(source_dialect="oracle", queries=[entry]), path)
        loaded = load_queries(path)
        assert loaded.queries[0].manual_review is True
        assert "CONNECT BY" in loaded.queries[0].transpile_error

    def test_empty_workload(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.yaml"
        dump_queries(QueryWorkload(source_dialect="postgres", queries=[]), path)
        loaded = load_queries(path)
        assert loaded.queries == []


# ---------------------------------------------------------------------------
# Transpiler
# ---------------------------------------------------------------------------

class TestTranspileQuery:
    def test_mysql_to_postgres(self) -> None:
        entry = _entry(MYSQL_SELECT)
        result = transpile_query(entry, "postgres")
        assert result.manual_review is False
        assert "LIMIT" in result.sql
        assert result.source_dialect == "mysql"

    def test_tsql_to_postgres(self) -> None:
        entry = _entry(TSQL_TOP, dialect="tsql")
        result = transpile_query(entry, "postgres")
        assert result.manual_review is False
        assert "LIMIT" in result.sql
        assert "TOP" not in result.sql

    def test_same_dialect_family_noop(self) -> None:
        entry = _entry(MYSQL_SELECT, dialect="mysql")
        result = transpile_query(entry, "mariadb")
        assert result.manual_review is False
        assert result.sql == MYSQL_SELECT

    def test_for_xml_flagged(self) -> None:
        entry = _entry(NONPORTABLE, dialect="tsql")
        result = transpile_query(entry, "postgres")
        assert result.manual_review is True
        assert result.transpile_error is not None
        assert "FOR XML" in result.transpile_error
        # Original SQL is preserved
        assert result.sql == NONPORTABLE

    def test_connect_by_flagged(self) -> None:
        entry = _entry(CONNECT_BY, dialect="oracle")
        result = transpile_query(entry, "postgres")
        assert result.manual_review is True

    def test_unsupported_source_dialect(self) -> None:
        entry = _entry("SELECT 1", dialect="db2")
        result = transpile_query(entry, "postgres")
        assert result.manual_review is True
        assert "db2" in result.transpile_error.lower()

    def test_transpile_workload(self) -> None:
        workload = QueryWorkload(
            source_dialect="tsql",
            queries=[
                _entry(TSQL_TOP, dialect="tsql"),
                _entry(NONPORTABLE, dialect="tsql"),
            ],
        )
        result = transpile_workload(workload, "postgres")
        assert result.source_dialect == "tsql"
        assert result.queries[0].manual_review is False
        assert result.queries[1].manual_review is True

    def test_aggregate_transpile(self) -> None:
        sql = "SELECT customer_id, SUM(total) FROM orders GROUP BY customer_id ORDER BY SUM(total) DESC LIMIT 5"
        entry = _entry(sql, dialect="mysql")
        result = transpile_query(entry, "postgres")
        assert result.manual_review is False
        assert "SUM" in result.sql

    def test_window_function_transpile(self) -> None:
        sql = "SELECT order_id, ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY created_at) as rn FROM orders"
        entry = _entry(sql, dialect="mysql")
        result = transpile_query(entry, "postgres")
        assert result.manual_review is False
        assert "ROW_NUMBER" in result.sql

    # ── User-supplied rewrites ────────────────────────────────────────────────

    def test_rewrite_overrides_sqlglot(self) -> None:
        """A rewrites entry for the target dialect is used verbatim, bypassing sqlglot."""
        pg_rewrite = "SELECT string_agg(name, ',') FROM categories"
        entry = QueryEntry(
            id="q1",
            source_dialect="tsql",
            sql="SELECT STUFF((SELECT ',' + name FROM categories FOR XML PATH('')), 1, 1, '')",
            manual_review=True,
            transpile_error="Contains 'FOR XML'",
            rewrites={"postgres": pg_rewrite, "databricks": "SELECT array_join(collect_list(name), ',') FROM categories"},
        )
        result = transpile_query(entry, "postgres")
        assert result.sql == pg_rewrite
        assert result.manual_review is False
        assert result.transpile_error is None

    def test_rewrite_lakebase_alias(self) -> None:
        """lakebase is a postgres alias — rewrites keyed 'postgres' apply to lakebase targets."""
        pg_rewrite = "SELECT string_agg(name, ',') FROM categories"
        entry = QueryEntry(
            id="q2",
            source_dialect="tsql",
            sql="SELECT STUFF((SELECT ',' + name FROM categories FOR XML PATH('')), 1, 1, '')",
            manual_review=True,
            rewrites={"postgres": pg_rewrite},
        )
        result = transpile_query(entry, "lakebase")
        assert result.sql == pg_rewrite
        assert result.manual_review is False

    def test_rewrite_exact_dialect_key_preferred(self) -> None:
        """When both 'lakebase' and 'postgres' keys exist, the exact key wins."""
        entry = QueryEntry(
            id="q3",
            source_dialect="tsql",
            sql="SELECT TOP 1 1",
            rewrites={
                "postgres":  "SELECT 1 LIMIT 1",
                "lakebase":  "SELECT 1 LIMIT 1 -- lakebase specific",
            },
        )
        result = transpile_query(entry, "lakebase")
        assert "lakebase specific" in result.sql

    def test_no_rewrite_falls_back_to_sqlglot(self) -> None:
        """An entry with rewrites for other dialects still uses sqlglot for unmatched targets."""
        entry = QueryEntry(
            id="q4",
            source_dialect="tsql",
            sql=TSQL_TOP,
            rewrites={"databricks": "SELECT order_id FROM orders LIMIT 10"},
        )
        result = transpile_query(entry, "postgres")
        assert result.manual_review is False
        assert "LIMIT" in result.sql
        assert "TOP" not in result.sql

    def test_rewrite_round_trips_yaml(self, tmp_path: Path) -> None:
        """rewrites dict is preserved through dump_queries / load_queries."""
        entry = QueryEntry(
            id="q5",
            source_dialect="tsql",
            sql="SELECT STUFF((SELECT '' FROM t FOR XML PATH('')), 1, 1, '')",
            manual_review=True,
            rewrites={"postgres": "SELECT string_agg(x, '') FROM t"},
        )
        path = tmp_path / "q.yaml"
        dump_queries(QueryWorkload(source_dialect="tsql", queries=[entry]), path)
        loaded = load_queries(path)
        assert loaded.queries[0].rewrites == {"postgres": "SELECT string_agg(x, '') FROM t"}


# ---------------------------------------------------------------------------
# Replayer (uses mock DB connection)
# ---------------------------------------------------------------------------

class TestReplayQueries:
    def _make_conn(self, plan_rows: list, error: Exception | None = None) -> MagicMock:
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        if error:
            cur.execute.side_effect = error
        else:
            cur.fetchall.return_value = [(row,) for row in plan_rows]
            cur.description = [("QUERY PLAN",)]
        return conn

    def test_successful_replay(self) -> None:
        plan = ["Seq Scan on orders (cost=0.00..500.00 rows=1000 width=50)"]
        conn = self._make_conn(plan)
        workload = QueryWorkload(
            source_dialect="mysql",
            queries=[_entry(MYSQL_SELECT, calls=1000, total_elapsed_ms=500)],
        )
        results = replay_queries(conn, workload, target_dialect="postgres")
        assert len(results) == 1
        assert results[0].success is True
        assert "Seq Scan" in results[0].explain_output

    def test_failed_replay(self) -> None:
        conn = self._make_conn([], error=Exception("table 'orders' does not exist"))
        workload = QueryWorkload(
            source_dialect="mysql",
            queries=[_entry(MYSQL_SELECT)],
        )
        results = replay_queries(conn, workload, target_dialect="postgres")
        assert results[0].success is False
        assert "does not exist" in results[0].error

    def test_skip_manual_review(self) -> None:
        conn = self._make_conn([])
        workload = QueryWorkload(
            source_dialect="tsql",
            queries=[_entry(NONPORTABLE, dialect="tsql")],
        )
        results = replay_queries(conn, workload, target_dialect="postgres", skip_manual_review=True)
        assert results[0].success is False
        assert "Skipped" in results[0].error
        # Connection should not have been used for EXPLAIN
        conn.cursor.return_value.execute.assert_not_called()

    def test_print_replay_report(self) -> None:
        results = [
            ReplayResult("q1", "postgres", "SELECT 1", "Seq Scan on t", success=True),
            ReplayResult("q2", "postgres", "SELECT 2", "", success=False, error="table not found"),
        ]
        buf = StringIO()
        print_replay_report(results, file=buf)
        output = buf.getvalue()
        assert "1 ok" in output
        assert "1 errors" in output
        assert "q2" in output
