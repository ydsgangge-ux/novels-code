"""任务清单必须活过"继续"。

宿主每发一条消息都会新建一个 AgenticLoop。以前 TodoState 的生命周期绑在
loop 实例上，于是用户说"继续"时新 loop 拿到的是空清单：界面 500ms 轮询
读到的瞬间变空，注入给模型的也是空字符串（``format_for_injection`` 在空
清单上直接返回 ""）—— 模型完全看不见自己上一轮规划过什么，只能从头重新
摸索，于是同一个文件被反复整体重写、动不动撞满轮数上限。

这里锁住三件事：
1. 同一工作目录共用一份清单，并落盘；
2. "继续"不清空清单，且模型真的能在上下文里读到它；
3. 清单已全部完成时，"继续"要当成新需求，而不是开场就"全部完成，退出"。
"""

from __future__ import annotations

import pytest

from gangge.layer3_agent.loop import AgenticLoop, LoopConfig
from gangge.layer3_agent.tools import todo as todo_mod
from gangge.layer3_agent.tools.registry import ToolRegistry
from gangge.layer3_agent.tools.todo import TodoState, get_state_for
from gangge.layer4_permission.guard import PermissionGuard
from gangge.layer5_llm.base import ContentType, Message, Role
from support.fake_llm import RecordingEmitter, ScriptedLLM, say


@pytest.fixture(autouse=True)
def _isolate_workspace_cache():
    """每个用例一份干净的按目录缓存，避免跨用例互相看见对方的清单。"""
    todo_mod._states_by_workspace.clear()
    yield
    todo_mod._states_by_workspace.clear()


def _build(tmp_path, state: TodoState | None, last_text: str = "继续"):
    llm = ScriptedLLM([say("好的") for _ in range(6)])
    loop = AgenticLoop(
        llm=llm,
        tools=ToolRegistry(),
        permission_guard=PermissionGuard(),
        config=LoopConfig(
            workspace_dir=str(tmp_path),
            max_tool_rounds=6,
            enable_summary_compression=False,
            enable_sliding_window=False,
            enable_lazy_project_map=False,
            enable_tool_result_truncation=False,
        ),
        todo_state=state,
    )
    loop.set_stream_callback(RecordingEmitter())
    return loop, llm


def _all_text(messages: list[Message]) -> str:
    return "\n".join(
        block.text
        for message in messages
        for block in message.content
        if block.type == ContentType.TEXT and block.text
    )


def test_same_workspace_shares_one_state(tmp_path):
    assert get_state_for(tmp_path) is get_state_for(tmp_path)


def test_different_workspace_gets_its_own_state(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    assert get_state_for(tmp_path) is not get_state_for(other)


def test_state_is_written_to_disk_and_read_back(tmp_path):
    state = get_state_for(tmp_path)
    state.update([{"id": "1", "content": "写测试", "status": "pending"}])

    stored = tmp_path / ".gangge" / "todos.json"
    assert stored.is_file(), "清单要落盘，重启后还能接着做"

    # 模拟重启：新实例从同一份文件读回
    assert TodoState(storage_path=stored).get_pending()[0]["content"] == "写测试"


def test_loop_uses_the_state_it_is_given(tmp_path):
    state = get_state_for(tmp_path)
    state.update([{"id": "1", "content": "写测试", "status": "pending"}])

    loop, _llm = _build(tmp_path, state)

    assert loop.todo_state is state, "宿主传进来的清单不能被换掉"


@pytest.mark.asyncio
async def test_continue_keeps_todos_and_shows_them_to_the_model(tmp_path):
    state = get_state_for(tmp_path)
    state.update([{"id": "1", "content": "写测试", "status": "pending"}])

    loop, llm = _build(tmp_path, state)
    await loop.run([Message(role=Role.USER, content="继续")])

    assert loop.todo_state.get_pending(), "说'继续'时清单不能丢"
    seen = "\n".join(_all_text(request) for request in llm.requests)
    assert "写测试" in seen, "模型必须在上下文里看得到待办"


@pytest.mark.asyncio
async def test_continue_after_everything_done_starts_a_fresh_task(tmp_path):
    state = get_state_for(tmp_path)
    state.update([{"id": "1", "content": "做完的", "status": "completed"}])

    loop, _llm = _build(tmp_path, state)
    emitter = loop._stream_callback
    await loop.run([Message(role=Role.USER, content="继续")])

    assert loop.todo_state.get_all() == [], "全完成时'继续'是新需求，不能沿用旧清单"
    assert "TodoWrite" in emitter.text, "新任务要重新催模型建清单"


@pytest.mark.asyncio
async def test_new_task_still_clears_todos(tmp_path):
    state = get_state_for(tmp_path)
    state.update([{"id": "1", "content": "上一个任务的", "status": "pending"}])

    loop, _llm = _build(tmp_path, state)
    await loop.run([Message(role=Role.USER, content="换个完全不相干的任务")])

    assert loop.todo_state.get_all() == [], "新任务不该继承上一个任务未完成的清单"
