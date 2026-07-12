"""`resource` (RFC 8707) needs two faces: the client validates it against the
URL it's actually connected to (the proxy), but the IdP must still bind the
issued token to the real upstream server, or the token is useless for real
API calls. rewrite_metadata() rewrites the client-facing value; relay()
patches it back to the real value on the way to the token endpoint.
"""
from urllib.parse import parse_qsl

import httpx

import proxy
from helpers import reload_proxy


def test_rewrite_metadata_swaps_resource_for_proxy_public(monkeypatch):
    monkeypatch.setattr(proxy, "PROXY_PUBLIC", "http://127.0.0.1:18080")
    monkeypatch.setattr(proxy, "_real_resource", None)

    doc = proxy.rewrite_metadata({"resource": "https://aws-mcp.us-east-1.api.aws/mcp"})

    assert doc["resource"] == "http://127.0.0.1:18080"
    assert proxy._real_resource == "https://aws-mcp.us-east-1.api.aws/mcp"


def test_patch_resource_param_substitutes_real_value(monkeypatch):
    monkeypatch.setattr(proxy, "PROXY_PUBLIC", "http://127.0.0.1:18080")
    monkeypatch.setattr(proxy, "_real_resource", "https://aws-mcp.us-east-1.api.aws/mcp")

    body = b"grant_type=authorization_code&resource=http%3A%2F%2F127.0.0.1%3A18080&code=abc123"
    patched = proxy.patch_resource_param(body)

    fields = dict(parse_qsl(patched.decode()))
    assert fields["resource"] == "https://aws-mcp.us-east-1.api.aws/mcp"
    assert fields["code"] == "abc123"  # other fields untouched


def test_patch_resource_param_tolerates_trailing_slash(monkeypatch):
    # Clients don't necessarily echo `resource` back byte-for-byte from what
    # rewrite_metadata() said -- e.g. mcp-remote's own /authorize request
    # observed a trailing "/" added even though PROXY_PUBLIC has none. An
    # unpatched `resource` here means the real IdP rejects the token request.
    monkeypatch.setattr(proxy, "PROXY_PUBLIC", "http://127.0.0.1:18080")
    monkeypatch.setattr(proxy, "_real_resource", "https://aws-mcp.us-east-1.api.aws/mcp")

    body = b"grant_type=authorization_code&resource=http%3A%2F%2F127.0.0.1%3A18080%2F&code=abc123"
    patched = proxy.patch_resource_param(body)

    fields = dict(parse_qsl(patched.decode()))
    assert fields["resource"] == "https://aws-mcp.us-east-1.api.aws/mcp"


def test_patch_resource_param_leaves_unrelated_bodies_alone(monkeypatch):
    monkeypatch.setattr(proxy, "PROXY_PUBLIC", "http://127.0.0.1:18080")
    monkeypatch.setattr(proxy, "_real_resource", "https://aws-mcp.us-east-1.api.aws/mcp")

    body = b"grant_type=refresh_token&refresh_token=xyz"
    assert proxy.patch_resource_param(body) == body


async def test_token_request_gets_real_resource_end_to_end(monkeypatch, tmp_path):
    px = reload_proxy(monkeypatch, "https://aws-mcp.us-east-1.api.aws/mcp", tmp_path)
    # simulate having already seen protected-resource metadata
    monkeypatch.setattr(px, "_real_resource", "https://aws-mcp.us-east-1.api.aws/mcp")
    px._known_auth_hosts.add("us-east-1.oauth.signin.aws")

    captured = {}

    async def fake_send(request, **kwargs):
        captured["body"] = request.content
        return httpx.Response(200, json={"access_token": "tok"}, request=request)

    async with px.lifespan(px.app):
        monkeypatch.setattr(px._client, "send", fake_send)
        transport = httpx.ASGITransport(app=px.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r = await client.post(
                "/_up/us-east-1.oauth.signin.aws/token",
                data={"grant_type": "authorization_code", "resource": px.PROXY_PUBLIC, "code": "abc"},
            )

    assert r.status_code == 200
    sent_fields = dict(parse_qsl(captured["body"].decode()))
    assert sent_fields["resource"] == "https://aws-mcp.us-east-1.api.aws/mcp"
