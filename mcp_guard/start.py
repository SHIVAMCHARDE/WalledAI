"""``mcp-guard start``: everything in one command.

1. makes sure the popup service (broker) is running,
2. runs the MCP Guard HTTP middleware in front of the MCP server (Kiwi Flights by default),
3. opens a public HTTPS tunnel (cloudflared) so Claude can reach it,
4. prints the URL to add as a custom connector in Claude (works in Claude Web and Claude Desktop).
"""

from __future__ import annotations

import asyncio
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from aiohttp import web

from mcp_guard.broker_client import BrokerClient
from mcp_guard.config import Settings, app_dir
from mcp_guard.http_proxy import HttpProxy

DEFAULT_UPSTREAM = "https://mcp.kiwi.com"
DEFAULT_NAME = "kiwi-flights"
TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def _say(msg: str = "") -> None:
    print(msg, flush=True)


def _box(lines: list[str]) -> None:
    width = max(len(l) for l in lines) + 4
    _say("+" + "-" * width + "+")
    for l in lines:
        _say("|  " + l.ljust(width - 2) + "|")
    _say("+" + "-" * width + "+")


def _pick_port(preferred: int) -> int:
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", preferred)) != 0:
            return preferred
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _secret() -> str:
    """Same secret every run, so only the tunnel host part of the URL changes."""
    path = app_dir() / "web_secret"
    if path.exists() and path.read_text().strip():
        return path.read_text().strip()
    value = secrets.token_urlsafe(12)
    path.write_text(value)
    return value


def _find_cloudflared() -> str | None:
    found = shutil.which("cloudflared")
    if found:
        return found
    for p in (r"C:\Program Files (x86)\cloudflared\cloudflared.exe", r"C:\Program Files\cloudflared\cloudflared.exe"):
        if Path(p).exists():
            return p
    return None


def _copy_to_clipboard(text: str) -> bool:
    try:
        if sys.platform == "win32":
            subprocess.run("clip", input=text.encode(), check=True, shell=True)
        elif sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=text.encode(), check=True)
        else:
            return False
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def _open_tunnel(port: int) -> tuple[subprocess.Popen | None, str | None, bool]:
    """Start a cloudflared quick tunnel; return (process, public https host, reachable yet?)."""
    exe = _find_cloudflared()
    if not exe:
        return None, None, False
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    proc = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, creationflags=flags)
    found: list[str] = []

    def reader() -> None:  # keep draining so cloudflared never blocks on a full pipe
        assert proc.stdout
        for line in proc.stdout:
            m = TUNNEL_RE.search(line)
            if m and not found:
                found.append(m.group(0))

    threading.Thread(target=reader, daemon=True).start()
    deadline = time.time() + 60
    while time.time() < deadline and not found and proc.poll() is None:
        time.sleep(0.2)
    host = found[0] if found else None
    reachable = False
    if host:  # wait until the new hostname actually answers (DNS can take a moment)
        for _ in range(60):
            try:
                with urllib.request.urlopen(f"{host}/healthz", timeout=3):
                    reachable = True
                    break
            except OSError:
                time.sleep(1)
    return proc, host, reachable


def run_start(settings: Settings, upstream: str, name: str, use_tunnel: bool, port: int = 8765) -> int:
    client = BrokerClient(settings)
    _say("[1/3] Starting the popup service ...")
    if not client.ensure_running(wait=15):
        _say(f"      could not start it, see {app_dir() / 'broker.log'}")
        return 1

    proxy = HttpProxy(name, upstream, settings, client, path_secret=_secret())
    port = _pick_port(port)
    tunnel: list[subprocess.Popen] = []

    async def main() -> None:
        runner = web.AppRunner(proxy.build_app())
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        local_url = f"http://127.0.0.1:{port}{proxy.path}"
        _say(f"[2/3] MCP Guard is guarding '{name}' ({upstream})")
        try:
            if not use_tunnel:
                _box(["Running locally (no tunnel):", local_url, "Press Ctrl+C to stop."])
            else:
                _say("[3/3] Opening a public tunnel (takes ~10 seconds) ...")
                proc, host, reachable = await asyncio.to_thread(_open_tunnel, port)
                if proc:
                    tunnel.append(proc)
                if not host:
                    _say("      No tunnel: install cloudflared (winget install --id Cloudflare.cloudflared)")
                    _say(f"      or check your internet. Local URL: {local_url}")
                else:
                    url = f"{host}{proxy.path}"
                    copied = _copy_to_clipboard(url)
                    _say()
                    _box([
                        "READY!  Add this URL as a custom connector in Claude:",
                        "",
                        url,
                        "",
                        "(copied to clipboard)" if copied else "",
                        "" if reachable else "(tunnel still warming up - if Claude can't connect, wait 1 min and retry)",
                        "Claude > Settings > Connectors > Add custom connector",
                        "Works in Claude Web and Claude Desktop.",
                        "Try: \"Find flights from BOM to GOI next Friday\"",
                        "",
                        "Keep this window open.  Press Ctrl+C to stop.",
                    ])
            while True:
                await asyncio.sleep(3600)
        finally:
            await runner.cleanup()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        _say("Stopping ...")
        for p in tunnel:
            p.terminate()
    return 0
