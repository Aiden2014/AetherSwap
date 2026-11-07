import json
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from steam.client import (  # noqa: E402
    market_hash_name_from_listing_url,
    resolve_market_hash_name_from_listing_url,
    SteamListingResolutionError,
)


@pytest.fixture(autouse=True)
def isolate_market_cooldown(monkeypatch):
    from steam import client
    from steam import request_policy

    cooldown = request_policy.MarketCooldown()
    monkeypatch.setattr(client, "steam_market_cooldown", cooldown)
    monkeypatch.setattr(request_policy, "steam_market_cooldown", cooldown)
    return cooldown


def _ssr_html(queries):
    ctx = {
        "queryData": json.dumps({"queries": queries}, ensure_ascii=False),
        "localizationSettings": {},
    }
    return f"window.SSR.renderContext=JSON.parse({json.dumps(json.dumps(ctx, ensure_ascii=False))});"


def _description(name, tags):
    return {
        "queryKey": ["market", "description", 730, name],
        "state": {
            "data": {
                "market_hash_name": name,
                "market_bucket_group_id": "G1802202E3004",
                "tags": [{"internal_name": tag} for tag in tags],
            }
        },
    }


def _orderbook(name):
    return {
        "queryKey": ["market", "orderbook", 730, name],
        "state": {"data": {"rgCompactSellOrders": [158, 1], "eCurrency": 23}},
    }


class DummyResponse:
    status_code = 200

    def __init__(self, text):
        self.text = text


class DummySession:
    def __init__(self, html):
        self.html = html
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return DummyResponse(self.html)


def test_market_hash_name_from_old_listing_url():
    url = "https://steamcommunity.com/market/listings/730/AK-47%20%7C%20Redline%20%28Field-Tested%29"

    assert market_hash_name_from_listing_url(url) == "AK-47 | Redline (Field-Tested)"


def test_market_hash_name_from_group_listing_url_is_not_used_as_name():
    url = "https://steamcommunity.com/market/listings/730/G1802202E3004"

    assert market_hash_name_from_listing_url(url) is None


def test_resolve_group_listing_url_uses_prefetched_orderbook_name():
    html = _ssr_html(
        [
            _description("Souvenir Dual Berettas | Contractor (Minimal Wear)", ["tournament", "WearCategory1"]),
            _description("Dual Berettas | Contractor (Field-Tested)", ["normal", "WearCategory2"]),
            _orderbook("Dual Berettas | Contractor (Factory New)"),
        ]
    )
    session = DummySession(html)

    name = resolve_market_hash_name_from_listing_url(
        "https://steamcommunity.com/market/listings/730/G1802202E3004",
        session=session,
    )

    assert name == "Dual Berettas | Contractor (Factory New)"
    assert session.urls == ["https://steamcommunity.com/market/listings/730/G1802202E3004"]


def test_resolve_group_listing_url_honors_category_filters():
    html = _ssr_html(
        [
            _description("Souvenir Dual Berettas | Contractor (Minimal Wear)", ["tournament", "WearCategory1"]),
            _description("Dual Berettas | Contractor (Minimal Wear)", ["normal", "WearCategory1"]),
            _orderbook("Dual Berettas | Contractor (Factory New)"),
        ]
    )

    name = resolve_market_hash_name_from_listing_url(
        "https://steamcommunity.com/market/listings/730/G1802202E3004"
        "?category_730_Exterior=tag_WearCategory1&category_730_Quality=tag_normal",
        session=DummySession(html),
    )

    assert name == "Dual Berettas | Contractor (Minimal Wear)"


def _current_ssr_html(queries, *, query_data_object=False):
    data = {"queries": queries}
    ctx = {"queryData": data if query_data_object else json.dumps(data)}
    return (
        "<script type='application/json' nonce='test' id='valve-ssr-data'>"
        + json.dumps({"renderContext": ctx}, ensure_ascii=False)
        + "</script>"
    )


