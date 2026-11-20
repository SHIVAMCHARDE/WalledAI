"""The MCP Guard middleware: a Streamable-HTTP reverse proxy in front of an MCP server.

Claude calls remote MCP servers ("custom connectors") from Anthropic's cloud over HTTPS,
for both Claude Web and Claude Desktop. Nothing passes through your browser, so the
place to intercept is *in front of the server*. This proxy is that place::

    Claude ──HTTPS──> tunnel (cloudflared) ──> [ http_proxy ] ──> real MCP server (e.g. Kiwi)
                                                     │
                                                     └── POST /v1/decide ──> broker + popup

You add ``https://<tunnel-host>/mcp/<secret>`` as a custom connector in Claude
(Settings → Connectors → Add custom connector).

Behaviour (MCP Streamable HTTP transport, spec 2025-03-26 / 2025-06-18):

* ``POST`` bodies are parsed; any gated request (default ``tools/call``) waits for the
  user's decision. Blocked -> the proxy answers with the denial itself (HTTP 200,
  JSON-RPC body) and the upstream never sees it. Allowed -> forwarded unchanged.
* The response from upstream (``application/json`` or ``text/event-stream``) is streamed
  back chunk-by-chunk, so SSE progress and results still flow.
* ``GET`` (server->client SSE stream) and ``DELETE`` (session end) are passed through.
* If Claude disconnects while a popup is open, the popup is closed and nothing is sent upstream.
* ``extra_headers`` lets upstream credentials live in the proxy, so Claude never sees them.
* The listening path contains a random secret, because a tunnel makes the proxy public.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Any

from aiohttp import ClientSession, ClientTimeout, web

from mcp_guard.broker_client import BrokerClient
from mcp_guard.config import Settings
from mcp_guard.protocol import (
    ERR_UPSTREAM,
    ApprovalRequest,
    Decision,
    denial_response,
    describe_target,
    dumps,
    error_response,
    is_request,
)

log = logging.getLogger(__name__)

MAX_BODY = 10 * 1024 * 1024
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
    "transfer-encoding", "upgrade", "host", "content-length", "accept-encoding",
}
RESPONSE_SKIP = {"connection", "keep-alive", "transfer-encoding", "content-length", "content-encoding", "upgrade"}


class HttpProxy:
    def __init__(
        self,
        server_name: str,
        upstream: str,
        settings: Settings,
        client: BrokerClient,
        path_secret: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ):
        self.server_name = server_name
        self.upstream = upstream
        self.settings = settings
        self.client = client
        self.path_secret = path_secret if path_secret is not None else secrets.token_urlsafe(18)
        self.extra_headers = extra_headers or {}
        self.path = f"/mcp/{self.path_secret}" if self.path_secret else "/mcp"
        self._clients_by_session: dict[str, str] = {}
        self._session: ClientSession | None = None

    # -- app -------------------------------------------------------------------------------

    def build_app(self) -> web.Application:
        app = web.Application(client_max_size=MAX_BODY)
        app.router.add_route("POST", self.path, self.handle_post)
        app.router.add_route("GET", self.path, self.handle_passthrough)
        app.router.add_route("DELETE", self.path, self.handle_passthrough)
        app.router.add_route("GET", "/healthz", self.handle_health)
        app.on_startup.append(self._startup)
        app.on_cleanup.append(self._cleanup)
        return app

    async def _startup(self, _app: web.Application) -> None:
        # No total timeout: SSE responses may legitimately stay open for a long time.
        self._session = ClientSession(timeout=ClientTimeout(total=None, sock_connect=15), auto_decompress=True)
        await asyncio.to_thread(self.client.ensure_running)

    async def _cleanup(self, _app: web.Application) -> None:
        if self._session:
            await self._session.close()

    # -- handlers --------------------------------------------------------------------------

    async def handle_health(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "server": self.server_name})

    async def handle_post(self, request: web.Request) -> web.StreamResponse:
        raw = await request.read()
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return await self._forward(request, raw)  # let upstream produce the parse error

        messages = payload if isinstance(payload, list) else [payload]
        session_id = request.headers.get("Mcp-Session-Id", "")
        client_label = self._client_label(messages, session_id)

        gated = [m for m in messages if is_request(m) and m.get("method") in self.settings.gated_methods]
        gated_ids = {id(m) for m in gated}
        for m in messages:
            if is_request(m) and id(m) not in gated_ids:
                self._event(m, client_label)
        if not gated:
            return await self._forward(request, raw, client_label=client_label)

        # Several gated calls in one batch get their popups concurrently.
        decisions = await asyncio.gather(*(self._ask(request, m, client_label) for m in gated))
        if request.transport is None or request.transport.is_closing():
            raise web.HTTPServiceUnavailable(text="client went away")

        denials = [denial_response(m, d.reason, self.settings.denial_style) for m, d in zip(gated, decisions) if not d.allowed]
        if not denials:
            return await self._forward(request, raw, client_label=client_label)

        blocked_ids = {id(m) for m, d in zip(gated, decisions) if not d.allowed}
        remaining = [m for m in messages if id(m) not in blocked_ids]
        if not isinstance(payload, list):
            return web.json_response(denials[0])
        if not any(is_request(m) for m in remaining):
            if remaining:  # only notifications/responses left: deliver them, answer with the denials
                await self._forward(request, dumps(remaining).encode(), discard=True)
            return web.json_response(denials)
        return await self._forward(request, dumps(remaining).encode(), extra_messages=denials)

    async def handle_passthrough(self, request: web.Request) -> web.StreamResponse:
        return await self._forward(request, await request.read() if request.can_read_body else None)

    # -- approval ----------------------------------------------------------------------------

    async def _ask(self, request: web.Request, msg: dict[str, Any], client_label: str) -> Decision:
        target, args = describe_target(msg)
        req = ApprovalRequest(server=self.server_name, method=str(msg["method"]), target=target, arguments=args,
                              transport="http", client=client_label, jsonrpc_id=msg.get("id"))
        log.info("REQUEST  %s / %s  %s  (waiting for user)", self.server_name, target, json.dumps(args)[:200])
        task = asyncio.ensure_future(asyncio.to_thread(self.client.decide, req))
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=0.5)
                if done:
                    decision = task.result()
                    log.info("%-8s %s / %s  (%s: %s)", decision.decision.upper(), self.server_name, target,
                             decision.by, decision.reason)
                    return decision
                if request.transport is None or request.transport.is_closing():
                    log.info("client disconnected while %s was pending; cancelling", target)
                    self.client.cancel(req.id)
                    return Decision("deny", "client disconnected", by="cancelled")
        except asyncio.CancelledError:
            self.client.cancel(req.id)
            raise

    def _client_label(self, messages: list[Any], session_id: str) -> str:
        for m in messages:
            if is_request(m) and m.get("method") == "initialize":
                info = (m.get("params") or {}).get("clientInfo") or {}
                return f"{info.get('name', 'unknown')} {info.get('version', '')}".strip()
        return self._clients_by_session.get(session_id, "remote client")

    def _event(self, msg: dict[str, Any], client_label: str) -> None:
        target, _ = describe_target(msg)
        self.client.event({"server": self.server_name, "transport": "http", "method": msg.get("method"),
                           "target": target, "client": client_label})

    # -- forwarding --------------------------------------------------------------------------

    async def _forward(
        self,
        request: web.Request,
        body: bytes | None,
        client_label: str | None = None,
        extra_messages: list[dict[str, Any]] | None = None,
        discard: bool = False,
    ) -> web.StreamResponse:
        assert self._session is not None
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
        headers.update(self.extra_headers)
        try:
            upstream = await self._session.request(request.method, self.upstream, data=body, headers=headers,
                                                   params=request.query, allow_redirects=False)
        except Exception as exc:  # DNS, refused, TLS...
            log.warning("upstream request failed: %s", exc)
            if request.method == "POST":
                return web.json_response(error_response(None, ERR_UPSTREAM, f"upstream MCP server unreachable: {exc}"),
                                         status=502)
            raise web.HTTPBadGateway(text=str(exc))

        async with upstream:
            sid = upstream.headers.get("Mcp-Session-Id")
            if sid and client_label:
                self._clients_by_session[sid] = client_label
            if discard:
                await upstream.read()
                return web.Response(status=204)

            resp = web.StreamResponse(status=upstream.status, reason=upstream.reason)
            for k, v in upstream.headers.items():
                if k.lower() not in RESPONSE_SKIP:
                    resp.headers.add(k, v)
            ctype = upstream.headers.get("Content-Type", "")

            if extra_messages and ctype.startswith("application/json"):
                data = json.loads(await upstream.read() or b"[]")
                merged = (data if isinstance(data, list) else [data]) + extra_messages
                return web.json_response(merged, status=upstream.status,
                                         headers={k: v for k, v in resp.headers.items() if k.lower() != "content-type"})

            await resp.prepare(request)
            if extra_messages and ctype.startswith("text/event-stream"):
                for m in extra_messages:
                    await resp.write(f"event: message\ndata: {dumps(m)}\n\n".encode())
            try:
                async for chunk in upstream.content.iter_any():
                    await resp.write(chunk)
            except (ConnectionResetError, asyncio.CancelledError):
                log.info("stream closed by client")
                raise
            await resp.write_eof()
            return resp
