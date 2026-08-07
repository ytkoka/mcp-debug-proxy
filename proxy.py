"""
MCP OAuth-aware logging reverse proxy (prototype).

Sits between an MCP client (e.g. Claude Desktop) and a third-party remote MCP
server. Transparently relays HTTP, logs every JSON-RPC exchange as JSONL, and
rewrites OAuth discovery metadata so the DCR + token-exchange legs also pass
through the proxy (and thus get logged).

What is captured:
  - MCP requests/responses (incl. tools/call name + arguments)
  - 401 challenge (WWW-Authenticate: resource_metadata=...)
  - protected-resource metadata, authorization-server metadata
  - dynamic client registration (/register)
  - the one-shot /_authorize redirect-bounce (resource patched, then handed
    off to the real IdP)
  - token exchange + refresh (/token)

What is NOT captured (by design):
  - the actual IdP login UI (browser -> IdP, direct, after the bounce above).
    The auth `code` is still visible on the subsequent token-exchange request.

Run:
    UPSTREAM=https://mcp.example.com/mcp uvicorn proxy:app --port 8080
Then point the MCP client at:  http://localhost:8080/
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from broker import Broker

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
UPSTREAM = os.environ.get("UPSTREAM", "https://mcp.example.com/mcp").rstrip("/")
PROXY_PUBLIC = os.environ.get("PROXY_PUBLIC", "http://localhost:8080").rstrip("/")
LOG_PATH = os.environ.get("LOG_PATH", "mcp_proxy.jsonl")
_UI_HTML_PATH = Path(__file__).resolve().parent / "static" / "ui.html"

# Cap on what a single response record (buffered JSON body or the
# accumulated SSE stream_end summary) may contain in the JSONL log / live
# feed. The client still gets the full, untruncated body -- only what
# feeds log()/broker.publish() is capped, so a huge response can't grow the
# log file or a subscriber's queue unbounded.
MAX_STREAM_LOG_BYTES = 20000

# Live subscriber fan-out (e.g. the /events SSE endpoint) for a debug UI.
# publish() is non-blocking by construction (see broker.py) -- log() calling
# it here never adds an `await` to relay()'s hot path. HISTORY_SIZE bounds
# how many past exchange-level records (never stream_chunk noise -- see
# broker.py) a UI that connects late gets backfilled with.
HISTORY_SIZE = int(os.environ.get("HISTORY_SIZE", "500"))
broker = Broker(
    queue_maxsize=int(os.environ.get("EVENTS_QUEUE_MAXSIZE", "512")),
    history_size=HISTORY_SIZE,
)

# How often (seconds) an idle /events connection gets a synthetic "stats"
# event -- doubles as a keep-alive and as the carrier for the live-UI drop
# counter (see handle_events()).
EVENTS_STATS_INTERVAL = float(os.environ.get("EVENTS_STATS_INTERVAL", "15"))

# Hop-by-hop headers must not be forwarded (RFC 7230 6.1).
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
    # let httpx/starlette recompute these:
    "content-length", "content-encoding", "accept-encoding",
}

# JSON keys whose values are secrets and should be masked in the log only.
SECRET_KEYS = {
    "access_token", "refresh_token", "id_token", "client_secret",
    "code", "authorization_code", "code_verifier",
}

_client: httpx.AsyncClient  # set in lifespan

# Monotonic id pairing a relay() call's request/response (and, later, its
# SSE stream_chunk) log records together. Safe without a lock: incremented
# synchronously (no `await` in between), and everything runs on one asyncio
# event loop, so there's no interleaving that could race two callers.
_exchange_ids = itertools.count(1)

# Hosts we've pointed the client at via to_proxy_url() (auth-server /_up
# legs). /_up/{host}/... must only proxy to hosts we ourselves handed out --
# otherwise it's an open relay to any host for anyone who can reach the port.
#
# This set lives in process memory, so it starts empty on every restart and
# is per-worker if run with `--workers > 1`. Seed it with UPSTREAM's own host
# (covers the case where the resource server is also its own AS) and with
# ALLOWED_AUTH_HOSTS (comma-separated) for IdP hosts you already know about,
# so a client that skips discovery after a restart (e.g. a cached refresh
# token) doesn't get a spurious 403.
_known_auth_hosts: set[str] = {urlsplit(UPSTREAM).netloc}
for _host in os.environ.get("ALLOWED_AUTH_HOSTS", "").split(","):
    _host = _host.strip()
    if _host:
        _known_auth_hosts.add(_host)
del _host

# The real `resource` identifier as reported by the upstream's own
# protected-resource metadata. MCP clients validate `resource` against the
# URL they're actually connected to (us), so rewrite_metadata() rewrites it
# to PROXY_PUBLIC -- but the token the IdP issues still needs to be bound to
# the *real* server, so relay() substitutes this real value back into the
# `resource` form field on the way to the real /token request.
_real_resource: str | None = None

# The real authorization_endpoint, captured the same way. The browser hits
# this directly (never through us), so it also carries the client-facing
# `resource` value in its query string -- and some IdPs (AWS's included)
# reject that with a 400 before the user ever sees a login screen. See
# handle_authorize() for the fix.
_real_authorization_endpoint: str | None = None


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def log(record: dict) -> None:
    record["ts"] = time.time()
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    broker.publish(record)


def mask(obj):
    """Recursively mask secret values so logs are safe to keep/share."""
    if isinstance(obj, dict):
        return {
            k: ("***MASKED***" if k.lower() in SECRET_KEYS else mask(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [mask(v) for v in obj]
    return obj


def mask_headers(headers) -> dict:
    out = {}
    for k, v in headers.items():
        out[k] = "***MASKED***" if k.lower() == "authorization" else v
    return out


def summarize_jsonrpc(body: bytes) -> dict | None:
    """Pull out the interesting bits of a JSON-RPC message for the log."""
    try:
        msg = json.loads(body)
    except Exception:
        return None
    if not isinstance(msg, dict):
        return None
    summary = {"id": msg.get("id"), "method": msg.get("method")}
    if msg.get("method") == "tools/call":
        params = msg.get("params", {}) or {}
        summary["tool"] = params.get("name")
        summary["arguments"] = params.get("arguments")
    if "error" in msg:
        summary["error"] = msg["error"]
    return summary


# ---------------------------------------------------------------------------
# URL rewriting for OAuth discovery
# ---------------------------------------------------------------------------
def to_proxy_url(absolute_url: str) -> str:
    """https://auth.host/path?q  ->  {PROXY_PUBLIC}/_up/auth.host/path?q"""
    parts = urlsplit(absolute_url)
    _known_auth_hosts.add(parts.netloc)
    query = f"?{parts.query}" if parts.query else ""
    return f"{PROXY_PUBLIC}/_up/{parts.netloc}{parts.path}{query}"


