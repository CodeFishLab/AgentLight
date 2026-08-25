"""AgentLight 的可选 Claude Code statusLine。

本模块只解析 Claude Code 传给 statusLine 的 stdin，并拼出一行用户可见文本。
AgentLight 配额卡的数据由原生 OAuth Provider 获取，不再读取这里的输出。stdin
一律当不可信输入：限长、限条数，字段畸形就丢掉而不是抛异常。
"""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime
from typing import Any

from .models import HIDDEN_QUOTA_WINDOW_KEYS

# stdin 的两道闸门。Claude Code 写完就关闭管道，正常情况下 read() 立刻返回；
# 这些上限是防它异常时把状态栏卡住。
STDIN_MAX_BYTES = 1024 * 1024
STDIN_TIMEOUT_SECONDS = 1.5

# Claude Code 内部的窗口键（取自其二进制里的 kWS 列表）。
# limits / extra_usage / cinder_cove 不是百分比窗口，不在这里登记；
# 未登记的键会走兜底命名，不会被静默丢掉。
CLAUDE_WINDOWS: dict[str, tuple[str, str, int]] = {
    # key: (完整名, 状态栏短名, window_minutes)
    "five_hour": ("5h", "5h", 300),
    "seven_day": ("Weekly", "7d", 10080),
    "seven_day_opus": ("Weekly Opus", "7d Opus", 10080),
    "seven_day_sonnet": ("Weekly Sonnet", "7d Sonnet", 10080),
    "seven_day_oauth_apps": ("Weekly OAuth", "7d OAuth", 10080),
}

MODEL_SCOPED_MAX_WINDOWS = 8
LABEL_MAX_LENGTH = 32


def _clamp_percent(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return max(0.0, min(100.0, number))


def _parse_reset(value: Any) -> int | None:
    """five_hour.resets_at 是 unix 秒，model_scoped[].resets_at 是 ISO 串。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        moment = int(value)
        return moment if moment > 0 else None
    if isinstance(value, str) and value:
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    return None


def _sanitize_label(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    # 这串会进状态栏、进配置页、进手机端，控制字符一律剔掉
    cleaned = "".join(char for char in value if char.isprintable())
    return cleaned.strip()[:LABEL_MAX_LENGTH]


def _window(key: str, label: str, short: str, minutes: int | None, used: float, resets_at: Any) -> dict[str, Any]:
    """输出结构与 quota.py 的 Codex 窗口保持一致，前端才能用同一套渲染。"""
    return {
        "key": key,
        "label": label,
        "short": short,
        "window_minutes": minutes,
        "used_percent": round(used, 1),
        "remaining_percent": max(0, min(100, round(100.0 - used))),
        "resets_at": _parse_reset(resets_at),
    }


def _iter_scoped(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value[:MODEL_SCOPED_MAX_WINDOWS] if isinstance(item, dict)]


def extract_windows(payload: Any) -> list[dict[str, Any]]:
    """从 statusLine stdin 里抽出所有可用的额度窗口。

    不硬编码只认 five_hour/seven_day —— 那份 schema 会变（seven_day_opus、
    model_scoped 都是后来加的），遍历比枚举稳。
    """
    if not isinstance(payload, dict):
        return []
    rate_limits = payload.get("rate_limits")
    if not isinstance(rate_limits, dict):
        return []

    windows: list[dict[str, Any]] = []
    for key, entry in rate_limits.items():
        if key == "model_scoped" or key in HIDDEN_QUOTA_WINDOW_KEYS or not isinstance(entry, dict):
            continue
        used = _clamp_percent(entry.get("used_percentage"))
        if used is None:
            continue
        known = CLAUDE_WINDOWS.get(key)
        if known:
            label, short, minutes = known
        else:
            label = short = key.replace("_", " ")[:LABEL_MAX_LENGTH]
            minutes = None
        windows.append(_window(key, label, short, minutes, used, entry.get("resets_at")))

    for index, entry in enumerate(_iter_scoped(rate_limits.get("model_scoped"))):
        label = _sanitize_label(entry.get("display_name"))
        if not label:
            continue
        used = _clamp_percent(entry.get("utilization"))
        if used is None:
            continue
        windows.append(_window(f"model_scoped:{index}", label, label, 10080, used, entry.get("resets_at")))

    return windows


def render_line(windows: list[dict[str, Any]], state_label: str = "", state_symbol: str = "●") -> str:
    """状态栏那一行。灯的状态在前，额度在后，两边都可能缺。"""
    parts: list[str] = []
    if state_label:
        parts.append(f"{state_symbol} {state_label}")
    quota = " · ".join(f"{window['short']} {window['remaining_percent']}%" for window in windows)
    if quota:
        parts.append(f"剩余 {quota}")
    return " · ".join(parts)


def read_stdin_json() -> dict[str, Any] | None:
    """带超时和长度上限地读一份 JSON。

    显式按 UTF-8 解码：Windows 上 sys.stdin.encoding 是 GBK，用文本模式读会把
    非 ASCII 内容解错。
    """
    if sys.stdin is None:
        return None
    try:
        if sys.stdin.isatty():
            return None
    except (OSError, ValueError):
        return None

    box: list[bytes] = []

    def _read() -> None:
        try:
            box.append(sys.stdin.buffer.read(STDIN_MAX_BYTES))
        except (OSError, ValueError):
            pass

    worker = threading.Thread(target=_read, daemon=True)
    worker.start()
    worker.join(STDIN_TIMEOUT_SECONDS)
    if not box:
        return None
    try:
        value = json.loads(box[0].decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None
