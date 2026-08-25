"""托盘重启：拉起新进程的时机比拉起本身更容易出错。"""

from __future__ import annotations

import inspect
import socket

from agentlight import app as app_module


def test_relaunch_hands_the_new_process_the_await_port_flag(monkeypatch) -> None:
    """没有这个 flag，新进程会撞上还没释放的端口，把自己当成第二实例直接退出。"""
    captured: list[list[str]] = []
    monkeypatch.setattr(app_module.subprocess, "Popen", lambda command, **kwargs: captured.append(command))

    app_module.relaunch()

    assert captured, "没有拉起任何进程"
    assert "--await-port" in captured[0]
    assert "--background" in captured[0]


def test_restart_only_relaunches_after_everything_is_torn_down() -> None:
    """顺序反了就是两个进程抢同一个 HID 设备和同一个端口。"""
    source = inspect.getsource(app_module.main)
    assert source.index("service.shutdown()") < source.index("relaunch()")
    assert source.index("api.stop()") < source.index("relaunch()")


def test_local_api_can_request_the_same_graceful_restart_as_the_tray() -> None:
    source = inspect.getsource(app_module.main)

    assert "on_restart=request_restart" in source


def test_await_port_waits_before_the_service_touches_the_hardware() -> None:
    source = inspect.getsource(app_module.main)
    assert source.index("args.await_port") < source.index("service = AgentLightService()")


def test_wait_for_free_port_returns_at_once_when_nothing_is_listening() -> None:
    assert app_module.wait_for_free_port("127.0.0.1", 0, timeout=0.5) is True


def test_wait_for_free_port_gives_up_instead_of_hanging_forever() -> None:
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]

        assert app_module.wait_for_free_port("127.0.0.1", port, timeout=0.5) is False
