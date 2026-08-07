"""OPEN_UI: opt-in auto-launch of a browser pointed at /ui on startup.

Must default to off -- lifespan() runs for real (not mocked) throughout
this test suite (`async with proxy.lifespan(proxy.app):`), so if this
defaulted to on, every test run would pop open dozens of browser windows
and break headless/CI environments outright.
"""
import webbrowser

from helpers import reload_proxy


async def test_open_ui_defaults_to_off(monkeypatch, tmp_path):
    proxy = reload_proxy(monkeypatch, "https://mcp.example.com/mcp", tmp_path)
    assert proxy.OPEN_UI is False

    calls = []
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: calls.append((a, k)))

    async with proxy.lifespan(proxy.app):
        pass

    assert calls == []


async def test_open_ui_true_launches_browser_at_ui_path(monkeypatch, tmp_path):
    monkeypatch.setenv("OPEN_UI", "1")
    proxy = reload_proxy(
        monkeypatch, "https://mcp.example.com/mcp", tmp_path,
        proxy_public="http://127.0.0.1:19999",
    )
    assert proxy.OPEN_UI is True

    calls = []
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: calls.append((a, k)))

    async with proxy.lifespan(proxy.app):
        pass

    assert calls == [(("http://127.0.0.1:19999/ui",), {"new": 1})]


async def test_open_ui_browser_failure_does_not_crash_startup(monkeypatch, tmp_path):
    monkeypatch.setenv("OPEN_UI", "1")
    proxy = reload_proxy(monkeypatch, "https://mcp.example.com/mcp", tmp_path)

    def boom(*a, **k):
        raise webbrowser.Error("no browser found")
    monkeypatch.setattr(webbrowser, "open", boom)

    async with proxy.lifespan(proxy.app):
        pass  # must not raise
