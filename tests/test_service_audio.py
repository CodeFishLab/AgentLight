from __future__ import annotations

from agentlight.config import ConfigManager
from agentlight import service as service_module


class FakeHardware:
    def __init__(self, force_reapply_seconds: int, frame_rate: int | None = None) -> None:
        self.force_reapply_seconds = force_reapply_seconds
        self.frame_rate = frame_rate or 20
        self.commands: list[tuple[str, tuple]] = []
        self.applies: list[tuple[str, bool, bool]] = []
        self.profiles: list = []
        self.sound_requests: list = []

    def apply(self, state, profile, muted, force=False, sound_style="standard", play_sound=None) -> None:
        self.applies.append((state, muted, force, sound_style))
        self.profiles.append(profile)
        self.sound_requests.append(play_sound)

    def command(self, name: str, *args) -> None:
        self.commands.append((name, args))

    def set_frame_rate(self, frame_rate: int) -> None:
        self.frame_rate = frame_rate

    def snapshot(self) -> dict:
        return {
            "connected": False,
            "status": None,
            "error": "",
            "last_seen": 0,
            "applied_state": "off",
            "last_interruption": None,
            "animating": False,
            "effect": "static",
            "frame_rate": 0,
        }

    def stop(self) -> None:
        return None


def test_device_mute_stops_and_blocks_sound(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(service_module, "HardwareWorker", FakeHardware)
    service = service_module.AgentLightService(ConfigManager(tmp_path / "config.json"))
    try:
        service.set_muted(True)
        assert service.hardware.commands[-1] == ("sound", ("stop", "standard"))
        command_count = len(service.hardware.commands)
        assert service.play_sound("success") is False
        assert len(service.hardware.commands) == command_count

        service.set_muted(False)
        assert service.play_sound("success") is True
        assert service.hardware.commands[-1] == ("sound", ("success", "standard"))

        # 预览现在会连提示音一起播，所以不再强制静音
        service.preview_state("busy")
        assert service.hardware.applies[-1][1] is False
        assert service.hardware.sound_requests[-1] is True
        assert service.preview_snapshot()["active"] is True
        service._finish_preview(service._preview_serial)
        assert service.preview_snapshot()["active"] is False
        assert service.hardware.applies[-1][0] == "off"
    finally:
        service.shutdown()


def test_saving_profiles_always_reapplies_without_sound(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(service_module, "HardwareWorker", FakeHardware)
    service = service_module.AgentLightService(ConfigManager(tmp_path / "config.json"))
    try:
        service.save_profiles(service.config.get("profiles"))
        assert service.hardware.applies[-1][1] is True
        assert service.hardware.applies[-1][2] is True
    finally:
        service.shutdown()


def test_active_state_suppresses_sleep_and_end_restores_configured_policy(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(service_module, "HardwareWorker", FakeHardware)
    service = service_module.AgentLightService(ConfigManager(tmp_path / "config.json"))
    try:
        assert service.hardware.commands[-1] == ("configure_sleep", (True, 300, False))
        service.emit("codex", "task", "busy")
        assert ("configure_sleep", (False, 300, False)) in service.hardware.commands
        assert service.sleep_now() is False
        service.configure_device({"auto_sleep": True, "sleep_timeout": 99})
        assert service.hardware.commands[-2] == ("configure_sleep", (False, 99, False))
        service.end("codex", "task")
        assert service.hardware.commands[-1] == ("configure_sleep", (True, 99, False))
        assert service.snapshot()["state"]["last_off"]["code"] == "session_end"
        assert service.sleep_now() is True
    finally:
        service.shutdown()


def test_sound_style_migrates_and_is_forwarded_to_hardware(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(service_module, "HardwareWorker", FakeHardware)
    config = ConfigManager(tmp_path / "config.json")
    service = service_module.AgentLightService(config)
    try:
        assert config.get("device")["sound_style"] == "standard"
        service.configure_device({"sound_style": "gentle"})
        assert service.play_sound("success") is True
        assert service.hardware.commands[-1] == ("sound", ("success", "gentle"))
    finally:
        service.shutdown()


def test_real_event_cancels_preview_and_restores_effective_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(service_module, "HardwareWorker", FakeHardware)
    service = service_module.AgentLightService(ConfigManager(tmp_path / "config.json"))
    try:
        service.preview_state("error")
        assert service.preview_snapshot()["active"] is True
        service.emit("codex", "task", "busy")
        assert service.preview_snapshot()["active"] is False
        assert service.hardware.applies[-1][0] == "busy"
        assert service.snapshot()["runtime_sleep_suppressed"] is True
    finally:
        service.shutdown()


def test_saving_a_software_effect_forces_the_stored_mode_to_static(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(service_module, "HardwareWorker", FakeHardware)
    service = service_module.AgentLightService(ConfigManager(tmp_path / "config.json"))
    try:
        # busy 默认是 breath；换成软件效果后固件必须保持常亮
        service.save_profiles({"busy": {"colors": ["ff0000", "00ff00", "0000ff"], "effect": "chase", "period_ms": 1800}})

        stored = service.config.get("profiles")["busy"]
        assert stored["effect"] == "chase"
        assert stored["mode"] == "static"
        assert stored["effect_params"] == {"direction": "forward", "duty": 1.0}
        assert stored["period_ms"] == 1800
    finally:
        service.shutdown()


def test_partial_profile_updates_keep_untouched_fields(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(service_module, "HardwareWorker", FakeHardware)
    service = service_module.AgentLightService(ConfigManager(tmp_path / "config.json"))
    try:
        service.save_profiles({"busy": {"brightness": 7}})

        stored = service.config.get("profiles")["busy"]
        assert stored["brightness"] == 7
        assert stored["colors"] == ["000000", "ff8000", "000000"]
        assert stored["effect"] == "breath"
    finally:
        service.shutdown()


def test_a_legacy_mode_only_update_overrides_a_stale_effect(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(service_module, "HardwareWorker", FakeHardware)
    service = service_module.AgentLightService(ConfigManager(tmp_path / "config.json"))
    try:
        service.save_profiles({"busy": {"effect": "chase"}})
        service.save_profiles({"busy": {"mode": "blink"}})

        stored = service.config.get("profiles")["busy"]
        assert stored["effect"] == "blink"
        assert stored["mode"] == "blink"
    finally:
        service.shutdown()


def test_unknown_effects_are_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(service_module, "HardwareWorker", FakeHardware)
    service = service_module.AgentLightService(ConfigManager(tmp_path / "config.json"))
    try:
        try:
            service.save_profiles({"busy": {"effect": "strobe"}})
        except ValueError as exc:
            assert "strobe" in str(exc)
        else:
            raise AssertionError("未知灯效应当被拒绝")
    finally:
        service.shutdown()


def test_switching_away_from_a_parameterised_effect_clears_its_params(tmp_path, monkeypatch) -> None:
    """切换灯效时完整替换档案，避免保留不适用的 effect_params。"""
    monkeypatch.setattr(service_module, "HardwareWorker", FakeHardware)
    service = service_module.AgentLightService(ConfigManager(tmp_path / "config.json"))
    try:
        service.save_profiles({"busy": {"effect": "comet", "effect_params": {"tail": 0.8, "direction": "backward"}}})
        assert service.config.get("profiles")["busy"]["effect_params"] == {"tail": 0.8, "direction": "backward"}

        service.save_profiles({"busy": {"effect": "breath", "effect_params": {}}})

        stored = service.config.get("profiles")["busy"]
        assert stored["effect"] == "breath"
        assert stored["effect_params"] == {}
    finally:
        service.shutdown()


def test_saving_one_state_leaves_the_others_untouched(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(service_module, "HardwareWorker", FakeHardware)
    service = service_module.AgentLightService(ConfigManager(tmp_path / "config.json"))
    try:
        before = service.config.get("profiles")

        service.save_profiles({"busy": {"effect": "chase"}})

        after = service.config.get("profiles")
        assert set(after) == set(before)
        for state in ("ready", "attention", "error", "done", "off"):
            assert after[state] == before[state]
    finally:
        service.shutdown()


def test_device_brightness_is_pushed_to_hardware_at_startup(tmp_path, monkeypatch) -> None:
    """启动时同步亮度配置，确保硬件状态与配置一致。"""
    monkeypatch.setattr(service_module, "HardwareWorker", FakeHardware)
    config = ConfigManager(tmp_path / "config.json")
    config.update({"device": {"base_brightness": 2, "max_brightness": 18}})

    service = service_module.AgentLightService(config)
    try:
        pushed = [c for c in service.hardware.commands if c[0] == "configure_led"]
        assert pushed, "启动时必须下发一次 configure_led"
        colors, base, ceiling = pushed[0][1]
        assert (base, ceiling) == (2, 18)
        assert colors == config.get("profiles")["off"]["colors"]
    finally:
        service.shutdown()


def test_startup_never_pushes_a_base_brightness_above_the_ceiling(tmp_path, monkeypatch) -> None:
    """base > max 会被固件拒绝，进而把设备误标成断开。"""
    monkeypatch.setattr(service_module, "HardwareWorker", FakeHardware)
    config = ConfigManager(tmp_path / "config.json")
    config.update({"device": {"base_brightness": 25, "max_brightness": 6}})

    service = service_module.AgentLightService(config)
    try:
        _, base, ceiling = [c for c in service.hardware.commands if c[0] == "configure_led"][0][1]
        assert base <= ceiling
    finally:
        service.shutdown()


def test_preview_uses_the_unsaved_draft_when_one_is_supplied(tmp_path, monkeypatch) -> None:
    """否则「在设备上试 5 秒」只能试到上次保存的样子，改了不保存就试不出来。"""
    monkeypatch.setattr(service_module, "HardwareWorker", FakeHardware)
    service = service_module.AgentLightService(ConfigManager(tmp_path / "config.json"))
    try:
        service.preview_state("busy", {
            "colors": ["ff0000", "00ff00", "0000ff"],
            "effect": "chase",
            "brightness": 44,
            "period_ms": 800,
        })

        sent = service.hardware.profiles[-1]
        assert sent.effect == "chase"
        assert sent.colors == ["ff0000", "00ff00", "0000ff"]
        assert sent.brightness == 44
        assert sent.period_ms == 800
        # 草稿只用于这次预览，不能落盘
        assert service.config.get("profiles")["busy"]["effect"] == "breath"
    finally:
        service.shutdown()


def test_preview_without_a_draft_still_uses_the_saved_profile(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(service_module, "HardwareWorker", FakeHardware)
    service = service_module.AgentLightService(ConfigManager(tmp_path / "config.json"))
    try:
        service.preview_state("busy")

        saved = service.config.get("profiles")["busy"]
        sent = service.hardware.profiles[-1]
        assert sent.effect == saved["effect"]
        assert sent.colors == saved["colors"]
    finally:
        service.shutdown()


def test_a_partial_draft_falls_back_to_the_saved_values(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(service_module, "HardwareWorker", FakeHardware)
    service = service_module.AgentLightService(ConfigManager(tmp_path / "config.json"))
    try:
        service.preview_state("busy", {"brightness": 70})

        sent = service.hardware.profiles[-1]
        assert sent.brightness == 70
        assert sent.colors == service.config.get("profiles")["busy"]["colors"]
    finally:
        service.shutdown()


def test_preview_rejects_an_unknown_effect_in_the_draft(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(service_module, "HardwareWorker", FakeHardware)
    service = service_module.AgentLightService(ConfigManager(tmp_path / "config.json"))
    try:
        try:
            service.preview_state("busy", {"effect": "strobe"})
        except ValueError as exc:
            assert "strobe" in str(exc)
        else:
            raise AssertionError("未知灯效应当被拒绝")
    finally:
        service.shutdown()
