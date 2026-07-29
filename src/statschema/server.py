"""
statschema/server.py — FastMCP server for statschema.

Transports:
    stdio (default)  — statschema-server  (console script, Cursor / Claude Desktop)
    SSE              — cli/run_mcp_server_sse.py  (survives Cursor restarts)
    HTTP             — src/statschema/app.py  (FastAPI, Databricks Apps / uvicorn)
"""
from __future__ import annotations

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as e:
    raise ImportError("Install the mcp extra:  pip install 'statschema[mcp]'") from e

from .prompts import register_prompts
from .resources import register_resources
from .tools import register_tools

SERVICE_NAME = "statschema"
SERVICE_DESCRIPTION = (
    "DDL transpiler, optimizer statistics bootstrapper, and synthetic data generator. "
    "Generates schemas from descriptions, creates referentially correct synthetic rows, "
    "and injects statistics into target databases — no production data required."
)

_DEFAULT_HOST      = "127.0.0.1"
_DEFAULT_PORT_BASE = 8765   # scan upward from here to find a free port


def _find_free_port(start: int = _DEFAULT_PORT_BASE) -> int:
    """Return the first TCP port >= start that is not currently bound."""
    import socket
    for port in range(start, start + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((_DEFAULT_HOST, port))
                return port
            except OSError:
                continue
    raise RuntimeError(
        f"No free port found in range {start}–{start + 99}. "
        "Kill any stale MCP server processes and retry."
    )


_DEFAULT_PORT = _find_free_port()


def make_server(host: str = _DEFAULT_HOST, port: int = _DEFAULT_PORT) -> FastMCP:
    """Create a configured FastMCP server instance."""
    s = FastMCP(SERVICE_NAME, instructions=SERVICE_DESCRIPTION, host=host, port=port)
    register_tools(s)
    register_resources(s)
    register_prompts(s)
    return s


# Shared instance for stdio (host/port irrelevant for stdio transport)
server = make_server()


def main() -> None:
    """Entry point for ``statschema-server`` console script (stdio transport)."""
    server.run()