@pytest.mark.parametrize("query_data_object", [False, True])
def test_current_group_link_resolves_selected_exterior_and_stattrak(query_data_object):
    name = "StatTrak™ USP-S | Ticket to Hell (Field-Tested)"
    descriptions = [
        _description("USP-S | Ticket to Hell (Field-Tested)", ["normal", "WearCategory2"]),
        _description("StatTrak™ USP-S | Ticket to Hell (Factory New)", ["strange", "WearCategory0"]),
        _description(name, ["strange", "WearCategory2"]),
    ]
    for query in descriptions:
        query["state"]["data"]["market_bucket_group_id"] = "G183D20F0083004"
    html = _current_ssr_html(
        [*descriptions, _orderbook("USP-S | Ticket to Hell (Factory New)")],
        query_data_object=query_data_object,
    )
    url = (
        "https://steamcommunity.com/market/listings/730/G183D20F0083004"
        "?category_Exterior=WearCategory2&category_Quality=strange&appid=730"
    )
    session = DummySession(html)

    assert resolve_market_hash_name_from_listing_url(url, session=session) == name
    assert session.urls == [url]


def test_current_group_link_accepts_exterior_filter_without_quality():
    name = "USP-S | Ticket to Hell (Field-Tested)"
    queries = [_description(name, ["normal", "WearCategory2"])]
    queries[0]["state"]["data"]["market_bucket_group_id"] = "G183D20F0083004"
    url = (
        "https://steamcommunity.com/market/listings/730/G183D20F0083004"
        "?category_Exterior=WearCategory2&appid=730"
    )
    assert resolve_market_hash_name_from_listing_url(
        url, session=DummySession(_current_ssr_html(queries))
    ) == name


@pytest.mark.parametrize("html_builder", [_ssr_html, _current_ssr_html])
def test_filtered_group_never_falls_back_to_wrong_wear_or_quality(html_builder):
    html = html_builder([
        _description("USP-S | Ticket to Hell (Factory New)", ["normal", "WearCategory0"]),
        _orderbook("USP-S | Ticket to Hell (Factory New)"),
    ])
    url = (
        "https://steamcommunity.com/market/listings/730/G1802202E3004"
        "?category_Exterior=WearCategory2&category_Quality=strange"
    )
    assert resolve_market_hash_name_from_listing_url(url, session=DummySession(html)) is None


def test_group_with_multiple_matching_qualities_requires_selection():
    html = _current_ssr_html([
        _description("USP-S | Ticket to Hell (Field-Tested)", ["normal", "WearCategory2"]),
        _description("StatTrak™ USP-S | Ticket to Hell (Field-Tested)", ["strange", "WearCategory2"]),
    ])
    url = "https://steamcommunity.com/market/listings/730/G1802202E3004?category_Exterior=WearCategory2"
    assert resolve_market_hash_name_from_listing_url(url, session=DummySession(html)) is None


def test_bare_group_with_multiple_descriptions_never_picks_arbitrary_name():
    html = _current_ssr_html([
        _description("USP-S | Ticket to Hell (Field-Tested)", ["normal", "WearCategory2"]),
        _description("USP-S | Ticket to Hell (Factory New)", ["normal", "WearCategory0"]),
    ])
    url = "https://steamcommunity.com/market/listings/730/G1802202E3004"
    assert resolve_market_hash_name_from_listing_url(url, session=DummySession(html)) is None


def test_duplicate_descriptions_for_same_item_are_unambiguous():
    name = "StatTrak™ USP-S | Ticket to Hell (Field-Tested)"
    description = _description(name, ["strange", "WearCategory2"])
    html = _current_ssr_html([description, description])
    url = "https://steamcommunity.com/market/listings/730/G1802202E3004"
    assert resolve_market_hash_name_from_listing_url(url, session=DummySession(html)) == name


@pytest.mark.parametrize("changes", [
    {"queryKey": ["market", "description", 440, "Other item"]},
    {"group_id": "G183D20F0083004"},
    {"queryKey": ["unrelated", "description", 730, "Other item"]},
])
def test_group_ignores_unrelated_descriptions(changes):
    description = _description("Other item", ["normal", "WearCategory2"])
    if "group_id" in changes:
        description["state"]["data"]["market_bucket_group_id"] = changes["group_id"]
    else:
        description.update(changes)
    url = "https://steamcommunity.com/market/listings/730/G1802202E3004?category_Exterior=WearCategory2"
    html = _current_ssr_html([description])
    assert resolve_market_hash_name_from_listing_url(url, session=DummySession(html)) is None


