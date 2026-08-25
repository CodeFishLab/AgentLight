from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator


def iter_codex_session_files(codex_home: Path) -> Iterator[Path]:
    sessions = codex_home / "sessions"
    if sessions.exists():
        yield from sessions.rglob("*.jsonl")


def read_lines_reverse(path: Path, block_size: int = 65_536) -> Iterator[str]:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        buffer = b""
        while position > 0:
            read_size = min(block_size, position)
            position -= read_size
            handle.seek(position)
            buffer = handle.read(read_size) + buffer
            lines = buffer.split(b"\n")
            buffer = lines[0]
            for line in reversed(lines[1:]):
                if line:
                    yield line.decode("utf-8", errors="replace")
        if buffer:
            yield buffer.decode("utf-8", errors="replace")


UNKNOWN = 255
# 设备屏幕只有两格，分别对应短窗口和长窗口
SHORT_WINDOW_MAX_MINUTES = 360


def remaining_percent(window: Any) -> int:
    if not isinstance(window, dict):
        return UNKNOWN
    used = window.get("used_percent")
    if not isinstance(used, (int, float)):
        return UNKNOWN
    return max(0, min(100, round(100.0 - float(used))))


def describe_window(minutes: Any) -> str:
    """按窗口长度给出名字。

    不能靠 primary/secondary 的位置去猜：实测账号只上报一个 window_minutes=10080
    的 primary，硬当成「5 小时」就会把周配额标错。
    """
    try:
        value = int(minutes)
    except (TypeError, ValueError):
        return "配额"
    if value <= 0:
        return "配额"
    if value == 10080:
        return "Weekly"
    if value % 1440 == 0:
        return f"{value // 1440}d"
    if value % 60 == 0:
        return f"{value // 60}h"
    return f"{value}m"


def _window_payload(key: str, window: Any) -> dict[str, Any] | None:
    if not isinstance(window, dict):
        return None
    remaining = remaining_percent(window)
    if remaining == UNKNOWN:
        return None
    minutes = window.get("window_minutes")
    label = describe_window(minutes)
    if label == "配额":
        # 老快照没有 window_minutes，退回按位置给名字
        label = "5h" if key == "primary" else "Weekly"
    return {
        "key": key,
        "label": label,
        "window_minutes": minutes if isinstance(minutes, int) else None,
        "used_percent": round(float(window.get("used_percent", 0)), 1),
        "remaining_percent": remaining,
        "resets_at": window.get("resets_at"),
    }


def latest_codex_rate_limits(codex_home: Path | None = None) -> dict[str, Any]:
    root = codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    files = sorted(iter_codex_session_files(root), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in files:
        for line in read_lines_reverse(path):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = record.get("payload")
            rate_limits = payload.get("rate_limits") if isinstance(payload, dict) else record.get("rate_limits")
            if isinstance(rate_limits, dict):
                return rate_limits
    raise RuntimeError(f"未在 {root} 下找到 Codex 配额快照")


def latest_codex_windows(codex_home: Path | None = None) -> list[dict[str, Any]]:
    """返回所有可用的配额窗口，名字由 window_minutes 推出。"""
    rate_limits = latest_codex_rate_limits(codex_home)
    windows = []
    for key in ("primary", "secondary"):
        entry = _window_payload(key, rate_limits.get(key))
        if entry:
            windows.append(entry)
    if not windows:
        raise RuntimeError("Codex 配额快照里没有可用的窗口")
    return windows


def latest_codex_quota(codex_home: Path | None = None) -> tuple[int, int]:
    """保持给设备屏幕用的两格契约：(短窗口剩余, 长窗口剩余)，缺失的一格为 255。"""
    short = long = UNKNOWN
    for window in latest_codex_windows(codex_home):
        minutes = window["window_minutes"]
        # 有窗口长度就按长度分，没有才退回按 primary/secondary 的位置分
        is_short = minutes <= SHORT_WINDOW_MAX_MINUTES if minutes is not None else window["key"] == "primary"
        if is_short:
            short = window["remaining_percent"]
        else:
            long = window["remaining_percent"]
    return short, long
