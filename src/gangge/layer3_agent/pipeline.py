"""Pipeline — Codex-style Plan → Execute → Verify architecture.

Instead of a ReAct loop (Think → Act → Observe → Think → ...),
this implements a three-phase pipeline:

1. Planner: One LLM call → generates a task list (JSON)
2. Executor: Executes tasks sequentially, no LLM thinking needed
3. Verifier: One LLM call → checks results, reports

If the Planner fails to produce a valid plan, falls back to
the classic ReAct loop for robustness.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Awaitable

from gangge.layer3_agent.tools.registry import ToolRegistry
from gangge.layer3_agent.tools.base import ToolResult
from gangge.layer3_agent.prompts.system import build_system_prompt, detect_empty_workspace
from gangge.layer3_agent.progress_emitter import ProgressEmitter, EventType
from gangge.i18n import t
from gangge.layer4_tools.mcp_client import MCPClientManager
from gangge.layer4_permission.guard import (
    PermissionGuard,
    PermissionDecision,
)
from gangge.layer5_llm.base import (
    BaseLLM,
    ContentBlock,
    ContentType,
    LLMResponse,
    Message,
    Role,
)

logger = logging.getLogger(__name__)


# ── Data structures ──

@dataclass
class PlanStep:
    """A single step in the execution plan."""
    step: int
    description: str
    tool: str
    args: dict[str, Any]
    # Optional: for steps that need LLM-generated content
    content_prompt: str = ""  # e.g. "Write a FastAPI main.py with user CRUD"


@dataclass
class ExecutionPlan:
    """The full execution plan from the Planner."""
    task_summary: str
    steps: list[PlanStep]
    tech_stack: str = ""
    file_structure: str = ""


@dataclass
class StepResult:
    """Result of executing a single plan step."""
    step: int
    tool_name: str
    success: bool
    output: str
    is_error: bool = False
    elapsed_ms: int = 0


@dataclass
class PipelineResult:
    """Result of the full pipeline execution."""
    final_response: str
    plan: ExecutionPlan | None = None
    step_results: list[StepResult] = field(default_factory=list)
    total_tokens: dict[str, int] = field(default_factory=dict)
    fallback_used: bool = False  # True if fell back to ReAct


# ── Planner Prompt ──

PLANNER_SYSTEM_PROMPT = """你是 Gangge Code 的任务规划器。你的唯一职责是将用户任务拆解为可执行的步骤列表。

## 规则

1. 分析用户任务，输出一个 JSON 格式的执行计划
2. 每个步骤必须指定工具名和参数
3. 步骤按依赖顺序排列
4. 尽量一步到位，减少步骤数量
5. 不要输出任何解释文字，只输出 JSON

## 可用工具

{tool_list}

## 输出格式

```json
{{
  "task_summary": "一句话概括任务",
  "tech_stack": "使用的技术栈",
  "file_structure": "将要创建的文件列表",
  "steps": [
    {{
      "step": 1,
      "description": "步骤描述",
      "tool": "工具名",
      "args": {{ "参数名": "参数值" }},
      "content_prompt": "（仅 write_file/edit_file 需要）要写的内容描述"
    }}
  ]
}}
```

## 重要

- write_file 的 args 必须包含 path 和 content
- 如果任务模糊（缺少关键信息），使用 ask_user 工具
- 如果工作目录不为空，先用 list_dir 了解结构
- bash 工具用于安装依赖、运行测试等
- 每个步骤尽量独立，不依赖前面步骤的输出
"""

PLANNER_USER_TEMPLATE = """## 任务
{task}

## 工作目录状态
{workspace_status}

## 项目上下文
{project_context}

