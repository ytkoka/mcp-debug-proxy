"""T3: log() fans records out to the broker; SSE chunks are published live
(per-chunk, before the stream closes) but never written to the JSONL file --
only the capped stream_end summary is persisted.
"""
import json

import httpx

from helpers import reload_proxy


def _records(log_path):
    with open(log_path) as fh:
        return [json.loads(line) for line in fh]


async def _stream_gen(chunks):
    for c in chunks:
        yield c


async def test_normal_request_response_are_published(monkeypatch, tmp_path):
    proxy = reload_proxy(monkeypatch, "https://mcp.example.com/mcp", tmp_path)

    async def fake_send(request, **kwargs):
        return httpx.Response(200, json={"ok": True}, request=request)

    async with proxy.lifespan(proxy.app):
        monkeypatch.setattr(proxy._client, "send", fake_send)
        queue, history = proxy.broker.subscribe()

        transport = httpx.ASGITransport(app=proxy.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r = await client.get("/")

    assert r.status_code == 200
    published = [queue.get_nowait(), queue.get_nowait()]
    assert published[0]["kind"] == "request"
    assert published[1]["kind"] == "response"
    assert published[0]["exchange_id"] == published[1]["exchange_id"]


async def test_sse_chunks_are_published_live_and_not_logged_to_file(monkeypatch, tmp_path):
    proxy = reload_proxy(monkeypatch, "https://mcp.example.com/mcp", tmp_path)

    chunks = [b"data: one\n\n", b"data: two\n\n", b"data: three\n\n"]

    async def fake_send(request, **kwargs):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=_stream_gen(chunks), request=request,
        )

    async with proxy.lifespan(proxy.app):
        monkeypatch.setattr(proxy._client, "send", fake_send)
        queue, _ = proxy.broker.subscribe()

        transport = httpx.ASGITransport(app=proxy.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r = await client.get("/")

    assert r.status_code == 200
    assert r.content == b"".join(chunks)

    published = []
    while not queue.empty():
        published.append(queue.get_nowait())

    stream_chunks = [p for p in published if p["kind"] == "stream_chunk"]
    stream_end = [p for p in published if p["kind"] == "stream_end"]
    assert len(stream_chunks) == 3
    assert [c["seq"] for c in stream_chunks] == [1, 2, 3]
    exchange_id = published[0]["exchange_id"]
    assert all(c["exchange_id"] == exchange_id for c in stream_chunks)
    assert len(stream_end) == 1
    assert stream_end[0]["exchange_id"] == exchange_id

    # File must never contain per-chunk noise -- only request + stream_end.
    recs = _records(proxy.LOG_PATH)
    kinds = [r.get("kind") for r in recs]
    assert "stream_chunk" not in kinds
    assert kinds == ["request", "stream_end"]


async def test_zero_subscribers_does_not_break_relay(monkeypatch, tmp_path):
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
    assert [rec["kind"] for rec in recs] == ["request", "response"]
