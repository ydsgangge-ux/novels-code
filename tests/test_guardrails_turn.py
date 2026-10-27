"""Turn-level guardrails: next-step hints, tool-name repair, premature stops."""

from __future__ import annotations

import pytest

from gangge.layer3_agent.tools.file_ops import ReadFileTool, WriteFileTool
from gangge.layer3_agent.tools.registry import ToolRegistry
from gangge.runtime import events
from gangge.runtime.context import Context
from gangge.runtime.pipeline import tool_pipeline
from gangge.runtime.plugins.next_step import NextStepHints
from gangge.runtime.plugins.schema_guard import SchemaGuard
from gangge.runtime.plugins.stop_guard import StopGuard
from gangge.runtime.seams import ToolOutcome, TurnDirective


class _Stub:
    def __init__(self, tool: str, arguments: dict | None = None):
        self.tool = tool
        self.arguments = dict(arguments or {})
        self.vetoed = False
        self.veto_reason = ""
        self.metadata: dict = {}

    def veto(self, reason: str) -> "_Stub":
        self.vetoed = True
        self.veto_reason = reason
        return self


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(ReadFileTool())
    reg.register(WriteFileTool())
    return reg


# ── Next-step hints ────────────────────────────────────────────


class TestNextStepHints:
    def _outcome(
        self, tool: str, rendered: str = "content", is_error: bool = False, **meta
    ) -> ToolOutcome:
        return ToolOutcome(
            tool=tool, rendered=rendered, is_error=is_error, metadata=dict(meta)
        )

    def test_hint_is_appended_to_the_first_read(self):
        hints = NextStepHints()
        out = self._outcome("read_file")
        hints.annotate(out, lambda o: o)
        assert "↳" in out.rendered
        assert "edit_file" in out.rendered

    def test_second_use_of_the_same_tool_is_silent(self):
        hints = NextStepHints()
        first = self._outcome("read_file")
        hints.annotate(first, lambda o: o)
        second = self._outcome("read_file")
        hints.annotate(second, lambda o: o)
        assert "↳" not in second.rendered

    def test_a_different_tool_gets_its_own_hint(self):
        hints = NextStepHints()
        a = self._outcome("read_file")
        b = self._outcome("grep")
        hints.annotate(a, lambda o: o)
        hints.annotate(b, lambda o: o)
        assert "↳" in a.rendered and "↳" in b.rendered

    def test_errors_always_earn_a_hint(self):
        hints = NextStepHints()
        out = self._outcome("read_file", is_error=True)
        hints.annotate(out, lambda o: o)
        assert "报错" in out.rendered or "重试" in out.rendered

    def test_error_hint_is_still_only_once_per_tool(self):
        hints = NextStepHints()
        first = self._outcome("read_file", is_error=True)
        hints.annotate(first, lambda o: o)
        second = self._outcome("read_file", is_error=True)
        hints.annotate(second, lambda o: o)

        assert first.rendered.endswith("不要用完全相同的参数重复调用。")
        assert second.rendered == "content"

    def test_original_content_is_kept(self):
        hints = NextStepHints()
        out = self._outcome("read_file", rendered="def login(): ...")
        hints.annotate(out, lambda o: o)
        assert out.rendered.startswith("def login(): ...")

    def test_on_error_only_disables_success_hints(self):
        hints = NextStepHints({"on_error_only": True})
        out = self._outcome("read_file")
        hints.annotate(out, lambda o: o)
        assert "↳" not in out.rendered

    def test_hints_are_clipped(self):
        hints = NextStepHints({"max_hint_chars": 10})
        out = self._outcome("read_file")
        hints.annotate(out, lambda o: o)
        assert len(out.rendered.split("↳ ")[1]) <= 10

    def test_tools_without_conventional_successor_stay_silent(self):
        hints = NextStepHints()
        out = self._outcome("TodoWrite", rendered="1 task")
        hints.annotate(out, lambda o: o)
        assert out.rendered == "1 task"

    def test_turn_start_resets_the_slate(self):
        hints = NextStepHints()
        first = self._outcome("read_file")
        hints.annotate(first, lambda o: o)
        hints.reset()
        again = self._outcome("read_file")
        hints.annotate(again, lambda o: o)
        assert "↳" in again.rendered

    def test_upstream_hint_takes_the_slot(self):
        hints = NextStepHints()
        out = self._outcome("read_file", hinted=True)
        hints.annotate(out, lambda o: o)
        assert "↳" not in out.rendered


# ── Hallucinated tool names ────────────────────────────────────


