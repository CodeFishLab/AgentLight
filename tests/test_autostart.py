from __future__ import annotations

import sys
from types import SimpleNamespace

from agentlight import autostart


def test_disable_autostart_deletes_only_the_agentlight_value(monkeypatch) -> None:
    deleted: list[str] = []

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    fake_winreg = SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        CreateKey=lambda _root, _subkey: Key(),
        DeleteValue=lambda _key, value_name: deleted.append(value_name),
    )
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    monkeypatch.setattr(autostart.os, "name", "nt")

    autostart.set_enabled(False)

    assert deleted == [autostart.VALUE_NAME]
