import mimetypes
import os
from pathlib import Path
from urllib.parse import urlparse

import pytest
from app.payment_qr import payment_token, _qr_png


@pytest.fixture(scope="module")
def browser():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as driver:
        try:
            browser = driver.chromium.launch(
                headless=True, executable_path=os.environ.get("AETHERSWAP_TEST_BROWSER"))
        except playwright.Error as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip("Playwright Chromium is not installed")
            raise
        yield browser
        browser.close()


@pytest.fixture(params=[1280, 390])
def page(browser, request):
    web = Path(__file__).resolve().parent.parent / "web"
    context = browser.new_context(viewport={"width": request.param, "height": 1000})
    page = context.new_page()
    page.cashier_requests = []
    page.qr_requests = []
    page.external_requests = []
    page.page_errors = []
    page.on("pageerror", lambda error: page.page_errors.append(str(error)))

    def local_only(route):
        url = urlparse(route.request.url)
        if url.hostname == "cashier.test":
            page.cashier_requests.append(route.request.url)
            route.fulfill(
                body='<h1>测试收银台</h1><label>备注<input id="note"></label>',
                content_type="text/html",
            )
            return
        if url.hostname != "aetherswap.test":
            page.external_requests.append(route.request.url)
            route.fulfill(body="", content_type="application/javascript")
            return
        if url.path.startswith("/api/payment_qrcode/"):
            page.qr_requests.append(url.path)
            route.fulfill(body=_qr_png("https://qr.alipay.com/test-order"), content_type="image/png")
            return
        assert not url.path.startswith("/api/"), "Cashier UI must not make real payment calls"
        path = web / url.path.lstrip("/") if url.path != "/" else web / "index.html"
        if path.suffix == ".js":
            source = path.read_text(encoding="utf-8") if path.name in ("utils.js", "main.js") else ""
            if path.name == "main.js":
                source = source.rstrip().removesuffix("init();")
            route.fulfill(body=source, content_type="application/javascript")
        elif path.is_file():
            route.fulfill(path=str(path), content_type=mimetypes.guess_type(path)[0] or "application/octet-stream")
        else:
            route.fulfill(status=404, body="")

    context.route("**/*", local_only)
    page.goto("http://aetherswap.test/")
    page.evaluate("""() => {
      window.pending = null;
      window.apiCalls = [];
      fetchJson = async (url, options = {}) => {
        apiCalls.push([url, options.method || 'GET']);
        if (url === '/api/status') return {status: 'idle'};
        if (url === '/api/pending_payment') return {pending};
        if (url === '/api/stats') return {};
        throw new Error('Unexpected API call: ' + url);
      };
    }""")
    yield page
    assert not page.page_errors
    context.close()


def show_payment(page, *, order="bill-1", channel="alipay", url="https://cashier.test/pay?bill=1", status="ready"):
    payment = {"order_id": order, "pay_type": channel, "pay_url": url,
               "name": "测试物品", "auto_check_until": 2000000000, "qr_status": status}
    payment["qr_image_url"] = f"/api/payment_qrcode/{payment_token(payment)}" if status == "ready" else None
    page.evaluate("""async payment => {
      pending = payment;
      await refreshStatus();
    }""", payment)
    if status == "ready":
        page.wait_for_function("document.querySelector('#pay-qrcode img')?.naturalWidth > 0")


def test_alipay_only_shows_qr_survives_polling_and_keeps_manual_payment_link(page):
    show_payment(page)
    assert page.locator("#pending-payment iframe").count() == 0
    assert page.locator("#pay-qrcode-wrap").is_visible()
    assert page.locator("#pay-qrcode-label").inner_text() == "支付宝付款码"
    assert page.locator("#pay-qrcode img").is_visible()
    page.evaluate("window.originalCode = document.querySelector('#pay-qrcode img')")
    page.evaluate("refreshStatus()")
    page.evaluate("refreshStatus()")
    assert page.evaluate("originalCode === document.querySelector('#pay-qrcode img')")
    assert page.cashier_requests == [], "polling must never load or embed a cashier page"
    assert len(page.qr_requests) == 1
    assert not page.external_requests, "QR display must not depend on a CDN"
    assert page.locator("#pay-auto-status").inner_text().startswith("每 5 秒自动检查付款结果")
    assert page.evaluate("apiCalls.every(([, method]) => method === 'GET')")
    with page.expect_popup() as popup_info:
        page.locator("#pay-link").click()
    popup = popup_info.value
    popup.wait_for_load_state()
    assert popup.url == "https://cashier.test/pay?bill=1"
    assert popup.evaluate("window.opener === null")
    popup.close()
    for selector in ("#pending-payment", ".payment-actions", ".payment-details", "#pay-qrcode-wrap"):
        assert page.locator(selector).evaluate("node => node.scrollWidth <= node.clientWidth + 1"), selector


