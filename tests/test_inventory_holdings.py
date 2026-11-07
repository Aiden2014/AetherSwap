from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import database, state
from app.inventory_holdings import match_buff_purchases
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
