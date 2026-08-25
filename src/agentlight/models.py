from __future__ import annotations

from dataclasses import asdict, dataclass, field
from time import time
from typing import Any

from . import effects


# 顺序即界面与托盘子菜单的展示顺序，大致按一次任务的推进过程排列
VALID_STATES = (
    "ready",       # SessionStart
    "thinking",    # UserPromptSubmit：模型在推理，还没动手
    "busy",        # PreToolUse / PostToolUse：正在跑工具
    "subagent",    # SubagentStart / SubagentStop
    "attention",   # 等你输入：idle_prompt / agent_needs_input / 询问类工具
    "permission",  # 请求授权：PermissionRequest / permission_prompt
    "error",       # StopFailure
    "done",        # Stop
    "stale",       # 长时间没有新事件，之前那个「正在干活」的说法已经不可信
    "off",         # SessionEnd
)

# 配置页的 app.js 里有一份同样的表。两边都要用，又不值得为它加一次请求，
# 所以靠 tests/test_webui_parity.py 卡住漂移。
STATE_LABELS: dict[str, str] = {
    "ready": "就绪",
    "thinking": "思考中",
    "busy": "工作中",
    "subagent": "子任务",
    "attention": "等待输入",
    "permission": "请求授权",
    "error": "错误",
    "done": "已完成",
    "stale": "状态存疑",
    "off": "熄灭",
}


# Anthropic 会返回这个未公开的内部桶：没有重置时间、没有金额上限、也没有可操作
# 含义，0% utilization 画出来就是一个假的「还剩 100%」。三条链路都要过滤——
# 原生 OAuth 响应、旧缓存、以及 Claude Code 喂给状态栏的 rate_limits。放在这里
# 是因为 statusline 每 5 秒跑一次，不该为了一个 frozenset 去 import 整个网络模块。
HIDDEN_QUOTA_WINDOW_KEYS = frozenset({"nimbus_quill"})


@dataclass(slots=True)
class StateProfile:
    colors: list[str]
    mode: str = "static"
    brightness: int = 20
    period_ms: int = 1000
    sound: str = "stop"
    effect: str = "static"
    effect_params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "StateProfile":
        colors = [str(item).lstrip("#").lower() for item in value.get("colors", [])]
        while len(colors) < 3:
            colors.append("000000")
        # 兼容仅提供 mode 的配置；effect 优先，否则沿用 mode。
        spec = effects.resolve(value.get("effect") or value.get("mode") or effects.DEFAULT_EFFECT)
        return cls(
            colors=colors[:3],
            # mode 始终是可以直接下发给固件 0x10 的值：软件效果期间固件保持常亮
            mode=spec.id if spec.kind == "hardware" else "static",
            brightness=max(0, min(100, int(value.get("brightness", 20)))),
            period_ms=max(100, min(65535, int(value.get("period_ms", 1000)))),
            sound=str(value.get("sound", "stop")),
            effect=spec.id,
            effect_params=effects.normalize_params(spec.id, value.get("effect_params")),
        )

    @property
    def is_software_effect(self) -> bool:
        return effects.is_software(self.effect)

    def frame(self, phase: float) -> list[tuple[int, int, int]]:
        """计算某个相位下三颗灯的 RGB，供软件动画逐帧下发。"""
        return effects.render_frame(self.effect, self.colors, phase, self.effect_params)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SessionRecord:
    source: str
    session_id: str
    state: str
    updated_at: float = field(default_factory=time)
    expires_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class EffectiveState:
    state: str
    source: str = "system"
    session_id: str = ""
    updated_at: float = field(default_factory=time)
    manual: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
