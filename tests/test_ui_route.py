"""T6: GET /ui serves the static debug-UI page and is never forwarded
upstream. No browser-side JS testing infrastructure exists in this repo
(Playwright would be disproportionate for an 8-task incremental plan), so
this is a smoke test of the route/content-type/basic-content contract.
"""
import httpx

from helpers import reload_proxy


async def test_ui_route_serves_html(monkeypatch, tmp_path):
    proxy = reload_proxy(monkeypatch, "https://mcp.example.com/mcp", tmp_path)

    async with proxy.lifespan(proxy.app):
        transport = httpx.ASGITransport(app=proxy.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r = await client.get("/ui")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "EventSource" in r.text
    assert "/events" in r.text


async def test_ui_route_not_forwarded_upstream(monkeypatch, tmp_path):
    proxy = reload_proxy(monkeypatch, "https://mcp.example.com/mcp", tmp_path)

    called = False

    async def fake_send(request, **kwargs):
        nonlocal called
        called = True
        return httpx.Response(200, json={"ok": True}, request=request)

    async with proxy.lifespan(proxy.app):
        monkeypatch.setattr(proxy._client, "send", fake_send)
        transport = httpx.ASGITransport(app=proxy.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r = await client.get("/ui")

    assert r.status_code == 200
    assert called is False
