"""JSON-RPC / MCP message helpers shared by both proxies and the broker."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

# Application-defined JSON-RPC error codes (the -32000..-32099 range is reserved for servers).
ERR_BLOCKED = -32001
ERR_UPSTREAM = -32002

ALLOW = "allow"
DENY = "deny"


def is_request(msg: Any) -> bool:
    return isinstance(msg, dict) and "method" in msg and "id" in msg


def is_notification(msg: Any) -> bool:
    return isinstance(msg, dict) and "method" in msg and "id" not in msg


def describe_target(msg: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """Return (target, arguments) for a gated request.

    * ``tools/call``     -> (tool name, arguments)
    * ``resources/read`` -> (resource uri, {})
    * ``prompts/get``    -> (prompt name, arguments)
    """
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        return None, {"params": params}
    method = msg.get("method")
    if method == "resources/read":
        return _str_or_none(params.get("uri")), {}
    args = params.get("arguments")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        args = {"arguments": args}
    return _str_or_none(params.get("name")), args


def _str_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


def error_response(req_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def denial_response(msg: dict[str, Any], reason: str, style: str = "tool_error") -> dict[str, Any]:
    """Build the reply sent back to Claude when the user blocks a request."""
    text = f"Blocked by MCP Guard: {reason}"
    if msg.get("method") == "tools/call" and style == "tool_error":
        # Per the MCP spec, tool-level failures are reported inside the result so the
        # model can see them and tell the user, instead of as a protocol error.
        return {
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "result": {"content": [{"type": "text", "text": text}], "isError": True},
        }
    return error_response(msg.get("id"), ERR_BLOCKED, text, {"blockedBy": "mcp-guard", "reason": reason})


def dumps(msg: Any) -> str:
    """Serialise one message as a single line (stdio framing forbids embedded newlines)."""
    return json.dumps(msg, ensure_ascii=False, separators=(",", ":"))


@dataclass
class ApprovalRequest:
    """Everything the broker needs to show a popup for one pending request."""

    server: str
    method: str
    target: str | None
    arguments: dict[str, Any]
    transport: str  # "stdio" | "http"
    client: str = "unknown"  # e.g. "claude-ai 0.1.0" from the initialize handshake
    jsonrpc_id: Any = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ApprovalRequest":
        fields = cls.__dataclass_fields__  # type: ignore[attr-defined]
        missing = [f for f in ("server", "method", "transport") if f not in data]
        if missing:
            raise ValueError(f"missing fields: {missing}")
        clean = {k: v for k, v in data.items() if k in fields}
        if not isinstance(clean.get("arguments", {}), dict):
            raise ValueError("arguments must be an object")
        clean.setdefault("arguments", {})
        clean.setdefault("target", None)
        return cls(**clean)


@dataclass
class Decision:
    decision: str  # ALLOW | DENY
    reason: str = ""
    by: str = "user"  # "user" | "rule" | "remembered" | "timeout" | "shutdown" | "fail-mode"

    @property
    def allowed(self) -> bool:
        return self.decision == ALLOW

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
