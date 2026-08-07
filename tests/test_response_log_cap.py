"""T7: buffered JSON responses are capped in the log/live feed the same way
SSE stream summaries already are -- the client still gets the full body,
only what's written to the JSONL file / published to the broker is capped.
"""
import json

import httpx

from helpers import reload_proxy


def _records(log_path):
    with open(log_path) as fh:
        return [json.loads(line) for line in fh]


async def test_large_buffered_response_is_capped_in_log_but_not_to_client(monkeypatch, tmp_path):
    proxy = reload_proxy(monkeypatch, "https://mcp.example.com/mcp", tmp_path)

    big_message = "x" * 50000

    async def fake_send(request, **kwargs):
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "error": {"message": big_message}},
            request=request,
        )

    async with proxy.lifespan(proxy.app):
        monkeypatch.setattr(proxy._client, "send", fake_send)
        transport = httpx.ASGITransport(app=proxy.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r = await client.get("/")

    assert r.status_code == 200
    assert r.json()["error"]["message"] == big_message  # client gets it in full

    recs = _records(proxy.LOG_PATH)
    resp = next(rec for rec in recs if rec["dir"] == "response")
    assert resp["truncated"] is True
    assert isinstance(resp["body"], str)
    assert len(resp["body"]) <= proxy.MAX_STREAM_LOG_BYTES


async def test_small_buffered_response_is_not_truncated(monkeypatch, tmp_path):
    proxy = reload_proxy(monkeypatch, "https://mcp.example.com/mcp", tmp_path)

    async def fake_send(request, **kwargs):
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}, request=request,
        )

    async with proxy.lifespan(proxy.app):
        monkeypatch.setattr(proxy._client, "send", fake_send)
        transport = httpx.ASGITransport(app=proxy.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await client.get("/")

    recs = _records(proxy.LOG_PATH)
    resp = next(rec for rec in recs if rec["dir"] == "response")
    assert resp["truncated"] is False
    assert resp["body"]["id"] == 1
