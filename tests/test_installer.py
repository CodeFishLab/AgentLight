from __future__ import annotations

from pathlib import Path


def test_installer_always_exposes_custom_directory_page() -> None:
    script = (Path(__file__).parents[1] / "installer" / "AgentLight.iss").read_text(encoding="utf-8")
    assert "DisableDirPage=no" in script
    assert "UsePreviousAppDir=yes" in script
    assert 'ValueData: """{app}\\AgentLight.exe"" --background"' in script
    assert "RegQueryStringValue" in script
    assert "ExpandConstant('\"{app}\\AgentLight.exe\" --background')" in script


def test_installer_closes_a_running_instance_before_installing() -> None:
    """只靠 CloseApplications 的重启管理器不够稳：应用没退出就会卡在 Closing applications。"""
    script = (Path(__file__).parents[1] / "installer" / "AgentLight.iss").read_text(encoding="utf-8")

    start = script.index("function InitializeSetup(): Boolean")
    body = script[start:script.index("procedure CurStepChanged", start)]

    # 先温和请退（不带 /F 会发 WM_CLOSE，应用能收回托盘图标），等不到才强杀
    assert "/C taskkill /IM AgentLight.exe > nul" in body
    assert "/C taskkill /IM AgentLight.exe /F > nul" in body
    assert body.index("taskkill /IM AgentLight.exe >") < body.index("taskkill /IM AgentLight.exe /F")


def test_tray_reports_quit_when_windows_asks_it_to_close() -> None:
    """安装程序与关机都靠这条路径；不通知主线程就会剩下一个没界面的僵尸进程。"""
    source = (Path(__file__).parents[1] / "src" / "agentlight" / "tray.py").read_text(encoding="utf-8")

    assert "WM_QUERYENDSESSION" in source
    assert "WM_ENDSESSION" in source
    assert "_request_quit" in source


def test_installer_clears_stale_dependencies_on_upgrade() -> None:
    """Inno 只覆盖同名文件；不清 _internal 的话，移除掉的 PySide6 会永远留在安装目录。"""
    script = (Path(__file__).parents[1] / "installer" / "AgentLight.iss").read_text(encoding="utf-8")

    assert "[InstallDelete]" in script
    assert r'Type: filesandordirs; Name: "{app}\_internal"' in script
    # 必须在复制新文件之前清理
    assert script.index("[InstallDelete]") < script.index("[Files]")


def test_uninstaller_uses_graceful_shutdown_then_a_conditional_fallback() -> None:
    script = (Path(__file__).parents[1] / "installer" / "AgentLight.iss").read_text(encoding="utf-8")
    section = script[script.index("[UninstallRun]"):script.index("[UninstallDelete]")]

    assert 'Parameters: "shutdown"' in section
    assert 'Parameters: "/C taskkill /IM AgentLight.exe /F"' in section
    assert "Check: AgentLightRunning" in section
    assert section.index('Parameters: "shutdown"') < section.index("taskkill /IM AgentLight.exe /F")
    assert section.index("taskkill /IM AgentLight.exe /F") < section.index('Parameters: "integrations remove"')


def test_uninstaller_removes_only_agentlight_autostart_and_preserves_user_data() -> None:
    script = (Path(__file__).parents[1] / "installer" / "AgentLight.iss").read_text(encoding="utf-8")

    assert "CurUninstallStepChanged" in script
    assert "RegDeleteValue(HKCU, 'Software\\Microsoft\\Windows\\CurrentVersion\\Run', 'AgentLight')" in script
    uninstall_delete = script[script.index("[UninstallDelete]"):script.index("[Code]")]
    assert r'Type: filesandordirs; Name: "{app}"' in uninstall_delete
    assert "{localappdata}\\AgentLight" not in uninstall_delete
