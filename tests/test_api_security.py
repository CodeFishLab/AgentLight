from __future__ import annotations

import asyncio
import socket
from typing import Any

import aiohttp
import pytest
from yarl import URL

from agentlight import api as api_module
from agentlight.api import LocalApiServer, reveal_path


TOKEN = "secret-token"


class FakeConfig:
    def __init__(self) -> None:
        self.data = {"api": {"host": "127.0.0.1", "port": 1, "token": "real-token"}, "profiles": {}}

    def snapshot(self) -> dict[str, Any]:
        return {"api": dict(self.data["api"]), "profiles": {}}

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def update(self, values: dict[str, Any]) -> None:
        self.data.update(values)


class FakeIntegrations:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def status(self) -> dict[str, Any]:
        return {"codex": {"installed": False}}

    def install_all(self) -> dict[str, str]:
        self.calls.append("install")
        return {"codex": "installed"}

    def remove_all(self) -> dict[str, bool]:
        self.calls.append("remove")
        return {"codex": True}


class FakeService:
    def __init__(self) -> None:
        self.config = FakeConfig()
        self.integrations = FakeIntegrations()
        self.emitted: dict[str, Any] | None = None
        self.updated_settings: dict[str, Any] | None = None

    def subscribe(self, callback: Any) -> None:
        return None

    def snapshot(self, include_integrations: bool = True) -> dict[str, Any]:
        return {"effective": {"state": "ready"}}

    def emit(self, source: str, session: str, state: str, ttl: Any = None, event_name: str = "") -> dict[str, Any]:
        self.emitted = {"source": source, "session": session, "state": state, "event_name": event_name}
        return {"state": state}

    def end(self, source: str, session: str) -> dict[str, Any]:
        return {"state": "ready"}

    def update_runtime_settings(self, values: dict[str, Any]) -> None:
        self.updated_settings = values
        self.config.update(values)


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def run_server(exercise: Any, service: FakeService | None = None, **server_options: Any) -> FakeService:
    service = service or FakeService()
    server = LocalApiServer(service, host="127.0.0.1", port=_free_port(), token=TOKEN, **server_options)
    server.start()
    try:
        asyncio.run(exercise(f"http://127.0.0.1:{server.port}"))
    finally:
        server.stop()
    return service


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_server_refuses_non_loopback_bind_addresses() -> None:
    with pytest.raises(ValueError, match="回环地址"):
        LocalApiServer(FakeService(), host="0.0.0.0", port=_free_port(), token=TOKEN)


def test_a_rebinding_host_header_is_rejected_before_auth() -> None:
    async def exercise(base: str) -> None:
        async with aiohttp.ClientSession() as client:
            # 攻击者把 evil.com 解析到 127.0.0.1，浏览器认为同源，但 Host 头会暴露
            async with client.get(f"{base}/v1/status", headers={**auth(), "Host": "evil.com"}) as response:
                assert response.status == 421
            # 连带宕掉免 token 的配置页，否则页面本身会被跨站读取
            async with client.get(f"{base}/", headers={"Host": "attacker.test:80"}) as response:
                assert response.status == 421

    run_server(exercise)


def test_localhost_and_loopback_hosts_are_both_accepted() -> None:
    async def exercise(base: str) -> None:
        port = base.rsplit(":", 1)[1]
        async with aiohttp.ClientSession() as client:
            for host in (f"127.0.0.1:{port}", f"localhost:{port}"):
                async with client.get(f"{base}/v1/status", headers={**auth(), "Host": host}) as response:
                    assert response.status == 200

    run_server(exercise)


def test_cross_site_origin_is_rejected_but_absent_origin_still_works() -> None:
    async def exercise(base: str) -> None:
        async with aiohttp.ClientSession() as client:
            async with client.get(f"{base}/v1/status", headers={**auth(), "Origin": "http://evil.com"}) as response:
                assert response.status == 403
            # 命令行客户端不发 Origin，必须继续可用
            async with client.get(f"{base}/v1/status", headers=auth()) as response:
                assert response.status == 200
            async with client.get(f"{base}/v1/status", headers={**auth(), "Origin": base}) as response:
                assert response.status == 200

    run_server(exercise)


