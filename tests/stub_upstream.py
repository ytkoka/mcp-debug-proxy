"""Minimal stub MCP + OAuth server used by the integration tests."""
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

PORT = 18081
BASE = f"http://127.0.0.1:{PORT}"


async def mcp_root(request: Request) -> Response:
    if request.headers.get("authorization"):
        return JSONResponse({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
    return Response(
        status_code=401,
        headers={
            "WWW-Authenticate": (
                f'Bearer resource_metadata="{BASE}/.well-known/oauth-protected-resource"'
            )
        },
    )


async def protected_resource(request: Request) -> Response:
    return JSONResponse({
        "resource": f"{BASE}/mcp",
        "authorization_servers": [f"{BASE}/as"],
    })


async def as_metadata(request: Request) -> Response:
    return JSONResponse({
        "token_endpoint": f"{BASE}/as/token",
        "registration_endpoint": f"{BASE}/as/register",
        "authorization_endpoint": f"{BASE}/as/authorize",
    })


async def token(request: Request) -> Response:
    return JSONResponse({"access_token": "sekrit-token", "token_type": "Bearer"})


async def stream(request: Request) -> Response:
    async def gen():
        chunk = b"data: " + b"x" * 1000 + b"\n\n"
        for _ in range(30):  # 30 * ~1008 bytes > the proxy's 20,000-byte log cap
            yield chunk
    return StreamingResponse(gen(), media_type="text/event-stream")


routes = [
    Route("/mcp", mcp_root, methods=["GET", "POST"]),
    Route("/mcp/stream", stream),
    Route("/.well-known/oauth-protected-resource", protected_resource),
    Route("/as/.well-known/oauth-authorization-server", as_metadata),
    Route("/as/token", token, methods=["POST"]),
]

app = Starlette(routes=routes)
