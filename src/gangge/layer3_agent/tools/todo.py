"""TodoWrite tool — maintain a structured task list across loop rounds.

This is the single most important tool for keeping the agent on track.
Claude Code uses the same pattern: the model writes todos, the system
injects them back every round so the model never loses track of where it is.

Design principles:
- Model calls TodoWrite to create/update tasks
- Loop automatically injects current todo state as a system reminder
- No LLM thinking required to "remember" what to do next
"""

from __future__ import annotations

import json
import logging
from typing import Any

from gangge.layer3_agent.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


# ── Global todo state (shared across tool instance and loop) ──

class TodoState:
    """Singleton-like state holder for the current todo list.

    The loop reads from this every round to inject into the system prompt.
    The model writes to this via the TodoWrite tool.
    The runtime auto-advances state when tools succeed — model doesn't need to update manually.
    """

    def __init__(self):
        self._todos: list[dict] = []
        # Each todo: {"id": str, "content": str, "status": "pending"|"in_progress"|"completed", "priority": "high"|"medium"|"low"}
        self._productive_hits: dict[str, int] = {}  # todo_id -> count of productive tool hits

    def update(self, todos: list[dict]) -> None:
        """Replace the entire todo list (Claude Code style — no partial updates)."""
        self._todos = todos
        # Clean up hit counters for completed todos
        completed_ids = {t["id"] for t in todos if t.get("status") == "completed"}
        for tid in list(self._productive_hits):
            if tid in completed_ids:
                del self._productive_hits[tid]

    def get_all(self) -> list[dict]:
        """Return current todo list."""
        return list(self._todos)

    def get_pending(self) -> list[dict]:
        """Return only non-completed todos."""
        return [t for t in self._todos if t.get("status") != "completed"]

    def get_current(self) -> dict | None:
        """Return the first in_progress task, or the first pending task."""
        for t in self._todos:
            if t.get("status") == "in_progress":
                return t
        for t in self._todos:
            if t.get("status") == "pending":
                return t
        return None

    # Minimum productive tool hits before auto-completing a todo.
    # 1 hit = old behavior (too eager, causes false completions).
    # 2+ hits = requires the model to have done substantial work on this todo.
    AUTO_COMPLETE_MIN_HITS = 2

    def auto_advance(self, tool_name: str, tool_input: dict, success: bool) -> bool:
        """Auto-advance todo state based on tool execution result.

        Accumulation-based logic: a todo must receive at least
        AUTO_COMPLETE_MIN_HITS productive tool calls before it is
        auto-completed. This prevents "one write_file → done" false
        completions.

        Rules:
        - Each successful productive tool call increments a per-todo counter
        - Only when counter >= AUTO_COMPLETE_MIN_HITS does the todo auto-complete
        - When model explicitly sets status=completed via TodoWrite, reset counter
        - If current todo is completed → auto-start next pending todo

        Returns True if state was changed.
        """
        if not self._todos:
            return False

        # Only auto-advance on productive tool calls (not read/explore tools)
        productive_tools = {
            "write_file", "edit_file", "bash", "lint_check",
            "novel_init", "novel_setup", "novel_outline",
            "novel_chapter_outlines", "novel_write_chapter",
            "novel_audit", "novel_revise", "novel_new_arc",
            "novel_export", "novel_import",
            "create_tool",
        }

        if tool_name not in productive_tools:
            return False

        if not success:
            return False

        changed = False
        current = self.get_current()

        if current and current.get("status") == "in_progress":
            tid = current["id"]
            self._productive_hits[tid] = self._productive_hits.get(tid, 0) + 1
            hits = self._productive_hits[tid]

            if hits >= self.AUTO_COMPLETE_MIN_HITS:
                # Enough productive work done — auto-complete
                for t in self._todos:
                    if t["id"] == tid:
                        t["status"] = "completed"
                        changed = True
                        logger.info(f"[TodoState] Auto-completed (after {hits} productive hits): {t['content']}")
                        break
            else:
                logger.info(f"[TodoState] Productive hit {hits}/{self.AUTO_COMPLETE_MIN_HITS} for: {current['content']}")

        # Auto-start next pending task
        if changed:
            for t in self._todos:
                if t.get("status") == "pending":
                    t["status"] = "in_progress"
                    logger.info(f"[TodoState] Auto-started: {t['content']}")
                    break

        return changed

    def format_for_injection(self) -> str:
        """Format the todo list for injection into the system prompt.

        Returns an empty string if no todos exist.
        """
        if not self._todos:
            return ""

        lines = ["## 当前任务列表\n"]
        for t in self._todos:
            status_icon = {
                "completed": "✅",
                "in_progress": "▶️",
                "pending": "⬜",
            }.get(t.get("status", "pending"), "⬜")
            content = t.get("content", "")
            lines.append(f"{status_icon} {content}")

        pending = self.get_pending()
        if pending:
            lines.append(f"\n剩余 {len(pending)} 个任务未完成。")
        else:
            lines.append("\n所有任务已完成！")

        current = self.get_current()
        if current:
            lines.append(f"\n**当前任务**: {current['content']}")

        return "\n".join(lines)

    def clear(self) -> None:
        """Clear all todos."""
        self._todos = []


