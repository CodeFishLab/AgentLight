from __future__ import annotations

import pytest

from agentlight.events import map_hook_payload, parse_hook_payload


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"hook_event_name": "SessionStart"}, "ready"),
        # 提交提示词后模型在推理，还没调工具
        ({"hook_event_name": "UserPromptSubmit"}, "thinking"),
        ({"hook_event_name": "SubagentStart"}, "subagent"),
        ({"hook_event_name": "SubagentStop"}, "subagent"),
        # 「请你批准」与「请你回答」是两件事
        ({"hook_event_name": "PermissionRequest"}, "permission"),
        ({"hook_event_name": "Notification", "notification_type": "permission_prompt"}, "permission"),
        ({"hook_event_name": "Notification", "notification_type": "idle_prompt"}, "ready"),
        ({"hook_event_name": "Notification", "notification_type": "agent_needs_input"}, "attention"),
        ({"hook_event_name": "PreToolUse", "tool_name": "functions.request_user_input"}, "attention"),
        ({"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion"}, "busy"),
        ({"hook_event_name": "Stop"}, "done"),
        ({"hook_event_name": "StopFailure"}, "error"),
        ({"hook_event_name": "SessionEnd"}, "off"),
    ],
)
def test_hook_mapping(payload, expected) -> None:
    payload["session_id"] = "session-1"
    mapped = map_hook_payload("codex", payload)
    assert mapped is not None
    assert mapped.state == expected
    assert mapped.session_id == "session-1"


def test_regular_tool_activity_refreshes_busy_state() -> None:
    mapped = map_hook_payload("codex", {"hook_event_name": "PreToolUse", "tool_name": "Bash"})
    assert mapped is not None
    assert mapped.state == "busy"


def test_truncated_desktop_payload_recovers_routing_fields() -> None:
    raw = (
        '{"session_id":"desktop-复杂","hook_event_name":"PreToolUse",'
        '"tool_name":"apply_patch","tool_input":{"command":"line 1\\n\\"quoted\\"",'
        '"large":"' + ("x" * 20_000)
    )

    payload = parse_hook_payload(raw)
    mapped = map_hook_payload("codex", payload)

    assert payload["tool_name"] == "apply_patch"
    assert mapped is not None
    assert mapped.session_id == "desktop-复杂"
    assert mapped.state == "busy"


def test_unrelated_notifications_are_ignored() -> None:
    assert map_hook_payload("claude", {"hook_event_name": "Notification", "notification_type": "whatever"}) is None
    assert map_hook_payload("claude", {"hook_event_name": "PreCompact"}) is None


def test_every_mapped_state_is_a_declared_state() -> None:
    from agentlight.models import VALID_STATES

    events = [
        "SessionStart", "UserPromptSubmit", "SubagentStart", "SubagentStop",
        "PermissionRequest", "PreToolUse", "PostToolUse", "Stop", "StopFailure", "SessionEnd",
    ]
    for event in events:
        mapped = map_hook_payload("codex", {"hook_event_name": event})
        assert mapped is None or mapped.state in VALID_STATES, event


def test_an_idle_session_never_escalates_into_an_alert() -> None:
    """idle_prompt 是「这个会话闲了 60 秒」，不是「有人在等你」。

    真实症状：一个已经收尾的会话在 Stop 之后 60 秒发来 idle_prompt，灯从「就绪」
    跳成最高优先级的「等待输入」，把灯从另一个正在干活的会话手里抢走，然后一直
    挂到 TTL 过期（实测 2 分钟）。开了自动审核、同时开多个会话时尤其明显。
    """
    from agentlight.arbitration import PROTECTED_STATES

    event = map_hook_payload("claude", {
        "hook_event_name": "Notification",
        "notification_type": "idle_prompt",
        "session_id": "idle-one",
    })

    assert event.state == "ready"
    assert event.state not in PROTECTED_STATES


def test_a_genuinely_blocked_agent_still_raises_an_alert() -> None:
    """别矫枉过正：agent 真的被挡住在等你回答时，仍然要亮告警。"""
    for kind in ("agent_needs_input", "elicitation_dialog", "elicitation_url_dialog"):
        event = map_hook_payload("claude", {
            "hook_event_name": "Notification",
            "notification_type": kind,
            "session_id": "blocked",
        })
        assert event.state == "attention", kind


def test_the_notification_kind_survives_into_the_event_name() -> None:
    """通知有好几种、映射结果各不相同。日志里只写 Notification 的话，
    出问题时分不清是哪一种触发的。"""
    event = map_hook_payload("claude", {
        "hook_event_name": "Notification",
        "notification_type": "idle_prompt",
        "session_id": "s",
    })

    assert event.event_name == "Notification:idle_prompt"
