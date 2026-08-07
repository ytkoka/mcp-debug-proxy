# MCP debug proxy 
[日本語](README.ja.md)

A reverse proxy that sits between an MCP client (Claude Desktop, Kiro, etc.)
and a **third-party remote MCP server**. It transparently relays traffic,
writes a JSONL audit log of every JSON-RPC exchange, and rewrites OAuth
discovery metadata so the registration + token legs also pass through the
proxy (and get logged).

Use it to watch exactly what an MCP client and a remote MCP server say to each other,
including the OAuth dance, without needing a packet sniffer or a debugger
inside the client.

This is **not** a transport-conversion proxy (stdio↔HTTP bridges like
`mcp-remote` already do that well) — its only job is to make MCP + OAuth
traffic observable by logging it, unmodified in substance, as it passes
through.

## Requirements

- Python 3.9+
- macOS or Linux (tested on macOS)

## Installation

```bash
git clone <this-repo-url>
cd mcp-debug-proxy

python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate

pip install -r requirements.txt
```

## Configuration

The proxy is configured entirely through environment variables — there is no
config file to edit.

| Variable       | Default                        | Meaning                                                        |
|----------------|---------------------------------|------------------------------------------------------------------|
| `UPSTREAM`     | `https://mcp.example.com/mcp`  | The remote MCP server you want to watch (its full endpoint URL). |
| `PROXY_PUBLIC` | `http://localhost:8080`        | The base URL clients use to reach this proxy. Only change this if you're not running on `localhost:8080` (e.g. behind an SSH tunnel on a different port). |
| `LOG_PATH`     | `mcp_proxy.jsonl`               | Where the JSONL audit log is written.                            |
| `ALLOWED_AUTH_HOSTS` | *(unset)*                | Comma-separated IdP hosts to allow through `/_up/{host}` in addition to the ones OAuth discovery hands out at runtime. Useful so a client that skips discovery after a proxy restart (e.g. reusing a cached refresh token) doesn't get a spurious 403. `UPSTREAM`'s own host is always allowed. |
| `HISTORY_SIZE` | `500`                     | How many past exchange-level records `/events` backfills to a UI that connects late (oldest first). `stream_chunk` records never count against this. |
| `EVENTS_QUEUE_MAXSIZE` | `512`             | Per-subscriber queue size for the live `/events` feed. A slow/stalled UI tab drops its own oldest queued records rather than blocking the proxy. |
| `EVENTS_STATS_INTERVAL` | `15`             | Seconds between `/events` heartbeat events (also carries the live drop counter) sent to an idle SSE connection. |
| `OPEN_UI`      | *(unset, off)*                  | Set to `1`/`true`/`yes` to automatically open `/ui` in a browser window on startup. Off by default — see [Live debug UI](#live-debug-ui). |

## Running

```bash
source venv/bin/activate
UPSTREAM=https://mcp.example.com/mcp uvicorn proxy:app --port 8080
```

Then point the MCP client at `http://localhost:8080/`.

The `UPSTREAM` may be **https** — the proxy connects to it over TLS via
httpx. Only the proxy's own listener is plain http on localhost, which is
what the local bridge below expects. By default uvicorn binds to
`127.0.0.1` only; there's no authentication on the proxy's own port, so
don't pass `--host 0.0.0.0` unless you know what you're doing (see
[Known limitations](#known-limitations)).

Run with a single worker (the default — don't pass `--workers N`). The
`/_up/{host}` allowlist lives in process memory, so multiple worker
processes wouldn't share it and requests could hit a worker that never saw
the discovery traffic that unlocked a given host.

Tail the log while you work:

```bash
tail -f mcp_proxy.jsonl | python3 -m json.tool --json-lines
```

## Wiring Claude Desktop (primary target)

Claude Desktop **custom connectors connect from Anthropic's cloud, not from
your machine**, so a `http://localhost` connector URL cannot work and your
local proxy can't sit in that path. Use the `mcp-remote` stdio bridge, which
runs locally, handles OAuth itself (opens the browser, does DCR + token
exchange), and makes its HTTP calls through the proxy:

```
Claude Desktop ─stdio─► mcp-remote (local) ─HTTP─► proxy ─https─► MCP + IdP
```

`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "target-via-proxy": {
      "command": "npx",
      "args": ["mcp-remote", "http://localhost:8080/", "--transport", "http-only"]
    }
  }
}
```

The OAuth authorize leg opens in your browser, bounces once through the proxy
(logged) to fix up the `resource` param, then goes straight to the IdP for
the actual login (not logged) — see [How the OAuth interception
works](#how-the-oauth-interception-works). DCR and token exchange flow
through the proxy and are logged. The proxy
rewrites the `Origin`/`Referer` headers to the upstream origin so servers that
do DNS-rebinding/Origin validation don't reject the relayed request. The
client-facing OAuth `resource` value is the proxy's own URL (required for
clients that validate it against the URL they're connected to), but it's
patched back to the real server's URL on every request that actually reaches
the IdP, so the token audience stays bound to the real server and the IdP
accepts it — see the `resource` section below for why this needs two faces.

Verified end-to-end against a real remote MCP server + IdP (AWS's MCP
Server, `aws-mcp.us-east-1.api.aws`) — RFC 8414/9728 discovery, DCR, browser
authorization, and token exchange all worked through the proxy.

### Kiro

Kiro connects to remote servers from the local machine, so it can point
directly at the proxy in `.kiro/settings/mcp.json`:

```json
{ "mcpServers": { "target-via-proxy": { "url": "http://localhost:8080/" } } }
```

## How the OAuth interception works

The proxy rewrites discovery metadata so each leg routes back through it.
The browser `/authorize` leg still ends up talking to the IdP directly for
the actual login UI — we only bounce it through a one-shot redirect first
(see below), we don't proxy the page content.

```
client ── GET /              ─► proxy ─► MCP server        401 + WWW-Authenticate
       ◄─ resource_metadata rewritten to proxy ───────────┘
client ── GET PR metadata    ─► proxy   (authorization_servers + resource rewritten)
client ── GET AS metadata    ─► proxy   (token/registration/authorization_endpoint
                                         all rewritten)
client ── POST /register     ─► proxy ─► IdP    (DCR logged)
browser ─ GET /_authorize    ─► proxy   (302, `resource` patched back to real value)
browser ─ GET /authorize     ─► IdP            (DIRECT from here — not logged)
client ── POST /token        ─► proxy ─► IdP    (`resource` patched back; code +
                                                  tokens logged, masked)
client ── POST / (tools/call)─► proxy ─► MCP    (tool name + args logged)
```

Auth-server legs are proxied via `/_up/{host}/{path}`, so one proxy can reach
both the MCP host and its IdP. Only hosts the proxy itself has already handed
out through this rewriting are allowed through `/_up` — unknown hosts get a
403 (see [Known limitations](#known-limitations)). `handle_root` also
recognizes `/.well-known/xxx/_up/{host}/{rest}` — the shape an MCP client
gets when it applies RFC 8414 path-insertion to one of our `/_up/{host}`
issuer URLs — and routes it to the real `{host}` with the well-known prefix
re-inserted ahead of its real path, instead of treating it as a lookup
against UPSTREAM's own origin.

**`resource` gets two faces.** MCP clients (e.g. `mcp-remote`) validate the
protected-resource metadata's `resource` value against the URL they're
actually connected to — which is us, not the real server — so
`rewrite_metadata()` rewrites `resource` to `PROXY_PUBLIC` for the client.
But the token the IdP issues still needs to be bound to the *real* server or
it won't be accepted for actual API calls, so on the way to the real
`/token` request, `relay()` patches the `resource` form field back to the
real value it captured earlier. The comparison ignores a trailing `/`, since
clients don't necessarily echo the value back byte-for-byte (`mcp-remote`
was observed adding one). Token/DCR request form fields are now also
logged (masked same as JSON bodies).

The same client-facing `resource` value also ends up in the query string the
browser sends to `authorization_endpoint` — and some IdPs validate it there
too (confirmed against AWS's real endpoint: the real `resource` gets a 302,
the proxy's own URL or a missing `resource` gets a 400). Since that leg goes
straight from the browser to the IdP and never touches the proxy, there's no
request to patch — so `rewrite_metadata()` instead points
`authorization_endpoint` at `{PROXY_PUBLIC}/_authorize`, a one-shot 302
redirect-bounce (`handle_authorize()`) that patches `resource` and sends the
browser straight on to the real IdP. The actual login UI is still rendered
by the real IdP to the browser directly, not relayed through us.

## Live debug UI

Open `http://localhost:8080/ui` while the proxy is running to watch
exchanges as they happen (Charles/Fiddler-style), or set `OPEN_UI=1` to have
the proxy open it for you in a browser window on startup: a live list of
request/response pairs (method, path, status, duration, tool name, OAuth-leg
badges), a detail pane on click, and SSE tool responses growing in place as
chunks arrive rather than only appearing once the stream closes. It's fed by
`GET /events` (`text/event-stream`), which backfills recent history on
connect (see `HISTORY_SIZE` above) and then streams live. Delivery to
`/events` is best-effort: a slow or disconnected UI tab can only drop its
own queued records (see `EVENTS_QUEUE_MAXSIZE`), never affect the proxy
itself or other subscribers. Both `/ui` and `/events` are bound to
`127.0.0.1` exactly like the rest of the proxy -- see [Known
limitations](#known-limitations).

The detail pane shows the **real response body** (pretty-printed when it's
JSON, e.g. a `tools/list` response's actual tool definitions) alongside
**response headers** (`content-type`, `mcp-session-id`, etc.) — both are
masked the same way request headers/bodies already are, so an OAuth token
response's `access_token` shows as `***MASKED***` there too, never the raw
value. Browser housekeeping requests for `/favicon.ico` are answered
directly by the proxy (204, not relayed upstream) so they don't clutter the
exchange list.

![Live debug UI showing OAuth discovery and tools/call exchanges against a real MCP server, with a selected exchange's request/response headers and body in the detail pane](docs/live-ui.png)

## Log format

One JSON object per line: a request record, followed by its paired response
record (same `exchange_id`), e.g. a `tools/list` call:

```json
{"dir":"request","kind":"request","exchange_id":7,"method":"POST",
 "url":"https://mcp.example.com/mcp",
 "headers":{"authorization":"***MASKED***"},
 "jsonrpc":{"id":2,"method":"tools/list"},
 "ts":1720000000.0}
{"dir":"response","kind":"response","exchange_id":7,"status":200,
 "content_type":"application/json",
 "headers":{"mcp-session-id":"abc123"},
 "body":{"id":2,"method":null},
 "body_text":"{\"jsonrpc\": \"2.0\", \"id\": 2, \"result\": {\"tools\": [...]}}",
 "body_text_truncated":false,
 "duration_ms":42.1, "ts":1720000000.1}
```

`body` is a minimal JSON-RPC summary (id/method/tool name/error only — for a
*response*, which doesn't echo back a `method`, this is often just
`{"id": N, "method": null}`). `body_text` is the actual response body —
JSON-parsed and masked when possible, capped at 20,000 bytes with a
`body_text_truncated` flag, otherwise raw text — and is what the [live
UI](#live-debug-ui)'s Response pane renders. Either way, the client's real
response is never touched by these caps; only what's written to the log /
sent to `/events` is.

Secrets (`access_token`, `refresh_token`, `client_secret`, auth `code`,
`code_verifier`, `Authorization` header) are masked in the log — in both
`headers` and anywhere they appear inside `jsonrpc`/`form`/`body`/`body_text`.
Tool `arguments` are logged in full — scrub these too if they may carry
secrets. The same unmasked `arguments` are what the live `/ui`/`/events`
feed shows, so anyone who can open that port sees them too — see [Live
debug UI](#live-debug-ui). Log files (`*.jsonl`) are gitignored by default
so they don't end up in the repo by accident.

## Origin / Referer

Rewritten to the upstream origin on every relayed request (see
`rewrite_origin`), since the client believes the proxy is the server. Only
values the client actually sent are replaced.

## Known limitations

- **Buffered vs streamed split** is by `Content-Type`: `text/event-stream` is
  streamed and tee'd; everything else is fully buffered. MCP Streamable HTTP
  can return either — fine here, but revisit if you see large non-SSE bodies.
- **Streamed (SSE) response bodies are logged up to 20,000 bytes**; the full
  body is still relayed to the client untouched, only the JSONL log entry is
  capped, so long-lived MCP sessions don't grow the proxy's memory unbounded.
- **Redirects** are not followed (`follow_redirects=False`) so the client sees
  them verbatim. If the upstream 3xx's to another host you may need to rewrite
  `Location` too.
- **The `/authorize` browser leg's login UI is never captured** (by design —
  only the one-shot redirect-bounce at `/_authorize` is logged, not the
  actual IdP login page or its cookies/CSP). The auth `code` is still
  visible on the token-exchange request.
- **No TLS on the proxy itself** — it listens on plain HTTP for localhost. If a
  client demands https for the MCP URL, terminate TLS in front (caddy/nginx) or
  add a self-signed cert.
- **No authentication on the proxy port** — anything that can reach it can
  relay traffic through it. Keep `--host 127.0.0.1` (the uvicorn default) and
  use SSH port forwarding rather than exposing it, if you need remote access.
  This includes `/ui` and `/events`: opening the port means anyone who can
  reach it can watch the full, unmasked traffic of every MCP session going
  through the proxy in real time (tool `arguments` in particular — see [Log
  format](#log-format)), not just replay the log file after the fact.
- **`/_up/{host}` is allowlisted, not open** — it only relays to hosts the
  proxy itself already handed out via `to_proxy_url()` (i.e. hosts seen in
  protected-resource/AS metadata or a `WWW-Authenticate` challenge), plus
  `UPSTREAM`'s own host and anything in `ALLOWED_AUTH_HOSTS`. Unknown hosts
  get a 403, so the debug port can't be used as a general-purpose relay to
  arbitrary internet hosts. The allowlist is in-process state — see the
  single-worker note under [Running](#running).
- **Single upstream** for the root path. Multi-server fan-out would need a
  routing table.
- **Log rotation / redaction policy** is not implemented.

## Troubleshooting: 502 through the proxy, but the upstream itself is fine

If `curl`ing `UPSTREAM` directly works but every request through the proxy
comes back `502 upstream error: ...`, check the `kind: "proxy_error"` record
the failed request produced (`tail -f mcp_proxy.jsonl` or watch `/ui`) — it
carries the original httpx exception's class name, which narrows down the
cause:

- **`RemoteProtocolError`** (or the connection just hangs/fails outright on
  an upstream that answers `curl --http2` fine) — usually an **HTTP/2-only
  upstream** (common behind CloudFront and similar edge proxies). The proxy
  negotiates HTTP/2 via ALPN automatically (`http2=True` on the httpx
  client, falling back to HTTP/1.1 when the upstream doesn't offer it) — if
  you're still seeing this, confirm the `h2` package from
  `requirements.txt` is actually installed (`pip show h2`).
- **`ConnectError` / `ConnectTimeout`** — `UPSTREAM` is unreachable from
  where the proxy runs (DNS, firewall, wrong host/port) — not a proxy bug.
- **`ReadTimeout`** — the upstream accepted the connection but never
  responded — check the upstream's own health, not the proxy.

## Testing

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

Tests spin up a stub MCP/IdP server and drive the proxy against it (some via
real localhost sockets, some via an in-process ASGI transport) — no network
access or real MCP server is needed. Tests marked `integration` (needing
real external network access) are excluded by default via `pytest.ini`; none
exist yet, but the marker is there for e.g. real HTTP/2-upstream checks that
would otherwise make CI flaky.

## License

MIT — see [LICENSE](LICENSE).
