"""时段调度：状态灯关闭与蜂鸣器静音共用同一套引擎。"""

from __future__ import annotations

import logging
import time

import pytest

from agentlight.service import AgentLightService, parse_clock, within_window


@pytest.mark.parametrize("text, expected", [
    ("00:00", 0), ("23:59", 1439), ("7:5", 425), (" 09:30 ", 570),
    ("24:00", None), ("12:60", None), ("abc", None), ("12", None), (None, None), (930, None),
])
def test_clock_parsing(text, expected) -> None:
    assert parse_clock(text) == expected


def test_a_window_that_crosses_midnight() -> None:
    """夜间时段基本都是 23:00-07:00 这种写法。用 start < end 那套判断在这里是错的。"""
    start, end = 23 * 60, 7 * 60
    assert within_window(23 * 60 + 30, start, end) is True
    assert within_window(2 * 60, start, end) is True
    assert within_window(6 * 60 + 59, start, end) is True
    assert within_window(7 * 60, start, end) is False
    assert within_window(12 * 60, start, end) is False


def test_a_same_day_window() -> None:
    start, end = 9 * 60, 18 * 60
    assert within_window(start, start, end) is True            # 起点算在内
    assert within_window(end, start, end) is False             # 终点不算
    assert within_window(20 * 60, start, end) is False


def test_identical_bounds_are_an_empty_window_not_a_whole_day() -> None:
    """起止相同若当成「全天」，就会变成一个永远退不出去的状态。"""
    assert within_window(0, 600, 600) is False
    assert within_window(600, 600, 600) is False


class FakeConfig:
    def __init__(self, values):
        self.values = dict(values)

    def get(self, key, default=None):
        return self.values.get(key, default)

    def update(self, values):
        self.values.update(values)
        return self.values


DAY = {"enabled": True, "start": "11:00", "end": "19:00"}
NIGHT = {"enabled": True, "start": "23:00", "end": "07:00"}


class Engine:
    """直接驱动通用引擎 _run_window，两个时段走的都是它。"""

    def __init__(self, schedule, active=False):
        self.svc = AgentLightService.__new__(AgentLightService)
        self.svc.config = FakeConfig({"device_rest_schedule": dict(schedule)})
        self.svc.logger = logging.getLogger("test")
        self.svc._flag = None
        self.active = active
        self.calls = []

    def tick(self, hour, minute=0):
        def apply(value):
            self.calls.append(value)
            self.active = value
        self.svc._run_window(
            "device_rest_schedule", "_flag",
            lambda: self.active, apply, "测试",
            time.struct_time((2026, 9, 9, hour, minute, 0, 2, 252, 0)),
        )


def test_it_stays_active_for_the_whole_window() -> None:
    """老版本只在边界动手，结果是「开关亮着、时间也在区间内、状态却没生效」——
    界面上完全看不出为什么。"""
    e = Engine(DAY)
    e.tick(15, 9)
    assert e.calls == [True]

    for minute in (10, 30, 59):
        e.tick(15, minute)
    assert e.calls == [True], "已经生效就别反复下发"

    # 被别的东西改掉了，下一拍要重新对齐 —— 这正是之前漏掉的那一条
    e.active = False
    e.tick(16, 0)
    assert e.calls == [True, True]


def test_leaving_the_window_restores() -> None:
    e = Engine(DAY)
    e.tick(15, 0)
    e.tick(19, 0)
    assert e.calls == [True, False]


def test_enabling_it_inside_the_window_takes_effect_at_once() -> None:
    """11:00-19:00，15:09 才打开，不该干等到明天。"""
    e = Engine(DAY)
    e.tick(15, 9)
    assert e.calls == [True]


def test_starting_up_outside_the_window_changes_nothing() -> None:
    """第一次判定就在窗口外时保持沉默 —— 那可能是用户自己手动设的状态。"""
    e = Engine(NIGHT, active=True)
    e.tick(12, 0)
    assert e.calls == []


