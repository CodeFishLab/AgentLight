"""每个模块都要能被 import。

起因：清理托盘死代码时删掉了 STATE_LABELS，但 app.py 还在 import 它。整套测试
一个都没红——因为当时没有任何用例 import 过 agentlight.app，打包出去才在用户
机器上炸成一个 ImportError 弹窗。这个文件就是防这种事。
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

import agentlight

MODULES = sorted(
    info.name for info in pkgutil.walk_packages(agentlight.__path__, "agentlight.")
    if not info.ispkg
)


def test_every_module_is_discovered() -> None:
    """空清单会让下面的用例静默通过，先确认真的扫到了东西。"""
    assert len(MODULES) >= 12
    assert "agentlight.app" in MODULES
    assert "agentlight.tray" in MODULES


@pytest.mark.parametrize("name", MODULES)
def test_module_imports_cleanly(name: str) -> None:
    importlib.import_module(name)
