"""Client used by the proxies to talk to the broker, including auto-start and fail-mode."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from mcp_guard.config import Settings, app_dir, load_or_create_token
from mcp_guard.protocol import ALLOW, DENY, ApprovalRequest, Decision

log = logging.getLogger(__name__)

TOKEN_HEADER = "X-MCP-Guard-Token"


class BrokerClient:
    def __init__(self, settings: Settings, token: str | None = None):
        self.settings = settings
        self.token = token or load_or_create_token()
        self.base = settings.broker_url
        self._start_lock = threading.Lock()
        self._last_start_attempt = 0.0
        # The broker's own long-poll timeout plus slack, so we never give up first.
        self.decide_timeout = settings.decision_timeout + 15

    # -- low level ------------------------------------------------------------------------

    def _call(self, method: str, path: str, body: Any = None, timeout: float = 5.0) -> Any:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header(TOKEN_HEADER, self.token)
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed local URL
            return json.loads(resp.read() or b"null")

    def healthy(self) -> bool:
        try:
            info = self._call("GET", "/v1/health", timeout=1.5)
            return bool(info and info.get("app") == "mcp-guard")
        except (OSError, ValueError):
            return False

    # -- high level -----------------------------------------------------------------------

    def ensure_running(self, wait: float = 8.0) -> bool:
        """Return True if a broker answers, starting one in the background if allowed."""
        if self.healthy():
            return True
        if not self.settings.autostart_broker:
            return False
        with self._start_lock:
            if self.healthy():
                return True
            # Don't hammer process creation if the broker keeps failing to start.
            if time.time() - self._last_start_attempt > 10:
                self._last_start_attempt = time.time()
                spawn_background_broker()
            deadline = time.time() + wait
            while time.time() < deadline:
                if self.healthy():
                    return True
                time.sleep(0.25)
        return False

    def decide(self, req: ApprovalRequest) -> Decision:
        """Ask the user (via the broker). Never raises: failures map to the configured fail mode."""
        for attempt in (1, 2):
            try:
                result = self._call("POST", "/v1/decide", req.to_dict(), timeout=self.decide_timeout)
                decision = Decision(**{k: result[k] for k in ("decision", "reason", "by") if k in result})
                if decision.decision not in (ALLOW, DENY):
                    raise ValueError(f"unexpected decision {decision.decision!r}")
                return decision
            except urllib.error.HTTPError as exc:
                log.error("broker rejected request: HTTP %s", exc.code)
                if exc.code == 401:
                    return self._fail("broker rejected the auth token (token file mismatch?)")
                return self._fail(f"broker error HTTP {exc.code}")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                log.warning("broker unreachable (attempt %s): %s", attempt, exc)
                if attempt == 1 and self.ensure_running():
                    continue
                return self._fail(f"approval service unavailable ({exc.__class__.__name__})")
        return self._fail("approval service unavailable")

    def cancel(self, request_id: str) -> None:
        try:
            self._call("POST", "/v1/cancel", {"id": request_id}, timeout=2)
        except (OSError, ValueError):
            pass

    def event(self, event: dict[str, Any]) -> None:
        """Fire-and-forget monitoring event; never blocks the MCP traffic."""

        def send() -> None:
            try:
                self._call("POST", "/v1/event", event, timeout=2)
            except (OSError, ValueError):
                pass

        threading.Thread(target=send, daemon=True).start()

    def status(self) -> Any:
        return self._call("GET", "/v1/status")

    def shutdown(self) -> Any:
        return self._call("POST", "/v1/shutdown", {})

    def _fail(self, reason: str) -> Decision:
        mode = self.settings.on_broker_unavailable
        return Decision(mode, reason if mode == DENY else f"{reason}; allowed by fail-open setting", by="fail-mode")


def spawn_background_broker() -> None:
    """Start ``mcp_guard broker`` detached from the current process (no console window)."""
    exe = Path(sys.executable)
    if sys.platform == "win32":
        pythonw = exe.with_name("pythonw.exe")
        if pythonw.exists():
            exe = pythonw
    launcher = Path(__file__).resolve().parent.parent / "run_mcp_guard.py"
    cmd = [str(exe), str(launcher), "broker", "--log-file", str(app_dir() / "broker.log")]
    kwargs: dict[str, Any] = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  close_fds=True, cwd=str(app_dir()))
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        # Claude Desktop may run MCP servers in a job object that is killed with the app;
        # try to break away so the broker (and its popups) outlive one server restart.
        try:
            subprocess.Popen(cmd, creationflags=flags | 0x01000000, **kwargs)  # CREATE_BREAKAWAY_FROM_JOB
            return
        except OSError:
            subprocess.Popen(cmd, creationflags=flags, **kwargs)
            return
    subprocess.Popen(cmd, start_new_session=True, **kwargs)
    log.info("started background broker: %s", " ".join(cmd))
