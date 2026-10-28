"""Tool exposure (profile core + tool_search) and argument validation."""

from __future__ import annotations

import pytest

from gangge.layer3_agent.tools.base import BaseTool, ToolResult
from gangge.layer3_agent.tools.registry import (
    AGENT_PROFILES,
    PROFILE_CORE,
    ToolRegistry,
    detect_agent_profile,
)
from gangge.layer3_agent.tools.validation import (
    ToolInputError,
    check_arguments,
    validate_arguments,
)


class _FakeTool(BaseTool):
    def __init__(self, tool_name: str, desc: str = "", schema: dict | None = None):
        self._name = tool_name
        self._desc = desc
        self._schema = schema or {"type": "object", "properties": {}}

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._desc

    @property
    def input_schema(self) -> dict:
        return self._schema

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(output="ok")


def _registry(*tools: BaseTool) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    registry.set_profile("coding")
    return registry


# ── Visibility ─────────────────────────────────────────────────


def test_core_set_is_visible_and_deferred_is_not():
    registry = _registry(
        _FakeTool("read_file", "读取文件"),
        _FakeTool("web_search", "联网搜索网页"),
        _FakeTool("tool_search", "搜索工具"),
    )
    names = {d.name for d in registry.get_definitions(phase=1)}
    assert "read_file" in names
    assert "tool_search" in names  # the escape hatch is always visible
    assert "web_search" not in names


def test_tool_search_reveals_a_deferred_tool():
    registry = _registry(
        _FakeTool("web_search", "联网搜索网页，抓取实时信息"),
        _FakeTool("tool_search", "搜索工具"),
    )
    assert "web_search" not in {d.name for d in registry.get_definitions(phase=1)}

    results = registry.search("联网搜索")
    assert results and results[0].name == "web_search"
    registry.unlock([t.name for t in results])

    names = {d.name for d in registry.get_definitions(phase=1)}
    assert "web_search" in names, "a discovered tool must stay visible"


def test_unlock_is_idempotent_and_ignores_unknown_names():
    registry = _registry(_FakeTool("web_search", "x"), _FakeTool("tool_search", "y"))
    assert registry.unlock(["web_search", "ghost"]) == ["web_search"]
    assert registry.unlock(["web_search"]) == []


def test_search_with_empty_query_lists_deferred_tools():
    registry = _registry(_FakeTool("web_search", "web"), _FakeTool("tool_search", "meta"))
    names = {t.name for t in registry.search("")}
    assert names == {"web_search"}


def test_cjk_bigram_search_matches_description():
    registry = _registry(
        _FakeTool("generate_image", "根据提示词生成图片，支持图生图"),
        _FakeTool("tool_search", "meta"),
    )
    assert registry.search("生成图片")[0].name == "generate_image"


def test_get_definitions_without_phase_returns_whole_profile():
    """Introspection (phase=None) must show everything the profile may use."""
    registry = _registry(
        _FakeTool("read_file", "读取"),
        _FakeTool("web_search", "web"),  # not part of the coding profile
        _FakeTool("tool_search", "meta"),
    )
    names = {d.name for d in registry.get_definitions()}
    assert names == {"read_file", "tool_search"}


def test_profile_env_override_beats_keyword_detection(monkeypatch):
    monkeypatch.setenv("GANGGE_AGENT_PROFILE", "research")
    assert detect_agent_profile("帮我写小说第一章") == "research"
    monkeypatch.delenv("GANGGE_AGENT_PROFILE")
    assert detect_agent_profile("帮我写小说第一章") == "novel"


def test_no_keyword_falls_back_to_coding(monkeypatch):
    """A message with no signal must not pick whichever profile is listed first.

    This regressed once: initialising the score at -1 let ``novel`` win every
    empty match, which silently hid the entire coding toolset from a plain
    "你好" — and with it every non-novel tool.
    """
    monkeypatch.delenv("GANGGE_AGENT_PROFILE", raising=False)
    assert detect_agent_profile("你好") == "coding"
    assert detect_agent_profile("") == "coding"


