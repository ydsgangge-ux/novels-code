"""Streaming: aggregation, delta batching, and the loop's incremental output."""

from __future__ import annotations

import pytest

from gangge.layer3_agent.loop import _DeltaBatcher
from gangge.layer5_llm.base import ContentBlock, ContentType, Message, Role
from support.fake_llm import RecordingEmitter, ScriptedLLM, call, say


def _messages() -> list[Message]:
    return [Message(role=Role.USER, content="hi")]


# ── Aggregation ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_stream_assembles_text_and_forwards_deltas():
    llm = ScriptedLLM([say("hello world", usage={"input_tokens": 7, "output_tokens": 2})], chunk_size=3)
    seen: list[str] = []

    async def on_delta(block: ContentBlock) -> None:
        seen.append(block.text)

    response = await llm.chat_stream(_messages(), on_delta=on_delta)

    assert response.text == "hello world"
    assert response.stop_reason == "end_turn"
    assert response.usage == {"input_tokens": 7, "output_tokens": 2}
    assert len(seen) > 1, "deltas must arrive in pieces, not as the whole answer"


@pytest.mark.asyncio
async def test_chat_stream_puts_thinking_before_text_and_keeps_tool_calls():
    llm = ScriptedLLM(
        [
            type("T", (), {
                "text": "done",
                "thinking": "hmm",
                "tool_calls": [("read_file", {"path": "a.py"})],
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "stop_reason": "tool_use",
            })()
        ]
    )
    response = await llm.chat_stream(_messages())

    kinds = [b.type for b in response.content]
    assert kinds == [ContentType.THINKING, ContentType.TEXT, ContentType.TOOL_USE]
    assert response.stop_reason == "tool_use"
    assert response.tool_calls[0].name == "read_file"


@pytest.mark.asyncio
async def test_chat_stream_without_callback_still_returns_response():
    llm = ScriptedLLM([say("abc")])
    response = await llm.chat_stream(_messages())
    assert response.text == "abc"


# ── Batching ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_batcher_coalesces_small_deltas():
    emitter = RecordingEmitter()
    batcher = _DeltaBatcher(emitter, min_chars=48)

    for char in "abcdefghij":
        await batcher(ContentBlock(type=ContentType.TEXT, text=char))

    assert emitter.texts == [], "nothing should be flushed below the threshold"
    await batcher.flush()
    assert emitter.text == "abcdefghij"
    assert batcher.deltas == 10
    assert batcher.emitted_chars == 10


@pytest.mark.asyncio
async def test_batcher_flushes_on_newline():
    emitter = RecordingEmitter()
    batcher = _DeltaBatcher(emitter, min_chars=200)

    await batcher(ContentBlock(type=ContentType.TEXT, text="line one\n"))
    assert emitter.text == "line one\n"

    await batcher(ContentBlock(type=ContentType.TEXT, text="tail"))
    assert emitter.text == "line one\n"  # still buffered
    await batcher.flush()
    assert emitter.text == "line one\ntail"


@pytest.mark.asyncio
async def test_batcher_coalesces_reasoning_into_one_block():
    """Reasoning is one block per model call, never one per token."""
    emitter = RecordingEmitter()
    batcher = _DeltaBatcher(emitter, min_chars=4)

    for fragment in ["思", "考", "很", "久"]:
        await batcher(ContentBlock(type=ContentType.THINKING, text=fragment))
    assert emitter.blocks == [], "reasoning must not be emitted token by token"

    await batcher(ContentBlock(type=ContentType.TEXT, text="answer"))
    await batcher.flush()

    assert [b.type for b in emitter.blocks] == [ContentType.THINKING, ContentType.TEXT]
    assert emitter.blocks[0].text == "思考很久"
    # Reasoning must not count as "the answer was already shown", or the
    # duplicate-printing guard would swallow the real answer.
    assert batcher.thinking_chars == 4
    assert batcher.emitted_chars == len("answer")


