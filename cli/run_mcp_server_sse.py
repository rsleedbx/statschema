"""
cli/run_mcp_server_sse.py — Run the statschema MCP server over SSE.

SSE transport survives Cursor restarts (the process keeps running between
conversations).  Cursor / Claude Desktop connect via a URL instead of
spawning a new subprocess each time.

Usage
-----
    python cli/run_mcp_server_sse.py                    # default 127.0.0.1:8765
    python cli/run_mcp_server_sse.py --port 9000
    python cli/run_mcp_server_sse.py --host 0.0.0.0 --port 8765

Cursor MCP config (.cursor/mcp.json):
    {
      "mcpServers": {
        "statschema": {
          "url": "http://127.0.0.1:8765/sse"
        }
      }
    }
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import typer

# Allow running from the repo root without pip install
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from statschema.server import make_server, _DEFAULT_HOST, _DEFAULT_PORT  # noqa: E402

app = typer.Typer(add_completion=False)


@app.command()
def main(
    host: str = typer.Option(_DEFAULT_HOST, "--host", help="Bind address"),
    port: int = typer.Option(_DEFAULT_PORT, "--port", help="TCP port"),
) -> None:
    """Run the statschema MCP server over SSE — survives Cursor restarts."""
    print(
        f"==> statschema MCP server (SSE)  http://{host}:{port}/sse",
        file=sys.stderr,
    )
    print(
        f'    Cursor config (.cursor/mcp.json):  "url": "http://{host}:{port}/sse"',
        file=sys.stderr,
    )
    print("    Stop: Ctrl-C", file=sys.stderr)
    s = make_server(host=host, port=port)
    asyncio.run(s.run_sse_async())


if __name__ == "__main__":
    app()
