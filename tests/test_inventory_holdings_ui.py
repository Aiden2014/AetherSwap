import mimetypes
import os
from pathlib import Path
from urllib.parse import urlparse

import pytest


@pytest.fixture(scope='module')
def browser():
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as driver:
        try:
            browser = driver.chromium.launch(headless=True, executable_path=os.environ.get('AETHERSWAP_TEST_BROWSER'))
        except playwright.Error as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip('Playwright Chromium is not installed')
            raise
        yield browser
        browser.close()


@pytest.fixture(params=[(1440, 1000), (1248, 700), (390, 844)])
def page(browser, request):
    web = Path(__file__).resolve().parent.parent / 'web'
    width, height = request.param
    page = browser.new_page(viewport={'width': width, 'height': height})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def local_only(route):
        url = urlparse(route.request.url)
        if url.hostname != 'aetherswap.test':
            route.fulfill(body='', content_type='application/javascript')
            return
        path = web / url.path.lstrip('/') if url.path != '/' else web / 'index.html'
        assert not url.path.startswith('/api/'), 'No real API calls in UI tests'
        if path.suffix == '.js':
            source = path.read_text(encoding='utf-8') if path.name in ('utils.js', 'main.js', 'inventoryHoldings.js') else ''
            if path.name == 'main.js':
                source = source.rstrip().removesuffix('init();')
            route.fulfill(body=source, content_type='application/javascript')
        elif path.is_file():
            route.fulfill(path=str(path), content_type=mimetypes.guess_type(path)[0] or 'application/octet-stream')
        else:
            route.fulfill(status=404, body='')

    page.route('**/*', local_only)
    page.goto('http://aetherswap.test/')
    page.evaluate('''async () => {
      document.querySelectorAll('.panel').forEach(panel => panel.classList.remove('active'));
      el('panel-inventory').classList.add('active');
      window.savedImports = [];
      window.refreshCount = 0;
      refreshTransactions = async () => { window.refreshCount++; };
      window.previewWarning = '';
      fetchJson = async (url, options = {}) => {
        if (url.endsWith('/holdings/preview')) {
          return {ok: true, warning: window.previewWarning, items: JSON.parse(options.body).assetids.map(assetid => ({assetid, name: '物品 ' + assetid, exists: assetid === '333', price: assetid === '111' ? 12.34 : null, source: assetid === '111' ? 'buff' : 'manual'}))};
        }
        if (url.endsWith('/holdings')) {
          const items = JSON.parse(options.body).items;
          window.savedImports.push(items);
          return {ok: true, added: items.filter(item => item.assetid !== '333').map(item => item.assetid), skipped: items.filter(item => item.assetid === '333').map(item => item.assetid)};
        }
        if (url.endsWith('/inventory')) return {source: 'cache', items: [
          {assetid: '111', name: '中文物品', market_hash_name: 'English item', lowest_price: 10},
          {assetid: '222', name: '另一件物品', market_hash_name: 'Other item', lowest_price: 20},
          {assetid: '333', name: '已存在的物品', market_hash_name: 'Existing item', lowest_price: 30},
        ]};
        if (url.endsWith('/market-prices')) return {prices: {'English item': 99}};
        throw new Error('Unexpected request ' + url);
      };
      await refreshInventory(false);
    }''')
    yield page
    page.close()
    assert errors == []


def test_selected_item_prefills_buff_and_manual_override_is_preserved(page):
    assert page.locator('#inv-table tbody tr').first.locator('td').nth(2).inner_text() == '111'
    page.locator('#inv-table .inv-select').first.check()
    page.locator('#btn-inventory-add-holdings').click()
    page.wait_for_function('!inventoryHoldingsBusy')
    price = page.locator('.inv-holding-price')
    assert price.input_value() == '12.34'
    price.fill('18.75')
    page.locator('#inventory-holdings-sync').click()
    page.wait_for_function('!inventoryHoldingsBusy')
    assert price.input_value() == '18.75'
    page.locator('#inventory-holdings-save').click()
    page.wait_for_function('window.savedImports.length === 1')
    assert page.evaluate('savedImports[0]') == [{'assetid': '111', 'price': 18.75}]
    assert page.locator('#inventory-holdings-overlay').is_hidden()
    assert page.evaluate('refreshCount') == 1


