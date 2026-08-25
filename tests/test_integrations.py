from __future__ import annotations

import json

from agentlight.integrations import IntegrationsManager


def count_marker(value, marker: str) -> int:
    if isinstance(value, dict):
        return sum(count_marker(item, marker) for item in value.values())
    if isinstance(value, list):
        return sum(count_marker(item, marker) for item in value)
    return int(marker.lower() in str(value).lower())


def test_install_is_idempotent_and_preserves_other_hooks(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    monkeypatch.setenv("AGENTLIGHT_DATA_DIR", str(data))
    codex = home / ".codex" / "hooks.json"
    codex.parent.mkdir(parents=True)
    codex.write_text(json.dumps({
        "description": "keep me",
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "other-tool.exe"}]},
                {"hooks": [{"type": "command", "command": "py C:/legacy/orvyn_codex_hook.py"}]},
            ]
        },
    }), encoding="utf-8")
    manager = IntegrationsManager(tmp_path / "agentlightctl.exe", home)
    manager.install_codex()
    manager.install_codex()
    installed = json.loads(codex.read_text(encoding="utf-8"))
    assert installed["description"] == "keep me"
    assert count_marker(installed, "other-tool.exe") == 1
    assert count_marker(installed, "orvyn_codex_hook.py") == 0
    assert count_marker(installed, "agentlightctl") > 0
    assert "x-agentlight-version" not in installed
    command = installed["hooks"]["SessionStart"][-1]["hooks"][0]["commandWindows"]
    assert "powershell.exe" in command
    assert "agentlightctl-hook.ps1" in command
    assert str(tmp_path / "agentlightctl-hook.ps1") in command
    assert "matcher" not in installed["hooks"]["PreToolUse"][-1]
    assert "matcher" not in installed["hooks"]["PostToolUse"][-1]
    assert len(list((data / "backups").glob("codex-hooks-*.json"))) >= 1


