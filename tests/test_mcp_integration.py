"""MCP 集成测试。

覆盖三件事：
  1. stdio 传输能把 Server 的工具拉下来，并按 ``{server}__{tool}`` 命名注册；
  2. ``build_tool_definitions()`` 交出的必须是与本地工具同类型的 ``ToolDefinition``
     —— 这是曾经的真 bug：返回 dict 时 ``layer5_llm`` 的
     ``to_openai_schema()`` / ``to_anthropic_schema()`` 会 AttributeError，
     MCP 工具根本到不了模型（旧测试断言的是 ``d["name"]``，所以没测出来）；
  3. 失败路径统一以 ``[错误]`` 开头、``isError`` 被尊重、超时有界。

不依赖真实 CAD 软件：Python mock server 覆盖协议细节，Node 用例验证 sidecar 路线。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from gangge.layer4_tools.mcp_client import MCPClientManager, MCPServerConfig
from gangge.layer5_llm.base import ToolDefinition

FIXTURES = Path(__file__).resolve().parent / "fixtures"
NODE_SERVER = FIXTURES / "mcp_echo_server.mjs"

EXPECTED_TOOLS = [
    "mockcad__draw_circle",
    "mockcad__create_layer",
    "mockcad__failing",
    "mockcad__hang",
]

# ── Python mock MCP Server（每行一条 JSON-RPC 2.0）──
MOCK_SERVER = '''
import json, sys, time

TOOLS = [
    {
        "name": "draw_circle",
        "description": "在 CAD 中画一个圆",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cx": {"type": "number"},
                "cy": {"type": "number"},
                "radius": {"type": "number"},
            },
            "required": ["cx", "cy", "radius"],
        },
    },
    {
        "name": "create_layer",
        "description": "创建 CAD 图层",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {"name": "failing", "description": "总是失败", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hang", "description": "收到调用后永不回应", "inputSchema": {"type": "object", "properties": {}}},
]


def respond(req_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}) + "\\n")
    sys.stdout.flush()


def fail(req_id, message):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": message}}) + "\\n")
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
    if req_id is None:          # 通知（notifications/initialized 等），不回包
        continue

    method = req.get("method", "")
    if method == "initialize":
        respond(req_id, {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}})
    elif method == "tools/list":
        respond(req_id, {"tools": TOOLS})
    elif method == "tools/call":
        name = req["params"]["name"]
        args = req["params"].get("arguments") or {}
        if name == "draw_circle":
            text = "已绘制圆 圆心({}, {}) 半径={}".format(args["cx"], args["cy"], args["radius"])
            respond(req_id, {"content": [{"type": "text", "text": text}]})
        elif name == "create_layer":
            respond(req_id, {"content": [{"type": "text", "text": "已创建图层 {}".format(args["name"])}]})
        elif name == "failing":
            respond(req_id, {"content": [{"type": "text", "text": "图层名非法"}], "isError": True})
        elif name == "hang":
            time.sleep(600)
        else:
            respond(req_id, {"content": [{"type": "text", "text": "未知工具"}], "isError": True})
    else:
        fail(req_id, "unknown method: {}".format(method))
'''


# ── fixtures ──────────────────────────────────────────

@pytest.fixture
def mock_server_path(tmp_path: Path) -> Path:
    path = tmp_path / "mock_mcp_server.py"
    path.write_text(MOCK_SERVER, encoding="utf-8")
    return path


def _make_manager(server_path: Path, timeout: int = 10) -> MCPClientManager:
    return MCPClientManager([
        MCPServerConfig(
            name="mockcad",
            transport="stdio",
            command=sys.executable,
            args=[str(server_path)],
            timeout=timeout,
        )
    ])


@pytest.fixture
def manager(mock_server_path: Path):
    m = _make_manager(mock_server_path)
    m.connect_all()
    assert m.get_all_tools(), "mock server 未注册任何工具（连接失败？看 [MCP] 日志）"
    yield m
    m.disconnect_all()


# ── 传输与命名 ────────────────────────────────────────

def test_tools_are_registered_with_server_prefix(manager):
    assert [t.full_name for t in manager.get_all_tools()] == EXPECTED_TOOLS


def test_raw_tool_name_is_kept_for_calling(manager):
    tool = manager.get_all_tools()[0]
    assert tool.server_name == "mockcad"
    assert tool.name == "draw_circle"          # 调用 Server 时用原始名
    assert tool.full_name == "mockcad__draw_circle"  # 对模型暴露的是带前缀的名字


# ── 回归：definitions 必须是 ToolDefinition ───────────

def test_definitions_are_tool_definition_objects(manager):
    defs = manager.build_tool_definitions()

    assert len(defs) == len(EXPECTED_TOOLS)
    for d in defs:
        assert isinstance(d, ToolDefinition), (
            "build_tool_definitions() 必须返回 ToolDefinition："
            "layer5_llm 的 provider 直接调用 to_openai_schema()/to_anthropic_schema()"
        )
        assert d.name.startswith("mockcad__")
        assert d.description.startswith("[MCP:mockcad]")


def test_definitions_serialize_for_both_providers(manager):
    defs = manager.build_tool_definitions()

    openai_tools = [d.to_openai_schema() for d in defs]
    assert openai_tools[0]["type"] == "function"
    assert openai_tools[0]["function"]["name"] == "mockcad__draw_circle"
    assert openai_tools[0]["function"]["parameters"]["required"] == ["cx", "cy", "radius"]

    anthropic_tools = [d.to_anthropic_schema() for d in defs]
    assert anthropic_tools[0]["input_schema"]["type"] == "object"
    # 真实请求体要能 JSON 序列化，schema 里有不可序列化对象会在这里暴露
    json.dumps(anthropic_tools, ensure_ascii=False)


# ── 调用 ──────────────────────────────────────────────

def test_call_tool_returns_text_content(manager):
    out = manager.call_tool("mockcad__draw_circle", {"cx": 100, "cy": 100, "radius": 50})
    assert out == "已绘制圆 圆心(100, 100) 半径=50"


def test_iserror_is_surfaced_as_error(manager):
    out = manager.call_tool("mockcad__failing", {})
    assert out.startswith("[错误]"), "isError 的工具结果不能被当成成功"
    assert "图层名非法" in out


def test_unknown_tool_reports_error(manager):
    out = manager.call_tool("nope__echo", {})
    assert out.startswith("[错误]")
    assert "未找到" in out


def test_has_tool_matches_exactly(manager):
    assert manager.has_tool("mockcad__draw_circle")
    assert not manager.has_tool("draw_circle")
    # 名字里带双下划线的本地工具不能被误判成 MCP 工具
    assert not manager.has_tool("my_local__helper")


# ── 生命周期 ──────────────────────────────────────────

def test_connect_all_twice_does_not_duplicate_tools(mock_server_path):
    m = _make_manager(mock_server_path)
    try:
        m.connect_all()
        first = [t.full_name for t in m.get_all_tools()]
        m.connect_all()
        second = [t.full_name for t in m.get_all_tools()]
        assert first == second == EXPECTED_TOOLS
    finally:
        m.disconnect_all()


def test_call_timeout_bounded_by_config(mock_server_path):
    m = _make_manager(mock_server_path, timeout=1)
    try:
        m.connect_all()
        out = m.call_tool("mockcad__hang", {})
        assert out.startswith("[错误]")
        assert "无响应" in out
    finally:
        m.disconnect_all()


@pytest.mark.skipif(sys.platform != "win32", reason="只验证 Windows 的 .cmd 垫片")
def test_bare_command_name_is_resolved_through_path(tmp_path, mock_server_path):
    """Windows 上 ``npx`` 是 ``npx.cmd``：不做 PATH 解析就会 WinError 2，所有 stdio server 都起不来。"""
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    (shim_dir / "mockmcp.cmd").write_text(
        f'@echo off\r\n"{sys.executable}" "{mock_server_path}" %*\r\n',
        encoding="utf-8",
    )
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("PATH", str(shim_dir) + os.pathsep + os.environ.get("PATH", ""))
    try:
        m = MCPClientManager([
            MCPServerConfig(name="mockcad", transport="stdio", command="mockmcp", timeout=20)
        ])
        try:
            m.connect_all()
            assert [t.full_name for t in m.get_all_tools()] == EXPECTED_TOOLS
        finally:
            m.disconnect_all()
    finally:
        monkeypatch.undo()


# ── 选择：没选中就不该占 token ────────────────────────

def _write_ws_config(ws: Path, servers: list[dict]) -> Path:
    cfg = ws / ".gangge" / "mcp_servers.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"servers": servers}, ensure_ascii=False), encoding="utf-8")
    return cfg


def _server_entry(name: str, server_path: Path, *, enabled: bool = True) -> dict:
    return {
        "name": name,
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(server_path)],
        "enabled": enabled,
        "timeout": 20,
    }


def test_parse_selection_forms():
    from gangge.layer4_tools.mcp_client import parse_selection

    available = ["blender", "github", "memory"]
    assert parse_selection(None, available) is None
    assert parse_selection("", available) is None
    assert parse_selection("all", available) == set(available)
    assert parse_selection("none", available) == set()
    assert parse_selection("blender,github", available) == {"blender", "github"}
    assert parse_selection("blender,  github", available) == {"blender", "github"}
    assert parse_selection("blender,nope", available) == {"blender"}, "未知名字只忽略，不该报错"


def test_selection_none_connects_nothing(tmp_path, mock_server_path):
    """选了 none：不启动进程、不产生定义——这才叫 0 token。"""
    ws = tmp_path / "ws"
    _write_ws_config(ws, [_server_entry("mockcad", mock_server_path, enabled=True)])

    m = MCPClientManager.from_workspace(ws, selection="none")
    try:
        assert m.get_all_tools() == []
        assert m.build_tool_definitions() == []
        assert m.estimate_tokens() == 0
        assert not m._connections, "未选中的 server 连进程都不该启动"
    finally:
        m.disconnect_all()


def test_selection_by_name_only_connects_that_server(tmp_path, mock_server_path):
    ws = tmp_path / "ws"
    _write_ws_config(
        ws,
        [
            _server_entry("alpha", mock_server_path),
            _server_entry("beta", mock_server_path),
        ],
    )

    m = MCPClientManager.from_workspace(ws, selection="alpha")
    try:
        names = {t.full_name for t in m.get_all_tools()}
        assert names and all(n.startswith("alpha__") for n in names)
        assert list(m._connections) == ["alpha"]
    finally:
        m.disconnect_all()


def test_explicit_selection_overrides_disabled_flag(tmp_path, mock_server_path):
    """显式点名要用的 server，即使配置里 enabled=false 也该连上。"""
    ws = tmp_path / "ws"
    _write_ws_config(ws, [_server_entry("mockcad", mock_server_path, enabled=False)])

    m = MCPClientManager.from_workspace(ws, selection="mockcad")
    try:
        assert m.get_all_tools(), "显式选择优先于配置的 enabled"
    finally:
        m.disconnect_all()


def test_selection_follows_config_enabled_when_unspecified(tmp_path, mock_server_path, monkeypatch):
    monkeypatch.delenv("GANGGE_MCP_SERVERS", raising=False)
    ws = tmp_path / "ws"
    _write_ws_config(
        ws,
        [
            _server_entry("on", mock_server_path, enabled=True),
            _server_entry("off", mock_server_path, enabled=False),
        ],
    )

    m = MCPClientManager.from_workspace(ws)
    try:
        assert list(m._connections) == ["on"]
    finally:
        m.disconnect_all()


def test_selection_read_from_env(tmp_path, mock_server_path, monkeypatch):
    ws = tmp_path / "ws"
    _write_ws_config(ws, [_server_entry("mockcad", mock_server_path)])
    monkeypatch.setenv("GANGGE_MCP_SERVERS", "none")

    m = MCPClientManager.from_workspace(ws)
    try:
        assert m.get_all_tools() == []
    finally:
        m.disconnect_all()


def test_status_lines_show_cost_and_selection(tmp_path, mock_server_path):
    ws = tmp_path / "ws"
    _write_ws_config(
        ws,
        [
            _server_entry("alpha", mock_server_path),
            _server_entry("beta", mock_server_path, enabled=False),
        ],
    )

    m = MCPClientManager.from_workspace(ws)
    try:
        text = "\n".join(m.status_lines())
        assert "alpha" in text and "●" in text
        assert "tokens/请求" in text
        assert "未选中 → 不连接" in text, "没选中的必须明确写 0 token"
        assert m.estimate_tokens() > 0
    finally:
        m.disconnect_all()


def test_repl_slash_command_toggles_selection(tmp_path, mock_server_path, monkeypatch):
    """/mcp 是会话里唯一的选择入口，改的是下一条任务读到的环境变量。"""
    from gangge.cli_repl import _handle_slash

    ws = tmp_path / "ws"
    _write_ws_config(
        ws,
        [
            _server_entry("alpha", mock_server_path),
            _server_entry("beta", mock_server_path, enabled=False),
        ],
    )
    monkeypatch.delenv("GANGGE_MCP_SERVERS", raising=False)

    _handle_slash("/mcp none", str(ws))
    assert os.environ["GANGGE_MCP_SERVERS"] == "none"

    _handle_slash("/mcp on beta", str(ws))
    assert os.environ["GANGGE_MCP_SERVERS"] == "beta"

    _handle_slash("/mcp off beta", str(ws))
    assert os.environ["GANGGE_MCP_SERVERS"] == "none"

    _handle_slash("/mcp on nope", str(ws))
    assert os.environ["GANGGE_MCP_SERVERS"] == "none", "未知名字不该改动现有选择"


# ── 阶段 0 spike：Node sidecar ────────────────────────

@pytest.mark.skipif(shutil.which("node") is None, reason="需要 Node 运行时")
@pytest.mark.skipif(not NODE_SERVER.exists(), reason="缺少 Node sidecar fixture")
def test_node_sidecar_roundtrip():
    """Node 侧的 MCP Server 能被现有 Python 客户端直接消费，无需任何桥接协议。"""
    m = MCPClientManager([
        MCPServerConfig(
            name="bridge",
            transport="stdio",
            command="node",
            args=[str(NODE_SERVER)],
            timeout=20,
        )
    ])
    try:
        m.connect_all()
        names = sorted(t.full_name for t in m.get_all_tools())
        assert names == ["bridge__add", "bridge__echo", "bridge__explode"]

        assert m.call_tool("bridge__echo", {"message": "hi"}) == "echo: hi"
        assert m.call_tool("bridge__add", {"a": 2, "b": 40}) == "42"
        assert m.call_tool("bridge__explode", {}).startswith("[错误]")

        defs = m.build_tool_definitions()
        assert all(isinstance(d, ToolDefinition) for d in defs)
        json.dumps([d.to_openai_schema() for d in defs], ensure_ascii=False)
    finally:
        m.disconnect_all()
