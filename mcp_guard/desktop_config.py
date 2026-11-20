"""Wrap / unwrap the local servers in Claude Desktop's ``claude_desktop_config.json``.

Install turns ``{"command": "npx", "args": ["-y", "some-server"]}`` into
``{"command": "<python>", "args": ["<repo>/run_mcp_guard.py", "proxy", "--name", "<name>", "--", "npx", "-y", "some-server"]}``.
The original command is kept verbatim after ``--``, so uninstall is an exact inverse.
A timestamped backup is written before every change.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

LAUNCHER = Path(__file__).resolve().parent.parent / "run_mcp_guard.py"


def config_paths() -> list[Path]:
    """Existing Claude Desktop config files (or the default location if none exists)."""
    home = Path.home()
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        roaming = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        candidates = [d / "claude_desktop_config.json" for d in sorted((local / "Claude-Data").glob("claude_*"))]
        candidates.append(roaming / "Claude" / "claude_desktop_config.json")
        candidates += [p / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json"
                       for p in sorted((local / "Packages").glob("Claude_*"))]
    elif sys.platform == "darwin":
        candidates = [home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"]
    else:
        candidates = [home / ".config" / "Claude" / "claude_desktop_config.json"]
    return [p for p in candidates if p.exists()] or candidates[:1]


def is_wrapped(entry: dict[str, Any]) -> bool:
    args = [str(a) for a in entry.get("args") or []]
    return "proxy" in args and "--" in args and any("run_mcp_guard" in a for a in args[:1])


def wrap(name: str, entry: dict[str, Any]) -> dict[str, Any]:
    new = dict(entry)
    new["command"] = sys.executable
    new["args"] = [str(LAUNCHER), "proxy", "--name", name, "--", entry["command"], *(entry.get("args") or [])]
    return new


def unwrap(entry: dict[str, Any]) -> dict[str, Any]:
    args = list(entry["args"])
    original = args[args.index("--") + 1:]
    new = dict(entry)
    new["command"] = original[0]
    if len(original) > 1:
        new["args"] = original[1:]
    else:
        new.pop("args", None)
    return new


def _load(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8").strip() if path.exists() else ""
    return json.loads(text) if text else {}


def _save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(f"{path.name}.mcp-guard-backup-{time.strftime('%Y%m%d-%H%M%S')}"))
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)  # atomic: Claude never sees a half-written file


def install(path: Path, add: dict[str, dict[str, Any]] | None = None) -> list[str]:
    """Wrap every local (command-based) server; optionally add new servers first."""
    data = _load(path)
    servers = data.setdefault("mcpServers", {})
    servers.update(add or {})
    changed = [n for n, e in servers.items() if isinstance(e, dict) and "command" in e and not is_wrapped(e)]
    for name in changed:
        servers[name] = wrap(name, servers[name])
    if changed:
        _save(path, data)
    return changed


def uninstall(path: Path) -> list[str]:
    data = _load(path)
    servers = data.get("mcpServers", {})
    changed = [n for n, e in servers.items() if isinstance(e, dict) and is_wrapped(e)]
    for name in changed:
        servers[name] = unwrap(servers[name])
    if changed:
        _save(path, data)
    return changed
