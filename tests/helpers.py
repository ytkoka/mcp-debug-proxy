import importlib


def reload_proxy(monkeypatch, upstream, tmp_path, allowed_hosts=None,
                  proxy_public="http://127.0.0.1:18080"):
    """(Re)load proxy.py against a fresh set of env vars.

    proxy.py reads UPSTREAM / PROXY_PUBLIC / LOG_PATH / ALLOWED_AUTH_HOSTS
    from os.environ at import time, so any test that needs a different value
    for one of these must reload the module afterwards.
    """
    monkeypatch.setenv("UPSTREAM", upstream)
    monkeypatch.setenv("PROXY_PUBLIC", proxy_public)
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "test.jsonl"))
    if allowed_hosts is None:
        monkeypatch.delenv("ALLOWED_AUTH_HOSTS", raising=False)
    else:
        monkeypatch.setenv("ALLOWED_AUTH_HOSTS", allowed_hosts)

    import proxy
    importlib.reload(proxy)
    return proxy