# Module-level singleton — the loop and tool share this instance
_global_state = TodoState()


def get_todo_state() -> TodoState:
    """Get the global todo state instance."""
    return _global_state


class TodoWriteTool(BaseTool):
    """Create or update the task list.

    Call this tool to plan out your work. The system will automatically
    show you the current task list every round so you never lose track.

    <good-example>
    User: "Create a FastAPI project"
    → TodoWrite with todos: [
        {"id": "1", "content": "Create project structure", "status": "pending"},
        {"id": "2", "content": "Write main.py with FastAPI app", "status": "pending"},
        {"id": "3", "content": "Add API routes", "status": "pending"},
        {"id": "4", "content": "Run and verify", "status": "pending"}
      ]
    Then execute each step, updating status as you go.
    </good-example>

    <bad-example>
    Don't call TodoWrite for trivial single-step tasks like "create hello.txt".
    Just call write_file directly.
    </bad-example>
    """

    def __init__(self, state: TodoState | None = None):
        self._state = state or _global_state

    @property
    def name(self) -> str:
        return "TodoWrite"

    @property
    def description(self) -> str:
        return (
            "创建或更新任务列表。用于多步骤任务的规划和跟踪。"
            "调用后系统会在每轮自动显示当前任务进度，确保不会遗漏步骤。"
            "每次调用需提供完整的任务列表（不支持部分更新）。"
            "简单任务（如创建单个文件）不需要调用此工具。"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "完整的任务列表。每次调用提供全部任务，不支持部分更新。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "任务唯一标识，如 '1', '2', '3'",
                            },
                            "content": {
                                "type": "string",
                                "description": "任务描述，如 '创建 main.py 入口文件'",
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                                "description": "任务状态：pending=待做, in_progress=进行中, completed=已完成",
                            },
                        },
                        "required": ["id", "content", "status"],
                    },
                },
            },
            "required": ["todos"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        todos = kwargs.get("todos")
        if not todos or not isinstance(todos, list):
            return ToolResult(
                output="❌ TodoWrite 需要 todos 参数（任务列表数组）",
                is_error=True,
            )

        # Validate each todo
        for i, todo in enumerate(todos):
            if not isinstance(todo, dict):
                return ToolResult(
                    output=f"❌ 第 {i+1} 个任务格式错误，必须是对象",
                    is_error=True,
                )
            if "id" not in todo or "content" not in todo or "status" not in todo:
                return ToolResult(
                    output=f"❌ 第 {i+1} 个任务缺少必要字段 (id/content/status)",
                    is_error=True,
                )
            if todo["status"] not in ("pending", "in_progress", "completed"):
                return ToolResult(
                    output=f"❌ 第 {i+1} 个任务状态无效: {todo['status']}，必须是 pending/in_progress/completed",
                    is_error=True,
                )

        # Update state
        self._state.update(todos)

        # Format confirmation
        total = len(todos)
        completed = sum(1 for t in todos if t["status"] == "completed")
        in_progress = sum(1 for t in todos if t["status"] == "in_progress")
        pending = sum(1 for t in todos if t["status"] == "pending")

        summary = f"任务列表已更新：共 {total} 个任务"
        if completed:
            summary += f"，✅ {completed} 已完成"
        if in_progress:
            summary += f"，▶️ {in_progress} 进行中"
        if pending:
            summary += f"，⬜ {pending} 待做"

        # Show current task
        current = self._state.get_current()
        if current:
            summary += f"\n\n当前任务: {current['content']}"

        return ToolResult(output=summary)
