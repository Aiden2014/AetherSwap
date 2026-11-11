"""Market reads must refresh expired auth before Steam silently uses IP currency."""
import base64
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import pipeline_steps, shared_market
from app.services import steam_auth
from steam import market_orders, request_policy, session as steam_session


STEAM_ID = "76561198000000000"
NAME = "Shadow Case"
FRESH_COOKIES = f"sessionid=new-session; steamLoginSecure={STEAM_ID}%7C%7Cfresh"


@pytest.fixture
def environment(monkeypatch):
    claims = base64.urlsafe_b64encode(json.dumps({"exp": 1}).encode()).decode().rstrip("=")
    credentials = {
        "steam_id": STEAM_ID,
        "cookies": f"sessionid=old; steamLoginSecure={STEAM_ID}%7C%7Cheader.{claims}.signature",
    }
    account = {"id": "account-1"}
    now = [1000.0]
    login = Mock(return_value=(False, "no_creds", "missing credentials"))
    cooldown = request_policy.MarketCooldown(lambda: now[0])
    monkeypatch.setattr(steam_auth, "get_steam_credentials", lambda: credentials.copy())
    monkeypatch.setattr(steam_auth, "get_current_account", lambda: account.copy())
    monkeypatch.setattr(steam_auth, "try_steam_auto_relogin", login)
    monkeypatch.setattr(steam_auth, "_market_login_failure", None)
    monkeypatch.setattr(steam_auth.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(steam_auth, "log", Mock())
    monkeypatch.setattr(request_policy, "steam_market_cooldown", cooldown)
    monkeypatch.setattr(market_orders, "_market_cooldown", cooldown)
    monkeypatch.setattr(market_orders, "_sell_orders_cache", {})
    monkeypatch.setattr(market_orders, "_load_exchange_rates", lambda: {})
    monkeypatch.setattr(market_orders, "get_proxy_manager", lambda: SimpleNamespace(
        get_proxies_for_request=lambda **_kwargs: {"https": "http://proxy.example:8080"},
    ))
    sent = []

    def curl_get(url, **kwargs):
        sent.append((url, kwargs))
        logged_in = FRESH_COOKIES in kwargs["headers"].get("Cookie", "")
        orderbook = {"eCurrency": 23 if logged_in else 13, "rgCompactSellOrders": [500, 20]}
        if "/market/orderbook" in url:
            body = json.dumps({"data": {"success": True, "data": orderbook}})
        else:
            queries = [] if use_action[0] else [{
                "queryKey": ["market", "orderbook", 730, NAME],
                "state": {"data": orderbook},
            }]
            body = "<script id='valve-ssr-data' type='application/json'>" + json.dumps({
                "renderContext": {"queryData": {"queries": queries}},
            }) + "</script>"
        return SimpleNamespace(status_code=200, url=url, headers={}, encoding="utf-8", content=body.encode())

    use_action = [False]
    monkeypatch.setattr(steam_session, "curl_requests", SimpleNamespace(get=curl_get))
    return SimpleNamespace(credentials=credentials, account=account, login=login,
                           now=now, cooldown=cooldown, sent=sent, use_action=use_action)


@pytest.mark.parametrize("use_action", [False, True])
def test_precheck_refreshes_login_and_sends_new_cookies_to_market_transport(environment, use_action):
    environment.use_action[0] = use_action

    def refresh():
        environment.credentials["cookies"] = FRESH_COOKIES
        return True, "auto_ok", "ready"

    environment.login.side_effect = refresh
    result, error = pipeline_steps._fetch_steam_sell_data(NAME, {}, return_error=True)

    assert error is None
    assert result["smart_price"] == 5.0
    environment.login.assert_called_once()
    assert len(environment.sent) == (2 if use_action else 1)
    for _, request in environment.sent:
        assert request["headers"]["Cookie"] == FRESH_COOKIES
        assert request["proxies"]["https"] == "http://proxy.example:8080"
    assert any("/market/orderbook" in url for url, _ in environment.sent) is use_action


def test_valid_market_login_does_not_need_password_login(environment):
    environment.credentials["cookies"] = FRESH_COOKIES

    assert steam_auth.get_steam_market_credentials() == environment.credentials
    environment.login.assert_not_called()


def test_failed_market_login_stops_public_reads_and_limits_repeated_login_attempts(environment):
    for _ in range(2):
        result, error = pipeline_steps._fetch_steam_sell_data(NAME, {}, return_error=True)
        assert result is None
        assert "登录 Cookie 已过期" in error
        assert "设置" in error
    environment.login.assert_called_once()
    assert environment.sent == []

    environment.now[0] += 31
    with pytest.raises(RuntimeError, match="登录 Cookie 已过期"):
        steam_auth.get_steam_market_credentials()
    assert environment.login.call_count == 2


def test_manual_cookie_update_bypasses_failed_login_backoff(environment):
    with pytest.raises(RuntimeError):
        steam_auth.get_steam_market_credentials()
    environment.credentials["cookies"] = FRESH_COOKIES

    assert steam_auth.get_steam_market_credentials()["cookies"] == FRESH_COOKIES
    environment.login.assert_called_once()


def test_reported_login_success_with_still_expired_cookie_does_not_send_request(environment):
    environment.login.return_value = (True, "session_valid", "ready")

    result, error = pipeline_steps._fetch_steam_sell_data(NAME, {}, return_error=True)

    assert result is None
    assert "登录 Cookie 已过期" in error
    assert environment.sent == []


@pytest.mark.parametrize("change", ["steam_id", "account_id"])
def test_account_change_during_refresh_cancels_market_read(environment, change):
    def refresh():
        environment.credentials["cookies"] = FRESH_COOKIES
        if change == "steam_id":
            environment.credentials["steam_id"] = "76561198000000001"
        else:
            environment.account["id"] = "account-2"
        return True, "auto_ok", "ready"

    environment.login.side_effect = refresh
    result, error = pipeline_steps._fetch_steam_sell_data(NAME, {}, return_error=True)

    assert result is None
    assert "账号已切换" in error
    assert environment.sent == []


def test_market_cooldown_does_not_trigger_password_login(environment):
    environment.cooldown.defer(120)

    with pytest.raises(request_policy.SteamMarketCooldownActive):
        steam_auth.get_steam_market_credentials()

    environment.login.assert_not_called()
    assert environment.sent == []


def test_missing_login_cookie_does_not_send_anonymous_market_request(environment):
    environment.credentials["cookies"] = "sessionid=only-session"

    result, error = pipeline_steps._fetch_steam_sell_data(NAME, {}, return_error=True)

    assert result is None
    assert "登录凭据" in error
    environment.login.assert_not_called()
    assert environment.sent == []


def test_batch_price_lookup_refreshes_login_before_spawning_requests(environment, monkeypatch):
    def refresh():
        environment.credentials["cookies"] = FRESH_COOKIES
        return True, "auto_ok", "ready"

    environment.login.side_effect = refresh
    monkeypatch.setattr(shared_market, "load_app_config_validated", lambda: {})

    assert shared_market.batch_fetch_prices({NAME}) == {NAME: 5.0}
    environment.login.assert_called_once()
    assert environment.sent[0][1]["headers"]["Cookie"] == FRESH_COOKIES


def test_batch_price_lookup_stops_during_market_cooldown(environment):
    environment.cooldown.defer(120)

    assert shared_market.batch_fetch_prices({NAME}) == {}

    environment.login.assert_not_called()
    assert environment.sent == []
