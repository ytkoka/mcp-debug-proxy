"""Fix1: when relay() can't reach the upstream at all (httpx.HTTPError), the
proxy must still return 502 to the client, but now also log/publish a
kind=proxy_error record carrying the original exception's class name --
without this, nothing distinguishes "unreachable" from "wrong protocol"
from any other httpx.HTTPError subclass in the JSONL log or live UI.
"""
import json

import httpx

from helpers import reload_proxy


def _records(log_path):
    with open(log_path) as fh:
        return [json.loads(line) for line in fh]


async def test_unreachable_upstream_logs_proxy_error_with_exception_type(monkeypatch, tmp_path):
    # Port 1 is a privileged/unassigned port that reliably refuses TCP
    # connections on localhost, so this needs no network access and no
    # monkeypatching of _client.send -- a real httpx.ConnectError happens.
    proxy = reload_proxy(monkeypatch, "https://127.0.0.1:1/nope", tmp_path)

    async with proxy.lifespan(proxy.app):
        queue, _ = proxy.broker.subscribe()
        transport = httpx.ASGITransport(app=proxy.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r = await client.post("/", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})

    assert r.status_code == 502

    recs = _records(proxy.LOG_PATH)
    err = next(rec for rec in recs if rec.get("kind") == "proxy_error")
    assert err["exchange_id"] is not None
    assert err["url"] == "https://127.0.0.1:1/nope"
    assert ":" in err["error"]  # "<ExceptionClassName>: <message>"
    exc_name = err["error"].split(":", 1)[0]
    assert exc_name.endswith("Error")  # e.g. ConnectError

    # Same record must have reached the live broker/UI feed too.
    published = []
    while not queue.empty():
        published.append(queue.get_nowait())
    assert any(rec.get("kind") == "proxy_error" for rec in published)


async def test_proxy_error_shares_exchange_id_with_its_request(monkeypatch, tmp_path):
    proxy = reload_proxy(monkeypatch, "https://127.0.0.1:1/nope", tmp_path)

    async with proxy.lifespan(proxy.app):
        transport = httpx.ASGITransport(app=proxy.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await client.get("/")

    recs = _records(proxy.LOG_PATH)
    req = next(rec for rec in recs if rec["dir"] == "request")
    err = next(rec for rec in recs if rec.get("kind") == "proxy_error")
    assert req["exchange_id"] == err["exchange_id"]
