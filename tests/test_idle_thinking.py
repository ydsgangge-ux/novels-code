"""只思考、不调工具的一轮：思考不能白费，空转也不能无限。

真实故障（2026-11-23 的 BOM 填充任务）：模型连续三轮各输出 8 万字符分析，
但既没有正文也没有工具调用。reasoning 按协议不回传，于是下一轮上下文里只剩
一条空的 assistant 消息——模型只能从头再推一遍。这三轮合计约 64 分钟、
输出 token 的一半以上，任务最后还打满了 30 轮上限。

这里锁住两条行为：
1. 那一轮的思考要压成一条可回传的笔记，下一轮能直接用；
2. 连续空转有上限，到点收尾并保留未完成项，而不是继续烧 token。
"""

from __future__ import annotations

from typing import Any, AsyncIterator

import pytest

from gangge.layer3_agent.loop import (
    AgenticLoop,
    LoopConfig,
    thinking_carryover_note,
)
from gangge.layer3_agent.tools.file_ops import ReadFileTool
from gangge.layer3_agent.tools.registry import ToolRegistry
from gangge.layer3_agent.tools.todo import TodoWriteTool
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
    """按脚本回放回复，并记录每次请求实际收到了什么。"""

    def __init__(self, script: list[LLMResponse]):
        super().__init__(model="mimo-v2.6-flash")
        self.script = list(script)
        self._last: LLMResponse | None = None
        self.seen_messages: list[list[Message]] = []

    async def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        system: str = "",
    ) -> LLMResponse:
        self.seen_messages.append(
            [Message(role=m.role, content=list(m.content)) for m in messages]
        )
        if self.script:
            self._last = self.script.pop(0)
        if self._last is None:
            self._last = _final("(script exhausted)")
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
        # 流式接口用末帧携带 finish_reason，这里照实转发，才能测到截断路径。
        if response.stop_reason:
            yield ContentBlock(
                type=ContentType.TEXT, text="", stop_reason=response.stop_reason
            )

    async def close(self) -> None:
        return None


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


def _thinking(text: str, stop_reason: str = "end_turn") -> LLMResponse:
    return LLMResponse(
        content=[ContentBlock(type=ContentType.THINKING, text=text)],
        stop_reason=stop_reason,
    )


def _final(text: str) -> LLMResponse:
    return LLMResponse(
        content=[ContentBlock(type=ContentType.TEXT, text=text)],
        stop_reason="end_turn",
    )


