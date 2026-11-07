from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import database, state
from app.inventory_holdings import match_buff_purchases, buff_commodity_prices
from app.routes import inventory


NAME = 'USP-S | Ticket to Hell (Field-Tested)'


def skin(assetid='123', **patch):
    return {'assetid': assetid, 'name': '中文名', 'market_hash_name': NAME,
            'paintwear': '0.125', 'paintseed': 42, **patch}


def order(bill='bill1', assetid='123', **patch):
    return {'id': bill, 'state': 'SUCCESS', 'price': '12.34', 'buyer_steamid': '7656',
            'goods_info': {'market_hash_name': NAME},
            'asset_info': {'assetid': assetid, 'info': {'paintwear': '0.125', 'paintseed': 42}}, **patch}


@pytest.fixture
def db(monkeypatch, tmp_path):
    engine = database.create_engine(f'sqlite:///{tmp_path / "test.db"}', connect_args={'check_same_thread': False})
    database.SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(database, '_engine', engine)
    yield database
    engine.dispose()


@pytest.fixture
def client(monkeypatch, db):
    monkeypatch.setattr(inventory, 'get_inventory', lambda: [skin(), skin('456', paintwear='0.25')])
    monkeypatch.setattr(state, 'get_purchases', db.db_get_purchases)
    monkeypatch.setattr(inventory, 'get_steam_credentials', lambda: {'steam_id': '7656'})
    def post(path, json):
        if path.endswith('/preview'):
            result = inventory.api_inventory_holdings_preview(inventory.InventoryPurchasePreview(**json))
        else:
            result = inventory.api_inventory_add_holdings(inventory.InventoryPurchaseImport(**json))
        return SimpleNamespace(json=lambda: result)
    return SimpleNamespace(post=post)


def test_import_preserves_assetid_english_name_and_per_item_price(client, db):
    result = client.post('/api/inventory/holdings', json={'items': [
        {'assetid': '123', 'price': 12.34}, {'assetid': '456', 'price': 45.67},
    ]}).json()
    assert result == {'ok': True, 'added': ['123', '456'], 'skipped': []}
    rows = db.db_get_purchases()
    assert [(row['assetid'], row['name'], row['price']) for row in rows] == [('123', NAME, 12.34), ('456', NAME, 45.67)]
    assert all(row['pending_receipt'] is False for row in rows)


def test_duplicate_asset_skips_existing_and_within_batch(client, db):
    db.db_append_purchase({'assetid': '123', 'name': NAME, 'price': 9})
    result = client.post('/api/inventory/holdings', json={'items': [
        {'assetid': '123'}, {'assetid': '456', 'price': 11}, {'assetid': '456', 'price': 11},
    ]}).json()
    assert result == {'ok': True, 'added': ['456'], 'skipped': ['123']}
    assert len(db.db_get_purchases()) == 2
    assert db.db_get_purchases()[0]['price'] == 9


def test_concurrent_imports_do_not_duplicate_assets(db):
    def add(_):
        return db.db_import_inventory_purchases([{'assetid': '123', 'name': NAME, 'price': 10}])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(add, range(2)))
    assert sum(len(result['added']) for result in results) == 1
    assert len(db.db_get_purchases()) == 1


@pytest.mark.parametrize('items', [[], [{'assetid': 'missing', 'price': 12}],
    [{'assetid': '123', 'price': 5}, {'assetid': '456', 'price': 0}],
    [{'assetid': '123', 'price': -1}], [{'assetid': '123', 'price': 0.001}]])
def test_invalid_import_is_all_or_nothing(client, db, items):
    assert client.post('/api/inventory/holdings', json={'items': items}).json()['ok'] is False
    assert db.db_get_purchases() == []


def test_completed_buff_order_matches_current_id_or_skin_fingerprint():
    assert match_buff_purchases([skin()], [order()], '7656')['123']['price'] == '12.34'
    assert match_buff_purchases([skin()], [order(assetid='seller-old')], '7656')['123']['id'] == 'bill1'


@pytest.mark.parametrize('patch', [{'state': 'TO_DELIVER'}, {'state': 'FAIL'}, {'progress': 105},
    {'buyer_steamid': 'someone-else'}, {'price': 'nan'}, {'price': 'Infinity'},
    {'goods_info': {'market_hash_name': 'Another skin'}}])
