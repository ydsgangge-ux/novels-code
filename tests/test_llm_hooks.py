"""Request-level hooks, step lifecycle, and interrupted-turn repair."""

from __future__ import annotations

import asyncio
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
from gangge.runtime import events
from gangge.runtime.context import Context
from gangge.runtime.plugins.llm import LLMFactory


# ── a recording adapter: no network, records what it was asked ──


class RecordingLLM(BaseLLM):
    def __init__(self, text: str = "ok"):
        super().__init__(model="glm-4.7-flash")
        self.text = text
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages, tools=None, system="") -> LLMResponse:
        self.calls.append({
            "messages": messages,
            "tools": tools,
            "system": system,
            "model": self.model,
        })
        return LLMResponse(
            content=[ContentBlock(type=ContentType.TEXT, text=self.text)],
            stop_reason="end_turn",
            usage={"input": 10, "output": 5},
        )

    async def stream(self, messages, tools=None, system="") -> AsyncIterator[ContentBlock]:  # pragma: no cover
        response = await self.chat(messages, tools, system)
        for block in response.content:
            yield block

    async def close(self) -> None:
        return None


def _factory() -> tuple[LLMFactory, Context]:
    ctx = Context("llm-test")
    factory = LLMFactory(ctx, {})
    ctx.provide("llm", factory)
    return factory, ctx


async def _ask(factory: LLMFactory, adapter: RecordingLLM, **kwargs):
    return await factory.chat(
        messages=[Message(role=Role.USER, content=[ContentBlock(type=ContentType.TEXT, text="hi")])],
        system="base system",
        adapter=adapter,
        **kwargs,
    )


# ── llm/request waterfall ──────────────────────────────────────


class TestRequestWaterfall:
    @pytest.mark.asyncio
    async def test_listener_can_rewrite_the_system_prompt(self):
        factory, _ctx = _factory()
        adapter = RecordingLLM()

        def shorten(request, next):
            request["system"] = request["system"][:4]
            return next(request)

        factory.ctx.on(events.LLM_REQUEST, shorten)
        await _ask(factory, adapter)

        assert adapter.calls[0]["system"] == "base"

    @pytest.mark.asyncio
    async def test_listener_can_drop_the_tool_list(self):
        factory, _ctx = _factory()
        adapter = RecordingLLM()
        factory.ctx.on(
            events.LLM_REQUEST,
            lambda request, next: (request.update(tools=[]), next(request))[1],
        )
        await factory.chat(
            messages=[Message(role=Role.USER, content="hi")],
            tools=["a-tool"],
            system="",
            adapter=adapter,
        )
        assert adapter.calls[0]["tools"] == []

    @pytest.mark.asyncio
    async def test_listener_non_dict_return_is_ignored(self):
        """A sloppy listener must not break the call."""
        factory, _ctx = _factory()
        adapter = RecordingLLM()
        factory.ctx.on(events.LLM_REQUEST, lambda request, next: None)
        response = await _ask(factory, adapter)
        assert response.text == "ok"
        assert adapter.calls  # still called

    @pytest.mark.asyncio
    async def test_listener_receives_a_mutable_dict(self):
        factory, _ctx = _factory()
        adapter = RecordingLLM()
        seen: list[dict] = []
        factory.ctx.on(events.LLM_REQUEST, lambda request, next: (seen.append(dict(request)), next(request))[1])
        await _ask(factory, adapter)
        assert set(seen[0]) == {"messages", "tools", "system", "model"}

    @pytest.mark.asyncio
    async def test_no_listener_means_plain_call(self):
        factory, _ctx = _factory()
        adapter = RecordingLLM()
        await _ask(factory, adapter)
        assert adapter.calls[0]["system"] == "base system"


# ── model routing ──────────────────────────────────────────────


