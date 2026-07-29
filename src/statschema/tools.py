"""
statschema/tools.py — MCP tool registry.

All business logic lives in statschema.services.
This file maps tool names → service functions and registers them on a FastMCP
server instance.

Adding a new tool: add one decorated async function inside register_tools().
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any

import yaml
try:
    from mcp.server.fastmcp import Context
except ImportError as e:
    raise ImportError("Install the mcp extra:  pip install 'statschema[mcp]'") from e


async def _run(
    fn: Any,
    ctx: Context | None = None,
    **kwargs: Any,
) -> str:
    """Call a synchronous service function in a thread pool.

    Emits MCP logging notifications (info / error) and progress ticks when
    ``ctx`` is provided.  Returns a JSON or YAML string.
    """
    tool = fn.__module__.split(".")[-1] + "." + fn.__name__
    args_summary = " ".join(f"{k}={v!r}" for k, v in kwargs.items() if v is not None)
    start_msg = f"[{tool}] starting — {args_summary}"
    print(f"==> {start_msg}", file=sys.stderr, flush=True)
    if ctx is not None:
        await ctx.info(start_msg)
        await ctx.report_progress(0, 1)

    t0 = time.monotonic()
    try:
        result = await asyncio.to_thread(fn, **kwargs)
        elapsed = time.monotonic() - t0
        done_msg = f"[{tool}] done ({elapsed:.1f}s)"
        print(f"==> {done_msg}", file=sys.stderr, flush=True)
        if ctx is not None:
            await ctx.info(done_msg)
            await ctx.report_progress(1, 1)
        if result is None:
            return "done"
        if isinstance(result, str):
            return result
        try:
            return json.dumps(result, default=str)
        except TypeError:
            return str(result)
    except Exception as exc:
        elapsed = time.monotonic() - t0
        err_msg = f"[{tool}] ERROR after {elapsed:.1f}s: {exc}"
        print(f"==> {err_msg}", file=sys.stderr, flush=True)
        if ctx is not None:
            await ctx.error(err_msg)
        raise


def register_tools(server: Any) -> None:
    """Register all statschema MCP tools on a FastMCP server instance."""

    # ── describe_schema ────────────────────────────────────────────────────

    @server.tool()
    async def describe_schema(
        num_tables: int = 10,
        num_columns: int = 10,
        pk_type: str = "bigint",
        col_types: str = "int,float,text",
        distribution: str = "zipf",
        distribution_params_json: str = "",
        seed: int | None = None,
        row_count: int = 10_000,
        ctx: Context = None,
    ) -> str:
        """Generate a canonical schema.yaml from a structured description.

        No source database or DDL file is required.  Returns a YAML string
        containing the generated schema (and heuristic statistics embedded
        as a comment block).

        Parameters
        ----------
        num_tables            Number of tables to generate (default: 10).
        num_columns           Non-PK columns per table (default: 10).
        pk_type               Primary key column type: bigint (default), int, long.
        col_types             Comma-separated type list to draw from, e.g.
                              "int,float,text" (default).  Repeat a type to
                              increase its frequency: "int,int,text".
        distribution          Distribution applied to every data column.
                              One of: zipf (default), uniform, normal,
                              exponential, sequential.
        distribution_params_json  JSON object of distribution parameters, e.g.
                              '{"exponent": 1.5}' for zipf.
        seed                  Integer seed for reproducible layout.
        row_count             Row count hint for heuristic stats (default: 10 000).
        """
        from .core.schema_io import dump_schema
        from .services.describe import describe

        types = [t.strip() for t in col_types.split(",") if t.strip()]
        dist_params = json.loads(distribution_params_json) if distribution_params_json else None

        def _inner() -> str:
            tables, _stats = describe(
                num_tables=num_tables,
                num_columns=num_columns,
                pk_type=pk_type,
                col_types=types,
                distribution=distribution,
                distribution_params=dist_params,
                seed=seed,
                row_count=row_count,
            )
            return dump_schema(tables)

        return await _run(_inner, ctx=ctx)

    # ── generate_data ──────────────────────────────────────────────────────

    @server.tool()
    async def generate_data(
        schema_yaml: str,
        rows: int = 1000,
        seed: int = 42,
        ctx: Context = None,
    ) -> str:
        """Generate synthetic rows from a schema YAML string.

        Returns a JSON object mapping table name → list of row dicts.

        Parameters
        ----------
        schema_yaml   YAML string produced by describe_schema or collect_stats.
        rows          Rows to generate per table (default: 1 000).
        seed          Random seed (default: 42).
        """
        from .core.schema_io import load_canonical
        from .services.generate import generate

        def _inner() -> str:
            tables = load_canonical(schema_yaml)
            result: dict[str, list[dict]] = {}
            for table in tables:
                df = generate(table, rows, seed=seed)
                try:
                    result[table.name] = df.to_dict(orient="records")
                except AttributeError:
                    # StreamingGenerator returns an iterator
                    result[table.name] = list(df)
            return json.dumps(result, default=str)

        return await _run(_inner, ctx=ctx)

    # ── load_data ──────────────────────────────────────────────────────────

    @server.tool()
    async def load_data(
        schema_yaml: str,
        dsn: str,
        dialect: str,
        sf: float = 1.0,
        seed: int = 42,
        ctx: Context = None,
    ) -> str:
        """Generate synthetic rows and load them into a live database.

        Creates tables (DROP + CREATE) and bulk-loads synthetic data.  The
        database must be reachable from this machine.

        Parameters
        ----------
        schema_yaml  YAML string produced by describe_schema or collect_stats.
        dsn          Connection string for the target database.
        dialect      Target dialect: postgres, mysql, mssql, oracle, databricks.
        sf           Scale factor applied to row_count_per_sf columns (default: 1.0).
        seed         Random seed (default: 42).
        """
        from .cli import _connect
        from .core.schema_io import load_canonical, resolve_row_counts
        from .data_loader import load_dataframe
        from .services.generate import generate

        def _inner() -> str:
            tables = load_canonical(schema_yaml)
            counts = resolve_row_counts(tables, scale_factor=sf)
            conn = _connect(dialect, dsn)
            loaded: dict[str, int] = {}
            try:
                for table in tables:
                    n = counts.get(table.name, 1000)
                    df = generate(table, n, seed=seed)
                    load_dataframe(df, table, conn, dialect)
                    loaded[table.name] = n
            finally:
                conn.close()
            return json.dumps({"loaded": loaded})

        return await _run(_inner, ctx=ctx)

    # ── collect_stats ──────────────────────────────────────────────────────

    @server.tool()
    async def collect_stats(
        dsn: str,
        dialect: str,
        tables: str,
        schema: str = "",
        ctx: Context = None,
    ) -> str:
        """Collect DDL and column statistics from a live source database.

        Returns a YAML string containing the collected stats, suitable for
        passing to inject_stats.

        Parameters
        ----------
        dsn      Connection string for the source database.
        dialect  Source dialect: postgres, mysql, mssql, oracle, db2, databricks.
        tables   Comma-separated table names to collect.
        schema   Schema / namespace name (empty = connection default).
        """
        from .cli import _connect
        from .services.collect import collect

        def _inner() -> str:
            table_list = [t.strip() for t in tables.split(",") if t.strip()]
            conn = _connect(dialect, dsn)
            try:
                db_stats = collect(conn, table_list, dialect, schema=schema or None)
            finally:
                conn.close()
            return yaml.dump(db_stats.to_dict(), sort_keys=False, allow_unicode=True)

        return await _run(_inner, ctx=ctx)

    # ── inject_stats ───────────────────────────────────────────────────────

    @server.tool()
    async def inject_stats(
        dsn: str,
        dialect: str,
        stats_yaml: str,
        schema: str = "public",
        ctx: Context = None,
    ) -> str:
        """Inject collected statistics into a target database optimizer.

        Loads statistics from a YAML string (produced by collect_stats or
        describe_schema) and pushes them into the target catalog so the
        optimizer sees production-representative cardinalities before any
        real data is loaded.

        Parameters
        ----------
        dsn         Connection string for the target database.
        dialect     Target dialect: postgres, mysql, mssql, oracle, databricks.
        stats_yaml  YAML string from collect_stats or describe_schema.
        schema      Target schema name (default: "public").
        """
        from .cli import _connect
        from .services.inject import inject
        from .stats_model import DatabaseStats

        def _inner() -> str:
            db_stats = DatabaseStats.from_dict(yaml.safe_load(stats_yaml))
            conn = _connect(dialect, dsn)
            results: list[dict] = []
            try:
                for ts in db_stats.tables:
                    result = inject(conn, ts, dialect, schema=schema)
                    results.append({
                        "table": ts.name,
                        "injected": result.rows_injected,
                        "skipped": result.columns_skipped,
                        "warnings": result.warnings,
                    })
            finally:
                conn.close()
            return json.dumps({"results": results})

        return await _run(_inner, ctx=ctx)

    # ── transpile_ddl ──────────────────────────────────────────────────────

    @server.tool()
    async def transpile_ddl(
        sql: str,
        source_dialect: str = "",
        target_dialect: str = "postgres",
        ctx: Context = None,
    ) -> str:
        """Parse DDL from one dialect and emit it in another.

        Handles type mapping, constraints, defaults, and column comments.

        Parameters
        ----------
        sql             DDL SQL string with one or more CREATE TABLE statements.
        source_dialect  Source dialect hint (empty = auto-detect).
        target_dialect  Target dialect (default: postgres).
        """
        from .services.transpile import transpile

        def _inner() -> str:
            return transpile(sql, source_dialect or None, target_dialect)

        return await _run(_inner, ctx=ctx)
