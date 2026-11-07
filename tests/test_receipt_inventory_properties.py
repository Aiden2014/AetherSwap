from types import SimpleNamespace

import pytest
import requests

from app import inventory_cs2
from steam.inventory import _fetch_full_context
from steam.session import _MarketReadAdapter


def test_paginated_inventory_keeps_properties_with_exact_asset_context(monkeypatch):
    pages = iter([
        {"success":1, "assets":[{"contextid":"16", "assetid":"first", "classid":"100"}],
         "asset_properties":[{"contextid":"16", "assetid":"first", "asset_properties":[{"propertyid":2,"float_value":"0.25"}]}],
         "more_items":1, "last_assetid":"first"},
        {"success":1, "assets":[{"contextid":"16", "assetid":"second", "classid":"100"}],
         "asset_properties":[{"contextid":"2", "assetid":"second", "asset_properties":[{"propertyid":2,"float_value":"0.5"}]}]},
    ])
    session=SimpleNamespace(get=lambda *_a, **_k: SimpleNamespace(status_code=200, json=lambda: next(pages)))
    monkeypatch.setattr("utils.delay.jittered_sleep", lambda *_a: None)
    assets, _descs = _fetch_full_context(session,"7656",730,16)
    assert assets[0]["asset_properties"] == [{"propertyid":2,"float_value":"0.25"}]
    assert "asset_properties" not in assets[1]


def test_inventory_scan_exposes_full_wear_seed_and_class(monkeypatch):
    monkeypatch.setattr(inventory_cs2,"get_steam",lambda: {"steam_id":"7656","cookies":"sessionid=test"})
    monkeypatch.setattr(inventory_cs2,"create_market_session",lambda *_a: object())
    monkeypatch.setattr(inventory_cs2,"fetch_cs2_inventory",lambda *_a: {
        "assets":[{"assetid":"new", "classid":"100", "instanceid":"200", "contextid":"16",
                   "asset_properties":[{"propertyid":2,"float_value":"0.315086483955383301"}, {"propertyid":1,"int_value":"805"}]}],
        "descriptions":[{"classid":"100", "instanceid":"200", "market_hash_name":"Test Item", "commodity": 1}],
    })
    ok, items, error=inventory_cs2.scan_cs2_inventory()
    assert ok, error
    assert items[0]["paintwear"] == "0.315086483955383301"
    assert items[0]["paintseed"] == "805"
    assert items[0]["classid"] == "100"
    assert items[0]["commodity"] is True


@pytest.mark.parametrize("method,url,expected", [
    ("GET","https://steamcommunity.com/inventory/7656/730/16",True),
    ("POST","https://steamcommunity.com/tradeoffer/1/accept",False),
    ("GET","https://example.com/inventory/7656/730/16",False),
])
def test_browser_transport_only_selects_steam_reads(method,url,expected):
    request=requests.Request(method,url).prepare()
    assert _MarketReadAdapter._is_market_read(request) is expected
