"""让模型在项目需要时自己新增 MCP server。

MCP 的能力应该是"按需长出来"的：写小说不需要 Blender，做 3D 场景时才需要。
没有这个工具，用户得自己找包、写配置、重启；而这三件事模型都能做。

风险控制：
- 它会启动一个新进程，所以参数里必须给出完整 ``command``；
- ``mcp_add_server`` 不在 profile 的 core 集合里，权限守卫对未知工具默认 ASK，
  用户在执行前会看到完整的命令和参数；
- 写入的是**项目级**配置（``scope=project``），不会污染其它项目。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from gangge.layer3_agent.tools.base import BaseTool, ToolResult
from gangge.layer3_agent.tools.mcp_proxy import register_mcp_tools
from gangge.layer4_tools.mcp_client import (
    SUPPORTED_TRANSPORTS,
    MCPClientManager,
    MCPServerConfig,
    upsert_server,
)

logger = logging.getLogger(__name__)


class MCPAddServerTool(BaseTool):
    """新增一个 MCP server 并立刻把它的工具挂上。"""

    def __init__(self, manager: MCPClientManager, registry: Any, workspace_dir: str = "."):
        self._manager = manager
        self._registry = registry
        self._workspace = workspace_dir or "."

    @property
    def name(self) -> str:
        return "mcp_add_server"

    @property
    def description(self) -> str:
        return (
            "新增一个 MCP server（外部工具服务）：写入项目配置、立即连接、注册它的工具。"
            "当项目需要当前没有的能力时使用，例如 3D 建模（uvx mcp-for-blender）、"
            "GitHub 操作（npx -y @modelcontextprotocol/server-github）。"
            "会启动一个新进程，属敏感操作，需要用户确认；已存在同名 server 时会被覆盖。"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "server 名，会作为工具名前缀（如 blender → blender__generate_3d），不能含双下划线",
                },
                "command": {
                    "type": "string",
                    "description": "启动命令，如 uvx / npx / node / python",
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "命令参数，如 ['-y', '@modelcontextprotocol/server-memory']",
                },
                "env": {
                    "type": "object",
                    "description": "附加环境变量，如 {\"GITHUB_TOKEN\": \"...\"}",
                },
                "transport": {
                    "type": "string",
                    "enum": list(SUPPORTED_TRANSPORTS),
                    "default": "stdio",
                },
                "url": {
                    "type": "string",
                    "description": "transport=sse 时的服务地址",
                },
                "timeout": {"type": "integer", "default": 60},
                "scope": {
                    "type": "string",
                    "enum": ["project", "global"],
                    "default": "project",
                    "description": "project=只在本项目生效；global=所有项目可用",
                },
            },
            "required": ["name", "command"],
        }

    @property
    def tags(self) -> tuple[str, ...]:
        return ("mcp", "add", "server", "install", "外部工具", "扩展", "新增")

    async def execute(
        self,
        name: str = "",
        command: str = "",
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        transport: str = "stdio",
        url: str = "",
        timeout: int = 60,
        scope: str = "project",
        **_: Any,
    ) -> ToolResult:
        name = (name or "").strip()
        if not name:
            return ToolResult(output="[错误] name 不能为空", is_error=True)
        if "__" in name:
            return ToolResult(
                output="[错误] name 不能包含 '__'，它是工具名前缀的分隔符（server__tool）",
                is_error=True,
            )
        if transport not in SUPPORTED_TRANSPORTS:
            return ToolResult(
                output=f"[错误] 不支持的 transport: {transport}（可选: {', '.join(SUPPORTED_TRANSPORTS)}）",
                is_error=True,
            )
        if transport == "stdio" and not command:
            return ToolResult(output="[错误] stdio 需要 command", is_error=True)
        if transport == "sse" and not url:
            return ToolResult(output="[错误] sse 需要 url", is_error=True)

        entry: dict[str, Any] = {
            "name": name,
            "transport": transport,
            "enabled": True,
            "timeout": int(timeout or 60),
        }
        if transport == "stdio":
            entry["command"] = command
            entry["args"] = [str(a) for a in (args or [])]
        else:
            entry["url"] = url
        if env:
            entry["env"] = {str(k): str(v) for k, v in env.items()}

        try:
            config_path = upsert_server(entry, self._workspace, scope)
        except Exception as e:
            return ToolResult(output=f"[错误] 写入配置失败: {e}", is_error=True)

        cfg = MCPServerConfig(
            name=name,
            transport=transport,
            command=command,
            args=[str(a) for a in (args or [])],
            env={str(k): str(v) for k, v in (env or {}).items()},
            url=url,
            enabled=True,
            timeout=int(timeout or 60),
        )
        try:
            new_names = await asyncio.to_thread(self._manager.add_server, cfg)
        except Exception as e:
            return ToolResult(
                output=f"[错误] 配置已写入 {config_path}，但连接失败: {e}",
                is_error=True,
            )

        register_mcp_tools(self._registry, self._manager, reveal=False)

        if not new_names:
            return ToolResult(
                output=(
                    f"已新增 MCP server '{name}'（写入 {config_path}），但连上后没有拉到任何工具。\n"
                    "可能是命令写错了，或该 server 需要额外参数/凭据。"
                ),
                is_error=True,
            )

        listed = "\n".join(f"  - {n}" for n in new_names[:20])
        more = f"\n  …… 另有 {len(new_names) - 20} 个" if len(new_names) > 20 else ""
        return ToolResult(output=(
            f"已新增 MCP server '{name}'（写入 {config_path}），连上 {len(new_names)} 个工具：\n"
            f"{listed}{more}\n\n"
            f"这些工具默认未展开（省 token）。下一步用 tool_search(\"{name}\") 或按能力关键词搜索，"
            "搜到后即可直接调用。"
        ))
