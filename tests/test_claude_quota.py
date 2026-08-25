from __future__ import annotations

import json
import threading
import time
import urllib.request

import pytest

from agentlight import claude_quota


def credential_document(**overrides):
    oauth = {
        "accessToken": "access-secret",
        "refreshToken": "refresh-secret",
        "expiresAt": int(time.time() * 1000) + 3_600_000,
        "refreshTokenExpiresAt": int(time.time() * 1000) + 86_400_000,
        "scopes": ["user:profile", "user:inference"],
        "subscriptionType": "max",
        "unknownFutureField": {"preserve": True},
    }
    oauth.update(overrides)
    return {"claudeAiOauth": oauth, "unrelated": {"keep": True}}


def write_credential(path, **overrides):
    path.write_text(json.dumps(credential_document(**overrides)), encoding="utf-8")


def quota_windows(used=42.0):
    return claude_quota.parse_usage_response({
        "five_hour": {"utilization": used, "resets_at": "2026-08-20T18:00:00Z"},
        "seven_day": {"utilization": 18.0, "resets_at": "2026-08-24T00:00:00Z"},
    })


def test_credential_exists_and_is_parsed_without_exposing_document(tmp_path) -> None:
    path = tmp_path / ".credentials.json"
    write_credential(path)

    credential, document = claude_quota.read_credential(path)

    assert credential.access_token == "access-secret"
    assert credential.refresh_token == "refresh-secret"
    assert credential.scopes == ("user:profile", "user:inference")
    assert document["unrelated"] == {"keep": True}


def test_missing_credential_requires_authentication(tmp_path) -> None:
    with pytest.raises(claude_quota.ClaudeAuthRequired):
        claude_quota.read_credential(tmp_path / "missing.json")


@pytest.mark.parametrize("content", ["{bad", "[]", "{}", '{"claudeAiOauth": null}'])
def test_malformed_credential_requires_authentication(tmp_path, content) -> None:
    path = tmp_path / ".credentials.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(claude_quota.ClaudeAuthRequired):
        claude_quota.read_credential(path)


def test_valid_usage_response_maps_five_hour_and_weekly() -> None:
    windows = quota_windows()
    assert [window["key"] for window in windows] == ["five_hour", "seven_day"]
    assert windows[0]["used_percent"] == 42.0
    assert windows[0]["remaining_percent"] == 58
    assert windows[0]["label"] == "5h"
    assert windows[1]["label"] == "Weekly"
    assert isinstance(windows[0]["resets_at"], int)


def test_null_and_missing_quota_fields_do_not_become_zero() -> None:
    windows = claude_quota.parse_usage_response({
        "five_hour": None,
        "seven_day": {"utilization": 25},
        "extra_usage": {"is_enabled": True},
    })
    assert [window["key"] for window in windows] == ["seven_day"]
    assert windows[0]["remaining_percent"] == 75


def test_undocumented_nimbus_quill_bucket_is_hidden() -> None:
    windows = claude_quota.parse_usage_response({
        "five_hour": {"utilization": 25, "resets_at": "2026-08-20T18:00:00Z"},
        "nimbus_quill": {
            "utilization": 0,
            "resets_at": None,
            "limit_dollars": None,
            "used_dollars": None,
            "remaining_dollars": None,
        },
    })

    assert [window["key"] for window in windows] == ["five_hour"]


class SequenceClient:
    """按顺序吐出预设结果的假客户端：值就返回，异常就抛。

    带 stamp() 是因为 Provider 要靠它感知 Claude Code 写回凭据；
    这里固定不变，表示「凭据没动过」。
    """

    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    def stamp(self):
        return (1, 1)

    def fetch(self):
        self.calls += 1
        result = self.results.pop(0) if self.results else self.results
        if isinstance(result, Exception):
            raise result
        return result


def test_cached_nimbus_quill_bucket_is_hidden_immediately(tmp_path) -> None:
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({
        "version": claude_quota.CACHE_VERSION,
        "quota_windows": [
            {"key": "seven_day", "remaining_percent": 41},
            {"key": "nimbus_quill", "remaining_percent": 100},
        ],
        "quota_updated_at": int(time.time()),
        "last_successful_refresh": int(time.time()),
    }), encoding="utf-8")

    provider = claude_quota.ClaudeQuotaProvider(path, client=SequenceClient(quota_windows()))

    assert [window["key"] for window in provider.snapshot()["quota_windows"]] == ["seven_day"]


