"""build_loop 是桌面端与 Web 端共用的装配入口。

它以前不存在：装配逻辑埋在 ``GanggeWorker._run_async``（约 180 行，混着
pyqt 信号、权限对话框、系统提示拼装）。Web 后端要复用它就得 import
``desktop/app.py``，也就把 PyQt6 和 GUI 对话框拖到服务器上。

这些用例锁住三件事：
1. 装配把 workspace / 系统提示 / 清单 都接到正确对象上；
2. 没接权限确认 UI 时默认拒绝，而不是默默放行；
3. 跑完一个任务后 pipeline 能正常释放。
"""

from __future__ import annotations

import pytest

from gangge.layer3_agent.tools import todo as todo_mod
from gangge.layer3_agent.tools.todo import get_state_for
from gangge.layer5_llm.base import Message, Role
from gangge.runtime.session import TaskSpec, build_loop
from support.fake_llm import ScriptedLLM, say


@pytest.fixture(autouse=True)
def _isolate(tmp_path):
    todo_mod._states_by_workspace.clear()
    yield
    todo_mod._states_by_workspace.clear()


@pytest.mark.asyncio
async def test_build_loop_wires_workspace_prompt_and_todos(tmp_path):
    (tmp_path / "README.md").write_text("# demo\nUNIQUE_MARKER_123", encoding="utf-8")
    spec = TaskSpec(task="读一下项目", workspace=str(tmp_path), max_rounds=4)
    llm = ScriptedLLM([say("好的")])

    bundle = await build_loop(spec, llm=llm)
    try:
        assert bundle.loop.config.workspace_dir == str(tmp_path)
        assert bundle.loop.config.max_tool_rounds == 4
        assert "UNIQUE_MARKER_123" in bundle.system_prompt, "README 要进上下文"
        assert bundle.loop.todo_state is get_state_for(str(tmp_path)), "清单要按工作目录复用"
    finally:
        bundle.dispose()


@pytest.mark.asyncio
async def test_build_loop_can_run_a_turn(tmp_path):
    spec = TaskSpec(task="随便说一句", workspace=str(tmp_path), max_rounds=3)
    llm = ScriptedLLM([say("收到")])

    bundle = await build_loop(spec, llm=llm)
    try:
        result = await bundle.loop.run([Message(role=Role.USER, content="随便说一句")])
        assert result.completion_reason, "收尾必须给出原因"
        assert llm.requests, "模型至少要被请求一次"
    finally:
        bundle.dispose()


@pytest.mark.asyncio
async def test_permission_without_a_host_callback_is_denied(tmp_path):
    from gangge.layer4_permission.guard import PermissionDecision

    spec = TaskSpec(task="x", workspace=str(tmp_path), auto_inject=False)
    bundle = await build_loop(spec, llm=ScriptedLLM([say("ok")]))
    try:
        decision = await bundle.guard._ask_callback(object())
        assert decision == PermissionDecision.DENY, "没有确认 UI 时必须拒绝"
    finally:
        bundle.dispose()


@pytest.mark.asyncio
async def test_host_callback_receives_the_request(tmp_path):
    seen = []

    async def ask(request):
        seen.append(request)
        return "ALLOWED"

    spec = TaskSpec(task="x", workspace=str(tmp_path), auto_inject=False)
    bundle = await build_loop(spec, llm=ScriptedLLM([say("ok")]), ask_permission=ask)
    try:
        decision = await bundle.guard._ask_callback("req-1")
        assert seen == ["req-1"]
        assert decision == "ALLOWED"
    finally:
        bundle.dispose()


@pytest.mark.asyncio
async def test_bundle_records_a_per_task_trace(tmp_path):
    """全局日志混着所有任务；出问题要能只翻这一个任务的轨迹。"""
    spec = TaskSpec(task="x", workspace=str(tmp_path), auto_inject=False)
    bundle = await build_loop(spec, llm=ScriptedLLM([say("ok")]))
    try:
        bundle.record({"kind": "tool", "name": "read_file"})
        bundle.record({"kind": "done", "reason": "model_end_turn"})
        lines = (tmp_path / ".gangge" / "trace.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert '"read_file"' in lines[0]
        assert '"model_end_turn"' in lines[1]
    finally:
        bundle.dispose()


@pytest.mark.asyncio
async def test_auto_allow_skips_the_callback(tmp_path):
    async def ask(_request):  # pragma: no cover - must not be called
        raise AssertionError("auto_allow 时不该再问用户")

    spec = TaskSpec(task="x", workspace=str(tmp_path), auto_inject=False, auto_allow=True)
    bundle = await build_loop(spec, llm=ScriptedLLM([say("ok")]), ask_permission=ask)
    try:
        from gangge.layer4_permission.guard import PermissionDecision

        assert await bundle.guard._ask_callback(object()) == PermissionDecision.ALLOW
    finally:
        bundle.dispose()
