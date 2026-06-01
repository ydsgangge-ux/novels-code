"""Ask User tool — pause the loop and ask the user for input."""

from __future__ import annotations

from typing import Any, Callable, Awaitable

from gangge.layer3_agent.tools.base import BaseTool, ToolResult


class AskUserTool(BaseTool):
    """Ask the user a question and wait for their response.

    This tool is special: when called, the agentic loop pauses,
    displays the question to the user, waits for input,
    and injects the answer back as a tool_result.
    """

    def __init__(self, ask_callback: Callable[[str], Awaitable[str]] | None = None):
        self._ask_callback = ask_callback

    @property
    def name(self) -> str:
        return "ask_user"

    @property
    def description(self) -> str:
        return (
            "当需要用户提供信息才能继续时调用此工具。"
            "比如：需要仓库地址、密码、选择方案、确认操作等。"
            "调用后循环会暂停等待用户输入，用户回答后继续执行。"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "要问用户的问题",
                },
            },
            "required": ["question"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        question = kwargs.get("question") or kwargs.get("message") or ""
        if not question:
            return ToolResult(
                output=f"❌ ask_user 缺少问题参数。收到的参数: {list(kwargs.keys())}。请使用 question=\"你的问题\"。",
                is_error=True,
            )
        if self._ask_callback:
            answer = await self._ask_callback(question)
        else:
            answer = ""
        return ToolResult(output=answer if answer else "(用户未提供输入)")
