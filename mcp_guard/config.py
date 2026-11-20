"""Settings, file locations and the shared broker auth token.

Everything lives in one per-user directory (``~/.mcp-guard``, i.e.
``C:\\Users\\<you>\\.mcp-guard`` on Windows, or ``$MCP_GUARD_HOME`` if set):

* ``config.json``  - optional user settings (see :class:`Settings`)
* ``token``        - random secret shared by the broker and the proxies
* ``audit.jsonl``  - one line per decision
* ``broker.log``   - broker log when it runs in the background
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


def app_dir() -> Path:
    override = os.environ.get("MCP_GUARD_HOME")
    # Deliberately NOT %APPDATA% on Windows: processes started from packaged (MSIX-style)
    # apps get AppData writes silently redirected to a private folder, which could split
    # the shared token between processes. The profile root is never redirected.
    path = Path(override) if override else Path.home() / ".mcp-guard"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class Rule:
    """A static policy rule. ``server`` and ``tool`` are fnmatch patterns."""

    action: str  # "allow" | "deny"
    server: str = "*"
    tool: str = "*"


@dataclass
class Settings:
    broker_host: str = "127.0.0.1"
    broker_port: int = 47823
    # Seconds a popup waits before the request is automatically blocked.
    decision_timeout: float = 120.0
    # What a proxy does when the broker can't be reached: "deny" (fail closed) or "allow".
    on_broker_unavailable: str = "deny"
    # Try to start the broker automatically when a proxy can't reach it.
    autostart_broker: bool = True
    # JSON-RPC methods that need approval. Everything else is logged and passed through.
    gated_methods: list[str] = field(default_factory=lambda: ["tools/call"])
    # Static rules evaluated before a popup is shown (first match wins).
    rules: list[Rule] = field(default_factory=list)
    # Max characters of the arguments shown in the popup.
    max_display_chars: int = 20000
    # How a blocked tools/call is answered: "tool_error" (a CallToolResult with
    # isError=true, which the model sees and can explain) or "jsonrpc_error".
    denial_style: str = "tool_error"

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or app_dir() / "config.json"
        if not path.exists():
            return cls()
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        data = {k: v for k, v in raw.items() if k in known}
        data["rules"] = [Rule(**r) for r in raw.get("rules", [])]
        settings = cls(**data)
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.on_broker_unavailable not in ("deny", "allow"):
            raise ValueError("on_broker_unavailable must be 'deny' or 'allow'")
        for rule in self.rules:
            if rule.action not in ("allow", "deny"):
                raise ValueError(f"rule action must be 'allow' or 'deny': {rule}")
        if self.denial_style not in ("tool_error", "jsonrpc_error"):
            raise ValueError("denial_style must be 'tool_error' or 'jsonrpc_error'")
        if self.decision_timeout <= 0:
            raise ValueError("decision_timeout must be positive")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def broker_url(self) -> str:
        return f"http://{self.broker_host}:{self.broker_port}"


def load_or_create_token() -> str:
    """Return the shared secret, creating it (owner-only permissions) on first use."""
    path = app_dir() / "token"
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    # O_EXCL avoids clobbering a token another process created a moment ago.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text(encoding="utf-8").strip()
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token)
    return token