@pytest.mark.parametrize("html", [
    "<script id='valve-ssr-data'>{broken}</script>",
    "<script id='valve-ssr-data'>[]</script>",
    _current_ssr_html([None, {}, {"queryKey": ["market", "description", 730, "test"], "state": []}]),
])
def test_invalid_group_data_does_not_produce_a_name(html):
    assert resolve_market_hash_name_from_listing_url(
        "https://steamcommunity.com/market/listings/730/G1802202E3004",
        session=DummySession(html),
    ) is None


def test_old_encoded_listing_resolves_without_network():
    name = "MP5-SD | Gold Leaf (Field-Tested)"
    session = DummySession("")
    url = "https://steamcommunity.com/market/listings/730/MP5-SD%20%7C%20Gold%20Leaf%20%28Field-Tested%29"
    assert resolve_market_hash_name_from_listing_url(url, session=session) == name
    assert session.urls == []


def test_rate_limited_group_response_does_not_produce_a_name():
    class RateLimitedSession:
        def get(self, url, **kwargs):
            response = DummyResponse(_current_ssr_html([_orderbook("Unrelated item")]))
            response.status_code = 429
            return response

    assert resolve_market_hash_name_from_listing_url(
        "https://steamcommunity.com/market/listings/730/G1802202E3004",
        session=RateLimitedSession(),
    ) is None


@pytest.mark.parametrize("status,reason", [(429, "rate_limited"), (403, "http_error"), (503, "http_error")])
def test_listing_resolution_preserves_http_failure_reason(status, reason):
    class Session:
        def get(self, url, **kwargs):
            response = DummyResponse("")
            response.status_code = status
            response.headers = {"Retry-After": "120"}
            return response

    with pytest.raises(SteamListingResolutionError) as error:
        resolve_market_hash_name_from_listing_url(
            "https://steamcommunity.com/market/listings/730/G183D20F0083004"
            "?category_Exterior=WearCategory2&category_Quality=strange&appid=730",
            session=Session(), raise_on_error=True,
        )

    assert error.value.reason == reason
    assert error.value.status_code == status
    assert error.value.retryable == (status == 503)
    if status == 429:
        assert error.value.retry_after == 120
        assert "限流" in str(error.value)
        assert "120" in str(error.value)


@pytest.mark.parametrize("exception,reason", [
    (requests.Timeout("private proxy details"), "timeout"),
    (requests.ConnectionError("private proxy details"), "network_error"),
])
def test_listing_network_errors_are_distinct_and_do_not_expose_connection_details(exception, reason):
    class Session:
        def get(self, url, **kwargs):
            raise exception

    with pytest.raises(SteamListingResolutionError) as error:
        resolve_market_hash_name_from_listing_url(
            "https://steamcommunity.com/market/listings/730/G183D20F0083004",
            session=Session(), raise_on_error=True,
        )

    assert error.value.reason == reason
    assert error.value.retryable is True
    assert "private proxy details" not in str(error.value)


def test_successful_page_without_item_data_reports_resolution_failure():
    with pytest.raises(SteamListingResolutionError) as error:
        resolve_market_hash_name_from_listing_url(
            "https://steamcommunity.com/market/listings/730/G183D20F0083004"
            "?category_Exterior=WearCategory2&category_Quality=strange&appid=730",
            session=DummySession(_current_ssr_html([])), raise_on_error=True,
        )

    assert error.value.reason == "item_not_resolved"
    assert error.value.retryable is False
    assert "请先选定" not in str(error.value)


