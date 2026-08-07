"""Fix1: the buffered-path response record must carry the actual response
body (masked, capped) for the live UI, not just the id/method/tool/error
JSON-RPC summary that `body` was limited to -- e.g. a tools/list response's
tool definitions never showed up anywhere before this.
"""
import json

import httpx

from helpers import reload_proxy


def _records(log_path):
    with open(log_path) as fh:
        return [json.loads(line) for line in fh]


async def test_tools_list_body_text_contains_the_real_tool_definitions(monkeypatch, tmp_path):
    proxy = reload_proxy(monkeypatch, "https://mcp.example.com/mcp", tmp_path)

    tools_result = {
        "jsonrpc": "2.0", "id": 2,
        "result": {"tools": [{"name": "search", "description": "Search things"}]},
    }

    async def fake_send(request, **kwargs):
        return httpx.Response(200, json=tools_result, request=request)

    async with proxy.lifespan(proxy.app):
        monkeypatch.setattr(proxy._client, "send", fake_send)
        transport = httpx.ASGITransport(app=proxy.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r = await client.post("/", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})

    assert r.status_code == 200
    assert r.json() == tools_result  # client gets the real body untouched

    recs = _records(proxy.LOG_PATH)
    resp = next(rec for rec in recs if rec["dir"] == "response")
    # the old summary field is still just id/method -- this is exactly the
    # bug: nothing about the actual tools ever appeared there
    assert resp["body"] == {"id": 2, "method": None}
    body_text = json.loads(resp["body_text"])
    assert body_text["result"]["tools"][0]["name"] == "search"
    assert resp["body_text_truncated"] is False


async def test_oauth_token_response_body_text_masks_secrets(monkeypatch, tmp_path):
    proxy = reload_proxy(monkeypatch, "https://mcp.example.com/mcp", tmp_path)

    async def fake_send(request, **kwargs):
        return httpx.Response(
            200,
            json={"access_token": "sekrit-abc123", "refresh_token": "sekrit-refresh", "token_type": "Bearer"},
            request=request,
        )

    async with proxy.lifespan(proxy.app):
        monkeypatch.setattr(proxy._client, "send", fake_send)
        transport = httpx.ASGITransport(app=proxy.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r = await client.post("/", json={})

    assert r.json()["access_token"] == "sekrit-abc123"  # client still gets the real token

    recs = _records(proxy.LOG_PATH)
    resp = next(rec for rec in recs if rec["dir"] == "response")
    assert "sekrit-abc123" not in resp["body_text"]
    assert "sekrit-refresh" not in resp["body_text"]
    assert "***MASKED***" in resp["body_text"]


async def test_non_json_response_body_text_is_raw_text(monkeypatch, tmp_path):
    proxy = reload_proxy(monkeypatch, "https://mcp.example.com/mcp", tmp_path)

    async def fake_send(request, **kwargs):
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"plain body", request=request)

    async with proxy.lifespan(proxy.app):
        monkeypatch.setattr(proxy._client, "send", fake_send)
        transport = httpx.ASGITransport(app=proxy.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await client.get("/")

    recs = _records(proxy.LOG_PATH)
    resp = next(rec for rec in recs if rec["dir"] == "response")
    assert resp["body_text"] == "plain body"


async def test_large_body_text_is_truncated_but_client_gets_full_body(monkeypatch, tmp_path):
    proxy = reload_proxy(monkeypatch, "https://mcp.example.com/mcp", tmp_path)
    big = "x" * 50000

    async def fake_send(request, **kwargs):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"blob": big}}, request=request)

    async with proxy.lifespan(proxy.app):
        monkeypatch.setattr(proxy._client, "send", fake_send)
        transport = httpx.ASGITransport(app=proxy.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r = await client.get("/")

    assert r.json()["result"]["blob"] == big  # client unaffected by the cap

    recs = _records(proxy.LOG_PATH)
    resp = next(rec for rec in recs if rec["dir"] == "response")
    assert resp["body_text_truncated"] is True
    assert len(resp["body_text"]) <= proxy.MAX_STREAM_LOG_BYTES
