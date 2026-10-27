"""Tests for the plugin runtime: container, composition, and tool pipeline."""

from __future__ import annotations

import textwrap

import pytest

from gangge.runtime import events
from gangge.runtime.boot import bootstrap, dump_tree, describe_events, describe_seams
from gangge.runtime.context import Context, Inject, MissingServiceError, Service
from gangge.runtime.patch import PatchError, Tree, build_tree, parse_ops
from gangge.runtime.profile import ProfileError, available_profiles, resolve
from gangge.runtime.seams import ToolInvocation, ToolOutcome


# ── Container ───────────────────────────────────────────────────


class TestContextServices:
    def test_provide_and_require(self):
        ctx = Context()
        ctx.provide("tools", "a-registry")
        assert ctx.require("tools") == "a-registry"
        assert ctx.has("tools")

    def test_missing_service_raises(self):
        ctx = Context()
        with pytest.raises(MissingServiceError):
            ctx.require("nothing")

    def test_child_sees_parent_but_not_the_reverse(self):
        root = Context("root")
        child = root.sub("child")
        root.provide("llm", "factory")
        assert child.get("llm") == "factory"

        child.provide("local", 1)
        assert root.get("local") is None

    def test_duplicate_provide_rejected(self):
        ctx = Context()
        ctx.provide("x", 1)
        with pytest.raises(KeyError):
            ctx.provide("x", 2)

    def test_disposers_run_last_in_first_out(self):
        ctx = Context()
        order: list[str] = []
        ctx.effect(lambda: order.append("first"))
        ctx.effect(lambda: order.append("second"))
        ctx.dispose()
        assert order == ["second", "first"]

    def test_dispose_is_idempotent(self):
        ctx = Context()
        ctx.effect(lambda: None)
        ctx.dispose()
        ctx.dispose()  # must not raise

    def test_failing_disposer_does_not_block_the_rest(self):
        ctx = Context()
        order: list[str] = []

        def boom():
            raise RuntimeError("boom")

        ctx.effect(lambda: order.append("kept"))
        ctx.effect(boom)
        ctx.dispose()
        assert order == ["kept"]


class TestDependencyInjection:
    def test_plugin_parks_until_service_appears(self):
        ctx = Context()
        seen: list[str] = []

        class Consumer:
            name = "consumer"
            inject = Inject(required=("tools",))

            def apply(self, ctx, config):
                seen.append("activated")
                return None

        mount = ctx.plugin(Consumer())
        assert not mount.active
        assert seen == []

        ctx.provide("tools", object())
        assert mount.active
        assert seen == ["activated"]

    def test_optional_dependency_never_blocks(self):
        ctx = Context()

        class Consumer:
            name = "consumer"
            inject = Inject(required=(), optional=("telemetry",))

            def apply(self, ctx, config):
                return None

        assert ctx.plugin(Consumer()).active

    def test_bare_tuple_means_required(self):
        ctx = Context()

        class Consumer:
            name = "consumer"
            inject = ("tools",)

            def apply(self, ctx, config):
                return None

        assert not ctx.plugin(Consumer()).active

    def test_service_subclass_publishes_its_key(self):
        class Counter(Service):
            name = "counter"

            def __init__(self, ctx, config):
                super().__init__(ctx, config)
                self.value = 0

            def stop(self):
                self.value = -1

        ctx = Context()
        ctx.plugin(Counter, {"start": 0})
        assert ctx.require("counter").value == 0

        instance = ctx.require("counter")
        ctx.dispose()
        assert instance.value == -1

    def test_self_dependency_is_reported_instead_of_parking_forever(self):
        from gangge.runtime.context import CycleError

        class Selfish(Service):
            name = "selfish"
            inject = ("selfish",)

        ctx = Context()
        with pytest.raises(CycleError):
            ctx.plugin(Selfish)

    def test_broken_plugin_does_not_poison_the_tree(self):
        ctx = Context()

        class Bad:
            name = "bad"

            def apply(self, ctx, config):
                raise RuntimeError("nope")

        ctx.plugin(Bad())
        ctx.provide("after", True)
        assert ctx.has("after")


# ── Events ──────────────────────────────────────────────────────