def test_order_channel_and_payment_completion_remove_stale_qr(page):
    show_payment(page)
    page.evaluate("window.oldCode = document.querySelector('#pay-qrcode img')")
    show_payment(page, order="bill-2", url="https://cashier.test/pay?bill=1")
    assert not page.evaluate("oldCode.isConnected")
    assert len(page.qr_requests) == 2
    show_payment(page, order="bill-3", channel="wechat", url="weixin://pay/bill-3")
    assert page.locator("#pending-payment iframe").count() == 0
    assert page.locator("#pay-qrcode-label").inner_text() == "微信付款码"
    assert page.locator("#pay-qrcode img").get_attribute("alt") == "微信付款二维码"
    show_payment(page, order="bill-4", url="https://cashier.test/pay?bill=4")
    page.evaluate("async () => { pending = null; await refreshStatus(); }")
    assert page.locator("#pending-payment").is_hidden()
    assert page.locator("#pending-payment iframe").count() == 0
    assert page.locator("#pay-qrcode").inner_html() == ""
    assert page.locator("#pay-link").get_attribute("href") is None
    assert page.locator("#pending-payment").get_attribute("data-pay-url") == ""


def test_payment_url_changes_replace_qr_without_requesting_cashier_or_confirming_payment(page):
    show_payment(page)
    previous_image = page.locator("#pay-qrcode img").get_attribute("src")
    show_payment(page, url="https://cashier.test/pay?bill=1&refreshed=1")
    assert page.locator("#pay-qrcode img").get_attribute("src") != previous_image
    assert page.locator("#pay-link").get_attribute("href") == "https://cashier.test/pay?bill=1&refreshed=1"
    assert not page.cashier_requests
    assert page.locator("#pay-qrcode-wrap").is_visible()
    page.evaluate("refreshStatus()")
    assert page.locator("#pending-payment").is_visible()
    assert page.evaluate("apiCalls.every(([, method]) => method === 'GET')")
    assert page.evaluate("pending.order_id") == "bill-1"


def test_loading_and_unavailable_qr_keep_status_and_manual_payment_link(page):
    show_payment(page, status="loading")
    assert page.locator("#pay-qrcode-wrap").is_hidden()
    assert "正在读取支付宝收银台" in page.locator("#pay-qrcode-status").inner_text()
    assert page.locator("#pending-payment iframe").count() == 0
    assert page.locator("#pay-link").is_visible()
    show_payment(page, order="bill-2", url="https://cashier.test/pay?bill=2", status="unavailable")
    assert "未能读取付款二维码" in page.locator("#pay-qrcode-status").inner_text()
    assert page.locator("#pay-qrcode-wrap").is_hidden()
    assert page.locator("#pending-payment iframe").count() == 0
    assert page.locator("#pay-link").get_attribute("href") == "https://cashier.test/pay?bill=2"


    assert page.qr_requests == []


def test_qr_opens_original_image_and_loading_clears_previous_order(page):
    show_payment(page)
    image_url = page.locator("#pay-qrcode img").get_attribute("src")
    with page.expect_popup() as popup_info:
        page.locator("#pay-qrcode a").click()
    popup = popup_info.value
    popup.wait_for_load_state()
    assert popup.url.endswith(image_url)
    assert popup.evaluate("window.opener === null")
    popup.close()
    show_payment(page, order="bill-2", status="loading")
    assert page.locator("#pay-qrcode img").count() == 0
    show_payment(page, order="bill-2")
    assert page.locator("#pay-qrcode img").is_visible()
    assert page.locator("#pay-qrcode-status").is_hidden()
    assert not page.cashier_requests
