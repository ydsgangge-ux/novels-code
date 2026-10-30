"""手工打样入口：不经过 agent，直接跑一次委派。

    # 1) 先确认 CLI 装好了
    python -m gangge.layer4_tools.qoder_cli --probe

    # 2) 只读分析（不改文件，最安全）
    python -m gangge.layer4_tools.qoder_cli --mode analyze --goal "说明 storyboard 的渲染链路"

    # 3) 带验收契约的编辑任务
    python -m gangge.layer4_tools.qoder_cli --mode edit \
        --goal "给 _bullets 加 align 参数" \
        --scope src/gangge/layer4_tools/storyboard/templates.py \
        --scope tests/test_storyboard.py \
        --check "pytest tests/test_storyboard.py -q" \
        --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from gangge.layer4_tools.qoder_cli.config import (
    MODELS,
    PERMISSIONS,
    STRENGTHS,
    QoderConfig,
    probe_version,
)
from gangge.layer4_tools.qoder_cli.runner import delegate
from gangge.layer4_tools.qoder_cli.spec import CheckSpec, DelegateSpec


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m gangge.layer4_tools.qoder_cli",
        description="把一件可验证的活委派给 Qoder CLI",
    )
    p.add_argument("--goal", "-g", default="", help="任务目标（必填，除非用 --probe）")
    p.add_argument("--mode", default="edit", choices=["analyze", "edit", "verify"])
    p.add_argument("--scope", action="append", default=[], help="允许改的文件，可重复")
    p.add_argument("--forbidden", action="append", default=[], help="禁止改的文件，可重复")
    p.add_argument("--check", action="append", default=[],
                   help="验收命令，可重复；写 'cmd' 表示期望退出码 0")
    p.add_argument("--strength", default="medium", choices=list(STRENGTHS),
                   help="思考强度，落地成 --reasoning-effort")
    p.add_argument("--model", default="", choices=["", *MODELS],
                   help="模型名（--list-models 的值），留空 = Auto")
    p.add_argument("--permission", default="edit", choices=list(PERMISSIONS))
    p.add_argument("--max-turns", type=int, default=0,
                   help="仅当 CLI 支持 --max-turns 时下发（v1.1.80 不支持）")
    p.add_argument("--timeout", type=int, default=0, help="秒，0 = 默认 600")
    p.add_argument("--workspace", "-w", default="")
    p.add_argument("--json", action="store_true", help="只输出紧凑 JSON")
    p.add_argument("--probe", action="store_true", help="探测 CLI 是否可用后退出")
    return p


async def _run(args: argparse.Namespace) -> int:
    cfg = QoderConfig.from_env(args.workspace or None)
    if args.strength:
        cfg.strength = args.strength
    cfg.model = args.model
    cfg.permission = args.permission

    if args.probe:
        ok, detail = probe_version(cfg.executable)
        print(("OK " if ok else "FAIL ") + detail)
        return 0 if ok else 1

    if not args.goal.strip():
        print("[错误] 需要 --goal", file=sys.stderr)
        return 2

    spec = DelegateSpec(
        goal=args.goal,
        mode=args.mode,
        scope=tuple(args.scope),
        forbidden=tuple(args.forbidden),
        dod=tuple(CheckSpec(cmd=c) for c in args.check),
        max_turns=args.max_turns,
        timeout_s=args.timeout,
    )
    problems = spec.validate()
    if problems:
        print("[错误] spec 不合法:\n  - " + "\n  - ".join(problems), file=sys.stderr)
        return 2

    result = await delegate(spec, cfg)

    if args.json:
        print(result.for_model(4000))
    else:
        print(f"状态: {result.status}  用时 {result.duration_s:.1f}s  退出码 {result.exit_code}")
        print(f"结论: {result.summary or '(空)'}")
        if result.files_changed:
            print("改动: " + ", ".join(result.files_changed))
        for chk in result.checks:
            flag = "PASS" if chk.ok else "FAIL"
            print(f"  [{flag}] {chk.cmd} (exit={chk.exit_code})")
        if result.scope_violations:
            print("越界: " + ", ".join(result.scope_violations))
        if result.error:
            print(f"错误: {result.error}")
        if result.log_path:
            print(f"日志: {result.log_path}")
    return 0 if result.status == "done" else 1


def main() -> int:
    args = _build_parser().parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
