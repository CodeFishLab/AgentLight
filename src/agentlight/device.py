from __future__ import annotations

import queue
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterator

from . import effects
from .models import StateProfile
from .vendor import orvyn_device


# 软件动画期间把状态轮询放慢，避免 0x30 往返插进帧序列造成抖动
ANIMATED_POLL_SECONDS = 10.0
IDLE_POLL_SECONDS = 3.0
# 动画帧只等 0x11 的 ack，用比常规命令更短的轮询间隔
FRAME_POLL_INTERVAL = 0.001

MODE_TO_ID = {"static": 0, "blink": 1, "breath": 2}
SOUND_TO_ID = {"stop": 0, "beep": 1, "double-beep": 2, "success": 3, "failure": 4, "alarm": 5}
SOUND_STYLE_FACTORS = {"gentle": 0.6, "standard": 1.0, "prominent": 1.3}
SOUND_TONES: dict[str, list[tuple[int, int]]] = {
    "beep": [(1200, 120)],
    "double-beep": [(1200, 100), (0, 80), (1200, 100)],
    "success": [(900, 90), (1300, 130)],
    "failure": [(900, 140), (650, 180)],
    "alarm": [(1400, 150), (0, 100), (1400, 150)],
}


def _sound_notes(sound: str) -> list[tuple[int, int]]:
    if sound.startswith("custom:"):
        notes: list[tuple[int, int]] = []
        for part in sound.removeprefix("custom:").split(",")[:6]:
            frequency, duration = part.split(":", 1)
            notes.append((max(0, int(frequency)), max(1, int(duration))))
        return notes
    return list(SOUND_TONES.get(sound, []))


def styled_sound(sound: str, style: str) -> str:
    """Map a sound to rhythm/duration variants; this does not change buzzer amplitude."""
    if sound == "stop" or style == "standard":
        return sound
    factor = SOUND_STYLE_FACTORS.get(style, 1.0)
    notes = [(frequency, max(1, round(duration * factor))) for frequency, duration in _sound_notes(sound)]
    if not notes:
        return sound
    if style == "prominent" and len(notes) <= 3:
        separator = [(0, 80)] if len(notes) <= 2 else []
        notes = (notes + separator + notes)[:6]
    return "custom:" + ",".join(f"{frequency}:{duration}" for frequency, duration in notes[:6])


def parse_status(data: bytes) -> dict[str, Any]:
    if len(data) < 15:
        raise ValueError("device returned a short status packet")
    mode_names = {0: "static", 1: "blink", 2: "breath"}
    result: dict[str, Any] = {
        "mode": mode_names.get(data[0], f"unknown-{data[0]}"),
        "brightness": data[1],
        "colors": [f"{data[i]:02x}{data[i+1]:02x}{data[i+2]:02x}" for i in (2, 5, 8)],
        "buzzer_active": bool(data[11]),
        "sequence_active": bool(data[12]),
        "period_ms": data[13] | (data[14] << 8),
    }
    if len(data) >= 26:
        result.update({
            "base_colors": [f"{data[i]:02x}{data[i+1]:02x}{data[i+2]:02x}" for i in (15, 18, 21)],
            "base_brightness": data[24],
            "max_brightness": data[25],
        })
    if len(data) >= 32:
        result.update({
            "auto_sleep": bool(data[26]),
            "sleep_timeout": data[27] | (data[28] << 8),
            "sleeping": bool(data[29]),
            "keep_5v": bool(data[30]),
            "saved_5v": bool(data[31]),
        })
    if len(data) >= 38:
        result.update({
            "display_ready": bool(data[32]),
            "screen_rotation": data[33] | (data[34] << 8),
            "quota_5h": data[35],
            "quota_week": data[36],
        })
    return result


