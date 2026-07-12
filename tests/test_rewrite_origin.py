"""P4: a literal Origin: null (opaque/sandboxed origin) must be left alone,
not rewritten to the upstream origin."""
import proxy


def test_null_origin_is_left_alone():
    headers = {"origin": "null"}
    result = proxy.rewrite_origin(headers, "https://mcp.example.com/mcp")
    assert result["origin"] == "null"


def test_real_origin_is_rewritten_to_upstream():
    headers = {"origin": "http://localhost:8080"}
    result = proxy.rewrite_origin(headers, "https://mcp.example.com/mcp")
    assert result["origin"] == "https://mcp.example.com"


def test_referer_is_rewritten_to_upstream():
    headers = {"referer": "http://localhost:8080/"}
    result = proxy.rewrite_origin(headers, "https://mcp.example.com/mcp")
    assert result["referer"] == "https://mcp.example.com/"


def test_missing_origin_is_not_added():
    headers = {"accept": "application/json"}
    result = proxy.rewrite_origin(headers, "https://mcp.example.com/mcp")
    assert "origin" not in result
