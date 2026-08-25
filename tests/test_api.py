from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass

import aiohttp

from agentlight.api import LocalApiServer


@dataclass
class FakeService:
    emitted: dict | None = None
    ended: tuple[str, str] | None = None

    def __post_init__(self) -> None:
        self._subscribers: list = []

    def subscribe(self, callback) -> None:
        self._subscribers.append(callback)

    def snapshot(self) -> dict:
        return {"effective": {"state": "ready"}, "device": {"connected": False}}

    def emit(
        self,
        source: str,
        session: str,
        state: str,
        ttl: int | None = None,
        event_name: str = "",
    ) -> dict:
        self.emitted = {
            "source": source,
            "session": session,
            "state": state,
            "ttl": ttl,
            "event_name": event_name,
        }
        return {"state": state}

    def end(self, source: str, session: str) -> dict:
        self.ended = (source, session)
        return {"state": "ready"}


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_http_auth_events_status_delete_and_websocket() -> None:
    service = FakeService()
    server = LocalApiServer(service, host="127.0.0.1", port=_free_port(), token="secret-token")
    server.start()

    async def exercise() -> None:
        base = f"http://127.0.0.1:{server.port}"
        headers = {"Authorization": "Bearer secret-token"}
        async with aiohttp.ClientSession() as client:
            async with client.get(f"{base}/v1/status") as response:
                assert response.status == 401

            async with client.get(f"{base}/v1/status", headers=headers) as response:
                assert response.status == 200
                assert (await response.json())["effective"]["state"] == "ready"

            async with client.post(
                f"{base}/v1/events",
                headers=headers,
                json={
                    "source": "pytest",
                    "sessionId": "api",
                    "state": "busy",
                    "ttlSeconds": 30,
                    "eventName": "test",
                },
            ) as response:
                assert response.status == 200
                assert (await response.json())["effective"]["state"] == "busy"

            assert service.emitted == {
                "source": "pytest",
                "session": "api",
                "state": "busy",
                "ttl": 30,
                "event_name": "test",
            }

            async with client.ws_connect(f"{base}/v1/stream", headers=headers) as websocket:
                initial = await websocket.receive_json(timeout=2)
                assert initial["effective"]["state"] == "ready"

            async with client.delete(f"{base}/v1/sessions/pytest/api", headers=headers) as response:
                assert response.status == 200

            assert service.ended == ("pytest", "api")

    try:
        asyncio.run(exercise())
    finally:
        server.stop()


def test_statusline_quota_intake_only_keeps_known_fields() -> None:
    """载荷来自 Claude Code 喂给状态栏的 stdin，是外部输入。
    它会进状态快照、经 WebSocket 广播、最后渲染到配置页，不能原样收下。"""
    from agentlight.api import _clean_quota_window

    cleaned = _clean_quota_window({
        "key": "five<script>_hour",
        "label": "x" * 80,
        "short": "5h",
        "remaining_percent": 142.7,
        "used_percent": -5,
        "resets_at": 1787811955,
        "window_minutes": 300,
        "注入": {"任意": "内容"},
    })

    assert "注入" not in cleaned
    assert "<" not in cleaned["key"]
    assert len(cleaned["label"]) <= 32
    assert cleaned["remaining_percent"] == 100
    assert cleaned["used_percent"] == 0.0


def test_statusline_quota_intake_rejects_unusable_rows() -> None:
    from agentlight.api import _clean_quota_window

    assert _clean_quota_window("not a dict") is None
    assert _clean_quota_window({"label": "5h"}) is None          # 没有百分比
    assert _clean_quota_window({"remaining_percent": True}) is None  # bool 不是数字
