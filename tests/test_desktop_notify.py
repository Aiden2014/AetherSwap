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
    calls = SimpleNamespace(shown=[], removed=[], apartments=[], opened=[], registry=[], notifiers=[])

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
    def show_payment_toast(toaster, toast):
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


def test_native_notification_is_retained_and_unsubscribed(monkeypatch):
    from xml.etree import ElementTree as ET

    references = []
    events = []

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
                            on_activated=lambda _: events.append("click"), on_failed=lambda _: events.append("failed"))

    def show(notification):
        assert notification.tag == "payment" and notification.group == "buff"
        root = ET.fromstring(notification.document.xml)
        assert root.find("./visual/binding/text").text == "物品 <A&B>"

    release = desktop._show_payment_toast(SimpleNamespace(toastNotifier=SimpleNamespace(show=show)), toast)
    import gc
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

    def notify(*args, **_kwargs):
        events.append(("notify", args))
        return lambda: events.append("dismiss")

    def wait(**_kwargs):
        events.append("wait")
        if outcome == "exception":
            raise RuntimeError("payment query failed")
        return outcome

    monkeypatch.setattr(steps, "notify_payment_required", notify)

    def run():
        return steps._do_payment_notify_and_wait(
            {"name": "item"}, {}, 12.5, num, "https://pay.invalid", "alipay", "order-1", 0,
            lambda p: events.append(("pending", p)), wait, lambda _ok: None, lambda: False, None,
        )

    if outcome == "exception":
        with pytest.raises(RuntimeError, match="payment query failed"):
            run()
    else:
        assert run() is outcome
    assert [event for event in events if isinstance(event, tuple) and event[0] == "notify"] == [
        ("notify", ("item", 12.5, num, "alipay"))]
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
