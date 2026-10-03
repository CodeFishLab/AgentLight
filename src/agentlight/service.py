from __future__ import annotations

import copy
import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from . import effects
from .arbitration import StateArbitrator
from .claude_quota import ClaudeQuotaProvider
from .config import DEFAULT_CONFIG, ConfigManager
from .device import HardwareWorker
from .hotkey import normalize_combo, parse_combo
from .integrations import IntegrationsManager
from .logging_setup import configure_logging
from .models import EffectiveState, StateProfile, VALID_STATES
from .quota import latest_codex_quota, latest_codex_windows


PREVIEW_SECONDS = 5.0
QUOTA_CACHE_SECONDS = 60.0
# statusLine 每 5 秒推一次。Claude Code 一关就不再有推送，超过这个岁数就不能再当实时值。
STATUSLINE_FRESH_SECONDS = 90.0
# 只有这两个状态代表 OAuth 真的刚取到新数字；其余都该让位给 statusLine
CLAUDE_LIVE_STATUSES = frozenset({"connected", "refreshing"})
# 状态时间线保留的转换条数。实测最忙的半小时也只有 42 次变化（只在真的变了时才记），
# 300 条足够覆盖极端情况，一条约 30 字节。
TIMELINE_MAX_ENTRIES = 300
TIMELINE_WINDOW_SECONDS = 1800
VALID_SOUND_STYLES = {"gentle", "standard", "prominent"}
SESSION_STATE_VERSION = 1


def parse_clock(value: Any) -> int | None:
    """把 "HH:MM" 解析成「当天第几分钟」。解析不了返回 None。"""
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def within_window(now_minute: int, start: int, end: int) -> bool:
    """now 是否落在 [start, end) 里，支持跨午夜。

    夜间休息基本都是 23:00–07:00 这种跨午夜的写法，start < end 那套判断在这里
    是错的，必须分开处理。start == end 视为空窗口（而不是整天），否则「起止相同」
    会变成一个永远无法退出的状态。
    """
    if start == end:
        return False
    if start < end:
        return start <= now_minute < end
    return now_minute >= start or now_minute < end


