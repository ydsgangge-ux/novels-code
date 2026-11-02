"""样式工作台：把风格 / 装饰 / 画幅 / 帧率做成四个**独立旋钮**。

为什么不是一个"套餐列表"：``5 风格 × 2^5 装饰 × 2 画幅`` 是乘出来的 320 种组合，
枚举成预设既写不完也选不动。所以这里只做两件事：

1. Python 侧把 **5 风格 × 2 画幅 = 10 份**真实页面渲染好，base64 塞进一个
   ``style-studio.html``（单文件，双击就能开，断网也行）；
2. 每份页面把**五层装饰全部生成**，前端按勾选显隐——32 种装饰组合不用预渲染，
   体积只有"每种组合各存一份"的三十二分之一。

为什么不能纯前端换风格：``style`` 除了调色板，还带 ``_STYLE_EXTRA``（neon 的三层
辉光写死在 CSS 里）、``style_class`` 作用域和默认装饰组合。换风格必须重新渲染，
这也是要预生成 10 份、而不是 1 份的原因。

运行：

    python -m gangge.layer4_tools.storyboard.studio
    python -m gangge.layer4_tools.storyboard.studio -o out/studio.html --fps 24
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

from gangge.layer4_tools.storyboard.demo import demo_storyboard
from gangge.layer4_tools.storyboard.pages import read_page
from gangge.layer4_tools.storyboard.schema import DECOR_NAMES, STYLE_NAMES
from gangge.layer4_tools.storyboard.templates import default_decor, render_page

#: 画幅 → 渲染基准尺寸（横屏投屏、竖屏手机各自一套排版）。
ASPECTS: dict[str, tuple[int, int]] = {"16:9": (1920, 1080), "9:16": (1080, 1920)}


def _aspect_key(aspect: str) -> str:
    return "portrait" if ASPECTS[aspect][1] > ASPECTS[aspect][0] else "landscape"


def preview_pages() -> dict[str, str]:
    """每个 ``风格|画幅`` 一份页面（base64），装饰层全开等着前端开关。"""
    pages: dict[str, str] = {}
    for style in STYLE_NAMES:
        for aspect in ASPECTS:
            sb = demo_storyboard(style)
            sb.width, sb.height = ASPECTS[aspect]
            # 全开：前端勾掉哪层就隐藏哪层，不用为 2^5 种组合各渲染一遍
            sb.decor = list(DECOR_NAMES)
            html = render_page(sb, sb.timeline())
            pages[f"{style}|{_aspect_key(aspect)}"] = base64.b64encode(
                html.encode("utf-8")
            ).decode("ascii")
    return pages


def build_studio(out: Path | str | None = None, *, fps: int = 30) -> Path:
    """写出一份自包含的工作台 HTML，返回路径。"""
    target = Path(out) if out else Path(".gangge") / "style-studio.html"
    if target.suffix.lower() not in (".html", ".htm"):
        target = target / "style-studio.html"

    html = read_page("studio")
    for token, value in (
        ("__PAGES__", json.dumps(preview_pages())),
        ("__STYLES__", json.dumps(list(STYLE_NAMES))),
        ("__DECORS__", json.dumps(list(DECOR_NAMES))),
        (
            "__DEFAULTS__",
            json.dumps({s: list(default_decor(s)) for s in STYLE_NAMES}, ensure_ascii=False),
        ),
        ("__FPS__", str(max(10, min(60, int(fps))))),
    ):
        html = html.replace(token, value)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8")
    return target


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 老控制台不支持就按原样输出
        pass

    parser = argparse.ArgumentParser(
        prog="python -m gangge.layer4_tools.storyboard.studio",
        description="样式工作台：风格 / 装饰 / 画幅 / 帧率随手调，导出可直接拍片的配置。",
    )
    parser.add_argument(
        "-o", "--out", metavar="PATH", help="输出 HTML 或目录，默认 .gangge/style-studio.html"
    )
    parser.add_argument("--fps", type=int, default=30, help="初始帧率，10-60，默认 30")
    args = parser.parse_args()

    path = build_studio(args.out, fps=args.fps)
    print(f"工作台: {path.resolve()}")
    print(f"大小: {path.stat().st_size // 1024} KB")
    print("用浏览器打开就能调；导出的配置可直接贴进 demo_video 或命令行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
