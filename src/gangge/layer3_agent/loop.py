"""Agentic Loop — the core engine of the AI assistant.

Implements the Plan & Execute pattern:
1. Send messages to LLM
2. If tool_use → check permission → execute → add result → loop
3. If end_turn → return final response
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Awaitable

from gangge.layer3_agent.tools.registry import ToolRegistry
from gangge.layer3_agent.tools.base import ToolResult
from gangge.layer3_agent.prompts.system import build_system_prompt, build_dynamic_state_text, detect_empty_workspace
from gangge.layer3_agent.progress_emitter import ProgressEmitter, EventType
from gangge.i18n import t
from gangge.layer4_tools.mcp_client import MCPClientManager
from gangge.layer4_permission.guard import (
    PermissionGuard,
    PermissionDecision,
    PermissionRequest,
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


class TurnBuffer:
    """
    方案C：单轮对话暂存区。
    收集 LLM 文字输出、tool_use、tool_result，
    聚合成标准 LLM 消息格式供存入 DB。
    """

    def __init__(self):
        self.text_parts: list[str] = []
        self.tool_uses: list[dict] = []
        self.tool_results: list[dict] = []

    def add_text(self, text: str):
        self.text_parts.append(text)

    def add_tool_use(self, tool_use_id: str, tool_name: str, tool_input: dict):
        self.tool_uses.append({
            "type": "tool_use",
            "id": tool_use_id,
            "name": tool_name,
            "input": tool_input,
        })

    def add_tool_result(self, tool_use_id: str, output: str, is_error: bool = False):
        self.tool_results.append({
            "role": "tool",
            "tool_use_id": tool_use_id,
            "content": output,
            "is_error": is_error,
        })

    def to_db_messages(self) -> list[dict]:
        """聚合成标准 LLM 消息格式。"""
        content = []
        full_text = "".join(self.text_parts).strip()
        if full_text:
            content.append({"type": "text", "text": full_text})
        content.extend(self.tool_uses)

        messages = []
        if content:
            messages.append({"role": "assistant", "content": content})
        messages.extend(self.tool_results)
        return messages

    def is_empty(self) -> bool:
        return not self.text_parts and not self.tool_uses and not self.tool_results


@dataclass
class LoopConfig:
    """Configuration for the agentic loop."""

    max_tool_rounds: int = 30        # Max tool-call iterations (可在 .env 中通过 MAX_ROUNDS 覆盖)
    llm_timeout: float = float(os.environ.get("LLM_TIMEOUT", "600"))  # LLM response timeout in seconds (本地模型/思考型模型可能较慢，可通过 LLM_TIMEOUT 覆盖)
    max_tokens: int = 8192
    system_prompt: str = ""
    workspace_dir: str = "."
    project_context: str = ""
    plan_mode: bool = False

    # ── Project map: file index injected into system prompt ──
    project_map: str = ""

    # ── Symbol table & dependency graph from repo_index ──
    symbol_table: str = ""
    dep_graph_summary: str = ""

    # ── File registry: tracks all file modifications ──
    file_registry: dict[str, dict] = field(default_factory=dict)
    # {"src/main.py": {"classes":["App"],"functions":["main"],"last_action":"write","round":5}}

    # ── Summary compression: auto-compress old rounds ──
    enable_summary_compression: bool = True
    summary_compression_interval: int = 5  # every N rounds

    # ── Sliding window: keep only recent N rounds, discard older ──
    enable_sliding_window: bool = True
    max_history_rounds: int = 6  # only keep recent N user/assistant pairs

    # ── Tool result truncation ──
    enable_tool_result_truncation: bool = True
    tool_result_max_chars: int = 6000

    # ── Lazy project map: only inject full index on first round ──
    enable_lazy_project_map: bool = True

    # ── Memory Bank: project-level progress tracking ──
    memory_bank_progress: str = ""
    memory_bank_changelog: str = ""
    memory_bank_decisions: str = ""

    # ── .ganggerules: project-specific rules ──
    ganggerules: str = ""

    # ── ask_user callback: pause loop and wait for user input ──
    ask_user_callback: Callable[[str], Awaitable[str]] | None = None

    # ── Streaming: forward model deltas to the UI as they arrive ──
    # Off only for hosts that cannot render partial output (or adapters that do
    # not support it — those fall back to a single shot automatically).
    enable_streaming: bool = True


@dataclass
class ToolExecution:
    """Record of a single tool execution."""

    tool_name: str
    input: dict[str, Any]
    output: str
    is_error: bool = False
    permission: str = ""  # "auto" | "allowed" | "denied"
    metadata: dict[str, Any] = field(default_factory=dict)  # e.g. {"diff": "...", "before_content": "..."}


@dataclass
class LoopResult:
    """Result of the agentic loop."""

    final_response: str
    tool_executions: list[ToolExecution] = field(default_factory=list)
    total_rounds: int = 0
    total_tokens: dict[str, int] = field(default_factory=dict)
    extra: dict[str, str] = field(default_factory=dict)  # e.g. {"memory_bank_update": "..."}
    completion_reason: str = ""  # e.g. "all_todos_done", "model_end_turn", "consecutive_readonly", "max_rounds"


# Callback types
StreamCallback = Callable[[ContentBlock], Awaitable[None]]


class _DeltaBatcher:
    """Coalesce token deltas into line-sized UI updates.

    A token-level emit means one Qt queued signal or one Textual message per
    token — that floods the UI thread and *looks* slower than blocking output.
    Batching on newline (or a character budget) keeps the cadence a reader
    actually perceives, at a fraction of the wakeups.
    """

    def __init__(self, emit: Callable[[ContentBlock], Awaitable[None]], min_chars: int = 48):
        self._emit = emit
        self._min_chars = min_chars
        self._buffer: list[str] = []
        self._size = 0
        self._reasoning: list[str] = []
        self.deltas = 0          # raw text/reasoning deltas received
        self.emitted_chars = 0   # answer characters pushed to the UI
        self.thinking_chars = 0  # reasoning characters pushed to the UI

    async def __call__(self, block: ContentBlock) -> None:
        if block.type == ContentType.THINKING:
            # Reasoning is delivered as ONE block per model call, never streamed
            # token by token: a collapsible "思考过程" bubble per token is pure
            # noise (and a UI flood), while a single block is what a reader
            # actually wants to expand afterwards.
            if block.text:
                self.deltas += 1
                self._reasoning.append(block.text)
            return

        if block.type == ContentType.TEXT:
            if not block.text:
                return
            if self._reasoning:
                # Reasoning must land before the answer it explains.
                await self._flush_thinking()
            self.deltas += 1
            self._buffer.append(block.text)
            self._size += len(block.text)
            if self._size >= self._min_chars or "\n" in block.text:
                await self.flush()
            return

        await self.flush()
        await self._emit(block)

    async def _flush_thinking(self) -> None:
        if not self._reasoning:
            return
        text = "".join(self._reasoning)
        self._reasoning.clear()
        self.thinking_chars += len(text)
        await self._emit(ContentBlock(type=ContentType.THINKING, text=text))

    async def flush(self) -> None:
        await self._flush_thinking()
        if not self._buffer:
            return
        text = "".join(self._buffer)
        self._buffer.clear()
        self._size = 0
        self.emitted_chars += len(text)
        await self._emit(ContentBlock(type=ContentType.TEXT, text=text))


@dataclass
class _ToolRound:
    """What one round of tool calls changed, handed back to the turn loop."""

    results_message: Message
    any_succeeded: bool
    modified_files: bool
    productive: bool


class AgenticLoop:
    """The core agentic loop engine.

    Orchestrates LLM calls, tool executions, and permission checks.
    """

    def __init__(
        self,
        llm: BaseLLM,
        tools: ToolRegistry,
        permission_guard: PermissionGuard,
        config: LoopConfig | None = None,
        cancel_check: Callable[[], bool] | None = None,
        tool_service: Any | None = None,
        context: Any | None = None,
        llm_factory: Any | None = None,
    ):
        self.llm = llm
        # Optional request gateway (``runtime.llm``). When present every model
        # call goes through ``llm/request`` / ``llm/response``, so prompt
        # rewriting, secret redaction, tool suppression and per-task model
        # routing are plugins instead of edits here.
        self.llm_factory = llm_factory
        self.tools = tools
        # Optional execution gateway (``runtime.tools``). When present, every
        # tool call runs through the plugin pipeline — argument repair, result
        # pruning and repeat detection are attached there instead of here, so
        # the loop stays unaware of them.
        self.tool_service = tool_service
        # Optional event bus (``runtime.ctx``). When present the loop asks
        # ``agent/turn-stopping`` before accepting a stop, so "the model said
        # it is done but nothing was changed" policy lives in a plugin.
        self.context = context
        self.guard = permission_guard
        self.config = config or LoopConfig()
        self._cancel_check = cancel_check
        self._stream_callback: StreamCallback | None = None
        # ── Progress Emitter ──
        self.emitter = ProgressEmitter()
        self._text_callback: Callable[[ContentBlock], None] | None = None
        # ── PATCH: MCP Client Manager ──
        self.mcp_manager: MCPClientManager | None = None
        self._init_mcp()
        self._profile: str = ""  # set by run()
        self._llm_failures: int = 0  # consecutive LLM call failures
        self._last_streamed: int = 0  # chars already shown to the UI by streaming

    def set_stream_callback(self, callback: StreamCallback) -> None:
        """Set callback for streaming content blocks."""
        self._stream_callback = callback

    def set_text_callback(self, callback: Callable[[ContentBlock], None]) -> None:
        """Set a synchronous text callback for progress messages."""
        self._text_callback = callback

    def _init_mcp(self):
        """初始化 MCP 客户端管理器（连接外部工具服务器）。"""
        ws = Path(self.config.workspace_dir) if self.config.workspace_dir else Path(".")
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

    def _ensure_memory_bank(self) -> None:
        """Auto-load memory bank files from .gangge/ directory.

        Reads progress.md, changelog.md, and decisions.md into config fields
        so they get injected into the system prompt.
        """
        gangge_dir = Path(self.config.workspace_dir) / ".gangge"
        progress_file = gangge_dir / "progress.md"
        changelog_file = gangge_dir / "changelog.md"
        decisions_file = gangge_dir / "decisions.md"

        # Create .gangge/ with defaults if missing
        if not gangge_dir.exists():
            gangge_dir.mkdir(parents=True, exist_ok=True)
        if not progress_file.exists():
            progress_file.write_text(t("memory_progress_title"), encoding="utf-8")
        if not changelog_file.exists():
            changelog_file.write_text(t("memory_changelog_title"), encoding="utf-8")
        if not decisions_file.exists():
            decisions_file.write_text(t("memory_decisions_title"), encoding="utf-8")

        # Read content
        try:
            self.config.memory_bank_progress = progress_file.read_text(encoding="utf-8").strip()
        except Exception:
            self.config.memory_bank_progress = ""
        try:
            self.config.memory_bank_changelog = changelog_file.read_text(encoding="utf-8").strip()
        except Exception:
            self.config.memory_bank_changelog = ""
        try:
            self.config.memory_bank_decisions = decisions_file.read_text(encoding="utf-8").strip()
        except Exception:
            self.config.memory_bank_decisions = ""

    def _save_memory_bank_update(self, update_text: str) -> None:
        """Save memory bank update text to .gangge/ files.

        Parses the LLM's memory-bank block and writes to progress.md, changelog.md, and decisions.md.
        """
        if not update_text.strip():
            return
        gangge_dir = Path(self.config.workspace_dir) / ".gangge"
        progress_file = gangge_dir / "progress.md"
        changelog_file = gangge_dir / "changelog.md"
        decisions_file = gangge_dir / "decisions.md"

        import re as _re
        progress_match = _re.search(r"(?:progress|进度)[：:]\s*(.+?)(?=(?:changelog|变更日志|decision|决策)[：:]|$)", update_text, _re.IGNORECASE | _re.DOTALL)
        changelog_match = _re.search(r"(?:changelog|变更日志)[：:]\s*(.+?)(?=(?:decision|决策)[：:]|$)", update_text, _re.IGNORECASE | _re.DOTALL)
        decisions_match = _re.search(r"(?:decision|决策)[：:]\s*(.+?)$", update_text, _re.IGNORECASE | _re.DOTALL)

        if progress_match:
            new_progress = progress_match.group(1).strip()
            if new_progress:
                try:
                    progress_file.write_text(f"# 项目进度\n\n{new_progress}\n", encoding="utf-8")
                    self.config.memory_bank_progress = new_progress
                    logger.info(f"[Memory Bank] progress.md 已更新")
                except Exception as e:
                    logger.warning(f"[Memory Bank] 写入 progress.md 失败: {e}")

        if changelog_match:
            new_changelog = changelog_match.group(1).strip()
            if new_changelog:
                new_entry = f"\n## {datetime.now().strftime('%Y-%m-%d')}\n{new_changelog}\n"
                try:
                    existing = changelog_file.read_text(encoding="utf-8") if changelog_file.exists() else ""
                    changelog_file.write_text(existing + new_entry, encoding="utf-8")
                    self.config.memory_bank_changelog = new_changelog
                    logger.info(f"[Memory Bank] changelog.md 已更新")
                except Exception as e:
                    logger.warning(f"[Memory Bank] 写入 changelog.md 失败: {e}")

        if decisions_match:
            new_decision = decisions_match.group(1).strip()
            if new_decision:
                new_entry = f"\n### {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{new_decision}\n"
                try:
                    existing = decisions_file.read_text(encoding="utf-8") if decisions_file.exists() else ""
                    decisions_file.write_text(existing + new_entry, encoding="utf-8")
                    self.config.memory_bank_decisions = (existing + new_entry).strip()
                    logger.info(f"[Memory Bank] decisions.md 已更新")
                except Exception as e:
                    logger.warning(f"[Memory Bank] 写入 decisions.md 失败: {e}")

    def _auto_save_progress(
        self,
        file_registry: dict[str, dict],
        round_num: int,
        user_task_desc: str = "",
    ) -> None:
        """Auto-save progress to .gangge/progress.md after each file write.

        This is the key fix: progress.md is updated in real-time by code,
        not waiting for the LLM to output a memory-bank block.
        """
        if not self.config.workspace_dir:
            return
        gangge_dir = Path(self.config.workspace_dir) / ".gangge"
        gangge_dir.mkdir(parents=True, exist_ok=True)
        progress_file = gangge_dir / "progress.md"
        changelog_file = gangge_dir / "changelog.md"

        written_files = sorted(
            p for p, info in file_registry.items()
            if info.get("last_action") in ("write_file", "edit_file")
        )
        if not written_files:
            return

        total = len(written_files)
        progress_pct = min(100, int(total / max(total, 1) * 100))

        lines = [
            f"进度: {progress_pct}%",
            f"已修改文件数: {total}",
            f"最后更新: 第 {round_num + 1} 轮",
            "",
            "### 已完成的文件",
        ]
        for p in written_files:
            info = file_registry[p]
            action = info.get("last_action", "?")
            rnd = info.get("round", "?")
            detail = ""
            if info.get("classes"):
                detail += f" (classes: {', '.join(info['classes'][:5])})"
            if info.get("functions"):
                detail += f" (funcs: {', '.join(info['functions'][:8])})"
            lines.append(f"- [x] `{p}` [{action}, 第{rnd}轮]{detail}")

        lines.append("")
        lines.append("### 下一步")
        lines.append("- [ ] 继续完成剩余文件（参考任务清单）")

        new_progress = "\n".join(lines)
        try:
            progress_file.write_text(f"# 项目进度\n\n{new_progress}\n", encoding="utf-8")
            self.config.memory_bank_progress = new_progress
        except Exception as e:
            logger.warning(f"[Memory Bank] 自动保存 progress.md 失败: {e}")

        changelog_entry = (
            f"\n## {datetime.now().strftime('%Y-%m-%d %H:%M')} (第{round_num + 1}轮自动记录)\n"
            f"- 已完成: {', '.join(f'`{p}`' for p in written_files[-3:])}"
            f"{' 等' if total > 3 else ''}\n"
        )
        try:
            existing = changelog_file.read_text(encoding="utf-8") if changelog_file.exists() else ""
            if changelog_entry.strip() not in existing:
                changelog_file.write_text(existing + changelog_entry, encoding="utf-8")
                self.config.memory_bank_changelog = (existing + changelog_entry).strip()
        except Exception as e:
            logger.warning(f"[Memory Bank] 自动保存 changelog.md 失败: {e}")

    async def _git_checkpoint(self, label: str) -> str | None:
        """Run Shadow Git checkpoint in a thread to avoid blocking the event loop."""
        if not self.config.workspace_dir:
            return None
        try:
            from gangge.layer4_tools.shadow_git import ShadowGit
            sg = ShadowGit(self.config.workspace_dir)
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: sg.checkpoint(label) if (sg.is_available() or sg.ensure_init()) else None,
            )
            if result:
                logger.info(f"Shadow Git checkpoint: {result}")
            return result
        except Exception as e:
            logger.warning(f"Shadow Git checkpoint failed: {e}")
            return None

    def _get_all_tool_defs(self, phase: int | None = None) -> list:
        """获取工具 definitions 列表，按阶段过滤。

        Args:
            phase: 如果提供，只返回该阶段及以下的工具。
                   如果 None，返回全部工具。
        """
        defs = list(self.tools.get_definitions(phase=phase))
        if self.mcp_manager:
            mcp_defs = self.mcp_manager.build_tool_definitions()
            defs.extend(mcp_defs)
        return defs

    def _build_static_system_prompt(self) -> str:
        """Build static system prompt — called once, byte-identical across rounds.

        This enables LLM API prompt caching: only content that never changes
        during the session goes here. Dynamic state (progress, changelog,
        decisions, todo, round warnings) is injected separately as a user message.
        """
        prompt = build_system_prompt(
            workspace_dir=self.config.workspace_dir,
            project_context=self.config.project_context[:500] if self.config.project_context else "",
            plan_mode=self.config.plan_mode,
        )

        # ── Inject .ganggerules (stable per session) ──
        if self.config.ganggerules:
            prompt += f"\n\n## 项目规则 (.ganggerules)\n{self.config.ganggerules[:500]}"

        # ── Coding profile: inject test verification instruction ──
        if getattr(self, "_profile", "") == "coding":
            prompt += (
                "\n\n## 测试验证\n"
                "代码实现完成后，必须运行 pytest 验证代码正确性：\n"
                "1. 如果项目有测试文件，运行 `pytest -v` 确认现有测试通过\n"
                "2. 如果没有测试文件，创建基本测试文件并运行\n"
                "3. 测试失败则修复后再结束\n"
            )
            prompt += (
                "\n## 环境与依赖\n"
                "需要安装依赖时，直接使用 bash 运行 pip/npm/curl 等命令。\n"
                "可以从网络下载资源文件，使用 curl 或 Invoke-WebRequest。\n"
                "解压文件使用 unzip 或 Expand-Archive。\n"
            )

        # ── Novel profile: inject novel writing prompt ──
        if getattr(self, "_profile", "") == "novel":
            from gangge.layer3_agent.prompts.system import NOVEL_PROMPT
            prompt += "\n\n" + NOVEL_PROMPT

        return prompt

    @staticmethod
    def _build_completion_summary() -> str:
        """Build a completion summary from completed todo items.

        Uses the actual work done (completed todos) instead of
        the user's original message, which may be negative feedback
        or a new request — not a description of what was accomplished.
        """
        from gangge.layer3_agent.tools.todo import get_todo_state
        todo_state = get_todo_state()
        all_todos = todo_state.get_all()
        completed = [t for t in all_todos if t.get("status") == "completed"]
        if completed:
            items = "; ".join(t["content"][:40] for t in completed[:5])
            return f"completed: {items}"
        return "task completed"

    def _build_dynamic_state_text(self, round_num: int = 0) -> str:
        """Build dynamic state text that changes between rounds.

        Returns a string to be injected as a user message, NOT into system prompt.
        Keeps the static system prompt byte-identical for API caching.
        """
        # ── TodoWrite state ──
        from gangge.layer3_agent.tools.todo import get_todo_state
        todo_state = get_todo_state()
        todo_injection = todo_state.format_for_injection()

        # ── Approaching max rounds warning ──
        round_warning = ""
        remaining = self.config.max_tool_rounds - round_num
        if remaining <= 3:
            round_warning = (
                f"⚠️ 轮数即将耗尽 (剩余 {remaining} 轮)\n"
                "停止调用新工具，输出当前进度和剩余工作。\n"
            )

        return build_dynamic_state_text(
            memory_bank_progress=self.config.memory_bank_progress[:200] if self.config.memory_bank_progress else "",
            memory_bank_changelog=self.config.memory_bank_changelog[:200] if self.config.memory_bank_changelog else "",
            memory_bank_decisions=self.config.memory_bank_decisions[:300] if self.config.memory_bank_decisions else "",
            todo_injection=todo_injection,
            round_warning=round_warning,
        )

    def _emit_metrics_for_round(
        self,
        round_num: int,
        response: Any,
        total_tokens: dict[str, int],
        system: str,
        tool_defs: list[Any],
        call_messages: list[Message],
        static_system: str,
        state_text: str,
    ) -> None:
        """Emit per-round experimental metrics via ProgressEmitter.

        Pure local computation — does NOT send extra API requests.
        All data is derived from already-assembled request/response.
        """
        import hashlib

        # ── Prompt sizes (chars) ──
        system_chars = len(system or "")
        tools_chars = 0
        for td in (tool_defs or []):
            try:
                tools_chars += len(td.description) + len(json.dumps(td.input_schema, ensure_ascii=False))
            except Exception:
                pass
        messages_chars = 0
        for m in (call_messages or []):
            try:
                messages_chars += len(m.get_text())
                for b in m.content:
                    if b.type == ContentType.TOOL_USE:
                        messages_chars += len(json.dumps(b.tool_input, ensure_ascii=False))
            except Exception:
                pass
        total_prompt_chars = system_chars + tools_chars + messages_chars

        # ── Cache estimation: static system prompt hash ──
        static_hash = hashlib.md5((static_system or "").encode("utf-8")).hexdigest()[:12]
        dynamic_state_chars = len(state_text or "")
        # First round: no cache possible. Later rounds: cache hit if static hash unchanged.
        prev_hash = getattr(self, "_prev_static_hash", None)
        cache_estimated_hit = bool(prev_hash) and prev_hash == static_hash
        self._prev_static_hash = static_hash

        # ── Token usage ──
        input_tokens = response.usage.get("input_tokens", 0) if response.usage else 0
        output_tokens = response.usage.get("output_tokens", 0) if response.usage else 0
        cumulative_input = total_tokens.get("input", 0)
        cumulative_output = total_tokens.get("output", 0)

        # ── Context window usage ──
        context_window = self.llm.max_context_tokens if self.llm else 0
        usage_ratio = (cumulative_input / context_window) if context_window > 0 else 0.0

        # ── Tool calls this round ──
        tool_calls_this_round: list[str] = []
        if response.tool_calls:
            tool_calls_this_round = [tc.name for tc in response.tool_calls]

        self.emitter.emit_metrics(
            round=round_num,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cumulative_input=cumulative_input,
            cumulative_output=cumulative_output,
            system_chars=system_chars,
            tools_chars=tools_chars,
            messages_chars=messages_chars,
            total_prompt_chars=total_prompt_chars,
            tools_count=len(tool_defs or []),
            messages_count=len(call_messages or []),
            context_window=context_window,
            usage_ratio=usage_ratio,
            static_system_hash=static_hash,
            dynamic_state_chars=dynamic_state_chars,
            cache_estimated_hit=cache_estimated_hit,
            tool_calls_this_round=tool_calls_this_round,
            model=getattr(response, "model", "") or getattr(self.llm, "model", ""),
        )

    def _deduplicate_reads(
        self, messages: list[Message], reads_cache: dict[str, int], current_round: int
    ) -> list[Message]:
        """Replace repeated file reads with short summaries to save tokens.

        Only compresses reads from older rounds (not the current round),
        so the latest read result is always preserved in full.
        TOOL_RESULT blocks are never touched — they carry tool_call_id
        and must be preserved for API correctness.
        """
        if not reads_cache or len(messages) <= 4:
            return messages

        result = []
        for msg in messages:
            if msg.role != Role.TOOL:
                result.append(msg)
                continue
            new_blocks = []
            changed = False
            for block in msg.content:
                if block.type == ContentType.TOOL_RESULT:
                    new_blocks.append(block)
                    continue
                if block.type != ContentType.TEXT:
                    new_blocks.append(block)
                    continue
                text = block.text
                if len(text) <= 200:
                    new_blocks.append(block)
                    continue
                compressed = False
                for path, read_round in reads_cache.items():
                    if read_round >= current_round:
                        continue
                    if path in text:
                        text = (
                            text[:80]
                            + f"\n[文件 {path} 已在第 {read_round} 轮读取，"
                            + f"共 {len(text)} 字符，此处省略以节省 Token]\n"
                        )
                        new_blocks.append(ContentBlock(
                            type=ContentType.TEXT, text=text,
                        ))
                        changed = True
                        compressed = True
                        break
                if not compressed:
                    new_blocks.append(block)
            if changed:
                result.append(Message(role=msg.role, content=new_blocks))
            else:
                result.append(msg)
        return result

    async def _compress_history(
        self, messages: list[Message], round_num: int
    ) -> list[Message]:
        """Compress old conversation rounds into a summary.

        CRITICAL: must not split tool_calls/tool message pairs.
        Scans backwards to find a safe split point.
        """
        if len(messages) <= 4:
            return messages

        # Find safe split point: walk backwards, don't split between
        # an assistant(tool_calls) and its following tool messages
        safe_idx = len(messages) - 1
        while safe_idx > 0:
            msg = messages[safe_idx]
            if msg.role == Role.USER:
                break  # USER is always a safe boundary
            if msg.role == Role.ASSISTANT:
                # Check if this assistant has tool_calls
                has_tc = any(b.type == ContentType.TOOL_USE for b in msg.content)
                if not has_tc:
                    break  # Pure text assistant is safe
            # If TOOL role or assistant with tool_calls, keep walking back
            safe_idx -= 1

        # Need at least 2 messages before safe point to make compression worthwhile
        if safe_idx < 2:
            return messages

        history_text = ""
        for msg in messages[:safe_idx]:
            t = msg.get_text()[:500]
            if t.strip():
                history_text += f"[{msg.role.value}]: {t}\n"

        try:
            summary_response = await self._chat(
                messages=[
                    Message(
                        role=Role.USER,
                        content=(
                            "压缩以下对话为一段 150 字以内的摘要，"
                            "保留: 已创建/修改的文件、关键决策、当前进度\n\n"
                            + history_text
                        ),
                    )
                ],
                tools=None,
                system="你是一个对话摘要助手，只输出摘要，不要多余内容。",
                # Never stream a compression summary into the chat: the user
                # asked for work, not for a progress digest.
                stream=False,
            )
            summary = summary_response.text.strip()
            logger.info(f"History compressed at round {round_num}: {len(summary)} chars")
        except Exception as e:
            logger.warning(f"History compression failed: {e}")
            return messages

        # Replace compressed portion with summary, keeping tool pairs intact
        compressed = [
            Message(
                role=Role.SYSTEM,
                content=f"[历史摘要 — 第 {round_num} 轮压缩]\n{summary}",
            )
        ] + messages[safe_idx:]
        return compressed

    def _trim_history(self, messages: list[Message]) -> list[Message]:
        """Sliding window: keep only recent N rounds, discard older messages.

        A "round" is a user/assistant pair (possibly followed by tool messages).
        We count user messages as round boundaries and keep the last N rounds.
        This is simpler and more reliable than summary compression.
        """
        max_rounds = self.config.max_history_rounds

        # Find all USER message indices (these are round boundaries)
        user_indices = []
        for i, msg in enumerate(messages):
            if msg.role == Role.USER:
                user_indices.append(i)

        # If we have fewer rounds than max, no trimming needed
        if len(user_indices) <= max_rounds:
            return messages

        # Find the start index of the (len - max_rounds)-th user message
        # This is where we start keeping messages
        cutoff_idx = user_indices[-max_rounds]

        # Walk backwards from cutoff to find a safe boundary:
        # must not split an assistant(tool_calls) + tool messages pair.
        safe_idx = cutoff_idx
        while safe_idx > 0:
            msg = messages[safe_idx]
            if msg.role == Role.USER:
                break
            if msg.role == Role.ASSISTANT:
                has_tc = any(b.type == ContentType.TOOL_USE for b in msg.content)
                if not has_tc:
                    break
            safe_idx -= 1

        # Walk forward from safe_idx to ensure we don't start in the
        # middle of an assistant(tool_calls) + tool results group.
        # If safe_idx lands on a TOOL message or an assistant with
        # tool_calls, advance past the entire group.
        fwd = safe_idx
        while fwd < len(messages):
            msg = messages[fwd]
            if msg.role == Role.ASSISTANT:
                has_tc = any(b.type == ContentType.TOOL_USE for b in msg.content)
                if has_tc:
                    # Skip past this assistant and its following tool results
                    fwd += 1
                    while fwd < len(messages) and messages[fwd].role == Role.TOOL:
                        fwd += 1
                    # Now fwd points to the first non-tool message after the group
                    # If this is still before cutoff_idx, it's a valid start
                    if fwd <= cutoff_idx:
                        safe_idx = fwd
                        continue
                break
            elif msg.role == Role.TOOL:
                # Orphan tool result — skip past it
                fwd += 1
                if fwd <= cutoff_idx:
                    safe_idx = fwd
                    continue
                break
            else:
                break

        trimmed = messages[safe_idx:]
        dropped = len(messages) - len(trimmed)
        if dropped > 0:
            logger.info(f"Sliding window: dropped {dropped} old messages, keeping {len(trimmed)}")
        return trimmed

    async def _emit(self, block: ContentBlock) -> None:
        """Emit a content block to the stream callback."""
        if self._stream_callback:
            await self._stream_callback(block)

    async def _emit_stream_delta(self, block: ContentBlock) -> None:
        """Forward one model delta, and announce it on the ``llm/stream`` topic."""
        from gangge.runtime import events as runtime_events

        self._emit_lifecycle(
            runtime_events.LLM_STREAM,
            {"type": block.type.value, "text": (block.text or "")[:120]},
        )
        await self._emit(block)

    def _get_permission_action(self, tool_name: str, tool_input: dict) -> str:
        """Extract the action string for permission checking.

        For ``apply_patch`` the action is *every* path the patch touches,
        joined so the guard can judge the worst one — a patch that writes to
        the workspace and to a system directory in one call must not be allowed
        because the first path happened to be benign.
        """
        if tool_name == "bash":
            return tool_input.get("command", "") or ""
        if tool_name in ("read_file", "write_file", "edit_file"):
            return tool_input.get("path", "") or ""
        if tool_name == "apply_patch":
            paths: list[str] = []
            try:
                from gangge.layer3_agent.tools.patch import parse_patch

                for op in parse_patch(tool_input.get("patch") or ""):
                    paths.append(op.path)
                    if op.move_to:
                        paths.append(op.move_to)
            except Exception:
                paths = []
            return "; ".join(paths) if paths else "apply_patch"
        return tool_name or ""

    async def _auto_lint_check(self, file_path: str) -> str:
        """Run a quick lint check on a modified file. Returns summary or empty string."""
        try:
            from gangge.layer3_agent.tools.lint_check import LintCheckTool
            checker = LintCheckTool(workspace=self.config.workspace_dir)
            result = await checker.execute(path=file_path)
            if result.is_error:
                return f"[lint] {result.output}"
            return ""
        except Exception:
            return ""

    async def _consult_turn_stopping(
        self,
        round_num: int,
        text: str,
        executions: "list[ToolExecution]",
        file_registry: dict,
        todo_all: list,
        todo_pending: list,
        user_task: str,
        is_empty_dir: bool,
    ) -> Any | None:
        """Ask ``agent/turn-stopping`` listeners whether the turn may end.

        Returns whatever the last listener returned (a ``TurnDirective`` by
        convention), or ``None`` when nobody has an opinion — then the loop's
        built-in rules decide exactly as before. Plugins cannot make the loop
        unsafe: they can only answer "one more round" a bounded number of times,
        because the loop's ``max_tool_rounds`` still applies.
        """
        if self.context is None:
            return None
        # Imported lazily so layer 3 stays usable without the runtime package.
        from gangge.runtime import events as runtime_events

        write_tools = ("write_file", "edit_file", "create_tool")
        verify_tools = ("bash", "lint_check")
        last_write = max(
            (i for i, e in enumerate(executions) if e.tool_name in write_tools),
            default=-1,
        )
        state = {
            "round": round_num,
            "text": text or "",
            "user_task": user_task or "",
            "is_empty_dir": is_empty_dir,
            "has_modified_files": last_write >= 0,
            "verified_after_write": (
                any(
                    e.tool_name in verify_tools and not e.is_error
                    for e in executions[last_write + 1 :]
                )
                if last_write >= 0
                else False
            ),
            "tool_history": [
                {"name": e.tool_name, "is_error": e.is_error} for e in executions[-30:]
            ],
            "files_touched": [
                path
                for path, info in file_registry.items()
                if info.get("last_action") in write_tools
            ],
            "todo_all": list(todo_all or []),
            "todo_pending": list(todo_pending or []),
        }
        try:
            return await self.context.serial(runtime_events.AGENT_TURN_STOPPING, state)
        except Exception:
            logger.exception("[Loop] agent/turn-stopping listener failed")
            return None

    async def _chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        system: str = "",
        stream: bool = True,
    ) -> Any:
        """Send one request, streaming deltas to the UI when possible.

        ``adapter=self.llm`` pins the instance the loop was built with, so a
        factory default can never silently replace it — the waterfall only gets
        to edit the request, not the identity of the caller.

        Streaming is transparent to the caller: the deltas are forwarded while
        the request is in flight, and the *assembled* response is returned.
        ``self._last_streamed`` records how much text the UI already saw, so the
        caller does not print the answer a second time.
        """
        batcher: _DeltaBatcher | None = None
        if stream and self.config.enable_streaming and self._stream_callback is not None:
            batcher = _DeltaBatcher(self._emit_stream_delta)
        self._last_streamed = 0

        try:
            if self.llm_factory is not None:
                return await self.llm_factory.chat(
                    messages=messages,
                    tools=tools,
                    system=system,
                    adapter=self.llm,
                    on_delta=batcher,
                )
            if batcher is not None and getattr(self.llm, "supports_streaming", False):
                return await self.llm.chat_stream(
                    messages=messages,
                    tools=tools,
                    system=system,
                    on_delta=batcher,
                )
            return await self.llm.chat(messages=messages, tools=tools, system=system)
        finally:
            if batcher is not None:
                await batcher.flush()
                self._last_streamed = batcher.emitted_chars

    def _emit_lifecycle(self, event: str, payload: dict) -> None:
        """Fire-and-forget lifecycle event. Never raises into the loop."""
        if self.context is None:
            return
        try:
            self.context.emit(event, payload)
        except Exception:
            logger.debug("[Loop] lifecycle listener failed: %s", event, exc_info=True)

    # ── step lifecycle ──
    # In this loop a *step* covers one model request. The tool executions that
    # follow a request are bracketed by their own ``tool/call`` → ``tool/result``
    # pair, which is where per-tool latency belongs. Splitting it this way keeps
    # the step honest and needs no restructuring of the round body.

    def _begin_step(self, round_num: int, n_messages: int, n_tools: int) -> dict:
        from gangge.runtime import events as runtime_events

        step = {"round": round_num, "started": time.monotonic()}
        self._emit_lifecycle(
            runtime_events.STEP_START,
            {"round": round_num, "messages": n_messages, "tools": n_tools},
        )
        return step

    def _end_step(self, step: dict | None, response: Any = None, error: str = "") -> None:
        from gangge.runtime import events as runtime_events

        started = (step or {}).get("started")
        latency = round((time.monotonic() - started) * 1000, 1) if started else 0.0

        stop_reason = ""
        text_chars = 0
        tool_calls = 0
        usage: dict[str, Any] = {}
        if response is not None:
            stop_reason = getattr(response, "stop_reason", "") or ""
            text_chars = len(getattr(response, "text", "") or "")
            tool_calls = len(getattr(response, "tool_calls", None) or [])
            usage = getattr(response, "usage", None) or {}

        self._emit_lifecycle(
            runtime_events.STEP_END,
            {
                "round": (step or {}).get("round", -1),
                "latency_ms": latency,
                "stop_reason": stop_reason,
                "tool_calls": tool_calls,
                "text_chars": text_chars,
                "usage": usage,
                "error": error,
            },
        )

    def _repair_message_chain(self, messages: list[Message]) -> int:
        """Close an interrupted turn so the history stays valid.

        If a turn is cancelled (Ctrl+C, stop button, ``task.cancel()``) while
        tool calls are in flight, the transcript can end with an assistant
        message that requested N tools but carries fewer than N results.
        Providers reject that history outright — "tool_use ids were found
        without tool_result blocks" — so the *next* message fails before the
        model ever sees it.

        The fix mirrors DeepSeek Harness: give each unmatched ``tool_use`` a
        synthetic error result that says the outcome is unknown and what to do
        about it. Only the tail is repaired; a hole in the middle of the history
        is reported instead of silently rewoven.

        Returns the number of results inserted.
        """
        if not messages:
            return 0

        last = len(messages) - 1
        while last >= 0 and messages[last].role != Role.ASSISTANT:
            last -= 1
        if last < 0 or not any(b.type == ContentType.TOOL_USE for b in messages[last].content):
            return 0

        answered = {
            b.tool_call_id
            for msg in messages
            for b in msg.content
            if b.type == ContentType.TOOL_RESULT
        }
        missing = [
            b for b in messages[last].content
            if b.type == ContentType.TOOL_USE and b.tool_call_id not in answered
        ]
        if not missing:
            return 0

        # Merge into the first tool-result message that already follows the
        # assistant, so every result for one request stays in a single message.
        target = None
        for msg in messages[last + 1 :]:
            if msg.role == Role.TOOL:
                target = msg
                break
        if target is None:
            target = Message(role=Role.TOOL)
            messages.append(target)

        for block in missing:
            target.content.append(ContentBlock(
                type=ContentType.TOOL_RESULT,
                tool_call_id=block.tool_call_id,
                is_error=True,
                text=(
                    f"[中断] 工具 `{block.tool_name}` 的调用被取消，结果未知。\n"
                    "如果它是只读或幂等操作，可以重试；否则请先确认是否已经生效，"
                    "避免重复执行造成副作用。"
                ),
            ))

        logger.warning(
            "[Loop] repaired interrupted turn: %d unmatched tool call(s) closed", len(missing)
        )
        self._emit_lifecycle(
            "turn/repaired",
            {"count": len(missing), "tools": [b.tool_name for b in missing]},
        )
        return len(missing)

    async def run(self, messages: list[Message]) -> LoopResult:
        """Run one turn, bracketed by ``turn/start`` → ``turn/end``.

        The bracket lives here rather than at each of the loop's exit points so
        that every path — normal stop, max rounds, exception — produces exactly
        one ``turn/end``. Subscribers (memory bank, telemetry, a stop policy)
        can rely on that.
        """
        from gangge.runtime import events as runtime_events

        self._emit_lifecycle(runtime_events.TURN_START, {"messages": len(messages)})
        try:
            result = await self._run_turn(messages)
        except BaseException as exc:
            # A cancelled turn can leave an assistant message that requested
            # tools without results. Close it before anyone sends the history
            # again, or the *next* request fails with a 400.
            self._repair_message_chain(messages)
            self._emit_lifecycle(
                runtime_events.TURN_ABORTED, {"error": type(exc).__name__, "detail": str(exc)[:300]}
            )
            raise

        self._repair_message_chain(messages)

        write_tools = ("write_file", "edit_file", "create_tool")
        touched = [
            path
            for path, info in self.config.file_registry.items()
            if info.get("last_action") in write_tools
        ]
        self._emit_lifecycle(
            runtime_events.TURN_END,
            {
                "completion_reason": result.completion_reason,
                "total_rounds": result.total_rounds,
                "files": touched,
                # Only a change is worth a changelog line; a pure Q&A turn
                # should not append anything.
                "summary": (result.final_response or "").strip()[:300] if touched else "",
            },
        )
        return result

    async def _finish_turn(
        self,
        *,
        final_response: str,
        executions: list[ToolExecution],
        round_num: int,
        total_tokens: dict[str, int],
        completion_reason: str,
        has_modified_files: bool,
        shadow_checkpoint: str | None,
        memory_bank_update: str = "",
    ) -> LoopResult:
        """Close a turn: post-task checkpoint, done signal, result payload.

        Every exit path used to rebuild this by hand — four near-identical
        blocks, one of which had already drifted (it forgot ``emit_done``). One
        constructor means a new exit path cannot get the payload wrong.
        """
        after_checkpoint = None
        if has_modified_files:
            after_checkpoint = await self._git_checkpoint(
                f"checkpoint: {self._build_completion_summary()}"
            )

        self.emitter.emit_done(total_steps=round_num + 1)
        return LoopResult(
            final_response=final_response,
            tool_executions=executions,
            total_rounds=round_num + 1,
            total_tokens=total_tokens,
            extra={
                "memory_bank_update": memory_bank_update,
                "shadow_checkpoint_before": shadow_checkpoint or "",
                "shadow_checkpoint_after": after_checkpoint or "",
            },
            completion_reason=completion_reason,
        )

    async def _execute_tool_calls(
        self,
        response: Any,
        *,
        round_num: int,
        user_task_desc: str,
        executions: list[ToolExecution],
        file_registry: dict[str, Any],
        reads_cache: dict[str, int],
        has_modified_files: bool,
        any_tool_succeeded: bool,
    ) -> _ToolRound:
        """Run every tool call of one model response, in order.

        The per-call pipeline is: phase bookkeeping → permission → invoke →
        truncate → record (todo / reads / file registry) → show. Denied calls
        still produce a tool result, because a model that is told "denied" can
        adapt, while a model that gets silence retries the same thing.

        Returns the round's counters rather than mutating the caller's locals;
        ``executions``, ``reads_cache`` and ``file_registry`` are shared
        accumulators and are updated in place.
        """
        from gangge.layer3_agent.tools.todo import get_todo_state

        results = Message(role=Role.TOOL)
        await self._emit(ContentBlock(
            type=ContentType.TEXT,
            text=f"🔧 AI 调用了 {len(response.tool_calls)} 个工具:\n",
        ))

        productive = False
        for tool_call in response.tool_calls:
            name = tool_call.name
            arguments = tool_call.input or {}

            # Phase counter is telemetry only — visibility is decided by the
            # registry (profile core + tool_search), not by this number.
            self._note_tool_phase(name)

            permission = await self.guard.check(
                tool_name=name,
                action=self._get_permission_action(name, arguments),
                context={"input": arguments},
            )
            self.emitter.emit_tool_start(name, arguments)

            if permission.decision == PermissionDecision.DENY:
                results.add_tool_result(
                    call_id=tool_call.id,
                    result=f"权限被拒绝: {permission.reason}",
                    is_error=True,
                )
                executions.append(ToolExecution(
                    tool_name=name,
                    input=arguments,
                    output=f"DENIED: {permission.reason}",
                    is_error=True,
                    permission="denied",
                ))
                self.emitter.emit_tool_end(name, False)
                await self._emit(ContentBlock(
                    type=ContentType.TEXT,
                    text=f"  ✗ {name}: 权限被拒绝\n",
                ))
                continue

            started = time.monotonic()
            result = await self._invoke_tool(name, arguments, tool_call.id)
            elapsed_ms = int((time.monotonic() - started) * 1000)

            output = self._truncate_tool_output(result.output)

            # File changes are recorded *before* the result is sealed into the
            # message, so the post-write lint finding travels with it. The old
            # order appended lint to a local variable after the message had been
            # built, which meant lint results never reached the model at all.
            if not result.is_error and name in ("write_file", "edit_file", "apply_patch"):
                has_modified_files = True
                lint_note = await self._record_file_change(
                    name, arguments, file_registry, round_num, user_task_desc
                )
                if lint_note:
                    output += f"\n\n{lint_note}"

            results.add_tool_result(
                call_id=tool_call.id,
                result=output,
                is_error=result.is_error,
            )
            executions.append(ToolExecution(
                tool_name=name,
                input=arguments,
                output=result.output[:2000],
                is_error=result.is_error,
                permission=permission.decision.value,
                metadata=result.metadata,
            ))
            self.emitter.emit_tool_end(name, not result.is_error, elapsed_ms)

            if result.is_error:
                await self._emit(ContentBlock(
                    type=ContentType.TEXT,
                    text=f"  ✗ {self._display_tool_name(name)}: {result.output[:100]}...\n",
                ))
                continue

            any_tool_succeeded = True
            # Productive = something changed on disk or a command ran. Reads and
            # searches never count, which is what the stall guard keys off.
            if name in ("write_file", "edit_file", "apply_patch", "bash", "novel_write_chapter"):
                productive = True

            # ── Runtime auto-advance Todo state ──
            # The system manages todo progress, not the model.
            todo_state = get_todo_state()
            if todo_state.auto_advance(name, arguments, success=True):
                current = todo_state.get_current()
                if current:
                    await self._emit(ContentBlock(
                        type=ContentType.TEXT,
                        text=f"  📋 自动推进: ▶️ {current['content']}\n",
                    ))

            if name == "read_file":
                path = arguments.get("path", "")
                if path and path not in reads_cache:
                    reads_cache[path] = round_num + 1

            await self._emit(ContentBlock(
                type=ContentType.TEXT,
                text=f"  ✓ {self._display_tool_name(name)}: {result.output[:100]}...\n",
            ))

        return _ToolRound(
            results_message=results,
            any_succeeded=any_tool_succeeded,
            modified_files=has_modified_files,
            productive=productive,
        )

    async def _invoke_tool(
        self, name: str, arguments: dict[str, Any], call_id: str
    ) -> ToolResult:
        """Route one call to the right executor.

        Four back-ends: ``ask_user`` (needs the host's prompt), MCP servers,
        the plugin pipeline (schema repair / pruning / repeat hints), and the
        plain registry. Anything that can block is awaited off-loop.
        """
        if name == "ask_user":
            question = arguments.get("question", "")
            await self._emit(ContentBlock(
                type=ContentType.TEXT,
                text=f"\n[yellow]❓ {question}[/yellow]\n",
            ))
            answer = (
                await self.config.ask_user_callback(question)
                if self.config.ask_user_callback
                else ""
            )
            return ToolResult(output=answer or "(用户未提供输入)")

        if "__" in name and self.mcp_manager:
            # MCP clients are synchronous (blocking HTTP / stdio); running them
            # on the loop thread freezes streaming and the UI for the duration
            # of the call.
            output = await asyncio.to_thread(self.mcp_manager.call_tool, name, arguments)
            return ToolResult(output=output, is_error=str(output).startswith("[错误]"))

        if self.tool_service is not None:
            return await self.tool_service.execute(name, arguments, call_id)

        return await self.tools.execute(name, arguments)

    def _truncate_tool_output(self, output: str) -> str:
        """Cap long output, keeping the head *and* the tail.

        Errors and summaries live at the end; truncating from the front alone
        was hiding exactly the part the model needed.
        """
        if (
            not self.config.enable_tool_result_truncation
            or len(output) <= self.config.tool_result_max_chars
        ):
            return output
        max_len = self.config.tool_result_max_chars
        head_len = max_len * 2 // 5
        tail_len = max_len - head_len - 30
        return (
            output[:head_len]
            + f"\n...(中间截断，共{len(output)}字符)...\n"
            + output[-tail_len:]
        )

    async def _record_file_change(
        self,
        name: str,
        arguments: dict[str, Any],
        file_registry: dict[str, Any],
        round_num: int,
        user_task_desc: str,
    ) -> str:
        """Track a written file, refresh the progress journal, return lint notes."""
        path = arguments.get("path", "")
        lint_note = ""

        if path:
            lint_note = await self._auto_lint_check(path) or ""
            try:
                source = Path(path)
                classes: list[str] = []
                functions: list[str] = []
                if source.exists():
                    content = source.read_text(encoding="utf-8", errors="replace")
                    for line in content.splitlines():
                        stripped = line.strip()
                        if stripped.startswith("class ") and ":" in stripped:
                            classes.append(
                                stripped.split("(")[0].replace("class ", "").replace(":", "").strip()
                            )
                        elif stripped.startswith(("def ", "async def ")):
                            functions.append(
                                stripped.replace("async def ", "")
                                .replace("def ", "")
                                .split("(")[0]
                                .strip()
                            )
                    file_registry[path] = {
                        "classes": classes[:10],
                        "functions": functions[:15],
                        "last_action": name,
                        "round": round_num + 1,
                    }
                else:
                    file_registry[path] = {"last_action": name, "round": round_num + 1}
            except Exception:
                file_registry[path] = {"last_action": name, "round": round_num + 1}

        self._auto_save_progress(file_registry, round_num, user_task_desc)
        return lint_note

    def _note_tool_phase(self, tool_name: str) -> None:
        """Keep the phase counter monotone, for status display/telemetry."""
        from gangge.layer3_agent.tools.registry import TOOL_PHASES, DEFAULT_PHASE

        tool_phase = TOOL_PHASES.get(tool_name, DEFAULT_PHASE)
        current_phase = self.tools.get_current_phase()
        if tool_phase > current_phase:
            self.tools.set_phase(tool_phase)
            logger.debug("[Loop] phase %s→%s for %s", current_phase, tool_phase, tool_name)

    @staticmethod
    def _display_tool_name(tool_name: str) -> str:
        if "__" in tool_name:
            server, name = tool_name.split("__", 1)
            return f"[MCP:{server}] {name}"
        return tool_name

    async def _post_round_outcome(
        self,
        *,
        response: Any,
        round_num: int,
        executions: list[ToolExecution],
        total_tokens: dict[str, int],
        has_modified_files: bool,
        any_tool_succeeded: bool,
        this_round_productive: bool,
        shadow_checkpoint: str | None,
        consecutive_readonly_rounds: int,
        stalled_rounds: int,
    ) -> tuple[LoopResult | None, int, int]:
        """Decide whether the turn should end after a round's tools ran.

        Three independent stop rules, in priority order:

        * **read-only grace** — files are done and the model has spent three
          rounds only reading; it is reviewing, not working.
        * **stall guard** — something was delivered but nothing productive
          happened for four rounds (typically a self-imposed todo the
          environment cannot satisfy, like "run the tests" with no bash).
        * **todos complete** — the model's own plan says it is finished, even if
          it keeps calling tools.

        Returns ``(result_or_None, readonly_counter, stall_counter)``. Counters
        come back because they are per-turn state that survives the round.
        """
        from gangge.layer3_agent.tools.todo import get_todo_state

        write_tools = {
            "write_file", "edit_file", "apply_patch", "bash", "novel_write_chapter",
            "novel_init", "novel_setup", "novel_outline", "create_tool",
        }
        round_had_write = any(tc.name in write_tools for tc in response.tool_calls)
        if round_had_write:
            consecutive_readonly_rounds = 0
        elif any_tool_succeeded and has_modified_files:
            consecutive_readonly_rounds += 1

        if consecutive_readonly_rounds >= 3:
            todo_state = get_todo_state()
            if todo_state.get_pending():
                consecutive_readonly_rounds = 0
                await self._emit(ContentBlock(
                    type=ContentType.TEXT,
                    text="⚠️ 还有待办任务未完成，请继续执行\n",
                ))
            elif self._is_new_task and not todo_state.get_all():
                consecutive_readonly_rounds = 0
                self._is_new_task = False
                await self._emit(ContentBlock(
                    type=ContentType.TEXT,
                    text="⚠️ 检测到新任务：请调用 TodoWrite 制定计划后执行\n",
                ))
            else:
                await self._emit(ContentBlock(
                    type=ContentType.TEXT,
                    text="📋 连续3轮仅使用只读工具，任务已完成，自动退出\n",
                ))
                return (
                    await self._finish_turn(
                        final_response="",
                        executions=executions,
                        round_num=round_num,
                        total_tokens=total_tokens,
                        completion_reason="consecutive_readonly",
                        has_modified_files=has_modified_files,
                        shadow_checkpoint=shadow_checkpoint,
                    ),
                    consecutive_readonly_rounds,
                    stalled_rounds,
                )

        if this_round_productive:
            stalled_rounds = 0
        else:
            stalled_rounds += 1
            if stalled_rounds >= 4 and has_modified_files:
                todo_state = get_todo_state()
                if todo_state.get_pending():
                    todo_state.mark_all_done()
                    await self._emit(ContentBlock(
                        type=ContentType.TEXT,
                        text="📋 核心产出已生成，但后续待办持续数轮无进展，已自动收尾\n",
                    ))
                    return (
                        await self._finish_turn(
                            final_response="核心产出已生成；剩余待办因当前环境无法继续推进，已自动收尾。",
                            executions=executions,
                            round_num=round_num,
                            total_tokens=total_tokens,
                            completion_reason="stalled_no_progress",
                            has_modified_files=has_modified_files,
                            shadow_checkpoint=shadow_checkpoint,
                        ),
                        consecutive_readonly_rounds,
                        stalled_rounds,
                    )
                stalled_rounds = 0

        todo_state = get_todo_state()
        if todo_state.get_all() and not todo_state.get_pending():
            await self._emit(ContentBlock(
                type=ContentType.TEXT,
                text="📋 所有任务已完成，自动退出\n",
            ))
            return (
                await self._finish_turn(
                    final_response="",
                    executions=executions,
                    round_num=round_num,
                    total_tokens=total_tokens,
                    completion_reason="all_todos_done",
                    has_modified_files=has_modified_files,
                    shadow_checkpoint=shadow_checkpoint,
                ),
                consecutive_readonly_rounds,
                stalled_rounds,
            )

        return None, consecutive_readonly_rounds, stalled_rounds

    async def _run_turn(self, messages: list[Message]) -> LoopResult:
        """Run the agentic loop — simple while loop + TodoWrite + tool layering.

        Core design (inspired by Claude Code):
        - Software manages state (todo, context, permissions)
        - Model only decides "what to do next"
        - Tool layering: start with explore tools, open more as needed
        - TodoWrite: model maintains task list via tool, system injects every round
        - Token-based compression: auto-compress at 92% context usage

        Args:
            messages: Conversation history (will be modified in place).

        Returns:
            LoopResult with final response and execution records.
        """
        # ── Reset tool phase for new task ──
        self.tools.reset_phase()

        # ── Detect agent profile from user message ──
        # This determines which tools the model can see.
        # Coding → 12 tools, Novel → 22 tools, Research → 9 tools
        user_message = ""
        for msg in reversed(messages):
            if msg.role == Role.USER:
                text = msg.get_text().strip()
                if text and not text.startswith("[系统提示]"):
                    user_message = text
                    break

        from gangge.layer3_agent.tools.registry import detect_agent_profile
        profile = detect_agent_profile(user_message)
        self._profile = profile  # store for _build_system_prompt
        self.tools.set_profile(profile)
        profile_desc = {
            "coding": "编程", "novel": "小说创作", "research": "研究搜索"
        }.get(profile, profile)
        logger.info(f"[Loop] Agent profile: {profile} ({profile_desc})")

        # ── Quick Q&A pre-check: skip tool loop for pure explanation questions ──
        # Questions like "解释一下你刚才做了什么" don't need tool execution.
        # Key words that indicate Q&A: 解释/说明/为什么/怎么回事/explain/what/why/describe
        # Key words that indicate task: 创建/修改/写/优化/改/add/create/write/edit/fix
        last_user_text = ""
        for msg in reversed(messages):
            if msg.role == Role.USER:
                t = msg.get_text().strip()
                if t and not t.startswith("[系统提示]") and not t.startswith("##"):
                    last_user_text = t
                    break
        _qa_words = ["解释", "说明", "为什么", "怎么回事", "什么意思", "explain", "what is", "why did", "describe"]
        _task_words = ["创建", "修改", "写", "优化", "改", "加", "增加", "实现", "重构",
                        "add", "create", "write", "edit", "fix", "implement", "refactor", "update"]
        _is_qa = any(w in last_user_text.lower() for w in _qa_words)
        _has_task = any(w in last_user_text.lower() for w in _task_words)
        if _is_qa and not _has_task:
            await self._emit(ContentBlock(
                type=ContentType.TEXT,
                text="📋 检测到问答请求，直接回答\n",
            ))
            qa_step = self._begin_step(0, len(messages), 0)
            response = await self._chat(
                messages=messages,
                tools=[],
                system=self._build_static_system_prompt(),
            )
            # Show the answer. Without this the Q&A path returned text the host
            # was expected to print itself — which the desktop UI never did, so
            # "解释一下…" asked and then appeared to hang.
            if not self._last_streamed:
                for block in response.content:
                    if block.type in (ContentType.TEXT, ContentType.THINKING):
                        await self._emit(block)
            self._end_step(qa_step, response=response)
            self.emitter.emit_done(total_steps=1)
            return LoopResult(
                final_response=response.text or "",
                tool_executions=[],
                total_rounds=1,
                total_tokens=response.usage,
                completion_reason="single_turn_answer",
            )

        # ── Build static system prompt (once, byte-identical for caching) ──
        # All dynamic state is injected as user messages per round
        static_system = self._build_static_system_prompt()

        # ── Clear stale todo state ──
        from gangge.layer3_agent.tools.todo import get_todo_state
        todo_state = get_todo_state()
        # Clear previous task's todos unless user explicitly says "继续"
        _should_keep_todos = False
        for msg in messages:
            if msg.role == Role.USER:
                text = msg.get_text().strip()
                if text and ("继续" in text or "continue" in text.lower()):
                    _should_keep_todos = True
                    break
        self._is_new_task = False  # set below
        if not _should_keep_todos:
            todo_state.clear()
            self._is_new_task = True  # new task: don't exit until model creates at least one todo

        # ── Load Memory Bank from .gangge/ files ──
        self._ensure_memory_bank()

        # ── Extract user task description for commit messages ──
        user_task_desc = ""
        for msg in messages:
            if msg.role == Role.USER:
                text = msg.get_text().strip()
                if text and not text.startswith("[系统提示]"):
                    user_task_desc = text[:80].replace("\n", " ")
                    break

        # ── Shadow Git: auto-checkpoint before AI starts ──
        shadow_checkpoint = await self._git_checkpoint(
            f"checkpoint: before task — {user_task_desc}" if user_task_desc else "checkpoint: before AI task"
        )

        tool_defs = self._get_all_tool_defs(phase=self.tools.get_current_phase())
        is_empty_dir = detect_empty_workspace(self.config.workspace_dir)
        executions: list[ToolExecution] = []
        total_tokens: dict[str, int] = {"input": 0, "output": 0}
        has_modified_files = False
        any_tool_succeeded = False
        consecutive_readonly_rounds = 0  # Track rounds with only read tools
        stalled_rounds = 0  # Consecutive rounds without productive output
        file_registry = dict(self.config.file_registry)  # mutable copy
        reads_cache: dict[str, int] = {}  # path -> round_number for de-dup
        memory_bank_update = ""  # extracted from LLM's final response

        # ── Detect "continue" intent from user's last message ──
        is_continue = False
        for msg in reversed(messages):
            if msg.role == Role.USER:
                last_user_text = msg.get_text().strip().lower()
                continue_keywords = ["继续", "接着做", "继续做", "continue", "go on", "keep going"]
                is_continue = any(kw in last_user_text for kw in continue_keywords)
                break

        if is_continue:
            # Build dynamic state for continue context
            continue_state = self._build_dynamic_state_text(round_num=0)
            file_list_summary = ""
            if file_registry:
                modified = sorted(
                    p for p, info in file_registry.items()
                    if info.get("last_action") in ("write_file", "edit_file")
                )
                if modified:
                    file_list_summary = (
                        f"\n\n### 已修改的文件（不需要再读取验证）\n"
                        + "\n".join(f"- `{p}`" for p in modified)
                        + "\n"
                    )
            inject_text = (
                "\n\n[系统提示] 用户说'继续'，这意味着上次任务未完成。"
                "请直接从上次中断的地方继续执行。"
                "绝对不要从头 read_file 所有源文件来'了解项目'！"
                "绝对不要读取 .gangge/changelog.md 或 .gangge/progress.md——进度信息已经直接提供在下面了！"
                f"\n\n{continue_state}"
                f"{file_list_summary}"
                "\n\n请立刻继续执行下一步，不要再读取任何已完成的文件。"
            )
            messages.append(Message(
                role=Role.USER,
                content=[ContentBlock(type=ContentType.TEXT, text=inject_text)],
            ))

        for round_num in range(self.config.max_tool_rounds):
            logger.info(f"Agentic loop round {round_num + 1}")

            # ── Emit round indicator ──
            self.emitter.emit(EventType.ROUND, f"第 {round_num + 1} 轮")
            await self._emit(ContentBlock(
                type=ContentType.TEXT,
                text=f"\n[dim]── 第 {round_num + 1} 轮 ──[/dim]\n",
            ))

            # ── Cancel check: allow external stop button to interrupt ──
            if self._cancel_check and self._cancel_check():
                await self._emit(ContentBlock(
                    type=ContentType.TEXT,
                    text="\n⏹ 任务已取消\n",
                ))
                break

            # ── Approaching max rounds: inject urgency hint ──
            remaining = self.config.max_tool_rounds - round_num
            if remaining <= 2:
                await self._emit(ContentBlock(
                    type=ContentType.TEXT,
                    text=f"⚠️ 即将达到最大轮数限制（剩余 {remaining} 轮），请尽快总结当前进度并输出最终回复！\n",
                ))

            # ── Use cached static system prompt (byte-identical across rounds) ──
            # Dynamic state (progress, changelog, decisions, todo) is injected
            # as a user message below to preserve system prompt caching.
            self.config.file_registry = file_registry
            system = static_system
            self.emitter.emit(EventType.THINKING, f"正在思考...")

            # ── Tool exposure ──
            # What the model can see is decided by the registry: the profile's
            # core set, plus everything ``tool_search`` already revealed. The
            # old keyword heuristics that lived here ("a todo mentions 测试 →
            # unlock bash", "empty dir → unlock write") are gone: they fired on
            # negations, misfired on real repos, and guessed where asking works
            # better. Nothing below depends on the phase counter any more.

            # ── Refresh tool definitions (tools may have been unlocked) ──
            current_phase = self.tools.get_current_phase()
            tool_defs = self._get_all_tool_defs(phase=current_phase)

            # ── Token-based auto compression (Claude Code style: 92% threshold) ──
            # Check token usage and compress if approaching context limit
            if round_num > 0:
                total_input = total_tokens.get("input", 0)
                # Estimate context usage: if input tokens exceed 92% of max, compress
                context_limit = self.config.max_tokens * 12  # rough char-to-token ratio
                if total_input > 0 and self.llm.max_context_tokens > 0:
                    usage_ratio = total_input / self.llm.max_context_tokens
                    if usage_ratio > 0.92:
                        _before = len(messages)
                        messages = await self._compress_history(messages, round_num)
                        _after = len(messages)
                        await self._emit(ContentBlock(
                            type=ContentType.TEXT,
                            text=f"\n📦 上下文压缩: 使用率 {usage_ratio:.0%}，已自动压缩旧对话\n",
                        ))
                        # Experimental: record compression event
                        self.emitter.emit_metrics(
                            round=round_num,
                            compression_before=_before,
                            compression_after=_after,
                            compressed=True,
                            usage_ratio=usage_ratio,
                        )
                elif self.config.enable_sliding_window:
                    # Fallback: sliding window
                    messages = self._trim_history(messages)
                elif (
                    self.config.enable_summary_compression
                    and round_num % self.config.summary_compression_interval == 0
                ):
                    messages = await self._compress_history(messages, round_num)
                    await self._emit(ContentBlock(
                        type=ContentType.TEXT,
                        text=f"\n📦 历史压缩: 第 {round_num} 轮\n",
                    ))

            # ── Deduplicate repeated file reads to save tokens ──
            if round_num > 0 and reads_cache:
                messages = self._deduplicate_reads(messages, reads_cache, round_num + 1)

            # ── Inject dynamic state as a user message (NOT into system prompt) ──
            # This keeps the static system prompt byte-identical for API caching.
            # Construct call_messages = persistent history + ephemeral state update
            state_text = self._build_dynamic_state_text(round_num=round_num)
            if state_text:
                call_messages = list(messages) + [
                    Message(
                        role=Role.USER,
                        content=[ContentBlock(type=ContentType.TEXT, text=state_text)],
                    )
                ]
            else:
                call_messages = messages

            # 1. Call LLM (streaming; bounded by ``llm_timeout``)
            # No "等待回复" placeholder here any more: with streaming the first
            # delta arrives as soon as the model starts, and a placeholder would
            # end up glued to the front of every answer.
            step = self._begin_step(round_num, len(call_messages), len(tool_defs))
            try:
                response = await asyncio.wait_for(
                    self._chat(
                        messages=call_messages,
                        tools=tool_defs,
                        system=system,
                    ),
                    timeout=self.config.llm_timeout,
                )
            except asyncio.TimeoutError:
                error_text = f"LLM 调用超时（{self.config.llm_timeout:.0f}s），请检查网络或 API 状态"
                logger.error(error_text)
                self.emitter.emit(EventType.ERROR, error_text)
                self._end_step(step, error="timeout")
                return LoopResult(
                    final_response=error_text,
                    tool_executions=executions,
                    total_rounds=round_num,
                    total_tokens=total_tokens,
                    completion_reason="llm_timeout",
                )
            except Exception as e:
                error_str = str(e)
                self._end_step(step, error=error_str[:160])
                # ── Retryable errors (rate limit / server overload) ──
                # 429 = too many requests, 503 = service unavailable, 5xx = server error
                is_retryable = any(code in error_str for code in ("429", "503", "502", "500"))
                if is_retryable:
                    # Count consecutive failures to cap retries
                    consecutive_failures = getattr(self, "_llm_failures", 0) + 1
                    self._llm_failures = consecutive_failures
                    max_retries = 3
                    if consecutive_failures <= max_retries:
                        backoff = [5, 15, 30][consecutive_failures - 1]
                        await self._emit(ContentBlock(
                            type=ContentType.TEXT,
                            text=f"⏳ API 限流，{backoff} 秒后自动重试（第 {consecutive_failures}/{max_retries} 次）...\n",
                        ))
                        await asyncio.sleep(backoff)
                        continue  # retry this round
                    else:
                        error_text = f"LLM 调用失败（已重试 {max_retries} 次仍限流）: {e}"
                else:
                    error_text = f"LLM 调用失败: {e}"

                logger.error(error_text)
                self.emitter.emit(EventType.ERROR, error_text)

                self._auto_save_progress(
                    file_registry, round_num, user_task_desc
                )

                return LoopResult(
                    final_response=error_text,
                    tool_executions=executions,
                    total_rounds=round_num,
                    total_tokens=total_tokens,
                    completion_reason="llm_error",
                )

            # Track token usage
            total_tokens["input"] += response.usage.get("input_tokens", 0)
            total_tokens["output"] += response.usage.get("output_tokens", 0)

            # ── Experimental Mode: emit per-round metrics ──
            try:
                self._emit_metrics_for_round(
                    round_num=round_num,
                    response=response,
                    total_tokens=total_tokens,
                    system=system,
                    tool_defs=tool_defs,
                    call_messages=call_messages,
                    static_system=static_system,
                    state_text=state_text,
                )
            except Exception:
                pass

            # Reset consecutive failure counter on success
            self._llm_failures = 0

            # 2. Add assistant message to history
            assistant_msg = Message(role=Role.ASSISTANT, content=response.content)
            messages.append(assistant_msg)

            # 3. Show text content to the UI — unless it was already streamed.
            # ``_last_streamed`` is how many characters went out live during the
            # call; re-emitting the assembled blocks would duplicate the answer.
            if not self._last_streamed:
                for block in response.content:
                    if block.type in (ContentType.TEXT, ContentType.THINKING):
                        await self._emit(block)

            # Step ends here: the request completed. Tool execution that follows
            # is bracketed by tool/call → tool/result, so both durations are
            # observable without splitting the round body in two.
            self._end_step(step, response=response)

            # 4. If no tool calls → check if done, then exit
            # Core principle: the model decides when to stop by not calling tools.
            has_tool_call = response.stop_reason == "tool_use" and response.tool_calls
            if not has_tool_call:
                text = response.text or ""

                # ── Check Todo state: if all tasks completed, exit immediately ──
                todo_pending = todo_state.get_pending()
                if not todo_pending and todo_state.get_all():
                    # All todos completed — exit regardless of what model says
                    await self._emit(ContentBlock(
                        type=ContentType.TEXT,
                        text="📋 所有任务已完成，自动退出\n",
                    ))

                # ── Only force retry in the first 2 rounds to get things started ──
                # "Nothing attempted yet" is the real signal, not "nothing
                # succeeded": if the model's only call was denied or failed, it
                # already knows and has decided to stop and report. Nudging it
                # again just repeats the same message one round later.
                elif round_num <= 1 and not executions and not has_modified_files:
                    # Early rounds with no tool use — give a nudge
                    self.emitter.emit(EventType.WARNING,
                                      f"第 {round_num+1} 轮未调用工具，正在引导")
                    await self._emit(ContentBlock(
                        type=ContentType.TEXT,
                        text="⚠️ AI 未使用工具，正在引导...\n",
                    ))
                    if is_empty_dir:
                        force_msg = (
                            "当前工作目录是空的，不需要探索，直接开始构建。\n"
                            "请立刻调用 write_file 或 bash 开始创建第一个文件。"
                        )
                    else:
                        force_msg = (
                            "请立刻调用工具开始执行任务（write_file / edit_file / bash / read_file）。\n"
                            "不要只输出文字规划，直接行动。"
                        )
                    messages.append(Message(
                        role=Role.USER,
                        content=[ContentBlock(type=ContentType.TEXT, text=force_msg)],
                    ))
                    continue

                # ── Model answered with meaningful text — not a task, just a Q&A ──
                # If the model produced a substantial response (>100 chars) without
                # calling tools, it likely answered a question (like "解释一下").
                # Don't force tool use — exit normally.
                if not todo_state.get_all() and len(text) > 100:
                    self._is_new_task = False
                    await self._emit(ContentBlock(
                        type=ContentType.TEXT,
                        text="📋 已收到回答\n",
                    ))

                # ── All other cases: model stopped calling tools ──
                # If there are still pending todos → nudge, don't exit
                todo_pending = todo_state.get_pending()

                # ── New task, no todos created yet — force model to plan ──
                if self._is_new_task and not todo_state.get_all():
                    self._is_new_task = False  # prevent infinite nudging
                    self.emitter.emit(EventType.WARNING,
                                      f"第 {round_num+1} 轮未调用工具，新任务需要先规划")
                    await self._emit(ContentBlock(
                        type=ContentType.TEXT,
                        text="⚠️ 新任务，请先调用 TodoWrite 创建任务列表再开始执行\n",
                    ))
                    force_msg = (
                        "这是一个新任务。请先调用 TodoWrite 创建任务计划，"
                        "然后按步骤调用工具执行。"
                    )
                    messages.append(Message(
                        role=Role.USER,
                        content=[ContentBlock(type=ContentType.TEXT, text=force_msg)],
                    ))
                    continue

                if todo_pending:
                    next_todo = todo_state.get_current()
                    todo_hint = f" 当前待办: {next_todo['content']}" if next_todo else ""
                    self.emitter.emit(EventType.WARNING,
                                      f"第 {round_num+1} 轮未调用工具，仍有待办任务{todo_hint}")
                    await self._emit(ContentBlock(
                        type=ContentType.TEXT,
                        text=f"⚠️ AI 未使用工具，仍有待办任务{todo_hint}\n",
                    ))
                    force_msg = "请立即调用工具执行当前待办任务，不要只输出文字。"
                    messages.append(Message(
                        role=Role.USER,
                        content=[ContentBlock(type=ContentType.TEXT, text=force_msg)],
                    ))
                    continue

                # ── agent/turn-stopping: last vote before accepting a stop ──
                # A weak model's most expensive failure is stopping without
                # acting ("我已分析完毕，你可以自己改"). Plugins subscribed to
                # this event can refuse the stop once and inject a directive.
                directive = await self._consult_turn_stopping(
                    round_num=round_num,
                    text=text,
                    executions=executions,
                    file_registry=file_registry,
                    todo_all=todo_state.get_all(),
                    todo_pending=todo_state.get_pending(),
                    user_task=user_task_desc,
                    is_empty_dir=is_empty_dir,
                )
                if directive is not None and getattr(directive, "action", "") == "continue":
                    reason = getattr(directive, "reason", "") or "继续执行"
                    await self._emit(ContentBlock(
                        type=ContentType.TEXT,
                        text=f"🔁 {reason}\n",
                    ))
                    messages.append(Message(
                        role=Role.USER,
                        content=[ContentBlock(
                            type=ContentType.TEXT,
                            text=getattr(directive, "message", "") or "请继续，直到任务真正完成。",
                        )],
                    ))
                    continue

                # No pending todos, model chose to stop → exit normally
                # This is the Claude Code way: no tool call = done.

                # ── Extract Memory Bank update from final response ──
                final_text = response.text
                mb_extracted = ""
                if "```memory-bank" in final_text:
                    import re as _re
                    m = _re.search(r"```memory-bank\n(.*?)```", final_text, _re.DOTALL)
                    if m:
                        mb_extracted = m.group(1).strip()
                # ── Save Memory Bank update to files ──
                if mb_extracted:
                    self._save_memory_bank_update(mb_extracted)

                return await self._finish_turn(
                    final_response=final_text,
                    executions=executions,
                    round_num=round_num,
                    total_tokens=total_tokens,
                    completion_reason="model_end_turn",
                    has_modified_files=has_modified_files,
                    shadow_checkpoint=shadow_checkpoint,
                    memory_bank_update=mb_extracted,
                )

            # 5. Run the tools this round asked for.
            round_io = await self._execute_tool_calls(
                response,
                round_num=round_num,
                user_task_desc=user_task_desc,
                executions=executions,
                file_registry=file_registry,
                reads_cache=reads_cache,
                has_modified_files=has_modified_files,
                any_tool_succeeded=any_tool_succeeded,
            )
            messages.append(round_io.results_message)
            any_tool_succeeded = round_io.any_succeeded
            has_modified_files = round_io.modified_files
            this_round_productive = round_io.productive

            # ── Round guard: should this round be the last one? ──
            outcome, consecutive_readonly_rounds, stalled_rounds = await self._post_round_outcome(
                response=response,
                round_num=round_num,
                executions=executions,
                total_tokens=total_tokens,
                has_modified_files=has_modified_files,
                any_tool_succeeded=any_tool_succeeded,
                this_round_productive=this_round_productive,
                shadow_checkpoint=shadow_checkpoint,
                consecutive_readonly_rounds=consecutive_readonly_rounds,
                stalled_rounds=stalled_rounds,
            )
            if outcome is not None:
                return outcome

        self.emitter.emit(EventType.WARNING, "达到最大工具调用轮数限制")

        if self.config.workspace_dir:
            gangge_dir = Path(self.config.workspace_dir) / ".gangge"
            gangge_dir.mkdir(parents=True, exist_ok=True)
            changelog_file = gangge_dir / "changelog.md"
            progress_file = gangge_dir / "progress.md"

            written_files = sorted(
                p for p, info in file_registry.items()
                if info.get("last_action") in ("write_file", "edit_file")
            )

            incomplete_entry = (
                f"\n## {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                f"### 未完成的工作\n"
                f"- [ ] 任务未完成，已达到最大工具调用轮数限制（{self.config.max_tool_rounds} 轮）\n"
            )
            if written_files:
                incomplete_entry += (
                    f"### 已完成的文件（{len(written_files)} 个）\n"
                    + "\n".join(f"- [x] `{p}`" for p in written_files)
                    + "\n"
                )
            incomplete_entry += (
                "- [ ] 用户说'继续'时，请从上次中断处继续，不要从头开始\n"
            )
            try:
                existing = changelog_file.read_text(encoding="utf-8") if changelog_file.exists() else ""
                changelog_file.write_text(existing + incomplete_entry, encoding="utf-8")
                self.config.memory_bank_changelog = (existing + incomplete_entry).strip()
                logger.info("[Loop] 已自动记录未完成的工作到 changelog.md")
            except Exception as e:
                logger.warning(f"[Loop] 写入 changelog.md 失败: {e}")

            self._auto_save_progress(
                file_registry, self.config.max_tool_rounds, user_task_desc
            )

        return LoopResult(
            final_response="[达到最大工具调用轮数限制]",
            tool_executions=executions,
            total_rounds=self.config.max_tool_rounds,
            total_tokens=total_tokens,
            completion_reason="max_rounds",
        )