def from_proxy_path(host: str, rest: str, query: str) -> str:
    """Reverse of to_proxy_url, used by the /_up/{host}/{rest} route."""
    path = "/" + rest if not rest.startswith("/") else rest
    return urlunsplit(("https", host, path, query, ""))


def rewrite_metadata(doc: dict) -> dict:
    """Rewrite endpoints we want to intercept."""
    for key in ("token_endpoint", "registration_endpoint", "userinfo_endpoint",
                "introspection_endpoint", "revocation_endpoint"):
        if isinstance(doc.get(key), str):
            doc[key] = to_proxy_url(doc[key])
    # protected-resource metadata lists the auth servers by issuer URL; route
    # those through the proxy so AS-metadata + DCR + token all get logged.
    if isinstance(doc.get("authorization_servers"), list):
        doc["authorization_servers"] = [
            to_proxy_url(u) if isinstance(u, str) else u
            for u in doc["authorization_servers"]
        ]
    if isinstance(doc.get("resource"), str):
        # MCP clients validate `resource` against the URL they're actually
        # connected to (us), so it must say PROXY_PUBLIC here. patch_resource()
        # substitutes the real value back in on the real /token request so
        # the IdP still binds the issued token to the real server.
        global _real_resource
        _real_resource = doc["resource"]
        doc["resource"] = PROXY_PUBLIC
    if isinstance(doc.get("authorization_endpoint"), str):
        # The browser hits this directly -- we don't proxy the interactive
        # login UI itself (cookies/CSP break across the origin change) -- but
        # its query string carries the same client-facing `resource` value,
        # which some IdPs reject outright. Point the client at a redirect-
        # bounce on the proxy instead: handle_authorize() patches `resource`
        # and 302s straight to the real endpoint, then gets out of the way.
        global _real_authorization_endpoint
        _real_authorization_endpoint = doc["authorization_endpoint"]
        doc["authorization_endpoint"] = PROXY_PUBLIC + "/_authorize"
    return doc


