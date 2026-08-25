from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from .paths import ctl_executable, data_root


MARKER = "agentlightctl"
LEGACY_MARKER = "orvyn_codex_hook.py"
INTEGRATION_VERSION = 2
# 每类配置保留的自动备份份数
BACKUP_KEEP = 10
CODEX_EVENTS: dict[str, str | None] = {
    "SessionStart": None,
    "UserPromptSubmit": None,
    "PermissionRequest": None,
    "PreToolUse": None,
    "PostToolUse": None,
    "SubagentStart": None,
    "SubagentStop": None,
    "Stop": None,
    "SessionEnd": None,
}


class IntegrationError(RuntimeError):
    pass


def _contains_marker(value: Any, markers: tuple[str, ...]) -> bool:
    if isinstance(value, dict):
        return any(_contains_marker(item, markers) for item in value.values())
    if isinstance(value, list):
        return any(_contains_marker(item, markers) for item in value)
    text = str(value).lower()
    return any(marker.lower() in text for marker in markers)


def _contains_path(value: Any, path: Path) -> bool:
    if isinstance(value, dict):
        return any(_contains_path(item, path) for item in value.values())
    if isinstance(value, list):
        return any(_contains_path(item, path) for item in value)
    target = str(path).replace("/", "\\").casefold()
    candidate = str(value).replace("/", "\\").casefold()
    return target in candidate


def _owned_command(value: Any, include_legacy: bool = False) -> bool:
    if not isinstance(value, str):
        return False
    names = ["agentlightctl.exe", "agentlightctl-hook.ps1"]
    if include_legacy:
        names.append(LEGACY_MARKER)
    normalized = value.replace("/", "\\").casefold()
    return any(re.search(rf"(?:^|[\\\s\"]){re.escape(name)}(?=$|[\s\"])", normalized) for name in names)


