"""MCP 工具的延迟揭示：不揭示就不占 token，揭示后完整可用。

这些用例守的是三个具体承诺：
1. 注册 ≠ 发送——延迟的工具必须不出现在 ``get_definitions()`` 里；
2. ``tool_search`` 能搜到并揭示它（按名字、按 server、按描述都能命中）；
3. 揭示之后调用真的走 manager，不是只有壳子。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from gangge.layer3_agent.tools.mcp_proxy import MCPToolProxy, register_mcp_tools
from gangge.layer3_agent.tools.registry import create_tool_registry
from gangge.layer4_tools.mcp_client import DEFER_ENV, MCPClientManager, MCPServerConfig

MOCK_SERVER = '''
import json, sys

TOOLS = [
    {
        "name": "draw_circle",
        "description": "在 CAD 里画圆 circle",
        "inputSchema": {
            "type": "object",
            "properties": {"cx": {"type": "number"}, "cy": {"type": "number"}, "radius": {"type": "number"}},
            "required": ["cx", "cy", "radius"],
        },
    },
    {
        "name": "failing",
        "description": "总是失败的工具",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

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
        name = req["params"]["name"]
        if name == "draw_circle":
            a = req["params"].get("arguments") or {}
            text = "已绘制圆 半径={}".format(a.get("radius"))
            respond(req_id, {"content": [{"type": "text", "text": text}]})
        else:
            respond(req_id, {"content": [{"type": "text", "text": "图层名非法"}], "isError": True})
'''


@pytest.fixture
def manager(tmp_path: Path):
    script = tmp_path / "srv.py"
    script.write_text(MOCK_SERVER, encoding="utf-8")
    m = MCPClientManager([
        MCPServerConfig(
            name="mockcad",
            transport="stdio",
            command=sys.executable,
            args=[str(script)],
            timeout=20,
        )
    ])
    m.connect_all()
    assert m.get_all_tools(), "mock server 没连上"
    yield m
    m.disconnect_all()


@pytest.fixture
def registry():
    r = create_tool_registry(workspace="", load_plugins=False)
    r.set_profile("coding")
    return r


def _visible(registry) -> set[str]:
    return {d.name for d in registry.get_definitions(phase=registry.get_current_phase())}


def test_deferred_tools_are_registered_but_not_sent(manager, registry):
    names = register_mcp_tools(registry, manager, reveal=False)

    assert names == ["mockcad__draw_circle", "mockcad__failing"]
    assert all(registry.get(n) is not None for n in names), "必须真的注册进来，否则搜不到"
    assert not (set(names) & _visible(registry)), "延迟的工具不该随请求发送"


def test_reveal_true_sends_immediately(manager, registry):
    names = register_mcp_tools(registry, manager, reveal=True)
    assert set(names) <= _visible(registry)


def test_tool_search_reveals_by_server_name(manager, registry):
    names = register_mcp_tools(registry, manager, reveal=False)

    hits = registry.search("mockcad")
    assert {t.name for t in hits} == set(names), "按 server 名应该一次搜出它的全部工具"

    registry.unlock([t.name for t in hits])
    assert set(names) <= _visible(registry)


def test_tool_search_reveals_by_capability(manager, registry):
    register_mcp_tools(registry, manager, reveal=False)
    hits = registry.search("circle")
    assert "mockcad__draw_circle" in {t.name for t in hits}


def test_proxy_executes_through_manager(manager):
    tool = next(t for t in manager.get_all_tools() if t.name == "draw_circle")
    proxy = MCPToolProxy(tool, manager)

    result = asyncio.run(proxy.execute(cx=0, cy=0, radius=42))

    assert not result.is_error
    assert "半径=42" in result.output


def test_proxy_reports_mcp_error(manager):
    tool = next(t for t in manager.get_all_tools() if t.name == "failing")
    result = asyncio.run(MCPToolProxy(tool, manager).execute())
    assert result.is_error


def test_proxy_schema_is_passed_through(manager):
    tool = next(t for t in manager.get_all_tools() if t.name == "draw_circle")
    proxy = MCPToolProxy(tool, manager)

    assert proxy.name == "mockcad__draw_circle"
    assert proxy.input_schema["required"] == ["cx", "cy", "radius"]
    assert "mockcad" in proxy.tags, "server 名要进 tags，才能按 server 搜"
    assert proxy.description.startswith("[MCP:mockcad]")


def test_should_defer_auto_threshold(manager, monkeypatch):
    monkeypatch.delenv(DEFER_ENV, raising=False)

    manager.estimate_tokens = lambda: 1000        # type: ignore[method-assign]
    assert manager.should_defer(), "大 server 默认延迟"

    manager.estimate_tokens = lambda: 100         # type: ignore[method-assign]
    assert not manager.should_defer(), "小 server 直接给，省掉一次搜索往返"


def test_should_defer_env_override(manager, monkeypatch):
    manager.estimate_tokens = lambda: 9999        # type: ignore[method-assign]

    monkeypatch.setenv(DEFER_ENV, "none")
    assert not manager.should_defer()

    monkeypatch.setenv(DEFER_ENV, "all")
    assert manager.should_defer()


def test_prompt_hint_is_cheap_and_actionable(manager):
    hint = manager.prompt_hint()

    assert "mockcad" in hint and "2 个工具" in hint
    assert "tool_search" in hint, "必须告诉模型怎么取，否则等于藏起来"
    assert len(hint) < 220, f"索引行要保持廉价，实际 {len(hint)} 字符"


def test_hint_is_empty_without_tools(tmp_path):
    m = MCPClientManager([])
    assert m.prompt_hint() == ""
    assert m.estimate_tokens() == 0
