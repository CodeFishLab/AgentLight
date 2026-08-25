from __future__ import annotations

import os
import sys
from pathlib import Path

from .paths import gui_executable


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "AgentLight"


def command() -> str:
    executable = gui_executable()
    if executable.is_file():
        return f'"{executable}" --background'
    return f'"{Path(sys.executable)}" -m agentlight.app --background'


def is_enabled() -> bool:
    if os.name != "nt":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return str(value) == command()
    except OSError:
        return False


def set_enabled(enabled: bool) -> None:
    if os.name != "nt":
        return
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command())
        else:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass

