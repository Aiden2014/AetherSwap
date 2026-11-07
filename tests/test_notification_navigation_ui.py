"""Payment notification activation should navigate without replacing the page."""
import os
from pathlib import Path

import pytest


def test_notification_opens_dashboard_and_respects_unsaved_strategy():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as driver:
        try:
            browser = driver.chromium.launch(
                headless=True, executable_path=os.environ.get("AETHERSWAP_TEST_BROWSER"))
        except playwright.Error as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip("Playwright Chromium is not installed")
            raise
        try:
            page = browser.new_page()
            page.set_content('''
                <div class="nav-menu"><button data-tab="auto"><span>仪表盘</span></button></div>
                <main id="panel-auto" class="panel"><div id="pending-payment">等待支付</div></main>
                <main id="panel-settings" class="panel active"><input id="draft" value="保留表单"></main>
                <main id="panel-strategies" class="panel"></main>
            ''')
            page.evaluate('''() => {
                window.el = id => document.getElementById(id);
                window.stopSteamGuardTimer = () => {};
            }''')
            source = (Path(__file__).resolve().parents[1] / "web/js/main.js").read_text(encoding="utf-8")
            page.add_script_tag(content=source.rstrip().removesuffix("init();"))
            page.evaluate('''() => {
                window.statusRefreshes = 0;
                refreshStatus = async () => { statusRefreshes++; };
            }''')
            assert page.evaluate("openPaymentDashboard()") is True
            assert page.locator("#panel-auto").evaluate("node => node.classList.contains('active')")
            assert page.locator("#draft").input_value() == "保留表单"
            assert page.evaluate("statusRefreshes") == 1

            # Browser fallback uses the same navigation through the URL hash.
            page.evaluate("tabSwitch('settings')")
            page.evaluate("location.hash = 'payment'")
            page.wait_for_function("document.querySelector('#panel-auto').classList.contains('active')")
            assert page.evaluate("statusRefreshes") == 2

            # Existing unsaved strategy protection must still apply.
            page.evaluate('''() => {
                document.querySelector('#panel-auto').classList.remove('active');
                document.querySelector('#panel-strategies').classList.add('active');
                window.strategyConfirmLeave = async () => false;
            }''')
            assert page.evaluate("openPaymentDashboard()") is False
            assert page.locator("#panel-strategies").evaluate("node => node.classList.contains('active')")
        finally:
            browser.close()