class TestUnknownToolNames:
    def _guard(self) -> SchemaGuard:
        ctx = Context("t")
        registry = _registry()
        ctx.provide("tools", type("S", (), {
            "get": registry.get,
            "list_tools": registry.list_tools,
        })())
        return SchemaGuard(ctx, {"coerce": True})

    def _check(self, name: str):
        guard = self._guard()
        invoke = _Stub(name, {"path": "a.py"})
        return guard.check(invoke, lambda i: i)

    @pytest.mark.parametrize("wrong,expected", [
        ("read", "read_file"),
        ("readfile", "read_file"),
        ("ReadFile", "read_file"),
        ("writ_file", "write_file"),
    ])
    def test_near_miss_is_suggested(self, wrong, expected):
        result = self._check(wrong)
        assert result.vetoed
        assert expected in result.veto_reason

    def test_message_says_the_tool_does_not_exist(self):
        result = self._check("read")
        assert "不存在" in result.veto_reason

    def test_message_includes_a_callable_example_when_unambiguous(self):
        result = self._check("read")
        assert "正确示例" in result.veto_reason
        assert "read_file(" in result.veto_reason

    def test_message_tells_it_not_to_repeat(self):
        result = self._check("read")
        assert "不要重复调用" in result.veto_reason

    def test_unrelated_name_is_left_to_the_registry(self):
        """An MCP tool registered later must not be blocked here."""
        result = self._check("autocad__draw_circle")
        assert not result.vetoed


# ── Premature stop ─────────────────────────────────────────────


def _state(**overrides):
    base = {
        "round": 2,
        "text": "我已经分析完了，你可以在 auth.py 加一个判断。",
        "user_task": "帮我修复 login 里的 bug",
        "is_empty_dir": False,
        "has_modified_files": False,
        "verified_after_write": False,
        "tool_history": [{"name": "read_file", "is_error": False}],
        "files_touched": [],
        "todo_all": [],
        "todo_pending": [],
    }
    base.update(overrides)
    return base


class TestStopGuard:
    def test_read_only_turn_is_refused(self):
        verdict = StopGuard().decide(_state())
        assert isinstance(verdict, TurnDirective)
        assert verdict.action == "continue"
        assert "没有修改任何文件" in verdict.message

    def test_no_tools_at_all_is_refused(self):
        verdict = StopGuard().decide(_state(tool_history=[]))
        assert verdict.action == "continue"
        assert "还没有调用任何工具" in verdict.message

    def test_pure_question_is_allowed_to_stop(self):
        verdict = StopGuard().decide(_state(user_task="解释一下这个函数在干什么"))
        assert verdict is None

    def test_ambiguous_request_is_allowed_to_stop(self):
        verdict = StopGuard().decide(_state(user_task="看看这个项目"))
        assert verdict is None

    def test_modified_but_unverified_turn_is_refused(self):
        verdict = StopGuard().decide(_state(
            has_modified_files=True,
            files_touched=["auth.py"],
            tool_history=[{"name": "edit_file", "is_error": False}],
        ))
        assert verdict.action == "continue"
        assert "还没有验证" in verdict.message
        assert "auth.py" in verdict.message

    def test_verified_turn_is_allowed_to_stop(self):
        verdict = StopGuard().decide(_state(
            has_modified_files=True,
            verified_after_write=True,
            tool_history=[{"name": "edit_file", "is_error": False},
                          {"name": "bash", "is_error": False}],
        ))
        assert verdict is None

    def test_each_condition_fires_only_once(self):
        guard = StopGuard()
        assert guard.decide(_state()).action == "continue"
        assert guard.decide(_state()) is None

    def test_nudges_are_capped(self):
        guard = StopGuard({"max_nudges": 1})
        assert guard.decide(_state()).action == "continue"
        # The action condition is spent, and the verdict cap is reached, so the
        # verify condition must not fire either.
        assert guard.decide(_state(has_modified_files=True, files_touched=["a.py"])) is None

    def test_reset_starts_a_new_turn(self):
        guard = StopGuard()
        guard.decide(_state())
        guard.reset()
        assert guard.decide(_state()).action == "continue"

    def test_round_going_backwards_resets(self):
        guard = StopGuard()
        guard.decide(_state(round=5))
        assert guard.decide(_state(round=1)).action == "continue"

    def test_require_action_can_be_disabled(self):
        guard = StopGuard({"require_action": False, "require_verification": False})
        assert guard.decide(_state()) is None

    def test_require_verification_can_be_disabled(self):
        guard = StopGuard({"require_verification": False})
        verdict = guard.decide(_state(
            has_modified_files=True,
            verified_after_write=False,
            tool_history=[{"name": "edit_file", "is_error": False}],
        ))
        assert verdict is None


# ── Tool-name repair inside the pipeline ───────────────────────


class TestPipelineUnknownTool:
    @pytest.mark.asyncio
    async def test_hallucinated_tool_returns_a_suggestion(self):
        registry = _registry()
        service, ctx = tool_pipeline(registry, prune=False, repeat=False, stop_guard=False)
        try:
            result = await service.execute("read", {"path": "a.py"})
            assert result.is_error
            assert "read_file" in result.output
        finally:
            ctx.dispose()


# ── The loop must actually ask ─────────────────────────────────