def test_response_without_any_quota_window_is_rejected() -> None:
    with pytest.raises(claude_quota.ClaudeQuotaError):
        claude_quota.parse_usage_response({"five_hour": {"utilization": None}})


def test_http_client_explicitly_builds_a_proxy_aware_opener(monkeypatch) -> None:
    seen = {"proxy_handler": False, "opened": False}

    class Response:
        status = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'{"five_hour":{"utilization":1}}'

    class Opener:
        def open(self, request, timeout):
            seen["opened"] = request.full_url == claude_quota.USAGE_URL and timeout > 0
            return Response()

    def proxy_handler():
        seen["proxy_handler"] = True
        return object()

    monkeypatch.setattr(urllib.request, "ProxyHandler", proxy_handler)
    monkeypatch.setattr(urllib.request, "build_opener", lambda handler: Opener())

    status, payload, _ = claude_quota._json_request(claude_quota.USAGE_URL)
    assert status == 200
    assert payload["five_hour"]["utilization"] == 1
    assert seen == {"proxy_handler": True, "opened": True}


def test_an_expired_token_is_reported_as_waiting_not_refreshed(tmp_path, monkeypatch) -> None:
    """凭据是 Claude Code 的，refresh token 又是一次性的。两个进程抢着轮换同一个，
    AgentLight 只要抢先成功一次，Claude Code 就可能被登出。所以过期了只能等。"""
    path = tmp_path / ".credentials.json"
    write_credential(path, expiresAt=1)
    before = path.read_text(encoding="utf-8")
    monkeypatch.setattr(claude_quota, "_json_request", lambda *a, **k: pytest.fail("不该发任何请求"))

    with pytest.raises(claude_quota.ClaudeCredentialStale):
        claude_quota.ClaudeCredentialReader(path).access_token()

    # 最要紧的一条：文件一个字节都不能动
    assert path.read_text(encoding="utf-8") == before


def test_the_credential_file_is_never_written(tmp_path) -> None:
    """整个模块都不该有写凭据的能力，别留后门。"""
    import inspect

    source = inspect.getsource(claude_quota)
    assert "TOKEN_URL" not in source
    assert "_write_refreshed" not in source
    assert "grant_type" not in source
    assert not hasattr(claude_quota.ClaudeCredentialReader, "_refresh")


def test_a_valid_token_is_handed_over_untouched(tmp_path) -> None:
    path = tmp_path / ".credentials.json"
    write_credential(path)
    assert claude_quota.ClaudeCredentialReader(path).access_token() == "access-secret"


def test_the_stamp_changes_when_claude_code_writes_the_file_back(tmp_path) -> None:
    """等待态唯一的唤醒信号。stamp 不动，用户重启完 Claude Code 也醒不过来。"""
    path = tmp_path / ".credentials.json"
    write_credential(path, expiresAt=1)
    reader = claude_quota.ClaudeCredentialReader(path)
    first = reader.stamp()
    assert first is not None

    write_credential(path, accessToken="fresh-from-claude-code")
    assert reader.stamp() != first


def test_a_missing_credential_file_has_no_stamp(tmp_path) -> None:
    assert claude_quota.ClaudeCredentialReader(tmp_path / "nope.json").stamp() is None


def test_a_rejected_token_is_not_retried(monkeypatch) -> None:
    """401 后等待 Claude Code 更新凭据，不重复发送无效请求。"""
    class Reader:
        def __init__(self):
            self.calls = 0

        def stamp(self):
            return (1, 1)

        def access_token(self):
            self.calls += 1
            return "stale-token"

    reader = Reader()
    client = claude_quota.AnthropicUsageClient(reader)
    calls = []

    def fake_request(url, **kwargs):
        calls.append(url)
        raise claude_quota.ClaudeAuthRequired("rejected")

    monkeypatch.setattr(claude_quota, "_json_request", fake_request)

    with pytest.raises(claude_quota.ClaudeAuthRequired):
        client.fetch()

    assert reader.calls == 1
    assert len(calls) == 1