class IntegrationsManager:
    def __init__(self, ctl_path: Path | None = None, home: Path | None = None) -> None:
        self.ctl_path = (ctl_path or ctl_executable()).resolve()
        self.codex_hook_path = self.ctl_path.with_name("agentlightctl-hook.ps1")
        self.home = home or Path.home()
        self.codex_path = self.home / ".codex" / "hooks.json"
        self.claude_path = self.home / ".claude" / "settings.json"
        self.backup_dir = data_root() / "backups"

    def _read(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise IntegrationError(f"无法解析配置文件 {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise IntegrationError(f"配置文件顶层必须是对象: {path}")
        return value

    def _auto_backups(self, label: str) -> list[Path]:
        """只认自动生成的那批（label + 时间戳）。

        目录里可能还躺着人手工命名的还原点，例如
        `codex-hooks-before-powershell-bridge-20260820.json`。那是有人特意留下的，
        清理绝不能按修改时间一刀切，否则正好把最该留的删掉。
        """
        pattern = re.compile(rf"^{re.escape(label)}-\d{{8}}-\d{{6}}-\d+\.json$")
        return sorted(
            (item for item in self.backup_dir.glob(f"{label}-*.json") if pattern.match(item.name)),
            key=lambda item: item.name,
        )

    def _prune_backups(self, label: str) -> None:
        """每类只留最近 BACKUP_KEEP 份。

        配置页有「打开备份目录」按钮，用户是真会去翻的。攒到几十个文件之后，
        这个目录就等于没有 —— 找不到东西的备份不叫备份。
        """
        existing = self._auto_backups(label)
        for stale in existing[: max(0, len(existing) - BACKUP_KEEP)]:
            try:
                stale.unlink()
            except OSError:
                pass  # 删不掉就算了，不能让清理把安装流程搞挂

    def _backup(self, path: Path, label: str) -> Path | None:
        if not path.exists():
            return None
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        content = path.read_bytes()
        newest = self._auto_backups(label)[-1:]
        # 连点几次「一键安装 / 修复」不该产生几份一模一样的文件
        if newest and newest[0].read_bytes() == content:
            return newest[0]
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        destination = self.backup_dir / f"{label}-{stamp}.json"
        shutil.copy2(path, destination)
        self._prune_backups(label)
        return destination

    def _write(self, path: Path, data: dict[str, Any], label: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        rendered = json.dumps(data, ensure_ascii=False, indent=2) + os.linesep
        if existing == rendered:
            return
        self._backup(path, label)
        temp = path.with_suffix(path.suffix + ".agentlight.tmp")
        temp.write_text(rendered, encoding="utf-8")
        temp.replace(path)

    @staticmethod
    def _clean_groups(groups: Any, include_legacy: bool) -> list[Any]:
        if not isinstance(groups, list):
            return []
        cleaned: list[Any] = []
        for group in groups:
            if not isinstance(group, dict):
                cleaned.append(group)
                continue
            hooks = group.get("hooks")
            if not isinstance(hooks, list):
                cleaned.append(group)
                continue
            remaining = [
                hook
                for hook in hooks
                if not (
                    isinstance(hook, dict)
                    and any(
                        _owned_command(hook.get(field), include_legacy)
                        for field in ("command", "commandWindows")
                    )
                )
            ]
            if not remaining:
                continue
            if len(remaining) == len(hooks):
                cleaned.append(group)
            else:
                cleaned.append({**group, "hooks": remaining})
        return cleaned

    def _codex_group(self, matcher: str | None = None) -> dict[str, Any]:
        command = (
            'powershell.exe -NoLogo -NoProfile -NonInteractive '
            f'-ExecutionPolicy Bypass -File "{self.codex_hook_path}" -Source codex'
        )
        group: dict[str, Any] = {
            "hooks": [{"type": "command", "command": command, "commandWindows": command, "timeout": 3}]
        }
        if matcher:
            group["matcher"] = matcher
        return group

    def install_codex(self) -> Path:
        data = self._read(self.codex_path)
        hooks = data.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise IntegrationError("Codex hooks 字段不是对象")
        for event, matcher in CODEX_EVENTS.items():
            groups = self._clean_groups(hooks.get(event), include_legacy=True)
            groups.append(self._codex_group(matcher))
            hooks[event] = groups
        # Codex only accepts documented top-level fields, so remove this marker.
        data.pop("x-agentlight-version", None)
        self._write(self.codex_path, data, "codex-hooks")
        return self.codex_path

    def _claude_group(self, matcher: str | None = None) -> dict[str, Any]:
        group: dict[str, Any] = {
            "hooks": [{"type": "command", "command": str(self.ctl_path), "args": ["hook", "--source", "claude"], "timeout": 3}]
        }
        if matcher:
            group["matcher"] = matcher
        return group

    def install_claude(self) -> Path:
        data = self._read(self.claude_path)
        hooks = data.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise IntegrationError("Claude hooks 字段不是对象")
        events: dict[str, str | None] = {
            "SessionStart": None,
            "UserPromptSubmit": None,
            "PermissionRequest": None,
            "Notification": "permission_prompt|idle_prompt|agent_needs_input|elicitation_dialog|elicitation_url_dialog",
            "PreToolUse": None,
            "PostToolUse": None,
            "SubagentStart": None,
            "SubagentStop": None,
            "Stop": None,
            "StopFailure": None,
            "SessionEnd": None,
        }
        for event, matcher in events.items():
            groups = self._clean_groups(hooks.get(event), include_legacy=True)
            groups.append(self._claude_group(matcher))
            hooks[event] = groups
        self.install_statusline(data)
        data["x-agentlight-version"] = INTEGRATION_VERSION
        self._write(self.claude_path, data, "claude-settings")
        return self.claude_path

    # ---- Claude 状态栏 ----------------------------------------------------
    # statusLine 只负责把 AgentLight 当前状态和 Claude Code 已提供的额度显示在
    # Claude Code 内部。AgentLight 自己的配额卡由原生 OAuth Provider 更新，
    # 不依赖这个槽位，也不依赖 Claude Code 正在运行。

    # 状态栏只在「有新消息 / compact 结束 / 权限模式变更」之后重跑，两条消息之间
    # 数据是冻住的。refreshInterval 让它按秒定时重跑，额度才能保鲜。
    STATUSLINE_REFRESH_SECONDS = 5

    def _statusline_entry(self) -> dict[str, Any]:
        return {
            "type": "command",
            "command": f'"{self.ctl_path}" statusline',
            "refreshInterval": self.STATUSLINE_REFRESH_SECONDS,
        }

    def _statusline_owner(self, data: dict[str, Any]) -> str:
        """槽位归谁：ours / free / foreign。"""
        entry = data.get("statusLine")
        if not entry:
            return "free"
        command = entry.get("command") if isinstance(entry, dict) else None
        return "ours" if _owned_command(command) else "foreign"

    def install_statusline(self, data: dict[str, Any]) -> str:
        """写入可选状态栏；槽位被别人占着就原样让开。

        settings.json 里 statusLine 只有一个槽位。覆盖掉用户自己配的状态栏
        是不可接受的，所以这里只在空着或本来就是我们的时候写。
        """
        owner = self._statusline_owner(data)
        if owner == "foreign":
            return "foreign"
        data["statusLine"] = self._statusline_entry()
        return "installed"

    def install_all(self) -> dict[str, str]:
        return {"codex": str(self.install_codex()), "claude": str(self.install_claude())}

    def _remove(self, path: Path, label: str) -> bool:
        if not path.exists():
            return False
        data = self._read(path)
        changed = False
        # 只收回自己那份状态栏，别人配的原样留下
        if self._statusline_owner(data) == "ours":
            del data["statusLine"]
            changed = True
        if "x-agentlight-version" in data:
            data.pop("x-agentlight-version")
            changed = True
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            if changed:
                self._write(path, data, label)
            return changed
        for event in list(hooks):
            original = hooks[event]
            if not isinstance(original, list):
                continue
            cleaned = self._clean_groups(original, include_legacy=False)
            if cleaned != original:
                changed = True
                if cleaned:
                    hooks[event] = cleaned
                else:
                    del hooks[event]
        if changed:
            self._write(path, data, label)
        return changed

    def remove_all(self) -> dict[str, bool]:
        return {
            "codex": self._remove(self.codex_path, "codex-hooks-remove"),
            "claude": self._remove(self.claude_path, "claude-settings-remove"),
        }

    def status(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, path in (("codex", self.codex_path), ("claude", self.claude_path)):
            try:
                data = self._read(path)
                hook_data = data.get("hooks", {})
                marker_present = _contains_marker(hook_data, (MARKER,))
                expected_path = self.codex_hook_path if name == "codex" else self.ctl_path
                path_matches = marker_present and _contains_path(hook_data, expected_path)
                legacy = _contains_marker(data.get("hooks", {}), (LEGACY_MARKER,))
                if name == "codex":
                    schema_valid = "x-agentlight-version" not in data
                    complete = isinstance(hook_data, dict) and all(
                        _contains_path(hook_data.get(event, []), self.codex_hook_path)
                        for event in CODEX_EVENTS
                    )
                    installed = path_matches and complete and schema_valid
                    upgrade_required = path_matches and not installed
                else:
                    version_matches = data.get("x-agentlight-version") == INTEGRATION_VERSION
                    installed = path_matches and version_matches
                    upgrade_required = marker_present and path_matches and not version_matches
                result[name] = {
                    "path": str(path),
                    "exists": path.exists(),
                    "installed": installed,
                    "stale": marker_present and not path_matches,
                    "upgrade_required": upgrade_required,
                    "ctl_path": str(self.ctl_path),
                    # Codex 审核的是 PowerShell 脚本，不是 exe。配置页要照实显示，
                    # 否则用户在 /hooks 里看到的命令跟页面对不上，会以为装错了。
                    "hook_path": str(expected_path),
                    "legacy": legacy,
                    "error": "",
                }
                if name == "claude":
                    # 槽位被别人占着不算安装失败，只是额度要另找来源
                    result[name]["statusline"] = self._statusline_owner(data)
            except IntegrationError as exc:
                result[name] = {
                    "path": str(path),
                    "exists": path.exists(),
                    "installed": False,
                    "stale": False,
                    "upgrade_required": False,
                    "ctl_path": str(self.ctl_path),
                    "legacy": False,
                    "error": str(exc),
                }
        return result
