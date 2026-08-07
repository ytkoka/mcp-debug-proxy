"""T1: request/response (and error) log records for one relay() call must
share a monotonically-increasing exchange_id, and response records must
carry timing (ended/duration_ms) alongside the request's started.
"""
import asyncio
import json

import httpx

from helpers import reload_proxy


def _records(log_path):
    with open(log_path) as fh:
        return [json.loads(line) for line in fh]


async def test_request_response_share_exchange_id_and_have_timing(monkeypatch, tmp_path):
    proxy = reload_proxy(monkeypatch, "https://mcp.example.com/mcp", tmp_path)

    async def fake_send(request, **kwargs):
        return httpx.Response(200, json={"ok": True}, request=request)

    async with proxy.lifespan(proxy.app):
        monkeypatch.setattr(proxy._client, "send", fake_send)
        transport = httpx.ASGITransport(app=proxy.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r = await client.get("/")

    assert r.status_code == 200

    recs = _records(proxy.LOG_PATH)
    assert len(recs) == 2
    req, resp = recs
    assert req["dir"] == "request" and resp["dir"] == "response"
    assert req["exchange_id"] == resp["exchange_id"]
    assert isinstance(req["started"], float)
    assert isinstance(resp["ended"], float)
    assert resp["ended"] >= req["started"]
    assert resp["duration_ms"] >= 0


async def test_exchange_id_is_monotonic_across_calls(monkeypatch, tmp_path):
    proxy = reload_proxy(monkeypatch, "https://mcp.example.com/mcp", tmp_path)

    async def fake_send(request, **kwargs):
        return httpx.Response(200, json={"ok": True}, request=request)

    async with proxy.lifespan(proxy.app):
        monkeypatch.setattr(proxy._client, "send", fake_send)
        transport = httpx.ASGITransport(app=proxy.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await client.get("/")
            await client.get("/")

    recs = _records(proxy.LOG_PATH)
    req_ids = [r["exchange_id"] for r in recs if r["dir"] == "request"]
    assert req_ids[1] == req_ids[0] + 1


async def test_concurrent_calls_get_distinct_exchange_ids(monkeypatch, tmp_path):
    proxy = reload_proxy(monkeypatch, "https://mcp.example.com/mcp", tmp_path)

    async def fake_send(request, **kwargs):
        await asyncio.sleep(0.01)
        return httpx.Response(200, json={"ok": True}, request=request)

    async with proxy.lifespan(proxy.app):
        monkeypatch.setattr(proxy._client, "send", fake_send)
        transport = httpx.ASGITransport(app=proxy.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await asyncio.gather(client.get("/"), client.get("/"))

    recs = _records(proxy.LOG_PATH)
    req_ids = [r["exchange_id"] for r in recs if r["dir"] == "request"]
    assert len(req_ids) == 2
    assert len(set(req_ids)) == 2


async def test_upstream_error_record_carries_exchange_id_and_duration(monkeypatch, tmp_path):
    proxy = reload_proxy(monkeypatch, "https://mcp.example.com/mcp", tmp_path)

    async def fake_send(request, **kwargs):
        raise httpx.ConnectError("boom", request=request)

    async with proxy.lifespan(proxy.app):
        monkeypatch.setattr(proxy._client, "send", fake_send)
        transport = httpx.ASGITransport(app=proxy.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r = await client.get("/")

    assert r.status_code == 502

    recs = _records(proxy.LOG_PATH)
    req, resp = recs
    assert req["exchange_id"] == resp["exchange_id"]
    assert "error" in resp
    assert resp["duration_ms"] >= 0
    assert isinstance(resp["ended"], float)
