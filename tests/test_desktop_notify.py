"""Native notifications must never change payment confirmation or checkout results."""
import sys
import weakref
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app import desktop_notify as desktop
from app import pipeline_steps as steps
from app.config_schema import DEFAULTS, merge, validate_and_fill


@pytest.fixture
def native(monkeypatch):
    calls = SimpleNamespace(shown=[], removed=[], apartments=[], opened=[], registry=[], notifiers=[], images=[])

    class Toast:
        pass

    class Toaster:
        def __init__(self, name, notifierAUMID):
            assert name == "AetherSwap"
            assert notifierAUMID == desktop.APP_ID
            calls.notifiers.append(weakref.ref(self))

        def show_toast(self, toast):
            calls.shown.append(toast)

        def remove_toast(self, toast):
            calls.removed.append(toast)

    @contextmanager
    def create_key(root, path, reserved, access):
        assert (root, path, reserved, access) == (1, r"Software\Classes\AppUserModelId\AetherSwap.Desktop", 0, 2)
        yield "key"

    monkeypatch.setitem(sys.modules, "winreg", SimpleNamespace(
        HKEY_CURRENT_USER=1, KEY_SET_VALUE=2, REG_SZ=3, CreateKeyEx=create_key,
        SetValueEx=lambda *args: calls.registry.append(args),
    ))
    monkeypatch.setitem(sys.modules, "windows_toasts", SimpleNamespace(Toast=Toast, InteractableWindowsToaster=Toaster))
    def uninit():
        assert all(ref() is None for ref in calls.notifiers), "release native objects before uninitializing COM"
        calls.apartments.append("uninit")

    monkeypatch.setitem(sys.modules, "winrt.runtime", SimpleNamespace(
        ApartmentType=SimpleNamespace(MULTI_THREADED=1),
        init_apartment=lambda kind: calls.apartments.append("init") if kind == 1 else pytest.fail("use MTA"),
        uninit_apartment=uninit,
    ))
    monkeypatch.setattr(desktop, "get_runtime_profile", lambda: SimpleNamespace(platform="win32", can_open_gui=True))
    monkeypatch.setattr(desktop, "_open_payment_console", lambda **_kwargs: calls.opened.append(True))
    def show_payment_toast(toaster, toast, qr_image=None):
        calls.images.append(qr_image)
        toaster.show_toast(toast)
        return lambda: calls.apartments.append("release")
    monkeypatch.setattr(desktop, "_show_payment_toast", show_payment_toast)
    return calls


@pytest.mark.parametrize("num,pay_type,label", [(1, "alipay", "支付宝"), (3, "wechat", "微信")])
def test_native_content_click_and_removal(native, num, pay_type, label):
    logs = []
    dismiss = desktop.notify_payment_required("物品 <A&B>", 12.5, num, pay_type,
                                              lambda msg, level: logs.append((msg, level)))
    assert len(native.shown) == 1
    toast = native.shown[0]
    assert "物品 <A&B>" in toast.text_fields[1]
    assert f"× {num}" in toast.text_fields[1]
    assert f"¥{12.5 * num:.2f}" in toast.text_fields[1]
    assert label in toast.text_fields[1]
    assert native.apartments == ["init"]
    assert native.registry == [("key", "DisplayName", 0, 3, "AetherSwap")]
    toast.on_activated(None)
    assert native.opened == [True]
    dismiss()
    dismiss()  # Cleanup is idempotent.
    assert native.removed == [toast]
    assert native.apartments == ["init", "release", "uninit"]
    toast.on_activated(None)
    assert native.opened == [True], "stale clicks must not reopen a finished payment"


@pytest.mark.parametrize("platform,can_open_gui", [("linux", True), ("darwin", True), ("win32", False)])
def test_unsupported_or_server_runtime_never_loads_windows_dependencies(monkeypatch, platform, can_open_gui):
    monkeypatch.setattr(desktop, "get_runtime_profile", lambda: SimpleNamespace(
        platform=platform, can_open_gui=can_open_gui))
    monkeypatch.setitem(sys.modules, "windows_toasts", None)
    assert desktop.notify_payment_required("item", 1, 1, "alipay") is None


