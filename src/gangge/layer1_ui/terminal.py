"""Main TUI application — Gangge Terminal AI Coding Assistant."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.widgets import (
    Footer,
    Header,
    Input,
    RichLog,
    Static,
)

from gangge.layer3_agent.loop import AgenticLoop, LoopConfig
from gangge.layer3_agent.tools.registry import ToolRegistry
from gangge.layer3_agent.tools.bash import BashTool
from gangge.layer3_agent.tools.file_ops import ReadFileTool, WriteFileTool, EditFileTool
from gangge.layer3_agent.tools.search import GrepTool, GlobTool, ListDirTool
from gangge.layer3_agent.tools.web import WebFetchTool, WebSearchTool
from gangge.layer4_permission.guard import (
    PermissionGuard,
    PermissionDecision,
    PermissionRequest,
)
from gangge.layer5_llm.base import BaseLLM, ContentBlock, ContentType
from gangge.layer5_llm.registry import create_llm

logger = logging.getLogger(__name__)


class ChatBox(RichLog):
    """Scrollable chat message display with copy support."""

    def __init__(self, **kwargs):
        super().__init__(highlight=True, markup=True, max_lines=2000, **kwargs)
        self._chat_lines: list[tuple[str, str]] = []  # (style_tag, text)

    def add_assistant(self, text: str) -> None:
        self.write(Text(text, style="bold cyan"))
        self._chat_lines.append(("assistant", text))

    def add_user(self, text: str) -> None:
        self.write(Text(f"> {text}", style="bold green"))
        self._chat_lines.append(("user", text))

    def add_tool(self, name: str, status: str, detail: str = "") -> None:
        icon = "[green]✓[/]" if status == "ok" else "[red]✗[/]"
        msg = f"  {icon} [dim]{name}[/dim]"
        if detail:
            msg += f" — {detail[:100]}"
        self.write(Text.from_markup(msg))
        self._chat_lines.append(("tool", msg))

    def add_system(self, text: str) -> None:
        self.write(Text(text, style="dim yellow"))
        self._chat_lines.append(("system", text))

    def add_permission_request(self, tool: str, action: str) -> None:
        self.write(Text.from_markup(
            f"  [yellow]?[/] [bold]{tool}[/bold]: {action[:80]}"
        ))
        self._chat_lines.append(("perm", f"? {tool}: {action[:80]}"))

    def add_error(self, text: str) -> None:
        self.write(Text(f"  Error: {text}", style="bold red"))
        self._chat_lines.append(("error", text))

    def get_all_text(self) -> str:
        """Get all conversation text for copying."""
        parts = []
        for tag, text in self._chat_lines:
            prefix = {"user": "> ", "error": "Error: ", "system": "", "tool": "  ", "assistant": "", "perm": "  "}.get(tag, "")
            parts.append(f"{prefix}{text}")
        return "\n".join(parts)

    def clear(self) -> None:
        super().clear()
        self._chat_lines.clear()


class PermissionBar(Static):
    """Permission request bar."""

    def __init__(self, **kwargs):
        super().__init__("", **kwargs)
        self.pending_request: PermissionRequest | None = None
        self._decision: PermissionDecision | None = None
        self._decision_event: asyncio.Event | None = None

    def show_request(self, request: PermissionRequest) -> None:
        self.pending_request = request
        self._decision = None
        self._decision_event = asyncio.Event()
        risk_icon = {
            "safe": "[green]●[/]",
            "low": "[yellow]●[/]",
            "medium": "[yellow]▲[/]",
            "high": "[red]▲[/]",
            "critical": "[red]✕[/]",
        }.get(request.risk.level.value, "[?]")

        self.update(
            f"  {risk_icon} [bold]{request.tool_name}[/bold]: "
            f"{request.action[:60]} "
            f"[dim]({request.risk.level.value})[/dim]   "
            f"[green](Y)允许[/] / [red](N)拒绝[/] / [dim](A)总是允许[/]"
        )

    def respond(self, decision: PermissionDecision) -> None:
        self.pending_request = None
        self._decision = decision
        self.update("")
        if self._decision_event:
            self._decision_event.set()

    async def wait_for_response(self) -> PermissionDecision:
        if self._decision_event:
            await self._decision_event.wait()
        return self._decision or PermissionDecision.ALLOW


class StatusLine(Static):
    """Status line showing model info and token usage."""

    def __init__(self, **kwargs):
        super().__init__("", **kwargs)
        self.model = ""
        self.tokens_in = 0
        self.tokens_out = 0

    def update_info(self, model: str, tokens_in: int = 0, tokens_out: int = 0) -> None:
        self.model = model
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.update(
            f"  [dim]Model: {model} | "
            f"Tokens: {tokens_in}+{tokens_out}[/dim]"
        )


class GanggeApp(App):
    """Main Gangge TUI application."""

    TITLE = "Gangge Code"
    SUB_TITLE = "AI Coding Assistant"
    CSS = """
    Screen {
        layout: vertical;
    }
    #main-container {
        height: 1fr;
    }
    #chat-box {
        height: 100%;
        border: round $primary;
        padding: 1;
    }
    #bottom-bar {
        height: auto;
        max-height: 6;
        dock: bottom;
        layout: vertical;
    }
    #input-container {
        height: 3;
        padding: 0 1 0 1;
    }
    #user-input {
        width: 100%;
        height: 3;
    }
    #permission-bar {
        height: 1;
        padding: 0 1;
        background: $surface;
        border-top: solid $warning;
    }
    #status-bar {
        height: 1;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "cancel", "取消当前操作"),
        Binding("ctrl+p", "toggle_plan", "规划模式"),
        Binding("ctrl+n", "new_session", "新会话"),
        Binding("ctrl+s", "copy_chat", "复制对话到剪贴板"),
        Binding("ctrl+q", "quit", "退出"),
    ]

    def __init__(self, workspace_dir: str = ".", **kwargs):
        super().__init__(**kwargs)
        self.workspace_dir = str(Path(workspace_dir).resolve())
        self.plan_mode = False

        # Initialize components
        self.llm: BaseLLM | None = None
        self.tools = ToolRegistry()
        self.guard = PermissionGuard(ask_callback=self._ask_permission)
        self.loop: AgenticLoop | None = None

        self._task: asyncio.Task | None = None
        self._session_path = Path(self.workspace_dir) / ".gangge" / "session_history.txt"
        self._messages_path = Path(self.workspace_dir) / ".gangge" / "session_messages.json"

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="main-container"):
            yield ChatBox(id="chat-box")
        with Container(id="bottom-bar"):
            with Container(id="input-container"):
                yield Input(placeholder="输入消息... (Ctrl+P 规划模式, Ctrl+Q 退出)", id="user-input")
            yield PermissionBar(id="permission-bar")
            yield StatusLine(id="status-bar")
        yield Footer()

    async def on_mount(self) -> None:
        """Initialize app on mount."""
        chat = self.query_one("#chat-box", ChatBox)
        chat.add_system("正在初始化 Gangge Code...")

        try:
            # Create LLM
            self.llm = create_llm()
            chat.add_system(f"✓ LLM 已连接: {self.llm.model}")

            # Register tools (all file tools get workspace lock for relative path resolution)
            self.tools.register(BashTool(workspace=self.workspace_dir))
            self.tools.register(ReadFileTool(workspace=self.workspace_dir))
            self.tools.register(WriteFileTool(workspace=self.workspace_dir))
            self.tools.register(EditFileTool(workspace=self.workspace_dir))
            self.tools.register(GrepTool())
            self.tools.register(GlobTool())
            self.tools.register(ListDirTool())
            self.tools.register(WebFetchTool())
            self.tools.register(WebSearchTool())
            chat.add_system(f"✓ 已加载 {len(self.tools)} 个工具")

            # Create agentic loop
            import os as _os
            max_rounds = int(_os.environ.get("MAX_ROUNDS", "30"))
            config = LoopConfig(
                workspace_dir=self.workspace_dir,
                plan_mode=self.plan_mode,
                max_tool_rounds=max_rounds,
            )

            # ── RepoMap / Symbol / FileRegistry ──
            chat.add_system("🗂️ 正在分析项目结构...")
            try:
                from gangge.layer4_tools.repo_index import (
                    get_or_build_index, build_dependency_graph,
                    format_symbol_table, format_project_map,
                )
                index = get_or_build_index(self.workspace_dir)
                config.symbol_table = format_symbol_table(index)
                dep_graph = build_dependency_graph(index, self.workspace_dir)
                if dep_graph:
                    dep_lines = ["## 文件依赖关系"]
                    for path, deps in sorted(dep_graph.items()):
                        dep_lines.append(f"- `{path}` ← {', '.join(f'`{d}`' for d in deps)}")
                    config.dep_graph_summary = "\n".join(dep_lines[:80])
                config.project_map = format_project_map(index, dep_graph)
                config.file_registry = {
                    path: {
                        "classes": [s["name"] for s in entry.get("symbols", []) if s["kind"] == "class"][:10],
                        "functions": [s["name"] for s in entry.get("symbols", []) if s["kind"] in ("function", "method")][:15],
                        "last_action": "existing",
                        "round": 0,
                    }
                    for path, entry in index.get("files", {}).items()
                }
                file_count = len(index.get("files", {}))
                chat.add_system(f"🗂️ 已分析 {file_count} 个文件")
            except Exception as e:
                chat.add_system(f"项目分析跳过: {e}")

            self.loop = AgenticLoop(self.llm, self.tools, self.guard, config)

            status = self.query_one("#status-bar", StatusLine)
            status.update_info(self.llm.model)

            chat.add_system(f"✓ 工作目录: {self.workspace_dir}")
            # 扫描已有项目文件
            p = Path(self.workspace_dir)
            files = [f.name for f in p.iterdir() if f.is_file()] if p.exists() else []
            if files:
                chat.add_system(f"📄 项目文件 ({len(files)} 个): {', '.join(files[:8])}{'...' if len(files) > 8 else ''}")
            # 恢复上一次会话记录
            history_path = Path(self.workspace_dir) / ".gangge" / "session_history.txt"
            if history_path.exists():
                text = history_path.read_text(encoding="utf-8", errors="replace")[-5000:]
                if text.strip():
                    chat.add_system("\n[dim]── 上一次会话记录 ──[/dim]")
                    for line in text.splitlines():
                        if line.startswith("> "):
                            chat.add_user(line[2:])
                        elif line.startswith("[系统]"):
                            chat.add_system(line[6:])
                        elif line.startswith("  Error:"):
                            chat.add_error(line[8:])
                    chat.add_system("[dim]── 以上为历史记录 ──[/dim]\n")
            chat.add_system("准备就绪，输入消息开始对话。\n")
        except Exception as e:
            chat.add_error(f"初始化失败: {e}")
            chat.add_system("请检查 .env 配置文件。")

        # Focus input
        self.query_one("#user-input", Input).focus()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle user input submission."""
        if self._task and not self._task.done():
            return

        user_input = event.value.strip()
        if not user_input:
            return

        input_widget = event.input
        input_widget.value = ""

        chat = self.query_one("#chat-box", ChatBox)
        chat.add_user(user_input)

        # ── 修复：用 run_worker 在后台线程运行，不阻塞 UI ──
        self.run_worker(self.run_agent(user_input), exclusive=True)

    async def run_agent(self, user_input: str) -> None:
        """Run the agentic loop."""
        chat = self.query_one("#chat-box", ChatBox)
        status = self.query_one("#status-bar", StatusLine)

        from gangge.layer5_llm.base import Message, Role, ContentBlock
        from gangge.layer3_agent.progress_emitter import EventType

        if not self.loop:
            chat.add_error("Agent 未初始化")
            return

        # Subscribe to progress events
        def on_event(event):
            if event.type == EventType.THINKING:
                pass  # silent
            elif event.type == EventType.WARNING:
                chat.add_system(f"[yellow]⚠ {event.message}[/yellow]")
            elif event.type == EventType.TOOL_START:
                chat.add_system(f"  [cyan]▶ {event.data.get('tool_name','')}[/cyan]")
            elif event.type == EventType.TOOL_END:
                ok = event.data.get('success', True)
                icon = "✅" if ok else "❌"
                dur = event.data.get('duration_ms', 0)
                chat.add_system(f"  {icon} {event.data.get('tool_name','')} [{dur}ms]")
            elif event.type == EventType.PROGRESS:
                c = event.data.get('current', 0)
                t = event.data.get('total', 0)
                s = event.data.get('step_name', '')
                chat.add_system(f"[green]✅ {c}/{t} {s}[/green]")

        self.loop.emitter.subscribe(on_event)

        # Set stream callback
        async def on_stream(block: ContentBlock):
            if block.type == ContentType.TEXT and block.text:
                chat.add_assistant(block.text)

        self.loop.set_stream_callback(on_stream)

        # ── Build messages: restore previous session + new user input ──
        messages = self._load_session_messages()
        messages.append(Message(
            role=Role.USER,
            content=[ContentBlock(type=ContentType.TEXT, text=user_input)],
        ))

        chat.add_system("\n[dim]⏳ 开始分析任务...[/dim]\n")

        # Run the loop
        result = await self.loop.run(messages)

        # ── Save full message history for next session ──
        self._save_session_messages(messages)

        # Update status
        total_in = result.total_tokens.get("input", 0)
        total_out = result.total_tokens.get("output", 0)
        status.update_info(self.llm.model, total_in, total_out)

        # Show summary
        if result.tool_executions:
            chat.add_system(
                f"\n[dim]— {len(result.tool_executions)} 个工具调用, "
                f"{result.total_rounds} 轮, "
                f"{total_in + total_out} tokens —[/dim]\n"
            )
        else:
            chat.add_system(
                "\n[dim]— AI 未使用任何工具，任务未执行 —[/dim]\n"
            )

    async def on_key(self, event) -> None:
        """Handle key presses for permission responses."""
        perm_bar = self.query_one("#permission-bar", PermissionBar)
        if perm_bar.pending_request:
            if event.key == "y":
                perm_bar.respond(PermissionDecision.ALLOW)
            elif event.key == "n":
                perm_bar.respond(PermissionDecision.DENY)
            elif event.key == "a":
                # Always allow this pattern
                req = perm_bar.pending_request
                if req:
                    self.guard.remember_decision(
                        "bash", req.action, PermissionDecision.ALLOW
                    )
                perm_bar.respond(PermissionDecision.ALLOW)

    async def _ask_permission(self, request: PermissionRequest) -> PermissionDecision:
        """Permission callback — show in UI and wait for user response."""
        perm_bar = self.query_one("#permission-bar", PermissionBar)
        perm_bar.show_request(request)
        decision = await perm_bar.wait_for_response()
        return decision

    def action_toggle_plan(self) -> None:
        """Toggle plan mode."""
        self.plan_mode = not self.plan_mode
        if self.loop:
            self.loop.config.plan_mode = self.plan_mode
        chat = self.query_one("#chat-box", ChatBox)
        mode = "规划模式" if self.plan_mode else "执行模式"
        chat.add_system(f"切换到 {mode}")

    def action_new_session(self) -> None:
        """Start a new session."""
        # Save current session first
        self._save_session_history()
        chat = self.query_one("#chat-box", ChatBox)
        chat.clear()
        chat.add_system("\n--- 新会话 ---\n")

    def action_cancel(self) -> None:
        """Cancel current operation."""
        if self._task and not self._task.done():
            self._task.cancel()

    def action_copy_chat(self) -> None:
        """Copy all chat content to clipboard."""
        chat = self.query_one("#chat-box", ChatBox)
        text = chat.get_all_text()
        if text:
            try:
                import pyperclip
                pyperclip.copy(text)
                self.notify("✅ 对话已复制到剪贴板", timeout=2)
            except Exception:
                # Fallback: write to temp file
                import tempfile
                tmp = Path(tempfile.gettempdir()) / "gangge_chat_copy.txt"
                tmp.write_text(text, encoding="utf-8")
                self.notify(f"✅ 已保存到 {tmp}", timeout=3)

    def _save_session_history(self) -> None:
        """Save current session display text to file."""
        try:
            chat = self.query_one("#chat-box", ChatBox)
            text = chat.get_all_text()
            if text.strip():
                self._session_path.parent.mkdir(parents=True, exist_ok=True)
                self._session_path.write_text(text, encoding="utf-8")
        except Exception:
            pass

    def _save_session_messages(self, messages: list) -> None:
        """Save full message history as JSON for context restoration."""
        try:
            self._session_messages = messages
            data = [_message_to_dict(m) for m in messages]
            self._messages_path.parent.mkdir(parents=True, exist_ok=True)
            import json
            self._messages_path.write_text(
                json.dumps(data, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _load_session_messages(self) -> list:
        """Load previous session messages from JSON."""
        if not self._messages_path.exists():
            return []
        try:
            import json
            data = json.loads(self._messages_path.read_text(encoding="utf-8"))
            return [_message_from_dict(d) for d in data]
        except Exception:
            return []

    async def on_unmount(self) -> None:
        """Clean up on exit."""
        self._save_session_history()
        if self._task and not self._task.done():
            self._task.cancel()
        if self.llm:
            await self.llm.close()


# ═════════════════════════════════════════════════════════════════
#  Message serialization helpers (for session persistence)
# ═════════════════════════════════════════════════════════════════

def _message_to_dict(msg) -> dict:
    from gangge.layer5_llm.base import Role
    return {
        "role": msg.role.value if isinstance(msg.role, Role) else msg.role,
        "content": [_block_to_dict(b) for b in msg.content],
    }


def _block_to_dict(block) -> dict:
    d = {"type": block.type.value if isinstance(block.type, ContentType) else block.type}
    if block.text:
        d["text"] = block.text
    if block.tool_name:
        d["tool_name"] = block.tool_name
    if block.tool_call_id:
        d["tool_call_id"] = block.tool_call_id
    if block.tool_input:
        d["tool_input"] = block.tool_input
    if block.is_error:
        d["is_error"] = True
    return d


def _message_from_dict(d: dict):
    from gangge.layer5_llm.base import Message, Role
    blocks = [_block_from_dict(b) for b in d.get("content", [])]
    role = Role(d["role"]) if isinstance(d["role"], str) else d["role"]
    return Message(role=role, content=blocks)


def _block_from_dict(d: dict):
    from gangge.layer5_llm.base import ContentBlock
    ct = ContentType(d["type"]) if isinstance(d["type"], str) else d["type"]
    return ContentBlock(
        type=ct,
        text=d.get("text", ""),
        tool_name=d.get("tool_name", ""),
        tool_call_id=d.get("tool_call_id", ""),
        tool_input=d.get("tool_input", {}),
        is_error=d.get("is_error", False),
    )
