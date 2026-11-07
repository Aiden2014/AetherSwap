"""A Steam 429 must stop endpoint fallbacks and proxy retries immediately."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from app.services import steam_client as history
from steam import market_orders as market
from steam.request_policy import MarketCooldown
from utils import proxy_manager


@pytest.fixture
def environment(monkeypatch):
    now = [1000.0]
    cooldown = MarketCooldown(lambda: now[0])
    monkeypatch.setattr(market, "_market_cooldown", cooldown, raising=False)
    monkeypatch.setattr(history, "_history_cooldown", cooldown)
    monkeypatch.setattr(history, "_history_cache", {})
    monkeypatch.setattr(history, "get_steam_credentials", lambda: {"cookies": "sessionid=test"})
    monkeypatch.setattr(market, "_cb_open_until", 0)
    monkeypatch.setattr(market, "_cb_fail_streak", 0)
    monkeypatch.setattr(market, "db_get_item_nameid", lambda _name: None)
    proxy = {"https": "http://test-proxy:8080"}
    manager = SimpleNamespace(get_proxies_for_request=Mock(return_value=proxy))
    monkeypatch.setattr(market, "get_proxy_manager", lambda: manager)
    monkeypatch.setattr(proxy_manager, "get_proxy_manager", lambda: manager)
    monkeypatch.setattr(market, "jittered_sleep", Mock())
    reply = requests.Response()
    reply.status_code = 429
    reply._content = b"null"
    reply.headers.update({"Content-Type": "application/json", "Retry-After": "120"})
    session = SimpleNamespace(get=Mock(return_value=reply))
    return SimpleNamespace(cooldown=cooldown, now=now, proxy=proxy, manager=manager,
                           session=session, reply=reply)


def test_market_429_stops_fallbacks_and_other_candidates(environment):
    for name in ("Prisma Case", "Next Case"):
        data, error = market.get_sell_orders_cny(environment.session, name, use_cache=False, return_error=True)
        assert data is None
        assert "HTTP 429" in error

    assert environment.session.get.call_count == 1
    assert environment.manager.get_proxies_for_request.call_count == 1
    assert environment.cooldown.remaining() == 120


@pytest.mark.parametrize("endpoint", ["ssr", "action", "nameid", "histogram"])
def test_each_market_endpoint_stops_at_first_429(environment, endpoint):
    session = environment.session
    if endpoint == "ssr":
        data, error = market._fetch_ssr_sell_orders_cny(session, "Prisma Case", 730)
    elif endpoint == "action":
        data, error = market._fetch_action_orderbook_cny(session, "Prisma Case", 730)
    elif endpoint == "nameid":
        data, error = market.get_item_nameid(session, "Prisma Case", use_cache=False, return_error=True)
    else:
        data, error = market.fetch_item_orders_histogram(session, "test-nameid", return_error=True)

    assert data is None
    assert "HTTP 429" in error
    assert session.get.call_count == 1
    assert environment.cooldown.remaining() == 120


def test_market_429_also_suppresses_history_requests(monkeypatch, environment):
    get = Mock()
    monkeypatch.setattr(history, "_fetch_history", get)
    market.get_sell_orders_cny(environment.session, "Prisma Case", use_cache=False)

    assert history.SteamClient().fetch_history("Next Case") is None
    get.assert_not_called()


def test_histogram_uses_configured_proxy_on_first_attempt(environment):
    environment.reply.status_code = 200
    environment.reply._content = b'{"success":1,"sell_order_graph":[[10.5,2]]}'

    data, error = market.fetch_item_orders_histogram(environment.session, "test-nameid", return_error=True)

    assert data
    assert error is None
    assert environment.session.get.call_args.kwargs["proxies"] == environment.proxy
