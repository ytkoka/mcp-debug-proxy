"""Fix3: favicon.ico requests (browser housekeeping against whatever origin
a tab is open on, e.g. /ui) must not be relayed upstream or clutter the
exchange log/live UI -- the proxy answers them directly with 204.
"""
import json

import httpx

from helpers import reload_proxy


async def test_favicon_not_forwarded_upstream(monkeypatch, tmp_path):
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
            r = await client.get("/favicon.ico")
            r2 = await client.get("/mcp/favicon.ico")

    assert r.status_code == 204
    assert r2.status_code == 204
    assert called is False


async def test_favicon_is_not_logged_or_published(monkeypatch, tmp_path):
    proxy = reload_proxy(monkeypatch, "https://mcp.example.com/mcp", tmp_path)

    async def fake_send(request, **kwargs):
        return httpx.Response(200, json={"ok": True}, request=request)

    async with proxy.lifespan(proxy.app):
        monkeypatch.setattr(proxy._client, "send", fake_send)
        queue, _ = proxy.broker.subscribe()
        transport = httpx.ASGITransport(app=proxy.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await client.get("/favicon.ico")

    assert queue.empty()
    # No log() call at all means the file may never have been created --
    # that itself is proof nothing was written.
    import os
    if os.path.exists(proxy.LOG_PATH):
        with open(proxy.LOG_PATH) as fh:
            assert fh.readlines() == []


async def test_normal_mcp_request_is_unaffected(monkeypatch, tmp_path):
    proxy = reload_proxy(monkeypatch, "https://mcp.example.com/mcp", tmp_path)

    async def fake_send(request, **kwargs):
        return httpx.Response(200, json={"ok": True}, request=request)

    async with proxy.lifespan(proxy.app):
        monkeypatch.setattr(proxy._client, "send", fake_send)
        transport = httpx.ASGITransport(app=proxy.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r = await client.get("/")

    assert r.status_code == 200
    with open(proxy.LOG_PATH) as fh:
        recs = [json.loads(line) for line in fh]
    assert [rec["dir"] for rec in recs] == ["request", "response"]