def test_batch_requires_missing_prices_and_skips_existing_assets(page):
    page.locator('#inv-select-all').check()
    assert '（3）' in page.locator('#btn-inventory-add-holdings').inner_text()
    page.locator('#btn-inventory-add-holdings').click()
    page.wait_for_function('!inventoryHoldingsBusy')
    assert page.locator('.inv-holding-price').nth(2).is_disabled()
    page.locator('#inventory-holdings-save').click()
    assert page.evaluate('savedImports.length') == 0
    assert '有效购入价' in page.locator('#inventory-holdings-status').inner_text()
    page.locator('.inv-holding-price').nth(1).fill('23.45')
    page.locator('#inventory-holdings-save').click()
    page.wait_for_function('window.savedImports.length === 1')
    assert page.evaluate('savedImports[0]') == [{'assetid': '111', 'price': 12.34}, {'assetid': '222', 'price': 23.45}, {'assetid': '333', 'price': None}]
    assert '新增 2 件，跳过重复 1 件' in page.locator('#toast-host').inner_text()


def test_market_prices_update_price_column_by_english_name(page):
    page.evaluate('lastEnrichData = []; refreshMarketPrices()')
    assert page.locator('#inv-table .inv-price').first.inner_text() == '99.00'
    assert page.locator('#inv-table .inv-name').first.inner_text() == '中文物品'
    assert page.locator('#inv-table tbody tr').first.locator('td').nth(2).inner_text() == '111'


def test_manual_edits_survive_pending_buff_query_without_manual_button(page):
    page.evaluate('''() => {
      const original = fetchJson;
      fetchJson = (url, options) => url.endsWith('/preview')
        ? new Promise(resolve => { window.finishPreview = resolve; }) : original(url, options);
    }''')
    page.locator('#inv-table .inv-select').first.check()
    page.locator('#btn-inventory-add-holdings').click()
    assert page.locator('#inventory-holdings-manual').count() == 0
    assert page.locator('#inventory-holdings-remove').is_disabled()
    page.locator('.inv-holding-price').fill('8.76')
    page.evaluate("finishPreview({ok: true, items: [{assetid: '111', name: '物品 111', price: 12.34, source: 'buff'}]})")
    page.wait_for_function('!inventoryHoldingsBusy')
    page.locator('#inventory-holdings-save').click()
    page.wait_for_function('window.savedImports.length === 1')
    assert page.evaluate('savedImports[0]') == [{'assetid': '111', 'price': 8.76}]
    assert page.locator('#inventory-holdings-overlay').is_hidden()


def test_batch_removal_preserves_remaining_price_and_excludes_removed_items_from_sync_and_save(page):
    page.locator('#inv-select-all').check()
    page.locator('#btn-inventory-add-holdings').click()
    page.wait_for_function('!inventoryHoldingsBusy')
    page.locator('.inv-holding-price').first.fill('18.75')
    page.locator('.inv-holding-select').nth(1).check()
    page.locator('.inv-holding-select').nth(2).check()
    assert page.locator('#inventory-holdings-select-all').evaluate('node => node.indeterminate')
    assert page.locator('#inventory-holdings-remove').inner_text() == '移除所选（2）'
    page.locator('#inventory-holdings-remove').click()
    assert page.locator('.inv-holding-select').count() == 1
    assert page.locator('.inv-holding-price').input_value() == '18.75'
    assert page.locator('#inv-table .inv-select:checked').count() == 3
    page.locator('#inventory-holdings-sync').click()
    page.wait_for_function('!inventoryHoldingsBusy')
    assert page.locator('.inv-holding-select').count() == 1
    page.locator('#inventory-holdings-save').click()
    page.wait_for_function('window.savedImports.length === 1')
    assert page.evaluate('savedImports[0]') == [{'assetid': '111', 'price': 18.75}]