class AgentLightService:
    def __init__(
        self,
        config: ConfigManager | None = None,
        claude_quota_provider: ClaudeQuotaProvider | None = None,
    ) -> None:
        self.config = config or ConfigManager()
        settings = self.config.snapshot()
        self.logger = configure_logging(self.config)
        self.claude_quota = claude_quota_provider or ClaudeQuotaProvider(
            self.config.path.parent / "claude-quota-cache.json",
            refresh_interval=int(settings.get("quota_refresh_interval_seconds", 300)),
            logger=self.logger,
        )
        self.arbitrator = StateArbitrator(
            settings["priorities"],
            settings["default_ttl_seconds"],
            settings["done_hold_seconds"],
            settings["stale_after_seconds"],
        )
        self.hardware = HardwareWorker(
            settings["force_reapply_seconds"],
            frame_rate=settings.get("device", {}).get("frame_rate"),
        )
        self.integrations = IntegrationsManager()
        self._session_state_path = self.config.path.parent / "state" / "sessions.json"
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._listeners_lock = threading.RLock()
        self._stop = threading.Event()
        self._paused = bool(settings.get("paused", False))
        self._muted = bool(settings.get("muted", False))
        self._resting = bool(settings.get("device_resting", False))
        # 上一次判定「是否在休息时段内」的结果。None 表示还没判过 —— 启动时
        # 会因此立刻评估一次，否则 23:30 才打开开关的话要干等到 07:00 才生效。
        self._in_rest_window: bool | None = None
        self._in_mute_window: bool | None = None
        # 全局快捷键由托盘线程注册，这里只持有注册函数和最近一次的结果。
        # 函数签名 (修饰位, 虚拟键码) -> 是否成功，(0, 0) 表示注销。
        self._hotkey_register: Callable[[int, int], bool] | None = None
        self._hotkey_error: str | None = None
        self._last_effective = EffectiveState("off")
        self._displayed_state = "off"
        self._runtime_sleep: tuple[bool, int, bool] | None = None
        self._preview_lock = threading.RLock()
        self._preview_timer: threading.Timer | None = None
        self._preview: dict[str, Any] | None = None
        self._preview_serial = 0
        self._next_quota_sync = 0.0
        self._last_quota: tuple[int, int] | None = None
        self._quota_cache: dict[str, Any] | None = None
        self._quota_cache_until = 0.0
        # Claude Code 的 statusLine 每隔几秒推来的活额度。桌面端登录时 OAuth 那条路
        # 拿不到凭据（令牌在应用自己的会话存储里），这是唯一还能拿到真实数字的来源。
        self._claude_statusline: dict[str, Any] | None = None
        self._statusline_lock = threading.RLock()
        # 时间线由服务端状态回调更新，不依赖配置页是否打开或停留在哪个页面。
        self._timeline: deque[tuple[int, str]] = deque(maxlen=TIMELINE_MAX_ENTRIES)
        self._timeline_lock = threading.RLock()
        self._last_effective = self._restore_session_state()
        self._timeline.append((int(time.time()), self._last_effective.state))
        self.arbitrator.subscribe(self._on_effective_state)
        self._apply_hardware(self._last_effective.state, force=True)
        self._push_device_config(settings)
        self._sync_runtime_sleep(self._last_effective.state, force=True)
        if self._resting:
            # 上次退出时设备在休息。亮度等参数上面已经下发过了，这里再断开，
            # 免得一次重启就把它叫醒——那样「休息」就等于没记住。
            self.hardware.command("detach")
        self._housekeeper = threading.Thread(target=self._housekeeping, name="AgentLight-Housekeeping", daemon=True)
        self._housekeeper.start()

    def start_background_services(self) -> None:
        """启动需要真实外部 I/O 的后台服务。

        与构造函数分开，避免测试或只读工具实例化 service 时意外访问 Claude 凭据。
        """
        self.claude_quota.start()

    def _restore_session_state(self) -> EffectiveState:
        try:
            data = json.loads(self._session_state_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != SESSION_STATE_VERSION:
                return self.arbitrator.restore_sessions([])
            effective = self.arbitrator.restore_sessions(data.get("sessions"))
            count = len(self.arbitrator.snapshot()["sessions"])
            if count:
                self.logger.info("已恢复 %s 个活动会话", count)
            return effective
        except FileNotFoundError:
            return self.arbitrator.restore_sessions([])
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            self.logger.warning("活动会话恢复失败，已忽略状态文件: %s", exc)
            return self.arbitrator.restore_sessions([])

    def _persist_session_state(self) -> None:
        try:
            payload = {
                "version": SESSION_STATE_VERSION,
                "sessions": self.arbitrator.snapshot()["sessions"],
            }
            self._session_state_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self._session_state_path.with_suffix(".tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temp.replace(self._session_state_path)
        except OSError as exc:
            self.logger.warning("活动会话保存失败: %s", exc)

    def subscribe(self, callback: Callable[[dict[str, Any]], None]) -> None:
        with self._listeners_lock:
            self._listeners.append(callback)

    def _notify(self) -> None:
        snapshot = self.snapshot(include_integrations=False)
        with self._listeners_lock:
            listeners = list(self._listeners)
        for callback in listeners:
            try:
                callback(snapshot)
            except Exception:
                self.logger.exception("状态监听器执行失败")

    def _dim_for_night(self, profile: StateProfile) -> StateProfile:
        """夜间模式按比例压低亮度，但不改各状态自己存的档案。"""
        night = self.config.get("night_mode", {})
        if not night.get("enabled"):
            return profile
        scale = max(5, min(100, int(night.get("scale", 35))))
        profile.brightness = max(0, round(profile.brightness * scale / 100))
        return profile

    def _profile(self, state: str) -> StateProfile:
        profiles = self.config.get("profiles", {})
        return self._dim_for_night(StateProfile.from_dict(profiles[state]))

    def set_night_mode(self, enabled: bool | None = None, scale: int | None = None) -> dict[str, Any]:
        night = dict(self.config.get("night_mode", {}))
        if enabled is not None:
            night["enabled"] = bool(enabled)
        if scale is not None:
            night["scale"] = max(5, min(100, int(scale)))
        self.config.update({"night_mode": night})
        self._cancel_preview()
        if not self._paused:
            self._apply_hardware(self.arbitrator.snapshot()["effective"]["state"], force=True, silent=True)
        self._notify()
        return night

    def reset_profiles(self) -> None:
        """把灯效档案恢复成出厂默认，其他设置不动。"""
        self.config.set_profiles(copy.deepcopy(DEFAULT_CONFIG["profiles"]))
        self._cancel_preview()
        if not self._paused:
            self._apply_hardware(self.arbitrator.snapshot()["effective"]["state"], force=True, silent=True)
        self.logger.info("灯效档案已恢复默认")
        self._notify()

    def _push_device_config(self, settings: dict[str, Any]) -> None:
        """启动时把设备亮度参数同步到硬件，确保配置与实际状态一致。"""
        device = settings.get("device", {})
        base = max(0, int(device.get("base_brightness", 0)))
        ceiling = max(base, int(device.get("max_brightness", 30)))
        self.hardware.command("configure_led", settings["profiles"]["off"]["colors"], base, ceiling)

    def _sound_style(self) -> str:
        style = str(self.config.get("device", {}).get("sound_style", "standard"))
        return style if style in VALID_SOUND_STYLES else "standard"

    def _apply_hardware(
        self,
        state: str,
        force: bool = False,
        silent: bool = False,
        profile: StateProfile | None = None,
        play_sound: bool | None = None,
    ) -> None:
        self.hardware.apply(
            state,
            profile or self._profile(state),
            self._muted or silent,
            force=force,
            sound_style=self._sound_style(),
            play_sound=play_sound,
        )
        self._displayed_state = state

    def _cancel_preview(self) -> dict[str, Any] | None:
        with self._preview_lock:
            preview = self._preview
            self._preview = None
            self._preview_serial += 1
            if self._preview_timer:
                self._preview_timer.cancel()
                self._preview_timer = None
            return preview

    def _finish_preview(self, serial: int) -> None:
        with self._preview_lock:
            if not self._preview or serial != self._preview_serial:
                return
            preview = self._preview
            self._preview = None
            self._preview_timer = None
        if self._paused:
            restore_state = str(preview["restore_state"])
        else:
            restore_state = str(self.arbitrator.snapshot()["effective"]["state"])
        self._apply_hardware(restore_state, force=True, silent=True)
        self.logger.info("灯光预览结束，恢复状态 %s", restore_state)
        self._notify()

    def _restore_after_cancelled_preview(self, preview: dict[str, Any] | None, effective: EffectiveState) -> None:
        if preview and not self._paused and self._displayed_state != effective.state:
            self._apply_hardware(effective.state, force=True, silent=True)

    def _sync_runtime_sleep(self, effective_state: str | None = None, force: bool = False) -> None:
        device = self.config.get("device", {})
        state = effective_state or self._last_effective.state
        configured = bool(device.get("auto_sleep", True))
        runtime_enabled = configured if self._paused or state == "off" else False
        desired = (runtime_enabled, int(device.get("sleep_timeout", 300)), bool(device.get("keep_5v", False)))
        if force or desired != self._runtime_sleep:
            self.hardware.command("configure_sleep", *desired)
            self._runtime_sleep = desired

    def _on_effective_state(self, effective: EffectiveState) -> None:
        self._cancel_preview()
        previous = self._last_effective
        self._last_effective = effective
        with self._timeline_lock:
            self._timeline.append((int(time.time()), effective.state))
        self.logger.info(
            "最终状态 %s -> %s source=%s session=%s manual=%s",
            previous.state,
            effective.state,
            effective.source,
            effective.session_id,
            effective.manual,
        )
        if not self._paused:
            self._apply_hardware(effective.state)
        self._sync_runtime_sleep(effective.state)
        if effective.state == "off":
            last_off = self.arbitrator.snapshot().get("last_off")
            if last_off:
                self.logger.info("熄灯原因 code=%s source=%s session=%s", last_off["code"], last_off["source"], last_off["session_id"])
        self._notify()

    def emit(self, source: str, session_id: str, state: str, ttl_seconds: int | None = None, event_name: str = "") -> dict[str, Any]:
        if state not in VALID_STATES:
            raise ValueError(f"无效状态: {state}")
        preview = self._cancel_preview()
        self.logger.info("事件 source=%s session=%s state=%s event=%s", source, session_id, state, event_name)
        reason = "session_end" if event_name == "SessionEnd" else "explicit_off"
        effective = self.arbitrator.emit(source, session_id, state, ttl_seconds, reason=reason)
        self._persist_session_state()
        self._restore_after_cancelled_preview(preview, effective)
        self._notify()
        return effective.to_dict()

    def end(self, source: str, session_id: str) -> dict[str, Any]:
        preview = self._cancel_preview()
        self.logger.info("会话结束 source=%s session=%s", source, session_id)
        effective = self.arbitrator.end(source, session_id)
        self._persist_session_state()
        self._restore_after_cancelled_preview(preview, effective)
        self._notify()
        return effective.to_dict()

    def set_manual(self, state: str | None, duration_seconds: int | None = None) -> dict[str, Any]:
        preview = self._cancel_preview()
        if state is not None and duration_seconds is None:
            # 手动状态不再无限期占用灯光，到点自动交还给自动联动
            duration_seconds = max(5, int(self.config.get("manual_timeout_seconds", 300)))
        effective = self.arbitrator.set_manual(state, duration_seconds)
        self._restore_after_cancelled_preview(preview, effective)
        self._notify()
        return effective.to_dict()

    def test_state(self, state: str) -> None:
        if state not in VALID_STATES:
            raise ValueError(f"无效状态: {state}")
        self.preview_state(state)

    def preview_state(self, state: str, profile: dict[str, Any] | None = None) -> None:
        """在设备上试一下某个状态。

        profile 传入时预览的是这份还没保存的草稿，否则用配置里已存的档案——
        否则「在设备上试 5 秒」只能试到上一次保存的样子，改了不保存就试不出来。
        """
        if state not in VALID_STATES:
            raise ValueError(f"无效状态: {state}")
        draft = None
        if profile:
            requested = profile.get("effect") or profile.get("mode")
            if requested and str(requested) not in effects.REGISTRY:
                raise ValueError(f"无效灯效: {requested}")
            draft = self._dim_for_night(
                StateProfile.from_dict({**self.config.get("profiles", {}).get(state, {}), **profile})
            )
        self._cancel_preview()
        with self._preview_lock:
            self._preview_serial += 1
            serial = self._preview_serial
            self._preview = {
                "active": True,
                "state": state,
                "restore_state": self._displayed_state,
                "until": time.time() + PREVIEW_SECONDS,
            }
            timer = threading.Timer(PREVIEW_SECONDS, self._finish_preview, args=(serial,))
            timer.daemon = True
            self._preview_timer = timer
            timer.start()
        # 预览连提示音一起放，这样「试 5 秒」就能完整听到这个状态的表现；
        # 设备蜂鸣静音仍然优先，_apply_hardware 里的 self._muted 会挡住
        self._apply_hardware(state, force=True, profile=draft, play_sound=True)
        self.logger.info(
            "预览状态 %s（%s），%.0f 秒后恢复",
            state,
            "草稿" if draft else "已保存档案",
            PREVIEW_SECONDS,
        )
        self._notify()

    def play_sound(self, sound: str) -> bool:
        if self._muted:
            return False
        self.hardware.command("sound", sound, self._sound_style())
        return True

    def set_paused(self, paused: bool) -> None:
        preview = self._cancel_preview()
        if preview and paused:
            self._apply_hardware(str(preview["restore_state"]), force=True, silent=True)
        self._paused = bool(paused)
        self.config.update({"paused": self._paused})
        if not self._paused:
            current = self.arbitrator.snapshot()["effective"]
            self._apply_hardware(current["state"], force=True)
        self._sync_runtime_sleep(force=True)
        self._notify()

    def set_device_resting(self, resting: bool, *, manual: bool = True) -> None:
        """关闭或恢复与设备的连接。

        和「暂停联动」是两回事：暂停只是不再跟着状态改灯，USB 上照样在轮询；
        休息会结束会话、让设备睡下，之后一条报文都不再发。想让设备彻底安静
        （比如夜里、或者拔了想省事）用这个。

        `manual=False` 是调度自己在调。手动在状态灯关闭时段内把设备开回来，会顺带把
        这个时段关掉 —— 否则下一秒就被调度按回去，或者留下一个界面上看不见的例外。
        """
        resting = bool(resting)
        if manual and not resting and self.inside_window("device_rest_schedule"):
            self.logger.info("状态灯关闭时段内手动恢复连接，同时关闭该时段")
            self.config.update({"device_rest_schedule": {**self.window_schedule("device_rest_schedule"), "enabled": False}})
            self._in_rest_window = None
        if resting == self._resting:
            return
        self._cancel_preview()
        self._resting = resting
        self.config.update({"device_resting": resting})
        if resting:
            self.hardware.command("detach")
        else:
            # 休息期间 hardware 一直在记「本来应该是什么样」，attach 会自己还原，
            # 这里不用再补一次 apply，否则同一帧会下发两遍
            self.hardware.command("attach")
            self._sync_runtime_sleep(force=True)
        self.logger.info("设备连接%s", "已关闭（休息）" if resting else "已恢复")
        self._notify()

    def rest_schedule(self) -> dict[str, Any]:
        raw = self.config.get("device_rest_schedule", {}) or {}
        return {
            "enabled": bool(raw.get("enabled", False)),
            "start": str(raw.get("start", "23:00")),
            "end": str(raw.get("end", "07:00")),
        }

    def window_schedule(self, key: str) -> dict[str, Any]:
        raw = self.config.get(key, {}) or {}
        default = DEFAULT_CONFIG[key]
        return {
            "enabled": bool(raw.get("enabled", False)),
            "start": str(raw.get("start", default["start"])),
            "end": str(raw.get("end", default["end"])),
        }

    def inside_window(self, key: str, now: time.struct_time | None = None) -> bool:
        """此刻是否落在某个时段内。时段没开或时间非法都算不在。"""
        schedule = self.window_schedule(key)
        if not schedule["enabled"]:
            return False
        start = parse_clock(schedule["start"])
        end = parse_clock(schedule["end"])
        if start is None or end is None or start == end:
            return False
        moment = now or time.localtime()
        return within_window(moment.tm_hour * 60 + moment.tm_min, start, end)

    def inside_rest_window(self, now: time.struct_time | None = None) -> bool:
        return self.inside_window("device_rest_schedule", now)

    def _run_window(
        self,
        key: str,
        state_attr: str,
        is_active: Callable[[], bool],
        apply: Callable[[bool], None],
        label: str,
        now: time.struct_time | None = None,
    ) -> None:
        """时段内持续保持开启，离开时收尾。状态灯关闭和静音共用这一套。

        早先只在跨越边界那一刻动手，本意是别跟用户较劲。结果是：开关亮着、时间
        也在区间内、状态却没生效 —— 界面完全看不出为什么，看起来就是坏了。
        所以改成持续对齐。想临时反悔就手动改，那会顺带把这个时段关掉（见各自的
        setter），把隐形的例外换成一个看得见的动作。
        """
        inside = self.inside_window(key, now)
        if inside:
            if not is_active():
                schedule = self.window_schedule(key)
                self.logger.info("%s时段 %s-%s 内，自动生效", label, schedule["start"], schedule["end"])
                apply(True)
        elif getattr(self, state_attr):
            # 只有「我们开的」才由我们收尾。启动时就在窗外的话这个标志是 None，
            # 不会误把用户自己设的状态改掉。
            self.logger.info("离开%s时段，自动恢复", label)
            apply(False)
        setattr(self, state_attr, inside)

    def _apply_schedules(self, now: time.struct_time | None = None) -> None:
        """由 housekeeping 每秒调一次，把两个时段都对齐一遍。"""
        self._run_window(
            "device_rest_schedule", "_in_rest_window",
            lambda: self._resting,
            lambda value: self.set_device_resting(value, manual=False),
            "状态灯关闭", now,
        )
        self._run_window(
            "mute_schedule", "_in_mute_window",
            lambda: self._muted,
            lambda value: self.set_muted(value, manual=False),
            "蜂鸣器静音", now,
        )

    def set_muted(self, muted: bool, *, manual: bool = True) -> None:
        """静音蜂鸣器。

        `manual=False` 是调度自己在调。手动在静音时段内取消静音，会顺带把这个
        时段关掉 —— 否则下一秒就被调度按回去，留下一个界面上看不见的例外。
        """
        muted = bool(muted)
        if manual and not muted and self.inside_window("mute_schedule"):
            self.logger.info("静音时段内手动取消静音，同时关闭静音时段")
            self.config.update({"mute_schedule": {**self.window_schedule("mute_schedule"), "enabled": False}})
            self._in_mute_window = None
        self._muted = muted
        self.config.update({"muted": self._muted})
        if self._muted:
            self.hardware.command("sound", "stop", "standard")
        self._notify()

    def save_profiles(self, profiles: dict[str, Any]) -> None:
        stored = self.config.get("profiles", {})
        normalized: dict[str, Any] = {}
        for state, value in profiles.items():
            if state not in VALID_STATES:
                raise ValueError(f"无效状态: {state}")
            requested = value.get("effect") or value.get("mode")
            if requested and str(requested) not in effects.REGISTRY:
                raise ValueError(f"无效灯效: {requested}")
            merged = {**stored.get(state, {}), **value}
            # 兼容只提供 mode 的调用方，并以显式传入的 mode 为准。
            if "mode" in value and "effect" not in value:
                merged["effect"] = value["mode"]
            # 存回规范化后的结果：软件效果必须把 mode 落成 static，否则配置自相矛盾
            normalized[state] = StateProfile.from_dict(merged).to_dict()
        self.config.set_profiles(normalized)
        self._cancel_preview()
        current = self.arbitrator.snapshot()["effective"]["state"]
        if not self._paused:
            self._apply_hardware(current, force=True, silent=True)
        self._notify()

    @staticmethod
    def _clean_window_schedule(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("时段设置必须是对象")
        start, end = parse_clock(raw.get("start")), parse_clock(raw.get("end"))
        if start is None or end is None:
            raise ValueError("时段的起止时间必须是 HH:MM")
        if start == end and bool(raw.get("enabled")):
            raise ValueError("时段的起止时间不能相同")
        return {
            "enabled": bool(raw.get("enabled", False)),
            "start": f"{start // 60:02d}:{start % 60:02d}",
            "end": f"{end // 60:02d}:{end % 60:02d}",
        }

    def update_runtime_settings(self, values: dict[str, Any]) -> None:
        self._cancel_preview()
        normalized = copy.deepcopy(values)
        if "quota_refresh_interval_seconds" in normalized:
            raw_interval = normalized["quota_refresh_interval_seconds"]
            if isinstance(raw_interval, bool):
                raise ValueError("配额自动刷新间隔必须是 0 到 86400 之间的整数")
            try:
                interval = int(raw_interval)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("配额自动刷新间隔必须是 0 到 86400 之间的整数") from exc
            if interval != raw_interval or not 0 <= interval <= 86400:
                raise ValueError("配额自动刷新间隔必须是 0 到 86400 之间的整数")
            normalized["quota_refresh_interval_seconds"] = interval
        for key in ("device_rest_schedule", "mute_schedule"):
            if key in normalized:
                normalized[key] = self._clean_window_schedule(normalized[key])
        if "hotkey" in normalized:
            normalized["hotkey"] = self._clean_hotkey(normalized["hotkey"])
        if "hotkey" in normalized and normalized["hotkey"] != self.config.get("hotkey"):
            # 没改就不重新注册：否则启动时就被占用的快捷键会让每次保存都失败。
            # 先注册再落盘：组合键被别的程序占着就整次保存失败，旧快捷键继续可用，
            # 而不是存进一个按了没反应的值
            if self._hotkey_register is not None:
                error = self._bind_hotkey(normalized["hotkey"])
                if error:
                    self._hotkey_error = self._bind_hotkey(self.config.get("hotkey", {}))
                    raise ValueError(error)
                self._hotkey_error = None
        updated = self.config.update(normalized)
        self._paused = bool(updated.get("paused", False))
        was_muted = self._muted
        self._muted = bool(updated.get("muted", False))
        if self._muted and not was_muted:
            self.hardware.command("sound", "stop", "standard")
        self.arbitrator.configure(
            updated["priorities"],
            updated["default_ttl_seconds"],
            updated["done_hold_seconds"],
            updated["stale_after_seconds"],
        )
        self.hardware.force_reapply_seconds = int(updated["force_reapply_seconds"])
        if not self._paused:
            current = self.arbitrator.snapshot()["effective"]["state"]
            self._apply_hardware(current, force=True, silent=True)
        self._sync_runtime_sleep(force=True)
        # 改完设置立刻重判一次：23:30 打开一个 23:00 开始的窗口，应当马上生效
        self._in_rest_window = None
        self._in_mute_window = None
        self._apply_schedules()
        interval = int(updated.get("quota_refresh_interval_seconds", 300))
        self.claude_quota.configure(interval)
        self._notify()

    @staticmethod
    def _clean_hotkey(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("快捷键设置必须是对象")
        return {"enabled": bool(raw.get("enabled")), "combo": normalize_combo(raw.get("combo"))}

    def attach_hotkey(self, register: Callable[[int, int], bool]) -> None:
        """接上托盘的注册函数，并按当前配置注册一次。"""
        self._hotkey_register = register
        self._hotkey_error = self._bind_hotkey(self.config.get("hotkey", {}))
        if self._hotkey_error:
            self.logger.warning("全局快捷键未生效：%s", self._hotkey_error)
        self._notify()

    def _bind_hotkey(self, settings: dict[str, Any]) -> str | None:
        """按设置注册或注销快捷键，返回失败原因；成功或已关闭时返回 None。"""
        register = self._hotkey_register
        if register is None:
            return None
        if not settings.get("enabled"):
            register(0, 0)
            return None
        try:
            modifiers, key, name = parse_combo(settings.get("combo", ""))
        except ValueError as exc:
            register(0, 0)
            return str(exc)
        if register(modifiers, key):
            return None
        return f"{name} 注册失败，可能已被其他程序占用，请换一个组合"

    def configure_device(self, device: dict[str, Any]) -> None:
        current = self.config.get("device", {})
        merged = {**current, **device}
        sound_style = str(merged.get("sound_style", "standard"))
        if sound_style not in VALID_SOUND_STYLES:
            raise ValueError("无效提示音风格")
        merged["frame_rate"] = effects.clamp_frame_rate(merged.get("frame_rate"))
        self.config.update({"device": merged})
        self.hardware.set_frame_rate(merged["frame_rate"])
        base_colors = self.config.get("profiles")["off"]["colors"]
        self.hardware.command("configure_led", base_colors, int(merged["base_brightness"]), int(merged["max_brightness"]))
        self._sync_runtime_sleep(force=True)
        self.hardware.command("screen_rotation", int(merged["screen_rotation"]))
        self._next_quota_sync = 0.0
        self._notify()

    def sleep_now(self) -> bool:
        state = self.arbitrator.snapshot()["effective"]["state"]
        if state != "off" and not self._paused:
            return False
        self.hardware.command("sleep_now")
        return True

    def timeline(self, window_seconds: int = TIMELINE_WINDOW_SECONDS) -> dict[str, Any]:
        """最近一段时间的状态转换。

        除了窗口内的条目，还要带上窗口**之前**最后一条 —— 否则「灯已经绿了两小时」
        这种情况会返回空列表，前端就只能画一片空白，或者瞎猜。那一条是基线。
        """
        cutoff = time.time() - max(1, int(window_seconds))
        with self._timeline_lock:
            entries = list(self._timeline)
        inside = [item for item in entries if item[0] >= cutoff]
        baseline = [item for item in entries if item[0] < cutoff][-1:]
        return {
            "window_seconds": int(window_seconds),
            "entries": [{"at": at, "state": state} for at, state in baseline + inside],
        }

    def record_claude_statusline(self, windows: list[dict[str, Any]]) -> None:
        """收下 statusLine 解析出来的窗口。

        只当补充来源：OAuth 能取到就以 OAuth 为准，这里是它取不到时的兜底。
        """
        with self._statusline_lock:
            if not windows:
                self._claude_statusline = None
                return
            self._claude_statusline = {"windows": windows, "at": time.time()}

    def _statusline_quota(self) -> dict[str, Any] | None:
        """还新鲜的 statusLine 数据。

        Claude Code 关掉之后就不再推送，过期的必须丢掉 —— 拿一个昨天的数字冒充
        实时值，和之前那个绿环撒谎是一回事。
        """
        with self._statusline_lock:
            entry = self._claude_statusline
            if not entry or time.time() - entry["at"] > STATUSLINE_FRESH_SECONDS:
                return None
            return copy.deepcopy(entry)

    def _merge_claude_quota(self, oauth: dict[str, Any]) -> dict[str, Any]:
        """OAuth 优先，statusLine 兜底。

        OAuth 是权威来源，而且 Claude Code 没开着也能取；只有它确实拿不到新数字时，
        才用 Claude Code 正在推送的活数据顶上，而不是直接退回几小时前的缓存。
        """
        if oauth.get("status") in CLAUDE_LIVE_STATUSES:
            return oauth
        fresh = self._statusline_quota()
        if not fresh:
            return oauth
        updated = int(fresh["at"])
        return {
            **oauth,
            "quota_windows": fresh["windows"],
            "quota_updated_at": updated,
            "quota_age_seconds": max(0, int(time.time() - updated)),
            "quota_source": "claude_code_statusline",
            "status": "statusline",
            "error": "",
            "reason": "取自正在运行的 Claude Code（OAuth 凭据不可用）",
        }

    def quota_snapshot(self, refresh: bool = False, source: str = "all") -> dict[str, Any]:
        """读取各 Agent 的配额。

        与「推送到设备屏幕」解耦：没有屏幕的设备同样应该能在配置页看到数字。
        结果缓存 60 秒，因为读取要倒序扫会话文件。
        """
        if source not in ("all", "codex", "claude"):
            raise ValueError("无效配额来源")
        now = time.monotonic()
        if refresh and source in ("all", "claude"):
            claude = self.claude_quota.request_refresh(wait=True)
        else:
            claude = self.claude_quota.snapshot()
        refresh_codex = refresh and source in ("all", "codex")
        claude = self._merge_claude_quota(claude)
        if not refresh_codex and self._quota_cache and now < self._quota_cache_until:
            return {**self._quota_cache, "claude": claude}
        codex = self._codex_quota()
        result = {"codex": codex, "claude": claude}
        self._quota_cache = result
        self._quota_cache_until = now + QUOTA_CACHE_SECONDS
        return result

    @staticmethod
    def _codex_quota() -> dict[str, Any]:
        try:
            windows = latest_codex_windows()
            five_hour, week = latest_codex_quota()
            return {"available": True, "windows": windows, "five_hour": five_hour, "week": week, "error": ""}
        except Exception as exc:
            return {"available": False, "windows": [], "five_hour": None, "week": None, "error": str(exc)}

    def sync_codex_quota(self) -> dict[str, Any]:
        # 设备屏幕只接收 Codex 两格，不能因此额外触发 Claude 网络请求。
        codex = self._codex_quota()
        if not codex["available"]:
            raise RuntimeError(codex["error"] or "未找到 Codex 配额快照")
        five_hour, week = codex["five_hour"], codex["week"]
        hardware = self.hardware.snapshot()
        status = hardware.get("status") or {}
        pushed = bool(hardware.get("connected") and status.get("display_ready"))
        if pushed:
            self.hardware.command("quota", five_hour, week)
            self._last_quota = (five_hour, week)
        interval = max(30, int(self.config.get("device", {}).get("quota_sync_interval", 300)))
        self._next_quota_sync = time.monotonic() + interval
        self.logger.info("Codex 配额 5h=%s week=%s（%s）", five_hour, week, "已推送到屏幕" if pushed else "设备无屏幕，仅在配置页显示")
        return {"five_hour": five_hour, "week": week, "pushed_to_screen": pushed}

    def _maybe_sync_codex_quota(self) -> None:
        now = time.monotonic()
        if now < self._next_quota_sync:
            return
        settings = self.config.get("device", {})
        if not settings.get("codex_quota_sync", True):
            self._next_quota_sync = now + 30
            return
        hardware = self.hardware.snapshot()
        status = hardware.get("status") or {}
        if not hardware.get("connected") or not status.get("display_ready"):
            self._next_quota_sync = now + 10
            return
        try:
            self.sync_codex_quota()
        except (OSError, RuntimeError, ValueError) as exc:
            self._next_quota_sync = now + 60
            self.logger.warning("Codex 配额同步暂不可用: %s", exc)

    def snapshot(self, include_integrations: bool = True) -> dict[str, Any]:
        config = self.config.snapshot()
        config["api"] = {**config["api"], "token": "***"}
        result = {
            "version": 1,
            "paused": self._paused,
            "muted": self._muted,
            "device_resting": self._resting,
            "state": self.arbitrator.snapshot(),
            "device": self.hardware.snapshot(),
            "config": config,
            "preview": self.preview_snapshot(),
            "hotkey": {"error": self._hotkey_error},
            "runtime_sleep_suppressed": bool(self._runtime_sleep and not self._runtime_sleep[0] and config["device"].get("auto_sleep")),
        }
        if include_integrations:
            result["integrations"] = self.integrations.status()
        return result

    def preview_snapshot(self) -> dict[str, Any]:
        with self._preview_lock:
            if not self._preview:
                return {"active": False, "state": None, "until": None}
            return {key: value for key, value in self._preview.items() if key != "restore_state"}

    def _housekeeping(self) -> None:
        while not self._stop.wait(1):
            before = self.arbitrator.snapshot()["effective"]
            after = self.arbitrator.expire().to_dict()
            if before != after:
                self._notify()
            self._maybe_sync_codex_quota()
            self._apply_schedules()

    def shutdown(self) -> None:
        self._cancel_preview()
        self._persist_session_state()
        self.claude_quota.stop()
        self._stop.set()
        self._housekeeper.join(timeout=2)
        self.hardware.stop()
        self.logger.info("Agent Light 服务已停止")
