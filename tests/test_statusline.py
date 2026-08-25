"""可选 Claude Code statusLine 的纯解析与文本渲染测试。"""

from __future__ import annotations

import pytest

from agentlight import statusline


def payload(**rate_limits):
    return {"model": {"display_name": "Opus"}, "rate_limits": rate_limits}


def test_extracts_the_documented_windows() -> None:
    windows = statusline.extract_windows(payload(
        five_hour={"used_percentage": 42.0, "resets_at": 1787811955},
        seven_day={"used_percentage": 18.5, "resets_at": 1788000000},
    ))

    assert [w["key"] for w in windows] == ["five_hour", "seven_day"]
    assert windows[0]["label"] == "5h"
    assert windows[0]["short"] == "5h"
    assert windows[0]["window_minutes"] == 300
    assert windows[0]["used_percent"] == 42.0
    assert windows[0]["remaining_percent"] == 58
    assert windows[0]["resets_at"] == 1787811955
    assert windows[1]["remaining_percent"] == 82


def test_model_scoped_windows_use_iso_resets_and_their_own_label() -> None:
    windows = statusline.extract_windows(payload(model_scoped=[
        {"display_name": "Fable", "utilization": 12.0, "resets_at": "2026-08-23T09:59:00Z"},
    ]))

    assert len(windows) == 1
    assert windows[0]["label"] == "Fable"
    assert windows[0]["remaining_percent"] == 88
    assert windows[0]["resets_at"] == int(
        __import__("datetime").datetime.fromisoformat("2026-08-23T09:59:00+00:00").timestamp()
    )


def test_unknown_window_keys_survive_instead_of_being_dropped() -> None:
    windows = statusline.extract_windows(payload(
        seven_day_opus={"used_percentage": 5.0},
        some_future_window={"used_percentage": 7.0},
    ))

    keys = {w["key"]: w for w in windows}
    assert keys["seven_day_opus"]["label"] == "Weekly Opus"
    assert keys["some_future_window"]["label"] == "some future window"
    assert keys["some_future_window"]["window_minutes"] is None


@pytest.mark.parametrize("bad", [
    {"used_percentage": None},
    {"used_percentage": "42"},
    {"used_percentage": True},
    {"used_percentage": float("nan")},
    {},
    "not-a-dict",
])
def test_malformed_windows_are_dropped_not_fatal(bad) -> None:
    windows = statusline.extract_windows(payload(five_hour=bad, seven_day={"used_percentage": 10.0}))
    assert [w["key"] for w in windows] == ["seven_day"]


def test_percentages_are_clamped_to_0_100() -> None:
    windows = statusline.extract_windows(payload(
        five_hour={"used_percentage": 130.0},
        seven_day={"used_percentage": -5.0},
    ))
    assert windows[0]["remaining_percent"] == 0
    assert windows[1]["remaining_percent"] == 100


def test_scoped_entries_are_bounded_and_sanitized() -> None:
    entries = [{"display_name": f"M{i}", "utilization": 1.0} for i in range(20)]
    entries.append({"display_name": "x\x07y" + "z" * 100, "utilization": 1.0})
    windows = statusline.extract_windows(payload(model_scoped=entries))

    assert len(windows) <= statusline.MODEL_SCOPED_MAX_WINDOWS
    for window in windows:
        assert len(window["label"]) <= statusline.LABEL_MAX_LENGTH
        assert "\x07" not in window["label"]


@pytest.mark.parametrize("bad", [None, {}, {"rate_limits": None}, {"rate_limits": []}, "x", 42])
def test_missing_rate_limits_yields_nothing(bad) -> None:
    assert statusline.extract_windows(bad) == []


def test_status_line_shows_state_then_quota() -> None:
    windows = statusline.extract_windows(payload(
        five_hour={"used_percentage": 42.0},
        seven_day={"used_percentage": 18.0},
    ))
    assert statusline.render_line(windows, "工作中") == "● 工作中 · 剩余 5h 58% · 7d 82%"


def test_status_line_degrades_when_either_half_is_missing() -> None:
    windows = statusline.extract_windows(payload(five_hour={"used_percentage": 42.0}))
    assert statusline.render_line(windows, "") == "剩余 5h 58%"
    assert statusline.render_line([], "工作中") == "● 工作中"
    assert statusline.render_line([], "") == ""


def test_the_undocumented_bucket_never_reaches_the_status_bar() -> None:
    """nimbus_quill 没有重置时间也没有可操作额度，画出来就是个假的「还剩 100%」。
    原生 OAuth 那条链路早就过滤了，状态栏是第四条链路，同样不能漏。"""
    windows = statusline.extract_windows({
        "rate_limits": {
            "five_hour": {"used_percentage": 40, "resets_at": 1700000000},
            "nimbus_quill": {"used_percentage": 0},
        }
    })

    assert [window["key"] for window in windows] == ["five_hour"]
    assert "nimbus" not in statusline.render_line(windows)
