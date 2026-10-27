"""End-to-end: a weak model's broken tool call, recovered inside the real loop.

This is the test that matters. It drives ``AgenticLoop`` with a stub model that
makes the exact mistakes GLM-Flash makes, and asserts the turn still finishes
with the work done — no user intervention, no wasted round on an exception.

If this test ever fails, the guardrails have stopped being wired into the loop.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

import pytest

from gangge.layer3_agent.loop import AgenticLoop, LoopConfig
from gangge.layer3_agent.tools.file_ops import ReadFileTool
from gangge.layer3_agent.tools.registry import ToolRegistry
from gangge.layer4_permission.guard import PermissionDecision, PermissionGuard
from gangge.layer5_llm.base import (
    BaseLLM,
    ContentBlock,
    ContentType,
    LLMResponse,
    Message,
    Role,
)
from gangge.runtime.pipeline import tool_pipeline


class ScriptedLLM(BaseLLM):
    """Replays a fixed list of responses, recording what it was told."""

    def __init__(self, script: list[LLMResponse]):
        super().__init__(model="glm-4.7-flash")
        self.script = list(script)
        self._last: LLMResponse | None = None
        self.seen_messages: list[list[Message]] = []

    async def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        system: str = "",
    ) -> LLMResponse:
        self.seen_messages.append([Message(role=m.role, content=list(m.content)) for m in messages])
        if self.script:
            self._last = self.script.pop(0)
        if self._last is None:
            self._last = _final("(empty script)")
        # The loop may make housekeeping calls of its own (compression, wrap-up),
        # so an exhausted script keeps replaying its last answer instead of
        # failing the test for the wrong reason.
        return self._last

    async def stream(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        system: str = "",
    ) -> AsyncIterator[ContentBlock]:
        response = await self.chat(messages, tools, system)
        for block in response.content:
            yield block

    async def close(self) -> None:
        return None

    def last_tool_text(self) -> str:
        """Everything the model was told through tool results, most recent last."""
        text = ""
        for message in self.seen_messages[-1] if self.seen_messages else []:
            for block in message.content:
                if block.type == ContentType.TOOL_RESULT:
                    text += block.text
        return text


def _tool_use(name: str, call_id: str, args: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        content=[
            ContentBlock(
                type=ContentType.TOOL_USE,
                tool_name=name,
                tool_call_id=call_id,
                tool_input=args,
            )
        ],
        stop_reason="tool_use",
    )


def _final(text: str) -> LLMResponse:
    return LLMResponse(
        content=[ContentBlock(type=ContentType.TEXT, text=text)],
        stop_reason="end_turn",
    )


@pytest.fixture
def harness(tmp_path):
    """A real loop with a real registry, wired through the plugin pipeline."""
    target = tmp_path / "answer.txt"
    target.write_text("STUBBORN_FACT_42", encoding="utf-8")

    registry = ToolRegistry()
    registry.register(ReadFileTool())
    service, ctx = tool_pipeline(registry, repeat_limit=2, max_chars=2000)

    async def _allow(_request):
        return PermissionDecision.ALLOW

    guard = PermissionGuard(ask_callback=_allow)
    config = LoopConfig(
        max_tool_rounds=4,
        workspace_dir=str(tmp_path),
        enable_summary_compression=False,
        enable_sliding_window=False,
        enable_lazy_project_map=False,
        enable_tool_result_truncation=False,
    )

    def build(llm: BaseLLM) -> AgenticLoop:
        return AgenticLoop(
            llm=llm,
            tools=registry,
            permission_guard=guard,
            config=config,
            tool_service=service,
        )

    yield build, target, ctx
    ctx.dispose()


class TestWeakModelRecovery:
    @pytest.mark.asyncio
    async def test_wrong_field_name_is_repaired_inside_the_loop(self, harness):
        build, target, _ctx = harness
        llm = ScriptedLLM([
            _tool_use("read_file", "c1", {"file_path": str(target)}),  # <-- "file_path"
            _final("读到了 STUBBORN_FACT_42"),
        ])
        result = await build(llm).run([Message(role=Role.USER, content="读一下 answer.txt")])

        assert result.completion_reason != "max_rounds"
        assert len(result.tool_executions) == 1
        execution = result.tool_executions[0]
        assert not execution.is_error, "the guard should have renamed the field"
        assert "STUBBORN_FACT_42" in execution.output
        assert "STUBBORN_FACT_42" in result.final_response

    @pytest.mark.asyncio
    async def test_bad_call_is_explained_and_the_retry_succeeds(self, harness):
        build, target, _ctx = harness
        llm = ScriptedLLM([
            _tool_use("read_file", "c1", {}),                          # missing "path"
            _tool_use("read_file", "c2", {"path": str(target)}),        # corrected
            _final("完成"),
        ])
        result = await build(llm).run([Message(role=Role.USER, content="读一下 answer.txt")])

        first, second = result.tool_executions[0], result.tool_executions[1]
        assert first.is_error
        assert "必填" in first.output
        assert "正确示例" in first.output
        assert not second.is_error
        assert "STUBBORN_FACT_42" in second.output

    @pytest.mark.asyncio
    async def test_repeated_identical_call_gets_nudged(self, harness):
        build, target, _ctx = harness
        args = {"path": str(target)}
        llm = ScriptedLLM([
            _tool_use("read_file", "c1", args),
            _tool_use("read_file", "c2", args),
            _final("已经读过了"),
        ])
        result = await build(llm).run([Message(role=Role.USER, content="读一下")])

        nudged = result.tool_executions[1]
        assert "完全相同" in nudged.output
        assert "STUBBORN_FACT_42" in nudged.output

    @pytest.mark.asyncio
    async def test_the_loop_still_finishes_when_the_model_keeps_failing(self, harness):
        build, _target, _ctx = harness
        llm = ScriptedLLM([
            _tool_use("read_file", "c1", {}),
            _tool_use("read_file", "c2", {}),
            _final("我放弃了"),
        ])
        result = await build(llm).run([Message(role=Role.USER, content="读一下")])

        # Every failure produced a readable error result, never an exception,
        # so the turn ended normally instead of dying.
        assert len(result.tool_executions) == 2
        assert all(e.is_error for e in result.tool_executions)
        assert result.completion_reason != "max_rounds"
