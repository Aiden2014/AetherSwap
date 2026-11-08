"""Cooldown must suppress actual HTTP calls, including background entrypoints."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from app import gift_engine, state, steam_listings
from app.routes import inventory
from steam import request_policy
from steam.session import create_steam_session


@pytest.fixture
def cooling(monkeypatch):
    now = [1000.0]
    cooldown = request_policy.MarketCooldown(lambda: now[0])
    cooldown.defer(120)
    monkeypatch.setattr(request_policy, "steam_market_cooldown", cooldown)
    monkeypatch.setattr(gift_engine, "steam_market_cooldown", cooldown)
    monkeypatch.setattr(steam_listings, "steam_market_cooldown", cooldown)
    app_state = state.State()
    app_state.set_status("running", "STEAM_COOLDOWN", progress_total=45, progress_done=8)
    monkeypatch.setattr(state, "get_state", lambda: app_state)
    return SimpleNamespace(now=now, cooldown=cooldown, state=app_state)


def test_inventory_refresh_during_cooldown_does_not_scan(monkeypatch, cooling):
    scan = Mock(return_value=(False, [], "test request"))
    monkeypatch.setattr(inventory, "scan_cs2_inventory", scan)

    result = inventory.api_inventory(refresh=True)

    scan.assert_not_called()
    assert result["source"] == "cache"


def test_stopping_pipeline_does_not_reenable_background_requests(cooling):
    cooling.state.set_status("stopped", "已停止")
    assert not cooling.state.is_steam_background_allowed()


def test_persistent_rate_limit_error_keeps_background_requests_disabled(cooling):
    cooling.now[0] += 121
    cooling.state.set_status("error", "STEAM_RATE_LIMITED")
    assert not cooling.state.is_steam_background_allowed()


def test_community_session_does_not_send_during_cooldown(monkeypatch, cooling):
    send = Mock(return_value=requests.Response())
    monkeypatch.setattr(requests.Session, "send", send)

    with create_steam_session() as session:
        with pytest.raises(requests.RequestException, match="429"):
            session.get("https://steamcommunity.com/my/profile", timeout=1)

    send.assert_not_called()


def test_explicit_auth_check_is_available_during_market_cooldown(monkeypatch, cooling):
    reply = requests.Response()
    reply.status_code = 200
    send = Mock(return_value=reply)
    monkeypatch.setattr(requests.Session, "send", send)

    with create_steam_session(allow_auth_during_cooldown=True) as session:
        assert session.get("https://steamcommunity.com/my/profile", proxies={}, timeout=1).status_code == 200

    send.assert_called_once()
    assert cooling.cooldown.remaining() == 120


@pytest.mark.parametrize("method,path", [
    ("GET", "/market/pricehistory/"), ("GET", "/market/mylistings/"),
    ("GET", "/inventory/123/730/2"), ("POST", "/my/profile"),
])
def test_auth_session_still_respects_market_cooldown(monkeypatch, cooling, method, path):
    send = Mock()
    monkeypatch.setattr(requests.Session, "send", send)
    with create_steam_session(allow_auth_during_cooldown=True) as session:
        with pytest.raises(requests.RequestException, match="429"):
            session.request(method, "https://steamcommunity.com" + path, proxies={}, timeout=1)
    send.assert_not_called()


def test_real_cookie_verification_works_during_market_cooldown(monkeypatch, cooling):
    from app.services import steam_auth

    reply = requests.Response()
    reply.status_code = 200
    reply.url = "https://steamcommunity.com/profiles/76561198000000000"
    send = Mock(return_value=reply)
    monkeypatch.setattr(requests.Session, "send", send)

    assert steam_auth._verify_steam_cookies_valid(
        "steamLoginSecure=76561198000000000%7C%7Ctest", "76561198000000000",
    ) is True
    send.assert_called_once()
    assert cooling.cooldown.remaining() == 120


def test_listings_request_does_not_send_during_cooldown(monkeypatch, cooling):
    get = Mock()
    monkeypatch.setattr(steam_listings.requests, "get", get)

    try:
        steam_listings._get_with_retry(steam_listings.MYLISTINGS_URL, {}, {}, {}, None)
    except requests.RequestException:
        pass

    get.assert_not_called()


def test_wallet_request_does_not_send_during_cooldown(monkeypatch, cooling):
    get = Mock()
    monkeypatch.setattr(gift_engine.requests, "get", get)

    try:
        gift_engine.get_wallet_balance("sessionid=test")
    except Exception:
        pass

    get.assert_not_called()


def test_other_services_are_not_blocked_by_steam_cooldown(monkeypatch, cooling):
    reply = requests.Response()
    reply.status_code = 200
    send = Mock(return_value=reply)
    monkeypatch.setattr(requests.Session, "send", send)
    # Isolate proxy routing; this test exercises only the Steam-specific guard.
    with create_steam_session() as session:
        result = session.get("https://example.com/", proxies={}, timeout=1)

    assert result.status_code == 200
    send.assert_called_once()


def test_wallet_429_does_not_fall_back_to_other_endpoints(monkeypatch, cooling):
    cooling.now[0] += 121
    reply = requests.Response()
    reply.status_code = 429
    reply._content = b"null"
    get = Mock(return_value=reply)
    monkeypatch.setattr(gift_engine.requests, "get", get)

    with pytest.raises(requests.RequestException, match="429"):
        gift_engine.get_wallet_balance("sessionid=test")

    get.assert_called_once()
