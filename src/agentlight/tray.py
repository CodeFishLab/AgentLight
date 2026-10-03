"""Win32 托盘图标，直接用 ctypes 调 Shell_NotifyIcon。

直接使用 Win32 API 可避免额外的 GUI 框架依赖，并复用 assets/agentlight.ico。

进程必须先声明 DPI 感知，否则在高分屏上 Windows 会把菜单按 96 DPI 画好再整体
拉伸，文字就是糊的（右键菜单是本程序唯一的可见界面，这一步不能省）。
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from typing import Any, Callable

from .paths import icon_path


# PER_MONITOR_AWARE_V2：菜单按所在显示器的实际 DPI 渲染，跨屏拖动也会重算
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)


def enable_dpi_awareness() -> None:
    """尽早声明 DPI 感知，必须在创建任何窗口之前调用。

    三级回退：Win10 1703+ 的 per-monitor v2 → Win8.1 的 shcore → 老系统的
    system-DPI。全部失败也只是回到「字发虚」，不该让程序起不来。
    """
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


WM_DESTROY = 0x0002
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_RETIP = WM_APP + 2
WM_SET_HOTKEY = WM_APP + 3
WM_HOTKEY = 0x0312

HOTKEY_ID = 1
MOD_NOREPEAT = 0x4000  # 按住不放只触发一次，不然会连开一串标签页
SMTO_ABORTIFHUNG = 0x0002

WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_LBUTTONUP = 0x0202

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04

MF_STRING, MF_POPUP, MF_SEPARATOR = 0x0000, 0x0010, 0x0800
MF_CHECKED, MF_UNCHECKED = 0x0008, 0x0000

TPM_RIGHTALIGN, TPM_BOTTOMALIGN, TPM_RETURNCMD, TPM_NONOTIFY = 0x0008, 0x0020, 0x0100, 0x0080

IMAGE_ICON = 1
LR_LOADFROMFILE, LR_DEFAULTSIZE, LR_SHARED = 0x0010, 0x0040, 0x8000
IDI_APPLICATION = 32512

CMD_OPEN = 1
CMD_MUTE = 3
CMD_PAUSE = 4
CMD_QUIT = 5
CMD_REST = 6
CMD_RESTART = 7

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.TrackPopupMenu.restype = wintypes.BOOL
user32.TrackPopupMenu.argtypes = [
    wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, wintypes.HWND, ctypes.c_void_p,
]
user32.LoadImageW.restype = wintypes.HANDLE
user32.SendMessageTimeoutW.restype = LRESULT
user32.SendMessageTimeoutW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
]


class TrayIcon:
    """托盘图标与右键菜单。

    六项：打开配置页、静音蜂鸣、暂停联动、设备休息、重启、退出。
    前三项是开关，打勾表示已生效。
    """

    def __init__(
        self,
        tooltip: str,
        on_open: Callable[[], None],
        on_mute: Callable[[bool], None],
        on_pause: Callable[[bool], None],
        on_quit: Callable[[], None],
        state_provider: Callable[[], dict[str, Any]],
        on_rest: Callable[[bool], None] | None = None,
        on_restart: Callable[[], None] | None = None,
    ) -> None:
        self.tooltip = tooltip
        self.on_open = on_open
        self.on_mute = on_mute
        self.on_pause = on_pause
        self.on_quit = on_quit
        self.on_rest = on_rest
        self.on_restart = on_restart
        self.state_provider = state_provider

        self._hwnd: int | None = None
        self._hicon: int | None = None
        self._stopping = False
        self._pending_tip = tooltip
        self._ready = threading.Event()
        # WNDPROC 和 WNDCLASSW 必须被强引用住，否则回调会被 GC 掉导致崩溃
        self._wndproc = WNDPROC(self._handle_message)
        self._wndclass: WNDCLASSW | None = None
        self._thread = threading.Thread(target=self._run, name="AgentLight-Tray", daemon=True)

    # ------------------------------------------------------------------ 生命周期

    def start(self, timeout: float = 5) -> bool:
        # 必须在建窗口之前声明，之后再调就晚了（进程的 DPI 上下文只认第一次）
        enable_dpi_awareness()
        self._thread.start()
        return self._ready.wait(timeout)

    def stop(self) -> None:
        # 标记成主动收尾，避免下面的 WM_CLOSE 又反过来触发一次退出回调
        self._stopping = True
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
        if self._thread.is_alive():
            self._thread.join(timeout=3)

    def set_hotkey(self, modifiers: int, key: int) -> bool:
        """注册全局快捷键，key 为 0 时只注销。可以在任意线程调用。

        RegisterHotKey 绑定的是调用线程的窗口，必须在托盘线程里执行，
        所以这里把请求发过去同步等结果。
        """
        if not self._hwnd or self._stopping:
            return False
        result = ctypes.c_size_t(0)
        sent = user32.SendMessageTimeoutW(
            self._hwnd, WM_SET_HOTKEY, modifiers, key,
            SMTO_ABORTIFHUNG, 2000, ctypes.byref(result),
        )
        return bool(sent) and bool(result.value)

    def set_tooltip(self, text: str) -> None:
        self._pending_tip = text[:127]
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_RETIP, 0, 0)

    # ------------------------------------------------------------------ 窗口

    def _run(self) -> None:
        instance = kernel32.GetModuleHandleW(None)
        self._wndclass = WNDCLASSW()
        self._wndclass.lpfnWndProc = self._wndproc
        self._wndclass.hInstance = instance
        self._wndclass.lpszClassName = "AgentLightTrayWindow"
        if not user32.RegisterClassW(ctypes.byref(self._wndclass)):
            # 类名已注册（同进程重复启动）不算失败，继续建窗口
            if ctypes.get_last_error() != 1410:
                self._ready.set()
                return

        self._hwnd = user32.CreateWindowExW(
            0, "AgentLightTrayWindow", "Agent 状态灯", 0,
            0, 0, 0, 0, None, None, instance, None,
        )
        if not self._hwnd:
            self._ready.set()
            return

        self._hicon = self._load_icon()
        self._notify(NIM_ADD)
        self._ready.set()

        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))

    def _load_icon(self) -> int:
        path = icon_path()
        if path.is_file():
            handle = user32.LoadImageW(None, str(path), IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if handle:
                return handle
        return user32.LoadIconW(None, ctypes.c_wchar_p(IDI_APPLICATION))

    def _notify(self, action: int) -> None:
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = self._hwnd
        data.uID = 1
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.uCallbackMessage = WM_TRAY
        data.hIcon = self._hicon or 0
        data.szTip = self._pending_tip[:127]
        shell32.Shell_NotifyIconW(action, ctypes.byref(data))

    # ------------------------------------------------------------------ 消息

    def _handle_message(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        if message == WM_TRAY:
            event = lparam & 0xFFFF
            if event in (WM_LBUTTONDBLCLK, WM_LBUTTONUP):
                self._safely(self.on_open)
            elif event == WM_RBUTTONUP:
                self._show_menu()
            return 0
        if message == WM_RETIP:
            self._notify(NIM_MODIFY)
            return 0
        if message == WM_HOTKEY:
            if wparam == HOTKEY_ID:
                self._safely(self.on_open)
            return 0
        if message == WM_SET_HOTKEY:
            user32.UnregisterHotKey(hwnd, HOTKEY_ID)
            if not lparam:
                return 1
            return 1 if user32.RegisterHotKey(hwnd, HOTKEY_ID, wparam | MOD_NOREPEAT, lparam) else 0
        if message == WM_QUERYENDSESSION:
            # 安装程序的重启管理器和关机流程都会先问一句，答应它
            return 1
        if message == WM_ENDSESSION:
            if wparam:
                self._notify(NIM_DELETE)
                self._request_quit()
            return 0
        if message == WM_CLOSE:
            self._notify(NIM_DELETE)
            user32.DestroyWindow(hwnd)
            # 关闭请求来自外部（安装程序、taskkill、关机）时必须通知主线程退出，
            # 否则窗口没了、托盘图标也没了，进程却还挂着，安装程序会一直等下去
            self._request_quit()
            return 0
        if message == WM_DESTROY:
            user32.UnregisterHotKey(hwnd, HOTKEY_ID)
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _request_quit(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        self._safely(self.on_quit)

    @staticmethod
    def _safely(callback: Callable[..., Any], *args: Any) -> None:
        try:
            callback(*args)
        except Exception:  # 托盘线程里抛异常会吞掉消息循环
            pass

    def build_menu(self, state: dict[str, Any]) -> int:
        """构建右键菜单，返回菜单句柄。调用方负责 DestroyMenu。"""
        # 临时状态和恢复自动控制都挪回配置页了：托盘只留开关类操作，
        # 需要挑状态的场景本来就要看着灯效和会话列表做决定。
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, CMD_OPEN, "打开配置页")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(
            menu,
            MF_STRING | (MF_CHECKED if state.get("muted") else MF_UNCHECKED),
            CMD_MUTE,
            "静音蜂鸣",
        )
        user32.AppendMenuW(
            menu,
            MF_STRING | (MF_CHECKED if state.get("paused") else MF_UNCHECKED),
            CMD_PAUSE,
            "暂停联动",
        )
        # 和「暂停联动」区分开：暂停只是不再改灯，USB 上还在轮询；休息是真的断开
        user32.AppendMenuW(
            menu,
            MF_STRING | (MF_CHECKED if state.get("resting") else MF_UNCHECKED),
            CMD_REST,
            "关闭设备连接",
        )
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, CMD_RESTART, "重启")
        user32.AppendMenuW(menu, MF_STRING, CMD_QUIT, "退出")
        return menu

    def _show_menu(self) -> None:
        try:
            state = self.state_provider() or {}
        except Exception:
            state = {}
        menu = self.build_menu(state)

        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        # 不先抢前台，菜单在点击别处时不会消失——这是托盘菜单的经典坑
        user32.SetForegroundWindow(self._hwnd)
        chosen = user32.TrackPopupMenu(
            menu,
            TPM_RIGHTALIGN | TPM_BOTTOMALIGN | TPM_RETURNCMD | TPM_NONOTIFY,
            point.x, point.y, 0, self._hwnd, None,
        )
        user32.PostMessageW(self._hwnd, 0, 0, 0)
        user32.DestroyMenu(menu)

        self._dispatch(int(chosen), state)

    def _dispatch(self, command: int, state: dict[str, Any]) -> None:
        if command == CMD_OPEN:
            self._safely(self.on_open)
        elif command == CMD_MUTE:
            self._safely(self.on_mute, not state.get("muted"))
        elif command == CMD_PAUSE:
            self._safely(self.on_pause, not state.get("paused"))
        elif command == CMD_REST and self.on_rest:
            self._safely(self.on_rest, not state.get("resting"))
        elif command == CMD_RESTART and self.on_restart:
            self._safely(self.on_restart)
        elif command == CMD_QUIT:
            self._safely(self.on_quit)