def test_api_routes_still_require_the_bearer_token() -> None:
    async def exercise(base: str) -> None:
        async with aiohttp.ClientSession() as client:
            for path in ("/v1/status", "/v1/config", "/v1/effects", "/v1/api-token", "/v1/logs"):
                async with client.get(f"{base}{path}") as response:
                    assert response.status == 401, path
            async with client.post(f"{base}/v1/integrations", json={"action": "install"}) as response:
                assert response.status == 401
            for path in ("/v1/uninstall", "/v1/restart"):
                async with client.post(f"{base}{path}") as response:
                    assert response.status == 401

    run_server(exercise)


def test_the_config_page_is_reachable_without_a_token_and_carries_nosniff(tmp_path, monkeypatch) -> None:
    (tmp_path / "index.html").write_text(
        '<meta name="agentlight-token" content="__AGENTLIGHT_TOKEN__">',
        encoding="utf-8",
    )
    monkeypatch.setattr(api_module, "webui_dir", lambda: tmp_path)

    async def exercise(base: str) -> None:
        async with aiohttp.ClientSession() as client:
            async with client.get(f"{base}/") as response:
                assert response.status == 200
                assert response.content_type == "text/html"
                # 没有 nosniff，攻击者能用 <script src> 跨站引入这个页面并执行
                assert response.headers["X-Content-Type-Options"] == "nosniff"
                assert response.headers["Cache-Control"] == "no-store"
                assert response.headers["Content-Security-Policy"].endswith("frame-ancestors 'none'")
                body = await response.text()
                assert TOKEN in body
                assert "__AGENTLIGHT_TOKEN__" not in body

    run_server(exercise)


def test_asset_route_refuses_path_traversal(tmp_path, monkeypatch) -> None:
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.css").write_text("body{}", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    monkeypatch.setattr(api_module, "webui_dir", lambda: tmp_path)

    async def exercise(base: str) -> None:
        async with aiohttp.ClientSession() as client:
            async with client.get(f"{base}/assets/app.css") as response:
                assert response.status == 200
                assert await response.text() == "body{}"
            # encoded=True 绕过客户端的 URL 规范化，让原始路径真正打到服务端
            for attempt in ("..%2Fsecret.txt", "..%2F..%2Fsecret.txt", "%2e%2e%2fsecret.txt"):
                url = URL(f"{base}/assets/{attempt}", encoded=True)
                async with client.get(url) as response:
                    assert response.status == 404, attempt
                    assert "nope" not in await response.text()

    run_server(exercise)


def test_reveal_only_accepts_the_server_side_enumeration() -> None:
    assert reveal_path("logs").name == "logs"
    assert reveal_path("backups").name == "backups"
    for bad in ("C:\\Windows", "../..", "", "config"):
        with pytest.raises(ValueError):
            reveal_path(bad)

    async def exercise(base: str) -> None:
        async with aiohttp.ClientSession() as client:
            async with client.post(f"{base}/v1/reveal", headers=auth(), json={"target": "C:\\Windows"}) as response:
                assert response.status == 400

    run_server(exercise)


def test_uninstall_endpoint_launches_only_the_fixed_inno_uninstaller(tmp_path, monkeypatch) -> None:
    target = tmp_path / "unins000.exe"
    target.write_bytes(b"test")
    launched: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(api_module, "uninstall_executable", lambda: target)
    monkeypatch.setattr(
        api_module.subprocess,
        "Popen",
        lambda command, **options: launched.append((command, options)),
    )

    async def exercise(base: str) -> None:
        async with aiohttp.ClientSession() as client:
            async with client.post(f"{base}/v1/uninstall", headers=auth()) as response:
                assert response.status == 200
                assert (await response.json())["ok"] is True

    run_server(exercise)
    assert launched == [([str(target)], {"cwd": str(tmp_path), "close_fds": True})]


def test_restart_endpoint_uses_the_app_callback() -> None:
    restarted: list[bool] = []

    async def exercise(base: str) -> None:
        async with aiohttp.ClientSession() as client:
            async with client.post(f"{base}/v1/restart", headers=auth()) as response:
                assert response.status == 200

    run_server(exercise, on_restart=lambda: restarted.append(True))
    assert restarted == [True]