def test_invalid_or_wrong_account_order_never_supplies_price(patch):
    assert match_buff_purchases([skin()], [order(**patch)], '7656') == {}


def test_ambiguous_history_and_indistinguishable_inventory_require_manual_price():
    assert match_buff_purchases([skin()], [order('bill1'), order('bill2')], '7656') == {}
    assert match_buff_purchases([skin(), skin('456')], [order(assetid='seller-old')], '7656') == {}
    assert match_buff_purchases([skin(paintwear=None)], [order(assetid='seller-old')], '7656') == {}
    assert match_buff_purchases([skin()], [order(assetid='seller-old')], '7656', [skin(), skin('456')]) == {}


def test_deleted_duplicate_with_missing_price_cannot_create_zero_cost_record(db):
    with pytest.raises(ValueError):
        db.db_import_inventory_purchases([
            {'assetid': 'valid', 'name': NAME, 'price': 1},
            {'assetid': 'deleted-duplicate', 'name': NAME, 'price': 0},
        ])
    assert db.db_get_purchases() == []


def test_preview_reads_multiple_pages_and_existing_assets_are_skipped(client, db, monkeypatch):
    from app import config_loader
    from app.services import buff_client
    db.db_append_purchase({'assetid': '456', 'name': NAME, 'price': 7})
    monkeypatch.setattr(config_loader, 'get_buff_credentials', lambda: {'cookies': 'mock'})
    monkeypatch.setattr(inventory, 'load_app_config_validated', lambda: {})
    remote = Mock()
    remote.get_purchase_history_page.side_effect = [
        {'items': [order(str(i), assetid='other', goods_info={'market_hash_name': 'Other'}) for i in range(20)], 'total_page': 2},
        {'items': [order()], 'total_page': 2},
    ]
    monkeypatch.setattr(buff_client, 'create_buff_client_from_config', lambda *args: remote)
    result = client.post('/api/inventory/holdings/preview', json={'assetids': ['123', '456']}).json()
    assert result['ok']
    assert result['items'][0]['price'] == 12.34
    assert result['items'][0]['source'] == 'buff'
    assert result['items'][1]['exists'] is True
    assert remote.get_purchase_history_page.call_count == 2
    remote.close.assert_called_once()


def test_buff_failure_still_allows_manual_import(client, monkeypatch):
    from app import config_loader
    from app.services import buff_client
    monkeypatch.setattr(config_loader, 'get_buff_credentials', lambda: {'cookies': 'mock'})
    monkeypatch.setattr(inventory, 'load_app_config_validated', lambda: {})
    remote = Mock()
    remote.get_purchase_history_page.side_effect = RuntimeError('secret-cookie')
    monkeypatch.setattr(buff_client, 'create_buff_client_from_config', lambda *args: remote)
    result = client.post('/api/inventory/holdings/preview', json={'assetids': ['123']}).json()
    assert result['ok'] and result['items'][0]['price'] is None
    assert '手动' in result['warning'] and 'secret-cookie' not in str(result)
    assert client.post('/api/inventory/holdings', json={'items': [{'assetid': '123', 'price': 8}]}).json()['ok']


def test_buff_history_uses_authenticated_read_and_enriches_goods_names():
    from buff.buyer import BuffBuyer, API_HISTORY
    buyer = BuffBuyer.__new__(BuffBuyer)
    buyer._make_request = Mock(return_value={'code': 'OK', 'data': {
        'items': [{'id': 'bill1', 'goods_id': 42}], 'goods_infos': {'42': {'market_hash_name': NAME}},
    }})
    result = buyer.get_purchase_history_page(2)
    assert result['items'][0]['goods_info']['market_hash_name'] == NAME
    args, kwargs = buyer._make_request.call_args
    assert args == ('GET', API_HISTORY)
    assert kwargs['params']['page_num'] == '2'
    assert 'state' not in kwargs['params']


