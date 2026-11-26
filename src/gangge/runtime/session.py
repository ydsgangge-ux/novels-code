"""Assemble an :class:`AgenticLoop` for one task — host-agnostic.

Every shell used to build its own loop, which is why the desktop worker grew a
180-line ``_run_async`` that mixes prompt assembly, tool registration, pyqt
signals and permission dialogs. The web backend cannot reuse that: importing
``desktop/app.py`` drags in PyQt6, and running a Qt dialog on a server is not a
thing.

So the assembly lives here, as a function of "what the task needs" plus a few
callbacks the host supplies:

    bundle = await build_loop(
        spec,
        llm=create_llm(),
        ask_user=my_async_ask,
        ask_permission=my_async_ask_permission,
    )
    result = await bundle.loop.run([Message(role=Role.USER, content=spec.task)])

The host keeps the ``LoopBundle`` alive for the duration of the task and calls
``bundle.dispose()`` when it ends, so tool-pipeline resources (spawned MCP
servers, background commands) are released with the task rather than with the
process.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from gangge.layer3_agent.loop import AgenticLoop, LoopConfig
from gangge.layer3_agent.prompts.system import build_system_prompt
from gangge.layer3_agent.tools.registry import ToolRegistry, create_tool_registry
from gangge.layer3_agent.tools.todo import get_state_for
from gangge.layer4_permission.guard import PermissionGuard
from gangge.layer5_llm.base import BaseLLM
from gangge.runtime.workspace_context import (
    build_initial_file_registry,
    build_project_map,
    detect_git_state,
    read_memory_bank,
    scan_project_context,
)

logger = logging.getLogger(__name__)

#: Injected when the task runs in plan mode. Kept here (not in the desktop
#: translation table) so both shells ask for the same thing.
PLAN_MODE_PROMPT = (
    "\n\n## 计划模式\n"
    "先给出完整的执行计划（步骤、涉及的文件、验证方式），等用户确认后再修改文件。"
)

ProgressCallback = Callable[[str], None]
AsyncAskUser = Callable[[str], Awaitable[str]]
AsyncAskPermission = Callable[[Any], Awaitable[Any]]


def aggregate_turn_messages(messages: list, start_index: int = 0) -> list[dict]:
    """Collapse one turn's ``Message`` objects into the JSON shape ``SessionDB`` stores.

    Rounds are grouped the way the providers expect them back: user → assistant
    (text + tool_use blocks) → tool results. Hosts used to each re-implement this
    and drift; the web shell needs the same layout the desktop stores, otherwise
    a session resumed in the other shell reads back wrong.
    """
    from gangge.layer5_llm.base import ContentType

    out: list[dict] = []
    for msg in messages[start_index:]:
        role = getattr(msg.role, "value", str(msg.role))
        if role == "user":
            out.append({"role": "user", "content": msg.get_text()})
        elif role == "assistant":
            blocks: list[dict] = []
            for block in getattr(msg, "content", []) or []:
                if block.type == ContentType.TEXT and (block.text or "").strip():
                    blocks.append({"type": "text", "text": block.text})
                elif block.type == ContentType.TOOL_USE:
                    blocks.append({
                        "type": "tool_use",
                        "id": block.tool_call_id,
                        "name": block.tool_name,
                        "input": block.tool_input,
                    })
            if blocks:
                out.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            for block in getattr(msg, "content", []) or []:
                if block.type == ContentType.TOOL_RESULT:
                    out.append({
                        "role": "tool",
                        "tool_use_id": block.tool_call_id,
                        "content": block.text or "",
                        "is_error": bool(block.is_error),
                    })
    return out


@dataclass
class TaskSpec:
    """Everything a host knows about the task before the loop starts.

    Fields mirror ``LoopConfig`` one-to-one; a host that already assembled
    context (desktop does, in the GUI thread, to keep the UI responsive) can
    pass it in and set ``auto_inject=False`` to skip the rescan.
    """

    task: str
    workspace: str
    max_rounds: int = 30
    plan_mode: bool = False
    auto_inject: bool = True
    ganggerules: str = ""
    system_prompt_extra: str = ""
    project_context: str = ""
    project_map: str = ""
    file_registry: dict[str, dict] = field(default_factory=dict)
    memory_bank_progress: str = ""
    memory_bank_changelog: str = ""
    tool_result_chars: int = 0
    history_rounds: int = 0
    auto_allow: bool = False
    attachments: list = field(default_factory=list)


@dataclass
class LoopBundle:
    """A ready-to-run loop plus the handles a host needs to drive and dispose it."""

    loop: AgenticLoop
    registry: ToolRegistry
    guard: PermissionGuard
    pipeline: Any
    system_prompt: str
    notes: list[str] = field(default_factory=list)
    trace_path: Path | None = None

    def record(self, event: dict) -> None:
        """Append one line to this task's trace (``.gangge/trace.jsonl``).

        The global log mixes every task together; when a run goes wrong the
        question is always "what did *this* task do", and the answer should not
        require grepping interleaved output from four concurrent tabs.
        """
        if self.trace_path is None:
            return
        try:
            self.trace_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"ts": datetime.now().isoformat(timespec="seconds"), **event}
            with self.trace_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001 - tracing must never fail a task
            logger.debug("[Session] trace 写入失败", exc_info=True)

    def dispose(self) -> None:
        """Release pipeline resources owned by this task."""
        try:
            if self.pipeline is not None and hasattr(self.pipeline, "dispose"):
                self.pipeline.dispose()
        except Exception:  # noqa: BLE001 - teardown must not raise
            logger.debug("[Session] dispose 失败", exc_info=True)


async def build_loop(
    spec: TaskSpec,
    *,
    llm: BaseLLM,
    multimodal_llm: Any | None = None,
    ask_user: AsyncAskUser | None = None,
    ask_permission: AsyncAskPermission | None = None,
    cancel_check: Callable[[], bool] | None = None,
    emit: ProgressCallback | None = None,
) -> LoopBundle:
    """Build a loop for ``spec``.

    ``ask_permission`` has no default that allows anything: a host that forgets
    to wire a confirmation UI gets denials, which is the failure mode you want
    when the alternative is silently writing files over the network.
    """
    notes: list[str] = []

    def _note(text: str) -> None:
        notes.append(text)
        if emit is not None:
            try:
                emit(text)
            except Exception:  # noqa: BLE001 - a broken UI must not fail the task
                logger.debug("[Session] emit 回调异常", exc_info=True)

    # ── 1. Project context ──
    if spec.auto_inject:
        _note("⏳ 正在加载项目上下文...")
        spec.project_context = scan_project_context(spec.workspace) or spec.project_context
        spec.project_map = build_project_map(spec.workspace) or spec.project_map
        spec.file_registry = build_initial_file_registry(spec.workspace) or spec.file_registry
        if not spec.ganggerules:
            rules_path = os.path.join(spec.workspace, ".ganggerules")
            if os.path.isfile(rules_path):
                try:
                    with open(rules_path, encoding="utf-8", errors="replace") as fh:
                        spec.ganggerules = fh.read()[:3000]
                except Exception:
                    pass
        progress, changelog = read_memory_bank(spec.workspace)
        spec.memory_bank_progress = progress or spec.memory_bank_progress
        spec.memory_bank_changelog = changelog or spec.memory_bank_changelog
        git_state = detect_git_state(spec.workspace)
        if git_state:
            spec.project_context += "\n\n## Git 状态\n" + git_state
        _note("✅ 项目上下文加载完成")

    # ── 2. Permission guard ──
    async def _permission_callback(request: Any) -> Any:
        from gangge.layer4_permission.guard import PermissionDecision

        if spec.auto_allow:
            return PermissionDecision.ALLOW
        if ask_permission is None:
            return PermissionDecision.DENY
        return await ask_permission(request)

    guard = PermissionGuard(
        ask_callback=_permission_callback,
        policy_path=os.environ.get("GANGGE_PERMISSION_POLICY") or None,
    )

    async def _default_ask_user(_question: str) -> str:
        return ""

    # ── 3. Tools ──
    _note("⏱ 正在注册工具...")
    registry = create_tool_registry(
        workspace=spec.workspace,
        ask_user_callback=ask_user or _default_ask_user,
        llm=llm,
        multimodal_llm=multimodal_llm,
        attachments=spec.attachments,
    )

    # ── 4. Guardrail pipeline (schema repair / result pruning / repeat guard) ──
    from gangge.runtime.pipeline import attach_llm, tool_pipeline

    tool_chars = spec.tool_result_chars or int(
        os.environ.get("TOOL_RESULT_MAX_CHARS", "6000") or 6000
    )
    tool_service, pipeline_ctx = tool_pipeline(
        registry,
        workspace=spec.workspace,
        max_chars=tool_chars,
        repeat_limit=3,
    )
    llm_factory = attach_llm(pipeline_ctx)

    # ── 5. System prompt ──
    system_text = build_system_prompt(
        workspace_dir=spec.workspace,
        project_context=spec.project_context,
        plan_mode=spec.plan_mode,
    )
    if spec.system_prompt_extra:
        system_text += f"\n\n## 额外指令\n\n{spec.system_prompt_extra}"
    if spec.plan_mode:
        system_text += PLAN_MODE_PROMPT

    # ── 6. Loop config ──
    config = LoopConfig(
        max_tool_rounds=spec.max_rounds,
        workspace_dir=spec.workspace,
        system_prompt=system_text,
        plan_mode=spec.plan_mode,
        project_map=spec.project_map,
        file_registry=spec.file_registry,
        memory_bank_progress=spec.memory_bank_progress,
        memory_bank_changelog=spec.memory_bank_changelog,
        ganggerules=spec.ganggerules,
        ask_user_callback=ask_user,
        tool_result_max_chars=tool_chars,
    )
    if spec.history_rounds:
        config.max_history_rounds = spec.history_rounds

    # ── 7. Symbol table + dependency graph ──
    try:
        from gangge.layer4_tools.repo_index import (
            build_dependency_graph,
            format_symbol_table,
            get_or_build_index,
        )

        index = get_or_build_index(spec.workspace)
        config.symbol_table = format_symbol_table(index)
        dep_graph = build_dependency_graph(index, spec.workspace)
        if dep_graph:
            lines = ["## 文件依赖关系 (修改文件前请检查影响范围)"]
            for path, deps in sorted(dep_graph.items()):
                lines.append(f"- `{path}` ← {', '.join(f'`{d}`' for d in deps)}")
            config.dep_graph_summary = "\n".join(lines[:80])
    except Exception:  # noqa: BLE001 - index is an optimization, never a blocker
        logger.debug("[Session] 代码索引构建失败", exc_info=True)

    # ── 8. Loop ──
    loop = AgenticLoop(
        llm=llm,
        tools=registry,
        permission_guard=guard,
        config=config,
        cancel_check=cancel_check,
        tool_service=tool_service,
        context=pipeline_ctx,
        llm_factory=llm_factory,
        todo_state=get_state_for(spec.workspace),
    )

    trace_path = Path(spec.workspace) / ".gangge" / "trace.jsonl"
    return LoopBundle(
        loop=loop,
        registry=registry,
        guard=guard,
        pipeline=pipeline_ctx,
        system_prompt=system_text,
        notes=notes,
        trace_path=trace_path,
    )
