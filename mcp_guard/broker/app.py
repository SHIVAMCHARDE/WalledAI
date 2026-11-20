"""Runs the popup service: decision engine + local HTTP API thread + popup UI on the main thread."""

from __future__ import annotations

import logging
import signal
import threading

from mcp_guard.broker.core import ApprovalBroker, AuditLog
from mcp_guard.broker.server import BrokerHTTPServer
from mcp_guard.broker.ui import TkPresenter
from mcp_guard.config import Settings, app_dir, load_or_create_token

log = logging.getLogger(__name__)


def run_broker(settings: Settings) -> int:
    """Run until stopped (Ctrl+C, ``mcp-guard stop`` or SIGTERM)."""
    token = load_or_create_token()
    broker = ApprovalBroker(settings, audit=AuditLog(app_dir() / "audit.jsonl"))
    presenter = TkPresenter(lambda: broker, settings.max_display_chars)
    broker.presenter = presenter

    try:
        httpd = BrokerHTTPServer((settings.broker_host, settings.broker_port), broker, token,
                                 on_shutdown=presenter.stop)
    except OSError as exc:
        log.error("cannot listen on %s:%s (%s) - is MCP Guard already running?",
                  settings.broker_host, settings.broker_port, exc)
        return 2

    threading.Thread(target=httpd.serve_forever, name="broker-http", daemon=True).start()
    log.info("MCP Guard popup service listening on %s (decision timeout %ss)",
             settings.broker_url, settings.decision_timeout)

    def _on_signal(signum, _frame):
        log.info("signal %s received, shutting down", signum)
        presenter.stop()

    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
        if sig is not None:
            try:
                signal.signal(sig, _on_signal)
            except (ValueError, OSError):
                pass

    try:
        presenter.run()
    finally:
        # Deny anything still waiting so no request is left hanging, then stop serving.
        broker.shutdown()
        httpd.shutdown()
        httpd.server_close()
        log.info("popup service stopped")
    return 0
