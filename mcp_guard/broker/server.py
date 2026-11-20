"""Local HTTP API of the broker (bound to 127.0.0.1 only).

Endpoints (all JSON; every endpoint except /v1/health needs the ``X-MCP-Guard-Token`` header):

=====================  ==============================================================
GET  /v1/health        liveness probe, identifies the process as MCP Guard
POST /v1/decide        body: ApprovalRequest -> blocks until decided -> Decision
POST /v1/cancel        body: {"id": ...} - the client gave up on a pending request
POST /v1/event         body: any monitoring event (non-gated MCP traffic)
GET  /v1/status        pending requests, remembered choices, recent activity
POST /v1/shutdown      stop the broker (pending requests are denied)
=====================  ==============================================================

``/v1/decide`` is a long poll: the proxy's HTTP request stays open until the user
clicks. ``ThreadingHTTPServer`` gives each request its own thread, so concurrent
requests never wait on each other.
"""

from __future__ import annotations

import hmac
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from mcp_guard import __version__
from mcp_guard.broker.core import ApprovalBroker
from mcp_guard.protocol import ApprovalRequest

log = logging.getLogger(__name__)

MAX_BODY = 4 * 1024 * 1024
TOKEN_HEADER = "X-MCP-Guard-Token"


class BrokerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    # Refuse to share the port: a second broker must fail to bind, not steal traffic.
    allow_reuse_address = False

    def __init__(self, addr: tuple[str, int], broker: ApprovalBroker, token: str, on_shutdown: Callable[[], None]):
        super().__init__(addr, _Handler)
        self.broker = broker
        self.token = token
        self.on_shutdown = on_shutdown

    def server_bind(self) -> None:
        import socket
        import sys

        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class _Handler(BaseHTTPRequestHandler):
    server: BrokerHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # route to logging, not stderr
        log.debug("%s - %s", self.address_string(), fmt % args)

    # -- helpers --------------------------------------------------------------------------

    def _send(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _authorized(self) -> bool:
        # Browsers always send Origin on cross-site POSTs; a local proxy never does.
        # Rejecting it blocks drive-by requests from web pages (DNS rebinding etc.).
        if self.headers.get("Origin"):
            self._send(403, {"error": "browser requests are not allowed"})
            return False
        supplied = self.headers.get(TOKEN_HEADER, "")
        if not hmac.compare_digest(supplied.encode(), self.server.token.encode()):
            self._send(401, {"error": "bad or missing token"})
            return False
        return True

    def _json_body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            raise ValueError("missing or oversized body")
        return json.loads(self.rfile.read(length))

    # -- routes ---------------------------------------------------------------------------

    def do_GET(self) -> None:
        if self.path == "/v1/health":
            self._send(200, {"ok": True, "app": "mcp-guard", "version": __version__})
        elif self.path == "/v1/status":
            if self._authorized():
                self._send(200, self.server.broker.snapshot())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._authorized():
            return
        try:
            body = self._json_body()
        except (ValueError, json.JSONDecodeError) as exc:
            self._send(400, {"error": f"bad request: {exc}"})
            return

        if self.path == "/v1/decide":
            try:
                req = ApprovalRequest.from_dict(body)
            except (TypeError, ValueError) as exc:
                self._send(400, {"error": f"bad approval request: {exc}"})
                return
            decision = self.server.broker.decide(req)
            self._send(200, decision.to_dict())
        elif self.path == "/v1/cancel":
            found = self.server.broker.cancel(str(body.get("id", "")))
            self._send(200, {"cancelled": found})
        elif self.path == "/v1/event":
            if isinstance(body, dict):
                self.server.broker.record_event(body)
            self._send(202, {"ok": True})
        elif self.path == "/v1/shutdown":
            self._send(200, {"ok": True})
            threading.Thread(target=self.server.on_shutdown, daemon=True).start()
        else:
            self._send(404, {"error": "not found"})
