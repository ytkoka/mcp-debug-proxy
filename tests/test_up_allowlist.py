"""P2: the /_up allowlist must (a) keep rejecting unknown hosts (regression
for the open-relay fix) and (b) not spuriously 403 a host we already know
about -- either because it's UPSTREAM's own host, or because it was seeded
via ALLOWED_AUTH_HOSTS, covering the "client skips discovery after a proxy
restart" scenario from TASKS.md.
"""
import httpx

from helpers import reload_proxy


async def test_unknown_host_still_rejected(monkeypatch, tmp_path):
    proxy = reload_proxy(monkeypatch, "https://mcp.example.com/mcp", tmp_path)

    async with proxy.lifespan(proxy.app):
        transport = httpx.ASGITransport(app=proxy.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r = await client.get("/_up/evil.example.com/token")

    assert r.status_code == 403


async def test_upstream_host_is_seeded_at_startup(monkeypatch, tmp_path):
    proxy = reload_proxy(monkeypatch, "https://mcp.example.com/mcp", tmp_path)
    assert "mcp.example.com" in proxy._known_auth_hosts

    captured = {}

    async def fake_send(request, **kwargs):
        captured["url"] = str(request.url)
        return httpx.Response(200, json={}, request=request)

    async with proxy.lifespan(proxy.app):
        monkeypatch.setattr(proxy._client, "send", fake_send)
        transport = httpx.ASGITransport(app=proxy.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r = await client.get("/_up/mcp.example.com/token")

    assert r.status_code == 200
    assert captured["url"] == "https://mcp.example.com/token"


async def test_allowed_auth_hosts_env_var_seeds_allowlist(monkeypatch, tmp_path):
    proxy = reload_proxy(
        monkeypatch, "https://mcp.example.com/mcp", tmp_path,
        allowed_hosts="auth.example.com, idp2.example.com",
    )
    assert proxy._known_auth_hosts >= {"auth.example.com", "idp2.example.com"}

    async def fake_send(request, **kwargs):
        return httpx.Response(200, json={}, request=request)

    async with proxy.lifespan(proxy.app):
        monkeypatch.setattr(proxy._client, "send", fake_send)
        transport = httpx.ASGITransport(app=proxy.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r = await client.post("/_up/auth.example.com/token")
            r_unknown = await client.get("/_up/still-unknown.example.com/token")

    assert r.status_code == 200
    assert r_unknown.status_code == 403


async def test_no_allowed_auth_hosts_env_var_means_only_upstream_seeded(monkeypatch, tmp_path):
    proxy = reload_proxy(monkeypatch, "https://mcp.example.com/mcp", tmp_path)
    assert proxy._known_auth_hosts == {"mcp.example.com"}