def test_cached_state_survives_timeout_and_server_error(tmp_path) -> None:
    client = SequenceClient(quota_windows(), claude_quota.ClaudeOffline(), claude_quota.ClaudeServerError())
    provider = claude_quota.ClaudeQuotaProvider(tmp_path / "cache.json", client=client)

    provider._refresh_once()
    first = provider.snapshot()["quota_windows"]
    provider._refresh_once()
    assert provider.snapshot()["status"] == "offline"
    assert provider.snapshot()["quota_windows"] == first
    provider._refresh_once()
    assert provider.snapshot()["status"] == "error"
    assert provider.snapshot()["quota_windows"] == first


def test_429_keeps_cache_and_respects_retry_after(tmp_path) -> None:
    client = SequenceClient(quota_windows(), claude_quota.ClaudeRateLimited(600))
    provider = claude_quota.ClaudeQuotaProvider(tmp_path / "cache.json", client=client)
    provider._refresh_once()
    first = provider.snapshot()["quota_windows"]
    provider._refresh_once()
    state = provider.snapshot()
    assert state["status"] == "rate_limited"
    assert state["quota_windows"] == first
    assert state["next_retry_at"] >= int(time.time()) + 590


def test_cache_contains_only_non_sensitive_quota_data(tmp_path) -> None:
    path = tmp_path / "cache.json"
    provider = claude_quota.ClaudeQuotaProvider(path, client=SequenceClient(quota_windows()))
    provider._refresh_once()
    text = path.read_text(encoding="utf-8")
    assert "access-secret" not in text
    assert "refresh-secret" not in text
    assert "Authorization" not in text
    assert set(json.loads(text)) == {"version", "quota_windows", "quota_updated_at", "last_successful_refresh"}


def test_refresh_cooldown_prevents_request_spam(tmp_path) -> None:
    client = SequenceClient(quota_windows(), quota_windows(43))
    provider = claude_quota.ClaudeQuotaProvider(tmp_path / "cache.json", client=client)
    provider.start()
    try:
        provider.request_refresh(wait=True)
        first_calls = client.calls
        provider.request_refresh(wait=True)
        provider.request_refresh(wait=True)
        assert client.calls == first_calls
    finally:
        provider.stop()


def test_background_refresh_start_does_not_block_caller(tmp_path) -> None:
    entered = threading.Event()
    release = threading.Event()

    class SlowClient:
        def fetch(self):
            entered.set()
            release.wait(2)
            return quota_windows()

    provider = claude_quota.ClaudeQuotaProvider(tmp_path / "cache.json", client=SlowClient())
    before = time.monotonic()
    provider.start()
    elapsed = time.monotonic() - before
    try:
        assert elapsed < 0.2
        assert entered.wait(1)
    finally:
        release.set()
        provider.stop()


def test_clean_provider_shutdown(tmp_path) -> None:
    provider = claude_quota.ClaudeQuotaProvider(tmp_path / "cache.json", client=SequenceClient(quota_windows()))
    provider.start()
    provider.request_refresh(wait=True)
    provider.stop()
    assert provider._thread is not None
    assert not provider._thread.is_alive()


def test_a_429_never_locks_out_manual_refresh_when_auto_refresh_is_off(tmp_path) -> None:
    """周期刷新关掉时「下次刷新」是 inf。拿它当 429 冷却判据的话，吃过一次限流
    就再也手动刷不动了，只能重启——冷却必须自己记时间。"""
    client = SequenceClient(quota_windows(), claude_quota.ClaudeRateLimited(1), quota_windows(43))
    provider = claude_quota.ClaudeQuotaProvider(tmp_path / "cache.json", refresh_interval=0, client=client)
    provider._refresh_once()
    provider._refresh_once()
    assert provider.snapshot()["status"] == "rate_limited"

    # 冷却还没过：挡住，但记的是冷却时间而不是 inf
    assert provider._rate_limited_until > time.monotonic()
    assert provider._next_refresh_monotonic == float("inf")

    # 冷却过了就必须放行。两道闸门都要放开：429 冷却之外还有一道 60 秒的网络节流，
    # 上面两次同步 _refresh_once() 刚把它踩下去，不清掉的话 request_refresh 会在
    # 后台线程完成启动刷新之前就短路返回，测试变成掷骰子。
    provider._rate_limited_until = time.monotonic() - 1
    provider._last_network_monotonic = 0.0
    provider.start()
    try:
        provider.request_refresh(wait=True)
        assert provider.snapshot()["status"] == "connected"
    finally:
        provider.stop()


