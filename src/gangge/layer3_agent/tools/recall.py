"""Recall conversation tool — 搜索历史对话记录。

让 Agent 能主动搜索之前的对话历史，解决多轮/跨会话记忆问题。
数据源是 SessionDB 的 turns 表（FTS5 全文搜索 / LIKE 降级）。

db_path 通过环境变量 GANGGE_SESSION_DB 传入（由 GUI/CLI 启动时设置）。
如果环境变量未设置或 DB 不存在，工具返回提示而非报错。
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

from gangge.layer3_agent.tools.base import BaseTool, ToolResult


class RecallConversationTool(BaseTool):
    """搜索历史对话记录。

    当用户提到"之前/上次/前面"等涉及历史对话的内容时，
    Agent 调用此工具搜索相关历史轮次。
    """

    @property
    def name(self) -> str:
        return "recall_conversation"

    @property
    def description(self) -> str:
        return (
            "搜索历史对话记录。当用户提到'之前'、'上次'、'前面'、'我们讨论过的'等涉及历史对话的内容时调用此工具。\n"
            "输入搜索关键词，返回匹配的历史对话轮次摘要（用户问了什么、用了什么工具、结果如何）。\n"
            "⚠️ 这是搜索你自己的历史对话，不是搜索网页。如果用户问的是通用知识，不需要用这个工具。"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词，如 '画图 prompt'、'asyncio timeout'、'ComfyUI 参数'",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回多少条结果（默认 5）",
                    "default": 5,
                },
            },
            "required": ["query"],
        }

    def _get_db_path(self) -> str | None:
        """从环境变量获取 DB 路径。"""
        path = os.environ.get("GANGGE_SESSION_DB", "")
        if path and os.path.exists(path):
            return path
        return None

    async def execute(self, **kwargs: Any) -> ToolResult:
        query = kwargs.get("query", "").strip()
        limit = int(kwargs.get("limit", 5))

        if not query:
            return ToolResult(output="需要 query 参数（搜索关键词）", is_error=True)

        db_path = self._get_db_path()
        if not db_path:
            return ToolResult(
                output="无法搜索历史对话：未找到会话数据库。\n"
                "（提示：此功能需要在 GUI/CLI 启动时设置 GANGGE_SESSION_DB 环境变量）"
            )

        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            results = self._search(conn, query, limit)
            conn.close()
        except Exception as e:
            return ToolResult(output=f"搜索失败: {type(e).__name__}: {e}", is_error=True)

        if not results:
            return ToolResult(output=f"未找到与 '{query}' 相关的历史对话。")

        lines = [f"找到 {len(results)} 条相关历史对话:\n"]
        for i, r in enumerate(results, 1):
            turn_num = r.get("turn_num", "?")
            created = (r.get("created_at", "") or "")[:19]
            user_text = (r.get("user_text", "") or "")[:120]
            summary = (r.get("summary", "") or "")[:200]
            tools_json = r.get("tools_used", "[]")

            try:
                tools_list = json.loads(tools_json) if tools_json else []
                tool_names = [t.get("name", "?") for t in tools_list]
                tools_str = ", ".join(tool_names) if tool_names else "无"
            except Exception:
                tools_str = "无"

            lines.append(f"{i}. [第{turn_num}轮 | {created}]")
            lines.append(f"   用户: {user_text}{'...' if len(r.get('user_text', '')) > 120 else ''}")
            lines.append(f"   工具: {tools_str}")
            lines.append(f"   结果: {summary}{'...' if len(r.get('summary', '')) > 200 else ''}")
            lines.append("")

        lines.append("提示: 如果需要某条对话的完整细节，可以查看该轮的详细消息。")
        return ToolResult(output="\n".join(lines))

    def _search(self, conn: sqlite3.Connection, query: str, limit: int) -> list[dict]:
        """执行搜索：FTS5 优先，LIKE 降级。"""
        # 检查 FTS5 是否可用
        try:
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='turns_fts'")
            has_fts = cur.fetchone() is not None
        except Exception:
            has_fts = False

        if has_fts:
            try:
                sql = (
                    "SELECT t.id, t.session_id, t.turn_num, t.user_text, t.summary, "
                    "t.tools_used, t.assistant_text, t.created_at "
                    "FROM turns_fts f JOIN turns t ON t.id = f.rowid "
                    "WHERE turns_fts MATCH ? "
                    "ORDER BY t.id DESC LIMIT ?"
                )
                rows = conn.execute(sql, (query, limit)).fetchall()
                return [dict(r) for r in rows]
            except Exception:
                pass

        # LIKE 降级
        pattern = f"%{query}%"
        sql = (
            "SELECT id, session_id, turn_num, user_text, summary, tools_used, assistant_text, created_at "
            "FROM turns WHERE (user_text LIKE ? OR summary LIKE ? OR assistant_text LIKE ? OR tools_used LIKE ?) "
            "ORDER BY id DESC LIMIT ?"
        )
        rows = conn.execute(sql, (pattern, pattern, pattern, pattern, limit)).fetchall()
        return [dict(r) for r in rows]