class TestModelRouting:
    @pytest.mark.asyncio
    async def test_requested_model_is_used_for_that_call(self):
        factory, _ctx = _factory()
        adapter = RecordingLLM()
        factory.ctx.on(
            events.LLM_REQUEST,
            lambda request, next: (request.update(model="cheap-model"), next(request))[1],
        )
        await _ask(factory, adapter)
        assert adapter.calls[0]["model"] == "cheap-model"

    @pytest.mark.asyncio
    async def test_adapter_model_is_restored_afterwards(self):
        """Routing must not permanently mutate a shared adapter."""
        factory, _ctx = _factory()
        adapter = RecordingLLM()
        factory.ctx.on(
            events.LLM_REQUEST,
            lambda request, next: (request.update(model="cheap-model"), next(request))[1],
        )
        await _ask(factory, adapter)
        assert adapter.model == "glm-4.7-flash"

    @pytest.mark.asyncio
    async def test_model_is_restored_even_when_the_call_fails(self):
        factory, _ctx = _factory()

        class Boom(RecordingLLM):
            async def chat(self, messages, tools=None, system=""):
                raise RuntimeError("network down")

        adapter = Boom()
        factory.ctx.on(
            events.LLM_REQUEST,
            lambda request, next: (request.update(model="cheap-model"), next(request))[1],
        )
        with pytest.raises(RuntimeError):
            await _ask(factory, adapter)
        assert adapter.model == "glm-4.7-flash"

    @pytest.mark.asyncio
    async def test_pinned_adapter_is_not_replaced_by_the_factory_default(self):
        factory, _ctx = _factory()
        mine = RecordingLLM()
        await _ask(factory, mine)
        assert len(mine.calls) == 1
        assert factory._default is None


# ── llm/response ──────────────────────────────────────────────


class TestResponseEvent:
    @pytest.mark.asyncio
    async def test_response_and_meta_are_emitted(self):
        factory, _ctx = _factory()
        adapter = RecordingLLM()
        seen: list[tuple] = []
        factory.ctx.on(events.LLM_RESPONSE, lambda *args: seen.append(args))

        await _ask(factory, adapter)

        assert len(seen) == 1
        response, meta = seen[0]
        assert response.text == "ok"
        assert "latency_ms" in meta
        assert meta["model"] == "glm-4.7-flash"

    @pytest.mark.asyncio
    async def test_stats_are_counted(self):
        factory, _ctx = _factory()
        adapter = RecordingLLM()
        await _ask(factory, adapter)
        await _ask(factory, adapter)
        assert factory.stats["calls"] == 2


# ── step lifecycle ────────────────────────────────────────────


class _ScriptedLLM(BaseLLM):
    """Replays responses; optionally explodes inside a tool."""

    def __init__(self, script: list[LLMResponse]):
        super().__init__(model="glm-4.7-flash")
        self.script = list(script)
        self._last: LLMResponse | None = None
        self.seen: list[list[Message]] = []

    async def chat(self, messages, tools=None, system="") -> LLMResponse:
        self.seen.append(list(messages))
        if self.script:
            self._last = self.script.pop(0)
        if self._last is None:
            self._last = LLMResponse(
                content=[ContentBlock(type=ContentType.TEXT, text="done")],
                stop_reason="end_turn",
            )
        return self._last

    async def stream(self, messages, tools=None, system=""):  # pragma: no cover
        response = await self.chat(messages, tools, system)
        for block in response.content:
            yield block

    async def close(self) -> None:
        return None


def _tool_use(name: str, call_id: str, args: dict) -> LLMResponse:
    return LLMResponse(
        content=[ContentBlock(
            type=ContentType.TOOL_USE,
            tool_name=name,
            tool_call_id=call_id,
            tool_input=args,
        )],
        stop_reason="tool_use",
    )


def _final(text: str) -> LLMResponse:
    return LLMResponse(
        content=[ContentBlock(type=ContentType.TEXT, text=text)], stop_reason="end_turn"
    )


def _loop(tmp_path, script=None, *, llm=None, tool_service=None, context=None):
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    if tool_service is None:
        from gangge.runtime.plugins.tools import ToolService

        ctx = context or Context("t")
        if not ctx.has("tools"):
            ctx.provide("tools", ToolService(ctx, registry, {}))
        tool_service = ctx.get("tools")

    async def _allow(_request):
        return PermissionDecision.ALLOW

    return AgenticLoop(
        llm=llm or _ScriptedLLM(script or []),
        tools=registry,
        permission_guard=PermissionGuard(ask_callback=_allow),
        config=LoopConfig(
            max_tool_rounds=4,
            workspace_dir=str(tmp_path),
            enable_summary_compression=False,
            enable_sliding_window=False,
            enable_lazy_project_map=False,
            enable_tool_result_truncation=False,
        ),
        tool_service=tool_service,
        context=context,
    )


