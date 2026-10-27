"""GUI 的 MCP 选择器（离屏运行，不弹窗）。

守的是四件事：
1. 打开时勾选状态 = 合并后的 enabled（用户级 + 项目级）；
2. 确定只写**项目级**配置——在 A 项目关掉 Blender 不会影响 B 项目；
3. 实测出来的 token 数会显示在行内和汇总里；
4. 入口真的挂在菜单上（上一轮就是"实现了但没入口"）。
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6.QtWidgets", reason="需要 PyQt6")

from PyQt6.QtWidgets import QApplication  # noqa: E402

APP_PATH = Path(__file__).resolve().parent.parent / "desktop" / "app.py"


@pytest.fixture(scope="module")
def gui_module():
    spec = importlib.util.spec_from_file_location("gangge_desktop_app", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def isolated_global(tmp_path, monkeypatch):
    """把用户级配置指到临时目录，避免读到真实的 ~/.gangge。"""
    import gangge.layer4_tools.mcp_client as mc

    path = tmp_path / "home" / ".gangge" / "mcp_servers.json"
    monkeypatch.setattr(mc, "GLOBAL_CONFIG_PATH", path)
    return path


def _write(path: Path, servers: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"servers": servers}, ensure_ascii=False), encoding="utf-8")
    return path


def _server(name: str, enabled: bool) -> dict:
    return {
        "name": name,
        "transport": "stdio",
        "command": "uvx",
        "args": ["mcp-for-blender"],
        "enabled": enabled,
        "timeout": 60,
    }


def _project_file(workspace: Path) -> Path:
    return workspace / ".gangge" / "mcp_servers.json"


def test_dialog_reflects_enabled_flags(gui_module, qapp, tmp_path):
    ws = tmp_path / "proj"
    _write(_project_file(ws), [_server("blender", True), _server("github", False)])

    dialog = gui_module.MCPServersDialog(ws)
    try:
        assert dialog.selected_names() == ["blender"]
        dialog._set_all(True)
        assert set(dialog.selected_names()) == {"blender", "github"}
        dialog._set_all(False)
        assert dialog.selected_names() == []
    finally:
        dialog.reject()


def test_global_entries_are_visible_and_marked(gui_module, qapp, tmp_path, isolated_global):
    ws = tmp_path / "proj"
    _write(isolated_global, [_server("blender", True)])
    _write(_project_file(ws), [_server("autocad", False)])

    dialog = gui_module.MCPServersDialog(ws)
    try:
        assert set(dialog._checks) == {"blender", "autocad"}
        from gangge.layer4_tools.mcp_client import source_label

        assert source_label(dialog._sources["blender"], ws) == "用户级"
        assert source_label(dialog._sources["autocad"], ws) == "本项目"
    finally:
        dialog.reject()


def test_save_only_touches_project_layer(gui_module, qapp, tmp_path, isolated_global):
    """关掉一个用户级 server：只在本项目写最小覆盖，用户级文件原样不动。"""
    ws = tmp_path / "proj"
    _write(isolated_global, [_server("blender", True)])
    global_before = isolated_global.read_text(encoding="utf-8")

    dialog = gui_module.MCPServersDialog(ws)
    dialog._checks["blender"].setChecked(False)
    dialog.accept()

    assert isolated_global.read_text(encoding="utf-8") == global_before, "不能改用户级文件"
    saved = json.loads(_project_file(ws).read_text(encoding="utf-8"))
    assert saved["servers"] == [{"name": "blender", "enabled": False}]


def test_save_removes_redundant_override(gui_module, qapp, tmp_path, isolated_global):
    """把用户级默认值改回来后，项目级的覆盖项应当消失，而不是留一条 enabled: true。"""
    ws = tmp_path / "proj"
    _write(isolated_global, [_server("blender", True)])
    _write(_project_file(ws), [{"name": "blender", "enabled": False}])

    dialog = gui_module.MCPServersDialog(ws)
    dialog._checks["blender"].setChecked(True)
    dialog.accept()

    saved = json.loads(_project_file(ws).read_text(encoding="utf-8"))
    assert saved["servers"] == []


def test_project_entries_are_edited_in_place(gui_module, qapp, tmp_path):
    ws = tmp_path / "proj"
    _write(_project_file(ws), [_server("blender", False)])

    dialog = gui_module.MCPServersDialog(ws)
    dialog._checks["blender"].setChecked(True)
    dialog.accept()

    saved = json.loads(_project_file(ws).read_text(encoding="utf-8"))
    entry = saved["servers"][0]
    assert entry["enabled"] is True
    assert entry["command"] == "uvx", "项目级完整定义不能被压成最小覆盖"
    assert dialog.result_text().startswith("MCP 已启用: blender")


def test_accept_clears_session_env(gui_module, qapp, tmp_path, monkeypatch):
    ws = tmp_path / "proj"
    _write(_project_file(ws), [_server("blender", True)])
    monkeypatch.setenv("GANGGE_MCP_SERVERS", "none")

    dialog = gui_module.MCPServersDialog(ws)
    dialog.accept()

    assert "GANGGE_MCP_SERVERS" not in os.environ


def test_measured_tokens_are_visible(gui_module, qapp, tmp_path):
    ws = tmp_path / "proj"
    _write(_project_file(ws), [_server("blender", True)])

    dialog = gui_module.MCPServersDialog(ws)
    try:
        assert "未测量" in dialog._details["blender"].text()
        dialog._on_probed("blender", 9, 1173)
        assert "9 个工具 ≈ 1173 tokens/请求" in dialog._details["blender"].text()
        assert "1173" in dialog._summary.text()
    finally:
        dialog.reject()


def test_probe_failure_is_shown(gui_module, qapp, tmp_path):
    ws = tmp_path / "proj"
    _write(_project_file(ws), [_server("blender", True)])

    dialog = gui_module.MCPServersDialog(ws)
    try:
        dialog._on_failed("blender", "进程已退出")
        assert "连接失败" in dialog._details["blender"].text()
    finally:
        dialog.reject()


def test_missing_config_does_not_create_files(gui_module, qapp, tmp_path):
    ws = tmp_path / "proj"

    dialog = gui_module.MCPServersDialog(ws)
    try:
        assert dialog.servers == []
        assert dialog.selected_names() == []
        assert not _project_file(ws).exists()
    finally:
        dialog.reject()


def test_menu_action_wired(gui_module):
    source = APP_PATH.read_text(encoding="utf-8")
    assert "MCP 服务(M)" in source
    assert "_open_mcp_dialog" in source
