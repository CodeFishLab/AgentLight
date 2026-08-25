from __future__ import annotations

import json

from agentlight.config import CURRENT_SCHEMA_VERSION, ConfigManager


def test_config_creates_token_and_merges_defaults(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"muted": True, "profiles": {"busy": {"brightness": 7}}}), encoding="utf-8")
    manager = ConfigManager(path)
    data = manager.snapshot()
    assert data["muted"] is True
    assert data["profiles"]["busy"]["brightness"] == 7
    assert data["profiles"]["busy"]["mode"] == "breath"
    assert len(data["api"]["token"]) >= 32
    assert data["quota_refresh_interval_seconds"] == 30


def test_config_never_accepts_a_non_loopback_api_host(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"api": {"host": "0.0.0.0"}}), encoding="utf-8")

    manager = ConfigManager(path)

    assert manager.get("api")["host"] == "127.0.0.1"
    manager.update({"api": {"host": "192.168.1.10"}})
    assert manager.get("api")["host"] == "127.0.0.1"


def test_v1_config_migrates_customised_mode_into_effect(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "schemaVersion": 1,
        "muted": True,
        "profiles": {"ready": {"colors": ["000000", "000000", "00ff00"], "mode": "breath"}},
    }), encoding="utf-8")

    data = ConfigManager(path).snapshot()

    assert data["schemaVersion"] == CURRENT_SCHEMA_VERSION
    # 迁移时以用户配置的 mode 为准，不被默认 effect 覆盖。
    assert data["profiles"]["ready"]["effect"] == "breath"
    assert data["profiles"]["ready"]["effect_params"] == {}
    assert data["muted"] is True
    assert data["device"]["frame_rate"] == 20


def test_migration_is_written_back_and_stable_on_reload(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"schemaVersion": 1, "profiles": {"busy": {"mode": "blink"}}}), encoding="utf-8")

    first = ConfigManager(path).snapshot()
    second = ConfigManager(path).snapshot()

    assert json.loads(path.read_text(encoding="utf-8"))["schemaVersion"] == CURRENT_SCHEMA_VERSION
    assert first == second
    assert second["profiles"]["busy"]["effect"] == "blink"


def test_config_update_is_persistent(tmp_path) -> None:
    path = tmp_path / "config.json"
    manager = ConfigManager(path)
    manager.update({"paused": True, "device": {"sleep_timeout": 99}})
    reloaded = ConfigManager(path).snapshot()
    assert reloaded["paused"] is True
    assert reloaded["device"]["sleep_timeout"] == 99
    assert reloaded["device"]["max_brightness"] == 30


def test_v6_removes_obsolete_external_claude_snapshot_setting(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"schemaVersion": 5, "claude_quota_snapshot": "old.json"}), encoding="utf-8")

    data = ConfigManager(path).snapshot()

    assert data["schemaVersion"] == CURRENT_SCHEMA_VERSION
    assert "claude_quota_snapshot" not in data



def test_v2_config_gains_the_new_states_without_touching_existing_ones(tmp_path) -> None:
    """细分状态是新增的，老用户已经调好的档案一个都不能动。"""
    path = tmp_path / "config.json"
    existing = {
        "ready": {"colors": ["aa0000", "ff5500", "00ff00"], "mode": "static", "effect": "static",
                  "effect_params": {}, "brightness": 3, "period_ms": 1000, "sound": "stop"},
        "busy": {"colors": ["000000", "ff5500", "000000"], "mode": "breath", "effect": "breath",
                 "effect_params": {}, "brightness": 3, "period_ms": 1500, "sound": "beep"},
    }
    path.write_text(json.dumps({"schemaVersion": 2, "profiles": existing}), encoding="utf-8")

    data = ConfigManager(path).snapshot()

    assert data["schemaVersion"] == CURRENT_SCHEMA_VERSION
    for state, profile in existing.items():
        assert data["profiles"][state] == profile, state
    for added in ("thinking", "subagent", "permission"):
        assert added in data["profiles"], added
        assert added in data["priorities"], added


def test_every_state_has_a_profile_and_a_priority() -> None:
    from agentlight.config import DEFAULT_CONFIG
    from agentlight.models import VALID_STATES

    assert set(DEFAULT_CONFIG["profiles"]) == set(VALID_STATES)
    assert set(DEFAULT_CONFIG["priorities"]) == set(VALID_STATES)
    # 优先级必须两两不同，否则仲裁在同分时结果不确定
    values = list(DEFAULT_CONFIG["priorities"].values())
    assert len(set(values)) == len(values)
    # 请求授权挡住了 agent，应当排在等待输入之上
    assert DEFAULT_CONFIG["priorities"]["permission"] > DEFAULT_CONFIG["priorities"]["attention"]
