from __future__ import annotations

import copy
import math
import threading
from time import time
from typing import Any, Callable

from .models import EffectiveState, SessionRecord, VALID_STATES


# 只有「agent 正在做事」这类断言会过期成存疑。等你处理、已完成、出错这些状态
# 挂着很久本来就是准确的，不该被降级。
STALE_CANDIDATES = frozenset({"thinking", "busy", "subagent"})

# 红黄告警不能被普通的干活事件冲掉。Hook 可能并发执行，一次 PostToolUse 或者
# 并行子任务的事件就能把「请求授权」顶掉，你一走开就再也看不到那次请求。
PROTECTED_STATES = frozenset({"attention", "permission", "error"})
ORDINARY_WORK_STATES = frozenset({"busy", "subagent"})

# 但保护必须有窗口。要挡的是「同一时刻并发的 Hook」——那是毫秒级的事。
# 隔了这么久还来的干活事件，只可能是你已经点了同意、活真的接着干了，
# 这时候还举着「请求授权」就是在说谎，灯和关注队列都会一直卡在那儿。
PROTECTION_WINDOW_SECONDS = 15.0


class StateArbitrator:
    def __init__(
        self,
        priorities: dict[str, int],
        default_ttl_seconds: int = 1800,
        done_hold_seconds: int = 10,
        stale_after_seconds: int = 300,
    ) -> None:
        self.priorities = dict(priorities)
        self.default_ttl_seconds = default_ttl_seconds
        self.done_hold_seconds = done_hold_seconds
        self.stale_after_seconds = stale_after_seconds
        self._sessions: dict[tuple[str, str], SessionRecord] = {}
        self._manual: tuple[str, float | None] | None = None
        self._lock = threading.RLock()
        self._listeners: list[Callable[[EffectiveState], None]] = []
        self._effective = EffectiveState("off")
        self._last_off: dict[str, Any] | None = None

    def subscribe(self, callback: Callable[[EffectiveState], None]) -> None:
        with self._lock:
            self._listeners.append(callback)

    def configure(
        self,
        priorities: dict[str, int],
        default_ttl_seconds: int,
        done_hold_seconds: int,
        stale_after_seconds: int | None = None,
    ) -> None:
        with self._lock:
            self.priorities = dict(priorities)
            self.default_ttl_seconds = max(5, int(default_ttl_seconds))
            self.done_hold_seconds = max(0, int(done_hold_seconds))
            if stale_after_seconds is not None:
                self.stale_after_seconds = max(0, int(stale_after_seconds))
        self._recompute()

    def emit(
        self,
        source: str,
        session_id: str,
        state: str,
        ttl_seconds: int | None = None,
        reason: str = "explicit_off",
    ) -> EffectiveState:
        if state not in VALID_STATES:
            raise ValueError(f"unsupported state: {state}")
        source = source.strip() or "unknown"
        session_id = session_id.strip() or "default"
        now = time()
        with self._lock:
            key = (source, session_id)
            if state == "off":
                self._sessions.pop(key, None)
            else:
                ttl = self.default_ttl_seconds if ttl_seconds is None else max(5, int(ttl_seconds))
                previous = self._sessions.get(key)
                if self._protects(previous, state, now):
                    # 保住告警，updated_at 保持原值，这样「等待时长」显示的仍然是
                    # 这次告警真实挂了多久。expires_at 也不动：一直续期的话告警
                    # 就永远不会自己过期，只能靠你手动移除。
                    self._sessions[key] = previous
                else:
                    self._sessions[key] = SessionRecord(source, session_id, state, now, now + ttl)
        return self._recompute(reason if state == "off" else "state_update")

    @staticmethod
    def _protects(previous: SessionRecord | None, state: str, now: float) -> bool:
        """这条普通的干活事件该不该被挡下来，好让告警继续挂着。"""
        if previous is None or previous.state not in PROTECTED_STATES:
            return False
        if state not in ORDINARY_WORK_STATES:
            return False
        return now - previous.updated_at <= PROTECTION_WINDOW_SECONDS

    def end(self, source: str, session_id: str) -> EffectiveState:
        with self._lock:
            self._sessions.pop((source, session_id), None)
        return self._recompute("session_end")

    def restore_sessions(self, records: Any) -> EffectiveState:
        """恢复仍在 TTL 内的会话；损坏、过期或不认识的记录直接忽略。"""
        now = time()
        restored: dict[tuple[str, str], SessionRecord] = {}
        if isinstance(records, list):
            for value in records[:1000]:
                if not isinstance(value, dict):
                    continue
                source = value.get("source")
                session_id = value.get("session_id")
                state = value.get("state")
                updated_at = value.get("updated_at")
                expires_at = value.get("expires_at")
                if not isinstance(source, str) or not source.strip() or len(source) > 512:
                    continue
                if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 512:
                    continue
                if not isinstance(state, str) or state not in VALID_STATES or state == "off":
                    continue
                if isinstance(updated_at, bool) or not isinstance(updated_at, (int, float)):
                    continue
                if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
                    continue
                updated_at = float(updated_at)
                expires_at = float(expires_at)
                if not math.isfinite(updated_at) or not math.isfinite(expires_at) or expires_at <= now:
                    continue
                record = SessionRecord(source.strip(), session_id.strip(), state, updated_at, expires_at)
                restored[(record.source, record.session_id)] = record
        with self._lock:
            self._sessions = restored
        return self._recompute("state_restore")

    def set_manual(self, state: str | None, duration_seconds: int | None = None) -> EffectiveState:
        if state is not None and state not in VALID_STATES:
            raise ValueError(f"unsupported state: {state}")
        with self._lock:
            if state is None:
                self._manual = None
            else:
                until = None if duration_seconds is None else time() + max(1, int(duration_seconds))
                self._manual = (state, until)
        return self._recompute("manual_released" if state is None else "manual_state")

    def expire(self) -> EffectiveState:
        now = time()
        expired_sessions = False
        expired_manual = False
        with self._lock:
            stale = [key for key, value in self._sessions.items() if value.expires_at and value.expires_at <= now]
            for key in stale:
                self._sessions.pop(key, None)
            expired_sessions = bool(stale)
            if self._manual and self._manual[1] is not None and self._manual[1] <= now:
                self._manual = None
                expired_manual = True
        reason = "manual_timeout" if expired_manual else "session_timeout" if expired_sessions else "state_refresh"
        return self._recompute(reason)

    def _candidate_state(self, record: SessionRecord, now: float) -> str:
        if record.state == "done" and now - record.updated_at > self.done_hold_seconds:
            return "ready"
        if (
            self.stale_after_seconds
            and record.state in STALE_CANDIDATES
            and now - record.updated_at > self.stale_after_seconds
        ):
            # 说了「我在干活」却这么久没动静，多半是卡住了或者 Hook 断了
            return "stale"
        return record.state

    def _calculate(self) -> EffectiveState:
        now = time()
        if self._manual:
            return EffectiveState(self._manual[0], "manual", "manual", now, True)
        if not self._sessions:
            return EffectiveState("off", "system", "", now, False)
        winner = max(
            self._sessions.values(),
            key=lambda item: (self.priorities.get(self._candidate_state(item, now), 0), item.updated_at),
        )
        state = self._candidate_state(winner, now)
        return EffectiveState(state, winner.source, winner.session_id, winner.updated_at, False)

    def _recompute(self, reason: str = "state_refresh") -> EffectiveState:
        callbacks: list[Callable[[EffectiveState], None]] = []
        with self._lock:
            previous = self._effective
            next_state = self._calculate()
            changed = (
                next_state.state != self._effective.state
                or next_state.source != self._effective.source
                or next_state.session_id != self._effective.session_id
                or next_state.manual != self._effective.manual
            )
            self._effective = next_state
            if changed and next_state.state == "off":
                self._last_off = {
                    "code": reason,
                    "at": time(),
                    "source": previous.source,
                    "session_id": previous.session_id,
                    "from_state": previous.state,
                }
            if changed:
                callbacks = list(self._listeners)
        for callback in callbacks:
            callback(next_state)
        return next_state

    def snapshot(self) -> dict[str, Any]:
        self.expire()
        with self._lock:
            return {
                "effective": self._effective.to_dict(),
                "sessions": [item.to_dict() for item in sorted(self._sessions.values(), key=lambda row: row.updated_at, reverse=True)],
                "manual": {"state": self._manual[0], "until": self._manual[1]} if self._manual else None,
                "last_off": copy.deepcopy(self._last_off),
            }
