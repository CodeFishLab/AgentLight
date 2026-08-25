"""状态时间线：后端才是权威数据源。"""

from __future__ import annotations

import threading
import time
from collections import deque

from agentlight.service import AgentLightService, TIMELINE_MAX_ENTRIES


def timeline_service() -> AgentLightService:
    service = AgentLightService.__new__(AgentLightService)
    service._timeline = deque(maxlen=TIMELINE_MAX_ENTRIES)
    service._timeline_lock = threading.RLock()
    return service


def test_the_window_always_carries_a_baseline_before_it() -> None:
    """「灯已经绿了两小时」这种情况，30 分钟窗口里一条记录都没有。
    不带上窗口之前最后那条，前端就只能画一片空白或者瞎猜。"""
    service = timeline_service()
    now = int(time.time())
    service._timeline.append((now - 7200, "ready"))     # 两小时前变绿，之后再没动过

    result = service.timeline(window_seconds=1800)

    assert [item["state"] for item in result["entries"]] == ["ready"]
    assert result["entries"][0]["at"] == now - 7200


def test_entries_inside_the_window_are_kept_in_order() -> None:
    service = timeline_service()
    now = int(time.time())
    for offset, state in [(-3600, "off"), (-1200, "busy"), (-600, "attention"), (-60, "ready")]:
        service._timeline.append((now + offset, state))

    entries = service.timeline(window_seconds=1800)["entries"]

    # 窗口外那条 off 作为基线保留，窗口内三条按时间排好
    assert [item["state"] for item in entries] == ["off", "busy", "attention", "ready"]


def test_the_buffer_is_bounded() -> None:
    """实测最忙的半小时只有 42 次变化，300 条足够；但不能无上限地涨。"""
    service = timeline_service()
    now = int(time.time())
    for i in range(TIMELINE_MAX_ENTRIES + 200):
        service._timeline.append((now - i, "busy"))

    assert len(service._timeline) == TIMELINE_MAX_ENTRIES