def patch_resource_param(body: bytes) -> bytes:
    """Undo the client-facing `resource` rewrite on an outgoing form body
    (the real /token request) so the IdP still binds the token to the real
    server's audience instead of the proxy's."""
    pairs = parse_qsl(body.decode("utf-8"), keep_blank_values=True)
    changed = False
    patched = []
    for k, v in pairs:
        # Compare origins with trailing slashes normalized away -- clients
        # don't necessarily echo `resource` back byte-for-byte from what the
        # metadata said (e.g. adding a trailing "/"), and an un-patched
        # `resource` here means the real IdP rejects the token request.
        if k == "resource" and v.rstrip("/") == PROXY_PUBLIC.rstrip("/"):
            v = _real_resource
            changed = True
        patched.append((k, v))
    return urlencode(patched).encode("utf-8") if changed else body


def summarize_form(body: bytes) -> dict | None:
    """Pull out form fields (token/DCR requests) for the log; secrets among
    them get masked same as JSON-RPC bodies."""
    try:
        return dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))
    except Exception:
        return None


def rewrite_www_authenticate(value: str) -> str:
    """Rewrite resource_metadata=\"https://...\" to point at the proxy."""
    marker = 'resource_metadata="'
    i = value.find(marker)
    if i == -1:
        return value
    start = i + len(marker)
    end = value.find('"', start)
    if end == -1:
        return value
    original = value[start:end]
    return value[:start] + to_proxy_url(original) + value[end:]


# ---------------------------------------------------------------------------
# Core relay
# ---------------------------------------------------------------------------
def clean_request_headers(request: Request) -> dict:
    return {
        k: v for k, v in request.headers.items()
        if k.lower() not in HOP_BY_HOP
    }