def test_video_intent_selects_media_profile(monkeypatch):
    monkeypatch.delenv("GANGGE_AGENT_PROFILE", raising=False)
    assert detect_agent_profile("帮我做个演示短视频，要有配音和字幕") == "media"
    assert "demo_video" in PROFILE_CORE["media"]


def test_every_profile_core_is_a_subset_of_its_tools():
    for profile, core in PROFILE_CORE.items():
        allowed = AGENT_PROFILES[profile]["tools"]
        unknown = core - allowed - {"tool_search"}
        assert not unknown, f"{profile} core references unlisted tools: {unknown}"


# ── Validation ─────────────────────────────────────────────────

SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "文件路径"},
        "limit": {"type": "integer", "description": "行数", "default": 200},
        "mode": {"type": "string", "enum": ["read", "write"]},
    },
    "required": ["path"],
}


def test_missing_required_field_is_reported_in_chinese():
    problems = check_arguments(SCHEMA, {"limit": 5})
    assert any("缺少必填字段" in p and "path" in p for p in problems)


def test_wrong_type_is_reported_with_actual_value():
    problems = check_arguments(SCHEMA, {"path": 123})
    assert any("应为 字符串" in p and "整数" in p for p in problems)


def test_enum_violation_is_reported():
    problems = check_arguments(SCHEMA, {"path": "a", "mode": "delete"})
    assert any("只能取" in p for p in problems)


def test_valid_arguments_produce_no_problems():
    assert check_arguments(SCHEMA, {"path": "a.py", "limit": 10, "mode": "read"}) == []


def test_validate_fills_defaults():
    args = validate_arguments("read_file", SCHEMA, {"path": "a.py"})
    assert args["limit"] == 200


def test_validate_raises_actionable_message():
    with pytest.raises(ToolInputError) as excinfo:
        validate_arguments("read_file", SCHEMA, {})
    message = str(excinfo.value)
    assert "参数校验失败：read_file" in message
    assert "参数说明（read_file）" in message
    assert "不要原样重试" in message


@pytest.mark.asyncio
async def test_safe_execute_never_runs_with_invalid_arguments():
    calls: list[dict] = []

    class Tool(BaseTool):
        @property
        def name(self):
            return "needs_path"

        @property
        def description(self):
            return "d"

        @property
        def input_schema(self):
            return SCHEMA

        async def execute(self, **kwargs):
            calls.append(kwargs)
            return ToolResult(output="ran")

    result = await Tool().safe_execute(limit=3)
    assert result.is_error
    assert calls == [], "an invalid call must not reach the tool body"
    assert "缺少必填字段" in result.output
    assert result.metadata["validation_errors"]


@pytest.mark.asyncio
async def test_safe_execute_passes_valid_calls_through():
    class Tool(BaseTool):
        @property
        def name(self):
            return "echo"

        @property
        def description(self):
            return "d"

        @property
        def input_schema(self):
            return {"type": "object", "properties": {"text": {"type": "string"}}}

        async def execute(self, **kwargs):
            return ToolResult(output=kwargs["text"])

    result = await Tool().safe_execute(text="hi")
    assert not result.is_error and result.output == "hi"


# ── The real registry ──────────────────────────────────────────


def test_real_registry_boots_with_search_and_patch():
    from gangge.layer3_agent.tools.registry import create_tool_registry

    registry = create_tool_registry(workspace="", load_plugins=False)
    names = {t.name for t in registry.list_tools()}
    assert "tool_search" in names
    assert "apply_patch" in names
    # The coding core is visible without any search.
    visible = {d.name for d in registry.get_definitions(phase=1)}
    assert {"read_file", "write_file", "bash", "apply_patch", "tool_search"} <= visible


def test_every_real_tool_schema_is_valid_json_schema():
    jsonschema = pytest.importorskip("jsonschema")
    from gangge.layer3_agent.tools.registry import create_tool_registry

    registry = create_tool_registry(workspace="", load_plugins=False)
    for tool in registry.list_tools():
        schema = tool.input_schema
        assert isinstance(schema, dict), f"{tool.name} schema is not a dict"
        assert schema.get("type") == "object", f"{tool.name} schema type must be object"
        jsonschema.Draft7Validator.check_schema(schema)
