"""Fix2: the proxy's httpx client must negotiate HTTP/2 (via ALPN) so
HTTP/2-only upstreams (common behind CloudFront and similar edge proxies)
don't fail with a protocol error that gets flattened into a 502. httpx
falls back to HTTP/1.1 automatically for upstreams that don't offer h2, so
this is a pure superset of the previous HTTP/1.1-only behavior.

Real network access to an HTTP/2-only server is out of scope for the
default unit test run (see pytest.ini's `integration` marker / README
Testing section) -- this asserts the client is configured correctly and
leaves actual protocol negotiation to manual E2E against a real upstream.
"""
from helpers import reload_proxy


async def test_async_client_is_configured_with_http2(monkeypatch, tmp_path):
    proxy = reload_proxy(monkeypatch, "https://mcp.example.com/mcp", tmp_path)

    async with proxy.lifespan(proxy.app):
        assert proxy._client._transport._pool._http2 is True