def test_group_resolution_uses_existing_chrome_market_transport(monkeypatch):
    from types import SimpleNamespace
    from steam import session as steam_session

    name = "StatTrak™ USP-S | Ticket to Hell (Field-Tested)"
    query = _description(name, ["strange", "WearCategory2"])
    query["state"]["data"]["market_bucket_group_id"] = "G183D20F0083004"
    html = _current_ssr_html([query])
    url = (
        "https://steamcommunity.com/market/listings/730/G183D20F0083004"
        "?category_Exterior=WearCategory2&category_Quality=strange&appid=730"
    )
    sent = []

    def curl_get(request_url, **kwargs):
        sent.append((request_url, kwargs))
        return SimpleNamespace(
            status_code=200, url=request_url, headers={"Content-Type": "text/html; charset=utf-8"},
            encoding="utf-8", content=html.encode("utf-8"),
        )

    monkeypatch.setattr(steam_session, "curl_requests", SimpleNamespace(get=curl_get))
    assert resolve_market_hash_name_from_listing_url(url, raise_on_error=True) == name
    assert len(sent) == 1
    assert sent[0][0] == url
    assert sent[0][1]["impersonate"] == "chrome120"
    assert sent[0][1]["headers"]["Accept-Language"] == "en-US,en;q=0.9"
    headers = sent[0][1]["headers"]
    assert "Chrome/156." in headers["User-Agent"]
    assert "Sec-Fetch-Mode" not in headers
    assert "Upgrade-Insecure-Requests" not in headers
    assert "sec-ch-ua" not in headers


def test_shared_cooldown_blocks_group_request_without_creating_session(monkeypatch, isolate_market_cooldown):
    from unittest.mock import Mock
    from steam import client

    isolate_market_cooldown.defer("120")
    before = isolate_market_cooldown.remaining()
    create = Mock()
    monkeypatch.setattr(client, "create_steam_session", create)
    with pytest.raises(SteamListingResolutionError) as error:
        resolve_market_hash_name_from_listing_url(
            "https://steamcommunity.com/market/listings/730/G183D20F0083004", raise_on_error=True,
        )

    assert error.value.reason == "cooldown_active"
    assert error.value.retryable is False
    assert "本次未发送请求" in str(error.value)
    assert 0 < isolate_market_cooldown.remaining() <= before
    create.assert_not_called()


def test_group_429_starts_cooldown_shared_with_other_market_readers():
    from steam import request_policy

    class Session:
        calls = 0

        def get(self, url, **kwargs):
            self.calls += 1
            response = DummyResponse("")
            response.status_code = 429
            response.headers = {"Retry-After": "120"}
            return response

    session = Session()
    url = "https://steamcommunity.com/market/listings/730/G183D20F0083004"
    with pytest.raises(SteamListingResolutionError) as first:
        resolve_market_hash_name_from_listing_url(url, session=session, raise_on_error=True)
    assert first.value.reason == "rate_limited"
    assert 119 < request_policy.steam_market_cooldown.remaining() <= 120
    with pytest.raises(request_policy.SteamMarketCooldownActive):
        request_policy.check_steam_market_cooldown()
    with pytest.raises(SteamListingResolutionError) as second:
        resolve_market_hash_name_from_listing_url(url, session=session, raise_on_error=True)
    assert second.value.reason == "cooldown_active"
    assert session.calls == 1


def test_old_listing_still_resolves_during_market_cooldown(isolate_market_cooldown):
    isolate_market_cooldown.defer("120")
    assert resolve_market_hash_name_from_listing_url(
        "https://steamcommunity.com/market/listings/730/MP5-SD%20%7C%20Gold%20Leaf%20%28Field-Tested%29",
        raise_on_error=True,
    ) == "MP5-SD | Gold Leaf (Field-Tested)"


def test_group_resolution_sends_login_cookie_through_chrome_transport(monkeypatch):
    from types import SimpleNamespace
    from steam import session as steam_session

    name = "StatTrak™ USP-S | Ticket to Hell (Field-Tested)"
    query = _description(name, ["strange", "WearCategory2"])
    html = _current_ssr_html([query])
    sent = []

    def curl_get(url, **kwargs):
        sent.append(kwargs)
        return SimpleNamespace(
            status_code=200, url=url, headers={}, encoding="utf-8", content=html.encode("utf-8"),
        )

    monkeypatch.setattr(steam_session, "curl_requests", SimpleNamespace(get=curl_get))
    url = "https://steamcommunity.com/market/listings/730/G1802202E3004?category_Exterior=WearCategory2&category_Quality=strange"
    assert resolve_market_hash_name_from_listing_url(
        url, cookies="sessionid=test-session; steamLoginSecure=test-login", raise_on_error=True,
    ) == name
    assert "steamLoginSecure=test-login" in sent[0]["headers"]["Cookie"]
    assert "sessionid=test-session" in sent[0]["headers"]["Cookie"]