def test_select_all_removal_disables_empty_submission_and_resets_when_reopened(page):
    page.locator('#inv-select-all').check()
    page.locator('#btn-inventory-add-holdings').click()
    page.wait_for_function('!inventoryHoldingsBusy')
    page.locator('#inventory-holdings-select-all').check()
    assert page.locator('.inv-holding-select:checked').count() == 3
    page.locator('#inventory-holdings-select-all').uncheck()
    assert page.locator('.inv-holding-select:checked').count() == 0
    assert page.locator('#inventory-holdings-remove').is_disabled()
    page.locator('#inventory-holdings-select-all').check()
    page.locator('#inventory-holdings-remove').click()
    assert '列表为空' in page.locator('#inventory-holdings-table tbody').inner_text()
    for control in ['save', 'sync', 'select-all', 'remove']:
        assert page.locator('#inventory-holdings-' + control).is_disabled()
    assert page.evaluate('savedImports.length') == 0
    page.locator('#inventory-holdings-cancel').click()
    page.locator('#btn-inventory-add-holdings').click()
    page.wait_for_function('!inventoryHoldingsBusy')
    assert page.locator('.inv-holding-select').count() == 3
    assert page.locator('.inv-holding-select:checked').count() == 0


def test_dialog_layout_keeps_long_item_names_and_status_readable(page, tmp_path):
    page.locator('#inv-table .inv-select').first.check()
    page.locator('#btn-inventory-add-holdings').click()
    page.wait_for_function('!inventoryHoldingsBusy')
    page.evaluate('''() => {
      inventoryHoldingsRows = [
        {assetid: '54443015616', name: 'StatTrak™ USP-S | Ticket to Hell', exists: true},
        {assetid: '54442804976', name: 'StatTrak™ USP-S | Ticket to Hell', source: 'buff', price: 123.45},
      ];
      renderInventoryHoldingsRows();
    }''')
    modal = page.locator('.inventory-holdings-modal')
    # Wait for the opening animation before measuring viewport containment.
    page.wait_for_function("getComputedStyle(document.querySelector('.inventory-holdings-modal')).transform === 'none'")
    box = modal.bounding_box()
    width = page.viewport_size['width']
    assert box['x'] >= 0 and box['x'] + box['width'] <= width
    assert box['width'] >= (900 if width > 760 else width - 30)
    assert modal.evaluate('node => node.scrollWidth <= node.clientWidth')
    assert page.locator('.inv-holding-name').first.evaluate('''node => {
        const range = document.createRange();
        range.selectNodeContents(node);
        return range.getBoundingClientRect().height;
    }''') <= 52
    assert page.locator('.inv-holding-source').first.evaluate("node => getComputedStyle(node).whiteSpace") == 'nowrap'
    for source in page.locator('.inv-holding-source').all():
        assert source.evaluate('node => node.getBoundingClientRect().width <= node.parentElement.clientWidth')
    actions = page.locator('.inventory-holdings-actions')
    assert actions.evaluate('node => node.scrollWidth <= node.clientWidth')
    if width > 760:
        assert page.locator('#inventory-holdings-sync').bounding_box()['y'] == page.locator('#inventory-holdings-save').bounding_box()['y']
    page.screenshot(path=str(tmp_path / f'inventory-holdings-{width}.png'), animations='disabled')


