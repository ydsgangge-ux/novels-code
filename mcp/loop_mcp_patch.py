"""
Gangge Code — AgenticLoop MCP 集成补丁
src/gangge/layer3_agent/loop_mcp_patch.py

这不是替换你原有的 loop.py，而是展示需要在 loop.py 里做的改动。
改动点共 4 处，用 # ── PATCH ── 标记。
"""

# ════════════════════════════════════════════════════════
# 改动 1: 在 AgenticLoop.__init__ 里初始化 MCPClientManager
# ════════════════════════════════════════════════════════

# 原来的 __init__ 大概是这样：
#
# class AgenticLoop:
#     def __init__(self, llm, tool_registry, permission_guard, ...):
#         self.llm = llm
#         self.tool_registry = tool_registry
#         ...
#
# 改成：

from pathlib import Path
from gangge.layer4_tools.mcp_client import MCPClientManager

class AgenticLoopMCPMixin:
    """
    把这个 Mixin 加到你的 AgenticLoop 继承链里，
    或者直接把下面几个方法复制进你的 AgenticLoop。
    """

    def _init_mcp(self, workspace: Path):
        """在 __init__ 末尾调用这个方法"""
        config_path = workspace / ".gangge" / "mcp_servers.json"
        self.mcp_manager = MCPClientManager.from_config_file(str(config_path))
        self.mcp_manager.connect_all()

        # 把 MCP 工具数量打印出来，让用户知道加载了什么
        tools = self.mcp_manager.get_all_tools()
        if tools:
            names = [t.full_name for t in tools]
            print(f"[MCP] 已加载 {len(tools)} 个外部工具: {', '.join(names)}")

    def _cleanup_mcp(self):
        """在 loop 结束 / __del__ 里调用"""
        if hasattr(self, "mcp_manager"):
            self.mcp_manager.disconnect_all()


# ════════════════════════════════════════════════════════
# 改动 2: 构建发给 LLM 的 tools 列表时，追加 MCP 工具
# ════════════════════════════════════════════════════════

# 原来大概是：
#
# def _build_tools_for_llm(self) -> list[dict]:
#     return self.tool_registry.get_definitions()   # 你的 8 个内置工具
#
# 改成：

def _build_tools_for_llm(self) -> list[dict]:
    # 内置工具
    definitions = self.tool_registry.get_definitions()

    # ── PATCH: 追加 MCP 工具 ──────────────────────────
    if hasattr(self, "mcp_manager"):
        mcp_defs = self.mcp_manager.build_tool_definitions()
        definitions = definitions + mcp_defs
    # ────────────────────────────────────────────────────

    return definitions


# ════════════════════════════════════════════════════════
# 改动 3: 工具调用分发时，识别 MCP 工具并转发
# ════════════════════════════════════════════════════════

# 原来大概是：
#
# def _dispatch_tool(self, tool_name: str, tool_input: dict) -> str:
#     handler = self.tool_registry.get(tool_name)
#     if handler is None:
#         return f"未知工具: {tool_name}"
#     return handler(tool_input)
#
# 改成：

def _dispatch_tool(self, tool_name: str, tool_input: dict) -> str:
    # ── PATCH: MCP 工具名包含双下划线，如 autocad__draw_circle ──
    if "__" in tool_name and hasattr(self, "mcp_manager"):
        return self.mcp_manager.call_tool(tool_name, tool_input)
    # ────────────────────────────────────────────────────────────

    # 原有内置工具路由不变
    handler = self.tool_registry.get(tool_name)
    if handler is None:
        return f"未知工具: {tool_name}"
    return handler(tool_input)


# ════════════════════════════════════════════════════════
# 改动 4: 流式输出时，MCP 工具调用显示特殊前缀
# ════════════════════════════════════════════════════════

# 在你渲染工具调用的地方加一行判断，让用户能区分
# 内置工具和 MCP 外部工具：
#
# 原来：
#   print(f"  ▶ {tool_name}({args_preview})")
#
# 改成：

def _render_tool_call(self, tool_name: str, tool_input: dict):
    args_preview = str(tool_input)[:80]

    # ── PATCH ──────────────────────────────────────────
    if "__" in tool_name:
        server, name = tool_name.split("__", 1)
        print(f"  🔌 [MCP:{server}] {name}({args_preview})")
    else:
        print(f"  ▶ {tool_name}({args_preview})")
    # ────────────────────────────────────────────────────
