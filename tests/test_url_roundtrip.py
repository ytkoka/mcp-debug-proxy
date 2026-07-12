"""P3: to_proxy_url() / from_proxy_path() must round-trip for issuer-only,
trailing-slash, and path-bearing authorization_server URLs -- these are all
real shapes that authorization_servers entries take in the wild."""
from urllib.parse import urlsplit

import pytest

import proxy


@pytest.mark.parametrize("original", [
    "https://auth.example.com",       # issuer only, no path
    "https://auth.example.com/",      # issuer only, trailing slash
    "https://auth.example.com/oauth", # issuer with a path component
])
def test_to_proxy_url_and_back_preserves_origin_and_path(original, monkeypatch):
    monkeypatch.setattr(proxy, "PROXY_PUBLIC", "http://127.0.0.1:18080")

    proxied = proxy.to_proxy_url(original)
    prefix = proxy.PROXY_PUBLIC + "/_up/"
    assert proxied.startswith(prefix)

    # Mirror exactly how Starlette's `/_up/{host}/{rest:path}` route splits
    # the URL, so this test exercises the same boundary the real router uses.
    tail = proxied[len(prefix):]
    host, _, rest = tail.partition("/")

    reconstructed = proxy.from_proxy_path(host, rest, "")

    parts = urlsplit(original)
    expected = f"{parts.scheme}://{parts.netloc}{parts.path or '/'}"
    assert reconstructed == expected


def test_query_string_is_carried_through_separately():
    # to_proxy_url embeds the *original* query in the URL it hands out, but
    # from_proxy_path is always called with the *live* query the client sent
    # to /_up -- these must not be conflated.
    proxied = proxy.to_proxy_url("https://auth.example.com/authorize?foo=bar")
    assert "foo=bar" in proxied

    reconstructed = proxy.from_proxy_path("auth.example.com", "authorize", "baz=qux")
    assert reconstructed == "https://auth.example.com/authorize?baz=qux"
