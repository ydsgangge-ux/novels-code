"""CLI entry point — parse args and launch the app."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


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


def _coerce(text: str) -> Any:
    """Turn a ``--set`` value into the obvious Python type."""
    lowered = text.strip().lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered in ("none", "null", ""):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def _parse_sets(items: list[str]) -> dict[str, dict[str, Any]]:
    """Parse ``--set row.key=value`` into ``{row: {key: value}}``."""
    updates: dict[str, dict[str, Any]] = {}
    for raw in items:
        target, sep, value = raw.partition("=")
        if not sep or "." not in target:
            raise SystemExit(f"--set 需要 ID.KEY=VALUE 形式，收到 {raw!r}")
        row_id, _, key = target.partition(".")
        updates.setdefault(row_id.strip(), {})[key.strip()] = _coerce(value)
    return updates


def _handle_runtime_flags(args: Any, ws: Path, profile: str) -> bool:
    """Handle the print-and-exit flags. Returns True when the CLI should stop."""
    from gangge.runtime import (
        describe_events,
        describe_seams,
        dump_tree,
        list_profiles,
        resolve,
    )

    if args.list_profiles:
        from gangge.runtime import load_profile

        for name in list_profiles(ws):
            try:
                desc = load_profile(name, ws).description
            except Exception:
                desc = ""
            marker = "*" if name == profile else " "
            print(f" {marker} {name:<12} {desc}")
        return True

    if args.list_mcp:
        from gangge.layer4_tools.mcp_client import (
            MCPClientManager,
            SELECTION_ENV,
            workspace_config_paths,
        )

        layers = workspace_config_paths(ws)
        if not any(p.exists() for p in layers):
            print("没有 MCP 配置。查找顺序（用户级 → 项目级）:")
            for path in layers:
                print(f"  · {path}")
            print("可以跑一次 gangge 并执行 /mcp，或在 GUI 里用「工具 → MCP 服务」生成模板。")
            return True
        print("配置来源:")
        for path in layers:
            print(f"  {'✓' if path.exists() else '·'} {path}")
        print()
        manager = MCPClientManager.from_workspace(ws)   # 只连接被选中的
        try:
            for line in manager.status_lines():
                print(line)
        finally:
            manager.disconnect_all()
        if os.environ.get(SELECTION_ENV) is None:
            print(f"\n提示: 用 --mcp all|none|<名字,...> 选择，或设环境变量 {SELECTION_ENV}。")
        return True

    if args.show_events:
        print(describe_events())
        return True

    if args.show_seams:
        print(describe_seams())
        return True

    if not args.dump_config:
        return False

    updates = _parse_sets(args.set)
    resolved = resolve(profile, workspace=ws, patches=args.patch)

    for row_id, values in updates.items():
        row = resolved.tree.get(row_id)
        if row is None:
            raise SystemExit(
                f"--set 目标行 '{row_id}' 不存在。可用行: {', '.join(resolved.tree.ids())}"
            )
        # A patch replaces the whole config, so restate it with the override.
        merged = dict(row.config)
        merged.update(values)
        resolved.tree.patch([{"id": row_id, "config": merged}], source="--set")

    print(dump_tree(resolved))
    return True


def _make_stdout_utf8_safe() -> None:
    """Windows consoles default to GBK, which cannot encode the banner emoji.

    A ``UnicodeEncodeError`` from a ``print`` would otherwise abort the CLI
    before any work starts, so the stream is switched to UTF-8 with replacement
    instead of crashing.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main() -> None:
    _make_stdout_utf8_safe()

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
        "-y", "--yes",
        action="store_true",
        help="自动批准所有需要确认的工具调用（无人值守/CI 用；默认会逐次询问）",
    )
    parser.add_argument(
        "--permission-policy",
        default=None,
        metavar="FILE",
        help="覆盖默认权限策略文件 (YAML)，默认使用内置 default_policy.yml",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="gangge-code 0.1.0",
    )

    # ── Runtime composition ──
    # Which implementations are mounted is decided by a profile (a list of
    # bundles plus patch layers), not by editing code. See gangge/runtime.
    runtime = parser.add_argument_group("runtime composition")
    runtime.add_argument(
        "--profile", "-P",
        default=None,
        help="插件组合 profile (standard / minimal / novel，默认读取 GANGGE_PROFILE 或 standard)",
    )
    runtime.add_argument(
        "--patch",
        action="append",
        default=[],
        metavar="FILE",
        help="额外的 patch 覆盖文件，可重复；后应用的层覆盖先应用的层",
    )
    runtime.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="ID.KEY=VALUE",
        help="覆盖某一行的配置项，如 --set tools.profile=novel",
    )
    runtime.add_argument(
        "--dump-config",
        action="store_true",
        help="打印最终组合出的插件树并退出",
    )
    runtime.add_argument(
        "--list-profiles",
        action="store_true",
        help="列出可用 profile 并退出",
    )
    runtime.add_argument(
        "--mcp",
        default=None,
        metavar="NAMES",
        help="本次启用哪些 MCP server：all / none / 逗号分隔的名字（默认跟随配置的 enabled）",
    )
    runtime.add_argument(
        "--list-mcp",
        action="store_true",
        help="列出 MCP server 的选择状态、工具数与 token 估算并退出",
    )
    runtime.add_argument(
        "--show-events",
        action="store_true",
        help="列出生命周期事件及其分发模式并退出",
    )
    runtime.add_argument(
        "--show-seams",
        action="store_true",
        help="列出能力 seam 及其内置实现并退出",
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
    if args.yes:
        os.environ["GANGGE_AUTO_APPROVE"] = "1"
    if args.permission_policy:
        os.environ["GANGGE_PERMISSION_POLICY"] = str(Path(args.permission_policy).resolve())
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

    # Profile selection is read by the UI layers through the environment, so
    # `gangge --profile minimal` works without threading a parameter everywhere.
    profile = args.profile or os.environ.get("GANGGE_PROFILE") or "standard"
    os.environ["GANGGE_PROFILE"] = profile
    if args.patch:
        os.environ["GANGGE_PATCH"] = os.pathsep.join(str(Path(p).resolve()) for p in args.patch)

    # MCP selection is read by the loop through the environment, same pattern as
    # the profile: nothing has to thread a parameter through the UI layers.
    if args.mcp is not None:
        from gangge.layer4_tools.mcp_client import SELECTION_ENV

        os.environ[SELECTION_ENV] = args.mcp

    # ── Introspection: print and exit, never launch a UI ──
    if _handle_runtime_flags(args, ws, profile):
        return

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