def test_api_port_update_preserves_host_and_token() -> None:
    service = FakeService()
    desired = _free_port()

    async def exercise(base: str) -> None:
        async with aiohttp.ClientSession() as client:
            async with client.put(
                f"{base}/v1/settings",
                headers=auth(),
                json={"api_port": desired},
            ) as response:
                assert response.status == 200
                assert (await response.json())["apiPort"] == desired

    run_server(exercise, service)
    assert service.updated_settings is not None
    assert service.updated_settings["api"] == {
        "host": "127.0.0.1",
        "port": desired,
        "token": "real-token",
    }


def test_api_port_rejects_invalid_or_occupied_values() -> None:
    service = FakeService()

    async def exercise(base: str) -> None:
        async with aiohttp.ClientSession() as client:
            for value in (True, 80, 65536, "47651"):
                async with client.put(
                    f"{base}/v1/settings",
                    headers=auth(),
                    json={"api_port": value},
                ) as response:
                    assert response.status == 400, value
            with socket.socket() as occupied:
                occupied.bind(("127.0.0.1", 0))
                port = int(occupied.getsockname()[1])
                async with client.put(
                    f"{base}/v1/settings",
                    headers=auth(),
                    json={"api_port": port},
                ) as response:
                    assert response.status == 400

    run_server(exercise, service)


def test_integrations_endpoint_takes_an_action_name_and_nothing_else() -> None:
    service = FakeService()

    async def exercise(base: str) -> None:
        async with aiohttp.ClientSession() as client:
            # 带路径的请求不能改变 Hook 命令；服务端只看 action
            async with client.post(
                f"{base}/v1/integrations",
                headers=auth(),
                json={"action": "install", "ctl_path": "C:\\evil.exe", "command": "calc.exe"},
            ) as response:
                assert response.status == 200
            async with client.post(f"{base}/v1/integrations", headers=auth(), json={"action": "calc.exe"}) as response:
                assert response.status == 400

    run_server(exercise, service)
    assert service.integrations.calls == ["install"]


def test_event_identifiers_are_sanitised_before_they_reach_the_service() -> None:
    service = FakeService()

    async def exercise(base: str) -> None:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{base}/v1/events",
                headers=auth(),
                json={
                    "source": "<script>alert(1)</script>",
                    "sessionId": "a" * 500,
                    "state": "busy",
                    "eventName": "<img onerror=x>",
                },
            ) as response:
                assert response.status == 200

    run_server(exercise, service)
    assert service.emitted is not None
    assert "<" not in service.emitted["source"] and ">" not in service.emitted["source"]
    assert service.emitted["source"] == "scriptalert1script"
    # 超长标识符会被截断，避免撑爆 WebSocket 广播
    assert len(service.emitted["session"]) == 64
    assert "<" not in service.emitted["event_name"]


def test_malformed_bodies_produce_400_not_500() -> None:
    async def exercise(base: str) -> None:
        async with aiohttp.ClientSession() as client:
            async with client.post(f"{base}/v1/events", headers=auth(), data="not json") as response:
                assert response.status == 400
            async with client.post(f"{base}/v1/events", headers=auth(), json=["a", "list"]) as response:
                assert response.status == 400
            async with client.post(f"{base}/v1/events", headers=auth(), json={"source": "", "sessionId": ""}) as response:
                assert response.status == 400

    run_server(exercise)


def test_config_endpoint_masks_the_token_but_the_dedicated_route_returns_it() -> None:
    async def exercise(base: str) -> None:
        async with aiohttp.ClientSession() as client:
            async with client.get(f"{base}/v1/config", headers=auth()) as response:
                payload = await response.json()
                assert payload["config"]["api"]["token"] == "***"
            async with client.get(f"{base}/v1/api-token", headers=auth()) as response:
                payload = await response.json()
                assert payload["token"] == TOKEN
                assert payload["endpoint"] == base

    run_server(exercise)


def test_effects_registry_is_served_for_the_web_ui() -> None:
    async def exercise(base: str) -> None:
        async with aiohttp.ClientSession() as client:
            async with client.get(f"{base}/v1/effects", headers=auth()) as response:
                payload = await response.json()
                identifiers = {item["id"] for item in payload["effects"]}
                assert {"static", "blink", "breath", "chase", "comet", "rainbow"} <= identifiers
                assert payload["frameRate"]["default"] == 20

    run_server(exercise)
