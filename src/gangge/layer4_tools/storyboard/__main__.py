"""Command line entry: ``python -m gangge.layer4_tools.storyboard``.

Exists so the pipeline is usable without the agent loop — no chat session, no
tool-calling round-trip. ``--file`` renders a storyboard you already have;
``--topic`` borrows the main model once to draft one, then does the rest here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from gangge.layer4_tools.storyboard.schema import DECOR_NAMES, STYLE_NAMES


SAMPLE_STORYBOARD: dict = {
    "title": "演示视频示例",
    "scenes": [
        {
            "template": "title",
            "title": "自动生成演示视频",
            "text": "文案 · 动画 · 配音，一条流水线",
            "voiceover": "这是一条自动生成的演示视频，文案、动画和配音都由流水线完成。",
        },
        {
            "template": "bullets",
            "title": "三个环节",
            "items": ["模型只写分镜", "TTS 逐句配音", "逐帧渲染成片"],
            "voiceover": "整条链路只有三步：模型负责写分镜，语音合成负责配音，渲染器把画面逐帧拍下来。",
        },
        {
            "template": "flow",
            "title": "处理流程",
            "nodes": ["主题", "分镜", "配音", "成片"],
            "voiceover": "你只需要给一个主题，剩下的分镜、配音、合成都自动完成。",
        },
        {
            "template": "outro",
            "title": "现在就能用",
            "text": "说出主题，等待出片",
            "items": ["demo_video"],
            "voiceover": "打开演示视频工具，描述你想演示的内容，剩下的交给流水线。",
        },
    ],
}


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m gangge.layer4_tools.storyboard",
        description="分镜 → 带配音的演示视频（mp4），不进入 agent 会话。",
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument("-f", "--file", metavar="JSON", help="分镜 JSON 文件")
    src.add_argument("-t", "--topic", metavar="TEXT", help="主题，用主模型自动写分镜")
    src.add_argument(
        "-c", "--script", metavar="FILE", help="现成文案（txt/md）：按段切成镜头，原文照念"
    )
    src.add_argument("-s", "--sample", action="store_true", help="使用内置示例分镜")
    # 不在互斥组里：单独给 = 由页面自动写分镜；配合 -c = 展示页要截的那个站点
    p.add_argument(
        "-u",
        "--url",
        metavar="URL",
        help="网址：单独给就当作分镜来源；配合 -c 就是展示页里要装的那个网站",
    )

    p.add_argument("-o", "--out", metavar="DIR", help="输出目录，默认 .gangge/demo_videos/<slug>")
    p.add_argument("--provider", help="LLM provider，覆盖 .env 的 LLM_PROVIDER")
    p.add_argument("--model", help="模型名，覆盖 provider 默认值")
    p.add_argument("--api-key", dest="api_key", help="API Key，仅本次生效")
    p.add_argument("--style", choices=list(STYLE_NAMES), help="视觉风格预设")
    p.add_argument("--decor", help="装饰层，逗号分隔：glow,grid,grain,particles,scanline")
    p.add_argument("--voice", default="zh-CN-XiaoxiaoNeural", help="edge-tts 音色")
    p.add_argument("--fps", type=int, help="帧率，默认沿用分镜（30）")
    p.add_argument("--no-narration", action="store_true", help="不配音，按字数估算时长")
    p.add_argument("--preview", action="store_true", help="只生成 HTML，不渲染视频")
    p.add_argument("--keep-frames", action="store_true", help="保留 PNG 帧序列")
    p.add_argument(
        "--page",
        choices=("storyboard", "showcase", "site"),
        default="storyboard",
        help="拍哪种页面：朴素分镜页（默认）/ 3D 展示页 / 3D 展示页+真实网站截图（需 --url）",
    )
    return p


def _make_llm(args: argparse.Namespace) -> tuple[object | None, str]:
    """按 .env / 命令行参数建主模型，失败时把原因交回去。"""
    import os

    from gangge.layer5_llm.registry import create_llm

    try:
        if args.api_key and args.provider:
            os.environ[f"{args.provider.upper()}_API_KEY"] = args.api_key
        llm = create_llm(args.provider)
        if args.model:
            llm.model = args.model
        return llm, ""
    except Exception as e:  # noqa: BLE001 - 配置问题，不是崩溃
        return None, str(e)


def _explain_llm_failure(reason: str) -> int:
    env_path = Path(".env")
    print(f"无法初始化主模型: {reason}", file=sys.stderr)
    print(
        f"检查 {env_path.resolve() if env_path.exists() else '.env（不存在）'}："
        "需要 LLM_PROVIDER 和该 provider 的 *_API_KEY。",
        file=sys.stderr,
    )
    print("也可以用 --sample 生成示例视频，不需要模型。", file=sys.stderr)
    return 1


async def _run(args: argparse.Namespace) -> int:
    from gangge.layer4_tools.storyboard import Storyboard, build_video, write_preview

    given = [bool(args.file), bool(args.topic), bool(args.script), bool(args.sample)]
    if sum(given) > 1:
        print("分镜来源只能给一个：-f / -t / -c / -s", file=sys.stderr)
        return 2
    if not any(given) and not args.url:
        print("需要分镜来源：-f / -t / -c / -s / -u 之一", file=sys.stderr)
        return 2

    if args.file:
        path = Path(args.file)
        if not path.exists():
            print(f"分镜文件不存在: {path}", file=sys.stderr)
            return 2
        storyboard = Storyboard.from_dict(json.loads(path.read_text(encoding="utf-8")))
        label = storyboard.title or "storyboard"
    elif args.sample:
        storyboard = Storyboard.from_dict(SAMPLE_STORYBOARD)
        label = "示例"
    elif args.script:
        from gangge.layer4_tools.storyboard.script import storyboard_from_script

        path = Path(args.script)
        if not path.exists():
            print(f"文案文件不存在: {path}", file=sys.stderr)
            return 2
        try:
            storyboard = storyboard_from_script(
                path.read_text(encoding="utf-8-sig"), style=args.style or "dark"
            )
        except ValueError as e:
            print(f"文案切分失败: {e}", file=sys.stderr)
            return 2
        label = storyboard.title
        print(f"文案: {path.name} → {len(storyboard.scenes)} 个镜头")
    elif args.url:
        # Imported lazily: drafting needs a model, rendering does not, and this
        # module must stay importable on a machine with no API key.
        from gangge.layer4_tools.storyboard.sources import SourceError, draft_from_url

        llm, reason = _make_llm(args)
        if llm is None:
            return _explain_llm_failure(reason)
        try:
            storyboard = await draft_from_url(args.url, llm)
        except SourceError as e:
            print(f"从网址写分镜失败: {e}", file=sys.stderr)
            print("也可以手写 JSON 用 --file 传入，或 -t 直接给主题。", file=sys.stderr)
            return 2
        label = storyboard.title or args.url
        print(f"来源: {args.url}")
    else:
        # Imported lazily: drafting needs a model, rendering does not, and this
        # module must stay importable on a machine with no API key.
        from gangge.layer3_agent.tools.demo_video import DemoVideoTool

        llm, reason = _make_llm(args)
        if llm is None:
            return _explain_llm_failure(reason)

        tool = DemoVideoTool(workspace=".", llm=llm)
        try:
            storyboard = Storyboard.from_dict(await tool._draft(args.topic))
        except Exception as e:  # noqa: BLE001 - surfaced to the operator
            print(f"自动写分镜失败: {e}", file=sys.stderr)
            print("可以先用 --preview 检查，或手写 JSON 用 --file 传入。", file=sys.stderr)
            return 1
        label = args.topic

    if args.voice:
        storyboard.voice = args.voice
    if args.fps:
        storyboard.fps = max(10, min(60, args.fps))
    if args.style:
        storyboard.style = args.style
    if args.decor is not None:
        # An explicit empty string means "no ornaments at all", which differs
        # from omitting the flag (style defaults apply).
        storyboard.decor = [
            d.strip() for d in args.decor.split(",") if d.strip() in DECOR_NAMES
        ]

    site_shot = None
    if args.page == "site":
        if not args.url:
            print("--page site 需要 --url：框里要装的是那个网站的截图", file=sys.stderr)
            return 2
        from gangge.layer4_tools.storyboard.site import SiteError, capture_site

        if not args.style:
            storyboard.style = "neon"      # 展示页默认就是发光深色
        print(f"截图: {args.url}")
        try:
            site_shot = await capture_site(
                args.url,
                color_scheme="dark"
                if storyboard.style in ("dark", "neon", "glass")
                else "light",
            )
        except SiteError as e:
            print(f"截图失败: {e}", file=sys.stderr)
            return 2
        print(f"截图尺寸: {site_shot.size_note()}")
        # 16:10：展示网页时多露一点内容，比 16:9 更像一块屏幕
        storyboard.width, storyboard.height = 1920, 1200

    out_dir = Path(args.out) if args.out else Path(".gangge") / "demo_videos" / _slug(label)

    print(f"分镜: {len(storyboard.scenes)} 个镜头，预计 {storyboard.total_duration:.1f}s")
    page_label = {
        "storyboard": "分镜页",
        "showcase": "3D 展示页",
        "site": "3D 展示页 + 网站截图",
    }[args.page]
    print(f"页面: {page_label}")
    print(f"输出: {out_dir}")
    source_url = args.url or ""

    if args.preview:
        html = write_preview(
            storyboard,
            out_dir,
            page=args.page,
            source_url=source_url,
            site_shot=site_shot,
        )
        print(f"预览已生成: {html}")
        return 0

    result = await build_video(
        storyboard,
        out_dir,
        narration=not args.no_narration,
        keep_frames=args.keep_frames,
        page=args.page,
        source_url=source_url,
        site_shot=site_shot,
        on_progress=lambda msg: print(f"  · {msg}"),
    )

    if result.video:
        print(f"\n✅ 视频: {result.video}  ({result.duration:.1f}s, {result.fps}fps)")
    else:
        print("\n⚠️ 视频未生成：")
        for note in result.notes:
            print(f"  - {note}")
        if result.frames_dir:
            print(f"  帧序列: {result.frames_dir}")
    if result.srt:
        print(f"字幕: {result.srt}")
    print(f"分镜页: {result.html}")
    return 0 if result.video else 1


def _slug(text: str) -> str:
    import re
    import time

    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "-", text or "").strip("-")
    return (cleaned[:16] or "demo") + "-" + time.strftime("%Y%m%d-%H%M%S")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
