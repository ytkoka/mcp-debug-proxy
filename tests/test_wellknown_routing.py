"""P1: /.well-known/* must go to UPSTREAM's origin, not be appended after
UPSTREAM's own path. Everything else keeps going to UPSTREAM (path included).

These drive the real ASGI app end to end but fake out proxy._client.send()
so no real upstream connection is needed -- we only care what URL the proxy
decided to call.
"""
import httpx
import pytest

from helpers import reload_proxy


def _capturing_send(captured):
    async def fake_send(request, **kwargs):
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True}, request=request)
    return fake_send


@pytest.mark.parametrize("upstream,expected_origin", [
    ("https://mcp.example.com/mcp", "https://mcp.example.com"),
    ("https://mcp.example.com", "https://mcp.example.com"),
    ("https://mcp.example.com:8443/mcp", "https://mcp.example.com:8443"),
])
async def test_wellknown_routed_to_origin_root(monkeypatch, tmp_path, upstream, expected_origin):
    proxy = reload_proxy(monkeypatch, upstream, tmp_path)
    captured = {}

    async with proxy.lifespan(proxy.app):
        monkeypatch.setattr(proxy._client, "send", _capturing_send(captured))
        transport = httpx.ASGITransport(app=proxy.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await client.get("/.well-known/oauth-protected-resource")

    assert captured["url"] == f"{expected_origin}/.well-known/oauth-protected-resource"


async def test_normal_requests_still_go_to_upstream_path(monkeypatch, tmp_path):
    proxy = reload_proxy(monkeypatch, "https://mcp.example.com/mcp", tmp_path)
    captured = {}

    async with proxy.lifespan(proxy.app):
        monkeypatch.setattr(proxy._client, "send", _capturing_send(captured))
        transport = httpx.ASGITransport(app=proxy.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await client.post("/", json={"jsonrpc": "2.0", "method": "ping"})

    assert captured["url"] == "https://mcp.example.com/mcp"
