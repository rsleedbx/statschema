"""
statschema/resources.py — MCP resource registry.

Resources are read-only data exposed over the MCP channel so the agent can
inspect schemas and stats files before deciding which tools to call.

    statschema://schema/{path}   — serve a schema.yaml file as text
    statschema://stats/{path}    — serve a stats.yaml file as text
    statschema://dialects        — list of supported SQL dialects
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def register_resources(server: Any) -> None:
    """Register all statschema MCP resources on a FastMCP server instance."""

    @server.resource("statschema://schema/{path}")
    def read_schema(path: str) -> str:
        """
        Read a schema.yaml file from the local filesystem.

        Returns the raw YAML text of the file at ``path``.  Use this to
        inspect an existing schema before calling generate_data or load_data.
        ``path`` may be relative to the current working directory or absolute.
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Schema file not found: {p.resolve()}")
        return p.read_text(encoding="utf-8")

    @server.resource("statschema://stats/{path}")
    def read_stats(path: str) -> str:
        """
        Read a stats.yaml file from the local filesystem.

        Returns the raw YAML text of the file at ``path``.  Use this to
        inspect collected statistics before calling inject_stats.
        ``path`` may be relative to the current working directory or absolute.
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Stats file not found: {p.resolve()}")
        return p.read_text(encoding="utf-8")

    @server.resource("statschema://dialects")
    def list_dialects() -> str:
        """
        List all SQL dialects supported by statschema.

        Returns a JSON array of dialect name strings accepted by collect_stats,
        inject_stats, load_data, and transpile_ddl.
        """
        import json

        from .ddl_emitter import SUPPORTED_DIALECTS

        return json.dumps(sorted(SUPPORTED_DIALECTS))
