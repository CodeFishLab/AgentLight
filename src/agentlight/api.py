from __future__ import annotations

import asyncio
import hmac
import json
import os
import socket
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

from aiohttp import WSMsgType, web

from . import __version__, effects
from .autostart import is_enabled as autostart_enabled, set_enabled as set_autostart
from .events import sanitize_identifier
from .paths import data_root, executable_dir, log_path, webui_dir
from .service import AgentLightService


# 配置页本身免 token：能连上回环端口的已经是本机进程，而跨站页面受同源策略限制
# 读不到响应体。真正的防线是 Host/Origin 校验加上 nosniff。
PUBLIC_PATHS = frozenset({"/", "/index.html"})
PUBLIC_PREFIXES = ("/assets/",)

TOKEN_PLACEHOLDER = "__AGENTLIGHT_TOKEN__"
# 浏览器的 WebSocket API 不能自定义请求头，token 只能借道子协议
WS_TOKEN_PREFIX = "agentlight.bearer."
MAX_LOG_TAIL = 400_000
DEFAULT_LOG_TAIL = 200_000


def reveal_path(target: str) -> Path:
    """把枚举值映射到固定目录。绝不接受客户端传入的任意路径。"""
    if target == "backups":
        return data_root() / "backups"
    if target == "logs":
        return data_root() / "logs"
    if target == "install":
        return executable_dir()
    raise ValueError(f"无效目录: {target}")


def uninstall_executable() -> Path | None:
    """只认安装目录里 Inno Setup 创建的固定卸载器。"""
    if os.name != "nt":
        return None
    root = executable_dir().resolve()
    target = (root / "unins000.exe").resolve()
    if target.parent != root or not target.is_file():
        return None
    return target


