from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="托盘只在 Windows 上可用")

from agentlight import tray as tray_module


def build_icon() -> tuple[tray_module.TrayIcon, list[str]]:
    calls: list[str] = []
    icon = tray_module.TrayIcon(
        tooltip="测试",
        on_open=lambda: calls.append("open"),
        on_mute=lambda value: calls.append(f"mute:{value}"),
        on_pause=lambda value: calls.append(f"pause:{value}"),
        on_quit=lambda: calls.append("quit"),
        on_rest=lambda value: calls.append(f"rest:{value}"),
        on_restart=lambda: calls.append("restart"),
        state_provider=lambda: {"muted": False, "paused": False, "resting": False},
    )
    return icon, calls


def test_menu_commands_dispatch_to_the_right_callbacks() -> None:
    icon, calls = build_icon()
    state = {"muted": False, "paused": False, "resting": False}

    for command in (tray_module.CMD_OPEN, tray_module.CMD_MUTE, tray_module.CMD_PAUSE,
                    tray_module.CMD_REST, tray_module.CMD_RESTART, tray_module.CMD_QUIT):
        icon._dispatch(command, state)

    assert calls == ["open", "mute:True", "pause:True", "rest:True", "restart", "quit"]


def test_rest_and_restart_are_optional_so_older_callers_still_work() -> None:
    """两个回调是后加的；没接的调用方误触也不该炸在托盘线程里。"""
    icon = tray_module.TrayIcon(
        tooltip="测试",
        on_open=lambda: None,
        on_mute=lambda value: None,
        on_pause=lambda value: None,
        on_quit=lambda: None,
        state_provider=lambda: {},
    )

    icon._dispatch(tray_module.CMD_REST, {})
    icon._dispatch(tray_module.CMD_RESTART, {})


def test_toggles_flip_the_current_state_rather_than_forcing_a_value() -> None:
    icon, calls = build_icon()

    icon._dispatch(tray_module.CMD_MUTE, {"muted": True, "paused": False})
    icon._dispatch(tray_module.CMD_PAUSE, {"muted": False, "paused": True})
    icon._dispatch(tray_module.CMD_REST, {"resting": True})

    assert calls == ["mute:False", "pause:False", "rest:False"]


def test_unknown_commands_are_ignored() -> None:
    icon, calls = build_icon()

    icon._dispatch(0, {})
    icon._dispatch(999, {})
    # 临时状态那批命令号已经废弃，误触也不该有反应
    icon._dispatch(10, {})

    assert calls == []


def test_a_failing_callback_never_escapes_into_the_message_loop() -> None:
    """托盘线程里抛异常会吞掉整个消息循环，回调必须被兜住。"""
    icon = tray_module.TrayIcon(
        tooltip="测试",
        on_open=lambda: 1 / 0,
        on_mute=lambda value: None,
        on_pause=lambda value: None,
        on_quit=lambda: None,
        state_provider=lambda: {},
    )

    icon._dispatch(tray_module.CMD_OPEN, {})


def test_menu_reflects_the_current_toggle_state() -> None:
    import ctypes

    icon, _ = build_icon()
    user32 = tray_module.user32
    buffer = ctypes.create_unicode_buffer(64)
    MF_BYPOSITION = 0x400

    menu = icon.build_menu({"muted": True, "paused": False, "resting": True})
    try:
        labels = []
        for index in range(user32.GetMenuItemCount(menu)):
            user32.GetMenuStringW(menu, index, buffer, 64, MF_BYPOSITION)
            state = user32.GetMenuState(menu, index, MF_BYPOSITION)
            labels.append((buffer.value, bool(state & tray_module.MF_CHECKED)))

        assert ("打开配置页", False) in labels
        assert ("退出", False) in labels
        assert ("重启", False) in labels
        # 静音开着就要打勾，暂停没开就不打勾
        assert ("静音蜂鸣", True) in labels
        assert ("暂停联动", False) in labels
        assert ("关闭设备连接", True) in labels
        assert [text for text, _ in labels if text] == [
            "打开配置页", "静音蜂鸣", "暂停联动", "关闭设备连接", "重启", "退出",
        ]
    finally:
        user32.DestroyMenu(menu)



def test_dpi_awareness_is_declared_before_any_window_exists() -> None:
    """不声明的话 Windows 会把菜单按 96 DPI 画完再拉伸，高分屏上字是糊的。

    菜单是本程序唯一的可见界面，所以这一步不能被顺手删掉。
    """
    import inspect

    source = inspect.getsource(tray_module.TrayIcon.start)
    assert "enable_dpi_awareness()" in source
    assert source.index("enable_dpi_awareness()") < source.index("self._thread.start()")


def test_dpi_awareness_never_raises_even_if_every_api_is_missing(monkeypatch) -> None:
    """老系统上这些 API 不存在，也只是回到「字发虚」，不该让程序起不来。"""
    class _Blank:
        def __getattr__(self, name):
            raise AttributeError(name)

    monkeypatch.setattr(tray_module.ctypes, "windll", _Blank())

    tray_module.enable_dpi_awareness()
