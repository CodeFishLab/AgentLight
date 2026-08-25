"""灯效定义与软件动画帧计算。

固件只提供 static / blink / breath 三种模式（App 层命令 0x10），而且模式、周期和
亮度都是全局的，三颗灯无法各自动画。

软件效果的做法是把模式固定为 static，由主机按帧连续下发 App 层命令 0x11（设三颗
灯颜色）。每颗灯的亮度通过缩放 RGB 数值实现，因为 0x10 的亮度是全局的——这样拖尾
渐隐、逐个呼吸都能做，而且一帧只要一条指令。

这里只计算颜色，不碰设备；下发由 device.HardwareWorker 负责。
"""

from __future__ import annotations

import colorsys
import math
from dataclasses import dataclass
from typing import Any, Callable


LED_COUNT = 3
BLACK: tuple[int, int, int] = (0, 0, 0)

# 固件原生模式，交给设备自己跑，主机零开销
HARDWARE_MODES = ("static", "blink", "breath")
# 软件效果，由主机按帧推送
SOFTWARE_EFFECTS = ("chase", "comet", "pulse_seq", "alternate", "wipe", "rainbow")

DEFAULT_EFFECT = "static"
DEFAULT_FRAME_RATE = 20
MIN_FRAME_RATE = 4
MAX_FRAME_RATE = 30

Rgb = tuple[int, int, int]
Renderer = Callable[[list[Rgb], float, dict[str, Any]], list[Rgb]]


@dataclass(slots=True, frozen=True)
class ParamSpec:
    key: str
    label: str
    type: str
    default: Any
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    help: str = ""

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "key": self.key,
            "label": self.label,
            "type": self.type,
            "default": self.default,
        }
        if self.minimum is not None:
            value["minimum"] = self.minimum
        if self.maximum is not None:
            value["maximum"] = self.maximum
        if self.choices:
            value["choices"] = list(self.choices)
        if self.help:
            value["help"] = self.help
        return value


@dataclass(slots=True, frozen=True)
class EffectSpec:
    id: str
    kind: str
    label: str
    description: str
    params: tuple[ParamSpec, ...] = ()
    uses_colors: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "description": self.description,
            "usesColors": self.uses_colors,
            "params": [item.to_dict() for item in self.params],
        }


_DIRECTION = ParamSpec(
    key="direction",
    label="方向",
    type="enum",
    default="forward",
    choices=("forward", "backward"),
    help="forward 为第一颗灯到第三颗灯。",
)


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


def parse_color(value: Any) -> Rgb:
    """把 "00ff00" 或 "#00FF00" 解析成 RGB；无法解析时返回全黑。"""
    text = str(value or "").lstrip("#").strip()
    if len(text) != 6:
        return BLACK
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return BLACK


def format_color(rgb: Rgb) -> str:
    return "".join(f"{max(0, min(255, int(channel))):02x}" for channel in rgb)


def scale(rgb: Rgb, intensity: float) -> Rgb:
    """按 0..1 的强度缩放颜色，用来模拟每颗灯的独立亮度。"""
    factor = _clamp(float(intensity), 0.0, 1.0)
    return (round(rgb[0] * factor), round(rgb[1] * factor), round(rgb[2] * factor))


def _step(direction: Any) -> int:
    return -1 if str(direction) == "backward" else 1


def _ordered_index(index: int, direction: Any) -> int:
    return index if _step(direction) > 0 else LED_COUNT - 1 - index


def _render_static(colors: list[Rgb], phase: float, params: dict[str, Any]) -> list[Rgb]:
    return list(colors)


def _render_blink(colors: list[Rgb], phase: float, params: dict[str, Any]) -> list[Rgb]:
    # 固件的闪烁是 50% 占空的方波，这里保持一致，不开放占空参数
    return list(colors) if phase < 0.5 else [BLACK] * LED_COUNT


def _render_breath(colors: list[Rgb], phase: float, params: dict[str, Any]) -> list[Rgb]:
    intensity = 0.5 - 0.5 * math.cos(2 * math.pi * phase)
    return [scale(color, intensity) for color in colors]


def _render_chase(colors: list[Rgb], phase: float, params: dict[str, Any]) -> list[Rgb]:
    duty = _clamp(float(params.get("duty", 1.0)), 0.05, 1.0)
    slot = phase * LED_COUNT
    position = int(slot) % LED_COUNT
    lit = (slot - int(slot)) < duty
    active = _ordered_index(position, params.get("direction"))
    return [colors[i] if (i == active and lit) else BLACK for i in range(LED_COUNT)]


