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


@pytest.fixture(params=[(1440, 1000), (390, 844)])
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


def test_manual_entry_can_proceed_while_buff_query_is_pending(page):
    page.evaluate('''() => {
      const original = fetchJson;
      fetchJson = (url, options) => url.endsWith('/preview')
        ? new Promise(resolve => { window.finishPreview = resolve; }) : original(url, options);
    }''')
    page.locator('#inv-table .inv-select').first.check()
    page.locator('#btn-inventory-add-holdings').click()
    page.locator('#inventory-holdings-manual').click()
    page.locator('.inv-holding-price').fill('8.76')
    page.locator('#inventory-holdings-save').click()
    page.wait_for_function('window.savedImports.length === 1')
    assert page.evaluate('savedImports[0]') == [{'assetid': '111', 'price': 8.76}]
    page.evaluate("finishPreview({ok: true, items: [], warning: ''})")
    assert page.locator('#inventory-holdings-overlay').is_hidden()


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


def test_large_batch_scrolls_items_while_actions_remain_visible(page):
    page.locator('#inv-table .inv-select').first.check()
    page.locator('#btn-inventory-add-holdings').click()
    page.wait_for_function('!inventoryHoldingsBusy')
    page.evaluate('''() => {
      inventoryHoldingsRows = Array.from({length: 100}, (_, i) => ({assetid: String(i), name: 'StatTrak™ USP-S | Ticket to Hell', price: 12.34}));
      renderInventoryHoldingsRows();
    }''')
    page.wait_for_function("getComputedStyle(document.querySelector('.inventory-holdings-modal')).transform === 'none'")
    table = page.locator('.inventory-holdings-modal .table-container')
    assert table.evaluate('node => node.scrollHeight > node.clientHeight')
    for button in ['inventory-holdings-save', 'inventory-holdings-cancel']:
        box = page.locator('#' + button).bounding_box()
        assert box['y'] >= 0 and box['y'] + box['height'] <= page.viewport_size['height']
