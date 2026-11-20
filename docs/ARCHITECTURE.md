# Architecture

## Where the MCP traffic is

When you add a remote MCP server to Claude as a **custom connector**, Claude calls it from
**Anthropic's cloud** over **MCP Streamable HTTP** (JSON-RPC 2.0 in HTTPS POSTs, with
JSON or SSE responses). This is the same for **Claude Web** and **Claude Desktop**, because
connectors belong to the account. The requests never pass through your browser, so a browser
extension or local packet sniffer would see nothing.

The only place to intercept them is **in front of the MCP server**. MCP Guard is a reverse
proxy there: Claude is given MCP Guard's URL, and MCP Guard forwards to the real server.

```
Claude (Web / Desktop)
      │  HTTPS (MCP Streamable HTTP)
      ▼
cloudflared tunnel ──▶ http_proxy (MCP Guard middleware) ──HTTPS──▶ Kiwi Flights MCP server
                              │
                              │ POST /v1/decide   (127.0.0.1, token auth, long poll)
                              ▼
                       broker: core (decisions) + ui (popup) ──▶ audit.jsonl
```

### Claude Desktop local servers

Servers in `claude_desktop_config.json` are different. Claude Desktop starts each one as a
**child process on your PC** and talks JSON-RPC over its **stdin/stdout** (the MCP stdio
transport). That traffic never leaves the PC. `mcp-guard install` rewrites each config entry
so Claude starts `stdio_proxy.py`, which starts the real server and sits on the pipe between them:

```
Claude Desktop ──stdin/stdout──▶ stdio_proxy ──stdin/stdout──▶ local MCP server (e.g. Filesystem)
                                      │
                                      └── POST /v1/decide ──▶ the same broker + popup
```

Both proxies share one broker, so the user gets one popup style, one set of rules and one audit log.

## Components

| Module | Responsibility |
|---|---|
| `stdio_proxy.py` | Claude Desktop local servers: one thread per pending request, forwards or answers on the stdio pipe, closes the popup on `notifications/cancelled`, and reports an error if the server exits |
| `desktop_config.py` | Reversible install into `claude_desktop_config.json`: backup first, then an atomic write. The original command is kept after `--` |
| `http_proxy.py` | Parses each POST. Holds gated requests (`tools/call`) until decided. Forwards allowed ones unchanged and streams the response back. Answers blocked ones itself |
| `broker/core.py` | Decision order: static rules → remembered choice → popup. Plus the timeout, cancel and shutdown. All resolve to **deny** |
| `broker/ui.py` | Tkinter popups, always on top, one per request. Thread-safe through a queue drained by the Tk main loop |
| `broker/server.py` | Local HTTP API on `127.0.0.1` (`/v1/decide`, `/cancel`, `/status`, `/shutdown`) |
| `broker_client.py` | Used by the middleware: long-poll decide, auto-start the broker, fail-closed fallback |
| `start.py` | One command: broker + middleware + tunnel + connector URL |

The broker is a **separate process**, so the popup UI never blocks network traffic, a
crash in one side doesn't take down the other, and every request shares one UI and one audit log.

## Request lifecycle

1. Claude sends `{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"search-flight","arguments":{...}}}`.
2. The middleware sees that `tools/call` is gated and sends `POST /v1/decide` to the broker.
   Other messages (`initialize`, `tools/list`, notifications) pass straight through.
3. The broker checks rules and remembered choices. Otherwise it shows a popup and waits on a
   per-request `threading.Event`.
4. **Allow:** the original request is forwarded to the upstream, and its JSON or SSE response is streamed back.
5. **Block:** the middleware replies itself, and the upstream never sees the request:
   ```json
   {"jsonrpc":"2.0","id":7,"result":{"isError":true,
     "content":[{"type":"text","text":"Blocked by MCP Guard: blocked by user"}]}}
   ```
   A tool error inside the result is the MCP-recommended form, so the model reads it and tells the user.

## Concurrency

* **Middleware:** aiohttp handles each request as its own task, and the blocking decide call
  runs in a worker thread. Several tool calls (including the calls in one batch) are pending at the same time.
* **Broker:** `ThreadingHTTPServer` uses one thread per long poll. Each request has its own `Event`, and
  `_finish()` resolves exactly once under a lock, so a click racing the timeout gives exactly one decision.
* **UI:** one popup per request, cascaded, answerable in any order.

## Failure handling

| Situation | Behaviour |
|---|---|
| User doesn't answer | **deny** after `decision_timeout` (120 s); the popup closes |
| Popup service not running | the middleware auto-starts it and retries once, then **denies** |
| Wrong token (401) | deny, with a reason that says so |
| Claude disconnects while a popup is open | the popup closes, and nothing is sent upstream |
| Popup service stops (`stop`, Ctrl+C) | every pending request is denied first |
| Popup can't be shown | deny. A UI failure is never an implicit allow |
| Upstream MCP server down | HTTP 502 with a JSON-RPC error body |

## Security

* **Fail closed** by default everywhere.
* **Broker API only on 127.0.0.1**, with a 256-bit token from `~/.mcp-guard/token`, compared in
  constant time. Requests with an `Origin` header are rejected, which blocks web pages and DNS rebinding.
* **The public URL contains a random secret** (`/mcp/<secret>`), because the tunnel makes the
  middleware public. Every other path returns 404.
* **The popup resists accidental approval.** Allow is disabled for the first 700 ms, Enter does
  nothing, and Esc or closing the window means Block. Control and bidi characters are stripped from
  displayed names, and arguments are shown read-only.
* **Audit trail:** every decision is logged with the server, tool, arguments, who decided, and how long it waited.

## Limitations

* The tunnel URL changes on every restart. A fixed URL needs a named Cloudflare tunnel or hosting the middleware.
* OAuth-protected MCP servers aren't supported (the OAuth discovery endpoints aren't rewritten). No-login servers like Kiwi work.
* The legacy HTTP+SSE transport (2024-11-05) isn't proxied.
