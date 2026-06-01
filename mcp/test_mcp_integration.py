"""
Gangge Code — MCP 集成测试脚本
tests/test_mcp_integration.py

运行方式：
    python tests/test_mcp_integration.py

不依赖真实 CAD 软件，用 mock MCP Server 验证整个链路。
"""

import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# ─────────────────────────────────────────────────────────────
# 1. Mock MCP Server（用 Python 模拟一个极简 stdio MCP Server）
#    实际使用时替换成真实的 autocad-mcp 进程
# ─────────────────────────────────────────────────────────────

MOCK_SERVER_CODE = """
import json, sys

TOOLS = [
    {
        "name": "draw_circle",
        "description": "在 CAD 中画一个圆",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cx": {"type": "number", "description": "圆心 X 坐标"},
                "cy": {"type": "number", "description": "圆心 Y 坐标"},
                "radius": {"type": "number", "description": "半径"}
            },
            "required": ["cx", "cy", "radius"]
        }
    },
    {
        "name": "create_layer",
        "description": "创建 CAD 图层",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "图层名称"},
                "color": {"type": "string", "description": "图层颜色（如 red、blue）"}
            },
            "required": ["name"]
        }
    }
]

def respond(req_id, result):
    msg = json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result})
    sys.stdout.write(msg + "\\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    method = req.get("method", "")
    req_id = req.get("id", 1)

    if method == "initialize":
        respond(req_id, {"protocolVersion": "2024-11-05", "capabilities": {}})

    elif method == "tools/list":
        respond(req_id, {"tools": TOOLS})

    elif method == "tools/call":
        name = req["params"]["name"]
        args = req["params"]["arguments"]
        if name == "draw_circle":
            respond(req_id, {"content": [{"type": "text", "text":
                f"✅ 圆已绘制: 圆心({args['cx']}, {args['cy']}), 半径={args['radius']}"}]})
        elif name == "create_layer":
            respond(req_id, {"content": [{"type": "text", "text":
                f"✅ 图层已创建: {args['name']} (颜色: {args.get('color', '默认')})"}]})
        else:
            respond(req_id, {"content": [{"type": "text", "text": f"❌ 未知工具: {name}"}]})
"""


# ─────────────────────────────────────────────────────────────
# 2. 测试用例
# ─────────────────────────────────────────────────────────────

def test_stdio_connection():
    """测试 stdio 传输的连接和工具列表拉取"""
    print("\n── 测试 1: stdio 连接 ──────────────────────")

    # 写 mock server 脚本到临时文件
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py",
                                     delete=False, encoding="utf-8") as f:
        f.write(MOCK_SERVER_CODE)
        server_path = f.name

    # 导入 MCPClientManager
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from gangge.layer4_tools.mcp_client import MCPClientManager, MCPServerConfig

    config = MCPServerConfig(
        name="mock_cad",
        transport="stdio",
        command=sys.executable,
        args=[server_path],
    )
    manager = MCPClientManager([config])
    manager.connect_all()

    tools = manager.get_all_tools()
    print(f"  拉取到 {len(tools)} 个工具:")
    for t in tools:
        print(f"    - {t.full_name}: {t.description}")

    assert len(tools) == 2, f"期望 2 个工具，实际 {len(tools)}"
    assert tools[0].full_name == "mock_cad__draw_circle"
    assert tools[1].full_name == "mock_cad__create_layer"
    print("  ✅ 连接和工具列表测试通过")
    return manager, server_path


def test_tool_call(manager):
    """测试工具调用"""
    print("\n── 测试 2: 工具调用 ────────────────────────")

    # 画圆
    result = manager.call_tool("mock_cad__draw_circle", {
        "cx": 100.0, "cy": 100.0, "radius": 50.0
    })
    print(f"  draw_circle 返回: {result}")
    assert "圆已绘制" in result

    # 创建图层
    result = manager.call_tool("mock_cad__create_layer", {
        "name": "线束层", "color": "red"
    })
    print(f"  create_layer 返回: {result}")
    assert "图层已创建" in result

    # 调用不存在的工具
    result = manager.call_tool("nonexistent__tool", {})
    print(f"  未知工具返回: {result}")
    assert "未找到" in result

    print("  ✅ 工具调用测试通过")


