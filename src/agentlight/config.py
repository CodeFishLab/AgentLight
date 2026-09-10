from __future__ import annotations

import copy
import json
import secrets
import threading
from pathlib import Path
from typing import Any

from .paths import config_path


CURRENT_SCHEMA_VERSION = 7
LOOPBACK_HOST = "127.0.0.1"


def _profile(colors: list[str], effect: str, brightness: int, period_ms: int, sound: str) -> dict[str, Any]:
    # mode 保留给固件 0x10；软件效果期间它固定为 static，由 StateProfile 负责收敛
    return {
        "colors": colors,
        "mode": effect if effect in ("static", "blink", "breath") else "static",
        "effect": effect,
        "effect_params": {},
        "brightness": brightness,
        "period_ms": period_ms,
        "sound": sound,
    }


DEFAULT_CONFIG: dict[str, Any] = {
    "schemaVersion": CURRENT_SCHEMA_VERSION,
    "profiles": {
        "ready": _profile(["000000", "000000", "00ff00"], "static", 18, 1000, "stop"),
        # 思考中与执行中都用第二颗橙灯，靠呼吸快慢区分：想得快、做得稳
        "thinking": _profile(["000000", "ff8000", "000000"], "breath", 20, 600, "stop"),
        "busy": _profile(["000000", "ff8000", "000000"], "breath", 20, 1200, "stop"),
        "subagent": _profile(["000000", "0080ff", "000000"], "breath", 18, 1200, "stop"),
        "attention": _profile(["ff0000", "000000", "000000"], "blink", 25, 550, "custom:1400:140,0:120,1400:140"),
        # 授权挡住了 agent，闪得更急促，提示音也更催
        "permission": _profile(["ff0000", "ffaa00", "000000"], "blink", 25, 350, "custom:1600:120,0:90,1600:120,0:90,1600:120"),
        "error": _profile(["ff0000", "000000", "000000"], "static", 25, 1000, "failure"),
        "done": _profile(["000000", "000000", "00ff00"], "static", 22, 1000, "success"),
        # 状态存疑：柔和的黄灯慢呼吸，提示「这个状态可能已经不准了」，不催人
        "stale": _profile(["000000", "ffd000", "000000"], "breath", 12, 2500, "stop"),
        "off": _profile(["000000", "000000", "000000"], "static", 0, 1000, "stop"),
    },
    "priorities": {
        "permission": 700,
        "attention": 600,
        "error": 500,
        "thinking": 420,
        "busy": 400,
        "subagent": 380,
        "done": 300,
        "ready": 200,
        # 存疑的状态排在就绪之下：真的就绪的会话比一个说不准的更可信
        "stale": 150,
        "off": 0,
    },
    "default_ttl_seconds": 1800,
    "done_hold_seconds": 10,
    "force_reapply_seconds": 240,
    # 手动状态不再无限期占用灯光，到点自动交还给自动联动
    "manual_timeout_seconds": 300,
    # 「正在干活」的会话超过这个时长没有新事件，就降级成状态存疑
    "stale_after_seconds": 300,
    # AgentLight 读取 Codex 快照并刷新 Claude OAuth 配额的共用间隔；0 关闭周期刷新。
    # 正数对 Codex 按原值生效；Claude 出于接口保护至少间隔 60 秒。
    "quota_refresh_interval_seconds": 30,
    # 夜间模式：按比例压低所有状态的亮度，不改各状态自己的档案
    "night_mode": {"enabled": False, "scale": 35},
    "muted": False,
    "paused": False,
    # 用户主动关闭了与设备的连接，让它休息。跨重启保留，否则一重启就又被叫醒
    "device_resting": False,
    # 定时休息：到点自动关闭/恢复设备连接。start 晚于 end 表示跨过午夜（夜间场景
    # 基本都是这样）。只在跨越边界的那一刻动手，所以窗口内你手动开回来它不会一直抢。
    "device_rest_schedule": {"enabled": False, "start": "23:00", "end": "07:00"},
    # 蜂鸣器静音时段。和状态灯关闭时段同一套引擎，只是作用在 muted 上。
    "mute_schedule": {"enabled": False, "start": "22:00", "end": "08:00"},
    "start_with_windows": True,
    "api": {"host": "127.0.0.1", "port": 47651, "token": ""},
    "device": {
        "base_brightness": 0,
        "max_brightness": 30,
        "auto_sleep": True,
        "sleep_timeout": 300,
        "keep_5v": False,
        "screen_rotation": 0,
        "codex_quota_sync": True,
        "quota_sync_interval": 300,
        "sound_style": "standard",
        "frame_rate": 20,
    },
    "logging": {"max_bytes": 2_000_000, "backup_count": 3},
}


def _merge(default: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(default)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def _migrate(data: dict[str, Any]) -> dict[str, Any]:
    """将配置规范化到当前 schema。"""
    try:
        version = int(data.get("schemaVersion", 1) or 1)
    except (TypeError, ValueError):
        version = 1
    if version < 2:
        # schema v1 只包含 mode，因此迁移时以 mode 为准生成 effect。
        for profile in data.get("profiles", {}).values():
            if isinstance(profile, dict):
                profile["effect"] = str(profile.get("mode", "static"))
                profile.setdefault("effect_params", {})
    if version < 6:
        # 原生 OAuth Provider 不再读取第三方状态栏快照。
        data.pop("claude_quota_snapshot", None)
    # API 承载配置修改和本地凭据派生的数据。即使用户手工改过配置，也不允许
    # 监听局域网或公网地址。
    api = data.get("api")
    if not isinstance(api, dict):
        api = copy.deepcopy(DEFAULT_CONFIG["api"])
        data["api"] = api
    api["host"] = LOOPBACK_HOST
    if version < 7:
        # 老配置没有定时休息。默认关闭，不能因为升级就在半夜把设备静音掉。
        data.setdefault("device_rest_schedule", copy.deepcopy(DEFAULT_CONFIG["device_rest_schedule"]))
        data.setdefault("mute_schedule", copy.deepcopy(DEFAULT_CONFIG["mute_schedule"]))
    data["schemaVersion"] = CURRENT_SCHEMA_VERSION
    return data


class ConfigManager:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config_path()
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        incoming: dict[str, Any] = {}
        file_exists = self.path.exists()
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    incoming = loaded
            except (OSError, json.JSONDecodeError):
                incoming = {}
        data = _migrate(_merge(DEFAULT_CONFIG, incoming))
        if not data["api"].get("token"):
            data["api"]["token"] = secrets.token_urlsafe(32)
        if not file_exists or data != incoming:
            self._write(data)
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(self.path)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return copy.deepcopy(self._data.get(key, default))

    def update(self, values: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._data = _migrate(_merge(self._data, values))
            self._write(self._data)
            return copy.deepcopy(self._data)

    def set_profiles(self, profiles: dict[str, Any]) -> dict[str, Any]:
        """整体替换指定状态的灯效档案。

        不能走 update()：它对 dict 是深合并，切换灯效后 effect_params 里的旧键永远
        清不掉，配置会越积越脏。这里按状态整条替换，未涉及的状态保持不变。
        """
        with self._lock:
            data = copy.deepcopy(self._data)
            data["profiles"] = {**data.get("profiles", {}), **copy.deepcopy(profiles)}
            self._data = data
            self._write(self._data)
            return copy.deepcopy(self._data)

    def replace(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._data = _migrate(_merge(DEFAULT_CONFIG, data))
            if not self._data["api"].get("token"):
                self._data["api"]["token"] = secrets.token_urlsafe(32)
            self._write(self._data)
            return copy.deepcopy(self._data)
