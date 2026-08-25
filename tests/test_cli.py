from __future__ import annotations

import io
import urllib.error

from agentlight import cli


def test_hook_posts_recovered_event_from_truncated_desktop_payload(monkeypatch) -> None:
    raw = (
        '{"session_id":"desktop-current-task","hook_event_name":"PostToolUse",'
        '"tool_name":"Bash","tool_response":{"output":"' + ("x" * 20_000)
    )
    posted: dict[str, object] = {}
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(raw))
    monkeypatch.setattr(cli.sys, "stdout", io.StringIO())

    def fake_request(method: str, path: str, body: dict[str, object]) -> dict[str, object]:
        posted.update({"method": method, "path": path, "body": body})
        return {"ok": True}

    monkeypatch.setattr(cli, "_request_with_start", fake_request)

    assert cli._hook("codex") == 0
    assert posted["method"] == "POST"
    assert posted["path"] == "/v1/events"
    assert posted["body"] == {
        "source": "codex",
        "sessionId": "desktop-current-task",
        "state": "busy",
        "eventName": "PostToolUse",
    }


def test_shutdown_requests_quit_without_starting_the_app(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_request(method: str, path: str, body=None, timeout: float = 2):
        calls.append((method, path))
        if method == "GET":
            raise urllib.error.URLError("stopped")
        return {"ok": True}

    monkeypatch.setattr(cli, "_request", fake_request)
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        cli,
        "_request_with_start",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("shutdown must not launch AgentLight")),
    )

    assert cli.main(["shutdown"]) == 0
    assert calls == [("POST", "/v1/quit"), ("GET", "/v1/status")]
