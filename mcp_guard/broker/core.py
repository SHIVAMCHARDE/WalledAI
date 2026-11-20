"""The approval broker: the single place where allow/block decisions are made.

It is UI-agnostic. Proxies submit an :class:`ApprovalRequest` (via the HTTP API in
``server.py``) and block in :meth:`ApprovalBroker.decide` until one of these happens:

1. a static rule from ``config.json`` matches              -> decided immediately
2. the user earlier ticked "remember" for this tool        -> decided immediately
3. the user clicks Allow / Block in the popup              -> :meth:`resolve`
4. the requesting client cancels the request               -> :meth:`cancel` (deny)
5. the decision timeout expires                            -> deny
6. the broker shuts down                                   -> deny

Every request gets its own ``threading.Event``, so any number of requests can be
pending at the same time and are resolved independently.
"""

from __future__ import annotations

import collections
import fnmatch
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from mcp_guard.config import Settings
from mcp_guard.protocol import ALLOW, DENY, ApprovalRequest, Decision

log = logging.getLogger(__name__)


class Presenter(Protocol):
    """Something that can show a pending request to the user (the Tk popup UI)."""

    def show(self, pending: "Pending") -> None: ...

    def dismiss(self, request_id: str, reason: str) -> None: ...


@dataclass
class Pending:
    request: ApprovalRequest
    deadline: float
    event: threading.Event = field(default_factory=threading.Event)
    decision: Decision | None = None


class AuditLog:
    """Append-only JSON-lines log of every decision and monitored message."""

    def __init__(self, path: Path | None):
        self.path = path
        self._lock = threading.Lock()

    def write(self, record: dict[str, Any]) -> None:
        if self.path is None:
            return
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **record}
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            try:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                log.exception("could not write audit log")


class ApprovalBroker:
    def __init__(self, settings: Settings, presenter: Presenter | None = None, audit: AuditLog | None = None):
        self.settings = settings
        self.presenter = presenter
        self.audit = audit or AuditLog(None)
        self._lock = threading.Lock()
        self._pending: dict[str, Pending] = {}
        # (server, method, target) -> "allow" | "deny", set by the "remember" checkbox.
        self._remembered: dict[tuple[str, str, str | None], str] = {}
        self._recent: collections.deque[dict[str, Any]] = collections.deque(maxlen=200)
        self._closed = False

    # -- called by the HTTP API (one thread per request) ---------------------------------

    def decide(self, req: ApprovalRequest, timeout: float | None = None) -> Decision:
        decision = self._decide_without_user(req)
        if decision is None:
            decision = self._ask_user(req, timeout or self.settings.decision_timeout)
        self._record(req, decision)
        return decision

    def record_event(self, event: dict[str, Any]) -> None:
        """Log a monitored (non-gated) MCP message such as tools/list or initialize."""
        entry = {"type": "event", **event}
        with self._lock:
            self._recent.append(entry)
        self.audit.write(entry)

    def cancel(self, request_id: str, reason: str = "request cancelled by client") -> bool:
        return self._finish(request_id, Decision(DENY, reason, by="cancelled"))

    # -- called by the UI ---------------------------------------------------------------

    def resolve(self, request_id: str, allow: bool, remember: bool = False) -> bool:
        with self._lock:
            pending = self._pending.get(request_id)
            if pending and remember:
                r = pending.request
                self._remembered[(r.server, r.method, r.target)] = ALLOW if allow else DENY
        decision = Decision(ALLOW, "approved by user", "user") if allow else Decision(DENY, "blocked by user", "user")
        return self._finish(request_id, decision, dismiss=False)

    # -- lifecycle / introspection --------------------------------------------------------

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            ids = list(self._pending)
        for rid in ids:
            self._finish(rid, Decision(DENY, "MCP Guard is shutting down", by="shutdown"))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "pending": [p.request.to_dict() for p in self._pending.values()],
                "remembered": [
                    {"server": s, "method": m, "target": t, "action": a} for (s, m, t), a in self._remembered.items()
                ],
                "recent": list(self._recent)[-50:],
            }

    # -- internals ------------------------------------------------------------------------

    def _decide_without_user(self, req: ApprovalRequest) -> Decision | None:
        for rule in self.settings.rules:
            if fnmatch.fnmatchcase(req.server, rule.server) and fnmatch.fnmatchcase(req.target or "", rule.tool):
                return Decision(rule.action, f"matched rule server={rule.server!r} tool={rule.tool!r}", by="rule")
        with self._lock:
            remembered = self._remembered.get((req.server, req.method, req.target))
            closed = self._closed
        if closed:
            return Decision(DENY, "MCP Guard is shutting down", by="shutdown")
        if remembered:
            return Decision(remembered, "remembered choice for this tool", by="remembered")
        if self.presenter is None:
            return Decision(DENY, "no UI available to ask the user", by="fail-mode")
        return None

    def _ask_user(self, req: ApprovalRequest, timeout: float) -> Decision:
        pending = Pending(req, deadline=time.time() + timeout)
        with self._lock:
            self._pending[req.id] = pending
        try:
            assert self.presenter is not None
            self.presenter.show(pending)
        except Exception:  # a broken UI must never turn into an implicit allow
            log.exception("presenter failed")
            self._finish(req.id, Decision(DENY, "could not display confirmation popup", by="fail-mode"))
        if not pending.event.wait(timeout):
            self._finish(req.id, Decision(DENY, f"no decision within {int(timeout)}s", by="timeout"))
        pending.event.wait()  # _finish always sets it; this just orders the read below
        assert pending.decision is not None
        return pending.decision

    def _finish(self, request_id: str, decision: Decision, dismiss: bool = True) -> bool:
        """Resolve a pending request exactly once. Returns False if it was already resolved."""
        with self._lock:
            pending = self._pending.pop(request_id, None)
        if pending is None:
            return False
        pending.decision = decision
        pending.event.set()
        if dismiss and self.presenter is not None:
            try:
                self.presenter.dismiss(request_id, decision.reason)
            except Exception:
                log.exception("presenter dismiss failed")
        return True

    def _record(self, req: ApprovalRequest, decision: Decision) -> None:
        entry = {
            "type": "decision",
            "request_id": req.id,
            "server": req.server,
            "method": req.method,
            "target": req.target,
            "transport": req.transport,
            "client": req.client,
            "decision": decision.decision,
            "by": decision.by,
            "reason": decision.reason,
            "waited_s": round(time.time() - req.created_at, 3),
        }
        with self._lock:
            self._recent.append(entry)
        self.audit.write({**entry, "arguments": req.arguments})
        log.info("%s %s/%s -> %s (%s)", req.transport, req.server, req.target, decision.decision, decision.by)
