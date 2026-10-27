"""把 MCP 工具注册成 ToolRegistry 里的委托工具。

为什么不再在 loop / pipeline 里特判 ``"__" in name``：

- 只有进了 registry，MCP 工具才能复用 ``tool_search`` 的延迟揭示、参数校验、
  权限守卫、结果裁剪这一整套现成机制；
- 默认**不可见** = 不随请求发送 = 不占 token；模型 ``tool_search`` 搜到之后
  才展开完整 schema。大 server（GitHub 26 个工具 ≈ 4.4k tokens）常驻成本直接归零。

执行仍然走 ``MCPClientManager.call_tool``，只是换了个入口——同步阻塞的 MCP 调用
放在线程池里跑，不能占住事件循环（否则流式输出和 UI 会卡死）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from gangge.layer3_agent.tools.base import BaseTool, ToolResult
from gangge.layer4_tools.mcp_client import MCPClientManager, MCPTool

logger = logging.getLogger(__name__)


class MCPToolProxy(BaseTool):
    """一个 MCP 工具的本地替身：schema 原样转发，执行转发给 manager。"""

    def __init__(self, tool: MCPTool, manager: MCPClientManager):
        self._tool = tool
        self._manager = manager

    @property
    def name(self) -> str:
        return self._tool.full_name

    @property
    def description(self) -> str:
        return f"[MCP:{self._tool.server_name}] {self._tool.description}"

    @property
    def input_schema(self) -> dict[str, Any]:
        return self._tool.input_schema or {"type": "object", "properties": {}}

    @property
    def tags(self) -> tuple[str, ...]:
        # server 名进 tags，模型搜 "blender" 就能命中这个 server 的全部工具
        return (self._tool.server_name, "mcp", "external")

    async def execute(self, **kwargs: Any) -> ToolResult:
        output = await asyncio.to_thread(self._manager.call_tool, self.name, kwargs)
        text = str(output)
        return ToolResult(output=text, is_error=text.startswith("[错误]"))


def register_mcp_tools(
    registry: Any,
    manager: MCPClientManager,
    *,
    reveal: bool = False,
) -> list[str]:
    """把 manager 拉到的工具注册进 registry。

    ``reveal=False``（默认）：注册但不可见，等 ``tool_search`` 揭示。
    ``reveal=True``：立即可见——工具很少的 server 直接给，省掉一次搜索往返。
    """
    names: list[str] = []
    for tool in manager.get_all_tools():
        if tool.full_name in registry:
            continue
        registry.register(MCPToolProxy(tool, manager))
        names.append(tool.full_name)

    if reveal and names:
        registry.reveal(names, reason="mcp-direct")
    if names:
        logger.info(
            "[MCP] 注册 %d 个工具（%s）: %s",
            len(names),
            "直接可见" if reveal else "延迟，等 tool_search 揭示",
            ", ".join(names),
        )
    return names
