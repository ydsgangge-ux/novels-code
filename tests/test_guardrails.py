"""Weak-model guardrails: the failure modes a small free model actually has.

Every case here is a real GLM-Flash / 7B-style mistake, not a hypothetical:
wrong field name, stringified number, fenced file content, extra field, and the
same call repeated until the turn dies. The point of the guardrails is that the
model recovers *without* the user noticing.
"""

from __future__ import annotations

import pytest

from gangge.layer3_agent.tools.file_ops import ReadFileTool, WriteFileTool
from gangge.layer3_agent.tools.registry import ToolRegistry
from gangge.runtime import events
from gangge.runtime.context import Context
from gangge.runtime.pipeline import tool_pipeline
from gangge.runtime.plugins.repeat_guard import RepeatGuard
from gangge.runtime.plugins.result_pruner import ResultPruner
from gangge.runtime.plugins.schema_guard import SchemaGuard, _coerce, _example, _strip_fence


# ── fixture: a registry with two real, dependency-free tools ────


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(ReadFileTool())
    reg.register(WriteFileTool())
    return reg


class _Stub:
    """Minimal invocation object matching what the pipeline passes around."""

    def __init__(self, tool: str, arguments: dict):
        self.tool = tool
        self.arguments = dict(arguments)
        self.vetoed = False
        self.veto_reason = ""
        self.metadata: dict = {}

    def veto(self, reason: str) -> "_Stub":
        self.vetoed = True
        self.veto_reason = reason
        return self


def _guard(registry: ToolRegistry) -> SchemaGuard:
    ctx = Context("t")
    ctx.provide("tools", type("S", (), {"get": registry.get})())
    return SchemaGuard(ctx, {"coerce": True})


def _run(guard: SchemaGuard, tool: str, args: dict):
    """Run only the pre-execute stage; returns the invocation + argv it would pass."""
    invoke = _Stub(tool, args)
    seen: dict = {}

    def downstream(inv):
        seen["args"] = inv.arguments
        return inv

    result = guard.check(invoke, downstream)
    return result, seen.get("args")


# ── Stage 1: silent repair (no round wasted) ───────────────────


class TestRepair:
    def test_wrong_field_name_is_fixed(self):
        guard = _guard(_registry())
        result, args = _run(guard, "read_file", {"file_path": "a.py"})
        assert not result.vetoed
        assert args == {"path": "a.py"}

    @pytest.mark.parametrize("alias", ["filepath", "filename", "file", "target"])
    def test_common_aliases_all_map_to_path(self, alias):
        guard = _guard(_registry())
        result, args = _run(guard, "read_file", {alias: "a.py"})
        assert not result.vetoed
        assert args["path"] == "a.py", f"{alias} should map to path"

    def test_stringified_integer_is_coerced(self):
        guard = _guard(_registry())
        result, args = _run(guard, "read_file", {"path": "a.py", "limit": "5"})
        assert not result.vetoed
        assert args["limit"] == 5

    def test_float_string_for_integer_field(self):
        guard = _guard(_registry())
        _, args = _run(guard, "read_file", {"path": "a.py", "offset": "2.0"})
        assert args["offset"] == 2

    def test_fenced_content_is_unwrapped(self):
        guard = _guard(_registry())
        payload = "```python\nprint(1)\n```"
        _, args = _run(guard, "write_file", {"path": "a.py", "content": payload})
        assert args["content"] == "print(1)"

    def test_unknown_fields_are_dropped_not_fatal(self):
        guard = _guard(_registry())
        result, args = _run(
            guard, "read_file", {"path": "a.py", "reason": "because", "note": "x"}
        )
        assert not result.vetoed
        assert args == {"path": "a.py"}

    def test_repairs_are_recorded_for_the_audit_trail(self):
        guard = _guard(_registry())
        result, _ = _run(guard, "read_file", {"file_path": "a.py", "limit": "3"})
        assert result.metadata["schema_repairs"]
        assert guard.repairs == 1

    def test_valid_call_is_untouched(self):
        guard = _guard(_registry())
        result, args = _run(guard, "read_file", {"path": "a.py", "offset": 10})
        assert args == {"path": "a.py", "offset": 10}
        assert "schema_repairs" not in result.metadata


# ── Stage 2: explain what cannot be repaired ───────────────────


