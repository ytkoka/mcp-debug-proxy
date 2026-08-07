"""T5: /events backfills recent history (oldest-first) to a UI that
connects late, bounded by HISTORY_SIZE, and stream_chunk noise from a
single SSE session must never evict other exchanges from that buffer.
"""
import asyncio
import json

import httpx
import pytest
import uvicorn

from helpers import reload_proxy

PROXY_PORT = 18100


async def _run_server(app, port):
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    return server, task


async def test_broker_history_is_bounded_and_oldest_first(monkeypatch, tmp_path):
    proxy = reload_proxy(monkeypatch, "https://mcp.example.com/mcp", tmp_path, history_size=3)

    for i in range(5):
        proxy.broker.publish({"kind": "x", "n": i})

    queue, history = proxy.broker.subscribe()
    assert [rec["n"] for rec in history] == [2, 3, 4]
    assert queue.empty()  # nothing new published since subscribing


async def test_stream_chunks_never_enter_history(monkeypatch, tmp_path):
    proxy = reload_proxy(monkeypatch, "https://mcp.example.com/mcp", tmp_path, history_size=3)

    proxy.broker.publish({"kind": "request", "n": 1})
    for seq in range(10):
        proxy.broker.publish({"kind": "stream_chunk", "seq": seq}, history=False)
    proxy.broker.publish({"kind": "stream_end", "n": 1})

    _, history = proxy.broker.subscribe()
    kinds = [rec["kind"] for rec in history]
    assert "stream_chunk" not in kinds
    assert kinds == ["request", "stream_end"]


@pytest.mark.asyncio
async def test_events_backfills_prior_exchanges_before_live_traffic(monkeypatch, tmp_path):
    proxy = reload_proxy(monkeypatch, "https://mcp.example.com/mcp", tmp_path)

    async def fake_send(request, **kwargs):
        return httpx.Response(200, json={"ok": True}, request=request)

    server, task = await _run_server(proxy.app, PROXY_PORT)
    try:
        monkeypatch.setattr(proxy._client, "send", fake_send)

        # two exchanges happen *before* anyone opens /events
        async with httpx.AsyncClient() as c:
            await c.get(f"http://127.0.0.1:{PROXY_PORT}/")
            await c.get(f"http://127.0.0.1:{PROXY_PORT}/")

        async with httpx.AsyncClient() as events_client:
            async with events_client.stream(
                "GET", f"http://127.0.0.1:{PROXY_PORT}/events"
            ) as resp:
                lines = resp.aiter_lines()
                backfilled = []

                async def _read():
                    async for line in lines:
                        if not line.startswith("data: "):
                            continue
                        rec = json.loads(line[len("data: "):])
                        backfilled.append(rec)
                        if len(backfilled) == 4:  # 2 requests + 2 responses
                            return

                await asyncio.wait_for(_read(), timeout=5)

        kinds = [rec["kind"] for rec in backfilled]
        assert kinds == ["request", "response", "request", "response"]
        exchange_ids = [rec["exchange_id"] for rec in backfilled]
        assert exchange_ids == sorted(exchange_ids)  # oldest-first
    finally:
        server.should_exit = True
        await task
