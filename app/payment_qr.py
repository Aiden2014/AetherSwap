"""Read the current cashier's scan code without creating or confirming an order."""
from __future__ import annotations

import hashlib
import html
import io
import re
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup


QR_SELECTORS = (
    '#J_qrCode img, #J_qrCode canvas, #J_qrCodeImg, #J_QRCodeImg, '
    '#qrcode img, #qrcode canvas, #qrCode img, #qrCode canvas, '
    '.qrcode-img, .qrcode-image, .qrcode img, .qrcode canvas, '
    'img[src*="mobilecodec.alipay.com"]'
)
_lock = threading.Lock()
_records: dict[str, PaymentQR] = {}


@dataclass
class PaymentQR:
    token: str
    created_at: float = field(default_factory=time.monotonic)
    ready: threading.Event = field(default_factory=threading.Event)
    image: bytes | None = None

    @property
    def status(self) -> str:
        return 'ready' if self.image else ('unavailable' if self.ready.is_set() else 'loading')


def payment_token(payment: dict) -> str:
    return hashlib.sha256(
        f"{payment.get('order_id')}\0{payment.get('pay_type')}\0{payment.get('pay_url')}".encode()).hexdigest()[:32]


def _alipay_url(url: str, *, assets: bool = False) -> bool:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ''
        domains = ('alipay.com', 'alipayobjects.com', 'alicdn.com') if assets else ('alipay.com',)
        return (parsed.scheme == 'https' and not parsed.username and not parsed.password
                and parsed.port in (None, 443)
                and any(host == domain or host.endswith('.' + domain) for domain in domains))
    except ValueError:
        return False


def _scan_url(value: str) -> str | None:
    value = html.unescape(value).replace('\\/', '/')
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (parsed.scheme in ('https', 'http') and parsed.hostname == 'qr.alipay.com'
            and not parsed.username and not parsed.password and port in (None, 80, 443)
            and parsed.path.strip('/') and len(value) <= 512):
        return value
    if parsed.hostname == 'mobilecodec.alipay.com' and parsed.path.endswith('/show.htm'):
        code = parse_qs(parsed.query).get('code', [''])[0]
        if code != value:
            return _scan_url(code)
    return None


def _qr_png(value: str) -> bytes:
    import qrcode
    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=4)
    code.add_data(value)
    code.make(fit=True)
    output = io.BytesIO()
    code.make_image(fill_color='black', back_color='white').save(output, format='PNG')
    return output.getvalue()


def _image_png(data: bytes) -> bytes:
    from PIL import Image, ImageOps
    if len(data) > 2_000_000:
        raise ValueError('cashier QR image too large')
    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
        if not 80 <= min(width, height) <= max(width, height) <= 2048 or not .85 <= width / height <= 1.15:
            raise ValueError('cashier QR image is not square')
        histogram = image.convert('L').histogram()
        pixels = width * height
        if sum(histogram[:100]) < pixels * .05 or sum(histogram[200:]) < pixels * .1:
            raise ValueError('cashier QR image is still blank')
        image = ImageOps.expand(image.convert('RGB'), border=16, fill='white')
        output = io.BytesIO()
        image.save(output, format='PNG')
        return output.getvalue()


def scan_url_from_html(source: str) -> str | None:
    """Prefer the payment-code element over download/app promotion QR codes."""
    soup = BeautifulSoup(source, 'html.parser')
    for element in soup.select(QR_SELECTORS + ', #J_qrCode, #qrcode, #qrCode'):
        for name in ('src', 'data-src', 'data-qrcode', 'data-qr-code', 'data-url', 'value'):
            candidate = _scan_url(str(element.get(name) or ''))
            if candidate:
                return candidate
    # Alipay's cashier commonly initializes its QR widget from a script string.
    decoded = html.unescape(source).replace('\\/', '/').replace('\\u002F', '/').replace('\\u002f', '/')
    for match in re.finditer(r'''https?://qr\.alipay\.com/[^\s<>"'\\]+''', decoded):
        candidate = _scan_url(match.group())
        if candidate:
            return candidate
    return None


