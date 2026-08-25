"""服务层的「设备休息」：是不是真的断，以及断了之后还记不记得住。"""

from __future__ import annotations

from agentlight.config import ConfigManager
from agentlight import service as service_module

from tests.test_service_audio import FakeHardware


def _service(tmp_path, monkeypatch, **overrides):
    monkeypatch.setattr(service_module, "HardwareWorker", FakeHardware)
    config = ConfigManager(tmp_path / "config.json")
    if overrides:
        config.update(overrides)
    return service_module.AgentLightService(config)


def test_resting_detaches_and_survives_a_restart(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch)
    try:
        service.set_device_resting(True)
        assert ("detach", ()) in service.hardware.commands
        assert service.snapshot(False)["device_resting"] is True
    finally:
        service.shutdown()

    # 同一份配置重新起一次：不能因为重启就把设备叫醒
    revived = _service(tmp_path, monkeypatch)
    try:
        assert revived.snapshot(False)["device_resting"] is True
        assert ("detach", ()) in revived.hardware.commands
    finally:
        revived.shutdown()


def test_waking_up_reattaches_and_resyncs_sleep(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch)
    try:
        service.set_device_resting(True)
        service.hardware.commands.clear()

        service.set_device_resting(False)

        names = [name for name, _ in service.hardware.commands]
        assert "attach" in names
        assert "configure_sleep" in names
        assert service.snapshot(False)["device_resting"] is False
    finally:
        service.shutdown()


def test_setting_the_same_value_twice_is_a_no_op(tmp_path, monkeypatch) -> None:
    """重复点不该重复发指令——每次 detach 都会真的叫醒设备再让它睡。"""
    service = _service(tmp_path, monkeypatch)
    try:
        service.set_device_resting(True)
        service.hardware.commands.clear()

        service.set_device_resting(True)

        assert service.hardware.commands == []
    finally:
        service.shutdown()


def test_resting_is_independent_of_pausing(tmp_path, monkeypatch) -> None:
    """暂停联动只是不再改灯，USB 上还在轮询；两个开关不能互相牵连。"""
    service = _service(tmp_path, monkeypatch)
    try:
        service.set_paused(True)
        assert service.snapshot(False)["device_resting"] is False

        service.set_device_resting(True)
        assert service.snapshot(False)["paused"] is True
    finally:
        service.shutdown()
