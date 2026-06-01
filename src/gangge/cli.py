"""CLI entry point — parse args and launch the app."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path


def _resolve_workspace(cwd: Path, task: str | None = None) -> Path:
    """Determine the project workspace directory.

    Priority:
      1. Explicit -w argument (already set in GANGGE_WORKSPACE env)
      2. Auto-create a project folder under gangge_workspace/
    """
    explicit = os.environ.get("GANGGE_WORKSPACE")
    if explicit:
        ws = Path(explicit).resolve()
        if not ws.exists():
            ws.mkdir(parents=True, exist_ok=True)
            print(f"已创建项目目录: {ws}")
        return ws

    # Auto-create project folder
    projects_root = cwd / "gangge_projects"
    if task:
        # Use task keywords as project name
        import re
        safe = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff_-]", "", task)[:30]
        project_name = safe if safe else f"project_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    else:
        project_name = f"project_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    ws = projects_root / project_name
    ws.mkdir(parents=True, exist_ok=True)
    print(f"📁 项目目录: {ws}")
    return ws


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="gangge",
        description="Gangge Code — AI Coding Assistant",
    )
    parser.add_argument(
        "task",
        nargs="?",
        default=None,
        help="编程任务描述。提供后进入单次执行模式，不提供则进入交互式 REPL。",
    )
    parser.add_argument(
        "-w", "--workspace",
        default=None,
        help="项目目录 (默认: 自动创建在 ./gangge_projects/ 下)",
    )
    parser.add_argument(
        "-p", "--provider",
        default=None,
        choices=["anthropic", "openai", "deepseek", "qwen", "zhipu", "moonshot", "baichuan", "yi", "minimax", "stepfun", "siliconflow", "ollama", "custom"],
        help="LLM Provider (默认: 从 .env 读取)",
    )
    parser.add_argument(
        "-m", "--model",
        default=None,
        help="模型名称 (默认: 从 .env 读取)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="显示详细日志",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="gangge-code 0.1.0",
    )

    args = parser.parse_args()

    # Setup logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Set provider/model overrides via env
    if args.provider:
        os.environ["LLM_PROVIDER"] = args.provider
    if args.model:
        provider = args.provider or "deepseek"
        model_env_map = {
            "anthropic": "ANTHROPIC_MODEL",
            "openai": "OPENAI_MODEL",
            "deepseek": "DEEPSEEK_MODEL",
            "ollama": "OLLAMA_MODEL",
        }
        env_key = model_env_map.get(provider)
        if env_key:
            os.environ[env_key] = args.model

    # Resolve workspace — auto-create project folder
    cwd = Path.cwd()
    if args.workspace:
        ws = Path(args.workspace).resolve()
        if not ws.exists():
            ws.mkdir(parents=True, exist_ok=True)
            print(f"已创建项目目录: {ws}")
    else:
        ws = _resolve_workspace(cwd, task=args.task)
    os.environ["GANGGE_WORKSPACE"] = str(ws)

    # Route to appropriate UI:
    #   - task provided → CLI REPL one-shot
    #   - no task      → Textual TUI (existing)
    if args.task is not None:
        from gangge.cli_repl import main as repl_main
        sys.exit(repl_main(task=args.task))
    else:
        from gangge.layer1_ui.terminal import GanggeApp

        app = GanggeApp(workspace_dir=str(ws))
        app.run()


if __name__ == "__main__":
    main()
