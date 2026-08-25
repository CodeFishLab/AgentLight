from __future__ import annotations

from pathlib import Path

from agentlight import paths


def test_project_root_is_derived_from_the_source_tree() -> None:
    """仓库可以克隆到任意位置，不能把某台机器的绝对路径写死在代码里。"""
    assert (paths.PROJECT_ROOT / "src" / "agentlight" / "paths.py").is_file()
    assert (paths.PROJECT_ROOT / "pyproject.toml").is_file()


def test_environment_variable_wins_over_everything(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTLIGHT_DATA_DIR", str(tmp_path / "custom"))

    root = paths.data_root()

    assert root == tmp_path / "custom"
    for name in ("backups", "logs", "state"):
        assert (root / name).is_dir()


def test_a_fresh_machine_lands_in_localappdata(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("AGENTLIGHT_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))

    assert paths.default_data_root() == tmp_path / "appdata" / "AgentLight"


def test_without_windows_variables_it_falls_back_to_the_home_directory(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("AGENTLIGHT_DATA_DIR", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)

    assert paths.default_data_root() == Path.home() / ".agentlight"


def test_no_machine_specific_absolute_paths_remain_in_shipped_code() -> None:
    """源码中不得出现机器专属的绝对路径。"""
    offenders = []
    for source in (paths.PROJECT_ROOT / "src").rglob("*.py"):
        if "vendor" in source.parts:
            continue
        text = source.read_text(encoding="utf-8")
        for needle in ("F:\\", "C:\\Users", "D:\\", "E:\\"):
            if needle in text:
                offenders.append(f"{source.name}: {needle}")
        if "F:/AgentLightData" in text:
            offenders.append(f"{source.name}: 硬编码数据目录")

    assert offenders == []