def validate_api_port(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("本地服务端口必须是 1024 到 65535 之间的整数")
    try:
        port = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("本地服务端口必须是 1024 到 65535 之间的整数") from exc
    if port != value or not 1024 <= port <= 65535:
        raise ValueError("本地服务端口必须是 1024 到 65535 之间的整数")
    return port


def port_is_available(host: str, port: int) -> bool:
    with socket.socket() as probe:
        try:
            probe.bind((host, port))
            return True
        except OSError:
            return False


MAX_STATUSLINE_WINDOWS = 12


def _clean_quota_window(item: Any) -> dict[str, Any] | None:
    """把一个额度窗口收敛成固定形状。

    这些字段会进状态快照、经 WebSocket 广播、最后渲染到配置页，所以只放行
    已知键，字符串限长，数字夹到合法区间。
    """
    if not isinstance(item, dict):
        return None
    percent = item.get("remaining_percent")
    if isinstance(percent, bool) or not isinstance(percent, (int, float)):
        return None
    used = item.get("used_percent")
    resets = item.get("resets_at")
    minutes = item.get("window_minutes")
    return {
        "key": sanitize_identifier(item.get("key"), fallback="window"),
        "label": str(item.get("label") or "")[:32] or "配额",
        "short": str(item.get("short") or "")[:32] or "配额",
        "window_minutes": int(minutes) if isinstance(minutes, int) and not isinstance(minutes, bool) else None,
        "used_percent": round(max(0.0, min(100.0, float(used))), 1) if isinstance(used, (int, float)) and not isinstance(used, bool) else None,
        "remaining_percent": max(0, min(100, round(float(percent)))),
        "resets_at": int(resets) if isinstance(resets, int) and not isinstance(resets, bool) and resets > 0 else None,
    }


async def read_payload(request: web.Request) -> dict[str, Any]:
    if not request.can_read_body:
        return {}
    try:
        data = await request.json()
    except Exception as exc:
        raise ValueError("请求体必须是合法 JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return data


class LocalApiServer:
    def __init__(
        self,
        service: AgentLightService,
        host: str,
        port: int,
        token: str,
        on_quit: Callable[[], None] | None = None,
        on_restart: Callable[[], None] | None = None,
    ) -> None:
        if host not in {"127.0.0.1", "localhost"}:
            raise ValueError("本地 API 只允许监听回环地址")
        self.service = service
        self.host = host
        self.port = int(port)
        self.token = token
        self.on_quit = on_quit
        self.on_restart = on_restart
        self._loop: asyncio.AbstractEventLoop | None = None
        self._runner: web.AppRunner | None = None
        self._thread = threading.Thread(target=self._thread_main, name="AgentLight-API", daemon=True)
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._clients: set[web.WebSocketResponse] = set()
        self.service.subscribe(self._service_changed)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self, timeout: float = 5) -> None:
        self._thread.start()
        if not self._ready.wait(timeout):
            raise TimeoutError("本地 API 启动超时")
        if self._error:
            raise RuntimeError(f"本地 API 启动失败: {self._error}")

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._start_async())
        except BaseException as exc:
            self._error = exc
            self._ready.set()
            return
        self._ready.set()
        self._loop.run_forever()
        self._loop.run_until_complete(self._stop_async())
        self._loop.close()

    async def _start_async(self) -> None:
        app = web.Application(
            client_max_size=256 * 1024,
            middlewares=[self._guard_middleware, self._error_middleware, self._auth_middleware],
        )
        app.add_routes([
            web.get("/", self._get_index),
            web.get("/index.html", self._get_index),
            web.get("/assets/{name:.*}", self._get_asset),

            web.get("/v1/status", self._get_status),
            web.get("/v1/timeline", self._get_timeline),
            web.post("/v1/events", self._post_event),
            web.delete("/v1/sessions/{source}/{session_id}", self._delete_session),
            web.get("/v1/stream", self._websocket),

            web.get("/v1/effects", self._get_effects),
            web.get("/v1/config", self._get_config),
            web.get("/v1/api-token", self._get_api_token),
            web.put("/v1/profiles", self._put_profiles),
            web.put("/v1/device", self._put_device),
            web.put("/v1/settings", self._put_settings),
            web.post("/v1/manual", self._post_manual),
            web.post("/v1/preview", self._post_preview),
            web.post("/v1/sound", self._post_sound),
            web.post("/v1/pause", self._post_pause),
            web.post("/v1/mute", self._post_mute),
            web.post("/v1/device/sleep", self._post_sleep),
            web.post("/v1/device/rest", self._post_device_rest),
            web.post("/v1/quota/sync", self._post_quota_sync),
            web.post("/v1/quota/statusline", self._post_quota_statusline),
            web.get("/v1/quota", self._get_quota),
            web.post("/v1/night-mode", self._post_night_mode),
            web.post("/v1/profiles/reset", self._post_reset_profiles),
            web.get("/v1/integrations", self._get_integrations),
            web.post("/v1/integrations", self._post_integrations),
            web.get("/v1/logs", self._get_logs),
            web.delete("/v1/logs", self._delete_logs),
            web.post("/v1/reveal", self._post_reveal),
            web.post("/v1/uninstall", self._post_uninstall),
            web.post("/v1/restart", self._post_restart),
            web.post("/v1/quit", self._post_quit),
        ])
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()

    # ------------------------------------------------------------------ 中间件

    def _allowed_hosts(self) -> set[str]:
        return {f"{name}:{self.port}" for name in (self.host, "127.0.0.1", "localhost")}

    def _allowed_origins(self) -> set[str]:
        return {f"http://{name}" for name in self._allowed_hosts()}

    @staticmethod
    def _is_public(path: str) -> bool:
        return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)

    @web.middleware
    async def _guard_middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        # 防 DNS rebinding：攻击者把自己的域名解析到 127.0.0.1 时，浏览器认为同源，
        # 但 Host 头仍是他的域名。
        if request.headers.get("Host", "") not in self._allowed_hosts():
            return web.json_response({"error": "bad host"}, status=421)
        origin = request.headers.get("Origin")
        if origin and origin not in self._allowed_origins():
            return web.json_response({"error": "bad origin"}, status=403)
        response = await handler(request)
        if not getattr(response, "prepared", False):
            # 没有 nosniff，攻击者可以用 <script src="http://127.0.0.1:47651/">
            # 跨站引入配置页并当成 JS 执行
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("Referrer-Policy", "no-referrer")
            response.headers.setdefault("Cache-Control", "no-store")
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                f"img-src 'self' data:; connect-src 'self' ws://127.0.0.1:{self.port} "
                f"ws://localhost:{self.port}; "
                "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            )
        return response

    @web.middleware
    async def _error_middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except (ValueError, TypeError, KeyError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    @staticmethod
    def _requested_ws_protocols(request: web.Request) -> list[str]:
        raw = request.headers.get("Sec-WebSocket-Protocol", "")
        return [item.strip() for item in raw.split(",") if item.strip().startswith(WS_TOKEN_PREFIX)]

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        if self._is_public(request.path):
            return await handler(request)
        supplied = (
            request.headers.get("Authorization", "").removeprefix("Bearer ")
            or request.headers.get("X-AgentLight-Token", "")
            or next((item[len(WS_TOKEN_PREFIX):] for item in self._requested_ws_protocols(request)), "")
        )
        if not self.token or not hmac.compare_digest(supplied, self.token):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    # ------------------------------------------------------------------ 静态资源

    async def _get_index(self, request: web.Request) -> web.Response:
        try:
            html = (webui_dir() / "index.html").read_text(encoding="utf-8")
        except OSError:
            return web.Response(text="配置页资源缺失，请重新安装。", status=500)
        return web.Response(
            text=html.replace(TOKEN_PLACEHOLDER, self.token),
            content_type="text/html",
            charset="utf-8",
            headers={"Cache-Control": "no-store"},
        )

    async def _get_asset(self, request: web.Request) -> web.StreamResponse:
        root = (webui_dir() / "assets").resolve()
        target = (root / request.match_info["name"]).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise web.HTTPNotFound()
        if not target.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(target, headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------------ 状态与事件

    async def _get_status(self, request: web.Request) -> web.Response:
        return web.json_response(self.service.snapshot())

    async def _get_timeline(self, request: web.Request) -> web.Response:
        """状态时间线。单独开一个端点而不是挂进 /v1/status：
        _notify() 在每个 hook 事件都会广播全量快照，忙起来一秒好几条，
        把历史挂上去等于每条广播都多带一份重复数据。"""
        return web.json_response(self.service.timeline())

    async def _post_event(self, request: web.Request) -> web.Response:
        payload = await read_payload(request)
        source = sanitize_identifier(payload.get("source"), fallback="")
        session_id = sanitize_identifier(payload.get("sessionId"), fallback="")
        if not source or not session_id:
            raise ValueError("source 和 sessionId 不能为空")
        effective = self.service.emit(
            source,
            session_id,
            str(payload.get("state", "")).strip(),
            payload.get("ttlSeconds"),
            sanitize_identifier(payload.get("eventName"), fallback=""),
        )
        return web.json_response({"ok": True, "effective": effective})

    async def _delete_session(self, request: web.Request) -> web.Response:
        effective = self.service.end(request.match_info["source"], request.match_info["session_id"])
        return web.json_response({"ok": True, "effective": effective})

    # ------------------------------------------------------------------ 配置

    async def _get_effects(self, request: web.Request) -> web.Response:
        return web.json_response({
            "effects": effects.registry_payload(),
            "frameRate": {
                "default": effects.DEFAULT_FRAME_RATE,
                "minimum": effects.MIN_FRAME_RATE,
                "maximum": effects.MAX_FRAME_RATE,
            },
        })

    async def _get_config(self, request: web.Request) -> web.Response:
        config = self.service.config.snapshot()
        config["api"] = {**config["api"], "token": "***"}
        return web.json_response({
            "config": config,
            "autostart": autostart_enabled(),
            "version": __version__,
            "installDir": str(executable_dir()),
            "dataDir": str(data_root()),
            "uninstallAvailable": uninstall_executable() is not None,
        })

    async def _get_api_token(self, request: web.Request) -> web.Response:
        # snapshot() 会把 token 打码，避免它随 WebSocket 广播出去；自定义接入页
        # 需要真值时走这个单独的端点。
        return web.json_response({"token": self.token, "endpoint": self.base_url})

    async def _put_profiles(self, request: web.Request) -> web.Response:
        payload = await read_payload(request)
        profiles = payload.get("profiles", payload)
        if not isinstance(profiles, dict) or not profiles:
            raise ValueError("profiles 不能为空")
        self.service.save_profiles(profiles)
        return web.json_response({"ok": True})

    async def _put_device(self, request: web.Request) -> web.Response:
        payload = await read_payload(request)
        self.service.configure_device(payload.get("device", payload))
        return web.json_response({"ok": True})

    async def _put_settings(self, request: web.Request) -> web.Response:
        payload = await read_payload(request)
        values = payload.get("settings", payload)
        if not isinstance(values, dict):
            raise ValueError("settings 必须是对象")
        values = dict(values)
        if "api_port" in values:
            port = validate_api_port(values.pop("api_port"))
            if port != self.port and not port_is_available(self.host, port):
                raise ValueError(f"端口 {port} 已被其他程序占用")
            current_api = self.service.config.snapshot().get("api", {})
            values["api"] = {**current_api, "port": port}
        if "start_with_windows" in values:
            set_autostart(bool(values["start_with_windows"]))
        self.service.update_runtime_settings(values)
        interval = int(self.service.config.get("quota_refresh_interval_seconds", 300))
        api_port = int(self.service.config.get("api", {}).get("port", self.port))
        return web.json_response({
            "ok": True,
            "autostart": autostart_enabled(),
            "quotaRefreshIntervalSeconds": interval,
            "apiPort": api_port,
        })

    # ------------------------------------------------------------------ 控制动作

    async def _post_manual(self, request: web.Request) -> web.Response:
        payload = await read_payload(request)
        state = payload.get("state")
        duration = payload.get("durationSeconds")
        effective = self.service.set_manual(None if state in (None, "", "auto") else str(state), duration)
        return web.json_response({"ok": True, "effective": effective})

    async def _post_preview(self, request: web.Request) -> web.Response:
        payload = await read_payload(request)
        profile = payload.get("profile")
        if profile is not None and not isinstance(profile, dict):
            raise ValueError("profile 必须是对象")
        self.service.preview_state(str(payload.get("state", "")), profile)
        return web.json_response({"ok": True, "preview": self.service.preview_snapshot()})

    async def _post_sound(self, request: web.Request) -> web.Response:
        payload = await read_payload(request)
        played = self.service.play_sound(str(payload.get("sound", "beep")))
        return web.json_response({"ok": True, "played": played})

    async def _post_pause(self, request: web.Request) -> web.Response:
        payload = await read_payload(request)
        self.service.set_paused(bool(payload.get("paused", False)))
        return web.json_response({"ok": True})

    async def _post_mute(self, request: web.Request) -> web.Response:
        payload = await read_payload(request)
        self.service.set_muted(bool(payload.get("muted", False)))
        return web.json_response({"ok": True})

    async def _post_sleep(self, request: web.Request) -> web.Response:
        accepted = self.service.sleep_now()
        return web.json_response({"ok": accepted})

    async def _post_device_rest(self, request: web.Request) -> web.Response:
        payload = await read_payload(request)
        if "resting" not in payload:
            raise ValueError("resting 必须是布尔值")
        self.service.set_device_resting(bool(payload["resting"]))
        return web.json_response({"ok": True, "resting": self.service.snapshot(False)["device_resting"]})

    async def _post_quota_statusline(self, request: web.Request) -> web.Response:
        """接收 Claude Code statusLine 解析出来的额度窗口。

        载荷来自 Claude Code 喂给状态栏的 stdin，属于外部输入：只收白名单字段，
        其余一律丢弃，别让它往状态快照里塞任意内容。
        """
        payload = await read_payload(request)
        raw = payload.get("windows")
        if not isinstance(raw, list):
            raise ValueError("windows 必须是数组")
        windows = [_clean_quota_window(item) for item in raw[:MAX_STATUSLINE_WINDOWS]]
        windows = [item for item in windows if item]
        self.service.record_claude_statusline(windows)
        return web.json_response({"ok": True, "accepted": len(windows)})

    async def _post_quota_sync(self, request: web.Request) -> web.Response:
        quota = await asyncio.to_thread(self.service.sync_codex_quota)
        return web.json_response({"ok": True, "quota": quota})

    async def _get_quota(self, request: web.Request) -> web.Response:
        refresh = request.query.get("refresh") in ("1", "true", "yes")
        source = request.query.get("source", "all").strip().lower()
        if source not in ("all", "codex", "claude"):
            raise ValueError("source 必须是 all、codex 或 claude")
        # 手动刷新可能等待后台 Claude 请求完成；放到 worker，不能阻塞 API event loop。
        result = await asyncio.to_thread(self.service.quota_snapshot, refresh, source)
        return web.json_response(result)

    async def _post_night_mode(self, request: web.Request) -> web.Response:
        payload = await read_payload(request)
        enabled = payload.get("enabled")
        scale = payload.get("scale")
        night = self.service.set_night_mode(
            None if enabled is None else bool(enabled),
            None if scale is None else int(scale),
        )
        return web.json_response({"ok": True, "nightMode": night})

    async def _post_reset_profiles(self, request: web.Request) -> web.Response:
        self.service.reset_profiles()
        return web.json_response({"ok": True})

    # ------------------------------------------------------------------ 集成与诊断

    async def _get_integrations(self, request: web.Request) -> web.Response:
        return web.json_response(self.service.integrations.status())

    async def _post_integrations(self, request: web.Request) -> web.Response:
        payload = await read_payload(request)
        action = str(payload.get("action", ""))
        # 只接受动作名。Hook 命令路径由服务端硬编码，绝不接受客户端传入，
        # 否则这个端点等于任意命令执行。
        if action == "install":
            result = self.service.integrations.install_all()
        elif action == "remove":
            result = self.service.integrations.remove_all()
        else:
            raise ValueError("action 必须是 install 或 remove")
        return web.json_response({"ok": True, "result": result, "status": self.service.integrations.status()})

    async def _get_logs(self, request: web.Request) -> web.Response:
        try:
            tail = int(request.query.get("tail", DEFAULT_LOG_TAIL))
        except ValueError:
            tail = DEFAULT_LOG_TAIL
        tail = max(1_000, min(MAX_LOG_TAIL, tail))
        try:
            text = log_path().read_text(encoding="utf-8", errors="replace")[-tail:]
        except OSError:
            text = ""
        return web.json_response({"text": text})

    async def _delete_logs(self, request: web.Request) -> web.Response:
        try:
            log_path().write_text("", encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"日志清空失败: {exc}") from exc
        return web.json_response({"ok": True})

    async def _post_reveal(self, request: web.Request) -> web.Response:
        payload = await read_payload(request)
        target = reveal_path(str(payload.get("target", "")))
        if os.name != "nt":
            raise ValueError("仅 Windows 支持打开目录")
        os.startfile(str(target))  # noqa: S606 - 路径来自服务端枚举，不含用户输入
        return web.json_response({"ok": True, "path": str(target)})

    async def _post_uninstall(self, request: web.Request) -> web.Response:
        target = uninstall_executable()
        if target is None:
            raise ValueError("当前运行方式没有可用的卸载程序")
        subprocess.Popen([str(target)], cwd=str(target.parent), close_fds=True)
        return web.json_response({"ok": True})

    async def _post_restart(self, request: web.Request) -> web.Response:
        if self.on_restart is None:
            raise web.HTTPNotImplemented(text="当前进程不支持远程重启")
        self.on_restart()
        return web.json_response({"ok": True})

    async def _post_quit(self, request: web.Request) -> web.Response:
        if self.on_quit is None:
            raise web.HTTPNotImplemented(text="当前进程不支持远程退出")
        self.on_quit()
        return web.json_response({"ok": True})

    # ------------------------------------------------------------------ WebSocket

    async def _websocket(self, request: web.Request) -> web.WebSocketResponse:
        # 回显客户端带 token 的子协议，否则浏览器会拒绝这次握手
        websocket = web.WebSocketResponse(heartbeat=20, protocols=self._requested_ws_protocols(request))
        await websocket.prepare(request)
        self._clients.add(websocket)
        await websocket.send_json(self.service.snapshot())
        try:
            async for message in websocket:
                if message.type == WSMsgType.TEXT and message.data == "ping":
                    await websocket.send_str("pong")
                elif message.type == WSMsgType.ERROR:
                    break
        finally:
            self._clients.discard(websocket)
        return websocket

    def _service_changed(self, snapshot: dict[str, Any]) -> None:
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._broadcast(snapshot), self._loop)

    async def _broadcast(self, snapshot: dict[str, Any]) -> None:
        if not self._clients:
            return
        encoded = json.dumps(snapshot, ensure_ascii=False)
        stale: list[web.WebSocketResponse] = []
        for client in self._clients:
            try:
                await client.send_str(encoded)
            except (ConnectionError, RuntimeError):
                stale.append(client)
        for client in stale:
            self._clients.discard(client)

    def stop(self) -> None:
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    async def _stop_async(self) -> None:
        for client in list(self._clients):
            await client.close()
        self._clients.clear()
        if self._runner:
            await self._runner.cleanup()