def test_missing_dependency_is_a_warning_not_a_checkout_error(native, monkeypatch):
    monkeypatch.setitem(sys.modules, "windows_toasts", None)
    logs = []
    assert desktop.notify_payment_required("item", 1, 1, "alipay",
                                           lambda msg, level: logs.append((msg, level))) is None
    assert logs[0][1] == "warn"


def test_native_show_failure_balances_apartment_and_returns_without_raising(native, monkeypatch):
    def fail_show(_self, _toast):
        raise OSError("notification service unavailable")

    monkeypatch.setattr(sys.modules["windows_toasts"].InteractableWindowsToaster, "show_toast", fail_show)
    assert desktop.notify_payment_required("item", 1, 1, "alipay") is None
    assert native.apartments == ["init", "uninit"]


def test_failed_removal_still_releases_apartment(native, monkeypatch):
    def fail_remove(_self, _toast):
        raise OSError("already gone")

    monkeypatch.setattr(sys.modules["windows_toasts"].InteractableWindowsToaster, "remove_toast", fail_remove)
    dismiss = desktop.notify_payment_required("item", 1, 1, "alipay")
    dismiss()
    assert native.apartments == ["init", "release", "uninit"]


@pytest.mark.parametrize("has_window", [True, False])
def test_click_restores_window_or_opens_local_console(monkeypatch, has_window):
    actions = []
    window = SimpleNamespace(show=lambda: actions.append("show"), restore=lambda: actions.append("restore"),
                             on_top=False, evaluate_js=lambda js: actions.append(js))
    monkeypatch.setattr(desktop, "_desktop_window", window if has_window else None)
    monkeypatch.setattr(desktop, "get_runtime_profile", lambda: None)
    monkeypatch.setattr(desktop, "get_bind_host", lambda _profile: "0.0.0.0")
    monkeypatch.setattr(desktop, "get_port", lambda: 3000)
    monkeypatch.setattr(desktop.webbrowser, "open", lambda url: actions.append(url))
    monkeypatch.setattr(desktop, "_focus_desktop_window", lambda _window: actions.append("focus"))
    desktop._open_payment_console()
    assert actions == (["restore", "show", "focus", desktop.PAYMENT_DASHBOARD_SCRIPT] if has_window
                       else ["http://127.0.0.1:3000/#payment"])
    assert window.on_top is False


@pytest.mark.parametrize("failure", ["restore", "focus", "navigate"])
def test_existing_gui_never_opens_browser_on_activation_errors(monkeypatch, failure):
    actions, warnings = [], []

    def action(name):
        actions.append(name)
        if name == failure:
            raise RuntimeError(name)

    window = SimpleNamespace(restore=lambda: action("restore"), show=lambda: action("show"),
                             on_top=False, evaluate_js=lambda _js: action("navigate"))
    monkeypatch.setattr(desktop, "_desktop_window", window)
    monkeypatch.setattr(desktop, "_focus_desktop_window", lambda _window: action("focus"))
    monkeypatch.setattr(desktop.webbrowser, "open", lambda _url: pytest.fail("existing GUI must not open browser"))
    desktop._open_payment_console(log_fn=lambda msg, level: warnings.append((msg, level)))
    assert warnings and all(level == "warn" for _, level in warnings)
    if failure == "focus":
        assert "navigate" in actions


def test_old_configs_default_to_enabled_and_false_roundtrips():
    assert validate_and_fill(merge(DEFAULTS, {"notify": {}}))["notify"]["desktop_payment_enabled"] is True
    assert validate_and_fill(merge(DEFAULTS, {"notify": {"desktop_payment_enabled": False}}))["notify"]["desktop_payment_enabled"] is False