def test_tool_definitions(manager):
    """测试 LLM tool definitions 格式"""
    print("\n── 测试 3: LLM tool definitions 格式 ──────")

    defs = manager.build_tool_definitions()
    print(f"  生成 {len(defs)} 个 tool definitions:")
    for d in defs:
        print(f"    - {d['name']}: {d['description'][:60]}")
        # 验证格式符合 Anthropic/OpenAI 规范
        assert "name" in d
        assert "description" in d
        assert "input_schema" in d
        assert d["name"].startswith("mock_cad__")
        assert "[MCP:mock_cad]" in d["description"]

    print("  ✅ tool definitions 格式测试通过")


def test_config_file_loading():
    """测试从 JSON 配置文件加载"""
    print("\n── 测试 4: 配置文件加载 ────────────────────")

    with tempfile.TemporaryDirectory() as tmpdir:
        gangge_dir = Path(tmpdir) / ".gangge"
        gangge_dir.mkdir()
        config_file = gangge_dir / "mcp_servers.json"
        config_file.write_text(json.dumps({
            "servers": [
                {
                    "name": "test_server",
                    "transport": "stdio",
                    "command": "nonexistent_command",
                    "args": [],
                    "enabled": False  # disabled，不会真正连接
                }
            ]
        }), encoding="utf-8")

        sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
        from gangge.layer4_tools.mcp_client import MCPClientManager

        manager = MCPClientManager.from_config_file(str(config_file))
        assert len(manager.configs) == 1
        assert manager.configs[0].name == "test_server"
        assert manager.configs[0].enabled == False
        print("  配置加载成功，disabled server 跳过连接")

    print("  ✅ 配置文件加载测试通过")


def test_dispatch_routing():
    """
    测试 AgenticLoop 工具分发路由逻辑（不依赖完整 loop）
    验证 __ 命名约定能正确区分内置工具和 MCP 工具
    """
    print("\n── 测试 5: 工具路由逻辑 ────────────────────")

    test_cases = [
        ("bash",                   False, "内置工具"),
        ("read_file",              False, "内置工具"),
        ("autocad__draw_circle",   True,  "MCP 工具"),
        ("freecad__create_sketch", True,  "MCP 工具"),
        ("mock_cad__create_layer", True,  "MCP 工具"),
    ]
    for tool_name, expected_is_mcp, label in test_cases:
        is_mcp = "__" in tool_name
        status = "✅" if is_mcp == expected_is_mcp else "❌"
        print(f"  {status} '{tool_name}' → {label} (is_mcp={is_mcp})")
        assert is_mcp == expected_is_mcp

    print("  ✅ 工具路由逻辑测试通过")


# ─────────────────────────────────────────────────────────────
# 3. 运行所有测试
# ─────────────────────────────────────────────────────────────

def main():
    print("=" * 50)
    print("Gangge Code MCP 集成测试")
    print("=" * 50)

    try:
        manager, server_path = test_stdio_connection()
        test_tool_call(manager)
        test_tool_definitions(manager)
        manager.disconnect_all()
        Path(server_path).unlink(missing_ok=True)

        test_config_file_loading()
        test_dispatch_routing()

        print("\n" + "=" * 50)
        print("✅ 全部测试通过！MCP 集成就绪。")
        print("=" * 50)
        print("\n下一步：")
        print("  1. 复制 mcp_client.py 到 src/gangge/layer4_tools/")
        print("  2. 把 loop_mcp_patch.py 里的 4 处改动合进 loop.py")
        print("  3. 复制 mcp_servers.json 到项目的 .gangge/ 目录")
        print("  4. 启用你要用的 CAD server，填好路径")
        print("  5. gangge '用 AutoCAD 画一个线束连接器示意图'")

    except AssertionError as e:
        print(f"\n❌ 测试失败: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
