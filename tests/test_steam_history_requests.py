import json

import pytest
import requests

from steam import client


def response(status=200, body=None, headers=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(body).encode()
    result.headers.update({"Content-Type": "application/json", **(headers or {})})
    return result


def test_history_can_report_429_without_exposing_response_secrets(monkeypatch):
    raw = response(429, {"cookie": "secret-cookie"}, {"Retry-After": "120"})
    monkeypatch.setattr(client, "get_market", lambda *_args, **_kwargs: raw)

    with pytest.raises(client.SteamHistoryError) as caught:
        client.fetch_history("Test Item", raise_on_error=True)

    assert caught.value.status_code == 429
    assert caught.value.retry_after == 120
    assert "HTTP 429" in str(caught.value)
    assert "body=object" in str(caught.value)
    assert "application/json" in str(caught.value)
    assert "secret-cookie" not in str(caught.value)
    assert client.fetch_history("Test Item") is None


@pytest.mark.parametrize(
    ("body", "reason"),
    [(None, "unexpected_json"), ([], "unexpected_json"),
     ({"success": False}, "unsuccessful_response"),
     ({"success": True, "prices": "invalid"}, "invalid_prices")],
)
def test_history_reports_invalid_success_responses(monkeypatch, body, reason):
    monkeypatch.setattr(client, "get_market", lambda *_args, **_kwargs: response(body=body))
    with pytest.raises(client.SteamHistoryError, match=reason):
        client.fetch_history("Test Item", raise_on_error=True)
    assert client.fetch_history("Test Item") is None


def test_history_transport_errors_do_not_leak_credentials(monkeypatch):
    def fail(*_args, **_kwargs):
        raise requests.exceptions.ProxyError("http://user:secret-password@example.com")

    monkeypatch.setattr(client, "get_market", fail)
    with pytest.raises(client.SteamHistoryError, match="ProxyError") as caught:
        client.fetch_history("Test Item", raise_on_error=True)
    assert "secret-password" not in str(caught.value)


@pytest.mark.parametrize("status", [400, 401, 403])
def test_history_rejected_login_suggests_refresh_without_claiming_rate_limit(monkeypatch, status):
    monkeypatch.setattr(client, "get_market", lambda *_args, **_kwargs: response(status, []))
    with pytest.raises(client.SteamHistoryError) as caught:
        client.fetch_history("Test Item", raise_on_error=True)

    assert caught.value.status_code == status
    assert caught.value.retryable is False
    assert "刷新 Steam 登录" in str(caught.value)
    assert "429" not in str(caught.value)


@pytest.mark.parametrize("return_currency", [False, True])
def test_history_success_contract_is_unchanged(monkeypatch, return_currency):
    prices = [["Aug 01 2026 01: +0", 10.5, "2"]]
    raw = response(body={"success": True, "prices": prices, "price_prefix": "$"})
    sent = []

    def get(_url, **kwargs):
        sent.append(kwargs)
        return raw

    monkeypatch.setattr(client, "get_market", get)
    result = client.fetch_history("Test Item", return_currency=return_currency)
    assert result == ({"history": prices, "currency": "USD"} if return_currency else prices)
    assert sent[0]["headers"]["Accept-Language"] == "zh-CN,zh;q=0.9"


def test_history_sends_current_browser_headers_and_login_through_market_transport(monkeypatch):
    from types import SimpleNamespace
    from steam import session as steam_session

    sent = []
    prices = [["Aug 01 2026 01: +0", 10.5, "2"]]

    def curl_get(url, **kwargs):
        sent.append((url, kwargs))
        return SimpleNamespace(
            status_code=200, url=url, headers={"Content-Type": "application/json"},
            encoding="utf-8", content=json.dumps({"success": True, "prices": prices}).encode(),
        )

    monkeypatch.setattr(steam_session, "curl_requests", SimpleNamespace(get=curl_get))
    proxies = {"https": "http://proxy.example:8080/"}
    assert client.fetch_history(
        "Tec-9 | Red Quartz (Factory New)", cookies="sessionid=test; steamLoginSecure=test-login",
        proxies=proxies, raise_on_error=True,
    ) == prices
    assert len(sent) == 1
    url, request = sent[0]
    assert "/market/pricehistory/?appid=730&market_hash_name=Tec-9%20%7C%20Red%20Quartz%20%28Factory%20New%29" in url
    assert "Chrome/156." in request["headers"]["User-Agent"]
    assert request["headers"]["Accept-Language"] == "zh-CN,zh;q=0.9"
    assert "steamLoginSecure=test-login" in request["headers"]["Cookie"]
    assert request["proxies"]["https"] == proxies["https"]
    assert request["impersonate"] == "chrome120"