@pytest.mark.parametrize("with_image", [True, False])
def test_native_notification_is_retained_and_unsubscribed(monkeypatch, tmp_path, with_image):
    from xml.etree import ElementTree as ET

    references = []
    events = []
    scheduled = []
    def schedule(tag, group, seconds):
        scheduled.append((tag, group, seconds))
        return lambda: events.append("cancel expiry")
    monkeypatch.setattr(desktop, "_schedule_notification_expiry", schedule)

    class Document:
        def load_xml(self, xml):
            self.xml = xml

    class Notification:
        def __init__(self, document):
            self.document = document
            self.handlers = {}
            references.append(weakref.ref(self))

        def add_activated(self, callback):
            self.handlers["activated"] = callback
            return 1

        def add_failed(self, callback):
            self.handlers["failed"] = callback
            return 2

        def remove_activated(self, token):
            assert token == 1
            self.handlers.pop("activated")
            events.append("unsubscribe activated")

        def remove_failed(self, token):
            assert token == 2
            self.handlers.pop("failed")
            events.append("unsubscribe failed")

    monkeypatch.setitem(sys.modules, "winrt.windows.data.xml.dom", SimpleNamespace(XmlDocument=Document))
    monkeypatch.setitem(sys.modules, "winrt.windows.ui.notifications", SimpleNamespace(ToastNotification=Notification))
    toast = SimpleNamespace(tag="payment", group="buff", text_fields=["物品 <A&B>"],
                            display_seconds=45,
                            on_activated=lambda _: events.append("click"), on_failed=lambda _: events.append("failed"))

    def show(notification):
        assert notification.tag == "payment" and notification.group == "buff"
        root = ET.fromstring(notification.document.xml)
        assert root.get("scenario") == "reminder"
        assert root.get("duration") == "long"
        assert root.find("./actions/action").get("activationType") == "system"
        from datetime import datetime, timezone
        assert 43 <= (notification.expiration_time - datetime.now(timezone.utc)).total_seconds() <= 45
        assert root.find("./visual/binding/text").text == "物品 <A&B>"
        image = root.find("./visual/binding/image")
        if with_image:
            assert image.get("src") == qr_image.as_uri()
            assert image.get("alt") == "付款二维码"
            assert image.get("placement") is None, "hero images would crop a square code"
        else:
            assert image is None

    qr_image = tmp_path / "付款 & test.png" if with_image else None
    release = desktop._show_payment_toast(SimpleNamespace(toastNotifier=SimpleNamespace(show=show)), toast,
                                          qr_image=qr_image)
    import gc
    assert scheduled == [("payment", "buff", 45)]
    gc.collect()
    notification = references[0]()
    assert notification is not None, "native notification must outlive show()"
    # Raw WinRT arguments deliberately cannot be converted by Windows-Toasts.
    notification.handlers["activated"](notification, object())
    notification.handlers["failed"](notification, object())
    assert events == ["click", "failed"]
    del notification
    release()
    release()
    assert events[-2:] == ["unsubscribe activated", "unsubscribe failed"]
    assert events.count("cancel expiry") == 1
    assert references[0]() is None


@pytest.mark.parametrize("direct_focus", [True, False])
def test_window_focus_detaches_notification_host_input(monkeypatch, direct_focus):
    import ctypes

    actions = []

    class NativeFunction:
        def __init__(self, callback):
            self.callback = callback

        def __call__(self, *args):
            return self.callback(*args)

    user32 = SimpleNamespace(
        GetForegroundWindow=NativeFunction(lambda: 99),
        GetWindowThreadProcessId=NativeFunction(lambda hwnd, _pid: 20),
        AttachThreadInput=NativeFunction(lambda *args: actions.append(("attach", args)) or True),
        SetForegroundWindow=NativeFunction(lambda hwnd: actions.append(("focus", hwnd)) or direct_focus),
        BringWindowToTop=NativeFunction(lambda hwnd: actions.append(("front", hwnd))),
    )
    kernel32 = SimpleNamespace(GetCurrentThreadId=lambda: 10)
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **_kwargs: user32 if name == "user32" else kernel32,
                        raising=False)
    window = SimpleNamespace(native=SimpleNamespace(Handle=SimpleNamespace(ToInt64=lambda: 42)))
    desktop._focus_desktop_window(window)
    if direct_focus:
        assert actions == [("focus", 42)]
    else:
        assert actions == [("focus", 42), ("attach", (10, 20, True)),
                           ("front", 42), ("focus", 42), ("attach", (10, 20, False))]


