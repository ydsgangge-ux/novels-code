"""3D 立体展示页：把一个分镜页装进浏览器框里，滚动着推近介绍。

效果由三件事叠加而成：

* **立体**——``#stage`` 给 ``perspective``，``#world`` 保留 3D 上下文，浏览器框和
  漂浮的小标签各自站在不同的 ``translateZ`` 上，镜头一动就有前后景。
* **滚动即机位**——页面只有一屏高，靠一个高的占位块产生滚动条；滚动进度被映射成
  ``translateZ``（从远处推到几乎贴脸）和 ``rotateX/rotateY``（俯视 → 平视），
  于是"上下滑动"本身就是在推镜头。
* **滚动即分镜**——滚动进度同时被切成 N 段（N = 分镜镜头数），第 i 段把框里那页
  ``__seek`` 到第 i 个镜头并从头播到尾。滑到哪儿，里面的动画就走到哪儿。

鼠标再加两层：指针位置驱动整体倾角（带阻尼），以及跟随指针的高光和反光条。全部
只用 ``transform``，不触发重排；``prefers-reduced-motion`` 下自动关掉鼠标倾斜。

输出是**单个 HTML 文件**，内页以 base64 内联进 ``iframe srcdoc``，所以断网、发给
别人、直接双击都能看——这一点跟"推荐项目的那种展示页"是同一个诉求。

运行（纯 Python，不装浏览器也能出页）：

    python -m gangge.layer4_tools.storyboard.showcase
    python -m gangge.layer4_tools.storyboard.showcase -f sb.json -o out/index.html

代码里用：

    from gangge.layer4_tools.storyboard.showcase import write_showcase
    write_showcase(storyboard, "out/index.html")
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path
from string import Template
from typing import Sequence

from gangge.layer4_tools.storyboard.demo import demo_storyboard
from gangge.layer4_tools.storyboard.pages import read_page
from gangge.layer4_tools.storyboard.schema import Scene, Storyboard
from gangge.layer4_tools.storyboard.templates import (
    _hex_to_rgb,
    render_page,
    style_palette,
)

#: 每段镜头滚动多高（视口高度的倍数）。1.0 = 滑一屏换一个镜头。
CHAPTER_VH = 1.0
#: 结尾多留一点滚动距离，最后那个镜头能停住看清楚。
TAIL_VH = 0.55
#: 浏览器框的标题栏高度，布局时要从可视高度里扣掉。
CHROME_H = 38

_TPL_NOTE: dict[str, str] = {
    "title": "开场镜头 · 一句话把结论说完",
    "bullets": "要点镜头 · 一条一条落下来",
    "flow": "流程镜头 · 节点依次点亮",
    "quote": "金句镜头 · 停一拍再说下一句",
    "outro": "收尾镜头 · 给出行动点",
}


def _chapter_label(index: int) -> str:
    return f"{index + 1:02d}"


def _chapter_title(scene: Scene) -> str:
    text = (scene.title or scene.text or scene.template).strip()
    return text[:42]


def _chapter_note(scene: Scene) -> str:
    if scene.voiceover:
        return scene.voiceover.strip()
    return _TPL_NOTE.get(scene.template, scene.template)


def chapters_of(
    sb: Storyboard, slots: Sequence[tuple[Scene, float, float]] | None = None
) -> list[dict[str, object]]:
    """把每个镜头变成一段滚动章节。

    每组都有两套时间：``s0/s1`` 是镜头的真实起止（录视频时按它把 ``t`` 换算成
    滚动进度，配音和画面才严格对齐），``t0/t1`` 内缩了 0.3s，手动滚动时用它，
    免得停在空镜或切镜闪白的那一帧上。
    """
    out: list[dict[str, object]] = []
    for i, (scene, start, end) in enumerate(slots or sb.timeline()):
        t0 = round(start + 0.30, 3)
        t1 = round(max(t0 + 0.2, end - 0.30), 3)
        out.append(
            {
                "label": _chapter_label(i),
                "title": _chapter_title(scene),
                "note": _chapter_note(scene),
                "s0": round(start, 3),
                "s1": round(max(start + 0.2, end), 3),
                "t0": t0,
                "t1": t1,
            }
        )
    return out


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def render_showcase(
    sb: Storyboard,
    slots: Sequence[tuple[Scene, float, float]] | None = None,
    *,
    source_url: str = "",
    inner_html: str | None = None,
    scroll_vh: float | None = None,
) -> str:
    """分镜 → 单文件 3D 展示页（HTML 字符串）。

    ``slots`` 只在渲染视频时传：它是配音合成之后的真实时间轴，展示页内部的分镜
    和章节都按它排，画面才跟配音对得上。``source_url`` 只是显示在假地址栏里。

    ``inner_html`` 用来换掉框里的东西——传一段整页截图页面进去，这个展示页就变成
    "框里装的是那个真实网站"（见 :mod:`gangge.layer4_tools.storyboard.site`）。
    ``scroll_vh`` 覆盖滚动长度，截图那种长内容需要比一屏多滑几屏。
    """
    slots = tuple(slots) if slots else sb.timeline()
    inner = inner_html if inner_html is not None else render_page(sb, slots)
    chapters = chapters_of(sb, slots)
    palette = style_palette(sb)
    n = max(1, len(chapters))
    total = round(slots[-1][2], 3) if slots else 0.0
    spacer = scroll_vh if scroll_vh is not None else n * CHAPTER_VH + TAIL_VH
    return _PAGE.substitute(
        title=_html(sb.title or "showcase"),
        brand=_html(sb.title or "分镜展示"),
        address=_html(source_url or f"{sb.title or '分镜'} · 预览"),
        bg=palette["bg"],
        fg=palette["fg"],
        fg_rgb=_hex_to_rgb(palette["fg"]),
        muted=palette["muted"],
        accent=palette["accent"],
        accent_rgb=_hex_to_rgb(palette["accent"]),
        accent2=palette["accent2"],
        accent2_rgb=_hex_to_rgb(palette["accent2"]),
        aspect=f"{sb.width / max(1, sb.height):.4f}",
        chrome=CHROME_H,
        total=f"{total:.3f}",
        chapters=json.dumps(chapters, ensure_ascii=False),
        # 只有一段时不画进度刻度：一根竖线立在右边反而像没加载完
        rails="".join("<i></i>" for _ in range(n)) if n > 1 else "",
        spacer_vh=f"{spacer:.2f}",
        inner_b64=_b64(inner),
    )


def write_showcase(
    sb: Storyboard,
    out: Path | str | None = None,
    *,
    slots: Sequence[tuple[Scene, float, float]] | None = None,
    source_url: str = "",
) -> Path:
    """写盘，返回生成的 HTML 路径。"""
    target = Path(out) if out else Path(".gangge") / "showcase" / "index.html"
    if target.suffix.lower() not in (".html", ".htm"):
        target = target / "index.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        render_showcase(sb, slots, source_url=source_url), encoding="utf-8"
    )
    return target


def _html(text: str) -> str:
    import html

    return html.escape(str(text), quote=True)


_PAGE = Template(read_page("showcase"))


def _from_url(url: str) -> Storyboard:
    """抓页面 + 让主模型写分镜；任何配置/抓取问题都给一句能照做的提示。"""
    import asyncio

    from gangge.layer4_tools.storyboard.sources import SourceError, draft_from_url

    try:
        from gangge.layer5_llm.registry import create_llm

        llm = create_llm(None)
    except Exception as e:  # noqa: BLE001 - 配置问题，不是崩溃
        raise SystemExit(
            f"无法初始化主模型: {e}\n"
            "检查 .env 里的 LLM_PROVIDER 和对应 *_API_KEY；"
            "也可以先用 -f 传一份手写分镜。"
        )
    try:
        return asyncio.run(draft_from_url(url, llm))
    except SourceError as e:
        raise SystemExit(str(e))
    except Exception as e:  # noqa: BLE001 - 网络/模型都算可解释失败
        raise SystemExit(f"从网址写分镜失败: {e}")


def _load(args: argparse.Namespace) -> tuple[Storyboard, str]:
    """返回 (分镜, 来源网址)——网址只用于假地址栏，没给就是空串。"""
    if args.url:
        return _from_url(args.url), args.url
    if args.file:
        path = Path(args.file)
        if not path.exists():
            raise SystemExit(f"分镜文件不存在: {path}")
        return Storyboard.from_dict(json.loads(path.read_text(encoding="utf-8"))), ""
    sb = demo_storyboard(args.style or "neon")
    if args.portrait:
        sb.width, sb.height = 1080, 1920
    return sb, ""


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    p = argparse.ArgumentParser(
        prog="python -m gangge.layer4_tools.storyboard.showcase",
        description="把分镜页做成 3D 立体展示页（单文件 HTML，可断网打开）。",
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument("-f", "--file", metavar="JSON", help="分镜 JSON；不给就用内置样片")
    src.add_argument("-u", "--url", metavar="URL", help="网址：抓页面后由主模型写分镜")
    p.add_argument("--style", help="视觉风格，如 neon/dark/paper/glass/brutal")
    p.add_argument("--portrait", action="store_true", help="竖屏画幅 1080×1920")
    p.add_argument(
        "--shot",
        action="store_true",
        help="配合 --url：抓整页截图，框里放那个网站本身（而不是从它写分镜）",
    )
    p.add_argument("-o", "--out", metavar="PATH", help="输出 HTML 或目录")
    args = p.parse_args()

    if args.shot:
        if not args.url:
            raise SystemExit("--shot 需要配合 --url 使用")
        from gangge.layer4_tools.storyboard.site import SiteError, build_site_page

        try:
            target = build_site_page(args.url, args.out, style=args.style or "neon")
        except SiteError as e:
            raise SystemExit(f"截图失败: {e}")
        print(f"展示页: {target}   （框内是该站点的整页截图，滚动时整页上移）")
        return 0

    sb, source_url = _load(args)
    if args.style:
        sb.style = args.style
    target = write_showcase(sb, args.out, source_url=source_url)
    print(f"展示页: {target}   ({len(sb.scenes)} 个镜头，滚动 {len(sb.scenes)} 段)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
