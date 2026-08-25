"""设备休息：真的把连接关掉，而不只是不再改灯。"""

from __future__ import annotations

from typing import Any

from agentlight.device import HardwareWorker
from agentlight.models import StateProfile


class FakeController:
    """记下每一次真实的 HID 往来。休息期间这个列表必须一动不动。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.session_active = False

    def begin_session(self) -> None:
        self.session_active = True

    def end_session(self) -> None:
        self.session_active = False

    def status(self) -> dict[str, Any]:
        self.calls.append("status")
        return {"sleeping": False}

    def apply_profile(self, profile: StateProfile, play_sound: bool = True, sound_style: str = "standard") -> dict[str, Any]:
        self.calls.append(f"apply:{profile.effect}")
        return {"sleeping": False}

    def sleep_now(self) -> dict[str, Any]:
        self.calls.append("sleep_now")
        return {"sleeping": True}

    def configure_led(self, *args: Any) -> dict[str, Any]:
        self.calls.append("configure_led")
        return {}

    def set_quota(self, *args: Any) -> dict[str, Any]:
        self.calls.append("quota")
        return {}


def _worker() -> tuple[HardwareWorker, FakeController]:
    worker = HardwareWorker(force_reapply_seconds=10)
    controller = FakeController()
    worker.controller = controller
    return worker, controller


def _profile() -> StateProfile:
    return StateProfile.from_dict({"colors": ["#ff0000"], "effect": "static", "brightness": 10, "period_ms": 800, "sound": "none"})


def test_detach_sends_one_sleep_then_goes_completely_quiet() -> None:
    worker, controller = _worker()
    try:
        worker._execute("detach", ())
        assert controller.calls == ["sleep_now"]

        # 休息期间任何指令都不该落到设备上
        worker._execute("apply", ("busy", _profile(), False, True, "standard", None))
        worker._execute("quota", (10, 20))
        worker._poll()

        assert controller.calls == ["sleep_now"]
        assert worker.snapshot()["detached"] is True
        assert worker.snapshot()["connected"] is False
    finally:
        worker.stop()


def test_detach_is_not_recorded_as_a_device_failure() -> None:
    """主动关掉的连接不是掉线；记成事故的话诊断页会多出一条假故障。"""
    worker, controller = _worker()
    try:
        worker._execute("apply", ("busy", _profile(), False, True, "standard", None))
        worker._execute("detach", ())

        assert worker.snapshot()["last_interruption"] is None
        assert worker.snapshot()["error"] == ""
    finally:
        worker.stop()


def test_attach_restores_the_state_that_arrived_while_resting() -> None:
    """休息期间状态在变，恢复时要直接跳到最新的那个，而不是从熄灭重爬。"""
    worker, controller = _worker()
    try:
        worker._execute("detach", ())
        worker._execute("apply", ("error", _profile(), False, True, "standard", None))
        controller.calls.clear()

        worker._execute("attach", ())

        assert controller.calls == ["apply:static"]
        assert worker.snapshot()["detached"] is False
        assert worker.snapshot()["applied_state"] == "error"
    finally:
        worker.stop()


def test_attach_replays_the_sleep_settings_saved_while_resting() -> None:
    worker, _ = _worker()
    try:
        worker._execute("detach", ())
        worker._execute("configure_sleep", (True, 120, False))

        assert worker._desired_sleep == (True, 120, False)
    finally:
        worker.stop()


def test_a_missing_device_still_counts_as_successfully_detached() -> None:
    """设备本来就不在，「不再打扰它」这个目标已经达成了，不能卡在已连接。"""
    worker, controller = _worker()
    try:
        def boom() -> dict[str, Any]:
            raise OSError("device is gone")

        controller.sleep_now = boom  # type: ignore[method-assign]
        worker._execute("detach", ())

        assert worker.snapshot()["detached"] is True
        assert worker.snapshot()["connected"] is False
    finally:
        worker.stop()