@pytest.mark.parametrize("outcome", [True, False, "exception"])
@pytest.mark.parametrize("num", [1, 3])
def test_common_payment_entry_notifies_once_and_always_cleans_up(monkeypatch, outcome, num):
    events = []

    def notify(*args, **kwargs):
        assert kwargs["duration_seconds"] == 45
        events.append(("notify", args, kwargs.get("pay_url")))
        return lambda: events.append("dismiss")

    def wait(**_kwargs):
        events.append("wait")
        if outcome == "exception":
            raise RuntimeError("payment query failed")
        return outcome

    monkeypatch.setattr(steps, "notify_payment_required", notify)

    def run():
        return steps._do_payment_notify_and_wait(
            {"name": "item"}, {"notify": {"desktop_payment_duration_seconds": 45}}, 12.5, num, "https://pay.invalid", "alipay", "order-1", 0,
            lambda p: events.append(("pending", p)), wait, lambda _ok: None, lambda: False, None,
        )

    if outcome == "exception":
        with pytest.raises(RuntimeError, match="payment query failed"):
            run()
    else:
        assert run() is outcome
    assert [event for event in events if isinstance(event, tuple) and event[0] == "notify"] == [
        ("notify", ("item", 12.5, num, "alipay"), "https://pay.invalid")]
    assert events[-2:] == [("pending", None), "dismiss"]


@pytest.mark.parametrize("disabled", [True, False])
def test_disabled_or_broken_notifications_do_not_prevent_payment(monkeypatch, disabled):
    calls = []

    def fail_notify(*_args, **_kwargs):
        calls.append(True)
        raise OSError("native failure")

    monkeypatch.setattr(steps, "notify_payment_required", fail_notify)
    assert steps._do_payment_notify_and_wait(
        {"name": "item"}, {"notify": {"desktop_payment_enabled": not disabled}},
        1, 1, "https://pay.invalid", "alipay", "order-1", 0,
        lambda _p: None, lambda **_kwargs: True, lambda _ok: None, lambda: False, None,
    ) is True
    assert calls == ([] if disabled else [True])


@pytest.mark.parametrize("pay_type,url", [
    ("alipay", "https://pay.example/alipay?bill=1&token=abc"),
    ("wechat", "weixin://wxpay/bizpayurl?pr=test"),
])
def test_toast_payment_qr_preserves_link_and_is_cleaned_up(native, monkeypatch, pay_type, url):
    import qrcode
    from PIL import Image
    from app import payment_qr

    payload = "https://qr.alipay.com/test-order" if pay_type == "alipay" else url
    monkeypatch.setattr(payment_qr, "_resolve_alipay_qr", lambda _url: payment_qr._qr_png(payload))
    payment_qr.forget_payment_qr({"pay_type": pay_type, "pay_url": url})

    dismiss = desktop.notify_payment_required("item", 1.25, 2, pay_type, pay_url=url)
    path = native.images[0]
    assert path.is_file()
    # Alipay encodes the cashier's short scan payload, never the long signed URL.
    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=4)
    code.add_data(payload)
    code.make(fit=True)
    with Image.open(path) as image:
        matrix = code.get_matrix()
        assert image.size == (len(matrix) * 10, len(matrix) * 10)
        for y, row in enumerate(matrix):
            for x, black in enumerate(row):
                assert bool(image.getpixel((x * 10 + 5, y * 10 + 5))) is not black
    assert "扫描下方二维码" in native.shown[0].text_fields[2]
    assert "item" not in path.name and "token" not in path.name
    native.shown[0].on_activated(None)
    assert path.exists(), "keep the image while Windows may still display the notification"
    dismiss()
    assert not path.parent.exists()


