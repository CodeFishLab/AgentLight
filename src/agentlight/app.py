from __future__ import annotations

import argparse
import ctypes
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from typing import Any

from .api import LocalApiServer
from .config import ConfigManager
from .integrations import IntegrationsManager
from .models import STATE_LABELS
from .paths import gui_executable
from .service import AgentLightService
from .tray import TrayIcon


MB_ICONERROR = 0x10


def _alert(title: str, text: str) -> None:
    """打包后没有控制台，启动失败只能弹窗告知。"""
    try:
        ctypes.WinDLL("user32").MessageBoxW(None, text, title, MB_ICONERROR)
    except Exception:
        print(f"{title}: {text}", file=sys.stderr)


def port_is_free(host: str, port: int) -> bool:
    """端口占用即视为已有实例在跑。

    注意不要设 SO_REUSEADDR：Windows 上它会允许重复绑定同一地址，单实例检测会失效。
    """
    with socket.socket() as probe:
        try:
            probe.bind((host, port))
            return True
        except OSError:
            return False


RESTART_PORT_WAIT_SECONDS = 15.0


def wait_for_free_port(host: str, port: int, timeout: float = RESTART_PORT_WAIT_SECONDS) -> bool:
    """等旧进程把端口让出来。

    单实例检测靠的就是这个端口。重启拉起的新进程如果跑得比旧进程收尾还快，
    就会认定「已经有实例在跑」，只弹个网页然后退出——重启看上去就像没反应。
    """
    deadline = time.monotonic() + timeout
    while True:
        if port_is_free(host, port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)


def relaunch() -> None:
    """拉起一个新的自己。必须在 api / service 都停掉之后再调。"""
    executable = gui_executable()
    creationflags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS if os.name == "nt" else 0
    if executable.is_file():
        command = [str(executable), "--background", "--await-port"]
    else:
        command = [sys.executable, "-m", "agentlight.app", "--background", "--await-port"]
    subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )


def tray_state(service: AgentLightService) -> dict[str, Any]:
    """喂给托盘菜单的三个开关。一次快照读完，别在 lambda 里反复取。"""
    snapshot = service.snapshot(False)
    return {
        "muted": snapshot["muted"],
        "paused": snapshot["paused"],
        "resting": snapshot["device_resting"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="AgentLight", add_help=True)
    parser.add_argument("--background", action="store_true", help="启动后不自动打开配置页")
    parser.add_argument("--remove-integrations", action="store_true", help="只移除本应用写入的 Hook 条目")
    parser.add_argument(
        "--await-port",
        action="store_true",
        help="启动前先等本地端口释放（重启时由旧进程带上，手动一般用不到）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.remove_integrations:
        IntegrationsManager().remove_all()
        return 0

    if args.await_port:
        # 必须在建 service 之前等：service 一构造就开始碰硬件，
        # 两个进程同时抢同一个 HID 设备会互相打架
        api_config = ConfigManager().get("api")
        wait_for_free_port(str(api_config["host"]), int(api_config["port"]))

    service = AgentLightService()
    api_settings = service.config.get("api")
    host = str(api_settings["host"])
    port = int(api_settings["port"])
    url = f"http://{host}:{port}/"

    # 单实例：端口被占说明后台已经在跑，直接把配置页顶到前面来就够了
    if not port_is_free(host, port):
        service.shutdown()
        webbrowser.open(url)
        return 0

    stop = threading.Event()
    restart = threading.Event()

    def request_restart() -> None:
        restart.set()
        stop.set()

    api = LocalApiServer(
        service,
        host,
        port,
        str(api_settings["token"]),
        on_quit=stop.set,
        on_restart=request_restart,
    )
    try:
        api.start()
    except Exception as exc:
        service.shutdown()
        _alert("启动失败", str(exc))
        return 1

    service.start_background_services()

    tray = TrayIcon(
        tooltip="Agent 状态灯",
        on_open=lambda: webbrowser.open(url),
        on_mute=service.set_muted,
        on_pause=service.set_paused,
        on_quit=stop.set,
        on_rest=service.set_device_resting,
        on_restart=request_restart,
        state_provider=lambda: tray_state(service),
    )
    tray.start()

    def on_change(snapshot: dict[str, Any]) -> None:
        effective = snapshot["state"]["effective"]
        label = STATE_LABELS.get(effective["state"], effective["state"])
        tray.set_tooltip(f"Agent 状态灯 · {label} · {effective['source']}")

    service.subscribe(on_change)

    if not args.background:
        webbrowser.open(url)

    try:
        while not stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass

    tray.stop()
    api.stop()
    service.shutdown()
    if restart.is_set():
        relaunch()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