class TestEventDispatch:
    def test_emit_observes_in_order(self):
        ctx = Context()
        seen: list[int] = []
        ctx.on("turn/start", lambda: seen.append(1))
        ctx.on("turn/start", lambda: seen.append(2))
        ctx.emit("turn/start")
        assert seen == [1, 2]

    def test_emit_swallows_listener_errors(self):
        ctx = Context()
        done: list[str] = []

        def boom():
            raise RuntimeError("listener")

        ctx.on("turn/start", boom)
        ctx.on("turn/start", lambda: done.append("ok"))
        ctx.emit("turn/start")
        assert done == ["ok"]

    def test_waterfall_wraps_and_returns(self):
        ctx = Context()
        ctx.on("llm/request", lambda request, next: next({**request, "system": "wrapped"}))
        result = ctx.waterfall("llm/request", {"system": "plain"})
        assert result["system"] == "wrapped"

    def test_waterfall_short_circuits_without_next(self):
        ctx = Context()
        downstream: list[str] = []
        ctx.on("llm/request", lambda request, next: {"system": "vetoed"})
        ctx.on("llm/request", lambda request, next: downstream.append("ran") or next(request))
        result = ctx.waterfall("llm/request", {"system": "plain"})
        assert result == {"system": "vetoed"}
        assert downstream == []

    def test_waterfall_passthrough_with_no_listener(self):
        ctx = Context()
        assert ctx.waterfall("llm/request", "value") == "value"

    def test_bail_stops_at_first_answer(self):
        ctx = Context()
        ctx.on("fs/write", lambda path: None)
        ctx.on("fs/write", lambda path: "handled")
        ctx.on("fs/write", lambda path: "second")
        assert ctx.bail("fs/write", "a.txt") == "handled"

    @pytest.mark.asyncio
    async def test_serial_awaits_in_order(self):
        ctx = Context()
        order: list[str] = []

        async def first(*_):
            order.append("first")
            return None

        async def second(*_):
            order.append("second")
            return "verdict"

        ctx.on("permission/check", first)
        ctx.on("permission/check", second)
        assert await ctx.serial("permission/check", "bash", "ls") == "verdict"
        assert order == ["first", "second"]

    def test_prepend_runs_first(self):
        ctx = Context()
        seen: list[str] = []
        ctx.on("turn/start", lambda: seen.append("normal"))
        ctx.on("turn/start", lambda: seen.append("early"), prepend=True)
        ctx.emit("turn/start")
        assert seen == ["early", "normal"]

    def test_children_inherit_listeners(self):
        root = Context()
        child = root.sub("child")
        seen: list[str] = []
        root.on("turn/end", lambda: seen.append("root"))
        child.emit("turn/end")
        assert seen == ["root"]

    def test_off_removes_a_listener(self):
        ctx = Context()
        seen: list[str] = []
        off = ctx.on("turn/start", lambda: seen.append("x"))
        off()
        ctx.emit("turn/start")
        assert seen == []

    def test_dispose_clears_listeners(self):
        ctx = Context()
        seen: list[str] = []
        ctx.on("turn/start", lambda: seen.append("x"))
        ctx.dispose()
        ctx.emit("turn/start")
        assert seen == []


class TestEventCatalogue:
    def test_every_declared_event_has_a_mode(self):
        for item in events.catalogue():
            assert item["mode"] in (
                events.EMIT,
                events.WATERFALL,
                events.PARALLEL,
                events.SERIAL,
                events.BAIL,
            )

    def test_waterfall_events_are_the_interception_points(self):
        assert events.mode_of(events.TOOLS_PRE_EXECUTE) == events.WATERFALL
        assert events.mode_of(events.AGENT_PRE_STEP) == events.WATERFALL
        assert events.mode_of(events.PERMISSION_CHECK) == events.SERIAL
        assert events.mode_of(events.TURN_END) == events.EMIT

    def test_unknown_event_defaults_to_emit(self):
        assert events.mode_of("whatever/happened") == events.EMIT

    def test_catalogue_renders(self):
        assert "tools/pre-execute" in describe_events()


# ── Patch documents ─────────────────────────────────────────────


