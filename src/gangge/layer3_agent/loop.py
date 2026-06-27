"""Agentic Loop — the core engine of the AI assistant.

Implements the Plan & Execute pattern:
1. Send messages to LLM
2. If tool_use → check permission → execute → add result → loop
3. If end_turn → return final response
"""

from __future__ import annotations

import asyncio
import logging
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
    llm_timeout: float = 300.0       # LLM response timeout in seconds (本地模型可能较慢)
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


# Callback types
StreamCallback = Callable[[ContentBlock], Awaitable[None]]


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
    ):
        self.llm = llm
        self.tools = tools
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
            summary_response = await self.llm.chat(
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

    def _get_permission_action(self, tool_name: str, tool_input: dict) -> str:
        """Extract the action string for permission checking."""
        if tool_name == "bash":
            return tool_input.get("command", "") or ""
        elif tool_name in ("read_file", "write_file", "edit_file"):
            return tool_input.get("path", "") or ""
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

    async def run(self, messages: list[Message]) -> LoopResult:
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
            response = await self.llm.chat(
                messages=messages,
                tools=[],
                system=self._build_static_system_prompt(),
            )
            self.emitter.emit_done(total_steps=1)
            return LoopResult(
                final_response=response.text or "",
                tool_executions=[],
                total_rounds=1,
                total_tokens=response.usage,
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
        consecutive_timeouts = 0
        consecutive_readonly_rounds = 0  # Track rounds with only read tools
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

            # ── Refresh tool definitions based on current phase ──
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
                        messages = await self._compress_history(messages, round_num)
                        await self._emit(ContentBlock(
                            type=ContentType.TEXT,
                            text=f"\n📦 上下文压缩: 使用率 {usage_ratio:.0%}，已自动压缩旧对话\n",
                        ))
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

            # 1. Call LLM (with 120s timeout to prevent hanging)
            await self._emit(ContentBlock(
                type=ContentType.TEXT,
                text="⏳ 等待 AI 回复...\n",
            ))
            try:
                response = await asyncio.wait_for(
                    self.llm.chat(
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
                return LoopResult(
                    final_response=error_text,
                    tool_executions=executions,
                    total_rounds=round_num,
                    total_tokens=total_tokens,
                )
            except Exception as e:
                error_str = str(e)
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
                )

            # Track token usage
            total_tokens["input"] += response.usage.get("input_tokens", 0)
            total_tokens["output"] += response.usage.get("output_tokens", 0)

            # Reset consecutive failure counter on success
            self._llm_failures = 0

            # 2. Add assistant message to history
            assistant_msg = Message(role=Role.ASSISTANT, content=response.content)
            messages.append(assistant_msg)

            # 3. Stream text content to UI
            for block in response.content:
                if block.type in (ContentType.TEXT, ContentType.THINKING):
                    await self._emit(block)

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
                elif round_num <= 1 and not any_tool_succeeded and not has_modified_files:
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

                # ── Shadow Git: post-task checkpoint ──
                after_checkpoint = None
                if has_modified_files:
                    after_checkpoint = await self._git_checkpoint(
                        f"checkpoint: completed task — {user_task_desc}" if user_task_desc else "checkpoint: after AI task completed"
                    )

                self.emitter.emit_done(total_steps=round_num + 1)
                return LoopResult(
                    final_response=final_text,
                    tool_executions=executions,
                    total_rounds=round_num + 1,
                    total_tokens=total_tokens,
                    extra={
                        "memory_bank_update": mb_extracted,
                        "shadow_checkpoint_before": shadow_checkpoint or "",
                        "shadow_checkpoint_after": after_checkpoint or "",
                    },
                )

            # 5. Process tool calls
            tool_results_msg = Message(role=Role.TOOL)
            await self._emit(ContentBlock(
                type=ContentType.TEXT,
                text=f"🔧 AI 调用了 {len(response.tool_calls)} 个工具:\n",
            ))

            for tool_call in response.tool_calls:
                tool_name = tool_call.name
                tool_input = tool_call.input

                # ── Auto-advance phase if model uses a higher-phase tool ──
                # This is intentional: the model "knows" what it needs to do,
                # and we open the gate automatically rather than rejecting.
                from gangge.layer3_agent.tools.registry import TOOL_PHASES, DEFAULT_PHASE
                tool_phase = TOOL_PHASES.get(tool_name, DEFAULT_PHASE)
                current_phase = self.tools.get_current_phase()
                if tool_phase > current_phase:
                    self.tools.set_phase(tool_phase)
                    phase_names = {1: "探索", 2: "编写", 3: "运行", 4: "特殊"}
                    logger.info(f"[Loop] Auto-advancing phase {current_phase}→{tool_phase} for {tool_name}")

                action = self._get_permission_action(tool_name, tool_input)
                self.emitter.emit_tool_start(tool_name, tool_input)

                # Check permission
                perm_result = await self.guard.check(
                    tool_name=tool_name,
                    action=action,
                    context={"input": tool_input},
                )

                if perm_result.decision == PermissionDecision.DENY:
                    tool_results_msg.add_tool_result(
                        call_id=tool_call.id,
                        result=f"权限被拒绝: {perm_result.reason}",
                        is_error=True,
                    )
                    executions.append(ToolExecution(
                        tool_name=tool_name,
                        input=tool_input,
                        output=f"DENIED: {perm_result.reason}",
                        is_error=True,
                        permission="denied",
                    ))
                    self.emitter.emit_tool_end(tool_name, False)
                    continue

                # Execute tool
                import time
                _t0 = time.monotonic()

                # ── Special handling: ask_user ──
                if tool_name == "ask_user":
                    question = tool_input.get("question", "")
                    await self._emit(ContentBlock(
                        type=ContentType.TEXT,
                        text=f"\n[yellow]❓ {question}[/yellow]\n",
                    ))
                    if self.config.ask_user_callback:
                        user_answer = await self.config.ask_user_callback(question)
                    else:
                        user_answer = ""
                    result = ToolResult(
                        output=user_answer if user_answer else "(用户未提供输入)",
                    )
                # ── PATCH: MCP tool dispatch ──
                elif "__" in tool_name and self.mcp_manager:
                    output = self.mcp_manager.call_tool(tool_name, tool_input)
                    result = ToolResult(output=output, is_error=output.startswith("[错误]"))
                else:
                    result = await self.tools.execute(tool_name, tool_input)

                _elapsed = int((time.monotonic() - _t0) * 1000)

                # ── Tool result truncation (keep head AND tail) ──
                # Old behavior: truncated from the front, losing error info at bottom.
                # New: keeps first 40% and last 60% — model can always see errors
                # at the end of long outputs.
                result_output = result.output
                if (
                    self.config.enable_tool_result_truncation
                    and len(result_output) > self.config.tool_result_max_chars
                ):
                    max_len = self.config.tool_result_max_chars
                    head_len = max_len * 2 // 5  # 40%
                    tail_len = max_len - head_len - 30  # ~60%, minus marker length
                    result_output = (
                        result_output[:head_len]
                        + f"\n...(中间截断，共{len(result_output)}字符)...\n"
                        + result_output[-tail_len:]
                    )

                tool_results_msg.add_tool_result(
                    call_id=tool_call.id,
                    result=result_output,
                    is_error=result.is_error,
                )
                executions.append(ToolExecution(
                    tool_name=tool_name,
                    input=tool_input,
                    output=result.output[:2000],
                    is_error=result.is_error,
                    permission=perm_result.decision.value,
                    metadata=result.metadata,
                ))
                self.emitter.emit_tool_end(tool_name, not result.is_error, _elapsed)

                if not result.is_error:
                    any_tool_succeeded = True
                    consecutive_timeouts = 0

                    # ── Runtime auto-advance Todo state ──
                    # The system manages todo progress, not the model.
                    # When a productive tool succeeds, auto-complete current todo
                    # and auto-start the next one.
                    todo_state = get_todo_state()
                    if todo_state.auto_advance(tool_name, tool_input, success=True):
                        current = todo_state.get_current()
                        if current:
                            await self._emit(ContentBlock(
                                type=ContentType.TEXT,
                                text=f"  📋 自动推进: ▶️ {current['content']}\n",
                            ))
                else:
                    is_timeout = isinstance(result.output, str) and result.output.startswith("[超时]")
                    if is_timeout:
                        consecutive_timeouts += 1
                    else:
                        consecutive_timeouts = 0

                # ── Track file reads for context de-duplication ──
                if tool_name == "read_file" and not result.is_error:
                    path = tool_input.get("path", "")
                    if path and path not in reads_cache:
                        reads_cache[path] = round_num + 1

                # ── Track file modifications in file registry ──
                if tool_name in ("write_file", "edit_file") and not result.is_error:
                    path = tool_input.get("path", "")
                    has_modified_files = True

                    # ── Auto lint check after file modification ──
                    lint_result = await self._auto_lint_check(path)
                    if lint_result:
                        result_output += f"\n\n{lint_result}"

                    if path:
                        # Scan file for classes/functions
                        try:
                            p = Path(path)
                            if p.exists():
                                text = p.read_text(encoding="utf-8", errors="replace")
                                classes: list[str] = []
                                functions: list[str] = []
                                for line in text.splitlines():
                                    s = line.strip()
                                    if s.startswith("class ") and ":" in s:
                                        name = s.split("(")[0].replace("class ", "").replace(":", "").strip()
                                        classes.append(name)
                                    elif s.startswith(("def ", "async def ")):
                                        name = s.replace("async def ", "").replace("def ", "").split("(")[0].strip()
                                        functions.append(name)
                                file_registry[path] = {
                                    "classes": classes[:10],
                                    "functions": functions[:15],
                                    "last_action": tool_name,
                                    "round": round_num + 1,
                                }
                        except Exception:
                            file_registry[path] = {
                                "last_action": tool_name,
                                "round": round_num + 1,
                            }

                    self._auto_save_progress(
                        file_registry, round_num, user_task_desc
                    )

                # Emit tool result info
                status = "✓" if not result.is_error else "✗"
                # ── PATCH: MCP tool display ──
                display_name = tool_name
                if "__" in tool_name:
                    server, name = tool_name.split("__", 1)
                    display_name = f"[MCP:{server}] {name}"
                # ──────────────────────────────
                await self._emit(ContentBlock(
                    type=ContentType.TEXT,
                    text=f"  {status} {display_name}: {result.output[:100]}...\n",
                ))

            messages.append(tool_results_msg)

            # ── Track consecutive read-only rounds ──
            # If the model only called read tools (read_file, grep, glob, list_dir)
            # for several rounds after already modifying files, it's likely done
            # and just reviewing — exit early.
            write_tools = {"write_file", "edit_file", "bash", "novel_write_chapter",
                           "novel_init", "novel_setup", "novel_outline", "create_tool"}
            round_had_write = any(tc.name in write_tools for tc in response.tool_calls)
            if round_had_write:
                consecutive_readonly_rounds = 0
            elif any_tool_succeeded and has_modified_files:
                consecutive_readonly_rounds += 1

            if consecutive_readonly_rounds >= 3:
                # ── Don't exit if there are still pending todos ──
                todo_state = get_todo_state()
                if todo_state.get_pending():
                    # Still have work to do — reset counter and nudge
                    consecutive_readonly_rounds = 0
                    await self._emit(ContentBlock(
                        type=ContentType.TEXT,
                        text="⚠️ 还有待办任务未完成，请继续执行\n",
                    ))
                elif self._is_new_task and not todo_state.get_all():
                    # New task, just reading files to understand — don't exit yet
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
                    final_text = ""
                    mb_extracted = ""
                    after_checkpoint = None
                    if has_modified_files:
                        after_checkpoint = await self._git_checkpoint(
                            f"checkpoint: completed task — {user_task_desc}" if user_task_desc else "checkpoint: after AI task completed"
                        )
                    self.emitter.emit_done(total_steps=round_num + 1)
                    return LoopResult(
                        final_response=final_text,
                        tool_executions=executions,
                        total_rounds=round_num + 1,
                        total_tokens=total_tokens,
                        extra={
                            "memory_bank_update": mb_extracted,
                            "shadow_checkpoint_before": shadow_checkpoint or "",
                            "shadow_checkpoint_after": after_checkpoint or "",
                        },
                    )

            # ── Check if all todos are completed → exit early ──
            # This is the key fix: even if the model keeps calling tools
            # (read_file, bash, etc.), if all planned tasks are done, we exit.
            todo_state = get_todo_state()
            todo_pending = todo_state.get_pending()
            todo_all = todo_state.get_all()
            if todo_all and not todo_pending:
                # All todos completed — no need to keep looping
                await self._emit(ContentBlock(
                    type=ContentType.TEXT,
                    text="📋 所有任务已完成，自动退出\n",
                ))
                final_text = ""
                mb_extracted = ""

                # Shadow Git checkpoint
                after_checkpoint = None
                if has_modified_files:
                    after_checkpoint = await self._git_checkpoint(
                        f"checkpoint: completed task — {user_task_desc}" if user_task_desc else "checkpoint: after AI task completed"
                    )

                self.emitter.emit_done(total_steps=round_num + 1)
                return LoopResult(
                    final_response=final_text,
                    tool_executions=executions,
                    total_rounds=round_num + 1,
                    total_tokens=total_tokens,
                    extra={
                        "memory_bank_update": mb_extracted,
                        "shadow_checkpoint_before": shadow_checkpoint or "",
                        "shadow_checkpoint_after": after_checkpoint or "",
                    },
                )

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
        )
