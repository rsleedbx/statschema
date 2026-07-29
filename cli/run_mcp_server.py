#!/usr/bin/env python3
"""
cli/run_mcp_server.py — Run the statschema MCP server (stdio transport).

Cursor and Claude Desktop spawn this script as a subprocess — they communicate
over stdin/stdout using the MCP JSON-RPC protocol.

Registration is handled by cli/setup_mcp.py, which writes the path to this
file into ~/.cursor/mcp.json and the Claude Desktop config.

    python cli/run_mcp_server.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from statschema.server import server  # noqa: E402


async def _main() -> None:
    await server.run_stdio_async()


if __name__ == "__main__":
    asyncio.run(_main())