class TestStepEvents:
    @pytest.mark.asyncio
    async def test_one_request_produces_one_step(self, tmp_path):
        ctx = Context("t")
        starts: list[dict] = []
        ends: list[dict] = []
        ctx.on(events.STEP_START, starts.append)
        ctx.on(events.STEP_END, ends.append)

        target = tmp_path / "a.txt"
        target.write_text("x", encoding="utf-8")
        loop = _loop(
            tmp_path,
            script=[_tool_use("read_file", "c1", {"path": str(target)}), _final("ok")],
            context=ctx,
        )
        await loop.run([Message(role=Role.USER, content="看一下 a.txt")])

        # The invariant that matters: every step that opened also closed.
        # The exact count depends on the loop's own nudges (a new task gets an
        # extra "please plan first" round), so it is not pinned here.
        assert len(starts) == len(ends)
        assert len(starts) >= 2
        assert [s["round"] for s in starts] == list(range(len(starts)))
        assert starts[0]["messages"] >= 1
        assert starts[0]["tools"] >= 1

    @pytest.mark.asyncio
    async def test_step_end_carries_the_outcome(self, tmp_path):
        ctx = Context("t")
        ends: list[dict] = []
        ctx.on(events.STEP_END, ends.append)

        loop = _loop(tmp_path, script=[_final("hello")], context=ctx)
        await loop.run([Message(role=Role.USER, content="打个招呼")])

        end = ends[0]
        assert end["stop_reason"] == "end_turn"
        assert end["text_chars"] == len("hello")
        assert end["tool_calls"] == 0
        assert end["latency_ms"] >= 0
        assert end["error"] == ""

    @pytest.mark.asyncio
    async def test_tool_count_is_reported_before_tools_run(self, tmp_path):
        ctx = Context("t")
        ends: list[dict] = []
        ctx.on(events.STEP_END, ends.append)

        target = tmp_path / "b.txt"
        target.write_text("y", encoding="utf-8")
        loop = _loop(
            tmp_path,
            script=[_tool_use("read_file", "c1", {"path": str(target)}), _final("ok")],
            context=ctx,
        )
        await loop.run([Message(role=Role.USER, content="看一下 b.txt")])
        assert ends[0]["tool_calls"] == 1
        assert ends[0]["stop_reason"] == "tool_use"

    @pytest.mark.asyncio
    async def test_timeout_closes_the_step_with_an_error(self, tmp_path):
        class Slow(_ScriptedLLM):
            async def chat(self, messages, tools=None, system=""):
                await asyncio.sleep(5)
                return _final("never")

        ctx = Context("t")
        ends: list[dict] = []
        ctx.on(events.STEP_END, ends.append)
        loop = _loop(tmp_path, llm=Slow([]), context=ctx)
        loop.config.llm_timeout = 0.05

        result = await loop.run([Message(role=Role.USER, content="hi")])

        assert result.completion_reason == "llm_timeout"
        assert ends and ends[0]["error"] == "timeout"


# ── host parity: the desktop worker's exact call shape ────────


class TestDesktopHostShape:
    """The PyQt worker cannot be imported here (no PyQt6), so its *calls* are
    reproduced instead. This test exists because `tool_pipeline(..., workspace=)`
    shipped without the parameter and only failed at GUI runtime."""

    @pytest.mark.asyncio
    async def test_desktop_wiring_shape(self, tmp_path):
        from gangge.runtime.pipeline import attach_llm, tool_pipeline

        registry = ToolRegistry()
        registry.register(ReadFileTool())

        tool_service, ctx = tool_pipeline(
            registry,
            workspace=str(tmp_path),
            max_chars=8000,
            repeat_limit=3,
        )
        llm_factory = attach_llm(ctx)

        try:
            assert ctx.data["workspace"] == str(tmp_path.resolve())
            assert llm_factory is not None

            loop = AgenticLoop(
                llm=_ScriptedLLM([_final("ok")]),
                tools=registry,
                permission_guard=PermissionGuard(ask_callback=_allow_all),
                config=LoopConfig(
                    max_tool_rounds=2,
                    workspace_dir=str(tmp_path),
                    enable_summary_compression=False,
                    enable_sliding_window=False,
                    enable_lazy_project_map=False,
                ),
                tool_service=tool_service,
                context=ctx,
                llm_factory=llm_factory,
            )
            result = await loop.run([Message(role=Role.USER, content="你好")])
            assert result.final_response
        finally:
            ctx.dispose()

    def test_no_nonexistent_keyword_arguments(self):
        """Guards the signature the GUI depends on."""
        import inspect

        from gangge.runtime.pipeline import tool_pipeline

        params = inspect.signature(tool_pipeline).parameters
        for name in ("workspace", "max_chars", "repeat_limit", "context"):
            assert name in params, f"tool_pipeline lost '{name}', the GUI passes it"


