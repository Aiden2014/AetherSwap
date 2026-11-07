"""Exercise the real inventory/auth callers with only HTTP transport replaced."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from app import inventory_cs2
from app.services import steam_auth
from steam.session import create_market_session
from utils import proxy_manager

STEAM_ID = "76561198000000000"
COOKIES = f"steamLoginSecure={STEAM_ID}%7C%7Ctest; sessionid=test"
PROXY = {"http": "http://proxy.example:8080/", "https": "http://proxy.example:8080/"}


@pytest.fixture
def policy(monkeypatch):
    manager = SimpleNamespace(
        get_proxies_for_request=Mock(return_value=PROXY),
        should_always_use_proxy=lambda: True,
        should_use_proxy_on_failure=lambda: False,
    )
    monkeypatch.setattr(proxy_manager, "get_proxy_manager", lambda: manager)
    monkeypatch.setattr(inventory_cs2, "get_steam", lambda: {"steam_id": STEAM_ID, "cookies": COOKIES})
    return manager


def response(request, payload=None, *, status=200, url=None):
    result = requests.Response()
    result.status_code = status
    result.url = url or request.url
    result.request = request
    result._content = json.dumps(payload or {}).encode()
    result.headers["Content-Type"] = "application/json"
    return result


def test_inventory_uses_configured_https_proxy_in_both_contexts(monkeypatch, policy):
    seen = []

    def send(_session, request, **kwargs):
        seen.append(kwargs.get("proxies"))
        if (kwargs.get("proxies") or {}).get("https") != PROXY["https"]:
            raise requests.ConnectTimeout("direct Steam community connection unavailable")
        if request.url.split("?")[0].endswith("/16"):
            return response(request, {"success": 1, "total_inventory_count": 0})
        return response(request, {
            "success": 1,
            "total_inventory_count": 1,
            "assets": [{"assetid": "123", "classid": "456", "appid": 730, "contextid": "2"}],
            "descriptions": [{"classid": "456", "name": "test item", "marketable": 1, "tradable": 1}],
        })

    monkeypatch.setattr(requests.Session, "send", send)
    ok, items, error = inventory_cs2.scan_cs2_inventory()

    assert ok, error
    assert [item["assetid"] for item in items] == ["123"]
    assert len(seen) == 2
    assert all(proxies["https"] == PROXY["https"] for proxies in seen)


def test_cookie_verification_uses_configured_proxy(monkeypatch, policy):
    def send(_session, request, **kwargs):
        if (kwargs.get("proxies") or {}).get("https") != PROXY["https"]:
            raise requests.ConnectTimeout("direct connection unavailable")
        return response(request, url=f"https://steamcommunity.com/profiles/{STEAM_ID}")

    monkeypatch.setattr(requests.Session, "send", send)

    assert steam_auth._verify_steam_cookies_valid(COOKIES, STEAM_ID) is True


def test_profile_fetch_uses_configured_proxy(monkeypatch, policy):
    def send(_session, request, **kwargs):
        if (kwargs.get("proxies") or {}).get("https") != PROXY["https"]:
            raise requests.ConnectTimeout("direct connection unavailable")
        return response(request, {"persona_name": "test user", "avatar_url": "https://avatars.example/test.jpg"})

    monkeypatch.setattr(requests.Session, "send", send)

    assert steam_auth.fetch_steam_profile_via_api(STEAM_ID, COOKIES) == (
        "test user", "https://avatars.example/test.jpg",
    )


def test_local_first_retries_failed_get_through_proxy(monkeypatch, policy):
    policy.should_always_use_proxy = lambda: False
    policy.should_use_proxy_on_failure = lambda: True
    policy.get_proxies_for_request.side_effect = lambda failed=False: PROXY if failed else None
    calls = []

    def send(_session, request, **kwargs):
        calls.append(kwargs.get("proxies"))
        if len(calls) == 1:
            raise requests.ConnectTimeout("direct connection unavailable")
        return response(request)

    monkeypatch.setattr(requests.Session, "send", send)
    with create_market_session(COOKIES, STEAM_ID) as session:
        assert session.get("https://steamcommunity.com/market/", timeout=1).status_code == 200

    assert len(calls) == 2
    assert calls[1]["https"] == PROXY["https"]
    assert policy.get_proxies_for_request.call_args.kwargs == {"failed": True}


def test_local_first_does_not_replay_failed_listing(monkeypatch, policy):
    policy.should_always_use_proxy = lambda: False
    policy.should_use_proxy_on_failure = lambda: True
    policy.get_proxies_for_request.return_value = None
    send = Mock(side_effect=requests.ReadTimeout("listing response unavailable"))
    monkeypatch.setattr(requests.Session, "send", send)

    with create_market_session(COOKIES, STEAM_ID) as session:
        with pytest.raises(requests.ReadTimeout):
            session.post("https://steamcommunity.com/market/sellitem/", data={"assetid": "123"}, timeout=1)

    assert send.call_count == 1


def test_required_proxy_without_nodes_does_not_send_direct(monkeypatch, policy):
    policy.get_proxies_for_request.return_value = None
    send = Mock()
    monkeypatch.setattr(requests.Session, "send", send)

    with create_market_session(COOKIES, STEAM_ID) as session:
        with pytest.raises(requests.exceptions.ProxyError):
            session.get("https://steamcommunity.com/my/profile", timeout=1)

    send.assert_not_called()


@pytest.mark.parametrize("status", [403, 429, 500])
def test_http_errors_do_not_trigger_implicit_retry(monkeypatch, policy, status):
    policy.should_always_use_proxy = lambda: False
    policy.should_use_proxy_on_failure = lambda: True
    policy.get_proxies_for_request.return_value = None
    calls = []

    def send(_session, request, **kwargs):
        calls.append(request)
        return response(request, status=status)

    monkeypatch.setattr(requests.Session, "send", send)
    with create_market_session(COOKIES, STEAM_ID) as session:
        assert session.get("https://steamcommunity.com/market/", timeout=1).status_code == status

    assert len(calls) == 1


def test_explicit_caller_proxy_is_preserved(monkeypatch, policy):
    explicit = {"https": "http://caller.example:8080/"}
    seen = []

    def send(_session, request, **kwargs):
        seen.append(kwargs["proxies"])
        return response(request)

    monkeypatch.setattr(requests.Session, "send", send)
    with create_market_session(COOKIES, STEAM_ID) as session:
        session.get("https://steamcommunity.com/market/", proxies=explicit, timeout=1)

    assert seen[0]["https"] == explicit["https"]
    policy.get_proxies_for_request.assert_not_called()


def test_configured_proxy_overrides_environment_proxy(monkeypatch, policy):
    monkeypatch.setenv("HTTPS_PROXY", "http://environment.example:8080/")
    seen = []

    def send(_session, request, **kwargs):
        seen.append(kwargs["proxies"])
        return response(request)

    monkeypatch.setattr(requests.Session, "send", send)
    with create_market_session(COOKIES, STEAM_ID) as session:
        session.get("https://steamcommunity.com/market/", timeout=1)

    assert seen[0]["https"] == PROXY["https"]


def test_disabled_policy_keeps_direct_connection_without_retry(monkeypatch, policy):
    policy.should_always_use_proxy = lambda: False
    policy.should_use_proxy_on_failure = lambda: False
    policy.get_proxies_for_request.return_value = None
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    # Windows can still discover a system proxy after environment variables clear.
    monkeypatch.setattr(requests.sessions, "get_environ_proxies", lambda *_args, **_kwargs: {})
    seen = []

    def send(_session, request, **kwargs):
        seen.append(kwargs["proxies"])
        raise requests.ConnectTimeout("direct connection unavailable")

    monkeypatch.setattr(requests.Session, "send", send)
    with create_market_session(COOKIES, STEAM_ID) as session:
        with pytest.raises(requests.ConnectTimeout):
            session.get("https://steamcommunity.com/market/", timeout=1)

    assert seen == [{}]