class OrvynController:
    def __init__(self, timeout_ms: int = 1500) -> None:
        self.timeout_ms = timeout_ms
        self._lock = threading.RLock()
        self._session: tuple[Any, Any] | None = None

    def begin_session(self) -> None:
        """打开一条常驻 HID 连接，供软件动画连续推帧复用。

        不开会话时每条命令都会重新开关设备，20fps 下根本撑不住。
        """
        with self._lock:
            if self._session is None:
                transport = orvyn_device.HidTransport(timeout_ms=self.timeout_ms)
                self._session = (transport, orvyn_device.RuntimeClient(transport))

    def end_session(self) -> None:
        with self._lock:
            session, self._session = self._session, None
        if session is None:
            return
        try:
            session[0].close()
        except OSError:
            pass

    @property
    def session_active(self) -> bool:
        return self._session is not None

    @contextmanager
    def session(self) -> Iterator[None]:
        self.begin_session()
        try:
            yield
        finally:
            self.end_session()

    def _call(self, callback: Callable[[Any], Any]) -> Any:
        with self._lock:
            if self._session is not None:
                try:
                    return callback(self._session[1])
                except Exception:
                    # 出错即丢弃会话，下一次调用退回开-用-关，保持拔插自愈
                    self.end_session()
                    raise
            transport = orvyn_device.HidTransport(timeout_ms=self.timeout_ms)
            client = orvyn_device.RuntimeClient(transport)
            try:
                return callback(client)
            finally:
                transport.close()

    def _wait(self, client: Any, command: int, poll_interval: float = 0.01) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout_ms / 1000
        while time.monotonic() < deadline:
            try:
                message = client.app_channel_read()
                if message["command"] == (command | 0x80):
                    return parse_status(message["data"])
            except orvyn_device.RuntimeStatusError as exc:
                if exc.status != 3:
                    raise
            time.sleep(poll_interval)
        raise TimeoutError(f"timed out waiting for Orvyn command 0x{command:02x}")

    def push_colors(self, frame: list[tuple[int, int, int]]) -> None:
        """下发一帧动画颜色（App 层 0x11）。

        仍然要消费 ack，否则响应会堆积在 HID 读队列里；但用比常规命令更短的轮询
        间隔，把每帧的固定开销压到最低。
        """
        payload = bytearray()
        for channel in list(frame)[:3]:
            payload.extend(bytes(max(0, min(255, int(value))) for value in channel[:3]))
        while len(payload) < 9:
            payload.append(0)

        def action(client: Any) -> None:
            client.app_channel_write(0x11, bytes(payload))
            self._wait(client, 0x11, poll_interval=FRAME_POLL_INTERVAL)

        self._call(action)

    def status(self) -> dict[str, Any]:
        def action(client: Any) -> dict[str, Any]:
            client.app_channel_write(0x30, b"")
            return self._wait(client, 0x30)
        return self._call(action)

    def apply_profile(self, profile: StateProfile, play_sound: bool = True, sound_style: str = "standard") -> dict[str, Any]:
        def action(client: Any) -> dict[str, Any]:
            color_payload = bytearray()
            for color in profile.colors:
                color_payload.extend(bytes.fromhex(color))
            client.app_channel_write(0x11, bytes(color_payload))
            self._wait(client, 0x11)
            mode_payload = bytes([
                MODE_TO_ID.get(profile.mode, 0),
                profile.period_ms & 0xFF,
                (profile.period_ms >> 8) & 0xFF,
                profile.brightness,
            ])
            client.app_channel_write(0x10, mode_payload)
            status = self._wait(client, 0x10)
            if play_sound:
                self._play_sound(client, profile.sound, sound_style)
            return status
        return self._call(action)

    def _play_sound(self, client: Any, sound: str, sound_style: str = "standard") -> None:
        sound = styled_sound(sound, sound_style)
        if sound.startswith("custom:"):
            notes = _sound_notes(sound)
            payload = bytearray([len(notes), 0])
            for frequency, duration in notes:
                payload.extend(frequency.to_bytes(4, "little"))
                payload.extend(duration.to_bytes(4, "little"))
            client.app_channel_write(0x21, bytes(payload))
            self._wait(client, 0x21)
            return
        sound_id = SOUND_TO_ID.get(sound, 0)
        client.app_channel_write(0x20, bytes([sound_id]))
        self._wait(client, 0x20)

    def play_sound(self, sound: str, sound_style: str = "standard") -> dict[str, Any]:
        def action(client: Any) -> dict[str, Any]:
            self._play_sound(client, sound, sound_style)
            client.app_channel_write(0x30, b"")
            return self._wait(client, 0x30)
        return self._call(action)

    def configure_led(self, base_colors: list[str], base_brightness: int, max_brightness: int) -> dict[str, Any]:
        if base_brightness > max_brightness:
            raise ValueError("base brightness cannot exceed maximum brightness")
        payload = bytearray()
        for color in base_colors[:3]:
            payload.extend(bytes.fromhex(color.lstrip("#")))
        payload.extend((base_brightness, max_brightness))
        return self._write_status_command(0x12, bytes(payload))

    def configure_sleep(self, enabled: bool, timeout_seconds: int, keep_5v: bool) -> dict[str, Any]:
        timeout_seconds = max(5, min(65535, int(timeout_seconds)))
        payload = bytes([int(enabled), timeout_seconds & 0xFF, (timeout_seconds >> 8) & 0xFF, int(keep_5v)])
        return self._write_status_command(0x13, payload)

    def sleep_now(self) -> dict[str, Any]:
        return self._write_status_command(0x14, b"")

    def set_screen_rotation(self, degrees: int) -> dict[str, Any]:
        if degrees not in {0, 90, 180, 270}:
            raise ValueError("rotation must be 0, 90, 180 or 270")
        return self._write_status_command(0x32, degrees.to_bytes(2, "little"))

    def set_quota(self, five_hour: int, week: int) -> dict[str, Any]:
        if any(value != 255 and not 0 <= value <= 100 for value in (five_hour, week)):
            raise ValueError("quota values must be between 0 and 100, or 255 for unknown")
        return self._write_status_command(0x31, bytes([five_hour, week]))

    def _write_status_command(self, command: int, payload: bytes) -> dict[str, Any]:
        def action(client: Any) -> dict[str, Any]:
            client.app_channel_write(command, payload)
            return self._wait(client, command)
        return self._call(action)