async def _allow_all(_request):
    return PermissionDecision.ALLOW


# ── interrupted turn repair ───────────────────────────────────


class TestMessageChainRepair:
    def _assistant_with_calls(self, *ids: str) -> Message:
        return Message(role=Role.ASSISTANT, content=[
            ContentBlock(
                type=ContentType.TOOL_USE,
                tool_name="bash",
                tool_call_id=call_id,
                tool_input={"command": "ls"},
            )
            for call_id in ids
        ])

    def test_missing_results_are_filled(self, tmp_path):
        loop = _loop(tmp_path)
        messages = [
            Message(role=Role.USER, content="go"),
            self._assistant_with_calls("c1", "c2"),
        ]
        assert loop._repair_message_chain(messages) == 2

        tail = messages[-1]
        assert tail.role == Role.TOOL
        assert {b.tool_call_id for b in tail.content} == {"c1", "c2"}
        assert all(b.is_error for b in tail.content)
        assert all("结果未知" in b.text for b in tail.content)

    def test_partial_results_are_merged_into_one_message(self, tmp_path):
        loop = _loop(tmp_path)
        messages = [
            Message(role=Role.USER, content="go"),
            self._assistant_with_calls("c1", "c2"),
            Message(role=Role.TOOL, content=[ContentBlock(
                type=ContentType.TOOL_RESULT, tool_call_id="c1", text="done"
            )]),
        ]
        assert loop._repair_message_chain(messages) == 1
        assert len(messages) == 3                       # merged, not appended
        assert len(messages[-1].content) == 2
        assert messages[-1].content[0].text == "done"   # real result kept

    def test_complete_chain_is_untouched(self, tmp_path):
        loop = _loop(tmp_path)
        messages = [
            Message(role=Role.USER, content="go"),
            self._assistant_with_calls("c1"),
            Message(role=Role.TOOL, content=[ContentBlock(
                type=ContentType.TOOL_RESULT, tool_call_id="c1", text="done"
            )]),
            Message(role=Role.ASSISTANT, content=[ContentBlock(type=ContentType.TEXT, text="fin")]),
        ]
        assert loop._repair_message_chain(messages) == 0
        assert len(messages) == 4

    def test_no_assistant_message_is_a_no_op(self, tmp_path):
        loop = _loop(tmp_path)
        messages = [Message(role=Role.USER, content="go")]
        assert loop._repair_message_chain(messages) == 0

    def test_repair_is_idempotent(self, tmp_path):
        loop = _loop(tmp_path)
        messages = [
            Message(role=Role.USER, content="go"),
            self._assistant_with_calls("c1"),
        ]
        assert loop._repair_message_chain(messages) == 1
        assert loop._repair_message_chain(messages) == 0

    def test_repair_emits_a_lifecycle_event(self, tmp_path):
        ctx = Context("t")
        seen: list[dict] = []
        ctx.on("turn/repaired", seen.append)
        loop = _loop(tmp_path, context=ctx)
        loop._repair_message_chain([
            Message(role=Role.USER, content="go"),
            self._assistant_with_calls("c1"),
        ])
        assert seen == [{"count": 1, "tools": ["bash"]}]

    @pytest.mark.asyncio
    async def test_cancelled_turn_leaves_a_valid_history(self, tmp_path):
        """The whole point: the *next* request must not 400."""
        from gangge.runtime.plugins.tools import ToolService

        class ExplodingRegistry(ToolRegistry):
            async def execute(self, name, input_data):
                raise asyncio.CancelledError()

        ctx = Context("t")
        registry = ExplodingRegistry()
        registry.register(ReadFileTool())
        ctx.provide("tools", ToolService(ctx, registry, {}))

        loop = _loop(
            tmp_path,
            script=[_tool_use("read_file", "c1", {"path": "whatever"})],
            tool_service=ctx.get("tools"),
            context=ctx,
        )
        messages = [Message(role=Role.USER, content="看一下")]
        with pytest.raises(asyncio.CancelledError):
            await loop.run(messages)

        # Every requested tool now has a result, so the history is sendable.
        answers = {
            b.tool_call_id for m in messages for b in m.content
            if b.type == ContentType.TOOL_RESULT
        }
        asks = {
            b.tool_call_id for m in messages for b in m.content
            if b.type == ContentType.TOOL_USE
        }
        assert asks <= answers
