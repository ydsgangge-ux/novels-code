"""End-to-end golden paths for the agent loop, driven by a scripted model.

These are the tests that did not exist: the loop's control flow — tool chain,
error feedback, permission denial, argument rejection, round limits — now runs
for real against a fake adapter, in milliseconds and without a network.

Each test asserts on observable behaviour (what the model saw, what ran, why the
turn ended), never on internals.
"""

from __future__ import annotations

import pytest

from gangge.layer3_agent.loop import AgenticLoop, LoopConfig
from gangge.layer3_agent.tools.base import BaseTool, ToolResult
from gangge.layer3_agent.tools.registry import ToolRegistry
from gangge.layer4_permission.guard import PermissionGuard
from gangge.layer4_permission.rules import PermissionPolicy
from gangge.layer5_llm.base import Message, Role
from support.fake_llm import RecordingEmitter, ScriptedLLM, call, say
from support.snapshot import assert_snapshot

LONG_ANSWER = (
    "任务已经完成，这里是结论说明。"
    "所有需要修改的文件都已处理，相关的验证也已通过，可以继续下一步工作了。"
    "如果需要调整细节，请直接说明具体位置和期望效果。"
    "以下是我的总结：第一步阅读了现有实现，第二步确认了调用链路，"
    "第三步完成修改并做了回归验证，四个环节都没有遗留问题。"
)
assert len(LONG_ANSWER) > 100


# ── Fixtures ───────────────────────────────────────────────────


class EchoTool(BaseTool):
    def __init__(self, log: list | None = None):
        self.log = log if log is not None else []

    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "回显文本，用于测试"

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要回显的文本"}},
            "required": ["text"],
        }

    async def execute(self, **kwargs) -> ToolResult:
        self.log.append(kwargs)
        return ToolResult(output=f"echo:{kwargs['text']}")


class FlakyTool(BaseTool):
    """Fails the first time, succeeds afterwards — the shape of a real retry."""

    def __init__(self) -> None:
        self.attempts = 0

    @property
    def name(self) -> str:
        return "flaky"

    @property
    def description(self) -> str:
        return "第一次失败，之后成功"

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> ToolResult:
        self.attempts += 1
        if self.attempts == 1:
            return ToolResult(output="错误: 文件被占用", is_error=True)
        return ToolResult(output="ok")


def _loop(tmp_path, llm, registry, guard=None, **config_kwargs):
    config = LoopConfig(
        workspace_dir=str(tmp_path),
        max_tool_rounds=config_kwargs.pop("max_tool_rounds", 6),
        **config_kwargs,
    )
    loop = AgenticLoop(
        llm=llm,
        tools=registry,
        permission_guard=guard or PermissionGuard(),
        config=config,
    )
    emitter = RecordingEmitter()
    loop.set_stream_callback(emitter)
    return loop, emitter


def _user(text: str) -> list[Message]:
    return [Message(role=Role.USER, content=text)]


def _text(message: Message) -> str:
    """All text carried by a message, whatever the block kinds.

    Tool results live in ``TOOL_RESULT`` blocks, so filtering to ``TEXT`` would
    silently hide exactly what these assertions are about.
    """
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(block.text for block in content)


def _all_text(messages: list[Message]) -> str:
    return "\n".join(_text(message) for message in messages)


def _shape(messages: list[Message]) -> list[str]:
    """A deterministic view of a request: role + block kinds per message."""
    return [
        f"{msg.role.value}:" + ",".join(block.type.value for block in msg.content)
        for msg in messages
    ]


# ── Golden paths ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_single_tool_then_answer(tmp_path):
    echo = EchoTool()
    registry = ToolRegistry()
    registry.register(echo)
    llm = ScriptedLLM([call("echo", text="hi"), say(LONG_ANSWER)])

    loop, emitter = _loop(tmp_path, llm, registry)
    result = await loop.run(_user("打个招呼"))

    assert result.completion_reason == "model_end_turn"
    assert [e.tool_name for e in result.tool_executions] == ["echo"]
    assert result.tool_executions[0].output == "echo:hi"
    assert result.tool_executions[0].is_error is False
    assert LONG_ANSWER in result.final_response


@pytest.mark.asyncio
async def test_tool_error_is_fed_back_and_recovered(tmp_path):
    registry = ToolRegistry()
    flaky = FlakyTool()
    registry.register(flaky)
    llm = ScriptedLLM([call("flaky"), call("flaky"), say(LONG_ANSWER)])

    loop, _ = _loop(tmp_path, llm, registry)
    result = await loop.run(_user("调用 flaky 工具"))

    assert flaky.attempts == 2
    assert len(result.tool_executions) == 2
    assert result.tool_executions[0].is_error is True
    assert result.tool_executions[1].is_error is False
    # The model was told about the failure before it retried.
    assert "错误: 文件被占用" in _all_text(llm.requests[1])


@pytest.mark.asyncio
async def test_invalid_arguments_never_reach_the_tool(tmp_path):
    echo = EchoTool()
    registry = ToolRegistry()
    registry.register(echo)
    llm = ScriptedLLM([call("echo"), say(LONG_ANSWER)])  # missing required `text`

    loop, _ = _loop(tmp_path, llm, registry)
    result = await loop.run(_user("调用 echo"))

    assert echo.log == [], "the tool body must not run with invalid arguments"
    assert result.tool_executions[0].is_error is True
    assert "参数校验失败" in result.tool_executions[0].output
    # …and the model saw an actionable message, not a stack trace.
    assert "缺少必填字段" in _all_text(llm.requests[1])