class TestExplain:
    def test_missing_required_names_the_field(self):
        guard = _guard(_registry())
        result, args = _run(guard, "read_file", {})
        assert result.vetoed
        assert "path" in result.veto_reason
        assert "必填" in result.veto_reason

    def test_message_lists_every_accepted_parameter(self):
        guard = _guard(_registry())
        result, _ = _run(guard, "read_file", {})
        for param in ("path", "offset", "limit"):
            assert param in result.veto_reason

    def test_message_echoes_what_the_model_actually_sent(self):
        guard = _guard(_registry())
        result, _ = _run(guard, "read_file", {"pth": "a.py"})
        assert "pth" in result.veto_reason

    def test_message_contains_a_copyable_example(self):
        guard = _guard(_registry())
        result, _ = _run(guard, "read_file", {})
        assert "正确示例" in result.veto_reason
        assert '"path"' in result.veto_reason

    def test_message_tells_the_model_to_retry(self):
        guard = _guard(_registry())
        result, _ = _run(guard, "read_file", {})
        assert "重新调用" in result.veto_reason

    def test_unusable_type_is_reported(self):
        guard = _guard(_registry())
        result, _ = _run(guard, "read_file", {"path": {"nested": 1}})
        assert result.vetoed
        assert "path" in result.veto_reason

    def test_rejection_is_counted(self):
        guard = _guard(_registry())
        _run(guard, "read_file", {})
        assert guard.rejections == 1


# ── Unknown tools are not our business ─────────────────────────


class TestUnknownTools:
    def test_mcp_style_name_passes_through(self):
        guard = _guard(_registry())
        result, args = _run(guard, "autocad__draw_circle", {"radius": 5})
        assert not result.vetoed
        assert args == {"radius": 5}

    def test_hallucinated_tool_name_passes_through(self):
        guard = _guard(_registry())
        result, _ = _run(guard, "totally_made_up_tool", {"x": 1})
        assert not result.vetoed  # the registry gives a clearer "unknown tool"


# ── Unit helpers ───────────────────────────────────────────────


class TestCoercionHelpers:
    @pytest.mark.parametrize(
        "value,wanted,expected",
        [
            ("7", "integer", 7),
            ("7.9", "integer", 7),
            ("2.5", "number", 2.5),
            ("true", "boolean", True),
            ("否", "boolean", False),
            ("single", "array", ["single"]),
            ('["a","b"]', "array", ["a", "b"]),
            ('{"k":1}', "object", {"k": 1}),
            (42, "string", "42"),
            ("keep", "integer", "keep"),  # unparseable → unchanged, validated later
        ],
    )
    def test_coerce_matrix(self, value, wanted, expected):
        assert _coerce(value, wanted) == expected

    def test_strip_fence_only_when_wrapped(self):
        assert _strip_fence("```py\nx=1\n```") == "x=1"
        assert _strip_fence("plain") == "plain"
        assert _strip_fence("```\na\nb\n```") == "a\nb"

    def test_example_is_derived_from_required_fields(self):
        schema = {
            "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["path"],
        }
        example = _example(schema)
        assert "path" in example
        assert isinstance(example["path"], str)


# ── Result pruner ──────────────────────────────────────────────


class TestResultPruner:
    def _outcome(self, text: str):
        from gangge.runtime.seams import ToolOutcome

        return ToolOutcome(tool="bash", rendered=text, value=text)

    def test_ansi_codes_are_stripped(self):
        pruner = ResultPruner({"max_chars": 1000})
        out = self._outcome("\x1b[32mPASSED\x1b[0m 3 tests")
        pruner.prune(out, lambda o: o)
        assert "\x1b" not in out.rendered
        assert "PASSED 3 tests" in out.rendered

    def test_short_output_keeps_its_canonical_value(self):
        pruner = ResultPruner({"max_chars": 10})
        payload = "y" * 500
        out = self._outcome(payload)
        pruner.prune(out, lambda o: o)
        assert out.rendered != payload          # model sees the short form
        assert out.value == payload             # program keeps the real thing

    def test_truncation_keeps_the_tail_where_errors_live(self):
        pruner = ResultPruner({"max_chars": 200, "head_ratio": 0.4})
        payload = "start" + ("x" * 5000) + "Traceback: boom"
        out = self._outcome(payload)
        pruner.prune(out, lambda o: o)
        assert out.rendered.startswith("start")
        assert out.rendered.endswith("Traceback: boom")
        assert "已截断" in out.rendered
        assert out.metadata["pruned"] is True
        assert out.metadata["original_chars"] == len(payload)

    def test_blank_line_flood_is_collapsed(self):
        pruner = ResultPruner({"max_chars": 1000})
        out = self._outcome("a" + "\n" * 20 + "b")
        pruner.prune(out, lambda o: o)
        assert len(out.rendered) < 10

    def test_cleaning_alone_does_not_mark_as_pruned(self):
        pruner = ResultPruner({"max_chars": 1000})
        out = self._outcome("\x1b[31mred\x1b[0m")
        pruner.prune(out, lambda o: o)
        assert "pruned" not in out.metadata


