import mimetypes
import os
from pathlib import Path
from urllib.parse import urlparse

import pytest


@pytest.fixture(scope="module")
def payment_browser():
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


@pytest.mark.parametrize("width", [1280, 390])
def test_payment_selection_priority_save_and_reload(payment_browser, width):
    web = Path(__file__).resolve().parent.parent / "web"
    page = payment_browser.new_page(viewport={"width": width, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def local_only(route):
        url = urlparse(route.request.url)
        if url.hostname != "aetherswap.test":
            route.fulfill(body="", content_type="application/javascript")
            return
        path = web / url.path.lstrip("/") if url.path != "/" else web / "index.html"
        assert not url.path.startswith("/api/"), "UI checks must not call a real API"
        if path.suffix == ".js":
            source = path.read_text(encoding="utf-8") if path.name in ("utils.js", "settings.js") else ""
            route.fulfill(body=source, content_type="application/javascript")
        elif path.is_file():
            route.fulfill(path=str(path), content_type=mimetypes.guess_type(path)[0] or "application/octet-stream")
        else:
            route.fulfill(status=404, body="")

    page.route("**/*", local_only)
    try:
        page.goto("http://aetherswap.test/")
        page.evaluate("""async () => {
          document.querySelectorAll('.panel').forEach(panel => panel.classList.remove('active'));
          el('panel-settings').classList.add('active');
          window.storedConfig = {buff: {pay_method: 'wechat'}};
          window.savedConfigs = [];
          updateUXStatus = () => {};
          setupInventoryAutoRefresh = () => {};
          fetchJson = async (url, options = {}) => {
            if (options.method === 'POST') {
              storedConfig = JSON.parse(options.body).config;
              savedConfigs.push(storedConfig);
              return {ok: true, config: storedConfig};
            }
            return {config: storedConfig};
          };
          await loadConfig();
        }""")
        alipay = page.locator("#cfg-pay-alipay")
        wechat = page.locator("#cfg-pay-wechat")
        priority = page.locator("#cfg-pay-priority")
        assert wechat.is_checked() and not alipay.is_checked()
        assert priority.is_disabled()
        alipay.check()
        assert priority.is_enabled()
        for preferred, expected in [("alipay", ["alipay", "wechat"]), ("wechat", ["wechat", "alipay"])]:
            priority.select_option(preferred)
            page.evaluate("saveConfigFromForm()")
            assert page.evaluate("storedConfig.buff.pay_methods") == expected
            assert alipay.is_checked() and wechat.is_checked()
            assert priority.input_value() == preferred
        alipay.uncheck()
        assert priority.is_disabled()
        page.evaluate("saveConfigFromForm()")
        assert page.evaluate("storedConfig.buff.pay_methods") == ["wechat"]
        wechat.uncheck()
        error = page.evaluate("async () => { try { await saveConfigFromForm(); } catch (err) { return err.message; } }")
        assert "至少选择一种" in error
        assert page.evaluate("savedConfigs.length") == 3
        assert not errors
    finally:
        page.close()