def _render_comet(colors: list[Rgb], phase: float, params: dict[str, Any]) -> list[Rgb]:
    tail = _clamp(float(params.get("tail", 0.35)), 0.05, 0.95)
    head = phase * LED_COUNT
    forward = _step(params.get("direction")) > 0
    frame: list[Rgb] = []
    for index in range(LED_COUNT):
        distance = (head - index) % LED_COUNT if forward else (index - head) % LED_COUNT
        frame.append(scale(colors[index], tail**distance))
    return frame


def _render_pulse_seq(colors: list[Rgb], phase: float, params: dict[str, Any]) -> list[Rgb]:
    spread = _clamp(float(params.get("spread", 1 / 3)), 0.0, 1.0)
    frame: list[Rgb] = []
    for index in range(LED_COUNT):
        offset = _ordered_index(index, params.get("direction")) * spread
        intensity = 0.5 - 0.5 * math.cos(2 * math.pi * (phase - offset))
        frame.append(scale(colors[index], intensity))
    return frame


def _render_alternate(colors: list[Rgb], phase: float, params: dict[str, Any]) -> list[Rgb]:
    duty = _clamp(float(params.get("duty", 0.5)), 0.05, 0.95)
    first = phase < duty
    return [
        colors[0] if first else BLACK,
        BLACK if first else colors[1],
        colors[2] if first else BLACK,
    ]


def _render_wipe(colors: list[Rgb], phase: float, params: dict[str, Any]) -> list[Rgb]:
    doubled = phase * 2
    filling = doubled < 1
    progress = (doubled if filling else doubled - 1) * LED_COUNT
    frame: list[Rgb] = []
    for index in range(LED_COUNT):
        ordered = _ordered_index(index, params.get("direction"))
        edge = progress - ordered
        intensity = _clamp(edge, 0.0, 1.0) if filling else _clamp(1.0 - edge, 0.0, 1.0)
        frame.append(scale(colors[index], intensity))
    return frame


def _render_rainbow(colors: list[Rgb], phase: float, params: dict[str, Any]) -> list[Rgb]:
    spread = _clamp(float(params.get("spread", 1 / 3)), 0.0, 1.0)
    saturation = _clamp(float(params.get("saturation", 1.0)), 0.0, 1.0)
    direction = _step(params.get("direction"))
    frame: list[Rgb] = []
    for index in range(LED_COUNT):
        hue = (phase * direction + index * spread) % 1.0
        red, green, blue = colorsys.hsv_to_rgb(hue, saturation, 1.0)
        frame.append((round(red * 255), round(green * 255), round(blue * 255)))
    return frame