# ── Repeat guard ───────────────────────────────────────────────


class TestRepeatGuard:
    def _outcome(self, tool="read_file", rendered="same", key="k1", count=1):
        from gangge.runtime.seams import ToolOutcome

        return ToolOutcome(
            tool=tool,
            rendered=rendered,
            metadata={"call_key": key, "repeat_count": count},
        )

    def test_first_calls_are_silent(self):
        guard = RepeatGuard({"limit": 3})
        out = self._outcome(count=2)
        guard.annotate(out, lambda o: o)
        assert out.rendered == "same"

    def test_hint_appears_at_the_limit(self):
        guard = RepeatGuard({"limit": 3})
        out = self._outcome(count=3)
        guard.annotate(out, lambda o: o)
        assert "完全相同" in out.rendered
        assert "grep" in out.rendered           # suggests the actual alternative
        assert out.metadata["repeat_blocked"] is True

    def test_hint_is_appended_not_replacing_output(self):
        guard = RepeatGuard({"limit": 1})
        out = self._outcome(rendered="ORIGINAL", count=1)
        guard.annotate(out, lambda o: o)
        assert out.rendered.startswith("ORIGINAL")

    def test_identical_args_are_counted_across_calls(self):
        guard = RepeatGuard({"limit": 3})
        for _ in range(3):
            invoke = _Stub("read_file", {"path": "a.py"})
            guard.count(invoke, lambda i: i)
        assert invoke.metadata["repeat_count"] == 3

    def test_different_args_are_counted_separately(self):
        guard = RepeatGuard({"limit": 3})
        first = _Stub("read_file", {"path": "a.py"})
        second = _Stub("read_file", {"path": "b.py"})
        guard.count(first, lambda i: i)
        guard.count(second, lambda i: i)
        assert second.metadata["repeat_count"] == 1

    def test_reset_clears_the_ledger_between_turns(self):
        guard = RepeatGuard({"limit": 3})
        invoke = _Stub("read_file", {"path": "a.py"})
        guard.count(invoke, lambda i: i)
        guard.reset()
        again = _Stub("read_file", {"path": "a.py"})
        guard.count(again, lambda i: i)
        assert again.metadata["repeat_count"] == 1


# ── End to end: the weak-model recovery loop ───────────────────


