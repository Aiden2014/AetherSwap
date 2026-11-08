"""Cashier QR resolution stays read-only and never encodes a signed gateway URL."""
import io
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from PIL import Image

from app import payment_qr as qr


SCAN = "https://qr.alipay.com/bax0123456789"
CASHIER = "https://openapi.alipay.com/gateway.do?sign=" + "a" * 3000


@pytest.fixture(autouse=True)
def clear_records():
    with qr._lock:
        qr._records.clear()
    yield
    with qr._lock:
        qr._records.clear()


@pytest.mark.parametrize("source", [
    f'<div id="J_qrCode"><img src="https://mobilecodec.alipay.com/show.htm?code={quote(SCAN, safe="")}&amp;picSize=180"></div>',
    f'<input id="qrCode" value="{SCAN}">',
    f'<script>var qrCode = "{SCAN}";</script>',
    '<script>var qrCode = "https:\\/\\/qr.alipay.com\\/bax0123456789";</script>',
    '<script>var qrCode = "https:\\u002f\\u002fqr.alipay.com\\u002fbax0123456789";</script>',
])
def test_extract_short_cashier_payload(source):
    assert qr.scan_url_from_html(source) == SCAN


def test_payment_element_takes_priority_over_other_links():
    assert qr.scan_url_from_html(f'<a href="https://qr.alipay.com/promo">App</a>'
                                f'<div id="J_qrCode" data-url="{SCAN}"></div>') == SCAN


@pytest.mark.parametrize("url", [
    CASHIER, "weixin://test", "https://qr.alipay.com.evil.test/bill",
    "https://qr.alipay.com:invalid/bill", "https://evil@qr.alipay.com/bill",
    "https://qr.alipay.com/", "https://qr.alipay.com/" + "x" * 600,
])
def test_reject_non_scan_urls(url):
    assert qr._scan_url(url) is None


@pytest.mark.parametrize("url", [
    "http://openapi.alipay.com/gateway.do", "https://alipay.com.evil.test/",
    "https://openapi.alipay.com:444/", "https://user@alipay.com/",
    "https://127.0.0.1/", "https://alicdn.com/asset",
])
def test_cashier_host_validation(url):
    assert not qr._alipay_url(url)


def test_resolver_uses_compact_payload_instead_of_signed_link(monkeypatch):
    monkeypatch.setattr(qr, "_read_cashier_html", lambda url: f'<div id="J_qrCode" data-url="{SCAN}"></div>')
    monkeypatch.setattr(qr, "_browser_cashier_qr", lambda _: pytest.fail("static QR should not launch a browser"))
    image = qr._resolve_alipay_qr(CASHIER)
    assert image == qr._qr_png(SCAN)
    with Image.open(io.BytesIO(image)) as png:
        assert 300 <= png.width < 500
        assert png.getpixel((0, 0)) != 0


def test_dynamic_cashier_uses_browser_image_without_encoding_gateway(monkeypatch):
    expected = qr._qr_png(SCAN)
    monkeypatch.setattr(qr, "_read_cashier_html", lambda _: "<div id='J_qrCode'></div>")
    monkeypatch.setattr(qr, "_browser_cashier_qr", lambda url: expected)
    monkeypatch.setattr(qr, "_qr_png", lambda _: pytest.fail("never encode gateway URL"))
    assert qr._resolve_alipay_qr(CASHIER) == expected


def test_unavailable_cashier_fails_without_long_url_fallback(monkeypatch):
    monkeypatch.setattr(qr, "_read_cashier_html", lambda _: "<html>Not ready</html>")
    def unavailable(_):
        raise ValueError("unavailable")
    monkeypatch.setattr(qr, "_browser_cashier_qr", unavailable)
    record = qr.prepare_payment_qr({"order_id": "one", "pay_type": "alipay", "pay_url": CASHIER})
    assert record.ready.wait(2)
    assert record.status == "unavailable" and record.image is None


def test_cache_runs_once_and_does_not_block_status(monkeypatch):
    entered, finish = threading.Event(), threading.Event()
    calls = []
    def resolve(url):
        calls.append(url)
        entered.set()
        finish.wait(2)
        return b"png"
    monkeypatch.setattr(qr, "_resolve_alipay_qr", resolve)
    payment = {"order_id": "one", "pay_type": "alipay", "pay_url": CASHIER}
    record = qr.prepare_payment_qr(payment)
    try:
        assert entered.wait(2)
        assert record.status == "loading"
        assert qr.prepare_payment_qr(dict(payment)) is record
        assert calls == [CASHIER]
    finally:
        finish.set()
    assert record.ready.wait(2)
    assert record.status == "ready"
    assert qr.payment_token(dict(payment, order_id="two")) != record.token
    qr.forget_payment_qr(payment)
    assert record.token not in qr._records


