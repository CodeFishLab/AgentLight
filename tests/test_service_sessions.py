from __future__ import annotations

import json
import time

from agentlight.config import ConfigManager
from agentlight.service import AgentLightService


def test_active_sessions_survive_a_service_restart(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTLIGHT_DATA_DIR", str(tmp_path / "app-data"))
    config_path = tmp_path / "config.json"
    first = AgentLightService(ConfigManager(config_path))
    try:
        first.emit("codex", "current-task", "busy", ttl_seconds=60)
    finally:
        first.shutdown()

    restored = AgentLightService(ConfigManager(config_path))
    try:
        snapshot = restored.snapshot(include_integrations=False)["state"]
        assert snapshot["effective"]["state"] == "busy"
        assert snapshot["sessions"][0]["session_id"] == "current-task"
    finally:
        restored.shutdown()


def test_expired_and_corrupt_saved_sessions_are_ignored(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTLIGHT_DATA_DIR", str(tmp_path / "app-data"))
    config_path = tmp_path / "config.json"
    ConfigManager(config_path)
    state_path = tmp_path / "state" / "sessions.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({
        "version": 1,
        "sessions": [
            {
                "source": "codex",
                "session_id": "expired",
                "state": "busy",
                "updated_at": time.time() - 120,
                "expires_at": time.time() - 60,
            },
            {"source": "codex", "session_id": "invalid", "state": "not-a-state"},
        ],
    }), encoding="utf-8")

    service = AgentLightService(ConfigManager(config_path))
    try:
        snapshot = service.snapshot(include_integrations=False)["state"]
        assert snapshot["sessions"] == []
        assert snapshot["effective"]["state"] == "off"
    finally:
        service.shutdown()