class TestPipelineRecovery:
    @pytest.fixture
    def pipe(self, tmp_path):
        registry = _registry()
        service, ctx = tool_pipeline(registry, repeat_limit=2, max_chars=500)
        yield service, ctx, tmp_path
        ctx.dispose()

    @pytest.mark.asyncio
    async def test_wrong_field_name_still_reads_the_file(self, pipe):
        service, _ctx, tmp = pipe
        target = tmp / "note.txt"
        target.write_text("hello world", encoding="utf-8")

        # The model asked for "file_path"; the guard renames it and the call works.
        result = await service.execute("read_file", {"file_path": str(target)})
        assert not result.is_error
        assert "hello world" in result.output

    @pytest.mark.asyncio
    async def test_missing_argument_returns_a_teachable_error(self, pipe):
        service, _ctx, _tmp = pipe
        result = await service.execute("read_file", {})
        assert result.is_error
        assert "必填" in result.output
        assert "正确示例" in result.output

    @pytest.mark.asyncio
    async def test_extra_field_does_not_crash_the_tool(self, pipe):
        service, _ctx, tmp = pipe
        target = tmp / "n.txt"
        target.write_text("ok", encoding="utf-8")
        result = await service.execute(
            "read_file", {"path": str(target), "why": "because the model added it"}
        )
        assert not result.is_error

    @pytest.mark.asyncio
    async def test_repeated_identical_call_gets_a_nudge(self, pipe):
        service, _ctx, tmp = pipe
        target = tmp / "loop.txt"
        target.write_text("same every time", encoding="utf-8")

        await service.execute("read_file", {"path": str(target)})
        second = await service.execute("read_file", {"path": str(target)})
        assert "完全相同" in second.output

    @pytest.mark.asyncio
    async def test_long_output_is_pruned_but_value_survives(self, pipe):
        service, _ctx, tmp = pipe
        target = tmp / "big.txt"
        target.write_text("HEAD" + "z" * 4000 + "TAIL", encoding="utf-8")

        result = await service.execute("read_file", {"path": str(target)})
        assert len(result.output) < 1200
        assert "已截断" in result.output
        assert result.metadata["value"] is not None

    @pytest.mark.asyncio
    async def test_full_recovery_sequence(self, pipe):
        """bad call → readable error → corrected call succeeds."""
        service, _ctx, tmp = pipe
        target = tmp / "recover.txt"
        target.write_text("final answer", encoding="utf-8")

        first = await service.execute("read_file", {"pth": str(target)})
        assert first.is_error and "正" in first.output

        second = await service.execute("read_file", {"path": str(target)})
        assert not second.is_error
        assert "final answer" in second.output


# ── The guardrails are a bundle, so they can be turned off ─────


class TestGuardrailsBundle:
    def test_bundle_is_part_of_standard(self):
        from gangge.runtime.profile import resolve

        resolved = resolve("standard")
        assert "bundle:guardrails" in resolved.layers
        for row_id in ("schema-guard", "result-pruner", "repeat-guard"):
            assert resolved.tree.get(row_id) is not None

    def test_guardrails_can_be_removed_by_a_profile_patch(self, tmp_path):
        gangge = tmp_path / ".gangge"
        gangge.mkdir()
        (gangge / "cordis.patch.yml").write_text(
            "- remove:\n    - repeat-guard\n", encoding="utf-8"
        )
        from gangge.runtime.profile import resolve

        resolved = resolve("standard", workspace=tmp_path)
        assert resolved.tree.get("repeat-guard") is None
        assert resolved.tree.get("schema-guard") is not None

    def test_guardrail_listeners_are_visible_from_the_root(self, tmp_path):
        """Regression: listeners used to land on each plugin's own child context,
        where sibling plugins could not see them — the guardrails were mounted
        but never invoked."""
        from gangge.runtime.boot import bootstrap

        rt = bootstrap(
            "standard",
            workspace=tmp_path,
            bindings={"tools": {"novel": False, "comfyui": False, "load_plugins": False}},
        )
        try:
            assert rt.ctx.listeners(events.TOOLS_PRE_EXECUTE)
            assert rt.ctx.listeners(events.TOOLS_POST_EXECUTE)
        finally:
            rt.dispose()

    @pytest.mark.asyncio
    async def test_booted_pipeline_repairs_a_wrong_field_name(self, tmp_path):
        """The bootstrap path (CLI/TUI) must behave like ``tool_pipeline``."""
        from gangge.runtime.boot import bootstrap

        target = tmp_path / "booted.txt"
        target.write_text("BOOTED_OK", encoding="utf-8")

        rt = bootstrap(
            "standard",
            workspace=tmp_path,
            bindings={"tools": {"novel": False, "comfyui": False, "load_plugins": False}},
        )
        try:
            result = await rt.tools.execute("read_file", {"file_path": str(target)})
            assert not result.is_error
            assert "BOOTED_OK" in result.output
        finally:
            await rt.aclose()

    def test_permission_is_not_checked_twice(self, tmp_path):
        """The loop checks permission itself; the pipeline must stay quiet."""
        from gangge.runtime.boot import bootstrap

        rt = bootstrap(
            "standard",
            workspace=tmp_path,
            bindings={"tools": {"novel": False, "comfyui": False, "load_plugins": False}},
        )
        try:
            assert not rt.ctx.listeners(events.PERMISSION_CHECK)
        finally:
            rt.dispose()