def test_status_requests_repair_for_invalid_codex_metadata(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("AGENTLIGHT_DATA_DIR", str(tmp_path / "data"))
    manager = IntegrationsManager(tmp_path / "agentlightctl.exe", home)
    manager.install_all()
    codex = json.loads(manager.codex_path.read_text(encoding="utf-8"))
    codex["x-agentlight-version"] = 1
    manager.codex_path.write_text(json.dumps(codex), encoding="utf-8")

    status = manager.status()["codex"]
    assert status["installed"] is False
    assert status["upgrade_required"] is True

    manager.install_codex()
    repaired = json.loads(manager.codex_path.read_text(encoding="utf-8"))
    assert "x-agentlight-version" not in repaired
    assert manager.status()["codex"]["installed"] is True


def test_status_requests_repair_when_codex_event_is_missing(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("AGENTLIGHT_DATA_DIR", str(tmp_path / "data"))
    manager = IntegrationsManager(tmp_path / "agentlightctl.exe", home)
    manager.install_codex()
    codex = json.loads(manager.codex_path.read_text(encoding="utf-8"))
    del codex["hooks"]["SubagentStop"]
    manager.codex_path.write_text(json.dumps(codex), encoding="utf-8")

    status = manager.status()["codex"]
    assert status["installed"] is False
    assert status["upgrade_required"] is True


def test_remove_only_owned_entries(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("AGENTLIGHT_DATA_DIR", str(tmp_path / "data"))
    manager = IntegrationsManager(tmp_path / "agentlightctl.exe", home)
    manager.install_all()
    codex = json.loads(manager.codex_path.read_text(encoding="utf-8"))
    codex["hooks"]["SessionStart"].append({"hooks": [{"type": "command", "command": "keep-this.exe"}]})
    manager.codex_path.write_text(json.dumps(codex), encoding="utf-8")
    result = manager.remove_all()
    assert result["codex"] is True
    remaining = json.loads(manager.codex_path.read_text(encoding="utf-8"))
    assert count_marker(remaining, "agentlightctl") == 0
    assert count_marker(remaining, "keep-this.exe") == 1


def test_remove_preserves_foreign_hooks_in_the_same_group(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("AGENTLIGHT_DATA_DIR", str(tmp_path / "data"))
    manager = IntegrationsManager(tmp_path / "agentlightctl.exe", home)
    manager.install_codex()
    codex = json.loads(manager.codex_path.read_text(encoding="utf-8"))
    group = codex["hooks"]["SessionStart"][0]
    group["hooks"].append({"type": "command", "command": "keep-this.exe"})
    manager.codex_path.write_text(json.dumps(codex), encoding="utf-8")

    manager.remove_all()

    remaining = json.loads(manager.codex_path.read_text(encoding="utf-8"))
    assert count_marker(remaining, "agentlightctl") == 0
    assert count_marker(remaining, "keep-this.exe") == 1


def test_remove_does_not_treat_foreign_metadata_as_an_owned_hook(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("AGENTLIGHT_DATA_DIR", str(tmp_path / "data"))
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "hooks": {
            "Stop": [{
                "description": "agentlightctl is mentioned in documentation only",
                "hooks": [{"type": "command", "command": "agentlightctl-wrapper.exe"}],
            }]
        }
    }), encoding="utf-8")
    manager = IntegrationsManager(tmp_path / "agentlightctl.exe", home)

    assert manager.remove_all()["claude"] is False
    assert count_marker(json.loads(path.read_text(encoding="utf-8")), "agentlightctl-wrapper.exe") == 1


def test_status_detects_stale_install_path_and_repair(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("AGENTLIGHT_DATA_DIR", str(tmp_path / "data"))
    old_manager = IntegrationsManager(tmp_path / "old" / "agentlightctl.exe", home)
    old_manager.install_all()

    new_path = tmp_path / "custom-install" / "agentlightctl.exe"
    new_manager = IntegrationsManager(new_path, home)
    before = new_manager.status()
    assert before["codex"]["installed"] is False
    assert before["codex"]["stale"] is True
    assert before["claude"]["stale"] is True

    new_manager.install_all()
    after = new_manager.status()
    assert after["codex"]["installed"] is True
    assert after["codex"]["stale"] is False
    assert after["claude"]["installed"] is True


# ---- Claude 状态栏槽位 ----------------------------------------------------
# settings.json 里 statusLine 只有一个位置，抢别人的等于毁掉用户的状态栏。


def _claude_settings(tmp_path, monkeypatch, existing=None):
    home = tmp_path / "home"
    monkeypatch.setenv("AGENTLIGHT_DATA_DIR", str(tmp_path / "data"))
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(existing or {}), encoding="utf-8")
    return IntegrationsManager(tmp_path / "agentlightctl.exe", home), path


def test_install_claims_an_empty_statusline_slot(tmp_path, monkeypatch) -> None:
    manager, path = _claude_settings(tmp_path, monkeypatch)

    manager.install_claude()

    entry = json.loads(path.read_text(encoding="utf-8"))["statusLine"]
    assert entry["type"] == "command"
    assert entry["command"].endswith('agentlightctl.exe" statusline')
    # 状态栏只在有新消息后重跑，没有这个定时刷新，额度会一直停在上一条消息时的值
    assert entry["refreshInterval"] == manager.STATUSLINE_REFRESH_SECONDS


def test_install_never_overwrites_someone_elses_statusline(tmp_path, monkeypatch) -> None:
    theirs = {"type": "command", "command": "custom-statusline", "refreshInterval": 1}
    manager, path = _claude_settings(tmp_path, monkeypatch, {"statusLine": theirs})

    manager.install_claude()

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["statusLine"] == theirs
    # hooks 照装不误，只是额度得另找来源
    assert count_marker(data["hooks"], "agentlightctl") > 0
    assert manager.status()["claude"]["statusline"] == "foreign"


def test_install_is_idempotent_on_the_statusline_slot(tmp_path, monkeypatch) -> None:
    manager, path = _claude_settings(tmp_path, monkeypatch)

    manager.install_claude()
    first = path.read_text(encoding="utf-8")
    manager.install_claude()

    assert path.read_text(encoding="utf-8") == first
    assert manager.status()["claude"]["statusline"] == "ours"


def test_remove_reclaims_only_our_own_statusline(tmp_path, monkeypatch) -> None:
    manager, path = _claude_settings(tmp_path, monkeypatch)
    manager.install_claude()

    manager.remove_all()

    assert "statusLine" not in json.loads(path.read_text(encoding="utf-8"))


def test_remove_leaves_a_foreign_statusline_alone(tmp_path, monkeypatch) -> None:
    theirs = {"type": "command", "command": "custom-statusline"}
    manager, path = _claude_settings(tmp_path, monkeypatch, {"statusLine": theirs})
    manager.install_claude()

    manager.remove_all()

    assert json.loads(path.read_text(encoding="utf-8"))["statusLine"] == theirs


def test_status_reports_the_path_codex_actually_asks_you_to_trust(tmp_path, monkeypatch) -> None:
    """Codex 的 /hooks 审核的是 PowerShell 脚本，不是 exe —— 页面必须显示同一个东西。"""
    manager, _ = _claude_settings(tmp_path, monkeypatch)
    manager.install_codex()

    status = manager.status()

    assert status["codex"]["hook_path"].endswith("agentlightctl-hook.ps1")
    assert status["claude"]["hook_path"].endswith("agentlightctl.exe")
    written = json.loads(manager.codex_path.read_text(encoding="utf-8"))
    command = written["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert status["codex"]["hook_path"] in command


def test_backups_are_capped_but_never_touch_hand_named_restore_points(tmp_path, monkeypatch) -> None:
    """配置页有「打开备份目录」按钮，用户是真会去翻的 —— 攒到几十个文件就等于没有。
    但目录里还躺着人手工命名的还原点（例如 codex-hooks-before-powershell-bridge-…），
    那是有人特意留下的，按修改时间一刀切正好会把最该留的删掉。
    """
    from agentlight.integrations import BACKUP_KEEP, IntegrationsManager

    manager = IntegrationsManager(ctl_path=tmp_path / "agentlightctl.exe", home=tmp_path)
    manager.backup_dir = tmp_path / "backups"
    manager.backup_dir.mkdir()

    for index in range(BACKUP_KEEP + 6):
        name = f"codex-hooks-2026081{index % 10}-04010{index % 10}-8085{index:02d}.json"
        (manager.backup_dir / name).write_text(f'{{"n": {index}}}', encoding="utf-8")
    keepers = ["codex-hooks-before-powershell-bridge-20260820.json",
               "codex-hooks-invalid-schema-20260820-restore.json"]
    for name in keepers:
        (manager.backup_dir / name).write_text("{}", encoding="utf-8")

    manager._prune_backups("codex-hooks")

    survivors = {item.name for item in manager.backup_dir.glob("*.json")}
    assert all(name in survivors for name in keepers)
    assert len(survivors - set(keepers)) == BACKUP_KEEP


def test_an_identical_config_is_not_backed_up_twice(tmp_path) -> None:
    """连点几次「一键安装 / 修复」不该产生几份一模一样的文件。"""
    from agentlight.integrations import IntegrationsManager

    manager = IntegrationsManager(ctl_path=tmp_path / "agentlightctl.exe", home=tmp_path)
    manager.backup_dir = tmp_path / "backups"
    source = tmp_path / "hooks.json"
    source.write_text('{"hooks": {}}', encoding="utf-8")

    first = manager._backup(source, "codex-hooks")
    again = manager._backup(source, "codex-hooks")

    assert first == again
    assert len(list(manager.backup_dir.glob("*.json"))) == 1

    # 内容变了就必须留新的一份
    source.write_text('{"hooks": {"Stop": []}}', encoding="utf-8")
    assert manager._backup(source, "codex-hooks") != first
    assert len(list(manager.backup_dir.glob("*.json"))) == 2
