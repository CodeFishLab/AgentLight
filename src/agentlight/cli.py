from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .config import ConfigManager
from .events import map_hook_payload, parse_hook_payload
from .integrations import IntegrationsManager
from . import statusline
from .models import STATE_LABELS, VALID_STATES
from .paths import data_root, gui_executable


def _api_settings() -> tuple[str, str]:
    api = ConfigManager().get("api")
    return f"http://{api['host']}:{api['port']}", str(api["token"])


def _request(method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 2) -> dict[str, Any]:
    base, token = _api_settings()
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _launch_app() -> None:
    executable = gui_executable()
    creationflags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS if os.name == "nt" else 0
    if executable.is_file():
        command = [str(executable), "--background"]
    else:
        command = [sys.executable, "-m", "agentlight.app", "--background"]
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags)


def _request_with_start(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        return _request(method, path, body)
    except (OSError, urllib.error.URLError, TimeoutError):
        _launch_app()
        last_error: BaseException | None = None
        for _ in range(30):
            time.sleep(0.1)
            try:
                return _request(method, path, body)
            except (OSError, urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
        raise RuntimeError(f"Agent Light 后台未就绪: {last_error}")


def _record_hook_error(message: str) -> None:
    try:
        path = data_root() / "logs" / "hook-errors.log"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def _hook(source: str) -> int:
    try:
        payload = parse_hook_payload(sys.stdin.read())
        mapped = map_hook_payload(source, payload)
        if mapped:
            _request_with_start("POST", "/v1/events", {
                "source": mapped.source,
                "sessionId": mapped.session_id,
                "state": mapped.state,
                "eventName": mapped.event_name,
            })
    except Exception as exc:
        _record_hook_error(str(exc))
    sys.stdout.write("{}\n")
    return 0


def _statusline() -> int:
    """Claude Code 的状态栏命令：顺手把额度采下来，再打一行给用户看。

    两条铁律：一是绝不抛异常（状态栏会把 stderr 一起显示，报错就是满屏红字），
    二是绝不启动后台（statusLine 每次渲染都跑，拉起进程会失控）。
    """
    line = ""
    try:
        payload = statusline.read_stdin_json()
        windows = statusline.extract_windows(payload)
        line = statusline.render_line(windows, _current_state_label())
        _push_statusline_quota(windows)
    except Exception as exc:  # noqa: BLE001 - 状态栏不能因为采集失败而报错
        _record_hook_error(f"statusline: {exc}")
    _write_utf8(line + "\n")
    return 0


def _push_statusline_quota(windows: list[dict[str, Any]]) -> None:
    """把额度转给后台。

    桌面端登录时 OAuth 那条路拿不到凭据（令牌在 Claude 应用自己的会话存储里），
    这些数字就是唯一还活着的来源。但这里是状态栏的热路径：后台没开就直接算了，
    绝不能为它拉起进程，也不能让超时把状态栏拖住。
    """
    if not windows:
        return
    try:
        _request("POST", "/v1/quota/statusline", {"windows": windows}, timeout=0.5)
    except Exception:  # noqa: BLE001 - 后台没开是常态，不是错误
        pass


def _write_utf8(text: str) -> None:
    """按 UTF-8 写 stdout。

    Windows 控制台的默认编码是 GBK，直接 sys.stdout.write 会把「剩余」和分隔点
    写成乱码 —— Claude Code 是按 UTF-8 读这行的。
    """
    stream = getattr(sys.stdout, "buffer", None)
    if stream is None:
        sys.stdout.write(text)
        return
    stream.write(text.encode("utf-8"))
    stream.flush()


def _current_state_label() -> str:
    """取当前灯的状态。后台没开就返回空串，绝不在这里拉起它。"""
    try:
        snapshot = _request("GET", "/v1/status", timeout=0.4)
    except Exception:  # noqa: BLE001 - 后台没开是常态，不是错误
        return ""
    effective = (snapshot.get("state") or {}).get("effective") or {}
    state = effective.get("state")
    return STATE_LABELS.get(state, "") if isinstance(state, str) else ""


def _shutdown(wait_seconds: float = 5.0) -> dict[str, Any]:
    """请求后台正常退出，并等到本地服务真正停止。不会为了退出而拉起应用。"""
    try:
        _request("POST", "/v1/quit", timeout=1)
    except urllib.error.HTTPError:
        raise
    except (OSError, urllib.error.URLError, TimeoutError):
        return {"ok": True, "running": False}

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        time.sleep(0.1)
        try:
            _request("GET", "/v1/status", timeout=0.25)
        except urllib.error.HTTPError:
            continue
        except (OSError, urllib.error.URLError, TimeoutError):
            return {"ok": True, "running": False}
    raise RuntimeError("Agent Light 在等待正常退出后仍在运行")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentlightctl", description="Agent Light 本地控制器")
    sub = parser.add_subparsers(dest="command", required=True)

    hook = sub.add_parser("hook", help="读取 Codex/Claude Hook JSON")
    hook.add_argument("--source", required=True)

    emit = sub.add_parser("emit", help="提交自定义 Agent 状态")
    emit.add_argument("--source", required=True)
    emit.add_argument("--session", required=True)
    emit.add_argument("--state", required=True, choices=VALID_STATES)
    emit.add_argument("--ttl", type=int)

    end = sub.add_parser("end", help="结束自定义 Agent 会话")
    end.add_argument("--source", required=True)
    end.add_argument("--session", required=True)

    sub.add_parser("status", help="读取聚合与设备状态")

    sub.add_parser("shutdown", help="请求 Agent Light 正常退出")

    sub.add_parser("statusline", help="Claude Code 状态栏：采集额度并输出一行")

    test = sub.add_parser("test", help="测试一种灯光状态")
    test.add_argument("state", choices=VALID_STATES)

    integrations = sub.add_parser("integrations", help="管理 Codex/Claude Hooks")
    integrations.add_argument("action", choices=("install", "remove", "status"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "hook":
        return _hook(args.source)
    if args.command == "statusline":
        return _statusline()
    try:
        if args.command == "emit":
            result = _request_with_start("POST", "/v1/events", {
                "source": args.source,
                "sessionId": args.session,
                "state": args.state,
                "ttlSeconds": args.ttl,
            })
        elif args.command == "end":
            source = urllib.parse.quote(args.source, safe="")
            session = urllib.parse.quote(args.session, safe="")
            result = _request_with_start("DELETE", f"/v1/sessions/{source}/{session}")
        elif args.command == "status":
            result = _request_with_start("GET", "/v1/status")
        elif args.command == "shutdown":
            result = _shutdown()
        elif args.command == "test":
            result = _request_with_start("POST", "/v1/events", {
                "source": "manual-test", "sessionId": "cli", "state": args.state, "ttlSeconds": 30
            })
        elif args.command == "integrations":
            manager = IntegrationsManager()
            if args.action == "install":
                result = manager.install_all()
            elif args.action == "remove":
                result = manager.remove_all()
            else:
                result = manager.status()
        else:
            raise RuntimeError("未知命令")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
