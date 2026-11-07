import json
from unittest.mock import Mock

import requests
import pytest

from app.routes import transactions
from steam import client


@pytest.fixture(autouse=True)
def isolate_market_cooldown(monkeypatch):
    from steam import request_policy

    cooldown = request_policy.MarketCooldown()
    monkeypatch.setattr(client, "steam_market_cooldown", cooldown)
    monkeypatch.setattr(request_policy, "steam_market_cooldown", cooldown)
    monkeypatch.setattr(transactions, "get_steam_credentials", lambda: {})


def test_manual_purchase_records_english_name_from_current_group_page(monkeypatch):
    name = "StatTrak™ USP-S | Ticket to Hell (Field-Tested)"
    html = "<script id='valve-ssr-data' type='application/json'>" + json.dumps({
        "renderContext": {"queryData": json.dumps({"queries": [{
            "queryKey": ["market", "description", 730, name],
            "state": {"data": {
                "market_hash_name": name,
                "market_bucket_group_id": "G183D20F0083004",
                "tags": [{"internal_name": "WearCategory2"}, {"internal_name": "strange"}],
            }},
        }]})},
    }) + "</script>"

    class Session:
        def get(self, url, **kwargs):
            assert "category_Exterior=WearCategory2" in url
            return type("Response", (), {"status_code": 200, "text": html})()

        def close(self):
            pass

    class ProxyManager:
        def get_proxies_for_request(self, **kwargs):
            return None

    from utils import proxy_manager

    monkeypatch.setattr(client, "create_steam_session", Session)
    monkeypatch.setattr(proxy_manager, "get_proxy_manager", ProxyManager)
    monkeypatch.setattr(transactions, "_fetch_steam_lowest_cny", lambda value: 15.0 if value == name else None)
    records = []
    monkeypatch.setattr(transactions, "append_purchase", records.append)

    result = transactions.api_add_purchase(transactions.AddPurchaseBody(
        steam_link="https://steamcommunity.com/market/listings/730/G183D20F0083004?category_Exterior=WearCategory2&category_Quality=strange&appid=730",
        price=12.34,
        quantity=2,
    ))

    assert result == {"ok": True, "added": 2}
    assert len(records) == 2
    assert all(record["name"] == name and record["price"] == 12.34 and record["market_price"] == 15.0 for record in records)


def test_unresolved_group_explains_selection_and_does_not_add_purchase(monkeypatch):
    monkeypatch.setattr(transactions, "_name_from_steam_link", lambda url: None)
    records = []
    monkeypatch.setattr(transactions, "append_purchase", records.append)
    result = transactions.api_add_purchase(transactions.AddPurchaseBody(
        steam_link="https://steamcommunity.com/market/listings/730/G183D20F0083004", price=12.34,
    ))

    assert result["ok"] is False
    assert "磨损和品质" in result["error"]
    assert records == []


def test_rate_limited_selected_stattrak_link_reports_429_without_immediate_retries(monkeypatch):
    from utils import proxy_manager

    response = Mock(status_code=429, headers={"Retry-After": "120"}, text="")
    session = Mock()
    session.get.return_value = response
    monkeypatch.setattr(client, "create_steam_session", lambda: session)
    manager = Mock()
    manager.get_proxies_for_request.return_value = None
    monkeypatch.setattr(proxy_manager, "get_proxy_manager", lambda: manager)
    append = Mock()
    monkeypatch.setattr(transactions, "append_purchase", append)

    result = transactions.api_add_purchase(transactions.AddPurchaseBody(
        steam_link="https://steamcommunity.com/market/listings/730/G183D20F0083004?category_Exterior=WearCategory2&category_Quality=strange&appid=730",
        price=12.34,
    ))

    assert result["ok"] is False
    assert "HTTP 429" in result["error"]
    assert "限流" in result["error"]
    assert "请先选定" not in result["error"]
    assert session.get.call_count == 1
    session.close.assert_called_once()
    append.assert_not_called()


def test_transient_connection_failure_still_retries_with_configured_proxy(monkeypatch):
    from utils import proxy_manager

    resolver = Mock(side_effect=[
        client.SteamListingResolutionError("network_error"),
        "StatTrak™ USP-S | Ticket to Hell (Field-Tested)",
    ])
    monkeypatch.setattr(client, "resolve_market_hash_name_from_listing_url", resolver)
    manager = Mock()
    manager.get_proxies_for_request.side_effect = [None, {"https": "http://test-proxy"}]
    monkeypatch.setattr(proxy_manager, "get_proxy_manager", lambda: manager)

    name = transactions._name_from_steam_link(
        "https://steamcommunity.com/market/listings/730/G183D20F0083004"
    )

    assert name == "StatTrak™ USP-S | Ticket to Hell (Field-Tested)"
    assert resolver.call_count == 2
    assert resolver.call_args.kwargs["proxies"] == {"https": "http://test-proxy"}
    assert manager.get_proxies_for_request.call_args.kwargs == {"failed": True}


def test_exhausted_timeout_retries_report_network_failure(monkeypatch):
    from utils import proxy_manager

    session = Mock()
    session.get.side_effect = requests.Timeout("private connection details")
    monkeypatch.setattr(client, "create_steam_session", lambda: session)
    manager = Mock()
    manager.get_proxies_for_request.return_value = None
    monkeypatch.setattr(proxy_manager, "get_proxy_manager", lambda: manager)
    append = Mock()
    monkeypatch.setattr(transactions, "append_purchase", append)

    result = transactions.api_add_purchase(transactions.AddPurchaseBody(
        steam_link="https://steamcommunity.com/market/listings/730/G183D20F0083004?category_Exterior=WearCategory2&category_Quality=strange",
        price=12.34,
    ))

    assert result["ok"] is False
    assert "超时" in result["error"]
    assert "private connection details" not in result["error"]
    assert session.get.call_count == 3
    append.assert_not_called()


def test_manual_link_uses_current_account_cookie_without_logging_credentials(monkeypatch):
    from utils import proxy_manager

    cookies = "sessionid=test-session; steamLoginSecure=test-login-secret"
    monkeypatch.setattr(transactions, "get_steam_credentials", lambda: {"cookies": cookies})
    manager = Mock()
    manager.get_proxies_for_request.return_value = {"https": "http://user:proxy-secret@proxy.invalid"}
    monkeypatch.setattr(proxy_manager, "get_proxy_manager", lambda: manager)
    resolve = Mock(side_effect=client.SteamListingResolutionError("rate_limited", status_code=429))
    monkeypatch.setattr(client, "resolve_market_hash_name_from_listing_url", resolve)
    logs = []
    monkeypatch.setattr(transactions, "log", lambda message, *args, **kwargs: logs.append(message))

    result = transactions.api_add_purchase(transactions.AddPurchaseBody(
        steam_link="https://steamcommunity.com/market/listings/730/G183D20F0083004?category_Exterior=WearCategory2&category_Quality=strange",
        price=12.34,
    ))

    assert result["ok"] is False
    assert resolve.call_args.kwargs["cookies"] == cookies
    assert resolve.call_count == 1
    assert "登录Cookie=已配置" in logs[0]
    assert "代理=已指定" in logs[0]
    assert all(secret not in logs[0] + result["error"] for secret in ["test-login-secret", "proxy-secret"])