请输出执行计划 JSON。"""


# ── Executor ──

class PlanExecutor:
    """Executes a plan step by step, no LLM thinking required."""

    def __init__(
        self,
        tools: ToolRegistry,
        guard: PermissionGuard,
        mcp_manager: MCPClientManager | None = None,
        ask_user_callback: Callable[[str], Awaitable[str]] | None = None,
        emitter: ProgressEmitter | None = None,
        stream_callback: Callable[[ContentBlock], Awaitable[None]] | None = None,
        workspace_dir: str = ".",
        tool_result_max_chars: int = 6000,
    ):
        self.tools = tools
        self.guard = guard
        self.mcp_manager = mcp_manager
        self.ask_user_callback = ask_user_callback
        self.emitter = emitter or ProgressEmitter()
        self._stream_callback = stream_callback
        self.workspace_dir = workspace_dir
        self.tool_result_max_chars = tool_result_max_chars

    async def _emit(self, block: ContentBlock) -> None:
        if self._stream_callback:
            await self._stream_callback(block)

    async def execute_step(self, step: PlanStep) -> StepResult:
        """Execute a single plan step."""
        tool_name = step.tool
        tool_input = step.args

        # If this is a write_file/edit_file with a content_prompt,
        # we need LLM to generate the content — this is handled by
        # the caller (pipeline) which does a targeted LLM call.
        # For now, the Planner should have filled in content directly.

        # Permission check
        action = tool_input.get("command", "") if tool_name == "bash" else tool_input.get("path", tool_name)
        perm_result = await self.guard.check(
            tool_name=tool_name,
            action=action,
            context={"input": tool_input},
        )

        if perm_result.decision == PermissionDecision.DENY:
            return StepResult(
                step=step.step,
                tool_name=tool_name,
                success=False,
                output=f"权限被拒绝: {perm_result.reason}",
                is_error=True,
            )

        # Execute tool
        _t0 = time.monotonic()

        if tool_name == "ask_user":
            question = tool_input.get("question", "")
            await self._emit(ContentBlock(
                type=ContentType.TEXT,
                text=f"\n[yellow]❓ {question}[/yellow]\n",
            ))
            if self.ask_user_callback:
                user_answer = await self.ask_user_callback(question)
            else:
                user_answer = ""
            result = ToolResult(output=user_answer or "(用户未提供输入)")
        elif "__" in tool_name and self.mcp_manager:
            output = self.mcp_manager.call_tool(tool_name, tool_input)
            result = ToolResult(output=output, is_error=output.startswith("[错误]"))
        else:
            result = await self.tools.execute(tool_name, tool_input)

        _elapsed = int((time.monotonic() - _t0) * 1000)

        # Truncate
        result_output = result.output
        if len(result_output) > self.tool_result_max_chars:
            result_output = result_output[:self.tool_result_max_chars] + f"\n...[截断，共{len(result_output)}字符]"

        return StepResult(
            step=step.step,
            tool_name=tool_name,
            success=not result.is_error,
            output=result_output,
            is_error=result.is_error,
            elapsed_ms=_elapsed,
        )

    async def execute_plan(self, plan: ExecutionPlan) -> list[StepResult]:
        """Execute all steps in the plan sequentially."""
        results = []
        total = len(plan.steps)

        await self._emit(ContentBlock(
            type=ContentType.TEXT,
            text=f"\n📋 执行计划: {plan.task_summary}\n"
                 f"共 {total} 个步骤\n\n",
        ))

        for step in plan.steps:
            self.emitter.emit(EventType.ROUND, f"步骤 {step.step}/{total}")
            await self._emit(ContentBlock(
                type=ContentType.TEXT,
                text=f"▶ 步骤 {step.step}/{total}: {step.description}\n",
            ))

            result = await self.execute_step(step)
            results.append(result)

            status = "✓" if result.success else "✗"
            await self._emit(ContentBlock(
                type=ContentType.TEXT,
                text=f"  {status} {result.tool_name} ({result.elapsed_ms}ms)\n",
            ))

            # If a step fails critically, stop execution
            if result.is_error and result.tool_name in ("write_file", "edit_file"):
                # For file operations, a failure usually means we should stop
                # and let the verifier handle it
                await self._emit(ContentBlock(
                    type=ContentType.TEXT,
                    text=f"  ⚠️ 文件操作失败，停止执行\n",
                ))
                break

            # Auto lint check after file modifications
            if result.success and step.tool in ("write_file", "edit_file"):
                path = step.args.get("path", "")
                if path:
                    lint_summary = await self._auto_lint(path)
                    if lint_summary:
                        await self._emit(ContentBlock(
                            type=ContentType.TEXT,
                            text=f"  [lint] {lint_summary}\n",
                        ))

        return results

    async def _auto_lint(self, file_path: str) -> str:
        """Quick lint check on a modified file."""
        try:
            from gangge.layer3_agent.tools.lint_check import LintCheckTool
            checker = LintCheckTool(workspace=self.workspace_dir)
            result = await checker.execute(path=file_path)
            if result.is_error:
                return result.output[:200]
            return ""
        except Exception:
            return ""


# ── Verifier Prompt ──

VERIFIER_SYSTEM_PROMPT = """你是 Gangge Code 的验证器。检查任务执行结果，报告完成情况。