class TestPatchTree:
    def test_insert_then_patch_replaces_config_wholesale(self):
        tree = build_tree([
            ("a", parse_ops([{"insert": [{"id": "tools", "name": "x.T", "config": {"a": 1, "b": 2}}]}])),
            ("b", parse_ops([{"patch": [{"id": "tools", "config": {"b": 3}}]}])),
        ])
        assert tree.get("tools").config == {"b": 3}  # replaced, not merged
        assert tree.get("tools").source == "b"

    def test_remove_drops_the_row(self):
        tree = build_tree([
            ("a", parse_ops([{"insert": [
                {"id": "one", "name": "x.A"},
                {"id": "two", "name": "x.B"},
            ]}])),
            ("b", parse_ops([{"remove": ["one"]}])),
        ])
        assert tree.ids() == ["two"]

    def test_duplicate_insert_is_an_error(self):
        tree = Tree()
        tree.insert([{"id": "a", "name": "x.A"}], "layer")
        with pytest.raises(PatchError):
            tree.insert([{"id": "a", "name": "x.A"}], "layer")

    def test_patching_unknown_row_is_an_error(self):
        with pytest.raises(PatchError):
            build_tree([("a", parse_ops([{"patch": [{"id": "ghost", "config": {}}]}]))])

    def test_operation_needs_exactly_one_key(self):
        with pytest.raises(PatchError):
            parse_ops([{"insert": [], "remove": []}], "doc")

    def test_row_needs_a_name(self):
        with pytest.raises(PatchError):
            parse_ops([{"insert": [{"id": "a"}]}], "doc")

    def test_disabled_row_is_skipped(self):
        tree = build_tree([("a", parse_ops([{"insert": [
            {"id": "on", "name": "x.A"},
            {"id": "off", "name": "x.B", "disabled": True},
        ]}]))])
        assert [r.id for r in tree.enabled_rows()] == ["on"]

    def test_when_predicate_reads_the_environment(self, monkeypatch):
        tree = build_tree([("a", parse_ops([{"insert": [
            {"id": "a", "name": "x.A", "when": "env:GANGGE_TEST_FLAG"},
            {"id": "b", "name": "x.B", "when": "!env:GANGGE_TEST_FLAG"},
        ]}]))])
        monkeypatch.delenv("GANGGE_TEST_FLAG", raising=False)
        assert [r.id for r in tree.enabled_rows()] == ["b"]

        monkeypatch.setenv("GANGGE_TEST_FLAG", "1")
        assert [r.id for r in tree.enabled_rows()] == ["a"]

    def test_unknown_predicate_fails_open(self):
        tree = build_tree([("a", parse_ops([{"insert": [
            {"id": "a", "name": "x.A", "when": "nonsense:here"},
        ]}]))])
        assert [r.id for r in tree.enabled_rows()] == ["a"]


# ── Profiles ────────────────────────────────────────────────────


class TestProfiles:
    def test_builtin_profiles_are_listed(self):
        names = available_profiles()
        assert "standard" in names
        assert "minimal" in names
        assert "novel" in names

    def test_standard_layers_bundles_then_nothing_else(self):
        resolved = resolve("standard")
        assert resolved.layers == [
            "bundle:base",
            "bundle:guardrails",
            "bundle:full-tools",
            "bundle:memory-bank",
        ]

    def test_core_rows_exist(self):
        tree = resolve("standard").tree
        for row_id in ("llm", "permission", "session", "tools", "memory-bank"):
            assert tree.get(row_id) is not None

    def test_minimal_removes_the_memory_bank(self):
        tree = resolve("minimal").tree
        assert tree.get("memory-bank") is None
        assert tree.get("tools").config["load_plugins"] is False

    def test_minimal_honours_its_profile_patch_layer(self):
        assert "profile:minimal" in resolve("minimal").layers

    def test_unknown_profile_lists_alternatives(self):
        with pytest.raises(ProfileError) as exc:
            resolve("no-such-profile")
        assert "standard" in str(exc.value)

    def test_unknown_bundle_is_reported(self, tmp_path):
        prof = tmp_path / ".gangge" / "profiles" / "broken.yml"
        prof.parent.mkdir(parents=True)
        prof.write_text("name: broken\nbundles: [nope]\n", encoding="utf-8")
        with pytest.raises(ProfileError) as exc:
            resolve("broken", workspace=tmp_path)
        assert "nope" in str(exc.value)

    def test_workspace_patch_applies_last(self, tmp_path):
        gangge = tmp_path / ".gangge"
        gangge.mkdir()
        (gangge / "cordis.patch.yml").write_text(
            textwrap.dedent(
                """
                - patch:
                    - id: tools
                      config: { profile: research }
                """
            ),
            encoding="utf-8",
        )
        resolved = resolve("standard", workspace=tmp_path)
        assert resolved.layers[-1] == "home"
        assert resolved.tree.get("tools").config == {"profile": "research"}

    def test_cli_patch_overlay_wins_over_home_patch(self, tmp_path):
        gangge = tmp_path / ".gangge"
        gangge.mkdir()
        (gangge / "cordis.patch.yml").write_text(
            "- patch:\n    - id: tools\n      config: { profile: home }\n",
            encoding="utf-8",
        )
        overlay = tmp_path / "extra.yml"
        overlay.write_text(
            "- patch:\n    - id: tools\n      config: { profile: overlay }\n",
            encoding="utf-8",
        )
        resolved = resolve("standard", workspace=tmp_path, patches=[overlay])
        assert resolved.tree.get("tools").config == {"profile": "overlay"}

    def test_static_dump_reports_rows_and_layers(self):
        text = dump_tree(resolve("standard"))
        assert "rows:" in text
        assert "bundle:base" in text
        assert "gangge.runtime.plugins.tools" in text