def test_large_batch_scrolls_items_while_actions_remain_visible(page, tmp_path):
    page.locator('#inv-table .inv-select').first.check()
    page.locator('#btn-inventory-add-holdings').click()
    page.wait_for_function('!inventoryHoldingsBusy')
    page.evaluate('''() => {
      inventoryHoldingsRows = Array.from({length: 100}, (_, i) => ({assetid: String(i), name: 'StatTrak™ USP-S | Ticket to Hell', price: 12.34,
        market_price: 16.78, market_price_note: 'Steam 历史成交价参考', purchased_at: 1785559200, source: 'buff'}));
      renderInventoryHoldingsRows();
    }''')
    page.wait_for_function("getComputedStyle(document.querySelector('.inventory-holdings-modal')).transform === 'none'")
    table = page.locator('.inventory-holdings-modal .table-container')
    assert table.evaluate('node => node.scrollHeight > node.clientHeight')
    if page.viewport_size['width'] > 760:
        assert table.bounding_box()['height'] >= page.viewport_size['height'] * 0.62
        visible = table.evaluate('''node => {
          const bounds = node.getBoundingClientRect();
          return [...node.querySelectorAll('tbody tr')].filter(row => {
            const box = row.getBoundingClientRect();
            return box.top >= bounds.top && box.bottom <= bounds.bottom;
          }).length;
        }''')
        assert visible >= 5
    for button in ['inventory-holdings-save', 'inventory-holdings-cancel']:
        box = page.locator('#' + button).bounding_box()
        assert box['y'] >= 0 and box['y'] + box['height'] <= page.viewport_size['height']
    before = table.bounding_box()['height']
    page.locator('.inventory-holdings-help summary').click()
    assert page.locator('.inventory-holdings-help-content').is_visible()
    assert table.bounding_box()['height'] == before
    page.locator('.inventory-holdings-help summary').click()
    page.screenshot(path=str(tmp_path / f'inventory-holdings-expanded-{page.viewport_size["width"]}.png'), animations='disabled')


def test_case_cost_choice_survives_sync_and_submits_current_inventory_asset(page):
    page.evaluate('''() => {
      const original = fetchJson;
      fetchJson = (url, options) => url.endsWith('/preview') ? Promise.resolve({ok: true, items: [
        {assetid: '111', name: 'Fracture Case', source: 'buff_choices', price: null,
         price_candidates: [{price: 2.13, count: 1}, {price: 2.14, count: 2}]},
      ]}) : original(url, options);
    }''')
    page.locator('#inv-table .inv-select').first.check()
    page.locator('#btn-inventory-add-holdings').click()
    page.wait_for_function('!inventoryHoldingsBusy')
    assert '多个成交价' in page.locator('#inventory-holdings-status').inner_text()
    assert '未找到' not in page.locator('#inventory-holdings-status').inner_text()
    assert page.locator('.inv-holding-price').input_value() == ''
    page.locator('.inv-holding-price-choice').select_option('2.13')
    assert page.locator('.inv-holding-price').input_value() == '2.13'
    assert page.locator('.inv-holding-source').inner_text() == '已选 BUFF 成交价'
    page.locator('#inventory-holdings-sync').click()
    page.wait_for_function('!inventoryHoldingsBusy')
    assert page.locator('.inv-holding-price-choice').input_value() == '2.13'
    page.locator('#inventory-holdings-save').click()
    page.wait_for_function('window.savedImports.length === 1')
    assert page.evaluate('savedImports[0]') == [{'assetid': '111', 'price': 2.13}]


def test_capsule_reference_is_labelled_and_manual_edits_override_case_choices(page):
    page.evaluate('''() => {
      const original = fetchJson;
      fetchJson = (url, options) => url.endsWith('/preview') ? Promise.resolve({ok: true, items: [
        {assetid: '111', name: 'Antwerp 2022 Contenders Autograph Capsule', source: 'buff_same_price', price: 2.8,
         price_candidates: [{price: 2.8, count: 2}]},
        {assetid: '222', name: 'Fracture Case', source: 'buff_choices', price: null,
         price_candidates: [{price: 2.13, count: 1}, {price: 2.14, count: 2}]},
      ]}) : original(url, options);
    }''')
    page.locator('#inv-table .inv-select').nth(0).check()
    page.locator('#inv-table .inv-select').nth(1).check()
    page.locator('#btn-inventory-add-holdings').click()
    page.wait_for_function('!inventoryHoldingsBusy')
    assert page.locator('.inv-holding-price').first.input_value() == '2.80'
    assert page.locator('.inv-holding-source').first.inner_text() == 'BUFF 同款同价'
    page.locator('.inv-holding-price-choice').select_option('2.14')
    page.locator('.inv-holding-price').nth(1).fill('3.00')
    assert page.locator('.inv-holding-price-choice').input_value() == ''
    assert page.locator('.inv-holding-source').nth(1).inner_text() == '手动填写'


