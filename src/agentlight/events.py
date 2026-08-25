from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


# source 与 session_id 来自 Hook 载荷和本地 API，是外部可控的。它们会进日志、进
# 状态快照、经 WebSocket 广播，最后渲染到配置页上——必须先剥掉标记字符并限长，
# 既防 XSS，也防超长字符串把广播撑爆。
_UNSAFE_IDENTIFIER = re.compile(r"[^\w.:-]", re.UNICODE)
MAX_IDENTIFIER_LENGTH = 64
_RECOVERABLE_HOOK_FIELDS = ("hook_event_name", "session_id", "thread_id", "tool_name")


def sanitize_identifier(value: Any, limit: int = MAX_IDENTIFIER_LENGTH, fallback: str = "unknown") -> str:
    cleaned = _UNSAFE_IDENTIFIER.sub("", str(value or "")).strip()[:limit]
    return cleaned or fallback


def parse_hook_payload(raw: str) -> dict[str, Any]:
    """Parse a hook payload and recover required routing fields from truncated JSON."""
    try:
        payload = json.loads(raw.lstrip("\ufeff"))
    except json.JSONDecodeError as original_error:
        payload = {}
        for field in _RECOVERABLE_HOOK_FIELDS:
            pattern = rf'"{re.escape(field)}"\s*:\s*"((?:\\.|[^"\\])*)"'
            match = re.search(pattern, raw)
            if not match:
                continue
            try:
                payload[field] = json.loads(f'"{match.group(1)}"')
            except json.JSONDecodeError:
                payload[field] = match.group(1)
        if not payload.get("hook_event_name"):
            raise original_error
    return payload if isinstance(payload, dict) else {}


@dataclass(slots=True)
class MappedEvent:
    source: str
    session_id: str
    state: str
    event_name: str


# 真正「agent 被挡住了，在等你回答」的通知。idle_prompt 不在其中：它只是说
# 这个会话闲着，没有任何人在等你操作。
ATTENTION_NOTIFICATIONS = frozenset({
    "agent_needs_input",
    "elicitation_dialog",
    "elicitation_url_dialog",
})


def _is_input_tool(value: object) -> bool:
    normalized = str(value or "").lower().replace("_", "")
    return normalized.endswith("requestuserinput") or normalized.endswith("askuserquestion")


def map_hook_payload(source: str, payload: dict[str, Any]) -> MappedEvent | None:
    event = sanitize_identifier(payload.get("hook_event_name", ""), fallback="")
    session_id = sanitize_identifier(payload.get("session_id") or payload.get("thread_id"), fallback="default")
    tool_name = payload.get("tool_name")
    notification = str(payload.get("notification_type", ""))
    state: str | None = None

    if event == "SessionStart":
        state = "ready"
    elif event == "UserPromptSubmit":
        # 刚收到提示词，模型在推理，还没开始调工具
        state = "thinking"
    elif event in {"SubagentStart", "SubagentStop"}:
        state = "subagent"
    elif event == "PermissionRequest":
        state = "permission"
    elif event == "Notification":
        # 「请你批准」和「请你回答」是两件事，别再合并成同一个状态
        if notification == "permission_prompt":
            state = "permission"
        elif notification in ATTENTION_NOTIFICATIONS:
            state = "attention"
        elif notification == "idle_prompt":
            # 「这个会话闲了 60 秒」不是告警。Stop 早就报过 done（已完成），
            # 那才是「轮到你了」。再升级成最高优先级的 attention，只会让一个
            # 已经收尾的会话把灯从正在干活的会话手里抢走，然后挂满整个 TTL。
            state = "ready"
    elif event == "PreToolUse":
        state = "attention" if _is_input_tool(tool_name) else "busy"
    elif event == "PostToolUse":
        state = "busy"
    elif event == "Stop":
        state = "done"
    elif event == "StopFailure":
        state = "error"
    elif event == "SessionEnd":
        state = "off"

    if state is None:
        return None
    if event == "Notification" and notification:
        # 通知有好几种，映射结果各不相同。日志里只写「Notification」的话，
        # 出问题时根本分不清是哪一种触发的（这条就是这么查了半天才定位的）。
        event = sanitize_identifier(f"{event}:{notification}", fallback=event)
    return MappedEvent(
        source=sanitize_identifier(source),
        session_id=session_id,
        state=state,
        event_name=event,
    )
