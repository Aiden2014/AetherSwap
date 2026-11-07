"""Cashier reads share the HTTP session and never open an auth browser."""
import json
import sys
import types

import pytest
import requests

from app.services import buff_auth, buff_client
from buff.buyer import (
    API_BUY, API_BUY_PREVIEW, API_PAGE_PAY, API_STEAM_TRADE, API_USER_INFO,
    API_WX_PAY_QRCODE, BuffBuyer,
)
from buff.request_policy import (
    BuffRequestPolicy, BuffRateLimited, BuffRiskControlTriggered,
    BuffVerificationRequired,
)


def response(payload, status=200, headers=None):
    result = requests.Response()
    result.status_code = status
    result.url = API_PAGE_PAY
    result.encoding = "utf-8"
    result._content = json.dumps(payload).encode()
    result.headers.update(headers or {})
    return result


@pytest.mark.parametrize("pay_method,method_id,endpoint", [
    ("alipay", 49, API_PAGE_PAY), ("wechat", 6, API_WX_PAY_QRCODE),
])
def test_two_checkouts_and_trade_poll_reuse_session_without_playwright(
    monkeypatch, pay_method, method_id, endpoint,
):
    from buff import buyer as buyer_module

    policy = BuffRequestPolicy(min_interval=0, persist=False)
    monkeypatch.setattr(buyer_module, "get_global_policy", lambda: policy)
    monkeypatch.setattr(buyer_module, "jittered_sleep", lambda *_: None)

    def unexpected_browser(*_args, **_kwargs):
        pytest.fail("Payment lookup must never open a browser or re-login")

    monkeypatch.setitem(sys.modules, "playwright.sync_api", types.SimpleNamespace(sync_playwright=unexpected_browser))
    monkeypatch.setattr(buff_auth, "try_buff_auto_relogin", unexpected_browser)
    credentials = {"cookies": "session=current; csrf_token=original", "generation": 0}
    monkeypatch.setattr("app.config_loader.get_buff_credentials", lambda: credentials.copy())
    rotations = []

    def persist(cookies, **kwargs):
        credentials["cookies"] = cookies
        credentials["generation"] += 1
        rotations.append(cookies)

    monkeypatch.setattr("app.config_loader.update_buff_creds", persist)
    client = buff_client.create_buff_client_from_config(credentials, {"buff": {"pay_method": pay_method}})
    calls = []
    created = []

    def request(session, verb, url, **kwargs):
        calls.append((session, verb, url, kwargs))
        if url == API_USER_INFO:
            return response({"code": "OK", "data": {"user_info": {"id": "u", "steamid": "s"}}})
        if url == API_BUY_PREVIEW:
            assert kwargs["headers"]["X-Csrftoken"] == ("rotated" if created else "original")
            return response({"code": "OK", "data": {"pay_methods": [{"value": method_id, "btn_clickable": True}]}})
        if url == API_BUY:
            return response({"code": "OK", "data": {"id": f"bill-{len(created) + 1}"}})
        if url == endpoint:
            assert verb == "GET"
            assert kwargs["params"]["bill_order_id"] == created[-1]
            assert "Buff-Cashier-Trace-ID" not in kwargs["headers"]
            result = response({"code": "OK", "data": {
                "url": "https://pay.example/1?risk_control=0", "captcha": None,
                "elements": {"html": '<script src="/static/challenge.js"></script>'},
            }})
            result.cookies.set("csrf_token", "rotated", domain="buff.163.com", path="/")
            return result
        assert url == API_STEAM_TRADE
        assert kwargs["headers"]["X-Csrftoken"] == "rotated"
        return response({"code": "OK", "data": []})

    monkeypatch.setattr(requests.Session, "request", request)
    try:
        for _ in range(2):
            result = client.lock_and_get_pay_url("csgo", 1, "sell-1", "10.00", on_created=created.append)
            assert result["success"] is True
            assert result["pay_url"].endswith("risk_control=0")
            assert client.get_steam_trades() == []
        assert created == ["bill-1", "bill-2"]
        assert len({id(session) for session, *_ in calls}) == 1
        assert [url for _, method, url, _ in calls if method == "POST"] == [API_BUY, API_BUY]
        assert sum(url == endpoint for _, _, url, _ in calls) == 2
        assert len(rotations) == 1
    finally:
        client.close()


@pytest.mark.parametrize("payload,status,exception,reason", [
    ({"code": "FAIL"}, 429, BuffRateLimited, "频繁"),
    ({"code": "FAIL"}, 403, BuffRiskControlTriggered, "403"),
    ({"code": "FAIL"}, 412, BuffRiskControlTriggered, "412"),
    ({"code": "FAIL", "msg": "captcha required"}, 200, BuffVerificationRequired, "captcha required"),
    ({"code": "OK", "msg": "captcha required"}, 200, BuffVerificationRequired, "captcha required"),
    ({"code": "LOGIN_REQUIRED"}, 401, None, "登录"),
    ({"code": "ERROR", "error": "bill expired"}, 200, None, "bill expired"),
])
def test_payment_failure_keeps_created_order_and_circuit(monkeypatch, payload, status, exception, reason):
    calls = []
    policy = BuffRequestPolicy(min_interval=0, persist=False)
    buyer = BuffBuyer("session=current; csrf_token=c", request_policy=policy)

    def request(verb, url, **kwargs):
        calls.append((verb, url))
        if url == API_BUY:
            return response({"code": "OK", "data": {"id": "created"}})
        assert (verb, url) == ("GET", API_PAGE_PAY)
        return response(payload, status, {"Retry-After": "60"})

    monkeypatch.setattr(buyer.session, "request", request)
    preview = {"code": "OK", "data": {"pay_methods": [{"value": 49, "btn_clickable": True}]}}
    try:
        result = buyer.lock_and_get_pay_url("csgo", 1, "sell-1", "1.00", preview=preview)
        assert result["code"] == "CREATED_WITHOUT_PAY_URL"
        assert result["created"] and result["order_id"] == "created"
        assert reason in result["msg"]
        assert calls == [("POST", API_BUY), ("GET", API_PAGE_PAY)]
        if exception:
            with pytest.raises(exception):
                buyer._make_request("GET", API_PAGE_PAY)
            assert len(calls) == 2
    finally:
        buyer.close()


@pytest.mark.parametrize("data", [
    {"url": " https://pay.example/1 "},
    {"elements_v2": {"alipay": {"url": "https://pay.example/1"}}},
    {"elements_v2": None, "elements": {"url": "https://pay.example/1"}},
    {"elements_v2": {"alipay": None}, "elements": None, "url": "https://pay.example/1"},
])
def test_payment_parser_handles_optional_nested_fields(data):
    assert BuffBuyer._parse_payment_url({"code": "OK", "data": data}, "alipay") == "https://pay.example/1"
