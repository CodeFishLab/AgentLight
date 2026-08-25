from __future__ import annotations

import os
import sys
from pathlib import Path


APP_NAME = "AgentLight"

# 源码树根目录：src/agentlight/paths.py 往上三层。只在未打包运行时用得到。
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def default_data_root() -> Path:
    """配置、日志与备份的默认位置。

    优先 %LOCALAPPDATA%\\AgentLight——这是 Windows 上放用户级应用数据的标准位置，
    也和安装包的 PrivilegesRequired=lowest 相符，不需要管理员权限。

    想放到别处可使用环境变量 AGENTLIGHT_DATA_DIR。不得硬编码具体盘符或开发机路径。
    """
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / f".{APP_NAME.lower()}"


def data_root() -> Path:
    override = os.environ.get("AGENTLIGHT_DATA_DIR")
    root = Path(override) if override else default_data_root()
    root.mkdir(parents=True, exist_ok=True)
    for name in ("backups", "logs", "state"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def executable_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return PROJECT_ROOT


def gui_executable() -> Path:
    override = os.environ.get("AGENTLIGHT_GUI_EXE")
    if override:
        return Path(override)
    return executable_dir() / "AgentLight.exe"


def ctl_executable() -> Path:
    override = os.environ.get("AGENTLIGHT_CTL_EXE")
    if override:
        return Path(override)
    return executable_dir() / "agentlightctl.exe"


def icon_path() -> Path:
    """托盘图标。保持与安装包、快捷方式同一个 .ico，观感不会有差别。"""
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(bundled) / "assets" / "agentlight.ico"
    return PROJECT_ROOT / "assets" / "agentlight.ico"


def webui_dir() -> Path:
    """浏览器配置页的静态资源目录。

    打包后 PyInstaller 会把它放进 _internal（onedir 下 sys._MEIPASS 也指向那里）。
    """
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(bundled) / "agentlight" / "webui"
    return Path(__file__).resolve().parent / "webui"


def config_path() -> Path:
    return data_root() / "config.json"


def log_path() -> Path:
    return data_root() / "logs" / "agentlight.log"
