"""``tool_search`` — the meta-tool that reveals other tools.

Why this exists instead of a bigger always-on tool list:

* Every extra tool definition costs prompt tokens *and* attention. A 35-tool
  list makes a small model measurably worse at picking the right one.
* The old answer was a keyword-scored "phase" gate in the loop: unlock bash
  when a todo contains "测试", unlock write when the directory is empty. Those
  heuristics are guesses, and they were wrong often enough to matter.
* Searching is not a guess. The model states what capability it wants, gets the
  schemas back, and can search again.

Unlocking is permanent for the session — a tool that disappears again would
make the model re-discover it (or hallucinate its name) on the next round.
"""

from __future__ import annotations

import json
from typing import Any

from gangge.layer3_agent.tools.base import BaseTool, ToolResult

_MAX_DESC_CHARS = 120


class ToolSearchTool(BaseTool):
    """Find tools by capability; revealed tools become available immediately."""

    def __init__(self, registry: Any):
        self.registry = registry

    @property
    def name(self) -> str:
        return "tool_search"

    @property
    def description(self) -> str:
        return (
            "按能力搜索可用工具。当你需要当前列表里没有的能力时（联网搜索、抓取网页、"
            "浏览器自动化、代码符号查找、图片识别/生成、小说写作、创建自定义工具等），"
            "先用本工具搜索。搜到的工具会立即出现在你的工具列表中，随后可直接调用。"
            "query 传自然语言或关键词（如 'web 搜索'、'浏览器截图'、'大纲'）。"
            "query 留空则列出所有当前未显示的工具。"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "能力描述或关键词；留空列出所有未显示工具",
                    "default": "",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回多少个工具（默认 8）",
                    "default": 8,
                },
            },
            "required": [],
        }

    @property
    def tags(self) -> tuple[str, ...]:
        return ("搜索", "查找", "发现", "search", "find", "tools", "capability")

    async def execute(self, query: str = "", limit: int = 8, **_: Any) -> ToolResult:
        limit = max(1, min(int(limit or 8), 20))
        results = self.registry.search(query, limit=limit)
        if not results:
            available = ", ".join(
                sorted(t.name for t in self.registry.list_tools())[:40]
            )
            return ToolResult(
                output=(
                    f"没有找到与 {query!r} 匹配的工具。\n"
                    f"当前已注册的工具名（供参考）: {available}\n"
                    "可以换一个关键词再搜，或直接用现有工具完成任务。"
                ),
                is_error=True,
            )

        newly = self.registry.unlock([t.name for t in results])
        lines = []
        if query:
            lines.append(f"找到 {len(results)} 个与 {query!r} 相关的工具：")
        else:
            lines.append(f"当前未显示的工具共 {len(results)} 个：")

        for tool in results:
            desc = " ".join((tool.description or "").split())[:_MAX_DESC_CHARS]
            required = list((tool.input_schema or {}).get("required") or [])
            params = f" 必填参数: {', '.join(required)}" if required else " 无需必填参数"
            marker = "（已可用）" if tool.name in newly else ""
            lines.append(f"- {tool.name}{marker}: {desc}{params}")

        if newly:
            lines.append("")
            lines.append(
                "以上新工具已加入你的工具列表，现在可以直接调用它们。"
                "不需要再搜索同一个关键词。"
            )
        else:
            lines.append("")
            lines.append("这些工具本来就已经可用，直接调用即可。")
        return ToolResult(
            output="\n".join(lines),
            metadata={"unlocked": newly, "tools": [t.name for t in results]},
        )