def _read_cashier_html(url: str) -> str:
    with requests.Session() as session:
        session.trust_env = False
        session.headers['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36'
        for _ in range(5):
            if not _alipay_url(url):
                raise ValueError('not an Alipay cashier URL')
            with session.get(url, timeout=(3, 4), allow_redirects=False, stream=True) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers['Location'])
                    continue
                response.raise_for_status()
                content = bytearray()
                for chunk in response.iter_content(65536):
                    content.extend(chunk)
                    if len(content) > 2_000_000:
                        raise ValueError('cashier page too large')
                return content.decode(response.encoding or 'utf-8', errors='replace')
    raise ValueError('too many cashier redirects')


def _browser_cashier_qr(url: str) -> bytes:
    """Render only the cashier in an isolated, cookie-free browser; never click pay."""
    from playwright.sync_api import Error as BrowserError, TimeoutError as BrowserTimeout, sync_playwright

    with sync_playwright() as driver:
        # A desktop may have Chrome/Edge even when bundled Chromium is absent.
        browser = None
        for options in ({}, {'channel': 'msedge'}, {'channel': 'chrome'}):
            try:
                browser = driver.chromium.launch(headless=True, **options)
                break
            except Exception:
                continue
        if browser is None:
            raise ValueError('cashier browser unavailable')
        try:
            context = browser.new_context(viewport={'width': 1100, 'height': 850}, device_scale_factor=2)
            context.route('**/*', lambda route: route.continue_() if _alipay_url(route.request.url, assets=True) else route.abort())
            page = context.new_page()
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=12000)
            except BrowserTimeout:
                pass  # A slow asset must not hide a code already rendered on the page.
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if not _alipay_url(page.url):
                    raise ValueError('cashier left Alipay')
                for frame in page.frames:
                    if not _alipay_url(frame.url):
                        continue
                    try:
                        candidate = scan_url_from_html(frame.content())
                    except BrowserError:
                        continue  # Gateway forms may navigate again after DOMContentLoaded.
                    if candidate:
                        return _qr_png(candidate)
                    elements = frame.locator(QR_SELECTORS)
                    for index in range(min(elements.count(), 6)):
                        try:
                            element = elements.nth(index)
                            if not element.is_visible():
                                continue
                            if element.evaluate("node => node.tagName === 'IMG' && (!node.complete || node.naturalWidth < 80)"):
                                continue
                            bounds = element.bounding_box()
                            if bounds and bounds['height'] >= 80 and .85 <= bounds['width'] / bounds['height'] <= 1.15:
                                return _image_png(element.screenshot(type='png', timeout=1500))
                        except (BrowserError, ValueError):
                            continue
                page.wait_for_timeout(400)
            raise ValueError('cashier did not expose a scan code')
        finally:
            browser.close()


def _resolve_alipay_qr(url: str) -> bytes:
    candidate = _scan_url(url)
    if candidate:
        return _qr_png(candidate)
    if not _alipay_url(url):
        raise ValueError('not an Alipay cashier URL')
    try:
        candidate = scan_url_from_html(_read_cashier_html(url))
        if candidate:
            return _qr_png(candidate)
    except (requests.RequestException, ValueError):
        pass
    return _browser_cashier_qr(url)


def prepare_payment_qr(payment: dict) -> PaymentQR:
    token = payment_token(payment)
    with _lock:
        record = _records.get(token)
        if record is not None:
            return record
        # Payment QR images stay in memory briefly, never in logs or persistent config.
        for key in list(_records):
            if time.monotonic() - _records[key].created_at > 300 or len(_records) >= 8:
                del _records[key]
        record = _records[token] = PaymentQR(token)
    url, channel = str(payment.get('pay_url') or ''), payment.get('pay_type')

    def resolve():
        try:
            if channel == 'alipay':
                record.image = _resolve_alipay_qr(url)
            elif channel == 'wechat' and url:
                record.image = _qr_png(url)
        except Exception:
            # Never include signed links, tokens, or a provider response in a UI error.
            record.image = None
        finally:
            record.ready.set()

    threading.Thread(target=resolve, name='payment-qr', daemon=True).start()
    return record


def forget_payment_qr(payment: dict) -> None:
    with _lock:
        _records.pop(payment_token(payment), None)