@pytest.fixture
def harness(tmp_path):
    target = tmp_path / "answer.txt"
    target.write_text("STUBBORN_FACT_42", encoding="utf-8")

    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(TodoWriteTool())
    service, ctx = tool_pipeline(registry, repeat_limit=2, max_chars=2000)

    async def _allow(_request):
        return PermissionDecision.ALLOW

    guard = PermissionGuard(ask_callback=_allow)
    config = LoopConfig(
        max_tool_rounds=8,
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

    yield build, target
    ctx.dispose()


def _user_text(messages: list[Message]) -> str:
    return "\n".join(
        block.text
        for message in messages
        if message.role == Role.USER
        for block in message.content
        if block.type == ContentType.TEXT and block.text
    )


def test_carryover_keeps_task_head_and_conclusion_tail():
    long_reasoning = (
        "任务：把 BOM 填进报价模板。"
        + "这里是无价值的重复推导。" * 200
        + "结论：先写映射脚本再运行，不要继续读文件。"
    )
    note = thinking_carryover_note(_thinking(long_reasoning), 400)

    assert "分析笔记" in note
    assert "中间省略" in note
    assert "任务：把 BOM" in note
    assert "先写映射脚本再运行" in note, "尾部通常是收敛后的结论，必须保留"
    assert len(note) < 400 + 200, f"笔记要短，实际 {len(note)} 字符"


def test_carryover_is_empty_without_reasoning():
    assert thinking_carryover_note(_final("普通回答")) == ""


@pytest.mark.asyncio
async def test_thinking_only_round_is_carried_into_the_next_request(harness):
    build, target = harness
    llm = ScriptedLLM([
        _tool_use("TodoWrite", "t1", {
            "todos": [{"id": "1", "content": "分析两个表的结构", "status": "pending"}],
        }),
        _thinking("两个表的结构已经确认。下一步要写映射脚本并运行。"),
        _tool_use("read_file", "c1", {"path": str(target)}),
        _tool_use("TodoWrite", "t2", {
            "todos": [{"id": "1", "content": "分析两个表的结构", "status": "completed"}],
        }),
        _final("完成"),
    ])

    result = await build(llm).run([Message(role=Role.USER, content="填充表格")])

    assert result.completion_reason != "idle_no_action"
    # 第 3 次请求就是"空转一轮之后"的那次：必须能看到上一轮的结论。
    follow_up = _user_text(llm.seen_messages[2])
    assert "分析笔记" in follow_up
    assert "下一步要写映射脚本" in follow_up
    assert "请立即调用工具" in follow_up, "笔记要和明确的行动指令一起下发"


@pytest.mark.asyncio
async def test_truncated_thinking_is_continued_not_restarted(harness):
    """输出上限被推理用尽时，要把断点交给下一轮，而不是当成"说完了"。"""
    build, target = harness
    llm = ScriptedLLM([
        _tool_use("TodoWrite", "t1", {
            "todos": [{"id": "1", "content": "推导型号与连接器的映射", "status": "pending"}],
        }),
        _thinking(
            "已确认两表结构，正在推导映射：" + "继续推导。" * 40,
            stop_reason="max_tokens",
        ),
        _tool_use("read_file", "c1", {"path": str(target)}),
        _tool_use("TodoWrite", "t2", {
            "todos": [{"id": "1", "content": "推导型号与连接器的映射", "status": "completed"}],
        }),
        _final("完成"),
    ])

    result = await build(llm).run([Message(role=Role.USER, content="填充表格")])

    assert result.completion_reason != "idle_no_action"
    follow_up = _user_text(llm.seen_messages[2])
    assert "截断" in follow_up, "要明确告诉模型这是截断，不是自己停下的"
    assert "接着想" in follow_up, "截断后应该续写，而不是从零重来"
    assert "不要从头重写" in follow_up


@pytest.mark.asyncio
async def test_consecutive_idle_rounds_are_capped(harness):
    build, _target = harness
    llm = ScriptedLLM([
        _tool_use("TodoWrite", "t1", {
            "todos": [{"id": "1", "content": "填充表格", "status": "pending"}],
        }),
        _thinking("第一轮分析"),
        _thinking("第二轮分析"),
        _thinking("第三轮分析"),
        _final("不应该走到这里"),
    ])

    result = await build(llm).run([Message(role=Role.USER, content="填充表格")])

    assert result.completion_reason == "idle_no_action"
    assert len(result.tool_executions) == 1, "空转轮不该产生假的工具执行"
    assert "待办未完成" in result.final_response, "保留未完成项，交给下一次继续"


class _TruncatingLLM(BaseLLM):
    """模拟流式末帧 finish_reason=length（被 max_tokens 截断）。"""

    def __init__(self) -> None:
        super().__init__(model="stub")

    async def chat(self, messages, tools=None, system=""):  # pragma: no cover
        raise AssertionError("流式路径不应调用 chat")

    async def stream(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        system: str = "",
    ) -> AsyncIterator[ContentBlock]:
        yield ContentBlock(type=ContentType.THINKING, text="想到一半")
        yield ContentBlock(type=ContentType.TEXT, text="", stop_reason="max_tokens")

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_streaming_truncation_is_not_reported_as_a_normal_stop():
    response = await _TruncatingLLM().chat_stream(
        [Message(role=Role.USER, content="开始")]
    )
    assert response.stop_reason == "max_tokens", (
        "流式截断必须带出来，否则循环会把'想到一半'当成'说完了'"
    )


def test_reasoning_effort_and_extra_body_are_passed_through(monkeypatch):
    """把"少想点"的开关交到用户手里，不用改代码。

    旧的 ``LLM_REASONING_EFFORT`` 仍然被识别，但它只是"档位"的一种写法：
    落到哪个字段由厂商决定（OpenAI 是 ``reasoning_effort``，MiMo 是
    ``thinking.type``），不再无条件塞进 ``extra_body``。
    """
    monkeypatch.setenv("LLM_REASONING_EFFORT", "low")
    # 本机 .env 里常常已经写了 LLM_THINKING_LEVEL，而 create_llm() 会把它读进
    # os.environ；不清掉的话，这个用例在"单独跑"和"全量跑"下结论会不一样。
    monkeypatch.delenv("LLM_THINKING_LEVEL", raising=False)
    monkeypatch.setenv("LLM_EXTRA_BODY", '{"thinking": {"type": "disabled"}}')

    from gangge.layer5_llm.openai_compat import OpenAICompatLLM

    # OpenAI 推理模型：档位翻译成 reasoning_effort
    openai_llm = OpenAICompatLLM(
        base_url="https://api.openai.com/v1",
        api_key="not-needed",
        model="gpt-5.5",
    )
    openai_body = openai_llm._apply_provider_options({})["extra_body"]
    assert openai_body["reasoning_effort"] == "low"
    assert openai_body["thinking"]["type"] == "disabled", "用户手写的 extra_body 优先"

    # MiMo 只有开关、没有强度：不该凭空多出 reasoning_effort
    mimo_llm = OpenAICompatLLM(
        base_url="https://api.xiaomimimo.com/v1",
        api_key="not-needed",
        model="mimo-v2.5",
        thinking_level="high",
    )
    mimo_body = mimo_llm._apply_provider_options({})["extra_body"]
    assert "reasoning_effort" not in mimo_body
    assert mimo_body["thinking"]["type"] == "disabled"
