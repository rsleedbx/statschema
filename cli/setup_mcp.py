#!/usr/bin/env python3
"""
Write the statschema MCP server entry into Cursor and Claude Desktop config files.

Default (SSE service) — one persistent process, survives Cursor restarts:
    python cli/setup_mcp.py
    # installs ~/Library/LaunchAgents/com.statschema.mcp-server.plist
    # loads the service (starts immediately)
    # writes URL-based config to both clients

stdio — Cursor/Claude spawn a subprocess per session:
    python cli/setup_mcp.py --stdio

Uninstall service + revert to stdio:
    python cli/setup_mcp.py --uninstall

Merges safely — existing mcpServers entries for other tools are preserved.
Run from anywhere; the repo root is detected from this file's location.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

import typer

app = typer.Typer(add_completion=False)

# ── Constants ─────────────────────────────────────────────────────────────────

_SERVER_NAME      = "statschema"
_LABEL            = "com.statschema.mcp-server"
_PLIST_PATH       = Path.home() / "Library" / "LaunchAgents" / f"{_LABEL}.plist"
_LOG_PATH         = Path.home() / "Library" / "Logs" / "statschema-mcp-server.log"
_DEFAULT_PORT_BASE = 8765
_DEFAULT_HOST     = "127.0.0.1"


def _find_free_port(start: int = _DEFAULT_PORT_BASE, host: str = _DEFAULT_HOST) -> int:
    """Return the first TCP port >= start that is not currently bound."""
    for port in range(start, start + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free port found in {start}–{start + 99}.")

_CURSOR_CONFIG = Path.home() / ".cursor" / "mcp.json"
_CLAUDE_CONFIG = (
    Path.home() / "Library" / "Application Support"
    / "Claude" / "claude_desktop_config.json"
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


# ── Entry builders ────────────────────────────────────────────────────────────

def _stdio_entry(repo: Path) -> dict:
    return {
        "command": sys.executable,
        "args":    [str(repo / "cli" / "run_mcp_server.py")],
        "env":     {},
    }


def _sse_entry(host: str, port: int) -> dict:
    return {"url": f"http://{host}:{port}/sse"}


# ── Client config helpers ─────────────────────────────────────────────────────

def _merge_config(path: Path, server_name: str, entry: dict) -> tuple[bool, str]:
    """Insert/update server_name in path's mcpServers. Returns (changed, action)."""
    if path.exists():
        try:
            config = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            return False, f"SKIP — could not parse existing file: {exc}"
    else:
        config = {}

    config.setdefault("mcpServers", {})
    existing = config["mcpServers"].get(server_name)
    config["mcpServers"][server_name] = entry

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n")

    if existing == entry:
        return False, "already up to date"
    if existing is None:
        return True, "added"
    return True, "updated"


def _write_client_configs(entry: dict) -> bool:
    targets = [("Cursor", _CURSOR_CONFIG), ("Claude Desktop", _CLAUDE_CONFIG)]
    any_changed = False
    for client, path in targets:
        changed, action = _merge_config(path, _SERVER_NAME, entry)
        status = "✓" if changed else "–"
        print(f"  {status}  {client:<18} {action}")
        print(f"       {path}")
        any_changed = any_changed or changed
    return any_changed


# ── LaunchAgent helpers ───────────────────────────────────────────────────────

def _plist_xml(repo: Path, host: str, port: int) -> str:
    sse_script = repo / "cli" / "run_mcp_server_sse.py"
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>{sys.executable}</string>
        <string>{sse_script}</string>
        <string>--host</string>
        <string>{host}</string>
        <string>--port</string>
        <string>{port}</string>
    </array>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>ThrottleInterval</key>
    <integer>2</integer>

    <key>StandardOutPath</key>
    <string>{_LOG_PATH}</string>

    <key>StandardErrorPath</key>
    <string>{_LOG_PATH}</string>

    <key>WorkingDirectory</key>
    <string>{repo}</string>