def test_a_successful_refresh_clears_the_rate_limit_cooldown(tmp_path) -> None:
    client = SequenceClient(claude_quota.ClaudeRateLimited(600), quota_windows())
    provider = claude_quota.ClaudeQuotaProvider(tmp_path / "cache.json", client=client)
    provider._refresh_once()
    assert provider._rate_limited_until > time.monotonic()

    provider._refresh_once()
    assert provider._rate_limited_until == 0.0


def test_errors_name_which_endpoint_failed(monkeypatch) -> None:
    """配额接口和 token 刷新接口都会抛「HTTP 403」，但排查方向完全相反：
    一个是额度/权限，一个是登录凭据。只写「Claude endpoint」等于没写。"""
    import urllib.error

    def boom(*args, **kwargs):
        raise urllib.error.HTTPError("https://x", 403, "Forbidden", {}, None)

    class Opener:
        open = staticmethod(boom)

    monkeypatch.setattr(claude_quota.urllib.request, "build_opener", lambda *a: Opener())

    with pytest.raises(claude_quota.ClaudeQuotaError) as usage:
        claude_quota._json_request(claude_quota.USAGE_URL, what="usage endpoint")
    assert "usage endpoint" in str(usage.value)

    with pytest.raises(claude_quota.ClaudeQuotaError) as token:
        claude_quota._json_request("https://example.invalid/x", what="token refresh endpoint")
    assert "token refresh endpoint" in str(token.value)


class StaleThenFreshClient:
    """先报「令牌过期」，等凭据 stamp 一变就返回真实配额。

    模拟的正是用户重启 Claude Code 那一刻。
    """

    def __init__(self):
        self.current = (1, 1)
        self.calls = 0

    def stamp(self):
        return self.current

    def fetch(self):
        self.calls += 1
        if self.current == (1, 1):
            raise claude_quota.ClaudeCredentialStale("等 Claude Code 写回")
        return quota_windows(20.0)


def test_an_expired_token_becomes_a_waiting_state_that_keeps_the_cache(tmp_path) -> None:
    client = SequenceClient(quota_windows(), claude_quota.ClaudeCredentialStale("过期"))
    provider = claude_quota.ClaudeQuotaProvider(tmp_path / "cache.json", client=client)

    provider._refresh_once()
    good = provider.snapshot()["quota_windows"]
    provider._refresh_once()

    state = provider.snapshot()
    assert state["status"] == "credential_stale"
    # 数字还在，只是不再声称是新的
    assert state["quota_windows"] == good


def test_waiting_does_not_pile_up_backoff(tmp_path) -> None:
    """等待不是故障。越等越慢的话，凭据回来了还要再干等半小时。"""
    client = SequenceClient(*[claude_quota.ClaudeCredentialStale("过期")] * 4)
    provider = claude_quota.ClaudeQuotaProvider(tmp_path / "cache.json", client=client)

    for _ in range(4):
        provider._refresh_once()

    assert provider._backoff_index == 0


def test_a_rewritten_credential_file_wakes_the_provider_up(tmp_path) -> None:
    """用户重启 Claude Code 之后必须自己恢复，不能还要手动点刷新。"""
    client = StaleThenFreshClient()
    provider = claude_quota.ClaudeQuotaProvider(tmp_path / "cache.json", refresh_interval=0, client=client)
    provider.start()
    try:
        deadline = time.time() + 3
        while provider.snapshot()["status"] != "credential_stale" and time.time() < deadline:
            time.sleep(0.02)
        assert provider.snapshot()["status"] == "credential_stale"

        # Claude Code 写回新凭据 —— 只有 stamp 变了这一个信号
        client.current = (2, 2)

        deadline = time.time() + 5
        while provider.snapshot()["status"] != "connected" and time.time() < deadline:
            time.sleep(0.02)
        assert provider.snapshot()["status"] == "connected"
        assert provider.snapshot()["quota_windows"][0]["remaining_percent"] == 80
    finally:
        provider.stop()
