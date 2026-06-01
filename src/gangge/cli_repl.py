"""CLI REPL — Claude Code-style interactive terminal.

Modes:
  - One-shot:  gangge "task description"
  - Pipe:      echo "task" | gangge
  - REPL:      gangge (interactive prompt)

All modes share the same AgenticLoop engine as the PyQt6 desktop app.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule

from gangge.layer3_agent.loop import AgenticLoop, LoopConfig, LoopResult
from gangge.layer3_agent.prompts.system import build_system_prompt
from gangge.layer4_permission.guard import (
    PermissionDecision,
    PermissionGuard,
    PermissionRequest,
)
from gangge.layer5_llm.base import (
    BaseLLM,
    ContentBlock,
    ContentType,
    Message,
    Role,
)
from gangge.layer5_llm.registry import create_llm
from gangge.layer2_session import SessionManager
from gangge.layer2_session.storage import SessionStorage

logger = logging.getLogger(__name__)

console = Console()

# ═════════════════════════════════════════════════════════════════
#  Helpers
# ═════════════════════════════════════════════════════════════════

GANGGE_ART = "[bold cyan]Gangge Code — AI Coding Assistant v0.1.0[/bold cyan]"


def _print_banner(workspace: str, model: str):
    console.print(GANGGE_ART)
    info = f"工作目录: [bold]{workspace}[/bold]  |  模型: [yellow]{model}[/yellow]"
    console.print(Panel(info, border_style="dim"))
    console.print()


def _print_tool(name: str, status: str, detail: str = ""):
    """Print a tool call result. status='ok' or 'err'."""
    icon = "✓" if status == "ok" else "✗"
    color = "green" if status == "ok" else "red"
    msg = f"  [{color}]{icon}[/] [dim]{name}[/dim]"
    if detail:
        msg += f" — {detail[:120]}"
    console.print(msg)


def _print_assistant(text: str):
    if not text.strip():
        return
    console.print(Markdown(text.strip()), style="cyan")


def _print_user(text: str):
    console.print(f"\n[bold green]> {text}[/bold green]\n")


def _print_divider():
    console.print(Rule(style="dim"))


def _print_error(text: str):
    console.print(f"[bold red]✗ {text}[/bold red]")


def _print_info(text: str):
    console.print(f"[dim]{text}[/dim]")


def _print_summary(result: LoopResult, provider: str = "", model: str = ""):
    inp = result.total_tokens.get("input", 0)
    out = result.total_tokens.get("output", 0)
    cost = ""
    if provider:
        try:
            from gangge.pricing import estimate_cost
            cost = estimate_cost(provider, model or "", inp, out)
        except Exception:
            pass
    cost_part = f"  费用: {cost}" if cost else ""
    console.print(
        f"\n[dim]完成: {result.total_rounds} 轮, "
        f"{len(result.tool_executions)} 次工具调用, "
        f"Token: {inp} → {out}{cost_part}[/dim]"
    )


# ═════════════════════════════════════════════════════════════════
#  Settings (from env / defaults)
# ═════════════════════════════════════════════════════════════════

def _load_env() -> dict[str, str]:
    """Load .env if present, return env dict."""
    try:
        from dotenv import load_dotenv

        cwd = Path.cwd()
        for p in [cwd, *cwd.parents]:
            if (p / ".env").exists():
                load_dotenv(p / ".env")
                break
    except ImportError:
        pass
    return dict(os.environ)


def get_settings() -> dict[str, Any]:
    """Read settings from environment variables."""
    env = _load_env()
    return {
        "provider": env.get("LLM_PROVIDER", "deepseek"),
        "model": env.get("DEEPSEEK_MODEL", "deepseek-chat"),
        "max_rounds": int(env.get("MAX_ROUNDS", "30")),
        "workspace": os.environ.get("GANGGE_WORKSPACE", str(Path.cwd())),
    }


# ═════════════════════════════════════════════════════════════════
#  Core Execution
# ═════════════════════════════════════════════════════════════════

async def execute_task(
    llm: BaseLLM,
    task: str,
    workspace: str,
    max_rounds: int = 30,
    project_context: str = "",
    system_prompt_extra: str = "",
    provider: str = "",
    model_name: str = "",
    history_messages: list[Message] | None = None,
) -> LoopResult:
    """Run a single task through the AgenticLoop, streaming output.

    Args:
        llm: LLM instance
        task: User's task description
        workspace: Working directory
        max_rounds: Max tool-call rounds
        project_context: Project context for system prompt
        system_prompt_extra: Extra text appended to system prompt
        provider: LLM provider name (for cost calculation)
        model_name: Model name (for cost calculation)
        history_messages: Previous conversation messages for multi-turn context

    Returns:
        LoopResult with final response and execution records
    """
    # ── Permission guard ──
    async def ask_callback(req: PermissionRequest) -> PermissionDecision:
        return PermissionDecision.ALLOW

    guard = PermissionGuard(ask_callback=ask_callback)

    # ── Tools ──
    from gangge.layer3_agent.tools.registry import create_tool_registry

    async def _ask_user_callback(question: str) -> str:
        console.print(f"\n[yellow]❓ {question}[/yellow]")
        try:
            answer = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        return answer

    registry = create_tool_registry(
        workspace=workspace,
        ask_user_callback=_ask_user_callback,
    )

    # ── Config ──
    config = LoopConfig(
        max_tool_rounds=max_rounds,
        workspace_dir=workspace,
        project_context=project_context,
        system_prompt=build_system_prompt(workspace_dir=workspace, project_context=project_context),
        ask_user_callback=_ask_user_callback,
    )
    if system_prompt_extra:
        config.system_prompt += f"\n\n{system_prompt_extra}"

    # ── RepoMap / Symbol / FileRegistry ──
    if workspace:
        _print_info("🗂️ 正在分析项目结构...")
        try:
            from gangge.layer4_tools.repo_index import (
                get_or_build_index, build_dependency_graph,
                format_symbol_table, format_project_map,
            )
            index = get_or_build_index(workspace)
            config.symbol_table = format_symbol_table(index)
            dep_graph = build_dependency_graph(index, workspace)
            if dep_graph:
                dep_lines = ["## 文件依赖关系 (修改文件前请检查影响范围)"]
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
            _print_info(f"🗂️ 已分析 {file_count} 个文件")
        except Exception as e:
            _print_info(f"项目分析跳过: {e}")

    # ── Loop ──
    loop = AgenticLoop(llm=llm, tools=registry, permission_guard=guard, config=config)

    # Streaming: print text directly (no Rich.Live — causes terminal deadlock on Windows)
    async def stream_cb(block: ContentBlock):
        if block.type == ContentType.TEXT and block.text.strip():
            console.print(block.text, style="cyan", end="")
        elif block.type == ContentType.THINKING and block.text.strip():
            console.print(f"[dim]🤔 {block.text[:80]}[/dim]")

    loop.set_stream_callback(stream_cb)

    messages = list(history_messages) if history_messages else []
    messages.append(Message(role=Role.USER, content=task))
    _print_user(task)
    _print_divider()
    console.print("[dim]⏳ 等待 AI 回复...[/dim]")

    result = await loop.run(messages)
    console.print()

    # Print tool calls summary
    for exc in result.tool_executions:
        status = "ok" if not exc.is_error else "err"
        _print_tool(exc.tool_name, status, exc.output[:100])

    # Print final response
    if result.final_response and not result.tool_executions:
        _print_assistant(result.final_response)
    elif result.final_response:
        _print_assistant(f"\n{result.final_response}")

    _print_summary(result, provider=provider, model=model_name)
    return result


# ═════════════════════════════════════════════════════════════════
#  Modes
# ═════════════════════════════════════════════════════════════════

def run_oneshot(task: str, settings: dict[str, Any]) -> int:
    """Execute a single task and exit."""
    try:
        llm = create_llm(settings["provider"])
    except Exception as e:
        _print_error(f"LLM 初始化失败: {e}")
        return 1

    try:
        asyncio.run(
            execute_task(
                llm=llm,
                task=task,
                workspace=settings["workspace"],
                max_rounds=settings.get("max_rounds", 30),
                provider=settings.get("provider", ""),
                model_name=settings.get("model", ""),
            )
        )
    except KeyboardInterrupt:
        _print_info("\n⏹ 已取消")
        return 130
    except Exception as e:
        _print_error(f"执行失败: {e}")
        return 1
    return 0


def run_repl(settings: dict[str, Any]) -> int:
    """Interactive REPL mode with session persistence."""
    try:
        llm = create_llm(settings["provider"])
    except Exception as e:
        _print_error(f"LLM 初始化失败: {e}")
        return 1

    _print_banner(settings["workspace"], settings.get("model", ""))

    # ── Initialize SessionManager for persistence ──
    storage = SessionStorage(db_path=".gangge_data/sessions.db")
    session_mgr = SessionManager(storage=storage, auto_save=True)

    # Try to load the most recent session
    history: list[Message] = []
    current_session_id = ""
    try:
        asyncio.run(session_mgr.init())
        # List sessions and load the latest one
        sessions_list = asyncio.run(storage.list_sessions(limit=1))
        if sessions_list:
            latest = sessions_list[0]
            loaded = asyncio.run(session_mgr.load_session(latest["id"]))
            if loaded:
                history = loaded.messages
                current_session_id = loaded.id
                session_mgr.current = loaded
                _print_info(f"📋 已加载历史会话 ({len(history)} 条消息)")
        if not current_session_id:
            new_session = asyncio.run(session_mgr.new_session("CLI REPL"))
            current_session_id = new_session.id
    except Exception as e:
        _print_info(f"会话管理初始化跳过: {e}")

    conversation_count = 0

    while True:
        try:
            # Check for pipe input
            if not sys.stdin.isatty():
                task = sys.stdin.read().strip()
                if not task:
                    break
                # Don't print prompt, just execute
            else:
                try:
                    task = input("[green]> [/green]").strip()
                except (EOFError, KeyboardInterrupt):
                    _print_info("\n再见!")
                    break

            if not task:
                continue
            if task in ("exit", "quit", "q"):
                break

            conversation_count += 1
            _print_divider()

            # ── Save user message to history ──
            history.append(Message(role=Role.USER, content=task))

            result = asyncio.run(
                execute_task(
                    llm=llm,
                    task=task,
                    workspace=settings["workspace"],
                    max_rounds=settings.get("max_rounds", 30),
                    provider=settings.get("provider", ""),
                    model_name=settings.get("model", ""),
                    history_messages=history[:-1],  # Pass all previous messages (exclude current task)
                )
            )

            # ── Save assistant response to history ──
            if result.final_response:
                history.append(
                    Message(role=Role.ASSISTANT, content=result.final_response)
                )

            # ── Persist session to SQLite ──
            try:
                # Add messages to session manager
                for msg in [history[-2], history[-1]]:  # user + assistant
                    session_mgr.current.messages.append(msg)
                asyncio.run(session_mgr.save())
            except Exception as e:
                logger.warning(f"会话保存失败: {e}")

            _print_divider()

            # If piped input, exit after first task
            if not sys.stdin.isatty():
                break

        except KeyboardInterrupt:
            _print_info("\n⏹ 已取消")
            continue
        except Exception as e:
            _print_error(f"错误: {e}")
            continue

    # ── Close session storage ──
    try:
        asyncio.run(session_mgr.close())
    except Exception:
        pass

    return 0


# ═════════════════════════════════════════════════════════════════
#  Entry
# ═════════════════════════════════════════════════════════════════

def main(task: str | None = None, **overrides) -> int:
    """Entry point. Called from cli.py.

    Args:
        task: Task string. If None, enters REPL mode.
        **overrides: Override settings (provider, model, workspace, etc.)
    """
    settings = get_settings()
    settings.update(overrides)

    if task:
        return run_oneshot(task, settings)
    return run_repl(settings)