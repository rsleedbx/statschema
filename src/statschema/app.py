"""
statschema/app.py — HTTP MCP server for statschema.

Run locally:
    uvicorn src.statschema.app:app --host 0.0.0.0 --port 8765

MCP endpoint:  /mcp  (SSE transport, mounted as Starlette sub-app)
Health check:  GET /health
"""
from __future__ import annotations

try:
    from fastapi import FastAPI
    from mcp.server.fastmcp import FastMCP
except ImportError as e:
    raise ImportError("Install the mcp extra:  pip install 'statschema[mcp]'") from e

from .prompts import register_prompts
from .resources import register_resources
from .server import SERVICE_DESCRIPTION, SERVICE_NAME
from .tools import register_tools

_mcp = FastMCP(SERVICE_NAME, instructions=SERVICE_DESCRIPTION)
register_tools(_mcp)
register_resources(_mcp)
register_prompts(_mcp)

app = FastAPI(title=SERVICE_NAME, description=SERVICE_DESCRIPTION)


@app.get("/health")
async def health() -> dict:
    return {"service": SERVICE_NAME, "status": "ok"}


app.mount("/mcp", _mcp.sse_app())
