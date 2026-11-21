"""异常收尾必须告诉用户"还剩什么没做"。

停滞 / 轮数耗尽 / 取消 / LLM 失败这几种收尾，模型是在中途被掐断的，没有机会
写收尾总结。以前界面上只剩一句"待办未完成"，用户不知道是哪几项；而 pending
列表和最后一次验证结论当时就在内存里。
"""
from __future__ import annotations

import pytest

from gangge.layer3_agent.loop import AgenticLoop, LoopConfig
from gangge.layer3_agent.tools.registry import ToolRegistry
from gangge.layer4_permission.guard import PermissionGuard
from gangge.layer5_llm.base import Message, Role
from support.fake_llm import RecordingEmitter, ScriptedLLM, say

LONG = "任务说明" * 40


def _loop(tmp_path, cancel_check=None, **cfg):
    loop = AgenticLoop(
        llm=ScriptedLLM([say(LONG)]),
        tools=ToolRegistry(),
        permission_guard=PermissionGuard(),
        config=LoopConfig(workspace_dir=str(tmp_path), **cfg),
        cancel_check=cancel_check,
    )
    loop.set_stream_callback(RecordingEmitter())
    return loop


def test_describe_unfinished_lists_only_pending(tmp_path):
    loop = _loop(tmp_path)
    loop.todo_state._todos = [
        {"content": "写测试", "status": "in_progress"},
        {"content": "跑冒烟", "status": "pending"},
        {"content": "已完成项", "status": "completed"},
    ]

    text = loop._describe_unfinished()

    assert "2 项待办未完成" in text
    assert "写测试" in text
    assert "跑冒烟" in text
    assert "已完成项" not in text


def test_describe_unfinished_includes_verification(tmp_path):
    loop = _loop(tmp_path)
    loop._last_verification_note = "❌ pytest 未通过\nAssertionError: boom"

    assert "最后一次验证：❌ pytest 未通过" in loop._describe_unfinished()


def test_describe_unfinished_is_empty_when_nothing_left(tmp_path):
    loop = _loop(tmp_path)
    loop.todo_state._todos = [{"content": "做完的", "status": "completed"}]
    assert loop._describe_unfinished() == ""


def test_describe_unfinished_caps_the_list(tmp_path):
    loop = _loop(tmp_path)
    loop.todo_state._todos = [
        {"content": f"任务{i}", "status": "pending"} for i in range(8)
    ]
    text = loop._describe_unfinished()
    assert "等共 8 项" in text
    assert text.count("- ") == 6  # 5 条明细 + "… 等共 8 项"


@pytest.mark.asyncio
async def test_cancel_reports_pending_items_to_the_user(tmp_path):
    loop = _loop(tmp_path, cancel_check=lambda: True)
    # "继续" 会保留上一轮的待办，否则 run() 开头会清空
    loop.todo_state._todos = [{"content": "写测试", "status": "pending"}]
    emitter = loop._stream_callback

    result = await loop.run([Message(role=Role.USER, content="继续做点事")])

    assert result.completion_reason == "cancelled"
    assert "写测试" in result.final_response
    joined = "".join(getattr(b, "text", "") for b in emitter.blocks)
    assert "尚未完成" in joined and "写测试" in joined
