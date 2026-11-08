import base64
import json
from unittest.mock import Mock

import pytest
import requests

from app.services import steam_auth
from steam import session


def cookie(claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"steamLoginSecure=76561198000000000%7C%7Cheader.{payload}.signature"


@pytest.mark.parametrize("expiry,expired", [(999, True), (1000, True), (1001, False), (None, False), (True, False), ("999", False)])
def test_cookie_expiry_is_only_a_local_rejection_hint(monkeypatch, expiry, expired):
    monkeypatch.setattr(session.time, "time", lambda: 1000)
    assert session.steam_login_cookie_expired(cookie({"exp": expiry})) is expired


@pytest.mark.parametrize("raw", ["", "sessionid=test", "steamLoginSecure=123%7C%7Ctest", "steamLoginSecure=123%7C%7Ca.invalid.c", cookie([]), cookie({})])
def test_unknown_cookie_formats_still_require_online_verification(raw):
    assert session.steam_login_cookie_expired(raw) is False


def test_expired_cookie_is_rejected_without_network_even_during_cooldown(monkeypatch):
    send = Mock(return_value=requests.Response())
    monkeypatch.setattr(requests.Session, "send", send)
    assert steam_auth._verify_steam_cookies_valid(cookie({"exp": 1}), "76561198000000000") is False
    send.assert_not_called()


def test_expired_cookie_can_complete_password_relogin_during_market_cooldown(monkeypatch):
    from steam import request_policy

    cooldown = request_policy.MarketCooldown(lambda: 1000)
    cooldown.defer(120)
    monkeypatch.setattr(request_policy, "steam_market_cooldown", cooldown)
    account = {"id": "test-account", "steam_id": "76561198000000000", "username": "test", "password": "test"}
    monkeypatch.setattr(steam_auth, "get_current_account", lambda: account.copy())
    monkeypatch.setattr(steam_auth, "get_steam_credentials", lambda: {"cookies": cookie({"exp": 1})})
    monkeypatch.setattr(steam_auth, "set_current", lambda _id: True)
    monkeypatch.setattr(steam_auth, "load_app_config_validated", lambda: {})
    monkeypatch.setattr(steam_auth, "fetch_steam_profile_via_api", lambda *_args: ("", ""))
    monkeypatch.setattr(steam_auth, "update_account", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(steam_auth, "log", lambda *_args, **_kwargs: None)
    login = Mock(return_value=(True, "", {"steamLoginSecure": "76561198000000000%7C%7Cnew", "sessionid": "new"}))
    monkeypatch.setattr(steam_auth, "_do_steampy_login", login)
    write = Mock()
    monkeypatch.setattr(steam_auth, "update_steam_creds", write)
    send = Mock()
    monkeypatch.setattr(requests.Session, "send", send)

    assert steam_auth._try_steam_auto_relogin_impl(account)[0] is True
    login.assert_called_once()
    write.assert_called_once()
    send.assert_not_called()
    assert cooldown.remaining() == 120
