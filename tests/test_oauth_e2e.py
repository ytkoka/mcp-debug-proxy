"""Full OAuth discovery -> DCR-less token exchange -> authenticated call flow
against real sockets, plus regression checks for the fixes made in earlier
rounds (SSE log cap, unreachable-upstream 502, unknown-host 403). This is the
closest thing to TASKS.md's "real server" acceptance E2E that's practical
without an actual external MCP server + IdP.
"""
import asyncio
import json
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import httpx
import pytest
import uvicorn

import stub_upstream
from helpers import reload_proxy

PROXY_PORT = 18080


async def _run_server(app, port):
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    return server, task


@pytest.mark.asyncio
async def test_full_oauth_flow_and_regressions(monkeypatch, tmp_path):
    stub_server, stub_task = await _run_server(stub_upstream.app, stub_upstream.PORT)
    try:
        proxy = reload_proxy(monkeypatch, f"{stub_upstream.BASE}/mcp", tmp_path)

        # from_proxy_path() hardcodes https:// (real IdPs are always TLS);
        # our stub is plain http for test simplicity, so patch just the
        # scheme for this test -- it doesn't touch the code under test.
        def http_from_proxy_path(host, rest, query):
            path = "/" + rest if not rest.startswith("/") else rest
            return urlunsplit(("http", host, path, query, ""))
        monkeypatch.setattr(proxy, "from_proxy_path", http_from_proxy_path)

        proxy_server, proxy_task = await _run_server(proxy.app, PROXY_PORT)
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(f"http://127.0.0.1:{PROXY_PORT}/")
                assert r.status_code == 401
                waa = r.headers["www-authenticate"]
                assert f"127.0.0.1:{PROXY_PORT}/_up/" in waa

                start = waa.find('resource_metadata="') + len('resource_metadata="')
                pr_url = waa[start:waa.find('"', start)]

                # open-relay regression: unknown /_up host still rejected
                r = await client.get(f"http://127.0.0.1:{PROXY_PORT}/_up/evil.example.com/x")
                assert r.status_code == 403

                # P1: well-known reachable directly at the proxy root even
                # though UPSTREAM has its own /mcp path
                r = await client.get(
                    f"http://127.0.0.1:{PROXY_PORT}/.well-known/oauth-protected-resource"
                )
                assert r.status_code == 200
                assert r.json()["authorization_servers"][0].startswith(
                    f"http://127.0.0.1:{PROXY_PORT}/_up/"
                )

                r = await client.get(pr_url)
                assert r.status_code == 200
                pr_doc = r.json()
                # resource dual-identity: client-facing value must be us, not
                # the real server, or mcp-remote's client-side RFC 8707 check
                # rejects the metadata outright
                assert pr_doc["resource"] == f"http://127.0.0.1:{PROXY_PORT}"
                as_url = pr_doc["authorization_servers"][0]

                as_meta_url = as_url.rstrip("/") + "/.well-known/oauth-authorization-server"
                r = await client.get(as_meta_url)
                assert r.status_code == 200
                as_doc = r.json()
                # authorization_endpoint must bounce through us too, or the
                # browser leg carries the client-facing `resource` straight
                # to an IdP that may reject it (confirmed against AWS's real
                # endpoint: real resource -> 302, proxy's resource -> 400)
                assert as_doc["authorization_endpoint"] == f"http://127.0.0.1:{PROXY_PORT}/_authorize"

                r = await client.get(
                    f"{as_doc['authorization_endpoint']}?resource={proxy.PROXY_PUBLIC}&foo=bar",
                    follow_redirects=False,
                )
                assert r.status_code == 302
                location = urlsplit(r.headers["location"])
                assert f"{location.scheme}://{location.netloc}{location.path}" == \
                    f"{stub_upstream.BASE}/as/authorize"
                bounced_params = dict(parse_qsl(location.query))
                assert bounced_params["resource"] == f"{stub_upstream.BASE}/mcp"  # patched back
                assert bounced_params["foo"] == "bar"  # unrelated params pass through untouched

                r = await client.post(
                    as_doc["token_endpoint"], data={"grant_type": "authorization_code"}
                )
                assert r.status_code == 200
                assert r.json()["access_token"] == "sekrit-token"

                r = await client.get(
                    f"http://127.0.0.1:{PROXY_PORT}/",
                    headers={"authorization": "Bearer sekrit-token"},
                )
                assert r.status_code == 200
                assert r.json()["result"]["ok"] is True

                # SSE regression: client still gets the full body...
                full = b""
                async with client.stream(
                    "GET", f"http://127.0.0.1:{PROXY_PORT}/stream"
                ) as resp:
                    assert resp.status_code == 200
                    async for chunk in resp.aiter_bytes():
                        full += chunk
                assert len(full) > 20000

                # ...502 regression: unreachable upstream doesn't crash the proxy
                proxy._known_auth_hosts.add("127.0.0.1:59999")
                r = await client.get(f"http://127.0.0.1:{PROXY_PORT}/_up/127.0.0.1:59999/nope")
                assert r.status_code == 502
        finally:
            proxy_server.should_exit = True
            await proxy_task
    finally:
        stub_server.should_exit = True
        await stub_task

    await asyncio.sleep(0.2)  # let the SSE handler's finally-block log write land

    stream_lens = []
    stream_kinds = []
    error_found = False
    with open(proxy.LOG_PATH) as fh:
        for line in fh:
            rec = json.loads(line)
            if rec.get("stream"):
                stream_lens.append(len(rec.get("raw", "")))
                stream_kinds.append(rec.get("kind"))
            if "error" in rec:
                error_found = True

    assert stream_lens and all(n <= 20000 for n in stream_lens)
    assert stream_kinds == ["stream_end"] * len(stream_kinds)
    assert error_found