@pytest.mark.parametrize('failure,expected,unexpected', [
    ('parameters', '查询参数', '登录'),
    ('network', '网络错误', '登录'),
    ('expired', '登录已过期', '查询参数'),
    ('limited', '频率受限', '登录'),
])
def test_preview_distinguishes_history_errors(client, monkeypatch, failure, expected, unexpected):
    from requests.exceptions import Timeout
    from buff import BuffAuthExpired, BuffRateLimited
    from buff.buyer import BuffPurchaseHistoryError
    from app import config_loader
    from app.services import buff_client
    errors = {
        'parameters': BuffPurchaseHistoryError('BUFF 购入记录接口拒绝了查询参数（Invalid Argument）'),
        'network': Timeout('secret-cookie'),
        'expired': BuffAuthExpired(),
        'limited': BuffRateLimited(30),
    }
    monkeypatch.setattr(config_loader, 'get_buff_credentials', lambda: {'cookies': 'mock'})
    monkeypatch.setattr(inventory, 'load_app_config_validated', lambda: {})
    remote = Mock()
    remote.get_purchase_history_page.side_effect = errors[failure]
    monkeypatch.setattr(buff_client, 'create_buff_client_from_config', lambda *args: remote)
    result = client.post('/api/inventory/holdings/preview', json={'assetids': ['123']}).json()
    assert result['ok'] is True
    assert expected in result['warning'] and unexpected not in result['warning']
    assert 'secret-cookie' not in str(result)


def test_history_parameter_error_is_not_reported_as_login_failure():
    from buff.buyer import BuffBuyer, BuffPurchaseHistoryError
    buyer = BuffBuyer.__new__(BuffBuyer)
    buyer._make_request = Mock(return_value={'code': 'Invalid Argument'})
    with pytest.raises(BuffPurchaseHistoryError, match='查询参数'):
        buyer.get_purchase_history_page()


def commodity_item(assetid='new', name='Fracture Case', **patch):
    return {'assetid': assetid, 'market_hash_name': name, 'paintwear': None, 'commodity': True, **patch}


def commodity_order(bill='bill1', name='Fracture Case', price='2.14', **patch):
    return order(bill, assetid='seller-' + bill, price=price,
                 asset_info={'assetid': 'seller-' + bill},
                 goods_info={'market_hash_name': name, 'tags': {'type': {'internal_name': 'csgo_type_weaponcase'}}}, **patch)


def test_capsules_with_new_asset_ids_prefill_common_price_without_claiming_exact_bill():
    name = 'Antwerp 2022 Contenders Autograph Capsule'
    items = [commodity_item(name=name), commodity_item('another-new', name=name)]
    orders = [commodity_order('one', name, '2.80'), commodity_order('two', name, '2.80'),
              commodity_order('failed', name, '9.99', state='FAIL')]
    assert match_buff_purchases(items, orders, '7656') == {}
    result = buff_commodity_prices(items, orders, '7656')
    assert set(result) == {'new', 'another-new'}
    assert result['new']['price'] == 2.8
    assert result['new']['source'] == 'buff_same_price'
    assert result['new']['price_candidates'] == [{'price': 2.8, 'count': 2}]
    assert len(result['new']['purchase_candidates']) == 2
    assert not result['new'].get('purchased_at')


def test_cases_with_multiple_costs_offer_choices_instead_of_an_arbitrary_price():
    orders = [commodity_order('one', price='2.14'), commodity_order('two', price='2.13'), commodity_order('three', price='2.14')]
    result = buff_commodity_prices([commodity_item()], orders, '7656')['new']
    assert result['price'] is None and result['source'] == 'buff_choices'
    assert result['price_candidates'] == [{'price': 2.13, 'count': 1}, {'price': 2.14, 'count': 2}]


def test_container_tags_support_inventory_cached_before_commodity_metadata():
    assert buff_commodity_prices([commodity_item(commodity=False)], [commodity_order()], '7656')['new']['price'] == 2.14


@pytest.mark.parametrize('patch', [{'state': 'FAIL'}, {'progress': 106}, {'state': 'TO_DELIVER'},
                                  {'buyer_steamid': 'other'}, {'buyer_steamid': ''}, {'price': 'nan'}])
def test_commodity_price_reference_ignores_invalid_and_other_account_orders(patch):
    orders = [commodity_order(**patch)]
    assert buff_commodity_prices([commodity_item()], orders, '7656') == {}


def test_commodity_reference_does_not_treat_a_skin_missing_properties_as_a_container():
    assert buff_commodity_prices([skin(paintwear=None)], [order()], '7656') == {}
    assert buff_commodity_prices([commodity_item()], [commodity_order(name='Other Case')], '7656') == {}


def test_commodity_order_repeated_across_pages_counts_once():
    row = commodity_order()
    result = buff_commodity_prices([commodity_item()], [row, row], '7656')['new']
    assert result['price_candidates'] == [{'price': 2.14, 'count': 1}]


