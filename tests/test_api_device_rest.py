"""/v1/device/rest：托盘和配置页共用同一个开关，两边不能各说各的。"""

from __future__ import annotations

import asyncio
import socket

import aiohttp

from agentlight.api import LocalApiServer


class FakeService:
    def __init__(self) -> None:
        self.resting = False
        self.calls: list[bool] = []
        self._subscribers: list = []

    def subscribe(self, callback) -> None:
        self._subscribers.append(callback)

    def snapshot(self, include_integrations: bool = True) -> dict:
        return {
            "effective": {"state": "ready"},
            "device": {"connected": False},
            "device_resting": self.resting,
        }

    def set_device_resting(self, resting: bool) -> None:
        self.calls.append(resting)
        self.resting = resting


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_rest_endpoint_toggles_and_reports_the_state_it_actually_reached() -> None:
    service = FakeService()
    port = _free_port()
    server = LocalApiServer(service, "127.0.0.1", port, "secret")

    async def scenario() -> None:
        server.start()
        await asyncio.sleep(0.4)
        headers = {"Authorization": "Bearer secret"}
        base = f"http://127.0.0.1:{port}"
        async with aiohttp.ClientSession(headers=headers) as http:
            async with http.post(f"{base}/v1/device/rest", json={"resting": True}) as response:
                assert response.status == 200
                assert (await response.json())["resting"] is True

            async with http.post(f"{base}/v1/device/rest", json={"resting": False}) as response:
                assert (await response.json())["resting"] is False

            # 缺字段要报 400，不能默默当成 False 把设备叫醒
            async with http.post(f"{base}/v1/device/rest", json={}) as response:
                assert response.status == 400

        assert service.calls == [True, False]

    try:
        asyncio.run(scenario())
    finally:
        server.stop()
