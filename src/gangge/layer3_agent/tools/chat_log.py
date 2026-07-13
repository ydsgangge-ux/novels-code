"""Chat log tool — 纯聊天场景下的工具出口。

当用户只是在聊天/提问/讨论，不需要读写文件或执行命令时，
Agent 调用此工具标记"本次为纯聊天"，既满足"必须使用工具"的
约束，又不会造成重复 token 消耗。

设计原则：
  - content 参数为可选，不传时只记录标题/时间戳（省 token）
  - 传 content 时才写入完整内容到 txt 文件
  - Agent 的文本回复本身已在对话历史中，不需要重复存储

生成的文件保存在 workspace/.gangge/chat_logs/ 目录下。
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

from gangge.layer3_agent.tools.base import BaseTool, ToolResult


class ChatLogTool(BaseTool):
    """纯聊天记录工具 — Agent 调用即算使用工具，零额外 token 开销。

    用途：纯聊天/Q&A/讨论/分析场景下，Agent 需要调用一个工具
    才能正常退出循环，此工具满足该约束。
    content 为可选参数，不传时不写入文件内容，节省 token。
    """

    @property
    def name(self) -> str:
        return "chat_log"

    @property
    def description(self) -> str:
        return (
            "纯聊天记录工具。当你的回复是纯文本（不需要读写文件、不需要执行命令、"
            "不需要搜索代码），比如回答问题、讨论方案、分析问题、评价、闲聊等场景时，"
            "调用此工具标记本次为纯聊天即可。"
            "title 参数写一个简短主题即可，content 可以不传（你的回复已在对话中）。"
            "⚠️ 如果你需要操作文件或执行命令，不要用这个工具，用对应的 write_file/bash 等。"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "聊天主题（简短描述，用于归档），如 'asyncio原理' '工具评价'",
                },
                "content": {
                    "type": "string",
                    "description": "聊天内容（可选，不传则只记录标题和时间。你的回复已在对话中，不需要重复写）",
                },
            },
            "required": ["title"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        title = kwargs.get("title", "").strip()
        content = kwargs.get("content", "").strip()

        if not title:
            return ToolResult(output="chat_log 需要 title 参数（聊天主题）", is_error=True)

        # 保存到 workspace/.gangge/chat_logs/
        workspace = os.environ.get("GANGGE_WORKSPACE", ".")
        log_dir = Path(workspace) / ".gangge" / "chat_logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        # 生成文件名：日期_主题.txt
        now = datetime.now()
        date_str = now.strftime("%Y%m%d_%H%M%S")
        # 清理 title 中的非法文件名字符
        safe_title = "_" + "".join(
            c for c in title[:30] if c.isalnum() or c in "_-— "
        ).strip()
        filename = f"chat_{date_str}{safe_title}.txt"
        log_path = log_dir / filename

        # 写入文件
        header = (
            f"{'='*60}\n"
            f"Gangge Code · 聊天记录\n"
            f"时间: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"主题: {title}\n"
            f"{'='*60}\n\n"
        )
        body = content if content else "（内容见对话历史，未额外记录）"
        log_path.write_text(header + body, encoding="utf-8")

        size_bytes = log_path.stat().st_size
        size_str = f"{size_bytes // 1024} KB" if size_bytes >= 1024 else f"{size_bytes} B"
        content_note = f" | 含内容" if content else " | 仅标题"
        return ToolResult(
            output=f"聊天记录已保存: {log_path} ({size_str}{content_note})",
            metadata={"chat_log_path": str(log_path)},
        )
