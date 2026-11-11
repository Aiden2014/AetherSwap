"""Account currency fallback for automatic and manual Steam sale readback."""
import json
from types import SimpleNamespace

import pytest

from app import accounts, repair_error_records, steam_listings


STEAM_ID = "76561198000000000"
COOKIES = f"steamLoginSecure={STEAM_ID}%7C%7Ctest; sessionid=test"


@pytest.fixture(params=["automatic", "rebuild"])
def history(monkeypatch, tmp_path, request):
    account = {"id": "account-1", "steam_id": STEAM_ID, "currency_code": "USD"}
    rates = {"USD": 7.0, "HKD": 0.9, "INR": 0.08}
    monkeypatch.setattr(accounts, "get_current_account", lambda: account.copy())
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "exchange_rate.json").write_text(json.dumps({"rates": rates}), encoding="utf-8")
    monkeypatch.setattr(steam_listings, "__file__", str(tmp_path / "app" / "steam_listings.py"))
    monkeypatch.setattr(repair_error_records, "_load_rate_map", lambda: rates)
    environment = SimpleNamespace(account=account, rates=rates, on_response=lambda: None)

    def read(price, cookies=COOKIES):
        data = {
            "success": True,
            "total_count": 1,
            "results_html": f"""
                <div class="market_listing_row" id="history_row_1_0">
                  <span class="market_listing_item_name">Test Item</span>
                  <div class="market_listing_listed_date_combined">Sold</div>
                  <span class="market_listing_price">{price}</span>
                </div>
            """,
            "hovers": "CreateItemHoverFromContainer( g_rgAssets, 'history_row_1_0_name', 730, '2', '123' );",
        }

        def respond(*_args, **_kwargs):
            environment.on_response()
            return SimpleNamespace(status_code=200, text=json.dumps(data), json=lambda: data)

        if request.param == "automatic":
            monkeypatch.setattr(steam_listings, "_get_with_retry", respond)
            ok, sold, error = steam_listings.fetch_my_history_sold(cookies)
            assert ok, error
        else:
            monkeypatch.setattr(repair_error_records.requests, "get", respond)
            sold, _ = repair_error_records._fetch_sold_with_names(
                repair_error_records._cookies_to_dict(cookies),
            )
        return sold

    environment.read = read
    return environment


@pytest.mark.parametrize("currency,expected", [("USD", 80.5), ("CNY", 11.5), (" inr ", 0.92)])
def test_price_without_unit_uses_current_account_currency(history, currency, expected):
    history.account["currency_code"] = currency

    assert history.read(" 10.00 ") == {"123": expected}


@pytest.mark.parametrize("price,expected", [("US$ 10.00", 80.5), ("¥ 10.00", 11.5),
                                            ("HK$ 10.00", 10.35), ("₹ 10.00", 0.92)])
def test_explicit_price_currency_takes_precedence_over_account_currency(history, price, expected):
    history.account["currency_code"] = "JPY"

    assert history.read(price) == {"123": expected}


@pytest.mark.parametrize("context", ["no_account", "no_currency", "different_account"])
def test_price_without_unit_is_skipped_when_account_currency_cannot_be_matched(history, context):
    if context == "no_account":
        history.account.clear()
    elif context == "no_currency":
        history.account.pop("currency_code")
    else:
        history.account.update(id="account-2", steam_id="76561198000000001", currency_code="INR")

    assert history.read("10.00") == {}


def test_current_account_currency_is_captured_before_history_request(history):
    history.on_response = lambda: history.account.update(
        id="account-2", steam_id="76561198000000001", currency_code="INR",
    )

    assert history.read("10.00") == {"123": 80.5}


def test_unknown_cookie_account_does_not_use_selected_accounts_currency(history):
    assert history.read("10.00", cookies="steamLoginSecure=unknown; sessionid=test") == {}


def test_missing_exchange_rate_does_not_record_foreign_amount_as_cny(history):
    history.account["currency_code"] = "SGD"

    assert history.read("10.00") == {}


def test_unknown_explicit_unit_is_not_treated_as_an_unlabelled_price(history):
    assert history.read("XYZ 10.00") == {}


def test_explicit_cny_price_still_works_without_current_account(history):
    history.account.clear()

    assert history.read("¥ 10.00") == {"123": 11.5}
