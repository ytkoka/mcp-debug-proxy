"""Fix2: response records (both the buffered path and the SSE stream_end
summary) must carry the upstream's response headers, masked the same way
request headers already are, so the live UI's detail pane isn't asymmetric
(request headers visible, response headers missing).
"""
import json

import httpx

from helpers import reload_proxy


def _records(log_path):
    with open(log_path) as fh:
        return [json.loads(line) for line in fh]


async def test_buffered_response_headers_are_logged(monkeypatch, tmp_path):
    proxy = reload_proxy(monkeypatch, "https://mcp.example.com/mcp", tmp_path)

    async def fake_send(request, **kwargs):
        return httpx.Response(
            200, json={"ok": True},
            headers={"mcp-session-id": "sess-123", "authorization": "Bearer sekrit"},
            request=request,
        )

    async with proxy.lifespan(proxy.app):
        monkeypatch.setattr(proxy._client, "send", fake_send)
        transport = httpx.ASGITransport(app=proxy.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r = await client.get("/")

    # client still gets the real headers, untouched
    assert r.headers["mcp-session-id"] == "sess-123"

    recs = _records(proxy.LOG_PATH)
    resp = next(rec for rec in recs if rec["dir"] == "response")
    assert resp["headers"]["mcp-session-id"] == "sess-123"
    assert resp["headers"]["authorization"] == "***MASKED***"


async def test_sse_stream_end_headers_are_logged(monkeypatch, tmp_path):
    proxy = reload_proxy(monkeypatch, "https://mcp.example.com/mcp", tmp_path)

    async def gen():
        yield b"data: hi\n\n"

    async def fake_send(request, **kwargs):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream", "mcp-session-id": "sess-456"},
            content=gen(), request=request,
        )

    async with proxy.lifespan(proxy.app):
        monkeypatch.setattr(proxy._client, "send", fake_send)
        transport = httpx.ASGITransport(app=proxy.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await client.get("/")

    recs = _records(proxy.LOG_PATH)
    stream_end = next(rec for rec in recs if rec["kind"] == "stream_end")
    assert stream_end["headers"]["mcp-session-id"] == "sess-456"
