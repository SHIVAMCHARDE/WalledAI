"""Command-line entry point: ``python run_mcp_guard.py <command>``."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from mcp_guard import __version__
from mcp_guard.config import Settings, app_dir


def _setup_logging(verbose: bool, log_file: str | None = None) -> None:
    handler: logging.Handler = logging.FileHandler(log_file, encoding="utf-8") if log_file else logging.StreamHandler()
    fmt = "%(asctime)s [mcp-guard %(name)s] %(levelname)s %(message)s" if log_file else "%(asctime)s  %(message)s"
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=[handler],
                        format=fmt, datefmt="%H:%M:%S" if not log_file else None)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    from mcp_guard.start import DEFAULT_NAME, DEFAULT_UPSTREAM

    p = argparse.ArgumentParser(prog="mcp-guard", description="Approve or block every MCP tool call Claude makes.")
    p.add_argument("--version", action="version", version=f"mcp-guard {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start", help="run MCP Guard in front of the MCP server and print the Claude connector URL")
    s.add_argument("--upstream", default=DEFAULT_UPSTREAM, help=f"MCP server URL to guard (default: {DEFAULT_UPSTREAM})")
    s.add_argument("--name", default=DEFAULT_NAME, help=f"server name shown in the popup (default: {DEFAULT_NAME})")
    s.add_argument("--port", type=int, default=8765, help="local port (default: 8765)")
    s.add_argument("--no-tunnel", action="store_true", help="local only, no public URL")

    b = sub.add_parser("broker", help="run only the popup service (start does this for you)")
    b.add_argument("--log-file")

    i = sub.add_parser("install", help="Claude Desktop: guard the local servers in claude_desktop_config.json")
    i.add_argument("--sandbox", metavar="FOLDER",
                   help="also add the official Filesystem MCP server, limited to FOLDER")
    sub.add_parser("uninstall", help="Claude Desktop: restore the original server commands")

    px = sub.add_parser("proxy", help="(used by Claude Desktop) stdio proxy in front of a local server")
    px.add_argument("--name", required=True)
    px.add_argument("command", nargs=argparse.REMAINDER, help="-- <server command> [args]")

    sub.add_parser("status", help="show pending requests and recent decisions")
    sub.add_parser("stop", help="stop the popup service (pending requests are blocked)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose, getattr(args, "log_file", None))
    try:
        settings = Settings.load()
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"invalid config.json in {app_dir()}: {exc}", file=sys.stderr)
        return 2

    if args.cmd == "start":
        from mcp_guard.start import run_start

        return run_start(settings, args.upstream, args.name, use_tunnel=not args.no_tunnel, port=args.port)

    if args.cmd == "broker":
        from mcp_guard.broker.app import run_broker

        return run_broker(settings)

    if args.cmd == "proxy":
        return _run_proxy(args, settings)

    if args.cmd in ("install", "uninstall"):
        from mcp_guard import desktop_config as dc

        add = None
        if getattr(args, "sandbox", None):
            folder = Path(args.sandbox).expanduser().resolve()
            folder.mkdir(parents=True, exist_ok=True)
            add = {"filesystem-sandbox": {"command": "npx",
                                          "args": ["-y", "@modelcontextprotocol/server-filesystem", str(folder)]}}
        for path in dc.config_paths():
            changed = dc.install(path, add) if args.cmd == "install" else dc.uninstall(path)
            print(f"{path}\n  {'guarded' if args.cmd == 'install' else 'restored'}: {changed or 'nothing to change'}")
        print("Quit Claude Desktop from the tray icon and open it again.")
        return 0

    from mcp_guard.broker_client import BrokerClient

    client = BrokerClient(settings)
    if not client.healthy():
        print(f"popup service is not running on {settings.broker_url}")
        return 1 if args.cmd == "status" else 0
    if args.cmd == "status":
        print(json.dumps(client.status(), indent=2))
    else:
        client.shutdown()
        print("popup service stopping")
    return 0


def _run_proxy(args: argparse.Namespace, settings: Settings) -> int:
    from mcp_guard.broker_client import BrokerClient
    from mcp_guard.stdio_proxy import StdioProxy

    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        print("usage: mcp-guard proxy --name NAME -- <command> [args]", file=sys.stderr)
        return 2
    try:
        code = StdioProxy(args.name, command, settings, BrokerClient(settings)).run()
    except FileNotFoundError as exc:
        logging.getLogger("proxy").error("cannot start MCP server: %s", exc)
        code = 127
    # A reader thread may still be blocked on stdin; exiting normally can crash on Windows.
    logging.shutdown()
    try:
        sys.stdout.flush()
    except (OSError, ValueError):
        pass
    os._exit(code & 0xFF if code >= 0 else 1)
