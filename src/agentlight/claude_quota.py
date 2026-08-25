from __future__ import annotations

import copy
import json
import logging
import os
import random
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from .models import HIDDEN_QUOTA_WINDOW_KEYS


USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"

# 这里刻意没有 token 刷新接口。凭据是 Claude Code 的，refresh token 又是一次性的：
# 两个进程抢着轮换同一个，AgentLight 只要抢先成功一次，Claude Code 就可能被登出。
# 所以 AgentLight 只读不写——access token 过期就等 Claude Code 自己写回新的。

CACHE_VERSION = 1
DEFAULT_REFRESH_SECONDS = 300
MIN_REQUEST_INTERVAL_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 10
TOKEN_EXPIRY_SKEW_MS = 60_000
BACKOFF_SECONDS = (60, 120, 300, 600, 1200, 1800)
# 令牌过期时的兜底重试间隔。真正的唤醒信号是凭据文件变化，这只是防止 stamp
# 因为某些原因没变（比如原地覆盖且 mtime 精度不够）时彻底不动。
CREDENTIAL_WAIT_SECONDS = 120
# 等待凭据期间多久看一眼文件。只是一次 stat，可以密一些。
CREDENTIAL_POLL_SECONDS = 3.0

WINDOWS: dict[str, tuple[str, str, int | None]] = {
    "five_hour": ("5h", "5h", 300),
    "seven_day": ("Weekly", "7d", 10080),
    "seven_day_opus": ("Weekly Opus", "7d Opus", 10080),
    "seven_day_sonnet": ("Weekly Sonnet", "7d Sonnet", 10080),
    "seven_day_oauth_apps": ("Weekly OAuth", "7d OAuth", 10080),
}

# 定义在 models.py，statusline 那条链路也要用同一份
HIDDEN_WINDOW_KEYS = HIDDEN_QUOTA_WINDOW_KEYS


class ClaudeQuotaError(RuntimeError):
    pass


class ClaudeAuthRequired(ClaudeQuotaError):
    pass


class ClaudeRateLimited(ClaudeQuotaError):
    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("Claude quota request was rate limited")
        self.retry_after = retry_after


class ClaudeCredentialStale(ClaudeQuotaError):
    """磁盘上那份 access token 过期了，而我们不能替 Claude Code 去换新的。

    这不是「需要重新登录」——Claude Code 多半正常跑着，只是它刷新后的新令牌还
    留在内存里没写回文件。等它写回就好了，所以这是个等待态，不是错误态。
    """


class ClaudeOffline(ClaudeQuotaError):
    pass


class ClaudeServerError(ClaudeQuotaError):
    pass


@dataclass(frozen=True)
class ClaudeCredential:
    access_token: str
    refresh_token: str
    expires_at_ms: int | None
    refresh_token_expires_at_ms: int | None
    scopes: tuple[str, ...]

    def access_valid(self, now_ms: int | None = None) -> bool:
        if not self.access_token:
            return False
        if self.expires_at_ms is None:
            return True
        reference = now_ms if now_ms is not None else int(time.time() * 1000)
        return reference + TOKEN_EXPIRY_SKEW_MS < self.expires_at_ms

    def refresh_valid(self, now_ms: int | None = None) -> bool:
        if not self.refresh_token:
            return False
        if self.refresh_token_expires_at_ms is None:
            return True
        reference = now_ms if now_ms is not None else int(time.time() * 1000)
        return reference + TOKEN_EXPIRY_SKEW_MS < self.refresh_token_expires_at_ms


def default_credential_path() -> Path:
    return Path.home() / ".claude" / ".credentials.json"


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number > 0 else None