def rewrite_origin(headers: dict, upstream_url: str) -> dict:
    """The client thinks the proxy IS the MCP server, so it sends an Origin/
    Referer pointing at the proxy. MCP servers that do DNS-rebinding / Origin
    validation will reject that mismatch. Rewrite to the upstream's origin.
    We only replace values that already exist -- never add ones the client
    chose to omit."""
    parts = urlsplit(upstream_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    for key in list(headers.keys()):
        low = key.lower()
        if low == "origin" and headers[key] and headers[key] != "null":
            # "null" is the literal value browsers send for opaque origins
            # (sandboxed iframes, etc.) -- it's not a mismatch to fix, so
            # leave it alone rather than replacing it with a real origin.
            headers[key] = origin
        elif low == "referer" and headers[key]:
            headers[key] = origin + "/"
    return headers


def clean_response_headers(resp: httpx.Response) -> list[tuple[str, str]]:
    out = []
    for k, v in resp.headers.items():
        if k.lower() in HOP_BY_HOP:
            continue
        if k.lower() == "www-authenticate":
            v = rewrite_www_authenticate(v)
        out.append((k, v))
    return out


async def relay(request: Request, upstream_url: str) -> Response:
    exchange_id = next(_exchange_ids)
    t0 = time.time()
    body = await request.body()
    req_headers = clean_request_headers(request)
    req_headers = rewrite_origin(req_headers, upstream_url)

    form_fields = None
    ctype_in = request.headers.get("content-type", "")
    if body and "application/x-www-form-urlencoded" in ctype_in:
        if _real_resource:
            body = patch_resource_param(body)
        form_fields = summarize_form(body)

    # Log the outbound request.
    log({
        "dir": "request",
        "kind": "request",
        "exchange_id": exchange_id,
        "started": t0,
        "method": request.method,
        "url": upstream_url,
        "headers": mask_headers(req_headers),
        "jsonrpc": mask(summarize_jsonrpc(body)) if body else None,
        "form": mask(form_fields) if form_fields else None,
    })

    upstream_req = _client.build_request(
        request.method, upstream_url,
        headers=req_headers,
        params=dict(request.query_params),
        content=body or None,
    )
    try:
        upstream = await _client.send(upstream_req, stream=True)
    except httpx.HTTPError as exc:
        # Surface the original exception type (e.g. RemoteProtocolError from
        # an HTTP/2-only upstream httpx can't speak to) before it's flattened
        # into a generic 502 -- otherwise nothing in the log/live UI
        # distinguishes "unreachable" from "wrong protocol" from any other
        # httpx.HTTPError subclass.
        ended = time.time()
        log({
            "dir": "response",
            "kind": "proxy_error",
            "exchange_id": exchange_id,
            "ended": ended,
            "duration_ms": round((ended - t0) * 1000, 1),
            "url": upstream_url,
            "error": f"{type(exc).__name__}: {exc}",
        })
        return Response(f"upstream error: {exc}", status_code=502)

    ctype = upstream.headers.get("content-type", "")
    resp_headers = clean_response_headers(upstream)

    # --- streaming (SSE) path: never buffer, tee chunks to the log ----------
    if "text/event-stream" in ctype:
        # Cap applied to each individually-published stream_chunk record
        # (never to what's actually relayed to the client). Separate from
        # MAX_STREAM_LOG_BYTES, which caps the cumulative buffer written to
        # the JSONL file as the stream_end summary.
        MAX_CHUNK_PUBLISH_BYTES = 8000

        async def body_iter():
            # Cap what we hold for the log entry -- the stream itself may run
            # for the life of an MCP session, so buf must not grow unbounded.
            buf = bytearray()
            seq = 0
            try:
                async for chunk in upstream.aiter_raw():
                    if len(buf) < MAX_STREAM_LOG_BYTES:
                        buf.extend(chunk[: MAX_STREAM_LOG_BYTES - len(buf)])
                    # Live fan-out only -- never written to the JSONL file,
                    # so a long-lived stream can't grow the log file
                    # unbounded or evict other exchanges from history (T5).
                    seq += 1
                    broker.publish({
                        "kind": "stream_chunk",
                        "dir": "response",
                        "exchange_id": exchange_id,
                        "url": upstream_url,
                        "seq": seq,
                        "data": chunk[:MAX_CHUNK_PUBLISH_BYTES].decode("utf-8", "replace"),
                        "truncated": len(chunk) > MAX_CHUNK_PUBLISH_BYTES,
                        "ts": time.time(),
                    }, history=False)
                    yield chunk
            finally:
                await upstream.aclose()
                ended = time.time()
                log({
                    "dir": "response",
                    "kind": "stream_end",
                    "exchange_id": exchange_id,
                    "ended": ended,
                    "duration_ms": round((ended - t0) * 1000, 1),
                    "url": upstream_url,
                    "status": upstream.status_code,
                    "stream": True,
                    "truncated": len(buf) >= MAX_STREAM_LOG_BYTES,
                    "raw": buf.decode("utf-8", "replace"),
                })
        return StreamingResponse(
            body_iter(),
            status_code=upstream.status_code,
            headers=dict(resp_headers),
        )

    # --- buffered path: read fully, maybe rewrite OAuth metadata ------------
    raw = await upstream.aread()
    await upstream.aclose()

    logged_body = None
    if "application/json" in ctype and raw:
        try:
            doc = json.loads(raw)
        except Exception:
            doc = None
        if isinstance(doc, dict) and any(
            k in doc for k in ("authorization_servers", "token_endpoint",
                               "registration_endpoint", "authorization_endpoint")
        ):
            doc = rewrite_metadata(doc)
            raw = json.dumps(doc).encode("utf-8")
            logged_body = doc
        else:
            logged_body = mask(summarize_jsonrpc(raw)) or doc

    # Cap what feeds the log/live-UI, never what's returned to the client
    # (`raw`, sent below via Response(content=raw, ...), is untouched here).
    body_for_log = mask(logged_body) if logged_body is not None else None
    truncated = False
    if body_for_log is not None:
        serialized = json.dumps(body_for_log, ensure_ascii=False).encode("utf-8")
        if len(serialized) > MAX_STREAM_LOG_BYTES:
            truncated = True
            body_for_log = serialized[:MAX_STREAM_LOG_BYTES].decode("utf-8", "replace")

    ended = time.time()
    log({
        "dir": "response",
        "kind": "response",
        "exchange_id": exchange_id,
        "ended": ended,
        "duration_ms": round((ended - t0) * 1000, 1),
        "url": upstream_url,
        "status": upstream.status_code,
        "content_type": ctype,
        "body": body_for_log,
        "truncated": truncated,
    })

    # drop content-length from rewritten headers; Starlette recomputes it
    hdrs = [(k, v) for k, v in resp_headers if k.lower() != "content-length"]
    return Response(content=raw, status_code=upstream.status_code, headers=dict(hdrs))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
async def handle_root(request: Request) -> Response:
    """Everything under / goes to the configured MCP upstream.

    Well-known discovery documents (RFC 8414 / RFC 9728) are conventionally
    served at the bare origin root, not nested under UPSTREAM's own path, so
    route those to the origin instead of appending them to UPSTREAM's path --
    otherwise e.g. UPSTREAM=https://mcp.example.com/mcp would send
    /.well-known/oauth-protected-resource to .../mcp/.well-known/... (404).
    """
    tail = request.url.path  # includes leading slash
    if tail.startswith("/.well-known/"):
        # RFC 8414 path-insertion: a client deriving the metadata URL for one
        # of our rewritten (/_up/{host}/...) issuers inserts /.well-known/xxx
        # *before* the issuer's apparent path -- which, for us, is our own
        # /_up/{host} marker -- producing e.g.
        # /.well-known/oauth-authorization-server/_up/{host}/{rest}. That
        # must go to the real {host} with the well-known prefix re-inserted
        # ahead of its real path, not be treated as a lookup against
        # UPSTREAM's own origin.
        up_marker = "/_up/"
        idx = tail.find(up_marker)
        if idx != -1:
            wellknown_prefix = tail[:idx]
            host, _, rest = tail[idx + len(up_marker):].partition("/")
            if host not in _known_auth_hosts:
                return Response("unknown upstream host", status_code=403)
            real_path = wellknown_prefix + ("/" + rest if rest else "")
            upstream_url = urlunsplit(("https", host, real_path, "", ""))
        else:
            origin = urlsplit(UPSTREAM)
            upstream_url = urlunsplit((origin.scheme, origin.netloc, tail, "", ""))
    else:
        upstream_url = UPSTREAM + ("" if tail == "/" else tail)
    return await relay(request, upstream_url)


async def handle_up(request: Request) -> Response:
    """/_up/{host}/{rest} -> https://{host}/{rest}  (auth server legs).

    Only hosts the proxy itself rewrote via to_proxy_url() are allowed
    through -- otherwise this route is an open relay to any host on the
    internet for anyone who can reach the proxy port.
    """
    host = request.path_params["host"]
    if host not in _known_auth_hosts:
        return Response("unknown upstream host", status_code=403)
    rest = request.path_params["rest"]
    upstream_url = from_proxy_path(host, rest, request.url.query)
    return await relay(request, upstream_url)


async def handle_authorize(request: Request) -> Response:
    """Redirect-bounce for the browser /authorize leg (see rewrite_metadata).

    We don't proxy the interactive login UI -- only patch `resource` back to
    the real value and 302 straight to the real IdP, so the browser's actual
    session with the IdP (cookies, assets, subsequent redirects) is direct,
    exactly as before, just with a correct `resource` on the way in.
    """
    if not _real_authorization_endpoint:
        return Response("no authorization endpoint known yet -- retry discovery", status_code=404)
    pairs = parse_qsl(request.url.query, keep_blank_values=True)
    patched = [
        (k, _real_resource) if k == "resource" and _real_resource else (k, v)
        for k, v in pairs
    ]
    parts = urlsplit(_real_authorization_endpoint)
    target = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(patched), ""))
    log({"dir": "request", "kind": "redirect", "exchange_id": next(_exchange_ids),
         "method": "GET", "url": target,
         "note": "authorize redirect-bounce; browser continues directly from here"})
    return Response(status_code=302, headers={"Location": target})


