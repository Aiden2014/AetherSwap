from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from app.inventory_holdings import buff_purchase_reference
from app.purchase_market import history_price_at, enrich_purchase_market_prices, verified_wallet_currency


PAID = datetime(2026, 8, 1, 2, tzinfo=timezone.utc).timestamp()


def test_payment_time_precedes_delivery_and_milliseconds_are_normalized():
    assert buff_purchase_reference({'id': 'one', 'buyer_pay_time': PAID * 1000, 'transact_time': PAID + 86400}) == {
        'purchase_id': 'one', 'purchased_at': PAID}
    assert buff_purchase_reference({'id': 'one', 'buyer_pay_time': None, 'transact_time': PAID})['purchased_at'] == PAID
    assert buff_purchase_reference({'id': 'one', 'buyer_pay_time': float('nan'), 'transact_time': -1})['purchased_at'] is None


def test_history_uses_previous_bucket_with_correct_timezone_and_ignores_invalid_rows():
    raw = {'currency': 'CNY', 'history': [
        ['Aug 01 2026 03: +0', 999, '1'], ['Aug 01 2026 08: +8', 3, '5'],
        ['bad', 10], None, ['Aug 01 2026 01: +0', float('nan')],
        ['Aug 01 2026 01: +0', 4.5, '7'],
    ]}
    assert history_price_at(raw, PAID) == {
        'market_price': 4.5, 'market_price_at': PAID - 3600, 'market_price_note': 'Steam 历史成交价参考'}


@pytest.mark.parametrize('raw', [None, {'currency': 'CNY', 'history': []},
    {'currency': None, 'history': [['Aug 01 2026 01: +0', 5]]},
    {'currency': 'CNY', 'history': [['Jul 30 2026 01: +0', 5]]},
    {'currency': 'CNY', 'history': [['Aug 01 2026 03: +0', 5]]},
    {'currency': 'CNY', 'history': [['Aug 01 2026 01: +0', -5]]},
])
def test_unavailable_stale_future_and_unknown_currency_prices_are_not_fabricated(raw):
    assert history_price_at(raw, PAID) == {}


def test_foreign_prices_are_converted_and_labelled_without_claiming_historical_fx(monkeypatch):
    from utils import money
    monkeypatch.setattr(money, '_load_exchange_rates', lambda: {'USD': 7})
    reference = history_price_at({'currency': 'USD', 'history': [['Aug 01 2026 01: +0', 2]]}, PAID)
    assert reference['market_price'] == 14
    assert '当前汇率' in reference['market_price_note']
    assert history_price_at({'currency': 'UNKNOWN', 'history': [['Aug 01 2026 01: +0', 2]]}, PAID) == {}


def test_failed_history_keeps_costs_and_time_and_does_not_query_existing_rows(monkeypatch):
    from app import config_loader
    from app.services import steam_client
    monkeypatch.setattr(config_loader, 'get_steam_credentials', lambda: {'cookies': 'mock'})
    fetch = Mock(side_effect=RuntimeError('secret-cookie'))
    monkeypatch.setattr(steam_client.SteamClient, 'fetch_history', fetch)
    rows = [{'assetid': '1', 'exists': False, 'purchased_at': PAID, 'price': 2},
            {'assetid': '2', 'exists': True, 'purchased_at': PAID, 'price': 3}]
    warning = enrich_purchase_market_prices(rows, {'1': 'One', '2': 'Two'})
    assert '留空' in warning and 'secret-cookie' not in warning
    assert rows[0]['price'] == 2 and rows[0]['purchased_at'] == PAID
    assert 'market_price' not in rows[0]
    fetch.assert_called_once_with('One', return_currency=True)


@pytest.mark.parametrize('patch,expected', [
    ({}, 'CNY'), ({'steam_id': 'other'}, None), ({'region_check_ok': False}, None),
    ({'currency_checked_at': 1}, None), ({'currency_checked_at': float('nan')}, None),
    ({'currency_code': 'JPY'}, 'JPY'),
])
def test_ambiguous_symbol_requires_recent_wallet_verification_for_same_account(monkeypatch, patch, expected):
    import time
    from app import accounts
    account = {'steam_id': '7656', 'currency_code': 'CNY', 'region_check_ok': True,
               'currency_checked_at': time.time(), **patch}
    monkeypatch.setattr(accounts, 'get_current_account', lambda: account)
    assert verified_wallet_currency({'steam_id': '7656'}) == expected


def test_real_history_with_ambiguous_yuan_symbol_uses_verified_wallet_metadata(monkeypatch):
    import time
    from app import accounts, config_loader
    from app.services import steam_client
    monkeypatch.setattr(config_loader, 'get_steam_credentials', lambda: {'steam_id': '7656', 'cookies': 'mock'})
    monkeypatch.setattr(accounts, 'get_current_account', lambda: {
        'steam_id': '7656', 'currency_code': 'CNY', 'region_check_ok': True,
        'currency_checked_at': time.time(),
    })
    raw = {'currency': None, 'history': [['Aug 01 2026 01: +0', 4.28]]}
    monkeypatch.setattr(steam_client.SteamClient, 'fetch_history', lambda *args, **kwargs: raw)
    rows = [{'assetid': '1', 'exists': False, 'purchased_at': PAID}]
    assert enrich_purchase_market_prices(rows, {'1': 'Capsule'}) == ''
    assert rows[0]['market_price'] == 4.28
    assert raw['currency'] is None  # Keep the shared history cache unchanged.


def test_large_import_history_budget_leaves_remaining_prices_optional(monkeypatch):
    from app import config_loader, purchase_market
    from app.services import steam_client
    monkeypatch.setattr(config_loader, 'get_steam_credentials', lambda: {'cookies': 'mock'})
    now = iter([0, 0, 21])
    monkeypatch.setattr(purchase_market.time, 'monotonic', lambda: next(now))
    fetch = Mock(return_value={'currency': 'CNY', 'history': [['Aug 01 2026 01: +0', 4.28]]})
    monkeypatch.setattr(steam_client.SteamClient, 'fetch_history', fetch)
    rows = [{'assetid': '1', 'exists': False, 'purchased_at': PAID},
            {'assetid': '2', 'exists': False, 'purchased_at': PAID}]
    assert '留空' in enrich_purchase_market_prices(rows, {'1': 'One', '2': 'Two'})
    assert rows[0]['market_price'] == 4.28 and 'market_price' not in rows[1]
    fetch.assert_called_once_with('One', return_currency=True)
