"""Claude Desktop local servers: a transparent stdio proxy.

Claude Desktop starts each *local* MCP server (from ``claude_desktop_config.json``) as a
child process and talks newline-delimited JSON-RPC over its stdin/stdout. Those calls
never leave the PC, so ``mcp-guard install`` points the config at this proxy, which
starts the real server behind it::

    Claude Desktop --stdin/stdout--> [ stdio_proxy ] --stdin/stdout--> real MCP server
                                            |
                                            +-- POST /v1/decide --> broker + popup

* Gated requests (``tools/call``) wait in their own thread for Allow / Block, so several
  can be pending while other traffic (pings, tools/list) keeps flowing.
* Allow -> the original line is written to the server. Block -> the proxy answers Claude
  itself and the server never sees the request.
* ``notifications/cancelled`` for a pending request closes its popup and drops it.
* If the server exits, waiting requests get an error. If Claude closes stdin, the server is stopped.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import threading
from typing import Any

from mcp_guard.broker_client import BrokerClient
from mcp_guard.config import Settings
from mcp_guard.protocol import (
    ERR_UPSTREAM,
    ApprovalRequest,
    denial_response,
    describe_target,
    dumps,
    error_response,
    is_notification,
    is_request,
)

log = logging.getLogger(__name__)


class StdioProxy:
    def __init__(self, server_name: str, command: list[str], settings: Settings, client: BrokerClient):
        self.server_name = server_name
        self.command = command
        self.settings = settings
        self.client = client
        self.stdin = sys.stdin.buffer
        self.stdout = sys.stdout.buffer
        self.client_label = "claude-desktop"
        self._out_lock = threading.Lock()  # writes to Claude
        self._srv_lock = threading.Lock()  # writes to the server
        self._state_lock = threading.Lock()
        self._awaiting: dict[str, str] = {}  # JSON-RPC id (json text) -> approval id
        self._cancelled: set[str] = set()
        self._server_alive = True
        self.proc: subprocess.Popen[bytes] | None = None

    def run(self) -> int:
        cmd = list(self.command)
        cmd[0] = shutil.which(cmd[0]) or cmd[0]  # finds npx.cmd etc. on Windows
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        log.info("starting MCP server '%s': %s", self.server_name, cmd)
        # stderr is inherited, so server logs still reach Claude Desktop's log files.
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0, creationflags=flags)
        threading.Thread(target=self.client.ensure_running, daemon=True).start()
        server_reader = threading.Thread(target=self._pump_server, daemon=True)
        server_reader.start()
        threading.Thread(target=self._pump_client, daemon=True).start()

        code = self.proc.wait()
        server_reader.join(timeout=2)
        self._server_alive = False
        self._fail_awaiting(f"MCP server '{self.server_name}' exited with code {code}")
        return code

    # -- pumps ----------------------------------------------------------------------------

    def _pump_server(self) -> None:
        assert self.proc and self.proc.stdout
        for line in iter(self.proc.stdout.readline, b""):
            self._write_client_raw(line if line.endswith(b"\n") else line + b"\n")

    def _pump_client(self) -> None:
        try:
            for line in iter(self.stdin.readline, b""):
                if line.strip():
                    try:
                        self._on_client_line(line)
                    except Exception:
                        log.exception("error handling client message; passing it through")
                        self._write_server_raw(line)
        finally:
            self._stop_server()

    def _stop_server(self) -> None:
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            assert proc.stdin is not None
            proc.stdin.close()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()

    # -- messages ---------------------------------------------------------------------------

    def _on_client_line(self, line: bytes) -> None:
        try:
            msg = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._write_server_raw(line)
            return
        if isinstance(msg, list):  # JSON-RPC batch: judge each element on its own
            passthrough = [m for m in msg if not self._intercept(m)]
            if passthrough:
                self._write_server_raw((dumps(passthrough) + "\n").encode())
        elif not self._intercept(msg):
            self._write_server_raw(line)

    def _intercept(self, msg: Any) -> bool:
        """Return True if the proxy took care of the message (gated / cancelled)."""
        if not isinstance(msg, dict):
            return False
        method = msg.get("method")
        if is_request(msg) and method == "initialize":
            info = (msg.get("params") or {}).get("clientInfo") or {}
            self.client_label = f"{info.get('name', 'claude-desktop')} {info.get('version', '')}".strip()
        if is_request(msg) and method in self.settings.gated_methods:
            self._start_gate(msg)
            return True
        if is_notification(msg) and method == "notifications/cancelled":
            return self._on_cancel(msg)
        if is_request(msg):
            target, _ = describe_target(msg)
            self.client.event({"server": self.server_name, "transport": "stdio", "method": method,
                               "target": target, "client": self.client_label})
        return False

    def _start_gate(self, msg: dict[str, Any]) -> None:
        target, args = describe_target(msg)
        req = ApprovalRequest(server=self.server_name, method=str(msg["method"]), target=target, arguments=args,
                              transport="stdio", client=self.client_label, jsonrpc_id=msg.get("id"))
        key = json.dumps(msg.get("id"))
        with self._state_lock:
            self._awaiting[key] = req.id
        threading.Thread(target=self._gate, args=(msg, req, key), daemon=True).start()

    def _gate(self, msg: dict[str, Any], req: ApprovalRequest, key: str) -> None:
        decision = self.client.decide(req)
        with self._state_lock:
            still_ours = self._awaiting.pop(key, None) == req.id
            cancelled = key in self._cancelled
            self._cancelled.discard(key)
        if cancelled or not still_ours:
            return  # cancelled requests get no response (MCP spec)
        if not decision.allowed:
            self._write_client(denial_response(msg, decision.reason, self.settings.denial_style))
        elif not self._server_alive or not self._write_server_raw((dumps(msg) + "\n").encode()):
            self._write_client(error_response(msg.get("id"), ERR_UPSTREAM, f"MCP server '{self.server_name}' is not running"))

    def _on_cancel(self, msg: dict[str, Any]) -> bool:
        key = json.dumps((msg.get("params") or {}).get("requestId"))
        with self._state_lock:
            approval_id = self._awaiting.get(key)
            if approval_id:
                self._cancelled.add(key)
        if approval_id:
            self.client.cancel(approval_id)  # closes the popup
            return True
        return False

    def _fail_awaiting(self, reason: str) -> None:
        with self._state_lock:
            waiting = dict(self._awaiting)
            self._awaiting.clear()
        for key, approval_id in waiting.items():
            self.client.cancel(approval_id)
            self._write_client(error_response(json.loads(key), ERR_UPSTREAM, reason))

    # -- writers ----------------------------------------------------------------------------

    def _write_client(self, msg: dict[str, Any]) -> None:
        self._write_client_raw((dumps(msg) + "\n").encode())

    def _write_client_raw(self, data: bytes) -> None:
        with self._out_lock:
            try:
                self.stdout.write(data)
                self.stdout.flush()
            except (OSError, ValueError):
                pass

    def _write_server_raw(self, data: bytes) -> bool:
        proc = self.proc
        if proc is None or proc.stdin is None:
            return False
        with self._srv_lock:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
                return True
            except (OSError, ValueError):
                return False
