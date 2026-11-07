import sys
import types
import threading

import pytest


@pytest.fixture(autouse=True)
def _isolated_global_buff_policy(monkeypatch, tmp_path):
    import buff
    from app.services import buff_checkout_guard

    monkeypatch.setattr(buff_checkout_guard, "_GUARD_PATH", tmp_path / "checkout.json")

    policy = buff.BuffRequestPolicy(
        min_interval=0.0,
        state_path=None,
        persist=False,
    )
    monkeypatch.setattr(buff, "get_global_policy", lambda: policy)
    return policy


def test_manual_buff_cookie_requires_session(monkeypatch):
    from app.routes import auth

    saved = []
    monkeypatch.setattr(auth, "update_buff_creds", lambda cookie, **kwargs: saved.append((cookie, kwargs)))

    result = auth.api_auth_manual_cookie("buff", auth.ManualCookieBody(cookies="csrf_token=abc"))

    assert result["ok"] is False
    assert "session" in result["error"]
    assert saved == []


@pytest.mark.parametrize(
    ("step", "should_resume"),
    [
        ("BUFF_AUTH_EXPIRED", True),
        ("BUFF_VERIFICATION_REQUIRED", True),
        ("BUFF_WRITE_RESULT_UNKNOWN", False),
        ("BUFF_ORDER_CREATED_PENDING", False),
        ("CHECKOUT_PENDING", False),
    ],
)
def test_cookie_update_only_resumes_pure_auth_failures(
    monkeypatch,
    step,
    should_resume,
):
    from app import pipeline, state
    from app.routes import auth

    starts = []
    monkeypatch.setattr(
        state,
        "get_status",
        lambda: {"status": "error", "step": step},
    )
    monkeypatch.setattr(
        pipeline,
        "start_pipeline",
        lambda config: starts.append(config) or True,
    )
    monkeypatch.setattr(auth, "load_app_config_validated", lambda: {"test": True})
    monkeypatch.setattr(auth, "set_buff_auth_expired", lambda _value: None)
    monkeypatch.setattr(auth, "set_buff_verification_required", lambda _value: None)

    auth._maybe_resume_after_buff_cookie_update()

    assert bool(starts) is should_resume


def test_relogin_finish_surfaces_worker_error(monkeypatch):
    from app.routes import auth

    done = threading.Event()
    done.set()
    wake = threading.Event()

    monkeypatch.setattr(auth, "_relogin_context", object())
    monkeypatch.setattr(auth, "_relogin_error", "missing login cookie")
    monkeypatch.setattr(auth, "_relogin_done", done)
    monkeypatch.setattr(auth, "_relogin_wake", wake)

    result = auth._relogin_finish(True)

    assert result == {"ok": False, "error": "missing login cookie"}
    assert wake.is_set()


def test_buff_auto_relogin_success_clears_auth_and_verification(monkeypatch):
    from app.services import buff_auth

    calls = []

    class FakePage:
        url = "https://buff.163.com/"

        def goto(self, *args, **kwargs):
            return None

        def wait_for_timeout(self, *args, **kwargs):
            return None

        def title(self):
            return "BUFF"

        def content(self):
            return "<html><body>market</body></html>"

        def evaluate(self, script):
            if "navigator.userAgent" in script:
                return "Test Browser/1.0"
            return {
                "status": 200,
                "url": "https://buff.163.com/account/api/user/info/v2",
                "contentType": "application/json",
                "body": (
                    '{"code":"OK","data":{'
                    '"user_info":{"id":"masked"},"meta_list":{}}}'
                ),
            }

    class FakeContext:
        pages = [FakePage()]

        def cookies(self, urls=None):
            calls.append(("cookie_urls", urls))
            return [
                {"name": "session", "value": "ok"},
                {"name": "csrf_token", "value": "csrf"},
            ]

        def close(self):
            return None

    class FakeChromium:
        def launch_persistent_context(self, *args, **kwargs):
            return FakeContext()

    class FakePlaywright:
        def __enter__(self):
            return types.SimpleNamespace(chromium=FakeChromium())

        def __exit__(self, exc_type, exc, tb):
            return False

    playwright_pkg = types.ModuleType("playwright")
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: FakePlaywright()
    monkeypatch.setitem(sys.modules, "playwright", playwright_pkg)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    monkeypatch.setattr(
        buff_auth,
        "get_buff_credentials",
        lambda: {"cookies": "session=old", "source": "playwright", "user_agent": "Test Browser/1.0"},
    )
    monkeypatch.setattr(
        buff_auth,
        "update_buff_creds",
        lambda cookie, **kwargs: calls.append(("update", cookie, kwargs)),
    )
    monkeypatch.setattr(
        buff_auth,
        "clear_buff_request_policy_after_verification",
        lambda account_id=buff_auth.BUFF_ACCOUNT_ID: calls.append(("clear_policy", account_id)) or True,
    )
    monkeypatch.setattr(buff_auth, "set_buff_auth_expired", lambda value: calls.append(("auth", value)))
    monkeypatch.setattr(
        buff_auth,
        "set_buff_verification_required",
        lambda value, reason="": calls.append(("verify", value, reason)),
    )

    result = buff_auth._try_buff_auto_relogin_impl()

    assert result[0] is True
    assert ("auth", False) in calls
    assert ("verify", False, "") in calls
    assert ("clear_policy", "default") in calls
    assert ("cookie_urls", ["https://buff.163.com/"]) in calls