@pytest.mark.asyncio
async def test_permission_denial_is_reported_to_the_model(tmp_path):
    registry = ToolRegistry()
    registry.register(EchoTool())
    denied_policy = PermissionPolicy.from_mapping(
        {
            "defaults": {"bash": "deny", "interactive": "allow"},
            "rules": [
                {"scope": "bash", "decision": "deny", "pattern": r"^rm\b"}
            ],
        }
    )
    llm = ScriptedLLM([call("bash", command="rm -rf build"), say(LONG_ANSWER)])

    loop, _ = _loop(tmp_path, llm, registry, guard=PermissionGuard(policy=denied_policy))
    result = await loop.run(_user("清理构建目录"))

    execution = result.tool_executions[0]
    assert execution.permission == "denied"
    assert execution.is_error is True
    assert "权限被拒绝" in _all_text(llm.requests[1])


@pytest.mark.asyncio
async def test_long_tool_output_is_truncated_keeping_head_and_tail(tmp_path):
    """A 20k-char output must keep the beginning *and* the end (where errors are)."""

    class VerboseTool(BaseTool):
        @property
        def name(self):
            return "verbose"

        @property
        def description(self):
            return "输出很长"

        @property
        def input_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            body = "HEAD-MARKER " + ("x" * 20_000) + " TAIL-MARKER"
            return ToolResult(output=body)

    registry = ToolRegistry()
    registry.register(VerboseTool())
    llm = ScriptedLLM([call("verbose"), say(LONG_ANSWER)])

    loop, _ = _loop(tmp_path, llm, registry, tool_result_max_chars=300)
    await loop.run(_user("跑个长输出"))

    sent = _all_text(llm.requests[1])
    assert "HEAD-MARKER" in sent
    assert "TAIL-MARKER" in sent, "the tail must survive truncation"
    assert "中间截断" in sent
    assert len(sent) < 2000


@pytest.mark.asyncio
async def test_denied_tool_still_yields_a_tool_result(tmp_path):
    """Silence makes a model retry; an explicit denial lets it adapt."""

    registry = ToolRegistry()
    registry.register(EchoTool())
    policy = PermissionPolicy.from_mapping(
        {"defaults": {"interactive": "allow"}, "rules": [{"scope": "bash", "decision": "deny", "pattern": r".*"}]}
    )
    llm = ScriptedLLM([call("bash", command="echo hi"), say(LONG_ANSWER)])

    loop, _ = _loop(tmp_path, llm, registry, guard=PermissionGuard(policy=policy))
    await loop.run(_user("执行命令"))

    shape = _shape(llm.requests[1])
    assert shape[-2:] == ["tool:tool_result", "user:text"], "every tool_call needs a matching result"


@pytest.mark.asyncio
async def test_max_rounds_terminates_with_reason(tmp_path):
    registry = ToolRegistry()
    registry.register(EchoTool())
    llm = ScriptedLLM([call("echo", text=str(i)) for i in range(5)])

    loop, _ = _loop(tmp_path, llm, registry, max_tool_rounds=2)
    result = await loop.run(_user("一直调用工具"))

    assert result.completion_reason == "max_rounds"
    assert result.total_rounds == 2


@pytest.mark.asyncio
async def test_tool_search_unlocks_a_deferred_tool_mid_turn(tmp_path):
    """The capability the keyword phases replaced: ask, get it, use it."""

    class WebSearch(BaseTool):
        called = 0

        @property
        def name(self):
            return "web_search"

        @property
        def description(self):
            return "联网搜索网页，获取实时信息"

        @property
        def input_schema(self):
            return {"type": "object", "properties": {"q": {"type": "string"}}}

        async def execute(self, **kwargs):
            WebSearch.called += 1
            return ToolResult(output="搜索结果: 3 条")

    from gangge.layer3_agent.tools.tool_search import ToolSearchTool

    registry = ToolRegistry()
    registry.register(ToolSearchTool(registry))
    registry.register(WebSearch())
    llm = ScriptedLLM(
        [
            call("tool_search", query="联网搜索"),
            call("web_search", q="今天新闻"),
            say(LONG_ANSWER),
        ]
    )

    loop, emitter = _loop(tmp_path, llm, registry)
    result = await loop.run(_user("查一下今天的新闻"))

    assert WebSearch.called == 1
    assert [e.tool_name for e in result.tool_executions] == ["tool_search", "web_search"]
    # The search result told the model the tool is now usable.
    assert "已加入你的工具列表" in result.tool_executions[0].output


# ── Snapshots (prompt-drift guards) ────────────────────────────


@pytest.mark.asyncio
async def test_request_chain_shape_is_stable(tmp_path):
    registry = ToolRegistry()
    registry.register(EchoTool())
    llm = ScriptedLLM([call("echo", text="a"), say(LONG_ANSWER)])

    loop, _ = _loop(tmp_path, llm, registry)
    await loop.run(_user("打个招呼"))

    assert_snapshot(_shape(llm.requests[0]), "loop/request_chain_round1")
    assert_snapshot(_shape(llm.requests[1]), "loop/request_chain_round2")


def test_system_prompt_is_stable(tmp_path):
    registry = ToolRegistry()
    registry.register(EchoTool())
    llm = ScriptedLLM([say("x")])
    loop, _ = _loop(tmp_path, llm, registry)

    prompt = loop._build_static_system_prompt()
    # Normalise the one volatile part: the absolute workspace path.
    prompt = prompt.replace(str(tmp_path), "<workspace>")
    assert_snapshot(prompt, "loop/system_prompt")
