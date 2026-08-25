from __future__ import annotations

import threading

from agentlight.service import AgentLightService


class StubClaudeQuota:
    def __init__(self) -> None:
        self.refresh_calls = 0

    def snapshot(self) -> dict:
        return {"status": "cached", "quota_windows": []}

    def request_refresh(self, *, wait: bool = False) -> dict:
        self.refresh_calls += 1
        return {"status": "connected", "quota_windows": [], "waited": wait}


def quota_service() -> tuple[AgentLightService, StubClaudeQuota]:
    service = AgentLightService.__new__(AgentLightService)
    provider = StubClaudeQuota()
    service.claude_quota = provider
    service._quota_cache = None
    service._quota_cache_until = 0.0
    # statusLine 兜底所需的状态：这里默认没有推送，走纯 OAuth 路径
    service._claude_statusline = None
    service._statusline_lock = threading.RLock()
    service._codex_quota = lambda: {"available": True, "windows": [{"label": "Weekly"}]}
    return service, provider


def test_codex_refresh_never_waits_for_claude() -> None:
    service, provider = quota_service()

    result = service.quota_snapshot(refresh=True, source="codex")

    assert result["codex"]["available"] is True
    assert result["claude"]["status"] == "cached"
    assert provider.refresh_calls == 0


def test_claude_refresh_keeps_cached_codex_and_waits_for_provider() -> None:
    service, provider = quota_service()
    service.quota_snapshot(refresh=True, source="codex")

    result = service.quota_snapshot(refresh=True, source="claude")

    assert result["codex"]["windows"] == [{"label": "Weekly"}]
    assert result["claude"]["waited"] is True
    assert provider.refresh_calls == 1


def _with_statusline(windows, age=0.0):
    import time
    service, provider = quota_service()
    service._claude_statusline = {"windows": windows, "at": time.time() - age} if windows else None
    return service, provider


WINDOW = [{"key": "five_hour", "label": "5h", "remaining_percent": 77}]


def test_oauth_wins_when_it_actually_has_fresh_numbers() -> None:
    """用户定的优先级：OAuth 是权威来源，Claude Code 没开着它也能取。"""
    service, provider = _with_statusline(WINDOW)
    provider.snapshot = lambda: {"status": "connected", "quota_windows": [{"key": "five_hour", "remaining_percent": 12}]}

    claude = service.quota_snapshot()["claude"]

    assert claude["status"] == "connected"
    assert claude["quota_windows"][0]["remaining_percent"] == 12


def test_statusline_fills_in_when_oauth_cannot_authenticate() -> None:
    """桌面端登录时 OAuth 那条路永远拿不到凭据。与其退回几小时前的缓存，
    不如用 Claude Code 正在推送的活数字。"""
    service, provider = _with_statusline(WINDOW)
    provider.snapshot = lambda: {"status": "credential_stale", "quota_windows": [{"key": "five_hour", "remaining_percent": 12}]}

    claude = service.quota_snapshot()["claude"]

    assert claude["status"] == "statusline"
    assert claude["quota_source"] == "claude_code_statusline"
    assert claude["quota_windows"][0]["remaining_percent"] == 77


def test_a_stale_statusline_push_is_discarded() -> None:
    """Claude Code 一关就不再推送。拿昨天的数字冒充实时值，和绿环撒谎是一回事。"""
    service, provider = _with_statusline(WINDOW, age=600.0)
    provider.snapshot = lambda: {"status": "credential_stale", "quota_windows": []}

    claude = service.quota_snapshot()["claude"]

    assert claude["status"] == "credential_stale"


def test_recording_an_empty_push_clears_the_fallback() -> None:
    service, _ = _with_statusline(WINDOW)
    service.record_claude_statusline([])
    assert service._statusline_quota() is None
