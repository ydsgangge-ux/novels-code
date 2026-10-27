"""MCP 分层配置（用户级 → 项目级 → 临时层）与"模型自己新增 server"。

设计要点都在这里被钉住：
- 后一层**整体**覆盖前一层同名条目（不做字段拼接）；
- 选择结果只写项目级，用户级文件一个字都不动；
- 模型能自己 `mcp_add_server`，但参数非法（名字带 `__`、缺 command）必须被拒。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

import gangge.layer4_tools.mcp_client as mc
from gangge.layer3_agent.tools.mcp_admin import MCPAddServerTool
from gangge.layer3_agent.tools.registry import create_tool_registry
from gangge.layer4_tools.mcp_client import (
    MCPClientManager,
    load_merged_config,
    merge_documents,
    save_selection,
    source_label,
    upsert_server,
)

MOCK_SERVER = '''
import json, sys

TOOLS = [{
    "name": "draw_circle",
    "description": "在 CAD 里画圆 circle",
    "inputSchema": {"type": "object", "properties": {"radius": {"type": "number"}}, "required": ["radius"]},
}]

def respond(req_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}) + "\\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except json.JSONDecodeError:
        continue
    req_id = req.get("id")
    if req_id is None:
        continue
    method = req.get("method", "")
    if method == "initialize":
        respond(req_id, {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}})
    elif method == "tools/list":
        respond(req_id, {"tools": TOOLS})
    elif method == "tools/call":
        respond(req_id, {"content": [{"type": "text", "text": "ok"}]})
'''


@pytest.fixture(autouse=True)
def isolated_global(tmp_path, monkeypatch):
    """用户级配置指到临时目录，别碰真实的 ~/.gangge。"""
    path = tmp_path / "home" / ".gangge" / "mcp_servers.json"
    monkeypatch.setattr(mc, "GLOBAL_CONFIG_PATH", path)
    monkeypatch.delenv(mc.CONFIG_ENV, raising=False)
    return path


def _write(path: Path, servers: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"servers": servers}, ensure_ascii=False), encoding="utf-8")
    return path


def _project(ws: Path) -> Path:
    return ws / ".gangge" / "mcp_servers.json"


# ── 合并语义 ─────────────────────────────────────────

def test_project_layer_overrides_whole_entry(tmp_path, isolated_global):
    ws = tmp_path / "proj"
    _write(isolated_global, [{
        "name": "blender", "transport": "stdio", "command": "uvx",
        "args": ["mcp-for-blender"], "enabled": True, "timeout": 60,
    }])
    _write(_project(ws), [{
        "name": "blender", "transport": "stdio", "command": "node",
        "args": ["custom/index.js"], "enabled": False, "timeout": 30,
    }])

    document, sources = load_merged_config(ws)
    servers = document["servers"]

    assert len(servers) == 1
    assert servers[0]["command"] == "node", "必须整体覆盖，不能出现 uvx + custom/index.js 的拼接"
    assert servers[0]["args"] == ["custom/index.js"]
    assert sources["blender"] == _project(ws)


def test_global_and_project_are_merged_by_name(tmp_path, isolated_global):
    ws = tmp_path / "proj"
    _write(isolated_global, [{"name": "github", "transport": "stdio", "command": "npx", "enabled": False}])
    _write(_project(ws), [{"name": "autocad", "transport": "stdio", "command": "node", "enabled": True}])

    document, sources = load_merged_config(ws)
    names = [s["name"] for s in document["servers"]]

    assert set(names) == {"github", "autocad"}
    assert sources["github"] == isolated_global
    assert sources["autocad"] == _project(ws)


def test_extra_layer_wins(tmp_path, isolated_global, monkeypatch):
    ws = tmp_path / "proj"
    _write(isolated_global, [{"name": "x", "transport": "stdio", "command": "global-cmd"}])
    extra = _write(tmp_path / "extra.json", [{"name": "x", "transport": "stdio", "command": "extra-cmd"}])
    monkeypatch.setenv(mc.CONFIG_ENV, str(extra))

    document, sources = load_merged_config(ws)
    assert document["servers"][0]["command"] == "extra-cmd"
    assert sources["x"] == extra


def test_merge_ignores_entries_without_name():
    document, _ = merge_documents([
        ({"servers": [{"transport": "stdio"}, {"name": "ok"}]}, Path("a.json")),
    ])
    assert [s["name"] for s in document["servers"]] == ["ok"]


def test_source_labels(tmp_path, isolated_global):
    ws = tmp_path / "proj"
    assert source_label(isolated_global, ws) == "用户级"
    assert source_label(_project(ws), ws) == "本项目"
    assert source_label(tmp_path / "x.json", ws) == "临时层"


# ── 选择写回 ─────────────────────────────────────────

def test_save_selection_writes_minimal_override_for_global_entry(tmp_path, isolated_global):
    ws = tmp_path / "proj"
    _write(isolated_global, [{"name": "blender", "transport": "stdio", "command": "uvx", "enabled": True}])
    before = isolated_global.read_text(encoding="utf-8")

    save_selection(ws, {"blender": False})

    assert isolated_global.read_text(encoding="utf-8") == before, "用户级文件不能被改"
    saved = json.loads(_project(ws).read_text(encoding="utf-8"))
    assert saved["servers"] == [{"name": "blender", "enabled": False}]


def test_save_selection_drops_redundant_override(tmp_path, isolated_global):
    ws = tmp_path / "proj"
    _write(isolated_global, [{"name": "blender", "transport": "stdio", "command": "uvx", "enabled": True}])
    _write(_project(ws), [{"name": "blender", "enabled": False}])

    save_selection(ws, {"blender": True})

    saved = json.loads(_project(ws).read_text(encoding="utf-8"))
    assert saved["servers"] == []


def test_save_selection_keeps_full_project_definition(tmp_path):
    ws = tmp_path / "proj"
    _write(_project(ws), [{
        "name": "autocad", "transport": "stdio", "command": "node",
        "args": ["cad.js"], "enabled": False, "timeout": 60,
    }])

    save_selection(ws, {"autocad": True})

    entry = json.loads(_project(ws).read_text(encoding="utf-8"))["servers"][0]
    assert entry["command"] == "node" and entry["enabled"] is True


# ── upsert ───────────────────────────────────────────

def test_upsert_server_overwrites_by_name(tmp_path, isolated_global):
    ws = tmp_path / "proj"
    upsert_server({"name": "blender", "transport": "stdio", "command": "uvx", "args": ["a"]}, ws)
    upsert_server({"name": "blender", "transport": "stdio", "command": "node", "args": ["b"]}, ws)

    servers = json.loads(_project(ws).read_text(encoding="utf-8"))["servers"]
    assert len(servers) == 1
    assert servers[0]["command"] == "node"


def test_upsert_server_global_scope(tmp_path, isolated_global):
    upsert_server({"name": "g", "transport": "stdio", "command": "x"}, tmp_path / "proj", scope="global")
    assert isolated_global.exists()
    assert not _project(tmp_path / "proj").exists()


# ── 模型自己新增 server ──────────────────────────────

def _add_tool(tmp_path, isolated_global):
    ws = tmp_path / "proj"
    manager = MCPClientManager.from_workspace(ws, connect=False)
    registry = create_tool_registry(workspace=str(ws), load_plugins=False)
    registry.set_profile("coding")
    return MCPAddServerTool(manager, registry, str(ws)), manager, registry, ws


def test_mcp_add_server_writes_config_and_registers_tools(tmp_path, isolated_global):
    script = tmp_path / "srv.py"
    script.write_text(MOCK_SERVER, encoding="utf-8")
    tool, manager, registry, ws = _add_tool(tmp_path, isolated_global)

    try:
        result = asyncio.run(tool.execute(
            name="mockcad", command=sys.executable, args=[str(script)]
        ))

        assert not result.is_error, result.output
        assert "mockcad__draw_circle" in result.output

        saved = json.loads(_project(ws).read_text(encoding="utf-8"))["servers"]
        assert saved[0]["name"] == "mockcad"
        assert saved[0]["enabled"] is True
        assert saved[0]["args"] == [str(script)]

        # 注册了但默认不发送（token 承诺），搜得到才揭示
        assert registry.get("mockcad__draw_circle") is not None
        visible = {d.name for d in registry.get_definitions(phase=registry.get_current_phase())}
        assert "mockcad__draw_circle" not in visible
        assert registry.search("mockcad")
    finally:
        manager.disconnect_all()


def test_mcp_add_server_global_scope(tmp_path, isolated_global):
    script = tmp_path / "srv.py"
    script.write_text(MOCK_SERVER, encoding="utf-8")
    tool, manager, _registry, ws = _add_tool(tmp_path, isolated_global)

    try:
        result = asyncio.run(tool.execute(
            name="shared", command=sys.executable, args=[str(script)], scope="global"
        ))
        assert not result.is_error, result.output
        assert "shared" in isolated_global.read_text(encoding="utf-8")
        assert not _project(ws).exists()
    finally:
        manager.disconnect_all()


def test_mcp_add_server_rejects_bad_name(tmp_path, isolated_global):
    tool, manager, _registry, ws = _add_tool(tmp_path, isolated_global)
    try:
        result = asyncio.run(tool.execute(name="bad__name", command="node"))
        assert result.is_error
        assert "__" in result.output
        assert not _project(ws).exists()
    finally:
        manager.disconnect_all()


def test_mcp_add_server_rejects_bad_transport(tmp_path, isolated_global):
    tool, manager, _registry, _ws = _add_tool(tmp_path, isolated_global)
    try:
        result = asyncio.run(tool.execute(name="x", command="node", transport="streamable-http"))
        assert result.is_error
        assert "transport" in result.output
    finally:
        manager.disconnect_all()


def test_mcp_add_server_requires_command_for_stdio(tmp_path, isolated_global):
    tool, manager, _registry, _ws = _add_tool(tmp_path, isolated_global)
    try:
        result = asyncio.run(tool.execute(name="x", command=""))
        assert result.is_error
        assert "command" in result.output
    finally:
        manager.disconnect_all()


def test_mcp_add_server_reports_connection_failure(tmp_path, isolated_global):
    tool, manager, _registry, ws = _add_tool(tmp_path, isolated_global)
    try:
        result = asyncio.run(tool.execute(name="ghost", command=sys.executable, args=["-c", "pass"]))
        assert result.is_error
        assert "连接失败" in result.output or "没有拉到任何工具" in result.output
        # 配置已写入，方便用户修好后重试
        assert _project(ws).exists()
    finally:
        manager.disconnect_all()