async def handle_events(request: Request) -> StreamingResponse:
    """Live SSE feed of proxy activity for a debug UI (e.g. /ui). Never
    forwarded upstream -- registered ahead of the catch-all route below.

    On connect, first backfills up to HISTORY_SIZE past exchange-level
    records (oldest first) so a UI opened late isn't starting blind, then
    subscribes to the broker, drains its queue, and forwards each new
    record as an SSE `data:` event. When idle, emits a periodic
    `kind: "stats"` event instead of a bare `:` comment -- EventSource
    discards comment lines at the browser's protocol layer, so a
    comment-only heartbeat would be invisible to the UI's drop-counter
    display; a real data event serves as both the keep-alive and the
    counter update.
    """
    queue, history = broker.subscribe()

    async def gen():
        try:
            for rec in history:
                yield f"data: {json.dumps(rec, ensure_ascii=False)}\n\n"
            while True:
                try:
                    rec = await asyncio.wait_for(queue.get(), timeout=EVENTS_STATS_INTERVAL)
                except asyncio.TimeoutError:
                    stats = {"kind": "stats", "dropped": broker.dropped_total, "ts": time.time()}
                    yield f"data: {json.dumps(stats)}\n\n"
                    continue
                yield f"data: {json.dumps(rec, ensure_ascii=False)}\n\n"
        finally:
            broker.unsubscribe(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def handle_ui(request: Request) -> Response:
    """Minimal Charles/Fiddler-style debug UI, backed by /events. Re-reads
    static/ui.html from disk on every request rather than caching it at
    import -- this isn't a hot path (opened a handful of times per debug
    session), and it means editing the HTML and refreshing the browser tab
    works without restarting uvicorn."""
    try:
        html = _UI_HTML_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Response("static/ui.html missing", status_code=500)
    return Response(html, media_type="text/html")


from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app):
    global _client
    # http2=True lets httpx negotiate HTTP/2 via ALPN with upstreams that
    # are HTTP/2-only (common behind CloudFront and similar edge proxies);
    # httpx falls back to HTTP/1.1 automatically for upstreams that don't
    # offer h2, so this is backward-compatible with existing HTTP/1.1
    # upstreams. Requires the `h2` package (see requirements.txt).
    _client = httpx.AsyncClient(timeout=httpx.Timeout(None), follow_redirects=False, http2=True)
    print(f"[mcp-proxy] upstream={UPSTREAM}  public={PROXY_PUBLIC}  log={LOG_PATH}")
    print("[mcp-proxy] run with a single worker (the default) -- the /_up "
          "allowlist is in-process state and is not shared across workers")
    yield
    await _client.aclose()


routes = [
    Route("/_authorize", handle_authorize, methods=["GET"]),
    Route("/_up/{host}/{rest:path}", handle_up,
          methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]),
    Route("/events", handle_events, methods=["GET"]),
    Route("/ui", handle_ui, methods=["GET"]),
    Route("/{path:path}", handle_root,
          methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]),
]

app = Starlette(routes=routes, lifespan=lifespan)
