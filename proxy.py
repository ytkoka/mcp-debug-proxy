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
  - token exchange + refresh (/token)

What is NOT captured (by design):
  - the browser -> IdP /authorize leg (browser hits the IdP directly, not us).
    The auth `code` is still visible on the subsequent token-exchange request.

Run:
    UPSTREAM=https://mcp.example.com/mcp uvicorn proxy:app --port 8080
Then point the MCP client at:  http://localhost:8080/
"""
from __future__ import annotations

import json
import os
import time
from urllib.parse import urlsplit, urlunsplit, quote, unquote

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
UPSTREAM = os.environ.get("UPSTREAM", "https://mcp.example.com/mcp").rstrip("/")
PROXY_PUBLIC = os.environ.get("PROXY_PUBLIC", "http://localhost:8080").rstrip("/")
LOG_PATH = os.environ.get("LOG_PATH", "mcp_proxy.jsonl")

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

# Hosts we've pointed the client at via to_proxy_url() (auth-server /_up
# legs). /_up/{host}/... must only proxy to hosts we ourselves handed out --
# otherwise it's an open relay to any host for anyone who can reach the port.
_known_auth_hosts: set[str] = set()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def log(record: dict) -> None:
    record["ts"] = time.time()
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


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
    """Rewrite endpoints we want to intercept; leave the browser-facing
    authorization_endpoint pointing straight at the IdP."""
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
    return doc


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
        if low == "origin" and headers[key]:
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
    body = await request.body()
    req_headers = clean_request_headers(request)
    req_headers = rewrite_origin(req_headers, upstream_url)

    # Log the outbound request.
    log({
        "dir": "request",
        "method": request.method,
        "url": upstream_url,
        "headers": mask_headers(req_headers),
        "jsonrpc": mask(summarize_jsonrpc(body)) if body else None,
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
        log({
            "dir": "response",
            "url": upstream_url,
            "error": str(exc),
        })
        return Response(f"upstream error: {exc}", status_code=502)

    ctype = upstream.headers.get("content-type", "")
    resp_headers = clean_response_headers(upstream)

    # --- streaming (SSE) path: never buffer, tee chunks to the log ----------
    if "text/event-stream" in ctype:
        MAX_STREAM_LOG_BYTES = 20000

        async def body_iter():
            # Cap what we hold for the log entry -- the stream itself may run
            # for the life of an MCP session, so buf must not grow unbounded.
            buf = bytearray()
            try:
                async for chunk in upstream.aiter_raw():
                    if len(buf) < MAX_STREAM_LOG_BYTES:
                        buf.extend(chunk[: MAX_STREAM_LOG_BYTES - len(buf)])
                    yield chunk
            finally:
                await upstream.aclose()
                log({
                    "dir": "response",
                    "url": upstream_url,
                    "status": upstream.status_code,
                    "stream": True,
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

    log({
        "dir": "response",
        "url": upstream_url,
        "status": upstream.status_code,
        "content_type": ctype,
        "body": mask(logged_body) if logged_body is not None else None,
    })

    # drop content-length from rewritten headers; Starlette recomputes it
    hdrs = [(k, v) for k, v in resp_headers if k.lower() != "content-length"]
    return Response(content=raw, status_code=upstream.status_code, headers=dict(hdrs))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
async def handle_root(request: Request) -> Response:
    """Everything under / goes to the configured MCP upstream."""
    tail = request.url.path  # includes leading slash
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


from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app):
    global _client
    _client = httpx.AsyncClient(timeout=httpx.Timeout(None), follow_redirects=False)
    print(f"[mcp-proxy] upstream={UPSTREAM}  public={PROXY_PUBLIC}  log={LOG_PATH}")
    yield
    await _client.aclose()


routes = [
    Route("/_up/{host}/{rest:path}", handle_up,
          methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]),
    Route("/{path:path}", handle_root,
          methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]),
]

app = Starlette(routes=routes, lifespan=lifespan)
