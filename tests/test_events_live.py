"""T4: GET /events streams live proxy activity over SSE, is never forwarded
upstream, unsubscribes on client disconnect, and sends a periodic `stats`
heartbeat (a real data event, not a `:` comment -- EventSource discards
comment lines browser-side, so the drop-counter must ride a data event).

Needs real sockets (not httpx.ASGITransport, which fully drains an ASGI
app's response before returning control to the caller -- fine for the
existing finite SSE relay, but a hang for a long-lived endpoint like this).
"""
import asyncio
import json

import httpx
import pytest
import uvicorn

from helpers import reload_proxy

PROXY_PORT = 18090


async def _run_server(app, port):
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    return server, task


async def _next_data_record(lines, timeout=5):
    async def _read():
        async for line in lines:
            if line.startswith("data: "):
                return json.loads(line[len("data: "):])
        return None
    return await asyncio.wait_for(_read(), timeout=timeout)


@pytest.mark.asyncio
async def test_events_streams_live_request_response(monkeypatch, tmp_path):
    proxy = reload_proxy(monkeypatch, "https://mcp.example.com/mcp", tmp_path)

    async def fake_send(request, **kwargs):
        return httpx.Response(200, json={"ok": True}, request=request)

    server, task = await _run_server(proxy.app, PROXY_PORT)
    try:
        monkeypatch.setattr(proxy._client, "send", fake_send)

        async with httpx.AsyncClient() as events_client:
            async with events_client.stream(
                "GET", f"http://127.0.0.1:{PROXY_PORT}/events"
            ) as resp:
                assert resp.status_code == 200
                assert resp.headers["content-type"].startswith("text/event-stream")

                # give /events a moment to subscribe before traffic is sent
                while proxy.broker.subscriber_count == 0:
                    await asyncio.sleep(0.01)

                async def _fire_request():
                    async with httpx.AsyncClient() as c:
                        await c.get(f"http://127.0.0.1:{PROXY_PORT}/")
                fire_task = asyncio.create_task(_fire_request())

                lines = resp.aiter_lines()
                seen_kinds = []
                exchange_ids = set()
                while len(seen_kinds) < 2:
                    rec = await _next_data_record(lines)
                    if rec["kind"] in ("request", "response"):
                        seen_kinds.append(rec["kind"])
                        exchange_ids.add(rec["exchange_id"])

                await fire_task
                assert seen_kinds == ["request", "response"]
                assert len(exchange_ids) == 1

            # after exiting the `async with` block, the client stream is
            # closed -- broker should notice the disconnect and unsubscribe
            for _ in range(200):
                if proxy.broker.subscriber_count == 0:
                    break
                await asyncio.sleep(0.01)
            assert proxy.broker.subscriber_count == 0
    finally:
        server.should_exit = True
        await task


@pytest.mark.asyncio
async def test_events_not_forwarded_upstream(monkeypatch, tmp_path):
    proxy = reload_proxy(monkeypatch, "https://mcp.example.com/mcp", tmp_path)

    called = False

    async def fake_send(request, **kwargs):
        nonlocal called
        called = True
        return httpx.Response(200, json={"ok": True}, request=request)

    server, task = await _run_server(proxy.app, PROXY_PORT + 1)
    try:
        monkeypatch.setattr(proxy._client, "send", fake_send)
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "GET", f"http://127.0.0.1:{PROXY_PORT + 1}/events"
            ) as resp:
                assert resp.status_code == 200
                # give it a beat to make sure nothing async sneaks a relay() call in
                await asyncio.sleep(0.05)
    finally:
        server.should_exit = True
        await task

    assert called is False


@pytest.mark.asyncio
async def test_events_heartbeat_carries_drop_count_as_data_event(monkeypatch, tmp_path):
    monkeypatch.setenv("EVENTS_STATS_INTERVAL", "0.2")
    proxy = reload_proxy(monkeypatch, "https://mcp.example.com/mcp", tmp_path)

    server, task = await _run_server(proxy.app, PROXY_PORT + 2)
    try:
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "GET", f"http://127.0.0.1:{PROXY_PORT + 2}/events"
            ) as resp:
                lines = resp.aiter_lines()
                rec = await _next_data_record(lines, timeout=2)
                assert rec["kind"] == "stats"
                assert rec["dropped"] >= 0
    finally:
        server.should_exit = True
        await task