## 规则

1. 分析执行结果，判断任务是否完成
2. 如果有错误，说明原因和建议
3. 输出简洁的完成报告
4. 如果需要继续工作，说明剩余任务

## 输出格式

```
## 任务结果

**状态**: 完成/部分完成/失败
**摘要**: 一句话概括

### 已完成
- ...

### 问题（如有）
- ...

### 建议的后续步骤（如有）
- ...
```
"""

VERIFIER_USER_TEMPLATE = """## 原始任务
{task}

## 执行计划
{plan_summary}

## 执行结果
{results_summary}

## 工作目录
{workspace_dir}

请验证任务完成情况。"""


# ── Content Generator Prompt ──
# For steps that need LLM-generated content (write_file with content_prompt)

CONTENT_GENERATOR_PROMPT = """你是代码生成器。根据描述生成完整的文件内容。

## 要求
- 生成完整、可运行的代码
- 不写占位符（禁止 TODO、pass）
- 遵循项目现有风格
- 只输出文件内容，不要解释

## 文件路径
{path}

## 内容要求
{prompt}

## 项目上下文
{context}

请直接输出文件内容，不要用代码块包裹。"""


# ── Main Pipeline ──

class CodexPipeline:
    """Codex-style Plan → Execute → Verify pipeline.

    This replaces the ReAct loop with a structured three-phase approach:
    1. Planner: One LLM call to generate an execution plan
    2. Executor: Execute the plan step by step (no LLM thinking)
    3. Verifier: One LLM call to check results

    Falls back to ReAct loop if planning fails.
    """

    def __init__(
        self,
        llm: BaseLLM,
        tools: ToolRegistry,
        guard: PermissionGuard,
        config: Any = None,  # LoopConfig
    ):
        self.llm = llm
        self.tools = tools
        self.guard = guard
        self.config = config
        self.emitter = ProgressEmitter()
        self._stream_callback: Callable[[ContentBlock], Awaitable[None]] | None = None
        self.mcp_manager: MCPClientManager | None = None

        # Initialize MCP
        self._init_mcp()

    def set_stream_callback(self, callback: Callable[[ContentBlock], Awaitable[None]]) -> None:
        self._stream_callback = callback

    def _init_mcp(self):
        ws = Path(self.config.workspace_dir) if self.config and self.config.workspace_dir else Path(".")
        config_path = ws / ".gangge" / "mcp_servers.json"
        try:
            self.mcp_manager = MCPClientManager.from_config_file(str(config_path))
            self.mcp_manager.connect_all()
            tools = self.mcp_manager.get_all_tools()
            if tools:
                names = [t.full_name for t in tools]
                logger.info(f"[MCP] 已加载 {len(tools)} 个外部工具: {', '.join(names)}")
        except Exception as e:
            logger.info(f"[MCP] 初始化跳过: {e}")
            self.mcp_manager = None

    async def _emit(self, block: ContentBlock) -> None:
        if self._stream_callback:
            await self._stream_callback(block)

    def _get_tool_list_str(self) -> str:
        """Get a concise tool list for the planner prompt."""
        defs = list(self.tools.get_definitions())
        if self.mcp_manager:
            defs.extend(self.mcp_manager.build_tool_definitions())
        lines = []
        for d in defs:
            lines.append(f"- {d.name}: {d.description}")
        return "\n".join(lines)

    # ── Phase 1: Planner ──

    async def plan(self, user_task: str, messages: list[Message]) -> ExecutionPlan | None:
        """Generate an execution plan from the user task.

        Returns None if planning fails (will trigger ReAct fallback).
        """
        # Build tool list for planner
        tool_list = self._get_tool_list_str()
        system = PLANNER_SYSTEM_PROMPT.format(tool_list=tool_list)

        # Workspace status
        workspace_dir = self.config.workspace_dir if self.config else "."
        is_empty = detect_empty_workspace(workspace_dir)
        workspace_status = "空目录，从零开始" if is_empty else f"已有项目: {workspace_dir}"

        # Project context
        project_context = ""
        if self.config:
            if self.config.project_context:
                project_context = self.config.project_context[:500]
            if self.config.memory_bank_progress:
                project_context += f"\n\n进度: {self.config.memory_bank_progress[:300]}"

        user_msg = PLANNER_USER_TEMPLATE.format(
            task=user_task,
            workspace_status=workspace_status,
            project_context=project_context or "无",
        )

        await self._emit(ContentBlock(
            type=ContentType.TEXT,
            text="🧠 正在规划任务...\n",
        ))

        try:
            response = await asyncio.wait_for(
                self.llm.chat(
                    messages=[Message(role=Role.USER, content=[ContentBlock(type=ContentType.TEXT, text=user_msg)])],
                    tools=None,  # No tools for planner — it outputs JSON
                    system=system,
                ),
                timeout=60.0,
            )

            plan_text = response.text or ""

            # Extract JSON from response
            plan = self._parse_plan(plan_text)
            if plan:
                logger.info(f"[Planner] 生成计划: {plan.task_summary}, {len(plan.steps)} 个步骤")
                return plan

            logger.warning("[Planner] 无法解析计划 JSON，将回退到 ReAct 模式")
            return None

        except asyncio.TimeoutError:
            logger.warning("[Planner] 规划超时，将回退到 ReAct 模式")
            return None
        except Exception as e:
            logger.warning(f"[Planner] 规划失败: {e}，将回退到 ReAct 模式")
            return None

    def _parse_plan(self, text: str) -> ExecutionPlan | None:
        """Parse the planner's JSON output into an ExecutionPlan."""
        # Try to extract JSON from the response
        # The LLM might wrap it in ```json ... ```
        json_str = text

        # Extract from code block
        m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if m:
            json_str = m.group(1).strip()

        # Try to find raw JSON object
        if not json_str.startswith("{"):
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                json_str = m.group(0)

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            return None

        if "steps" not in data or not isinstance(data["steps"], list):
            return None

        steps = []
        for i, s in enumerate(data["steps"]):
            if not isinstance(s, dict):
                continue
            step = PlanStep(
                step=s.get("step", i + 1),
                description=s.get("description", ""),
                tool=s.get("tool", ""),
                args=s.get("args", {}),
                content_prompt=s.get("content_prompt", ""),
            )
            if step.tool:  # Must have a tool name
                steps.append(step)

        if not steps:
            return None

        return ExecutionPlan(
            task_summary=data.get("task_summary", ""),
            steps=steps,
            tech_stack=data.get("tech_stack", ""),
            file_structure=data.get("file_structure", ""),
        )

    # ── Phase 1.5: Content Generation ──
    # For write_file steps that have a content_prompt instead of direct content,
    # we need an LLM call to generate the actual file content.

    async def _generate_content(self, step: PlanStep, plan: ExecutionPlan) -> str:
        """Generate file content for a write_file step that has a content_prompt."""
        if not step.content_prompt:
            return step.args.get("content", "")

        system = CONTENT_GENERATOR_PROMPT.format(
            path=step.args.get("path", "unknown"),
            prompt=step.content_prompt,
            context=f"技术栈: {plan.tech_stack}\n文件结构: {plan.file_structure}",
        )

        try:
            response = await asyncio.wait_for(
                self.llm.chat(
                    messages=[Message(role=Role.USER, content=[ContentBlock(
                        type=ContentType.TEXT,
                        text=f"请生成 {step.args.get('path', '')} 的内容",
                    )])],
                    tools=None,
                    system=system,
                ),
                timeout=60.0,
            )
            content = response.text or ""
            # Strip code block wrappers if present
            content = re.sub(r"^```\w*\n?", "", content)
            content = re.sub(r"\n?```$", "", content)
            return content.strip()
        except Exception as e:
            logger.warning(f"[ContentGen] 生成内容失败: {e}")
            return step.args.get("content", "")

    # ── Phase 2: Executor ──

    async def execute(self, plan: ExecutionPlan) -> list[StepResult]:
        """Execute the plan step by step."""
        executor = PlanExecutor(
            tools=self.tools,
            guard=self.guard,
            mcp_manager=self.mcp_manager,
            ask_user_callback=self.config.ask_user_callback if self.config else None,
            emitter=self.emitter,
            stream_callback=self._stream_callback,
            workspace_dir=self.config.workspace_dir if self.config else ".",
            tool_result_max_chars=self.config.tool_result_max_chars if self.config else 6000,
        )

        # Pre-generate content for write_file steps that need it
        for step in plan.steps:
            if step.tool in ("write_file", "edit_file") and step.content_prompt and not step.args.get("content"):
                content = await self._generate_content(step, plan)
                step.args["content"] = content

        return await executor.execute_plan(plan)

    # ── Phase 3: Verifier ──

    async def verify(
        self,
        user_task: str,
        plan: ExecutionPlan,
        results: list[StepResult],
    ) -> str:
        """Verify the execution results with one LLM call."""
        plan_summary = f"任务: {plan.task_summary}\n"
        plan_summary += f"步骤数: {len(plan.steps)}\n"
        for s in plan.steps:
            plan_summary += f"  {s.step}. {s.description} ({s.tool})\n"

        results_summary = ""
        success_count = sum(1 for r in results if r.success)
        results_summary += f"成功: {success_count}/{len(results)}\n"
        for r in results:
            status = "✓" if r.success else "✗"
            results_summary += f"  {status} 步骤{r.step}: {r.tool_name} — {r.output[:100]}\n"

        workspace_dir = self.config.workspace_dir if self.config else "."

        user_msg = VERIFIER_USER_TEMPLATE.format(
            task=user_task,
            plan_summary=plan_summary,
            results_summary=results_summary,
            workspace_dir=workspace_dir,
        )

        await self._emit(ContentBlock(
            type=ContentType.TEXT,
            text="🔍 正在验证结果...\n",
        ))

        try:
            response = await asyncio.wait_for(
                self.llm.chat(
                    messages=[Message(role=Role.USER, content=[ContentBlock(type=ContentType.TEXT, text=user_msg)])],
                    tools=None,
                    system=VERIFIER_SYSTEM_PROMPT,
                ),
                timeout=30.0,
            )
            return response.text or "验证完成"
        except Exception as e:
            return f"验证调用失败: {e}"

    # ── Main Entry Point ──

    async def run(self, messages: list[Message]) -> PipelineResult:
        """Run the full Plan → Execute → Verify pipeline.

        Falls back to ReAct if planning fails.
        """
        # Extract user task from messages
        user_task = ""
        for msg in reversed(messages):
            if msg.role == Role.USER:
                text = msg.get_text().strip()
                if text and not text.startswith("[系统提示]"):
                    user_task = text
                    break

        if not user_task:
            return PipelineResult(
                final_response="未收到用户任务",
                fallback_used=False,
            )

        # ── Shadow Git: auto-checkpoint ──
        shadow_checkpoint = None
        workspace_dir = self.config.workspace_dir if self.config else ""
        if workspace_dir:
            try:
                from gangge.layer4_tools.shadow_git import ShadowGit
                sg = ShadowGit(workspace_dir)
                if sg.is_available() or sg.ensure_init():
                    shadow_checkpoint = sg.checkpoint(f"checkpoint: before task — {user_task[:80]}")
            except Exception:
                pass

        # ── Phase 1: Plan ──
        plan = await self.plan(user_task, messages)

        if not plan:
            # Planning failed — signal fallback to ReAct
            await self._emit(ContentBlock(
                type=ContentType.TEXT,
                text="⚠️ 规划失败，回退到交互模式\n",
            ))
            return PipelineResult(
                final_response="",
                fallback_used=True,
            )

        # Display plan
        await self._emit(ContentBlock(
            type=ContentType.TEXT,
            text=f"\n📋 任务: {plan.task_summary}\n"
                 f"技术栈: {plan.tech_stack}\n"
                 f"步骤: {len(plan.steps)} 个\n\n",
        ))

        # ── Phase 2: Execute ──
        results = await self.execute(plan)

        # ── Phase 3: Verify ──
        verification = await self.verify(user_task, plan, results)

        # ── Shadow Git: post-task checkpoint ──
        any_file_modified = any(
            r.success and r.tool_name in ("write_file", "edit_file")
            for r in results
        )
        if workspace_dir and any_file_modified:
            try:
                from gangge.layer4_tools.shadow_git import ShadowGit
                sg = ShadowGit(workspace_dir)
                sg.checkpoint(f"checkpoint: completed task — {user_task[:80]}")
            except Exception:
                pass

        # ── Update Memory Bank ──
        self._update_memory_bank(plan, results, user_task)

        self.emitter.emit_done(total_steps=len(plan.steps))

        return PipelineResult(
            final_response=verification,
            plan=plan,
            step_results=results,
            fallback_used=False,
        )

    def _update_memory_bank(self, plan: ExecutionPlan, results: list[StepResult], user_task: str):
        """Update .gangge/ progress and changelog files."""
        workspace_dir = self.config.workspace_dir if self.config else ""
        if not workspace_dir:
            return

        gangge_dir = Path(workspace_dir) / ".gangge"
        gangge_dir.mkdir(parents=True, exist_ok=True)

        # Progress
        success_count = sum(1 for r in results if r.success)
        total_count = len(results)
        progress_pct = int(success_count / max(total_count, 1) * 100)

        written_files = [
            r for r in results
            if r.success and r.tool_name in ("write_file", "edit_file")
        ]

        progress_lines = [
            f"进度: {progress_pct}%",
            f"任务: {plan.task_summary}",
            f"最后更新: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "",
            "### 已完成",
        ]
        for r in results:
            status = "✓" if r.success else "✗"
            progress_lines.append(f"- [{status}] 步骤{r.step}: {plan.steps[r.step-1].description if r.step <= len(plan.steps) else '?'}")

        if progress_pct < 100:
            progress_lines.append("")
            progress_lines.append("### 下一步")
            progress_lines.append("- [ ] 继续完成剩余步骤")

        try:
            (gangge_dir / "progress.md").write_text(
                f"# 项目进度\n\n" + "\n".join(progress_lines) + "\n",
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[Memory Bank] 写入 progress.md 失败: {e}")

        # Changelog
        changelog_entry = (
            f"\n## {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"- 任务: {plan.task_summary}\n"
            f"- 完成: {success_count}/{total_count} 步骤\n"
        )
        if written_files:
            paths = [plan.steps[r.step-1].args.get("path", "?") for r in written_files if r.step <= len(plan.steps)]
            changelog_entry += f"- 修改文件: {', '.join(paths)}\n"

        try:
            changelog_file = gangge_dir / "changelog.md"
            existing = changelog_file.read_text(encoding="utf-8") if changelog_file.exists() else ""
            changelog_file.write_text(existing + changelog_entry, encoding="utf-8")
        except Exception as e:
            logger.warning(f"[Memory Bank] 写入 changelog.md 失败: {e}")
