import base64
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from app.services import steam_client as service
from steam import client
from steam.request_policy import MarketCooldown


def response(status=200, body=None, headers=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(body).encode()
    result.headers.update({"Content-Type": "application/json", **(headers or {})})
    return result


@pytest.fixture
def environment(monkeypatch):
    from app import state
    from utils import delay, proxy_manager

    now = [1000.0]
    credentials = {"steam_id": "account-a", "cookies": "sessionid=secret-cookie"}
    proxy = Mock()
    proxy.get_proxies_for_request.return_value = {"https": "http://user:secret-proxy@example.com"}
    proxy.is_proxy_enabled.return_value = True
    logs = Mock()
    sleep = Mock()
    monkeypatch.setattr(service, "_history_cache", {})
    monkeypatch.setattr(service, "_history_auth_failure", None)
    monkeypatch.setattr(service, "_history_cooldown", MarketCooldown(lambda: now[0]), raising=False)
    monkeypatch.setattr(service.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(service, "get_steam_credentials", lambda: credentials.copy())
    monkeypatch.setattr(service, "load_app_config_validated", lambda: {})
    monkeypatch.setattr(proxy_manager, "get_proxy_manager", lambda: proxy)
    monkeypatch.setattr(state, "log", logs)
    monkeypatch.setattr(delay, "jittered_sleep", sleep)
    return SimpleNamespace(now=now, credentials=credentials, proxy=proxy, logs=logs, sleep=sleep)


def expired_cookie():
    payload = base64.urlsafe_b64encode(json.dumps({"exp": 1}).encode()).decode().rstrip("=")
    return f"steamLoginSecure=76561198000000000%7C%7Cheader.{payload}.signature"


def test_expired_history_login_refreshes_before_request_and_uses_new_cache_key(monkeypatch, environment):
    from app.services import steam_auth

    environment.credentials["cookies"] = expired_cookie()

    def refresh():
        environment.credentials["cookies"] = "sessionid=refreshed; steamLoginSecure=fresh"
        return True, "auto_ok", "ready"

    login = Mock(side_effect=refresh)
    monkeypatch.setattr(steam_auth, "try_steam_auto_relogin", login)
    get = Mock(return_value=response(body={"success": True, "prices": []}))
    monkeypatch.setattr(client, "get_market", get)

    assert service.SteamClient().fetch_history("Item A") == []
    assert service.SteamClient().fetch_history("Item A") == []
    login.assert_called_once()
    get.assert_called_once()
    assert get.call_args.kwargs["cookies"]["sessionid"] == "refreshed"


def test_failed_history_refresh_is_bounded_across_candidates(monkeypatch, environment):
    from app.services import steam_auth

    environment.credentials["cookies"] = expired_cookie()
    login = Mock(return_value=(False, "network_error", "network unavailable"))
    monkeypatch.setattr(steam_auth, "try_steam_auto_relogin", login)
    get = Mock()
    monkeypatch.setattr(client, "get_market", get)

    assert service.SteamClient().fetch_history("Item A") is None
    assert service.SteamClient().fetch_history("Item B") is None
    login.assert_called_once()
    get.assert_not_called()
    environment.now[0] += 31
    assert service.SteamClient().fetch_history("Item C") is None
    assert login.call_count == 2
    assert environment.logs.call_args_list


def test_account_switch_during_history_refresh_does_not_send_with_other_credentials(monkeypatch, environment):
    from app.services import steam_auth

    environment.credentials["cookies"] = expired_cookie()

    def refresh():
        environment.credentials.update(steam_id="account-b", cookies="steamLoginSecure=other-account")
        return True, "auto_ok", "ready"

    monkeypatch.setattr(steam_auth, "try_steam_auto_relogin", refresh)
    get = Mock()
    monkeypatch.setattr(client, "get_market", get)
    assert service.SteamClient().fetch_history("Item A") is None
    get.assert_not_called()


def test_expired_history_does_not_refresh_automatically_during_cooldown(monkeypatch, environment):
    from app.services import steam_auth

    environment.credentials["cookies"] = expired_cookie()
    environment.cooldown = service._history_cooldown
    environment.cooldown.defer(120)
    login = Mock()
    monkeypatch.setattr(steam_auth, "try_steam_auto_relogin", login)
    get = Mock()
    monkeypatch.setattr(client, "get_market", get)

    assert service.SteamClient().fetch_history("Item A") is None
    login.assert_not_called()
    get.assert_not_called()
    assert environment.cooldown.remaining() == 120


def test_history_429_stops_retries_and_cools_down_other_candidates(monkeypatch, environment):
    get = Mock(return_value=response(429, None, {"Retry-After": "120"}))
    monkeypatch.setattr(client, "get_market", get)

    assert service.SteamClient().fetch_history("Item A") is None
    assert service.SteamClient().fetch_history("Item B") is None
    assert get.call_count == 1
    assert environment.proxy.get_proxies_for_request.call_count == 1
    environment.sleep.assert_not_called()

    messages = " ".join(call.args[0] for call in environment.logs.call_args_list)
    assert "HTTP 429" in messages
    assert "body=null" in messages
    assert "secret-cookie" not in messages
    assert "secret-proxy" not in messages

    environment.now[0] += 121
    get.return_value = response(body={"success": True, "prices": []})
    assert service.SteamClient().fetch_history("Item B") == []
    assert get.call_count == 2


def test_failed_history_is_reused_across_clients_then_expires(monkeypatch, environment):
    get = Mock(return_value=response(503, None))
    monkeypatch.setattr(client, "get_market", get)

    assert service.SteamClient().fetch_history("Item A", return_currency=True) is None
    assert service.SteamClient().fetch_history("Item A", return_currency=True) is None
    assert get.call_count == 2
    environment.now[0] += 31
    assert service.SteamClient().fetch_history("Item A", return_currency=True) is None
    assert get.call_count == 4


@pytest.mark.parametrize("raw", [response(403, None), response(200, None)])
def test_nontransient_history_failure_is_not_retried(monkeypatch, environment, raw):
    get = Mock(return_value=raw)
    monkeypatch.setattr(client, "get_market", get)
    assert service.SteamClient().fetch_history("Item A") is None
    assert get.call_count == 1
    environment.sleep.assert_not_called()


def test_history_cache_isolated_after_cookie_or_account_change(monkeypatch, environment):
    get = Mock(return_value=response(body={"success": True, "prices": []}))
    monkeypatch.setattr(client, "get_market", get)
    history = service.SteamClient()
    assert history.fetch_history("Item A") == []
    assert history.fetch_history("Item A") == []
    assert get.call_count == 1

    environment.credentials["steam_id"] = "account-b"
    assert history.fetch_history("Item A") == []
    assert get.call_count == 2
    environment.credentials["cookies"] = "sessionid=refreshed-cookie"
    assert history.fetch_history("Item A") == []
    assert get.call_count == 3


def test_history_cache_respects_each_clients_ttl(monkeypatch, environment):
    get = Mock(return_value=response(body={"success": True, "prices": []}))
    monkeypatch.setattr(client, "get_market", get)
    assert service.SteamClient(cache_ttl=300).fetch_history("Item A") == []
    environment.now[0] += 6
    assert service.SteamClient(cache_ttl=5).fetch_history("Item A") == []
    assert get.call_count == 2


def test_short_429_cooldown_can_retry_same_item_after_server_delay(monkeypatch, environment):
    get = Mock(return_value=response(429, None, {"Retry-After": "5"}))
    monkeypatch.setattr(client, "get_market", get)
    assert service.SteamClient().fetch_history("Item A") is None
    environment.credentials["cookies"] = "sessionid=new-cookie"
    assert service.SteamClient().fetch_history("Item A") is None
    assert get.call_count == 1
    environment.credentials["cookies"] = "sessionid=secret-cookie"
    environment.now[0] += 6
    get.return_value = response(body={"success": True, "prices": []})
    assert service.SteamClient().fetch_history("Item A") == []
    assert get.call_count == 2


def test_concurrent_history_cache_misses_send_one_request(monkeypatch, environment):
    start = threading.Barrier(4)

    def get(*_args, **_kwargs):
        time.sleep(0.03)
        return response(body={"success": True, "prices": []})

    send = Mock(side_effect=get)
    monkeypatch.setattr(client, "get_market", send)

    def fetch():
        start.wait(timeout=3)
        return service.SteamClient().fetch_history("Item A")

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(fetch) for _ in range(4)]
        assert [future.result(timeout=3) for future in futures] == [[], [], [], []]
    assert send.call_count == 1


def test_history_transport_retry_can_recover(monkeypatch, environment):
    get = Mock(side_effect=[
        requests.exceptions.Timeout("secret-cookie"),
        response(body={"success": True, "prices": []}),
    ])
    monkeypatch.setattr(client, "get_market", get)
    assert service.SteamClient().fetch_history("Item A") == []
    assert get.call_count == 2
    environment.sleep.assert_called_once()
    messages = " ".join(call.args[0] for call in environment.logs.call_args_list)
    assert "Timeout" in messages
    assert "secret-cookie" not in messages
