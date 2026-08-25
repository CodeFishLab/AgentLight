from __future__ import annotations

import time
from typing import Any

import pytest

from agentlight import device
from agentlight.device import OrvynController
from agentlight.models import StateProfile
from agentlight.vendor import orvyn_device


TRIO = ["ff0000", "00ff00", "0000ff"]


class FakeTransport:
    opened = 0

    def __init__(self, timeout_ms: int = 1500) -> None:
        FakeTransport.opened += 1
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeClient:
    def __init__(self, transport: FakeTransport) -> None:
        self.transport = transport
        self.writes: list[tuple[int, bytes]] = []
        self.fail_next = False

    def app_channel_write(self, command: int, data: bytes = b"") -> None:
        if self.fail_next:
            raise OSError("HID write failed")
        self.writes.append((command, data))

    def app_channel_read(self) -> dict[str, Any]:
        command, _ = self.writes[-1]
        return {"command": command | 0x80, "data": bytes(38)}


@pytest.fixture()
def fake_hid(monkeypatch: pytest.MonkeyPatch) -> list[FakeClient]:
    clients: list[FakeClient] = []
    FakeTransport.opened = 0

    def build_client(transport: FakeTransport) -> FakeClient:
        client = FakeClient(transport)
        clients.append(client)
        return client

    monkeypatch.setattr(orvyn_device, "HidTransport", FakeTransport)
    monkeypatch.setattr(orvyn_device, "RuntimeClient", build_client)
    return clients


def test_push_colors_sends_nine_bytes_on_the_app_colour_command(fake_hid: list[FakeClient]) -> None:
    controller = OrvynController()

    controller.push_colors([(255, 0, 0), (0, 128, 0), (0, 0, 255)])

    command, payload = fake_hid[0].writes[0]
    assert command == 0x11
    assert payload == bytes.fromhex("ff0000008000" + "0000ff")


def test_push_colors_clamps_channels_and_pads_missing_leds(fake_hid: list[FakeClient]) -> None:
    controller = OrvynController()

    controller.push_colors([(999, -5, 12)])

    _, payload = fake_hid[0].writes[0]
    assert payload == bytes([255, 0, 12, 0, 0, 0, 0, 0, 0])


def test_without_a_session_every_command_reopens_the_device(fake_hid: list[FakeClient]) -> None:
    controller = OrvynController()

    controller.push_colors([(1, 1, 1)])
    controller.push_colors([(2, 2, 2)])

    assert FakeTransport.opened == 2
    assert all(client.transport.closed for client in fake_hid)


def test_a_session_reuses_one_connection_across_frames(fake_hid: list[FakeClient]) -> None:
    controller = OrvynController()

    with controller.session():
        for _ in range(5):
            controller.push_colors([(1, 1, 1)])
        assert controller.session_active is True

    assert FakeTransport.opened == 1
    assert len(fake_hid[0].writes) == 5
    assert controller.session_active is False
    assert fake_hid[0].transport.closed is True


def test_a_failed_frame_drops_the_session_so_the_next_call_reconnects(fake_hid: list[FakeClient]) -> None:
    controller = OrvynController()
    controller.begin_session()
    fake_hid[0].fail_next = True

    with pytest.raises(OSError):
        controller.push_colors([(1, 1, 1)])

    assert controller.session_active is False
    assert fake_hid[0].transport.closed is True

    # 拔插自愈：下一条命令退回开-用-关
    controller.push_colors([(2, 2, 2)])
    assert FakeTransport.opened == 2


def test_end_session_is_idempotent(fake_hid: list[FakeClient]) -> None:
    controller = OrvynController()
    controller.begin_session()
    controller.begin_session()

    controller.end_session()
    controller.end_session()

    assert FakeTransport.opened == 1
    assert controller.session_active is False


class FakeController:
    def __init__(self) -> None:
        self.frames: list[list[tuple[int, int, int]]] = []
        self.applied: list[StateProfile] = []
        self.session_active = False
        self.opened = 0
        self.push_error: Exception | None = None

    def begin_session(self) -> None:
        self.session_active = True
        self.opened += 1

    def end_session(self) -> None:
        self.session_active = False

    def push_colors(self, frame: list[tuple[int, int, int]]) -> None:
        if self.push_error:
            raise self.push_error
        self.frames.append(list(frame))

    def status(self) -> dict[str, Any]:
        raise OSError("device not connected")

    def apply_profile(self, profile: StateProfile, play_sound: bool = True, sound_style: str = "standard") -> dict[str, Any]:
        self.applied.append(profile)
        return {}