def test_preview_exposes_commodity_prices_and_exact_matches_take_precedence(client, monkeypatch):
    from app import config_loader
    from app.services import buff_client
    monkeypatch.setattr(inventory, 'get_inventory', lambda: [commodity_item('123'), commodity_item('456')])
    monkeypatch.setattr(config_loader, 'get_buff_credentials', lambda: {'cookies': 'mock'})
    monkeypatch.setattr(inventory, 'load_app_config_validated', lambda: {})
    remote = Mock()
    orders = [commodity_order('one', price='2.13'), commodity_order('two', price='2.14')]
    orders[0]['asset_info']['new_assetid'] = '123'
    remote.get_purchase_history_page.return_value = {'items': orders, 'total_page': 1}
    monkeypatch.setattr(buff_client, 'create_buff_client_from_config', lambda *args: remote)
    result = client.post('/api/inventory/holdings/preview', json={'assetids': ['123', '456']}).json()
    assert result['items'][0]['source'] == 'buff'
    assert result['items'][0]['price'] == 2.13
    assert result['items'][1]['source'] == 'buff_choices'
    assert result['items'][1]['price'] is None
    assert len(result['items'][1]['price_candidates']) == 2


def test_import_stores_purchase_time_and_market_price_without_overwriting_duplicates(client, db):
    db.db_append_purchase({'assetid': '456', 'name': NAME, 'price': 7, 'market_price': 8, 'at': 100})
    result = client.post('/api/inventory/holdings', json={'items': [
        {'assetid': '123', 'price': 12.34, 'market_price': 16.78, 'purchased_at': 1700000000},
        {'assetid': '456', 'price': 99, 'market_price': 100, 'purchased_at': 1700000000},
    ]}).json()
    assert result['ok'] and result['skipped'] == ['456']
    records = {row['assetid']: row for row in db.db_get_purchases()}
    assert records['123']['market_price'] == 16.78
    assert records['123']['at'] == 1700000000
    assert records['456']['market_price'] == 8 and records['456']['at'] == 100


def test_import_rejects_future_purchase_time_without_writing(client, db):
    import time
    result = client.post('/api/inventory/holdings', json={'items': [
        {'assetid': '123', 'price': 1, 'purchased_at': time.time() + 3600},
    ]}).json()
    assert result['ok'] is False
    assert db.db_get_purchases() == []


def test_preview_links_dated_orders_to_history_with_one_request_per_product(client, monkeypatch):
    from app import config_loader
    from app.services import buff_client, steam_client
    import datetime
    paid = datetime.datetime(2026, 8, 1, 2, tzinfo=datetime.timezone.utc).timestamp()
    creds = lambda: {'steam_id': '7656', 'cookies': 'mock'}
    monkeypatch.setattr(config_loader, 'get_buff_credentials', creds)
    monkeypatch.setattr(config_loader, 'get_steam_credentials', creds)
    monkeypatch.setattr(inventory, 'get_steam_credentials', creds)
    monkeypatch.setattr(inventory, 'load_app_config_validated', lambda: {})
    monkeypatch.setattr(inventory, 'get_inventory', lambda: [commodity_item('123'), commodity_item('456')])
    remote = Mock()
    remote.get_purchase_history_page.return_value = {'items': [
        commodity_order('one', buyer_pay_time=paid, transact_time=paid + 86400),
        commodity_order('two', buyer_pay_time=paid - 86400),
    ], 'total_page': 1}
    monkeypatch.setattr(buff_client, 'create_buff_client_from_config', lambda *args: remote)
    fetch = Mock(return_value={'currency': 'CNY', 'history': [
        ['Jul 31 2026 01: +0', 3.5, '3'], ['Aug 01 2026 01: +0', 4.5, '5'],
    ]})
    monkeypatch.setattr(steam_client.SteamClient, 'fetch_history', fetch)
    rows = client.post('/api/inventory/holdings/preview', json={'assetids': ['123', '456']}).json()['items']
    assert all(row['market_price'] is None and row['purchased_at'] is None for row in rows)
    for row in rows:
        first, second = row['purchase_candidates']
        assert first['purchased_at'] == paid and first['market_price'] == 4.5
        assert second['market_price'] == 3.5
    fetch.assert_called_once_with('Fracture Case', return_currency=True)