def test_a_disabled_schedule_does_nothing_at_all() -> None:
    e = Engine({"enabled": False, "start": "23:00", "end": "07:00"}, active=True)
    for hour in (22, 23, 2, 8):
        e.tick(hour)
    assert e.calls == []


def test_a_malformed_time_is_ignored_rather_than_crashing_housekeeping() -> None:
    """这个函数跑在 housekeeping 里，抛异常会把整条后台线程带走。"""
    e = Engine({"enabled": True, "start": "25:00", "end": "07:00"})
    e.tick(23, 30)
    assert e.calls == []


def test_both_windows_are_wired_into_the_housekeeping_tick() -> None:
    """接线漏一个，就是「设置了但从不生效」。"""
    import inspect

    source = inspect.getsource(AgentLightService._apply_schedules)
    assert '"device_rest_schedule"' in source and '"_in_rest_window"' in source
    assert '"mute_schedule"' in source and '"_in_mute_window"' in source
    assert "set_device_resting(value, manual=False)" in source
    assert "set_muted(value, manual=False)" in source
    # housekeeping 必须真的调它
    assert "self._apply_schedules()" in inspect.getsource(AgentLightService._housekeeping)


# ---------------------------------------------------------------- 手动覆盖

class ManualHarness:
    def __init__(self, values):
        self.svc = AgentLightService.__new__(AgentLightService)
        self.svc.config = FakeConfig(values)
        self.svc.logger = logging.getLogger("test")
        self.svc._in_rest_window = True
        self.svc._in_mute_window = True
        self.svc._resting = True
        self.svc._muted = True
        self.svc._cancel_preview = lambda: None
        self.svc._notify = lambda: None
        self.svc._sync_runtime_sleep = lambda **kw: None
        self.commands = []

        class FakeHardware:
            def command(inner, name, *args):
                self.commands.append(name)
        self.svc.hardware = FakeHardware()


def test_waking_the_device_by_hand_inside_the_window_turns_the_schedule_off(monkeypatch) -> None:
    """否则下一拍就被调度按回去，或者留下一个界面上看不见的例外 ——
    「开关亮着却没生效」这种自相矛盾的画面就是这么来的。"""
    h = ManualHarness({"device_rest_schedule": dict(DAY)})
    monkeypatch.setattr(AgentLightService, "inside_window", lambda self, key, now=None: True)

    h.svc.set_device_resting(False)

    assert h.svc.config.values["device_rest_schedule"]["enabled"] is False
    assert h.svc._resting is False


def test_unmuting_by_hand_inside_the_window_turns_the_mute_schedule_off(monkeypatch) -> None:
    h = ManualHarness({"mute_schedule": dict(NIGHT)})
    monkeypatch.setattr(AgentLightService, "inside_window", lambda self, key, now=None: True)

    h.svc.set_muted(False)

    assert h.svc.config.values["mute_schedule"]["enabled"] is False
    assert h.svc._muted is False


def test_the_schedule_itself_does_not_turn_itself_off(monkeypatch) -> None:
    """离开时段是调度自己在收尾，不能顺手把开关也关了。"""
    h = ManualHarness({"device_rest_schedule": dict(DAY), "mute_schedule": dict(NIGHT)})
    monkeypatch.setattr(AgentLightService, "inside_window", lambda self, key, now=None: True)

    h.svc.set_device_resting(False, manual=False)
    h.svc.set_muted(False, manual=False)

    assert h.svc.config.values["device_rest_schedule"]["enabled"] is True
    assert h.svc.config.values["mute_schedule"]["enabled"] is True


def test_changing_things_outside_the_window_leaves_schedules_alone(monkeypatch) -> None:
    """白天手动动一下，不该把晚上的时段给关了。"""
    h = ManualHarness({"device_rest_schedule": dict(NIGHT), "mute_schedule": dict(NIGHT)})
    monkeypatch.setattr(AgentLightService, "inside_window", lambda self, key, now=None: False)

    h.svc.set_device_resting(False)
    h.svc.set_muted(False)

    assert h.svc.config.values["device_rest_schedule"]["enabled"] is True
    assert h.svc.config.values["mute_schedule"]["enabled"] is True