@pytest.fixture()
def worker(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, FakeController]:
    controller = FakeController()
    monkeypatch.setattr(device, "OrvynController", lambda *args, **kwargs: controller)
    instance = device.HardwareWorker(frame_rate=10)
    # 停掉后台线程，后面同步调用各方法，避免时序不稳定
    instance.stop()
    return instance, controller


def software_profile(effect: str = "chase", period_ms: int = 1000) -> StateProfile:
    return StateProfile.from_dict({"colors": TRIO, "effect": effect, "period_ms": period_ms})


def test_software_effect_opens_a_session_and_streams_frames(worker: tuple[Any, FakeController]) -> None:
    instance, controller = worker

    instance._sync_animation(software_profile())

    assert controller.session_active is True
    snapshot = instance.snapshot()
    assert snapshot["animating"] is True
    assert snapshot["effect"] == "chase"
    assert snapshot["frame_rate"] == 10

    instance._push_animation_frame()
    assert controller.frames == [[(255, 0, 0), (0, 0, 0), (0, 0, 0)]]


def test_hardware_effect_leaves_the_animation_loop_idle(worker: tuple[Any, FakeController]) -> None:
    instance, controller = worker

    instance._sync_animation(StateProfile.from_dict({"colors": TRIO, "effect": "breath"}))

    assert controller.session_active is False
    assert controller.frames == []
    snapshot = instance.snapshot()
    assert snapshot["animating"] is False
    assert snapshot["effect"] == "breath"


def test_switching_from_software_to_hardware_closes_the_session(worker: tuple[Any, FakeController]) -> None:
    instance, controller = worker
    instance._sync_animation(software_profile())

    instance._sync_animation(StateProfile.from_dict({"colors": TRIO, "effect": "static"}))

    assert controller.session_active is False
    assert instance.snapshot()["animating"] is False
    assert instance.snapshot()["frame_rate"] == 0


def test_frame_phase_advances_with_elapsed_time(worker: tuple[Any, FakeController]) -> None:
    instance, controller = worker
    instance._sync_animation(software_profile(period_ms=1000))

    instance._push_animation_frame()
    # 往前拨半个周期，跑马灯应该走到第二颗灯
    instance._animation_started -= 0.5
    instance._push_animation_frame()

    assert controller.frames[0] == [(255, 0, 0), (0, 0, 0), (0, 0, 0)]
    assert controller.frames[1] == [(0, 0, 0), (0, 255, 0), (0, 0, 0)]


def test_a_frame_failure_stops_the_animation_and_marks_disconnected(worker: tuple[Any, FakeController]) -> None:
    instance, controller = worker
    instance._sync_animation(software_profile())
    controller.push_error = OSError("device unplugged")

    instance._push_animation_frame()

    assert controller.session_active is False
    snapshot = instance.snapshot()
    assert snapshot["animating"] is False
    assert snapshot["connected"] is False
    assert "unplugged" in snapshot["error"]


def test_frame_interval_follows_the_configured_rate(worker: tuple[Any, FakeController]) -> None:
    instance, _ = worker

    assert instance.frame_interval == pytest.approx(0.1)
    instance.set_frame_rate(20)
    assert instance.frame_interval == pytest.approx(0.05)
    instance.set_frame_rate(9999)
    assert instance.frame_rate == device.effects.MAX_FRAME_RATE


def test_applying_the_same_state_with_a_new_effect_restarts_the_animation(worker: tuple[Any, FakeController]) -> None:
    instance, controller = worker
    instance._execute("apply", ("busy", StateProfile.from_dict({"colors": TRIO, "effect": "static"}), False, False, "standard", None))
    assert controller.session_active is False

    instance._execute("apply", ("busy", software_profile(), False, False, "standard", None))

    assert controller.session_active is True
    assert instance.snapshot()["animating"] is True
    assert len(controller.applied) == 2


def test_stopping_the_worker_releases_the_session(monkeypatch: pytest.MonkeyPatch) -> None:
    controller = FakeController()
    monkeypatch.setattr(device, "OrvynController", lambda *args, **kwargs: controller)
    instance = device.HardwareWorker(frame_rate=10)
    instance._sync_animation(software_profile())
    assert controller.session_active is True

    instance.stop()

    deadline = time.monotonic() + 3
    while controller.session_active and time.monotonic() < deadline:
        time.sleep(0.01)
    assert controller.session_active is False