# ── Tool pipeline ───────────────────────────────────────────────


class _FakeRegistry:
    """Minimal ToolRegistry stand-in — no tool construction, no imports."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def register(self, tool):
        pass

    def unregister(self, name):
        pass

    def get(self, name):
        return None

    def list_tools(self):
        return []

    def get_definitions(self, **scope):
        return []

    def get_profile(self):
        return "coding"

    def set_profile(self, profile):
        pass

    def __len__(self):
        return 0

    def __contains__(self, name):
        return False

    async def execute(self, name, arguments):
        from gangge.layer3_agent.tools.base import ToolResult

        self.calls.append((name, arguments))
        return ToolResult(output=f"ran {name}", metadata={"value": "canonical"})


def _service(registry=None, config=None):
    from gangge.runtime.plugins.tools import ToolService

    ctx = Context("test")
    # `registry or _FakeRegistry()` would be wrong: _FakeRegistry defines
    # __len__ == 0, so it is falsy and the caller's registry would be dropped.
    target = _FakeRegistry() if registry is None else registry
    return ctx, ToolService(ctx, target, config or {})


class TestToolPipeline:
    @pytest.mark.asyncio
    async def test_plain_call_reaches_the_tool(self):
        registry = _FakeRegistry()
        ctx, service = _service(registry)
        result = await service.execute("read_file", {"path": "a.txt"})
        assert result.output == "ran read_file"
        assert registry.calls == [("read_file", {"path": "a.txt"})]

    @pytest.mark.asyncio
    async def test_veto_short_circuits_and_returns_an_error_result(self):
        registry = _FakeRegistry()
        ctx, service = _service(registry)
        ctx.on(events.TOOLS_PRE_EXECUTE, _veto_bash)

        result = await service.execute("bash", {"command": "rm -rf /"})
        assert result.is_error
        # The veto reason is passed to the model verbatim, so a policy owns the
        # wording it gives the model.
        assert "dangerous" in result.output
        assert registry.calls == []

    @pytest.mark.asyncio
    async def test_a_vetoed_call_still_emits_a_tool_result(self):
        seen: list[object] = []
        ctx, service = _service()
        ctx.on(events.TOOLS_PRE_EXECUTE, _veto_bash)
        ctx.on(events.TOOL_RESULT, seen.append)
        await service.execute("bash", {"command": "nope"})
        assert len(seen) == 1

    @pytest.mark.asyncio
    async def test_pre_execute_can_rewrite_arguments(self):
        registry = _FakeRegistry()
        ctx, service = _service(registry)

        def rewrite(invoke, next):
            invoke.arguments["path"] = "/safe/" + invoke.arguments["path"]
            return next(invoke)

        ctx.on(events.TOOLS_PRE_EXECUTE, rewrite)
        await service.execute("read_file", {"path": "a.txt"})
        assert registry.calls == [("read_file", {"path": "/safe/a.txt"})]

    @pytest.mark.asyncio
    async def test_permission_deny_blocks_execution(self):
        registry = _FakeRegistry()
        ctx, service = _service(registry)
        ctx.on(events.PERMISSION_CHECK, lambda *_: "deny: policy")
        result = await service.execute("bash", {"command": "ls"})
        assert result.is_error
        assert registry.calls == []

    @pytest.mark.asyncio
    async def test_permission_allow_passes(self):
        registry = _FakeRegistry()
        ctx, service = _service(registry)
        ctx.on(events.PERMISSION_CHECK, lambda *_: "allow")
        result = await service.execute("bash", {"command": "ls"})
        assert not result.is_error
        assert registry.calls

    @pytest.mark.asyncio
    async def test_post_execute_can_truncate_without_losing_the_value(self):
        payload = "x" * 10_000
        registry = _FakeRegistry()
        registry.execute = _returning(payload)  # type: ignore[assignment]
        ctx, service = _service(registry)

        def truncate(outcome, next):
            outcome.rendered = outcome.rendered[:100]
            return next(outcome)

        ctx.on(events.TOOLS_POST_EXECUTE, truncate)
        result = await service.execute("read_file", {"path": "big.txt"})

        assert len(result.output) == 100                 # what the model sees
        assert result.metadata["value"] == payload       # what a caller reads


def _veto_bash(invoke: ToolInvocation, next):
    if invoke.tool == "bash":
        return invoke.veto("dangerous command")
    return next(invoke)


def _returning(text: str):
    async def execute(name, arguments):
        from gangge.layer3_agent.tools.base import ToolResult

        return ToolResult(output=text, metadata={"value": text})

    return execute


# ── Boot ────────────────────────────────────────────────────────


class TestBootstrap:
    @pytest.fixture
    def runtime(self, tmp_path):
        rt = bootstrap(
            "standard",
            workspace=tmp_path,
            bindings={"tools": {"novel": False, "comfyui": False, "load_plugins": False}},
        )
        yield rt
        rt.dispose()

    def test_core_services_are_present(self, runtime):
        assert runtime.missing() == []
        assert runtime.tools is not None
        assert runtime.permission is not None
        assert runtime.llm is not None
        assert runtime.sessions is not None
        assert runtime.memory is not None

    def test_tools_are_registered(self, runtime):
        assert len(runtime.tools.registry) > 0
        assert "read_file" in runtime.tools

    def test_disabled_tool_groups_are_absent(self, runtime):
        assert "generate_image" not in runtime.tools

    def test_memory_bank_points_at_the_workspace(self, runtime, tmp_path):
        assert runtime.memory.dir == (tmp_path / ".gangge").resolve()

    def test_llm_is_lazy_about_credentials(self, runtime):
        # Mounting must not require an API key; only create() may fail.
        assert runtime.llm is not None

    def test_live_dump_describes_services(self, runtime):
        text = runtime.dump_config()
        assert "services:" in text
        assert "tools" in text

    def test_missing_service_is_reported_not_raised(self, tmp_path):
        rt = bootstrap(
            "minimal",
            workspace=tmp_path,
            bindings={"tools": {"novel": False, "comfyui": False}},
        )
        try:
            assert rt.missing() == []
        finally:
            rt.dispose()

    def test_dispose_releases_services(self, tmp_path):
        rt = bootstrap(
            "standard",
            workspace=tmp_path,
            bindings={"tools": {"novel": False, "comfyui": False, "load_plugins": False}},
        )
        rt.dispose()
        assert rt.ctx.get("tools") is None

    def test_seam_catalogue_renders(self):
        text = describe_seams()
        assert "llm" in text
        assert "tools" in text


# ── Seam dataclasses ────────────────────────────────────────────


class TestSeamObjects:
    def test_tool_invocation_veto_is_chainable(self):
        invoke = ToolInvocation(tool="bash", arguments={}).veto("no")
        assert invoke.vetoed and invoke.veto_reason == "no"

    def test_tool_outcome_keeps_value_separate_from_rendering(self):
        outcome = ToolOutcome(tool="read_file", rendered="short", value="full")
        assert outcome.rendered != outcome.value