</dict>
</plist>
"""


def _launchctl(args: list[str]) -> bool:
    r = subprocess.run(["launchctl"] + args, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  [warn] launchctl {' '.join(args)}: {(r.stderr or r.stdout).strip()}")
    return r.returncode == 0


def _service_loaded() -> bool:
    r = subprocess.run(["launchctl", "list", _LABEL], capture_output=True, text=True)
    return r.returncode == 0


def _install_service(repo: Path, host: str, port: int) -> bool:
    """Write plist, unload stale copy if present, load fresh copy."""
    _PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PLIST_PATH.write_text(_plist_xml(repo, host, port))
    print(f"  ✓  plist written → {_PLIST_PATH}")

    if _service_loaded():
        _launchctl(["unload", str(_PLIST_PATH)])

    ok = _launchctl(["load", str(_PLIST_PATH)])
    if ok:
        print("  ✓  service loaded (auto-starts on login, restarts on crash)")
        print(f"     log → {_LOG_PATH}")
    else:
        print(f"  ✗  launchctl load failed — try: launchctl load {_PLIST_PATH}")
    return ok


def _uninstall_service() -> None:
    """Unload and remove the LaunchAgent plist."""
    if _service_loaded():
        _launchctl(["unload", str(_PLIST_PATH)])
        print("  ✓  service unloaded")
    else:
        print("  –  service was not loaded")

    if _PLIST_PATH.exists():
        _PLIST_PATH.unlink()
        print(f"  ✓  plist removed ({_PLIST_PATH})")
    else:
        print(f"  –  plist not found ({_PLIST_PATH})")


# ── CLI ───────────────────────────────────────────────────────────────────────

@app.command()
def main(
    stdio:     bool          = typer.Option(False, "--stdio",
                                            help="Write subprocess config instead of SSE service"),
    uninstall: bool          = typer.Option(False, "--uninstall",
                                            help="Remove SSE service + revert to stdio config"),
    port:      int | None    = typer.Option(None, "--port",
                                            help="SSE server port (default: auto-detect free port from 8765)"),
    host:      str           = typer.Option(_DEFAULT_HOST, "--host",
                                            help="SSE bind address"),
) -> None:
    """Configure Cursor and Claude Desktop to use the statschema MCP server (SSE by default)."""
    repo = _repo_root()

    # Auto-detect a free port if not explicitly provided
    if port is None:
        port = _find_free_port(start=_DEFAULT_PORT_BASE, host=host)
        print(f"==> port:   {port} (auto-detected)")
    else:
        print(f"==> port:   {port} (explicit)")

    print(f"==> repo:   {repo}")
    print(f"==> python: {sys.executable}")
    print()

    # ── uninstall ─────────────────────────────────────────────────────────────
    if uninstall:
        print("── Uninstall SSE service ──")
        _uninstall_service()
        print()
        print("── Revert client configs to stdio ──")
        entry = _stdio_entry(repo)
        _write_client_configs(entry)
        print()
        print("Restart Cursor (Cmd-Q) and Claude Desktop to reconnect via stdio.")
        raise typer.Exit(0)

    # ── stdio mode ────────────────────────────────────────────────────────────
    if stdio:
        stdio_script = repo / "cli" / "run_mcp_server.py"
        if not stdio_script.exists():
            print(f"ERROR: {stdio_script} not found", file=sys.stderr)
            raise typer.Exit(1)

        print(f"==> server: {stdio_script}")
        print()
        print("── Write stdio config to clients ──")
        entry = _stdio_entry(repo)
        _write_client_configs(entry)
        print()
        print("Next steps:")
        print("  • Cursor        — Cmd-Q and reopen, open Agent chat, check tools icon.")
        print("  • Claude Desktop — quit and relaunch, check tools icon.")
        raise typer.Exit(0)

    # ── SSE mode (default) ────────────────────────────────────────────────────
    sse_script = repo / "cli" / "run_mcp_server_sse.py"
    if not sse_script.exists():
        print(f"ERROR: {sse_script} not found", file=sys.stderr)
        raise typer.Exit(1)

    print(f"── Install SSE service  (port {port}) ──")
    ok = _install_service(repo, host, port)
    print()

    print("── Write SSE URL config to clients ──")
    entry = _sse_entry(host, port)
    _write_client_configs(entry)
    print()

    if ok:
        print("Next steps:")
        print(f"  • Server is running at http://{host}:{port}/sse")
        print(f"  • Check logs:  tail -f {_LOG_PATH}")
        print("  • Restart Cursor (Cmd-Q) so it picks up the new URL config.")
    raise typer.Exit(0 if ok else 1)


if __name__ == "__main__":
    app()
