import copy

import pytest

from app import config_loader
from app.config_schema import DEFAULTS, _validate_ranges, merge, validate_and_fill
from app.routes import config as config_routes
from app.services.buff_client import create_buff_client_from_config
from buff.buyer import API_BUY, PAY_METHOD_ALIPAY, PAY_METHOD_WECHAT, BuffBuyer
from fastapi import HTTPException


@pytest.mark.parametrize("raw,expected", [
    ({"pay_method": "wechat"}, ["wechat"]),
    ({"pay_method": "alipay"}, ["alipay"]),
    ({"pay_methods": ["wechat", "alipay"]}, ["wechat", "alipay"]),
    ({"pay_methods": ["alipay", "wechat"]}, ["alipay", "wechat"]),
    ({"pay_methods": [" WECHAT ", "wallet", "wechat", None, "alipay"]}, ["wechat", "alipay"]),
    ({"pay_methods": [], "pay_method": "wechat"}, ["wechat"]),
])
def test_payment_config_migration_and_normalization(raw, expected):
    result = _validate_ranges(validate_and_fill(merge(DEFAULTS, {"buff": raw})))
    assert result["buff"]["pay_methods"] == expected
    assert result["buff"]["pay_method"] == expected[0]


def test_payment_preferences_roundtrip_and_legacy_partial_update(monkeypatch):
    stored = {"buff": {"pay_method": "wechat"}}
    monkeypatch.setattr(config_loader, "load_app_config", lambda: copy.deepcopy(stored))
    monkeypatch.setattr(config_loader, "_config_file_revision", lambda: None)

    def save(data):
        stored.clear()
        stored.update(copy.deepcopy(data))

    monkeypatch.setattr(config_loader, "save_app_config", save)
    config_loader._invalidate_config_cache()
    try:
        result = config_routes.api_save_config(config_routes.ConfigBody(
            config={"buff": {"pay_methods": ["alipay", "wechat"]}}))
        assert result["config"]["buff"]["pay_methods"] == ["alipay", "wechat"]
        assert result["config"]["buff"]["pay_method"] == "alipay"
        assert stored["buff"]["pay_methods"] == ["alipay", "wechat"]
        config_loader.update_app_config_validated({"system": {"ui_scale": "0.8"}})
        assert config_loader.load_app_config_validated()["buff"]["pay_methods"] == ["alipay", "wechat"]
        config_loader.update_app_config_validated({"buff": {"pay_method": "wechat"}})
        assert config_loader.load_app_config_validated()["buff"]["pay_methods"] == ["wechat"]
    finally:
        config_loader._invalidate_config_cache()


@pytest.mark.parametrize("methods", [[], None, "alipay", ["wallet"], ["alipay", "alipay"], [{}]])
def test_invalid_payment_selection_cannot_be_saved(monkeypatch, methods):
    monkeypatch.setattr(config_routes, "update_app_config_validated", lambda _: pytest.fail("must not save"))
    with pytest.raises(HTTPException) as exc:
        config_routes.api_save_config(config_routes.ConfigBody(config={"buff": {"pay_methods": methods}}))
    assert exc.value.status_code == 400


def test_client_factory_passes_preferences_to_recreated_buyer(monkeypatch):
    credentials = {"cookies": "session=test; csrf_token=test", "generation": 0}
    monkeypatch.setattr(config_loader, "get_buff_credentials", lambda: credentials.copy())
    client = create_buff_client_from_config(credentials, {"buff": {"pay_methods": ["wechat", "alipay"]}})
    try:
        assert client._pay_methods == ["wechat", "alipay"]
        assert client._buyer.pay_methods == (PAY_METHOD_WECHAT, PAY_METHOD_ALIPAY)
        credentials["generation"] += 1
        assert client._ensure_current_buyer().pay_methods == (PAY_METHOD_WECHAT, PAY_METHOD_ALIPAY)
    finally:
        client.close()


@pytest.mark.parametrize("preferences,methods,expected", [
    ((49, 6), [{"value": 6, "btn_clickable": True}, {"value": 49, "btn_clickable": True}], 49),
    ((6, 49), [{"value": 49, "btn_clickable": True}, {"value": 6, "btn_clickable": True}], 6),
    ((49, 6), [{"value": 49, "btn_clickable": False}, {"value": 6, "btn_clickable": True}], 6),
    ((6, 49), [{"value": 49, "btn_clickable": True}], 49),
    ((49, 6), [{"value": 49, "btn_clickable": True, "error": "unavailable"}, {"value": 6, "btn_clickable": True}], 6),
    ((49,), [{"value": 6, "btn_clickable": True}], None),
    ((49, 6), [{"value": 59, "btn_clickable": True}], None),
])
def test_single_checkout_chooses_enabled_method_before_one_write(monkeypatch, preferences, methods, expected):
    buyer = object.__new__(BuffBuyer)
    buyer.pay_method = preferences[0]
    buyer.pay_methods = preferences
    buyer.steam_id = "steam"
    calls = []

    def request(method, url, **kwargs):
        import json
        assert method == "POST" and url == API_BUY
        calls.append(json.loads(kwargs["data"]))
        return {"code": "OK", "data": {"id": "bill-1"}}

    monkeypatch.setattr(buyer, "_make_request", request)
    monkeypatch.setattr("buff.buyer.jittered_sleep", lambda *_: None)
    monkeypatch.setattr(buyer, "_get_alipay_url", lambda *_: "https://pay.test/alipay")
    monkeypatch.setattr(buyer, "_get_wechat_pay_url", lambda *_: "weixin://pay")
    result = buyer.lock_and_get_pay_url("csgo", 42, "sell-1", "1.00",
                                        preview={"code": "OK", "data": {"pay_methods": methods}})
    if expected is None:
        assert not result["success"] and result["created"] is False
        assert not calls
    else:
        assert result["success"]
        assert result["pay_type"] == ("wechat" if expected == 6 else "alipay")
        assert len(calls) == 1 and calls[0]["pay_method"] == expected


def test_next_single_preview_restores_preferred_method():
    buyer = object.__new__(BuffBuyer)
    buyer.pay_method = 49
    buyer.pay_methods = (49, 6)
    assert not buyer._preview_payment_error({"pay_methods": [{"value": 6, "btn_clickable": True}]})
    assert buyer.pay_method == 6
    assert not buyer._preview_payment_error({"pay_methods": [
        {"value": 6, "btn_clickable": True}, {"value": 49, "btn_clickable": True}]})
    assert buyer.pay_method == 49
