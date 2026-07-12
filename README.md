# MCP OAuth logging proxy (prototype)

A reverse proxy that sits between an MCP client (Claude Desktop, Kiro, etc.)
and a **third-party remote MCP server**. It transparently relays traffic,
writes a JSONL audit log of every JSON-RPC exchange, and rewrites OAuth
discovery metadata so the registration + token legs also pass through the
proxy (and get logged).

This is a prototype seed meant to be extended in Claude Code — use it to
watch exactly what an MCP client and a remote MCP server say to each other,
including the OAuth dance, without needing a packet sniffer or a debugger
inside the client.

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

The OAuth authorize leg opens in your browser straight to the IdP (not logged);
DCR and token exchange flow through the proxy and are logged. The proxy
rewrites the `Origin`/`Referer` headers to the upstream origin so servers that
do DNS-rebinding/Origin validation don't reject the relayed request. It does
**not** touch the OAuth `resource` value, so the token audience stays bound to
the real server and the IdP accepts it.

### Kiro

Kiro connects to remote servers from the local machine, so it can point
directly at the proxy in `.kiro/settings/mcp.json`:

```json
{ "mcpServers": { "target-via-proxy": { "url": "http://localhost:8080/" } } }
```

## How the OAuth interception works

The proxy rewrites discovery metadata so each leg routes back through it,
**except** the browser `/authorize` leg, which must go straight to the IdP.

```
client ── GET /              ─► proxy ─► MCP server        401 + WWW-Authenticate
       ◄─ resource_metadata rewritten to proxy ───────────┘
client ── GET PR metadata    ─► proxy   (authorization_servers rewritten)
client ── GET AS metadata    ─► proxy   (token/registration rewritten,
                                         authorization_endpoint left as-is)
client ── POST /register     ─► proxy ─► IdP    (DCR logged)
browser ─ GET /authorize     ─► IdP            (DIRECT — not logged)
client ── POST /token        ─► proxy ─► IdP    (code + tokens logged, masked)
client ── POST / (tools/call)─► proxy ─► MCP    (tool name + args logged)
```

Auth-server legs are proxied via `/_up/{host}/{path}`, so one proxy can reach
both the MCP host and its IdP. Only hosts the proxy itself has already handed
out through this rewriting are allowed through `/_up` — unknown hosts get a
403 (see [Known limitations](#known-limitations)).

## Log format

One JSON object per line, e.g. a `tools/call`:

```json
{"dir":"request","method":"POST","url":"https://mcp.example.com/mcp",
 "headers":{"authorization":"***MASKED***"},
 "jsonrpc":{"id":2,"method":"tools/call","tool":"search","arguments":{"q":"hi"}},
 "ts":1720000000.0}
```

Secrets (`access_token`, `refresh_token`, `client_secret`, auth `code`,
`code_verifier`, `Authorization` header) are masked in the log. Tool
`arguments` are logged in full — scrub these too if they may carry secrets.

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
- **The `/authorize` browser leg is never captured** (by design). The auth
  `code` is still visible on the token-exchange request.
- **No TLS on the proxy itself** — it listens on plain HTTP for localhost. If a
  client demands https for the MCP URL, terminate TLS in front (caddy/nginx) or
  add a self-signed cert.
- **No authentication on the proxy port** — anything that can reach it can
  relay traffic through it. Keep `--host 127.0.0.1` (the uvicorn default) and
  use SSH port forwarding rather than exposing it, if you need remote access.
- **`/_up/{host}` is allowlisted, not open** — it only relays to hosts the
  proxy itself already handed out via `to_proxy_url()` (i.e. hosts seen in
  protected-resource/AS metadata or a `WWW-Authenticate` challenge). Unknown
  hosts get a 403, so the debug port can't be used as a general-purpose relay
  to arbitrary internet hosts.
- **Single upstream** for the root path. Multi-server fan-out would need a
  routing table.
- **Log rotation / redaction policy** is not implemented.

## Ideas to build next

- Pretty live TUI instead of tailing JSONL
- Assertion mode: flag spec violations (missing `WWW-Authenticate`, PKCE not
  used, `resource` param absent on token request, etc.)
- Replay a captured session against the server
- Per-session correlation IDs linking request/response pairs