def test_relogin_context_retries_with_temp_profile(monkeypatch, tmp_path):
    from app.routes import auth

    calls = []

    class FakeChromium:
        def launch_persistent_context(self, profile_dir, **kwargs):
            calls.append(profile_dir)
            if len(calls) == 1:
                raise RuntimeError("BrowserType.launch_persistent_context: Target page, context or browser has been closed")
            return object()

    temp_profile = tmp_path / "temp_profile"
    monkeypatch.setattr(auth.tempfile, "mkdtemp", lambda prefix, dir: str(temp_profile))

    context, temp_dir = auth._launch_relogin_context(
        types.SimpleNamespace(chromium=FakeChromium()),
        tmp_path / "playwright_buff",
        "buff",
    )

    assert context is not None
    assert temp_dir == temp_profile
    assert len(calls) == 2


def test_browser_launch_error_is_user_friendly():
    from app.routes import auth

    raw = (
        "BrowserType.launch_persistent_context: Target page, context or browser has been closed\n"
        "Browser logs:\n<launching> very long chromium command"
    )

    message = auth._friendly_browser_launch_error(RuntimeError(raw), "buff", retried=True)

    assert "Buff" in message
    assert "完整错误见调试日志" in message
    assert "Browser logs" not in message


def test_steam_auto_relogin_does_not_reuse_cookie_from_different_account(monkeypatch):
    from app.services import steam_auth

    calls = []
    monkeypatch.setattr(
        steam_auth,
        "get_current_account",
        lambda: {"id": "acc-new", "username": "new-user", "password": "pw", "steam_id": "222"},
    )
    monkeypatch.setattr(steam_auth, "set_current", lambda account_id: calls.append(("set_current", account_id)))
    monkeypatch.setattr(
        steam_auth,
        "get_steam_credentials",
        lambda: {
            "cookies": "sessionid=old; steamLoginSecure=111%7C%7Cold-token",
            "steam_id": "111",
        },
    )
    monkeypatch.setattr(
        steam_auth,
        "_verify_steam_cookies_valid",
        lambda cookies: (_ for _ in ()).throw(AssertionError("mismatched cookie must not be reused")),
    )
    monkeypatch.setattr(
        steam_auth,
        "_do_steampy_login",
        lambda username, password, guard: (
            True,
            "",
            {"sessionid": "new-session", "steamLoginSecure": "222%7C%7Cnew-token"},
        ),
    )
    monkeypatch.setattr(
        steam_auth,
        "update_steam_creds",
        lambda cookie, session_id, steam_id=None: calls.append(("update_creds", cookie, session_id, steam_id)),
    )
    monkeypatch.setattr(steam_auth, "fetch_steam_profile_via_api", lambda steam_id, cookies: ("New User", "avatar"))
    monkeypatch.setattr(
        steam_auth,
        "update_account",
        lambda account_id, **kwargs: calls.append(("update_account", account_id, kwargs)),
    )
    monkeypatch.setattr(steam_auth, "load_app_config_validated", lambda: {})

    result = steam_auth._try_steam_auto_relogin_impl()

    assert result[0] is True
    assert any(call[0] == "update_creds" and "222%7C%7Cnew-token" in call[1] for call in calls)