@pytest.mark.asyncio
async def test_batcher_flushes_reasoning_at_end_of_call():
    emitter = RecordingEmitter()
    batcher = _DeltaBatcher(emitter)

    await batcher(ContentBlock(type=ContentType.THINKING, text="只在思考"))
    await batcher.flush()

    assert [b.text for b in emitter.blocks] == ["只在思考"]


@pytest.mark.asyncio
async def test_batcher_flushes_text_before_passing_through_a_tool_block():
    emitter = RecordingEmitter()
    batcher = _DeltaBatcher(emitter)

    await batcher(ContentBlock(type=ContentType.TEXT, text="part"))
    await batcher(ContentBlock(type=ContentType.TOOL_USE, tool_name="read_file"))

    assert [b.type for b in emitter.blocks] == [ContentType.TEXT, ContentType.TOOL_USE]
    assert emitter.texts[0] == "part"


# ── End to end through the loop ────────────────────────────────


@pytest.mark.asyncio
async def test_loop_streams_incrementally_and_does_not_duplicate(tmp_path):
    from gangge.layer3_agent.loop import AgenticLoop, LoopConfig
    from gangge.layer3_agent.tools.base import BaseTool, ToolResult
    from gangge.layer3_agent.tools.registry import ToolRegistry
    from gangge.layer4_permission.guard import PermissionGuard

    class EchoTool(BaseTool):
        @property
        def name(self):
            return "echo"

        @property
        def description(self):
            return "回显文本"

        @property
        def input_schema(self):
            return {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            }

        async def execute(self, **kwargs):
            return ToolResult(output=f"echo:{kwargs['text']}")

    registry = ToolRegistry()
    registry.register(EchoTool())

    # >100 chars so the loop treats it as a real answer (a short reply to a
    # brand-new task triggers the "plan first / act now" nudge, by design).
    answer = (
        "这是一段需要被流式输出的回答。" * 8
    )
    llm = ScriptedLLM(
        [call("echo", text="hi"), say(answer)],
        chunk_size=2,
    )
    config = LoopConfig(workspace_dir=str(tmp_path), max_tool_rounds=3)
    loop = AgenticLoop(
        llm=llm,
        tools=registry,
        permission_guard=PermissionGuard(),
        config=config,
    )
    emitter = RecordingEmitter()
    loop.set_stream_callback(emitter)

    messages = [Message(role=Role.USER, content="打个招呼")]
    result = await loop.run(messages)

    assert llm.stream_calls == 2, "the loop must use the streaming path"
    assert llm.chat_calls == 0
    assert result.completion_reason in ("model_end_turn", "all_todos_done")
    # The answer appears exactly once, even though it was streamed and also
    # present in the assembled response.
    assert emitter.text.count(answer) == 1
    # And it arrived in more than one piece.
    assert len([t for t in emitter.texts if t and t in answer or answer.startswith(t)]) > 1


@pytest.mark.asyncio
async def test_loop_falls_back_to_chat_when_adapter_cannot_stream(tmp_path):
    from gangge.layer3_agent.loop import AgenticLoop, LoopConfig
    from gangge.layer3_agent.tools.registry import ToolRegistry
    from gangge.layer4_permission.guard import PermissionGuard

    # The Q&A fast-path: one model call, no tools, so the fallback is isolated
    # from the "act, don't chat" nudging of the tool loop.
    llm = ScriptedLLM([say("递归就是函数调用自己。")], supports_streaming=False)
    loop = AgenticLoop(
        llm=llm,
        tools=ToolRegistry(),
        permission_guard=PermissionGuard(),
        config=LoopConfig(workspace_dir=str(tmp_path), max_tool_rounds=2),
    )
    emitter = RecordingEmitter()
    loop.set_stream_callback(emitter)

    await loop.run([Message(role=Role.USER, content="解释一下递归")])

    assert llm.chat_calls == 1
    assert llm.stream_calls == 0
    assert "递归" in emitter.text
