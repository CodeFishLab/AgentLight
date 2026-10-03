"""全局快捷键：组合键解析，以及服务端注册 / 回滚逻辑。"""

from __future__ import annotations

import pytest

from agentlight.config import DEFAULT_CONFIG, ConfigManager
from agentlight.hotkey import MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, parse_combo
from agentlight.service import AgentLightService


@pytest.mark.parametrize("text, expected", [
    ("Ctrl+Alt+L", (MOD_CONTROL | MOD_ALT, ord("L"), "Ctrl+Alt+L")),
    # 大小写、空格、修饰键顺序和别名都不影响结果，存下来是同一个字符串
    (" alt + control + l ", (MOD_CONTROL | MOD_ALT, ord("L"), "Ctrl+Alt+L")),
    ("Win+Shift+9", (MOD_SHIFT | MOD_WIN, 0x39, "Shift+Win+9")),
    ("meta+F12", (MOD_WIN, 0x7B, "Win+F12")),
    ("Ctrl+F1", (MOD_CONTROL, 0x70, "Ctrl+F1")),
])
def test_combos_parse_to_register_hotkey_arguments(text, expected) -> None:
    assert parse_combo(text) == expected


@pytest.mark.parametrize("text", [
    "", "   ", None, "L", "Ctrl+Alt", "Ctrl++L", "Ctrl+Ctrl+L", "Ctrl+L+K",
    "Ctrl+Enter", "Ctrl+F25",
    # 只有 Shift 等于在打大写字母，全局注册会让这个字母在所有程序里都打不出来
    "Shift+L",
])
def test_invalid_combos_are_rejected(text) -> None:
    with pytest.raises(ValueError):
        parse_combo(text)


def test_default_is_ctrl_alt_l_and_enabled() -> None:
    assert DEFAULT_CONFIG["hotkey"] == {"enabled": True, "combo": "Ctrl+Alt+L"}
    parse_combo(DEFAULT_CONFIG["hotkey"]["combo"])


class FakeTray:
    def __init__(self, taken: set[tuple[int, int]] | None = None) -> None:
        self.taken = taken or set()
        self.calls: list[tuple[int, int]] = []

    def register(self, modifiers: int, key: int) -> bool:
        self.calls.append((modifiers, key))
        return (modifiers, key) not in self.taken


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTLIGHT_DATA_DIR", str(tmp_path / "app-data"))
    instance = AgentLightService(ConfigManager(tmp_path / "config.json"))
    yield instance
    instance.shutdown()


def hotkey_status(service: AgentLightService) -> dict:
    return service.snapshot(include_integrations=False)["hotkey"]


def test_attaching_registers_the_configured_combo(service) -> None:
    tray = FakeTray()
    service.attach_hotkey(tray.register)

    assert tray.calls == [(MOD_CONTROL | MOD_ALT, ord("L"))]
    assert hotkey_status(service) == {"error": None}


def test_a_taken_combo_at_startup_is_reported_not_fatal(service) -> None:
    tray = FakeTray(taken={(MOD_CONTROL | MOD_ALT, ord("L"))})
    service.attach_hotkey(tray.register)

    assert "Ctrl+Alt+L" in hotkey_status(service)["error"]


def test_changing_the_combo_reregisters_and_saves_the_normalized_form(service) -> None:
    tray = FakeTray()
    service.attach_hotkey(tray.register)

    service.update_runtime_settings({"hotkey": {"enabled": True, "combo": "shift+win+k"}})

    assert tray.calls[-1] == (MOD_SHIFT | MOD_WIN, ord("K"))
    assert service.config.get("hotkey") == {"enabled": True, "combo": "Shift+Win+K"}


def test_a_taken_combo_is_refused_and_the_old_one_restored(service) -> None:
    """被占用就整次保存失败，旧快捷键继续可用，不存一个按了没反应的值。"""
    tray = FakeTray(taken={(MOD_CONTROL, ord("K"))})
    service.attach_hotkey(tray.register)

    with pytest.raises(ValueError, match="Ctrl\\+K"):
        service.update_runtime_settings({"hotkey": {"enabled": True, "combo": "Ctrl+K"}})

    assert tray.calls[-1] == (MOD_CONTROL | MOD_ALT, ord("L"))
    assert service.config.get("hotkey") == {"enabled": True, "combo": "Ctrl+Alt+L"}
    assert hotkey_status(service) == {"error": None}


def test_disabling_unregisters_and_clears_a_previous_error(service) -> None:
    tray = FakeTray(taken={(MOD_CONTROL | MOD_ALT, ord("L"))})
    service.attach_hotkey(tray.register)

    service.update_runtime_settings({"hotkey": {"enabled": False, "combo": "Ctrl+Alt+L"}})

    assert tray.calls[-1] == (0, 0)
    assert hotkey_status(service) == {"error": None}
    assert service.config.get("hotkey")["enabled"] is False


def test_resaving_an_unchanged_but_taken_combo_does_not_block_other_settings(service) -> None:
    tray = FakeTray(taken={(MOD_CONTROL | MOD_ALT, ord("L"))})
    service.attach_hotkey(tray.register)
    calls_before = len(tray.calls)

    service.update_runtime_settings({
        "hotkey": {"enabled": True, "combo": "Ctrl+Alt+L"},
        "done_hold_seconds": 15,
    })

    assert len(tray.calls) == calls_before
    assert service.config.get("done_hold_seconds") == 15


def test_invalid_combo_is_rejected_before_touching_the_tray(service) -> None:
    tray = FakeTray()
    service.attach_hotkey(tray.register)
    calls_before = len(tray.calls)

    with pytest.raises(ValueError):
        service.update_runtime_settings({"hotkey": {"enabled": True, "combo": "Shift+L"}})

    assert len(tray.calls) == calls_before
    assert service.config.get("hotkey")["combo"] == "Ctrl+Alt+L"
