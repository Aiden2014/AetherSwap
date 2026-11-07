"""Optional Windows notifications, independent of the web console and PushPlus."""
from __future__ import annotations

import threading
import webbrowser
from typing import Callable
from xml.etree import ElementTree as ET

from app.runtime_env import get_bind_host, get_local_url, get_port, get_runtime_profile

_desktop_window = None
APP_ID = "AetherSwap.Desktop"
PAYMENT_DASHBOARD_SCRIPT = """(() => {
    if (typeof window.openPaymentDashboard === 'function') {
        void window.openPaymentDashboard();
    } else if (typeof tabSwitch === 'function') {
        void tabSwitch('auto');
    }
    return true;
})()"""


def _focus_desktop_window(window) -> None:
    """Transfer keyboard focus from the Windows notification host."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    handle = window.native.Handle.ToInt64()
    if user32.SetForegroundWindow(handle):
        return
    foreground_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
    current_thread = kernel32.GetCurrentThreadId()
    attached = foreground_thread != current_thread and user32.AttachThreadInput(current_thread, foreground_thread, True)
    try:
        user32.BringWindowToTop(handle)
        user32.SetForegroundWindow(handle)
    finally:
        if attached:
            user32.AttachThreadInput(current_thread, foreground_thread, False)


def _create_windows_toaster():
    import winreg
    from windows_toasts import InteractableWindowsToaster

    # Windows requires a registered AUMID for a distinct name and activation.
    # HKCU registration needs no administrator privileges or Start menu shortcut.
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER,
        rf"Software\Classes\AppUserModelId\{APP_ID}",
        0,
        winreg.KEY_SET_VALUE,
    ) as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "AetherSwap")
    return InteractableWindowsToaster("AetherSwap", notifierAUMID=APP_ID)


def set_desktop_window(window) -> None:
    """Keep the current pywebview window available for notification activation."""
    global _desktop_window
    _desktop_window = window


def _open_payment_console(log_fn: Callable[[str, str], None] | None = None) -> None:
    window = _desktop_window
    if window is not None:
        try:
            window.restore()
            window.show()
        except Exception as exc:
            if log_fn:
                log_fn(f"[Buff]   → GUI 窗口恢复失败 ({type(exc).__name__})，请手动切回应用", "warn")
            return
        try:
            # Activation may be refused when another application has focus.
            # Bring the window forward without leaving it permanently on top.
            was_on_top = window.on_top
            window.on_top = True
            window.on_top = was_on_top
            _focus_desktop_window(window)
        except Exception as exc:
            if log_fn:
                log_fn(f"[Buff]   → GUI 前台激活失败 ({type(exc).__name__})，继续在原窗口打开仪表盘", "warn")
        try:
            window.evaluate_js(PAYMENT_DASHBOARD_SCRIPT)
        except Exception as exc:
            if log_fn:
                log_fn(f"[Buff]   → GUI 仪表盘跳转失败 ({type(exc).__name__})，请在程序中切换仪表盘", "warn")
        return
    profile = get_runtime_profile()
    if log_fn:
        log_fn("[Buff]   → 当前后台进程没有 GUI 窗口，使用浏览器打开仪表盘；如已打开 GUI，请完整退出旧后台后重新启动", "warn")
    webbrowser.open(get_local_url(get_bind_host(profile), get_port()) + "/#payment")


def _show_payment_toast(toaster, toast) -> Callable[[], None]:
    """Retain the native notification and event tokens until checkout ends.

    Windows-Toasts.show_toast discards its ToastNotification on return. Keep
    that object alive and avoid converting activation arguments we do not use.
    """
    from winrt.windows.data.xml.dom import XmlDocument
    from winrt.windows.ui.notifications import ToastNotification

    root = ET.Element("toast", launch=toast.tag)
    binding = ET.SubElement(ET.SubElement(root, "visual"), "binding", template="ToastGeneric")
    for line in toast.text_fields:
        ET.SubElement(binding, "text").text = line
    document = XmlDocument()
    document.load_xml(ET.tostring(root, encoding="unicode"))
    notification = ToastNotification(document)
    notification.tag = toast.tag
    notification.group = toast.group
    tokens = []

    def release() -> None:
        nonlocal notification
        if notification is None:
            return
        try:
            for remove, token in tokens:
                remove(token)
        finally:
            tokens.clear()  # Bound methods also retain the native object.
            notification = None

    try:
        tokens.append((notification.remove_activated, notification.add_activated(
            lambda _sender, _args: toast.on_activated(None))))
        tokens.append((notification.remove_failed, notification.add_failed(
            lambda _sender, args: toast.on_failed(args))))
        toaster.toastNotifier.show(notification)
    except Exception:
        release()
        raise
    return release


def notify_payment_required(
    name: str,
    unit_price: float,
    num: int,
    pay_type: str,
    log_fn: Callable[[str, str], None] | None = None,
) -> Callable[[], None] | None:
    """Show once on payment entry; return cleanup for the same checkout thread.

    WinRT stays initialized until payment waiting ends. No Windows dependencies
    are imported on other platforms or in server mode.
    """
    profile = get_runtime_profile()
    if profile.platform != "win32" or not profile.can_open_gui:
        return None

    initialized = False
    toaster = toast = None
    release_notification = None
    failure = None
    active = threading.Event()
    try:
        from winrt.runtime import ApartmentType, init_apartment, uninit_apartment
        from windows_toasts import Toast

        init_apartment(ApartmentType.MULTI_THREADED)
        initialized = True
        toaster = _create_windows_toaster()
        label = {"alipay": "支付宝", "wechat": "微信"}.get(pay_type, pay_type)
        toast = Toast()
        toast.tag = "payment"
        toast.group = "buff"
        toast.text_fields = [
            "AetherSwap · BUFF 等待支付",
            f"{name} × {num}\n合计 ¥{unit_price * num:.2f} · {label}",
            "点击返回控制台完成付款",
        ]

        def on_activated(_event) -> None:
            if active.is_set():
                try:
                    if log_fn:
                        log_fn("[Buff]   → 已点击待付款通知，打开仪表盘", "info")
                    _open_payment_console(log_fn=log_fn)
                except Exception:
                    if log_fn:
                        log_fn("[Buff]   → 无法打开付款控制台，请手动切回应用", "warn")

        def on_failed(_event) -> None:
            if log_fn:
                log_fn("[Buff]   → Windows 未能显示待付款通知，请查看控制台", "warn")

        toast.on_activated = on_activated
        toast.on_failed = on_failed
        active.set()
        release_notification = _show_payment_toast(toaster, toast)
    except Exception as exc:
        failure = type(exc).__name__

    # Drop both the exception traceback and WinRT objects before uninitializing
    # COM. Releasing a notifier after its apartment is gone can crash Python.
    if failure is not None:
        active.clear()
        toaster = toast = None
        if initialized:
            uninit_apartment()
        if log_fn:
            log_fn(f"[Buff]   → 系统通知不可用 ({failure})，请查看页面待付款提示", "warn")
        return None

    if log_fn:
        log_fn("[Buff]   → 已提交 Windows 待付款通知", "info")

    def dismiss() -> None:
        nonlocal toaster, toast, release_notification
        if not active.is_set():
            return
        active.clear()
        try:
            toaster.remove_toast(toast)
        except Exception:
            pass
        finally:
            try:
                if release_notification is not None:
                    release_notification()
            finally:
                release_notification = toaster = toast = None
                uninit_apartment()

    return dismiss