def test_wechat_generation_uses_same_image_service():
    payment = {"order_id": "one", "pay_type": "wechat", "pay_url": "weixin://wxpay/test"}
    record = qr.prepare_payment_qr(payment)
    assert record.ready.wait(2)
    assert record.image == qr._qr_png(payment["pay_url"])


def test_cashier_requests_validate_redirect_before_following(monkeypatch):
    requested = []
    @contextmanager
    def response(*args, **kwargs):
        requested.append((args, kwargs))
        yield SimpleNamespace(is_redirect=True, headers={"Location": "http://127.0.0.1/private"})
    session = SimpleNamespace(headers={}, get=response)
    @contextmanager
    def session_factory():
        yield session
    monkeypatch.setattr(qr.requests, "Session", session_factory)
    with pytest.raises(ValueError, match="not an Alipay"):
        qr._read_cashier_html(CASHIER)
    assert len(requested) == 1
    assert requested[0][1]["allow_redirects"] is False
    assert session.trust_env is False


def test_current_order_only_can_retrieve_png(monkeypatch):
    from fastapi import HTTPException
    from app.routes import status

    payment = {"order_id": "one", "pay_type": "alipay", "pay_url": CASHIER}
    record = qr.PaymentQR(qr.payment_token(payment))
    monkeypatch.setattr(status, "get_pending_payment", lambda: payment)
    monkeypatch.setattr(status, "prepare_payment_qr", lambda _: record)
    result = status.api_pending_payment()["pending"]
    assert result["qr_status"] == "loading" and result["qr_image_url"] is None
    with pytest.raises(HTTPException) as error:
        status.api_payment_qrcode(record.token)
    assert error.value.status_code == 404
    record.image = qr._qr_png(SCAN)
    record.ready.set()
    result = status.api_pending_payment()["pending"]
    assert result["qr_status"] == "ready"
    assert result["qr_image_url"] == f"/api/payment_qrcode/{record.token}"
    response = status.api_payment_qrcode(record.token)
    assert response.body == record.image
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store"
    for payment in (dict(payment, order_id="two"), None):
        with pytest.raises(HTTPException) as error:
            status.api_payment_qrcode(record.token)
        assert error.value.status_code == 404


@pytest.mark.parametrize("element", ["img", "canvas"])
def test_browser_extracts_rendered_cashier_image_offline(monkeypatch, element):
    import base64
    import os
    playwright = pytest.importorskip("playwright.sync_api")
    original_driver = playwright.sync_playwright
    png = qr._qr_png(SCAN)
    encoded = base64.b64encode(png).decode()
    if element == "img":
        source = f'<div id="J_qrCode"><img width="180" height="180" src="data:image/png;base64,{encoded}"></div>'
    else:
        source = f'''<div id="J_qrCode"><canvas width="370" height="370" style="width:185px;height:185px"></canvas></div>
        <script>const image = new Image(); image.onload = () => setTimeout(() => document.querySelector('canvas').getContext('2d').drawImage(image, 0, 0, 370, 370), 900);
        image.src = 'data:image/png;base64,{encoded}';</script>'''
    requests = []
    @contextmanager
    def replay_driver():
        with original_driver() as driver:
            def launch(**options):
                try:
                    browser = driver.chromium.launch(headless=True, executable_path=os.environ.get("AETHERSWAP_TEST_BROWSER"))
                except playwright.Error as exc:
                    if "Executable doesn't exist" in str(exc):
                        pytest.skip("Playwright Chromium is not installed")
                    raise
                def new_context(**options):
                    context = browser.new_context(**options)
                    def route(pattern, callback):
                        def replay(request_route):
                            requests.append((request_route.request.method, request_route.request.url))
                            callback(SimpleNamespace(request=request_route.request, abort=request_route.abort,
                                continue_=lambda: request_route.fulfill(body=source, content_type="text/html")))
                        context.route(pattern, replay)
                    return SimpleNamespace(route=route, new_page=context.new_page)
                return SimpleNamespace(new_context=new_context, close=browser.close)
            yield SimpleNamespace(chromium=SimpleNamespace(launch=launch))
    monkeypatch.setattr(playwright, "sync_playwright", replay_driver)
    resolved = qr._browser_cashier_qr(CASHIER)
    with Image.open(io.BytesIO(resolved)) as image:
        assert image.width == image.height
        assert image.width >= 360
        assert image.getpixel((0, 0)) == (255, 255, 255)
        colors = image.convert("L").getextrema()
        assert colors == (0, 255)
    assert requests == [("GET", CASHIER)]