def _parse_credential_document(document: Any) -> ClaudeCredential:
    if not isinstance(document, dict):
        raise ClaudeAuthRequired("Claude credential file must contain a JSON object")
    oauth = document.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        raise ClaudeAuthRequired("Claude OAuth credential is missing")
    access = oauth.get("accessToken")
    refresh = oauth.get("refreshToken")
    scopes = oauth.get("scopes")
    return ClaudeCredential(
        access_token=access if isinstance(access, str) else "",
        refresh_token=refresh if isinstance(refresh, str) else "",
        expires_at_ms=_positive_int(oauth.get("expiresAt")),
        refresh_token_expires_at_ms=_positive_int(oauth.get("refreshTokenExpiresAt")),
        scopes=tuple(item for item in scopes if isinstance(item, str)) if isinstance(scopes, list) else (),
    )


def read_credential(path: Path) -> tuple[ClaudeCredential, dict[str, Any]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ClaudeAuthRequired("Claude authentication is required") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ClaudeAuthRequired("Claude credential file is unreadable") from exc
    return _parse_credential_document(document), document


def _parse_reset(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = int(value)
        return number if number > 0 else None
    if isinstance(value, str) and value:
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    return None


def _percent(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return max(0.0, min(100.0, number))


def parse_usage_response(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ClaudeQuotaError("Claude usage response must be a JSON object")
    windows: list[dict[str, Any]] = []
    for key, value in payload.items():
        if key == "model_scoped" or key in HIDDEN_WINDOW_KEYS or not isinstance(value, dict):
            continue
        used = _percent(value.get("utilization"))
        if used is None:
            used = _percent(value.get("used_percentage"))
        if used is None:
            continue
        label, short, minutes = WINDOWS.get(key, (key.replace("_", " ")[:32], key.replace("_", " ")[:32], None))
        windows.append({
            "key": key,
            "label": label,
            "short": short,
            "window_minutes": minutes,
            "used_percent": round(used, 1),
            "remaining_percent": max(0, min(100, round(100.0 - used))),
            "resets_at": _parse_reset(value.get("resets_at")),
        })
    if not windows:
        raise ClaudeQuotaError("Claude usage response did not contain quota windows")
    return windows


def _retry_after(headers: Any) -> float | None:
    raw = headers.get("Retry-After") if headers is not None else None
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        try:
            return max(0.0, parsedate_to_datetime(str(raw)).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return None


def _json_request(
    url: str,
    *,
    what: str = "endpoint",
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> tuple[int, Any, Any]:
    """`what` 只用来拼错误消息。

    配额接口和 token 刷新接口都会抛「HTTP xxx」，但两者的排查方向完全不同：
    前者是额度/权限问题，后者是登录凭据问题。不写清是哪个，日志等于没写。
    """
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    # 显式构造 ProxyHandler，确保打包后的进程也读取 HTTP(S)_PROXY / NO_PROXY，
    # 不依赖 urllib 的全局 opener 是否曾被其他模块替换。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(1024 * 1024)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ClaudeQuotaError(f"Claude {what} returned invalid JSON") from exc
            return int(response.status), payload, response.headers
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise ClaudeRateLimited(_retry_after(exc.headers)) from exc
        if exc.code == 401:
            raise ClaudeAuthRequired(f"Claude {what} rejected the OAuth access token") from exc
        if exc.code in (500, 502, 503, 504):
            raise ClaudeServerError(f"Claude {what} returned HTTP {exc.code}") from exc
        raise ClaudeQuotaError(f"Claude {what} returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError) as exc:
        raise ClaudeOffline(f"Claude {what} is unreachable") from exc


class ClaudeCredentialReader:
    """只读地取出 Claude Code 那份 access token。

    刻意不具备写能力：这个文件是 Claude Code 的状态，AgentLight 只是搭便车。
    """

    def __init__(self, credential_path: Path | None = None) -> None:
        self.credential_path = credential_path or default_credential_path()

    def stamp(self) -> tuple[int, int] | None:
        """凭据文件的 (mtime_ns, size)。Claude Code 一写回就会变，用来触发重试。"""
        try:
            info = self.credential_path.stat()
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size)

    def access_token(self) -> str:
        credential, _ = read_credential(self.credential_path)
        if not credential.access_token:
            raise ClaudeAuthRequired("Claude authentication is required")
        if not credential.access_valid():
            # 不去动 refresh token。Claude Code 写回新凭据后 stamp 会变，那时自然会重试。
            raise ClaudeCredentialStale("Claude Code 的登录令牌已过期，等它写回新凭据")
        return credential.access_token


class AnthropicUsageClient:
    def __init__(self, credentials: ClaudeCredentialReader | None = None) -> None:
        self.credentials = credentials or ClaudeCredentialReader()

    def stamp(self) -> tuple[int, int] | None:
        return self.credentials.stamp()

    def fetch(self) -> list[dict[str, Any]]:
        # 401 不再重试：能重试的唯一手段是刷新令牌，而那正是我们不做的事。
        # 交给 stamp 变化去驱动下一次尝试。
        return self._fetch_with_token(self.credentials.access_token())

    @staticmethod
    def _fetch_with_token(token: str) -> list[dict[str, Any]]:
        _, payload, _ = _json_request(
            USAGE_URL,
            what="usage endpoint",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "anthropic-beta": OAUTH_BETA,
            },
        )
        return parse_usage_response(payload)


def _empty_state() -> dict[str, Any]:
    return {
        "quota_windows": [],
        "quota_updated_at": None,
        "quota_source": "native_oauth",
        "status": "auth_required",
        "last_successful_refresh": None,
        "last_attempt": None,
        "next_retry_at": None,
        "error": "Claude authentication is required",
        "reason": "需要先通过 Claude Code 登录 Claude 账号",
        # 说明：AgentLight 只读取 Claude Code 的登录凭据，从不改写它
    }


class ClaudeQuotaProvider:
    def __init__(
        self,
        cache_path: Path,
        *,
        refresh_interval: int = DEFAULT_REFRESH_SECONDS,
        client: AnthropicUsageClient | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.cache_path = cache_path
        self.refresh_interval = self._normalize_interval(refresh_interval)
        self.client = client or AnthropicUsageClient()
        self.logger = logger or logging.getLogger(__name__)
        self._condition = threading.Condition(threading.RLock())
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._refresh_requested = False
        self._refreshing = False
        self._generation = 0
        self._last_network_monotonic = 0.0
        self._next_refresh_monotonic = 0.0
        # 429 冷却必须和「下次周期刷新」分开记。周期刷新关掉时后者是 inf，
        # 拿它当冷却判据的话，吃过一次 429 就再也手动刷不动了。
        self._rate_limited_until = 0.0
        self._backoff_index = 0
        self._credential_stamp = self._read_stamp()
        self._state = self._load_cache()

    def _read_stamp(self) -> tuple[int, int] | None:
        """凭据文件的指纹。客户端是替身时可能没有这个方法，缺了就当不支持。"""
        reader = getattr(self.client, "stamp", None)
        if reader is None:
            return None
        try:
            return reader()
        except Exception:
            return None

    def _credential_changed(self) -> bool:
        """Claude Code 有没有写回新凭据。

        这是等待态唯一的正经唤醒信号：我们不去刷新令牌，只能等它换。
        """
        stamp = self._read_stamp()
        if stamp is None or stamp == self._credential_stamp:
            return False
        self._credential_stamp = stamp
        return True

    @staticmethod
    def _normalize_interval(value: int) -> int:
        number = int(value)
        return 0 if number <= 0 else max(MIN_REQUEST_INTERVAL_SECONDS, number)

    def _load_cache(self) -> dict[str, Any]:
        state = _empty_state()
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return state
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            return state
        windows = data.get("quota_windows")
        updated = _positive_int(data.get("quota_updated_at"))
        if not isinstance(windows, list) or not windows or updated is None:
            return state
        visible_windows = [
            item for item in windows
            if isinstance(item, dict) and item.get("key") not in HIDDEN_WINDOW_KEYS
        ]
        if not visible_windows:
            return state
        state.update({
            "quota_windows": visible_windows,
            "quota_updated_at": updated,
            "status": "cached",
            "last_successful_refresh": updated,
            "error": "",
            "reason": "已加载上次成功获取的 Claude 配额，正在后台刷新",
        })
        return state

    def _write_cache(self, windows: list[dict[str, Any]], updated_at: int) -> None:
        data = {
            "version": CACHE_VERSION,
            "quota_windows": windows,
            "quota_updated_at": updated_at,
            "last_successful_refresh": updated_at,
        }
        rendered = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        temp = self.cache_path.with_name(f".{self.cache_path.name}.{os.getpid()}.{random.randrange(1 << 30):x}.tmp")
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temp.write_text(rendered, encoding="utf-8")
            os.replace(temp, self.cache_path)
        except OSError:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            self.logger.warning("Claude quota cache could not be updated")

    def configure(self, refresh_interval: int) -> None:
        with self._condition:
            self.refresh_interval = self._normalize_interval(refresh_interval)
            if self.refresh_interval == 0:
                self._next_refresh_monotonic = float("inf")
            else:
                self._next_refresh_monotonic = min(
                    self._next_refresh_monotonic,
                    time.monotonic() + self.refresh_interval,
                )
        self._wake.set()

    def start(self) -> None:
        with self._condition:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._refresh_requested = True
            self._thread = threading.Thread(target=self._run, name="AgentLight-ClaudeQuota", daemon=True)
            self._thread.start()
        self.logger.info("Claude quota provider initialized")
        if self._state.get("status") == "cached":
            self.logger.info("Loaded cached Claude quota")
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=REQUEST_TIMEOUT_SECONDS + 2)

    def request_refresh(self, *, wait: bool = False, timeout: float = REQUEST_TIMEOUT_SECONDS + 2) -> dict[str, Any]:
        with self._condition:
            thread_running = bool(self._thread and self._thread.is_alive())
            if not thread_running:
                return self.snapshot()
            now = time.monotonic()
            if self._refreshing:
                generation = self._generation
            elif now < self._rate_limited_until:
                return self.snapshot()
            elif self._last_network_monotonic and now - self._last_network_monotonic < MIN_REQUEST_INTERVAL_SECONDS:
                return self.snapshot()
            else:
                self._refresh_requested = True
                generation = self._generation
                self._wake.set()
            if wait:
                self._condition.wait_for(lambda: self._generation != generation or self._stop.is_set(), timeout=timeout)
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
            result = copy.deepcopy(self._state)
            if self._refreshing:
                result["status"] = "refreshing"
            updated = result.get("quota_updated_at")
            if isinstance(updated, int):
                result["quota_age_seconds"] = max(0, int(time.time() - updated))
            return result

    def _run(self) -> None:
        while not self._stop.is_set():
            now = time.monotonic()
            # 凭据一变就立刻重试，不等退避走完 —— 用户重启 Claude Code 之后
            # 还要再干等几分钟才恢复，那这个等待态就毫无意义了
            credential_changed = self._credential_changed()
            with self._condition:
                if credential_changed:
                    self._refresh_requested = True
                    self._backoff_index = 0
                    self._rate_limited_until = 0.0
                requested = self._refresh_requested
                due = self._next_refresh_monotonic <= now
                if requested or due:
                    self._refresh_requested = False
                    self._refreshing = True
                    self._state["last_attempt"] = int(time.time())
                else:
                    delay = max(0.1, self._next_refresh_monotonic - now)
                    self._wake.clear()
            if not requested and not due:
                # 等凭据的时候盯紧一点：stat 一次几乎不要钱，但能让「重启 Claude Code」
                # 之后几秒内就恢复，而不是干等一分钟
                ceiling = CREDENTIAL_POLL_SECONDS if self._state.get("status") == "credential_stale" else 60.0
                self._wake.wait(min(delay, ceiling))
                continue
            self._refresh_once()
            with self._condition:
                self._refreshing = False
                self._generation += 1
                self._condition.notify_all()

    def _refresh_once(self) -> None:
        self._last_network_monotonic = time.monotonic()
        self.logger.info("Claude quota refresh started")
        try:
            windows = self.client.fetch()
        except ClaudeRateLimited as exc:
            self._record_failure("rate_limited", "Claude quota request was rate limited", exc.retry_after)
            self.logger.warning("Claude quota HTTP 429")
        except ClaudeCredentialStale as exc:
            # 等 Claude Code 写回新凭据。这不是故障，别按错误退避越退越久 ——
            # 真正唤醒我们的是凭据文件的 stamp 变化。
            self._record_failure("credential_stale", str(exc), retry_after=CREDENTIAL_WAIT_SECONDS)
            self.logger.info("Claude 令牌已过期，等待 Claude Code 写回新凭据")
        except ClaudeAuthRequired:
            self._record_failure("auth_required", "Claude authentication is required")
            self.logger.warning("Claude authentication required")
        except ClaudeOffline:
            self._record_failure("offline", "Claude quota network is unavailable")
            self.logger.warning("Claude quota network unavailable")
        except ClaudeQuotaError as exc:
            # 本模块自己抛的异常，消息都是固定串或 HTTP 状态码，不含凭据，可以照实记。
            # 只写「refresh failed」等于没写——线上再出问题就只能靠猜。
            self._record_failure("error", f"Claude quota refresh failed: {exc}")
            self.logger.warning("Claude quota refresh failed: %s", exc)
        except (OSError, ValueError) as exc:
            # 外部异常的消息不受我们控制，只记类型
            self._record_failure("error", "Claude quota refresh failed")
            self.logger.warning("Claude quota refresh failed with %s", type(exc).__name__)
        except Exception as exc:  # Provider 必须隔离意外的客户端/schema 错误，且不能把异常对象写入日志
            self._record_failure("error", "Claude quota refresh failed")
            self.logger.error("Claude quota refresh failed with an unexpected %s", type(exc).__name__)
        else:
            updated_at = int(time.time())
            with self._condition:
                self._state = {
                    "quota_windows": windows,
                    "quota_updated_at": updated_at,
                    "quota_source": "native_oauth",
                    "status": "connected",
                    "last_successful_refresh": updated_at,
                    "last_attempt": updated_at,
                    "next_retry_at": None,
                    "error": "",
                    "reason": "Claude 配额由 AgentLight 直接从 Anthropic 获取",
                }
                self._backoff_index = 0
                self._rate_limited_until = 0.0
                self._next_refresh_monotonic = (
                    time.monotonic() + self.refresh_interval
                    if self.refresh_interval
                    else float("inf")
                )
            self._write_cache(windows, updated_at)
            self.logger.info("Claude quota refresh succeeded")

    def _record_failure(self, status: str, message: str, retry_after: float | None = None) -> None:
        if retry_after is None:
            delay = BACKOFF_SECONDS[min(self._backoff_index, len(BACKOFF_SECONDS) - 1)]
            self._backoff_index = min(self._backoff_index + 1, len(BACKOFF_SECONDS) - 1)
        else:
            delay = max(MIN_REQUEST_INTERVAL_SECONDS, retry_after)
        if status == "credential_stale":
            # 等待态不累积退避：凭据一旦写回就该立刻恢复，不该因为等久了反而更慢
            self._backoff_index = 0
        next_retry = int(time.time() + delay)
        with self._condition:
            if status == "rate_limited":
                self._rate_limited_until = time.monotonic() + delay
            self._state["status"] = status
            self._state["error"] = message
            self._state["reason"] = message
            self._state["next_retry_at"] = next_retry
            self._next_refresh_monotonic = (
                time.monotonic() + delay if self.refresh_interval else float("inf")
            )
