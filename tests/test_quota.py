from __future__ import annotations

import json
from pathlib import Path

from agentlight.quota import describe_window, latest_codex_quota, latest_codex_windows, remaining_percent


def test_remaining_percent() -> None:
    assert remaining_percent({"used_percent": 12.6}) == 87
    assert remaining_percent({"used_percent": 150}) == 0
    assert remaining_percent(None) == 255


def test_latest_codex_quota_reads_newest_valid_snapshot(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions" / "2026" / "08"
    sessions.mkdir(parents=True)
    path = sessions / "events.jsonl"
    records = [
        {"payload": {"rate_limits": {"primary": {"used_percent": 10}, "secondary": {"used_percent": 20}}}},
        {"unrelated": True},
        {"rate_limits": {"primary": {"used_percent": 42}, "secondary": {"used_percent": 75}}},
    ]
    path.write_text("\n".join(json.dumps(item) for item in records) + "\n", encoding="utf-8")

    assert latest_codex_quota(tmp_path) == (58, 25)


def write_snapshot(tmp_path: Path, rate_limits: dict) -> None:
    sessions = tmp_path / "sessions" / "2026" / "08"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "events.jsonl").write_text(
        json.dumps({"payload": {"rate_limits": rate_limits}}) + "\n", encoding="utf-8"
    )


def test_window_names_come_from_length_not_position(tmp_path: Path) -> None:
    """实测账号只上报一个 window_minutes=10080 的 primary，按位置猜会把周配额标成 5 小时。"""
    write_snapshot(tmp_path, {
        "primary": {"used_percent": 100.0, "window_minutes": 10080, "resets_at": 1787199392},
        "secondary": None,
    })

    windows = latest_codex_windows(tmp_path)

    assert len(windows) == 1
    assert windows[0]["label"] == "Weekly"
    assert windows[0]["remaining_percent"] == 0
    assert windows[0]["resets_at"] == 1787199392
    # 设备两格：短窗口缺失，长窗口 0%
    assert latest_codex_quota(tmp_path) == (255, 0)


def test_short_window_is_recognised_by_length(tmp_path: Path) -> None:
    write_snapshot(tmp_path, {
        "primary": {"used_percent": 40, "window_minutes": 300},
        "secondary": {"used_percent": 10, "window_minutes": 10080},
    })

    assert [w["label"] for w in latest_codex_windows(tmp_path)] == ["5h", "Weekly"]
    assert latest_codex_quota(tmp_path) == (60, 90)


def test_describe_window_covers_common_lengths() -> None:
    assert describe_window(300) == "5h"
    assert describe_window(10080) == "Weekly"
    assert describe_window(1440) == "1d"
    assert describe_window(90) == "90m"
    assert describe_window(None) == "配额"


