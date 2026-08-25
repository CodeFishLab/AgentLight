from __future__ import annotations

import time

import agentlight.arbitration as arbitration_module
from agentlight.arbitration import StateArbitrator


PRIORITIES = {"attention": 600, "error": 500, "busy": 400, "done": 300, "ready": 200, "off": 0}


def test_priority_beats_recency() -> None:
    manager = StateArbitrator(PRIORITIES)
    manager.emit("codex", "one", "attention")
    manager.emit("claude", "two", "done")
    assert manager.snapshot()["effective"]["state"] == "attention"
    manager.end("codex", "one")
    assert manager.snapshot()["effective"]["state"] == "done"


def test_done_decays_to_ready(monkeypatch) -> None:
    now = 1000.0
    monkeypatch.setattr(arbitration_module, "time", lambda: now)
    manager = StateArbitrator(PRIORITIES, done_hold_seconds=10)
    manager.emit("codex", "one", "done", ttl_seconds=60)
    assert manager.snapshot()["effective"]["state"] == "done"
    now = 1011.0
    assert manager.snapshot()["effective"]["state"] == "ready"


def test_manual_override_expires(monkeypatch) -> None:
    now = 2000.0
    monkeypatch.setattr(arbitration_module, "time", lambda: now)
    manager = StateArbitrator(PRIORITIES)
    manager.emit("codex", "one", "busy")
    manager.set_manual("off", 5)
    assert manager.snapshot()["effective"]["state"] == "off"
    now = 2006.0
    manager.expire()
    assert manager.snapshot()["effective"]["state"] == "busy"


def test_session_ttl_removes_stale_state(monkeypatch) -> None:
    now = 3000.0
    monkeypatch.setattr(arbitration_module, "time", lambda: now)
    manager = StateArbitrator(PRIORITIES)
    manager.emit("custom", "task", "error", ttl_seconds=5)
    now = 3006.0
    manager.expire()
    snapshot = manager.snapshot()
    assert snapshot["sessions"] == []
    assert snapshot["effective"]["state"] == "off"
    assert snapshot["last_off"]["code"] == "session_timeout"
    assert snapshot["last_off"]["source"] == "custom"


def test_manual_state_can_continue_until_released() -> None:
    manager = StateArbitrator(PRIORITIES)
    manager.set_manual("busy", None)
    assert manager.snapshot()["manual"]["until"] is None
    manager.set_manual(None)
    snapshot = manager.snapshot()
    assert snapshot["effective"]["state"] == "off"
    assert snapshot["last_off"]["code"] == "manual_released"


def _arbitrator(**kwargs):
    from agentlight.arbitration import StateArbitrator
    from agentlight.config import DEFAULT_CONFIG

    defaults = {"default_ttl_seconds": 1800, "done_hold_seconds": 10, "stale_after_seconds": 300}
    defaults.update(kwargs)
    return StateArbitrator(DEFAULT_CONFIG["priorities"], **defaults)


def test_ordinary_work_cannot_clear_a_pending_permission_request() -> None:
    """Hook 会并发执行，一条 PostToolUse 就能把授权请求顶掉，你一走开就再也看不到。"""
    arbitrator = _arbitrator()
    arbitrator.emit("codex", "s1", "permission")

    arbitrator.emit("codex", "s1", "busy")
    assert arbitrator.snapshot()["effective"]["state"] == "permission"

    arbitrator.emit("codex", "s1", "subagent")
    assert arbitrator.snapshot()["effective"]["state"] == "permission"


def test_protection_covers_attention_and_error_too() -> None:
    for protected in ("attention", "error"):
        arbitrator = _arbitrator()
        arbitrator.emit("codex", "s1", protected)
        arbitrator.emit("codex", "s1", "busy")
        assert arbitrator.snapshot()["effective"]["state"] == protected, protected


def test_a_real_reply_or_finish_still_clears_the_alert() -> None:
    """保护不能变成卡死：用户回应、任务结束、会话结束都要能解除。"""
    for resolving in ("thinking", "done", "ready"):
        arbitrator = _arbitrator()
        arbitrator.emit("codex", "s1", "permission")
        arbitrator.emit("codex", "s1", resolving)
        assert arbitrator.snapshot()["effective"]["state"] == resolving, resolving

    arbitrator = _arbitrator()
    arbitrator.emit("codex", "s1", "permission")
    arbitrator.emit("codex", "s1", "off")
    assert arbitrator.snapshot()["effective"]["state"] == "off"


def test_protection_freezes_the_record_instead_of_renewing_it() -> None:
    """被挡下的事件不能刷新 updated_at，否则「等待时长」会显示成刚刚才发生；
    也不能延长 expires_at，否则告警永远等不到自己过期。"""
    arbitrator = _arbitrator()
    arbitrator.emit("codex", "s1", "permission")
    first = arbitrator.snapshot()["sessions"][0]
    time.sleep(0.05)

    arbitrator.emit("codex", "s1", "busy")

    after = arbitrator.snapshot()["sessions"][0]
    assert after["state"] == "permission"
    assert after["updated_at"] == first["updated_at"]
    assert after["expires_at"] == first["expires_at"]


def test_protection_lets_go_once_work_has_clearly_resumed(monkeypatch) -> None:
    """挡的是毫秒级的并发 Hook。隔了一分钟还在干活，说明你早就点了同意，
    这时候还举着「请求授权」，灯和关注队列就会一直卡在那儿。"""
    monkeypatch.setattr(arbitration_module, "PROTECTION_WINDOW_SECONDS", 0.05)
    arbitrator = _arbitrator()
    arbitrator.emit("codex", "s1", "permission")

    arbitrator.emit("codex", "s1", "busy")
    assert arbitrator.snapshot()["effective"]["state"] == "permission"

    time.sleep(0.06)
    arbitrator.emit("codex", "s1", "busy")
    assert arbitrator.snapshot()["effective"]["state"] == "busy"


def test_a_working_session_goes_stale_when_events_stop() -> None:
    arbitrator = _arbitrator(stale_after_seconds=1)
    arbitrator.emit("codex", "s1", "busy")
    assert arbitrator.snapshot()["effective"]["state"] == "busy"

    time.sleep(1.05)

    assert arbitrator.snapshot()["effective"]["state"] == "stale"


def test_waiting_and_finished_states_never_go_stale() -> None:
    """「等你处理」挂着很久本来就是准确的，降级成存疑反而是在骗人。"""
    for state in ("attention", "permission", "error", "ready"):
        arbitrator = _arbitrator(stale_after_seconds=1)
        arbitrator.emit("codex", "s1", state)
        time.sleep(1.05)
        assert arbitrator.snapshot()["effective"]["state"] == state, state


def test_a_genuinely_ready_session_outranks_a_stale_one() -> None:
    arbitrator = _arbitrator(stale_after_seconds=1)
    arbitrator.emit("codex", "old", "busy")
    time.sleep(1.05)
    arbitrator.emit("claude", "fresh", "ready")

    assert arbitrator.snapshot()["effective"]["state"] == "ready"


def test_stale_detection_can_be_switched_off() -> None:
    arbitrator = _arbitrator(stale_after_seconds=0)
    arbitrator.emit("codex", "s1", "busy")
    time.sleep(0.1)

    assert arbitrator.snapshot()["effective"]["state"] == "busy"
