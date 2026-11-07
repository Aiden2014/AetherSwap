from pathlib import Path
from types import SimpleNamespace

import pytest

from app.runtime_env import (
    RuntimeProfile,
    get_bind_host,
    get_local_url,
    get_port,
    get_runtime_profile,
)


def test_server_requirements_omit_desktop_webview_dependency():
    text = Path("requirements-server.txt").read_text(encoding="utf-8").lower()
    deps = [line.strip().split(">", 1)[0] for line in text.splitlines() if line.strip() and not line.startswith("#")]

    assert "pywebview" not in deps


def test_legacy_app_main_exposes_fastapi_app_lazily():
    import app.main as main

    assert getattr(main, "app").title == "aetherswap"


def test_auto_linux_without_display_uses_server_mode():
    profile = get_runtime_profile(env={}, platform_name="linux")

    assert profile.mode == "server"
    assert profile.headless is True
    assert profile.can_launch_headful_browser is False
    assert get_bind_host(profile, env={}) == "0.0.0.0"


def test_no_display_stays_effectively_headless_even_if_env_disables_headless():
    profile = get_runtime_profile(env={"AETHERSWAP_HEADLESS": "0"}, platform_name="linux")

    assert profile.mode == "server"
    assert profile.headless is True
    assert profile.can_open_gui is False


def test_auto_linux_with_display_uses_desktop_mode():
    profile = get_runtime_profile(env={"DISPLAY": ":0"}, platform_name="linux")

    assert profile.mode == "desktop"
    assert profile.can_launch_headful_browser is True
    assert get_bind_host(profile, env={}) == "127.0.0.1"


def test_env_overrides_host_port_and_local_url():
    env = {"AETHERSWAP_MODE": "server", "AETHERSWAP_HOST": "127.0.0.1", "AETHERSWAP_PORT": "3000"}
    profile = get_runtime_profile(env=env, platform_name="linux")

    assert profile.mode == "server"
    assert get_bind_host(profile, env=env) == "127.0.0.1"
    assert get_port(env=env) == 3000
    assert get_local_url("0.0.0.0", 28472) == "http://127.0.0.1:28472"


def test_relogin_start_requires_manual_cookie_without_gui(monkeypatch):
    from app.routes import auth

    profile = RuntimeProfile(
        platform="linux",
        requested_mode="auto",
        mode="server",
        display_available=False,
        headless=True,
        can_open_gui=False,
        can_launch_headful_browser=False,
        open_browser=False,
        reason="no graphical display detected",
    )
    monkeypatch.setattr(auth, "get_runtime_profile", lambda: profile)

    def fail_thread(*args, **kwargs):
        raise AssertionError("headful relogin should not start without a GUI")

    monkeypatch.setattr(auth.threading, "Thread", fail_thread)

    result = auth._relogin_start("steam")

    assert result["ok"] is False
    assert result["code"] == "manual_cookie_required"
    assert result["manual_cookie_required"] is True


def test_occupied_port_does_not_create_another_gui_or_backend(monkeypatch, capsys):
    from app import main

    monkeypatch.setattr(main, "check_runtime_dependencies", lambda: None)
    monkeypatch.setattr(main, "_ensure_disclaimer", lambda: None)
    monkeypatch.setattr(main, "get_runtime_profile", lambda: SimpleNamespace(
        mode="desktop", reason="test", open_browser=True))
    monkeypatch.setattr(main, "get_bind_host", lambda _profile: "127.0.0.1")
    monkeypatch.setattr(main, "get_port", lambda: 28472)
    monkeypatch.setattr(main, "_is_server_listening", lambda _host, _port: True)
    monkeypatch.setattr(main, "_create_server", lambda *_args: pytest.fail("must not launch a second backend"))
    monkeypatch.setattr(main, "_load_webview", lambda: pytest.fail("must not open GUI attached to another process"))
    main.main()
    assert "端口 28472 已被占用" in capsys.readouterr().out


@pytest.mark.parametrize("started", [True, False])
def test_gui_readiness_uses_its_own_server_not_an_existing_port(monkeypatch, started):
    from app import main

    alive = iter([True, False])
    thread = SimpleNamespace(is_alive=lambda: next(alive))
    server = SimpleNamespace(started=started)
    monkeypatch.setattr(main.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(main.socket, "create_connection", lambda *_args, **_kwargs: pytest.fail(
        "must not use another process's listener to decide GUI readiness"))
    assert main._wait_for_server_ready(thread, "127.0.0.1", 28472, server=server) is started