_SPECS: tuple[EffectSpec, ...] = (
    EffectSpec(
        id="static",
        kind="hardware",
        label="常亮",
        description="三颗灯保持设定颜色不变，由固件执行，主机零开销。",
    ),
    EffectSpec(
        id="blink",
        kind="hardware",
        label="闪烁",
        description="一轮亮起与熄灭，由固件执行。周期越小闪得越快。",
    ),
    EffectSpec(
        id="breath",
        kind="hardware",
        label="呼吸",
        description="一轮渐亮与渐暗，由固件执行。实际观感以设备为准。",
    ),
    EffectSpec(
        id="chase",
        kind="software",
        label="跑马灯",
        description="三颗灯依次点亮，走完一轮为一个周期。",
        params=(
            _DIRECTION,
            ParamSpec(
                key="duty",
                label="点亮占比",
                type="float",
                default=1.0,
                minimum=0.05,
                maximum=1.0,
                help="每颗灯在自己的时段里点亮的比例，1.0 为无缝衔接。",
            ),
        ),
    ),
    EffectSpec(
        id="comet",
        kind="software",
        label="彗星拖尾",
        description="亮点沿三颗灯移动，身后按指数渐隐。",
        params=(
            _DIRECTION,
            ParamSpec(
                key="tail",
                label="拖尾强度",
                type="float",
                default=0.35,
                minimum=0.05,
                maximum=0.95,
                help="数值越大拖尾越长越亮。",
            ),
        ),
    ),
    EffectSpec(
        id="pulse_seq",
        kind="software",
        label="逐个呼吸",
        description="三颗灯错开相位依次呼吸，像波浪一样推过去。",
        params=(
            _DIRECTION,
            ParamSpec(
                key="spread",
                label="相位间隔",
                type="float",
                default=1 / 3,
                minimum=0.0,
                maximum=1.0,
                help="0 为三颗灯同步呼吸，1/3 为均匀错开。",
            ),
        ),
    ),
    EffectSpec(
        id="alternate",
        kind="software",
        label="交替闪",
        description="第一、三颗灯与第二颗灯交替点亮。",
        params=(
            ParamSpec(
                key="duty",
                label="首组占比",
                type="float",
                default=0.5,
                minimum=0.05,
                maximum=0.95,
                help="第一、三颗灯在一个周期里占的时间比例。",
            ),
        ),
    ),
    EffectSpec(
        id="wipe",
        kind="software",
        label="扫描填充",
        description="前半周期从一端逐个填满，后半周期再逐个清空。",
        params=(_DIRECTION,),
    ),
    EffectSpec(
        id="rainbow",
        kind="software",
        label="色相轮转",
        description="三颗灯错开相位轮转色相。此效果忽略配置的颜色。",
        params=(
            _DIRECTION,
            ParamSpec(
                key="spread",
                label="色相间隔",
                type="float",
                default=1 / 3,
                minimum=0.0,
                maximum=1.0,
            ),
            ParamSpec(
                key="saturation",
                label="饱和度",
                type="float",
                default=1.0,
                minimum=0.0,
                maximum=1.0,
            ),
        ),
        uses_colors=False,
    ),
)

REGISTRY: dict[str, EffectSpec] = {spec.id: spec for spec in _SPECS}

_RENDERERS: dict[str, Renderer] = {
    "static": _render_static,
    "blink": _render_blink,
    "breath": _render_breath,
    "chase": _render_chase,
    "comet": _render_comet,
    "pulse_seq": _render_pulse_seq,
    "alternate": _render_alternate,
    "wipe": _render_wipe,
    "rainbow": _render_rainbow,
}

VALID_EFFECTS = tuple(REGISTRY)


def resolve(effect: Any) -> EffectSpec:
    """把任意输入收敛到一个合法效果，未知值回退为常亮。"""
    return REGISTRY.get(str(effect), REGISTRY[DEFAULT_EFFECT])


def is_software(effect: Any) -> bool:
    return resolve(effect).kind == "software"


def normalize_params(effect: Any, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """按效果的参数表清洗输入，缺失和越界都收敛到合法值。"""
    spec = resolve(effect)
    incoming = params or {}
    values: dict[str, Any] = {}
    for item in spec.params:
        raw = incoming.get(item.key, item.default)
        if item.type == "enum":
            values[item.key] = str(raw) if str(raw) in item.choices else item.default
            continue
        try:
            number = float(raw)
        except (TypeError, ValueError):
            number = float(item.default)
        if not math.isfinite(number):
            number = float(item.default)
        if item.minimum is not None:
            number = max(item.minimum, number)
        if item.maximum is not None:
            number = min(item.maximum, number)
        values[item.key] = number
    return values


def render_frame(
    effect: Any,
    colors: list[str] | tuple[str, ...],
    phase: float,
    params: dict[str, Any] | None = None,
) -> list[Rgb]:
    """计算某个相位下三颗灯的 RGB。

    phase 取动画周期内的位置，超出 [0, 1) 会自动回绕，负数也可以。
    """
    spec = resolve(effect)
    values = normalize_params(spec.id, params)
    padded = list(colors) + ["000000"] * LED_COUNT
    rgb = [parse_color(item) for item in padded[:LED_COUNT]]
    try:
        wrapped = float(phase) % 1.0
    except (TypeError, ValueError):
        wrapped = 0.0
    if not math.isfinite(wrapped):
        wrapped = 0.0
    return _RENDERERS[spec.id](rgb, wrapped, values)


def registry_payload() -> list[dict[str, Any]]:
    """供 GET /v1/effects 使用；前端据此生成预览网格和编辑器表单。"""
    return [spec.to_dict() for spec in _SPECS]


def clamp_frame_rate(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return DEFAULT_FRAME_RATE
    return max(MIN_FRAME_RATE, min(MAX_FRAME_RATE, number))