class TestLoopConsultsTurnStopping:
    @pytest.fixture
    def rig(self, tmp_path):
        from tests.test_loop_guardrails import ScriptedLLM, _final, _tool_use

        target = tmp_path / "answer.txt"
        target.write_text("ORIGINAL", encoding="utf-8")

        registry = ToolRegistry()
        registry.register(ReadFileTool())
        registry.register(WriteFileTool())
        service, ctx = tool_pipeline(registry, prune=False, repeat=False, next_step=False)

        seen: list[dict] = []

        def recorder(state):
            seen.append(dict(state))
            return None

        ctx.on(events.AGENT_TURN_STOPPING, recorder)

        from gangge.layer3_agent.loop import AgenticLoop, LoopConfig
        from gangge.layer4_permission.guard import PermissionDecision, PermissionGuard
        from gangge.layer5_llm.base import Message, Role

        async def _allow(_request):
            return PermissionDecision.ALLOW

        config = LoopConfig(
            max_tool_rounds=6,
            workspace_dir=str(tmp_path),
            enable_summary_compression=False,
            enable_sliding_window=False,
            enable_lazy_project_map=False,
            enable_tool_result_truncation=False,
        )

        def build(script):
            return AgenticLoop(
                llm=ScriptedLLM(script),
                tools=registry,
                permission_guard=PermissionGuard(ask_callback=_allow),
                config=config,
                tool_service=service,
                context=ctx,
            )

        yield build, ctx, seen, target, _final, _tool_use, Message, Role, tmp_path
        ctx.dispose()

    @pytest.mark.asyncio
    async def test_state_is_passed_to_listeners(self, rig):
        build, _ctx, seen, target, _final, _tool_use, Message, Role, _tmp = rig
        llm_script = [
            _tool_use("read_file", "c1", {"path": str(target)}),
            _final("读完了，没什么要改的。"),
        ]
        # Not a pure question ("解释/为什么"): those short-circuit to a single
        # answer before the tool loop starts, so turn-stopping never runs.
        await build(llm_script).run(
            [Message(role=Role.USER, content="帮我看看 answer.txt 的内容")]
        )

        assert seen, "the loop never consulted agent/turn-stopping"
        state = seen[-1]
        for key in ("round", "text", "user_task", "has_modified_files",
                    "verified_after_write", "tool_history", "files_touched"):
            assert key in state
        assert state["has_modified_files"] is False

    @pytest.mark.asyncio
    async def test_a_listener_can_force_another_round(self, rig):
        build, ctx, _seen, target, _final, _tool_use, Message, Role, _tmp = rig

        forced = {"done": False}

        def force_once(state):
            if not forced["done"]:
                forced["done"] = True
                return TurnDirective.keep_going("请继续，别停下。", "测试强制继续")
            return None

        ctx.on(events.AGENT_TURN_STOPPING, force_once)

        llm = build([
            _tool_use("read_file", "c1", {"path": str(target)}),
            _final("先这样吧。"),          # would exit here
            _final("这次真的完成了。"),     # only reached if the stop was refused
        ])
        result = await llm.run(
            [Message(role=Role.USER, content="帮我看看 answer.txt 的内容")]
        )

        assert forced["done"] is True
        assert "这次真的完成了" in result.final_response


# ── The booted profile must wire all of it ─────────────────────


class TestBootstrapWiring:
    @pytest.fixture
    def runtime(self, tmp_path):
        from gangge.runtime.boot import bootstrap

        rt = bootstrap(
            "standard",
            workspace=tmp_path,
            bindings={"tools": {"novel": False, "comfyui": False, "load_plugins": False}},
        )
        yield rt
        rt.dispose()

    def test_all_turn_events_have_listeners(self, runtime):
        assert runtime.ctx.listeners(events.AGENT_TURN_STOPPING)
        assert runtime.ctx.listeners(events.TURN_START)
        assert runtime.ctx.listeners(events.TURN_END)

    @pytest.mark.asyncio
    async def test_timid_model_is_pushed_to_act(self, tmp_path, runtime):
        from gangge.layer3_agent.loop import AgenticLoop, LoopConfig
        from gangge.layer4_permission.guard import PermissionDecision, PermissionGuard
        from gangge.layer5_llm.base import Message, Role
        from tests.test_loop_guardrails import ScriptedLLM, _final

        (tmp_path / "answer.txt").write_text("ORIGINAL", encoding="utf-8")

        async def _allow(_request):
            return PermissionDecision.ALLOW

        llm = ScriptedLLM([
            _final("我已经分析完毕，问题在 answer.txt，建议你自己修改。"),
            _final("好的，这次真的完成了。"),
        ])
        loop = AgenticLoop(
            llm=llm,
            tools=runtime.tools.registry,
            permission_guard=PermissionGuard(ask_callback=_allow),
            config=LoopConfig(
                max_tool_rounds=8,
                workspace_dir=str(tmp_path),
                enable_summary_compression=False,
                enable_sliding_window=False,
                enable_lazy_project_map=False,
                enable_tool_result_truncation=False,
            ),
            tool_service=runtime.tools,
            context=runtime.ctx,
        )
        result = await loop.run(
            [Message(role=Role.USER, content="帮我修复 answer.txt 里的内容")]
        )

        # The loop refused to accept the stop and told the model to act.
        assert result.total_rounds >= 2
        transcript = "\n".join(
            m.get_text() for m in llm.seen_messages[-1]
        )
        assert "调用" in transcript and "工具" in transcript
        assert "你还没有调用任何工具" in transcript or "没有修改任何文件" in transcript
