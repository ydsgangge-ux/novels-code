"""General QA tool — 通用知识问答工具（防止小模型偷懒）。

仅用于与当前项目无关的通用知识问答（如"Python 的 GIL 是什么"）。
涉及项目代码的问题必须走 read_file/grep 等工具调查。

设计原则：
  - content 参数为可选，不传时只记录标题（省 token）
  - Agent 的文本回复本身已在对话历史中，不需要重复存储
  - 生成 txt 文件保存在 workspace/.gangge/chat_logs/ 下
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

from gangge.layer3_agent.tools.base import BaseTool, ToolResult


class ChatLogTool(BaseTool):
    """通用知识问答工具 — 仅用于非项目相关的纯知识问答。

    ⚠️ 严格限制：只有当问题与当前项目/代码/文件完全无关时才使用。
    涉及项目的任何问题（分析代码、解释实现、调试问题）都必须走
    read_file/grep 等工具先调查，不允许凭记忆回答。
    """

    @property
    def name(self) -> str:
        return "chat_log"

    @property
    def description(self) -> str:
        return (
            "通用知识问答记录工具。仅当问题与当前项目完全无关时使用，"
            "例如'Python GIL 是什么'、'HTTP 状态码含义'等通用知识。\n"
            "title 参数写一个简短主题即可，content 可以不传（你的回复已在对话中）。\n"
            "⚠️ 严禁用于：分析项目代码、解释当前项目实现、调试项目问题——"
            "这些场景必须先 read_file/grep 调查，不允许凭记忆回答。\n"
            "⚠️ 如果你需要操作文件或执行命令，不要用这个工具。"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "问答主题（简短描述），如 'asyncio原理' 'HTTP状态码'",
                },
                "content": {
                    "type": "string",
                    "description": "回答内容（可选，不传则只记录标题和时间）",
                },
            },
            "required": ["title"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        title = kwargs.get("title", "").strip()
        content = kwargs.get("content", "").strip()

        if not title:
            return ToolResult(output="chat_log 需要 title 参数（问答主题）", is_error=True)

        # 保存到 workspace/.gangge/chat_logs/
        workspace = os.environ.get("GANGGE_WORKSPACE", ".")
        log_dir = Path(workspace) / ".gangge" / "chat_logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        # 生成文件名：日期_主题.txt
        now = datetime.now()
        date_str = now.strftime("%Y%m%d_%H%M%S")
        safe_title = "_" + "".join(
            c for c in title[:30] if c.isalnum() or c in "_-— "
        ).strip()
        filename = f"chat_{date_str}{safe_title}.txt"
        log_path = log_dir / filename

        header = (
            f"{'='*60}\n"
            f"Gangge Code · 通用问答记录\n"
            f"时间: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"主题: {title}\n"
            f"{'='*60}\n\n"
        )
        body = content if content else "（内容见对话历史，未额外记录）"
        log_path.write_text(header + body, encoding="utf-8")

        size_bytes = log_path.stat().st_size
        size_str = f"{size_bytes // 1024} KB" if size_bytes >= 1024 else f"{size_bytes} B"
        content_note = " | 含内容" if content else " | 仅标题"
        return ToolResult(
            output=f"问答记录已保存: {log_path} ({size_str}{content_note})",
            metadata={"chat_log_path": str(log_path)},
        )