def test_historical_market_price_and_original_purchase_time_are_imported(page):
    page.evaluate('''() => {
      const original = fetchJson;
      fetchJson = (url, options) => url.endsWith('/preview') ? Promise.resolve({ok: true, items: [
        {assetid: '111', name: 'Test Item', source: 'buff', price: 12.34, purchased_at: 1785559200,
         market_price: 16.78, market_price_note: 'Steam 历史成交价参考'},
      ]}) : original(url, options);
    }''')
    page.locator('#inv-table .inv-select').first.check()
    page.locator('#btn-inventory-add-holdings').click()
    page.wait_for_function('!inventoryHoldingsBusy')
    assert page.locator('.inv-holding-market-price').input_value() == '16.78'
    assert '历史成交价' in page.locator('.inv-holding-market-note').inner_text()
    assert '2026' in page.locator('.inv-holding-date').inner_text()
    page.locator('#inventory-holdings-save').click()
    page.wait_for_function('window.savedImports.length === 1')
    assert page.evaluate('savedImports[0]') == [
        {'assetid': '111', 'price': 12.34, 'market_price': 16.78, 'purchased_at': 1785559200}]


def test_same_cost_different_dates_require_record_selection_and_preserve_manual_market_override(page):
    page.evaluate('''() => {
      const original = fetchJson;
      fetchJson = (url, options) => url.endsWith('/preview') ? Promise.resolve({ok: true, items: [
        {assetid: '111', name: 'Antwerp 2022 Contenders Autograph Capsule', source: 'buff_same_price', price: 2.8,
         price_candidates: [{price: 2.8, count: 2}], purchase_candidates: [
           {purchase_id: 'first', price: 2.8, purchased_at: 1785559200, market_price: 4.5, market_price_note: 'Steam 历史成交价参考'},
           {purchase_id: 'second', price: 2.8, purchased_at: 1785645600, market_price: 5.5, market_price_note: 'Steam 历史成交价参考'},
         ]},
      ]}) : original(url, options);
    }''')
    page.locator('#inv-table .inv-select').first.check()
    page.locator('#btn-inventory-add-holdings').click()
    page.wait_for_function('!inventoryHoldingsBusy')
    assert page.locator('.inv-holding-market-price').input_value() == ''
    assert '选择购买记录' in page.locator('.inv-holding-market-note').inner_text()
    choice = page.locator('.inv-holding-purchase-choice')
    choice.select_option('first')
    assert page.locator('.inv-holding-market-price').input_value() == '4.50'
    choice.select_option('second')
    assert page.locator('.inv-holding-market-price').input_value() == '5.50'
    choice.select_option('')
    assert page.locator('.inv-holding-market-price').input_value() == ''
    assert page.locator('.inv-holding-date').count() == 0
    choice.select_option('second')
    page.locator('.inv-holding-market-price').fill('6.25')
    page.locator('#inventory-holdings-sync').click()
    page.wait_for_function('!inventoryHoldingsBusy')
    assert choice.input_value() == 'second'
    assert page.locator('.inv-holding-market-price').input_value() == '6.25'
    assert page.locator('.inv-holding-market-note').inner_text() == '手动填写'
    assert choice.evaluate('node => node.getBoundingClientRect().width <= node.parentElement.clientWidth')
    page.locator('#inventory-holdings-save').click()
    page.wait_for_function('window.savedImports.length === 1')
    assert page.evaluate('savedImports[0]') == [
        {'assetid': '111', 'price': 2.8, 'market_price': 6.25, 'purchased_at': 1785645600}]


def test_market_price_can_be_cleared_and_invalid_values_block_import(page):
    page.locator('#inv-table .inv-select').first.check()
    page.locator('#btn-inventory-add-holdings').click()
    page.wait_for_function('!inventoryHoldingsBusy')
    price = page.locator('.inv-holding-market-price')
    price.fill('-1')
    page.locator('#inventory-holdings-save').click()
    assert page.evaluate('savedImports.length') == 0
    assert '有效购入市场价' in page.locator('#inventory-holdings-status').inner_text()
    price.fill('')
    page.locator('#inventory-holdings-save').click()
    page.wait_for_function('window.savedImports.length === 1')
    assert page.evaluate('savedImports[0]') == [{'assetid': '111', 'price': 12.34}]