@dataclass(slots=True)
class HardwareSnapshot:
    connected: bool = False
    status: dict[str, Any] | None = None
    error: str = ""
    last_seen: float = 0.0
    applied_state: str = "off"
    last_interruption: dict[str, Any] | None = None
    animating: bool = False
    effect: str = "static"
    frame_rate: int = 0
    # 用户主动关闭了连接（休息），和「设备掉线」是两回事，界面要分开说
    detached: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class HardwareWorker:
    def __init__(self, force_reapply_seconds: int = 240, frame_rate: int | None = None) -> None:
        self.controller = OrvynController()
        self.force_reapply_seconds = max(10, int(force_reapply_seconds))
        self.frame_rate = effects.clamp_frame_rate(frame_rate)
        self._queue: queue.Queue[tuple[str, tuple[Any, ...]]] = queue.Queue()
        self._snapshot = HardwareSnapshot()
        self._snapshot_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="AgentLight-Hardware", daemon=True)
        self._desired: tuple[str, StateProfile, bool, str] | None = None
        self._desired_sleep: tuple[bool, int, bool] | None = None
        self._last_apply = 0.0
        self._animation: StateProfile | None = None
        self._animation_started = 0.0
        self._detached = False
        self._thread.start()

    @property
    def frame_interval(self) -> float:
        return 1.0 / self.frame_rate

    def set_frame_rate(self, frame_rate: int) -> None:
        self.frame_rate = effects.clamp_frame_rate(frame_rate)
        if self._animation is not None:
            self._set_snapshot(frame_rate=self.frame_rate)

    def apply(
        self,
        state: str,
        profile: StateProfile,
        muted: bool,
        force: bool = False,
        sound_style: str = "standard",
        play_sound: bool | None = None,
    ) -> None:
        """play_sound 为 None 时沿用默认规则（只在状态变化时响），
        显式传值可以强制响或强制不响——预览需要在同一状态下也发声。"""
        self._queue.put(("apply", (state, profile, muted, force, sound_style, play_sound)))

    def command(self, name: str, *args: Any) -> None:
        self._queue.put((name, args))

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(("stop", ()))
        self._thread.join(timeout=3)

    def snapshot(self) -> dict[str, Any]:
        with self._snapshot_lock:
            return self._snapshot.to_dict()

    def _set_snapshot(self, **values: Any) -> None:
        with self._snapshot_lock:
            for key, value in values.items():
                setattr(self._snapshot, key, value)

    def _run(self) -> None:
        next_poll = 0.0
        next_frame = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            deadline = next_poll if self._animation is None else min(next_poll, next_frame)
            timeout = max(0.001, min(1.0, deadline - now))
            try:
                name, args = self._queue.get(timeout=timeout)
                if name == "stop":
                    self._end_animation()
                    return
                self._execute(name, args)
            except queue.Empty:
                pass
            now = time.monotonic()
            if self._animation is not None and now >= next_frame:
                self._push_animation_frame()
                next_frame = time.monotonic() + self.frame_interval
            if now >= next_poll:
                self._poll()
                interval = ANIMATED_POLL_SECONDS if self._animation is not None else IDLE_POLL_SECONDS
                next_poll = time.monotonic() + interval

    def _sync_animation(self, profile: StateProfile | None) -> None:
        """按当前档案决定要不要跑软件动画。固件原生模式交给设备自己跑。"""
        if profile is not None and profile.is_software_effect:
            self._begin_animation(profile)
            return
        self._end_animation()
        self._set_snapshot(effect=profile.effect if profile is not None else "static")

    def _begin_animation(self, profile: StateProfile) -> None:
        restart = self._animation is None or self._animation.effect != profile.effect
        self._animation = profile
        if restart or self._animation_started == 0.0:
            self._animation_started = time.monotonic()
        self.controller.begin_session()
        self._set_snapshot(animating=True, effect=profile.effect, frame_rate=self.frame_rate)

    def _end_animation(self) -> None:
        if self._animation is None and not self.controller.session_active:
            return
        self._animation = None
        self._animation_started = 0.0
        self.controller.end_session()
        self._set_snapshot(animating=False, frame_rate=0)

    def _push_animation_frame(self) -> None:
        profile = self._animation
        if profile is None:
            return
        period = max(0.05, profile.period_ms / 1000)
        phase = (time.monotonic() - self._animation_started) / period
        try:
            self.controller.push_colors(profile.frame(phase))
        except Exception as exc:
            self._end_animation()
            self._mark_disconnected(exc)

    def _detach(self) -> None:
        """彻底断开与设备的往来，并让它睡下。

        和「暂停联动」的区别就在这里：暂停只是不再跟着状态改灯，USB 上照样在
        轮询；休息会结束常驻会话、发一次休眠指令，之后一条 HID 报文都不再发。
        """
        self._detached = True
        self._end_animation()
        error = ""
        try:
            self.controller.sleep_now()
        except Exception as exc:
            # 设备本来就不在，那「不再打扰它」这个目标已经达成了，不算失败
            error = str(exc)
        # 走的不是 _mark_disconnected：这是用户按的，不该记成一次掉线事故
        self._set_snapshot(connected=False, detached=True, animating=False, error=error)

    def _attach(self) -> None:
        """恢复连接，并把休息期间攒下的目标状态重新下发一次。"""
        self._detached = False
        self._last_apply = 0.0
        self._set_snapshot(detached=False, error="")
        if self._desired is None:
            return
        state, profile, muted, sound_style = self._desired
        # play_sound=False：恢复连接不是状态变化，不该响一声
        self._execute("apply", (state, profile, muted, True, sound_style, False))

    def _remember_while_detached(self, name: str, args: tuple[Any, ...]) -> None:
        """休息期间只记不发。

        目标状态照常更新，这样重新连上时能一次性还原到「本来应该是什么样」，
        而不是从熄灭重新爬一遍。
        """
        if name == "apply":
            state, profile, muted, _force, sound_style, _want_sound = args
            self._desired = (state, profile, muted, sound_style)
        elif name == "configure_sleep":
            self._desired_sleep = (bool(args[0]), int(args[1]), bool(args[2]))

    def _execute(self, name: str, args: tuple[Any, ...]) -> None:
        if name == "detach":
            self._detach()
            return
        if name == "attach":
            self._attach()
            return
        if self._detached:
            self._remember_while_detached(name, args)
            return
        try:
            if name == "apply":
                state, profile, muted, force, sound_style, want_sound = args
                previous = self._desired
                changed = previous is None or previous[0] != state
                # 状态没变但灯效换了（例如在配置页把常亮改成跑马灯）也要重新下发
                effect_changed = previous is not None and previous[1].effect != profile.effect
                self._desired = (state, profile, muted, sound_style)
                if changed or force or effect_changed or time.monotonic() - self._last_apply >= self.force_reapply_seconds:
                    self._end_animation()
                    status = self.controller.apply_profile(
                        profile,
                        play_sound=(changed if want_sound is None else want_sound) and not muted,
                        sound_style=sound_style,
                    )
                    self._last_apply = time.monotonic()
                    self._set_snapshot(connected=True, status=status, error="", last_seen=time.time(), applied_state=state)
                    self._sync_animation(profile)
            elif name == "configure_led":
                status = self.controller.configure_led(*args)
                self._set_snapshot(connected=True, status=status, error="", last_seen=time.time())
            elif name == "configure_sleep":
                self._desired_sleep = (bool(args[0]), int(args[1]), bool(args[2]))
                status = self.controller.configure_sleep(*args)
                self._set_snapshot(connected=True, status=status, error="", last_seen=time.time())
            elif name == "sleep_now":
                status = self.controller.sleep_now()
                self._set_snapshot(connected=True, status=status, error="", last_seen=time.time())
            elif name == "screen_rotation":
                status = self.controller.set_screen_rotation(*args)
                self._set_snapshot(connected=True, status=status, error="", last_seen=time.time())
            elif name == "quota":
                status = self.controller.set_quota(*args)
                self._set_snapshot(connected=True, status=status, error="", last_seen=time.time())
            elif name == "sound":
                status = self.controller.play_sound(*args)
                self._set_snapshot(connected=True, status=status, error="", last_seen=time.time())
        except Exception as exc:
            self._mark_disconnected(exc)

    def _mark_disconnected(self, exc: Exception) -> None:
        # 设备没了就别再推帧，同时丢弃常驻会话
        self._end_animation()
        with self._snapshot_lock:
            was_connected = self._snapshot.connected
        values: dict[str, Any] = {"connected": False, "error": str(exc)}
        if was_connected:
            values["last_interruption"] = {"code": "device_disconnected", "at": time.time(), "recovered": False}
        self._set_snapshot(**values)

    def _poll(self) -> None:
        if self._detached:
            # 休息期间连轮询都不做，否则「关闭连接」只是嘴上说说
            return
        try:
            status = self.controller.status()
            now = time.time()
            with self._snapshot_lock:
                last_interruption = self._snapshot.last_interruption
            values: dict[str, Any] = {"connected": True, "status": status, "error": "", "last_seen": now}
            if (
                last_interruption
                and last_interruption.get("code") == "device_disconnected"
                and not last_interruption.get("recovered")
            ):
                values["last_interruption"] = {**last_interruption, "recovered": True, "recovered_at": now}
            self._set_snapshot(**values)
            if self._desired_sleep:
                enabled, timeout_seconds, keep_5v = self._desired_sleep
                if (
                    bool(status.get("auto_sleep")) != enabled
                    or int(status.get("sleep_timeout", 0)) != timeout_seconds
                    or bool(status.get("keep_5v")) != keep_5v
                ):
                    status = self.controller.configure_sleep(enabled, timeout_seconds, keep_5v)
                    self._set_snapshot(status=status, last_seen=time.time())
            animating = self._animation is not None
            sleeping = bool(status.get("sleeping"))
            # 正在推帧就不需要周期性重发；但软件效果如果掉线停了，要重新起来
            overdue = not animating and time.monotonic() - self._last_apply >= self.force_reapply_seconds
            stalled = self._desired is not None and self._desired[1].is_software_effect and not animating
            if self._desired and (sleeping or overdue or stalled):
                state, profile, muted, sound_style = self._desired
                if sleeping:
                    self._set_snapshot(last_interruption={
                        "code": "device_auto_sleep",
                        "at": time.time(),
                        "state": state,
                        "recovered": False,
                    })
                self._end_animation()
                refreshed = self.controller.apply_profile(profile, play_sound=False, sound_style=sound_style)
                self._last_apply = time.monotonic()
                values = {"status": refreshed, "applied_state": state, "last_seen": time.time()}
                if sleeping:
                    values["last_interruption"] = {
                        "code": "device_auto_sleep",
                        "at": time.time(),
                        "state": state,
                        "recovered": True,
                    }
                self._set_snapshot(**values)
                self._sync_animation(profile)
        except Exception as exc:
            self._mark_disconnected(exc)
