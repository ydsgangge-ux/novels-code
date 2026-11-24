"""问答快速路径：给只读工具、不给写工具，答不出来就转回工具循环。

回归背景（2026-11-23）：用户问"没做解卦吗？为什么要摇8次？"，`classify()`
因为"为什么"判成 answer，旧代码 `tools=[]` 把工具全撤了，但系统提示仍写着
"每次回复必须包含工具调用"。模型于是把调用写成正文里的 `<tool_call>`，
用户收到一坨 XML，三轮都是 0 次工具调用、0 token。

修法（A+B）：
A. 问答路径只发只读工具（含 web_search / web_fetch），能看见文件但改不了；
B. 三道兜底：回答为空 / 回答是伪工具调用 → 转回工具循环；usage 键名归一化。
"""

from __future__ import annotations

import pytest

from gangge.layer3_agent.loop import AgenticLoop, LoopConfig
from gangge.layer3_agent.tools.base import BaseTool, ToolResult
from gangge.layer3_agent.tools.registry import ToolRegistry
from gangge.layer4_permission.guard import PermissionDecision, PermissionGuard
from gangge.layer5_llm.base import Message, Role
from gangge.runtime.intent import READ_ONLY_TOOLS, tool_policy
from support.fake_llm import RecordingEmitter, ScriptedLLM, call, say

#: 超过 100 字，避开"回答太短 → 要求先规划"的提醒，让断言只看本次改动。
LONG_ANSWER = "看过了：app.js 里用的字段名和 hexagrams.js 里的数据对不上，" * 4


class StubTool(BaseTool):
    """只记录被调用过，不碰磁盘。"""

    def __init__(self, name: str) -> None:
        self._name = name
        self.calls: list[dict] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"{self._name} stub"

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> ToolResult:
        self.calls.append(kwargs)
        return ToolResult(output=f"{self._name} ok")


async def _allow(_request) -> PermissionDecision:
    return PermissionDecision.ALLOW


def _loop(tmp_path, llm, registry):
    loop = AgenticLoop(
        llm=llm,
        tools=registry,
        permission_guard=PermissionGuard(ask_callback=_allow),
        config=LoopConfig(
            workspace_dir=str(tmp_path),
            max_tool_rounds=4,
            enable_sliding_window=False,
            enable_summary_compression=False,
            enable_lazy_project_map=False,
            enable_tool_result_truncation=False,
        ),
    )
    emitter = RecordingEmitter()
    loop.set_stream_callback(emitter)
    return loop, emitter


def _registry(*names: str) -> ToolRegistry:
    registry = ToolRegistry()
    for name in names:
        registry.register(StubTool(name))
    return registry


def test_tool_policy_only_downgrades_questions():
    """提问 → 只读；要动手 → 全套。词表本身不动（stop_guard 还靠它）。"""
    assert tool_policy("解释一下这个项目为什么启动慢") == "read_only"
    # 这一句就是线上翻车的那条：仍然算提问，但会拿到只读工具。
    assert tool_policy("没做解卦吗？为什么要摇8次？") == "read_only"
    assert tool_policy("把按钮改成蓝色") == "all"
    assert tool_policy("") == "all"


def test_readonly_set_includes_web_search():
    """联网搜索是问答的刚需：答案常常在项目之外（版本差异、平台行为）。"""
    assert "web_search" in READ_ONLY_TOOLS
    assert "web_fetch" in READ_ONLY_TOOLS
    assert "write_file" not in READ_ONLY_TOOLS


@pytest.mark.asyncio
async def test_qa_path_offers_readonly_tools_and_answers(tmp_path):
    llm = ScriptedLLM([say(LONG_ANSWER)])
    registry = _registry("read_file", "web_search", "write_file", "bash")
    loop, _emitter = _loop(tmp_path, llm, registry)

    result = await loop.run([Message(role=Role.USER, content="解释一下这个项目为什么启动慢")])

    assert result.completion_reason == "single_turn_answer"
    offered = set(llm.tool_requests[0])
    assert {"read_file", "web_search"} <= offered
    assert "write_file" not in offered and "bash" not in offered
    # 问答提示不能带"每次回复必须包含工具调用"——正是它逼出了伪工具调用。
    assert "每次回复必须包含工具调用" not in llm.systems[0]
    assert "tool_call" not in llm.systems[0]
    # usage 归一化：适配器给 input_tokens，界面读 input。
    assert result.total_tokens == {"input": 100, "output": 10}


@pytest.mark.asyncio
async def test_pseudo_tool_call_falls_back_to_tool_loop(tmp_path):
    """模型把调用写进正文时，不能原样交给用户，要转回工具循环。"""
    llm = ScriptedLLM([
        say('<tool_call><function=read_file><parameter=path>app.js</parameter></function></tool_call>'),
        call("read_file", path="app.js"),
        say(LONG_ANSWER),
    ])
    registry = _registry("read_file")
    loop, emitter = _loop(tmp_path, llm, registry)

    result = await loop.run([Message(role=Role.USER, content="解释一下为什么解卦不出来")])

    assert result.completion_reason != "single_turn_answer"
    assert "↩️" in emitter.text, "转回工具模式时要告诉用户，不能静默"
    assert "tool_call" not in result.final_response, "伪工具调用不能当成答案"
    assert result.final_response.startswith("看过了")
    # 转回后真的带上了工具，并且工具被执行过。
    assert any("read_file" in names for names in llm.tool_requests[1:])
    assert [e.tool_name for e in result.tool_executions] == ["read_file"]
    # 花掉的那次问答 token 不能被悄悄丢掉。
    assert result.total_tokens["input"] >= 200


@pytest.mark.asyncio
async def test_empty_qa_answer_falls_back_to_tool_loop(tmp_path):
    llm = ScriptedLLM([say(""), say(LONG_ANSWER)])
    loop, emitter = _loop(tmp_path, llm, _registry("read_file"))

    result = await loop.run([Message(role=Role.USER, content="解释一下这个项目为什么启动慢")])

    assert result.final_response.startswith("看过了")
    assert "↩️" in emitter.text
    assert result.total_tokens["input"] >= 200


@pytest.mark.asyncio
async def test_normal_answer_is_not_treated_as_tool_call(tmp_path):
    """带反引号的正常回答不能被误判成伪工具调用。"""
    answer = "这是 `tool_search` 的用法说明，" + LONG_ANSWER
    llm = ScriptedLLM([say(answer)])
    loop, _emitter = _loop(tmp_path, llm, _registry("read_file"))

    result = await loop.run([Message(role=Role.USER, content="解释一下 tool_search 是什么")])

    assert result.completion_reason == "single_turn_answer"
    assert result.final_response.startswith("这是")