@pytest.mark.parametrize("failure", ["generate", "show", "remove"])
def test_qr_failures_keep_text_notification_or_clean_up(native, monkeypatch, tmp_path, failure):
    import tempfile
    from app.payment_qr import PaymentQR, _qr_png

    record = PaymentQR("test")
    record.image = None if failure == "generate" else _qr_png("https://qr.alipay.com/test")
    record.ready.set()

    directories = []

    def temporary_directory(**kwargs):
        directory = tempfile.TemporaryDirectory(dir=tmp_path, **kwargs)
        directories.append(directory.name)
        return directory

    monkeypatch.setattr(desktop, "TemporaryDirectory", temporary_directory)

    def fail(*_args, **_kwargs):
        raise OSError("test failure")

    if failure != "generate":
        monkeypatch.setattr(sys.modules["windows_toasts"].InteractableWindowsToaster,
                            "show_toast" if failure == "show" else "remove_toast", fail)
    logs = []
    dismiss = desktop.notify_payment_required("item", 1, 1, "alipay", pay_url="https://pay.example/1",
                                               qr_record=record,
                                               log_fn=lambda msg, level: logs.append((msg, level)))
    if failure == "generate":
        assert native.images == [None]
        assert "点击返回控制台完成付款" in native.shown[0].text_fields[2]
        assert any("二维码生成失败" in msg for msg, _ in logs)
        dismiss()
    elif failure == "remove":
        dismiss()
    else:
        assert dismiss is None
    from pathlib import Path
    assert bool(directories) is (failure != "generate")
    assert all(not Path(path).exists() for path in directories)
    assert native.apartments[-1] == "uninit"


@pytest.mark.parametrize("value,expected", [(None, 30), (True, 30), ("bad", 30), ("45", 45), (0, 5), (999, 300), (30, 30)])
def test_notification_duration_validates_and_defaults(value, expected):
    from app.config_schema import _validate_ranges
    config = _validate_ranges(validate_and_fill(merge(DEFAULTS, {
        "notify": {"desktop_payment_duration_seconds": value}})))
    # Typed config validation restores the default for invalid values.
    assert config["notify"]["desktop_payment_duration_seconds"] == expected


def test_notification_timeout_uses_independent_apartment_and_exact_order(monkeypatch):
    events = []

    class Timer:
        def __init__(self, seconds, callback):
            assert seconds == 30
            self.callback = callback
            events.append(self)
        def start(self):
            assert self.daemon is True
        def cancel(self):
            events.append("cancel")

    monkeypatch.setattr(desktop.threading, "Timer", Timer)
    monkeypatch.setitem(sys.modules, "winrt.runtime", SimpleNamespace(
        ApartmentType=SimpleNamespace(MULTI_THREADED=1),
        init_apartment=lambda _: events.append("init"),
        uninit_apartment=lambda: events.append("uninit")))
    history = SimpleNamespace(remove_grouped_tag_with_id=lambda *args: events.append(args))
    monkeypatch.setitem(sys.modules, "winrt.windows.ui.notifications", SimpleNamespace(
        ToastNotificationManager=SimpleNamespace(history=history)))
    cancel = desktop._schedule_notification_expiry("order-unique", "buff", 30)
    events[0].callback()
    assert events[1:] == ["init", ("order-unique", "buff", desktop.APP_ID), "uninit"]
    cancel()
    assert events[-1] == "cancel"


def test_payment_notifications_use_distinct_tags_and_configured_duration(native):
    first = desktop.notify_payment_required("item", 1, 1, "alipay", duration_seconds=45)
    first()
    second = desktop.notify_payment_required("item", 1, 1, "wechat")
    assert native.shown[0].tag != native.shown[1].tag
    assert native.shown[0].display_seconds == 45
    assert native.shown[1].display_seconds == 30
    second()
