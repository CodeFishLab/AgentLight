"""开源发布的护栏：许可证齐全，且仓库里不再残留「禁止分发」这类说法。"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_apache_license_is_present_and_complete() -> None:
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")

    assert "Apache License" in text and "Version 2.0" in text
    # Apache 2.0 相对 MIT 的两个关键条款，缺了就等于没选它
    assert "Grant of Patent License" in text
    assert "Grant of Copyright License" in text
    assert "Disclaimer of Warranty" in text


def test_notice_is_not_required_by_project_metadata() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert not (ROOT / "NOTICE").exists()
    assert "NOTICE" not in data["project"]["license-files"]


def test_project_metadata_declares_the_license() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]

    assert project["license"] == "Apache-2.0"
    assert "LICENSE" in project["license-files"]


def test_no_file_still_forbids_redistribution() -> None:
    """发布源码中不得包含禁止再分发的声明。"""
    assert not (ROOT / "PRIVATE_BUILD_NOTICE.md").exists()

    banned = ("不得公开发布", "private build", "PRIVATE_BUILD_NOTICE")
    offenders = []
    for pattern in ("*.py", "*.md", "*.ps1", "*.iss", "*.toml"):
        for path in ROOT.rglob(pattern):
            parts = set(path.parts)
            if parts & {".venv", "build", "dist", ".cache", ".tmp", "__pycache__"}:
                continue
            if path == Path(__file__):
                continue  # 这个文件本身就带着这些字符串当检查模式
            text = path.read_text(encoding="utf-8", errors="replace")
            for needle in banned:
                if needle.lower() in text.lower():
                    offenders.append(f"{path.relative_to(ROOT)}: {needle}")

    assert offenders == []


def test_unused_vendored_module_is_gone() -> None:
    """default_cli.py 有 532 行且无任何引用，开源前不该带着走。"""
    assert not (ROOT / "src" / "agentlight" / "vendor" / "default_cli.py").exists()
