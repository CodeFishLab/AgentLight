"""PowerShell 桥接脚本与 events.py 的事件映射必须一致。

Codex 的 hook 走 scripts/agentlightctl-hook.ps1，它自己重写了一遍事件→状态映射；
Claude 的 hook 走 events.py。两份实现如果漂了，症状是「Claude 报某个状态、Codex
永远不报」——不崩溃、不报错、不留日志，只是安静地少一半，极难排查。
"""

from __future__ import annotations

import re
from pathlib import Path

from agentlight.events import map_hook_payload
from agentlight.integrations import CODEX_EVENTS


BRIDGE = Path(__file__).parents[1] / "scripts" / "agentlightctl-hook.ps1"


def bridge_source() -> str:
    return BRIDGE.read_text(encoding="utf-8")


def bridge_switch() -> str:
    block = re.search(r"\$state = switch \(\$eventName\) \{(.*?)\n    \}", bridge_source(), re.S)
    assert block, "桥接脚本里找不到 $state = switch (\\$eventName) 结构"
    return block.group(1)


def bridge_simple_map() -> dict[str, str]:
    """只取一行式分支；PreToolUse 依赖工具名，单独验。"""
    return dict(re.findall(r"'(\w+)'\s*\{\s*'(\w+)'\s*\}", bridge_switch()))


def python_state(event: str, tool_name: str = "") -> str | None:
    payload = {"hook_event_name": event, "session_id": "parity"}
    if tool_name:
        payload["tool_name"] = tool_name
    mapped = map_hook_payload("codex", payload)
    return mapped.state if mapped else None


def test_bridge_covers_exactly_the_events_codex_registers() -> None:
    """少一个分支 Codex 就永远不报那个状态；多一个则是注册和映射对不上。"""
    covered = set(bridge_simple_map()) | {"PreToolUse"}

    assert covered == set(CODEX_EVENTS)


def test_bridge_maps_every_event_to_the_same_state_as_python() -> None:
    bridge = bridge_simple_map()

    for event in CODEX_EVENTS:
        if event == "PreToolUse":
            continue
        assert bridge.get(event) == python_state(event), event


def test_bridge_and_python_agree_on_input_tools() -> None:
    """PreToolUse 是唯一按工具名分流的事件，两边的判定必须同步。"""
    branch = re.search(r"'PreToolUse' \{(.*?)\n\s*\}", bridge_switch(), re.S)
    assert branch
    body = branch.group(1)

    assert "requestuserinput" in body and "askuserquestion" in body
    assert "'attention'" in body and "'busy'" in body

    assert python_state("PreToolUse", "functions.request_user_input") == "attention"
    assert python_state("PreToolUse", "AskUserQuestion") == "attention"
    assert python_state("PreToolUse", "Bash") == "busy"


def test_bridge_only_emits_states_the_backend_accepts() -> None:
    from agentlight.models import VALID_STATES

    for event, state in bridge_simple_map().items():
        assert state in VALID_STATES, f"{event} -> {state}"


def test_bridge_never_blocks_codex_on_failure() -> None:
    """Hook 出错必须静默记日志后退出 0，否则会拖住 Codex 的每一次工具调用。"""
    source = bridge_source()

    assert source.rstrip().endswith("exit 0")
    assert "Write-HookError" in source
    assert "-TimeoutSec 1" in source
    assert "for ($attempt" not in source
    assert "Start-Sleep" not in source


def test_bridge_never_falls_back_to_the_removed_machine_specific_data_root() -> None:
    """Hook bridge 只使用当前用户数据目录，不回退到机器专属路径。"""
    source = bridge_source()

    assert "AgentLightData" not in source
    assert "$env:LOCALAPPDATA" in source
