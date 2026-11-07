"""一次性生成 skill 效果预览图（每种风格一张，外加竖屏 / 3D 展示页 / 网站页）。

运行（仅开发期，需要 playwright）：

    python -m gangge.layer4_tools.storyboard._make_previews

产出写到 ``previews/<name>.png``，随包分发；运行时不会导入本模块，所以
没有 playwright / 浏览器也能正常使用界面——PNG 已经是死图。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from gangge.layer4_tools.storyboard.demo import demo_storyboard, title_only
from gangge.layer4_tools.storyboard.schema import STYLE_NAMES, Storyboard
from gangge.layer4_tools.storyboard.templates import render_page

PREVIEW_DIR = Path(__file__).resolve().parent / "previews"

#: 标题帧元素在 1.9s 前已全部落位（fade-up 0.14+0.65 / rule 0.56+0.55）。
SETTLE = 1.9
#: 展示页截图用的时间点：第一镜头已落位、机位刚推近一点，最像"正在介绍"。
SHOWCASE_AT = 1.5
#: 网站页预览里那张"截图"用的是自己的分镜页——离线、可复现，不依赖外网站点。
SITE_DEMO_URL = "https://github.com/ydsgangge-ux/novels-code"


@dataclass(frozen=True)
class Shot:
    """一张预览图：怎么渲染、按多大出图。"""

    name: str
    storyboard: Storyboard
    width: int
    height: int
    scale: float
    page: str = "storyboard"
    #: 截图取哪一刻。飞行页要停在某个站点上才好看，所以这一刻是按分镜算出来的。
    at: float = SHOWCASE_AT


def _portrait(style: str = "neon") -> Storyboard:
    sb = title_only(style)
    sb.width, sb.height = 1080, 1920
    return sb


def _site(style: str = "neon") -> Storyboard:
    sb = title_only(style)
    sb.width, sb.height = 1920, 1200  # 16:10 更像一块屏幕
    return sb


def _journey() -> Storyboard:
    """飞行页 / 沙盘页预览用的分镜：四个站点，正好是一段"历程"的样子。"""
    return Storyboard.from_dict(
        {
            "title": "从一间小屋到一座城",
            "subtitle": "十年，一直在往前走",
            "style": "dark",
            "scenes": [
                {
                    "template": "title",
                    "title": "从一间小屋到一座城",
                    "text": "十年，一直在往前走",
                    "voiceover": "十年前，我们从山脚下的一间小屋出发。",
                },
                {
                    "template": "quote",
                    "year": "2015",
                    "title": "山脚下的小屋",
                    "text": "一台旧电脑，一个想把事情做好的念头。",
                    "voiceover": "那时候只有一台旧电脑，和一个想把事情做好的念头。",
                },
                {
                    "template": "quote",
                    "year": "2018",
                    "title": "搬进镇上",
                    "text": "八个人，一间带院子的小楼。",
                    "voiceover": "三年后我们搬进镇上一间带院子的小楼，团队有了八个人。",
                },
                {
                    "template": "quote",
                    "year": "2021",
                    "title": "走进城市",
                    "text": "第一次带着作品走进大城市。",
                    "voiceover": "第一次带着作品走进大城市，站台很小，来看的人很多。",
                },
                {
                    "template": "quote",
                    "year": "2025",
                    "title": "在一座城里",
                    "text": "窗外是高楼，窗内还是那群人。",
                    "voiceover": "现在我们在一座城里，窗外是高楼，窗内还是那群人。",
                },
                {
                    "template": "outro",
                    "title": "",
                    "text": "路还长，继续走。",
                    "voiceover": "路还长，我们继续往前走。",
                },
            ],
        }
    )


def _station_at(sb: Storyboard, index: int = 1) -> float:
    """分镜里第 ``index`` 个镜头的正中间——停站推近的那一刻。"""
    slots = sb.timeline()
    scene, start, end = slots[min(index, len(slots) - 1)]
    return round((start + end) / 2, 2)


def _shots() -> list[Shot]:
    shots = [
        # 1920×1080 → 768×432：右侧面板最宽 600px，这个尺寸放大看也不糊
        Shot(style, title_only(style), 1920, 1080, 0.4) for style in STYLE_NAMES
    ]
    shots.append(Shot("portrait", _portrait(), 1080, 1920, 0.5))
    shots.append(Shot("showcase", demo_storyboard("neon"), 1920, 1080, 0.4, page="showcase"))
    shots.append(Shot("site", _site(), 1920, 1200, 0.36, page="site"))
    for page in ("flight", "miniature"):
        journey = _journey()
        shots.append(
            Shot(page, journey, 1920, 1080, 0.4, page=page, at=_station_at(journey, 2))
        )
    return shots


def _html_for(shot: Shot) -> str:
    """按页面类型渲染预览用的 HTML。"""
    sb = shot.storyboard
    slots = sb.timeline()
    if shot.page == "storyboard":
        return render_page(sb, slots)

    if shot.page in ("flight", "miniature"):
        from gangge.layer4_tools.storyboard.flight import render_flight

        return render_flight(sb, slots, page=shot.page)

    from gangge.layer4_tools.storyboard.showcase import render_showcase

    if shot.page == "site":
        inner_sb = demo_storyboard("neon")
        return render_showcase(
            sb,
            slots,
            source_url=SITE_DEMO_URL,
            inner_html=render_page(inner_sb, inner_sb.timeline()),
            scroll_vh=4.55,
        )
    return render_showcase(sb, slots, source_url=SITE_DEMO_URL)


async def _shoot(shots: list[Shot], html_files: dict[str, Path]) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        # 飞行页是 WebGL 场景：没有显卡的机器要靠 SwiftShader 软件渲染，否则拍到空画面
        browser = await pw.chromium.launch(
            args=[
                "--hide-scrollbars",
                "--use-angle=swiftshader",
                "--enable-unsafe-swiftshader",
                "--ignore-gpu-blocklist",
            ]
        )
        for shot in shots:
            page = await browser.new_page(
                viewport={"width": shot.width, "height": shot.height},
                device_scale_factor=shot.scale,
            )
            # 飞行页认这个标记：关掉自带播放循环，只由 __seek 驱动。
            # 别往 file:// URL 上挂 query，Chromium 会随机 net::ERR_FAILED。
            if shot.page in ("flight", "miniature"):
                await page.add_init_script("window.__CAPTURE_MODE__=true")
            await page.goto(html_files[shot.name].resolve().as_uri(), wait_until="load")
            await page.evaluate("() => document.fonts.ready")
            if shot.page == "storyboard":
                await page.evaluate("() => window.__hide_ui()")
                scenes = await page.evaluate(
                    "() => Array.prototype.map.call(document.querySelectorAll('.scene'),"
                    " s => ({start: parseFloat(s.dataset.start),"
                    "        end: parseFloat(s.dataset.end)}))"
                )
                start, end = scenes[0]["start"], scenes[0]["end"]
                await page.evaluate("(t) => window.__seek(t)", min(start + SETTLE, end - 0.3))
            else:
                # 展示页等内页就绪；飞行页的 __ready 是函数，要调用才拿到 Promise
                if shot.page in ("flight", "miniature"):
                    await page.evaluate("() => window.__ready()")
                else:
                    await page.evaluate("() => window.__ready")
                await page.evaluate("() => window.__hide_ui()")
                await page.evaluate("(t) => window.__seek(t)", shot.at)
            await page.wait_for_timeout(60)
            await page.screenshot(
                path=str(PREVIEW_DIR / f"{shot.name}.png"),
                animations="disabled",
                caret="hide",
                timeout=60000,
            )
            await page.close()
            print("preview:", shot.name)
        await browser.close()


def main() -> int:
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    shots = _shots()
    html_files: dict[str, Path] = {}
    tmp = Path(tempfile.mkdtemp(prefix="skill-preview-"))
    for shot in shots:
        html_files[shot.name] = tmp / f"{shot.name}.html"
        html_files[shot.name].write_text(_html_for(shot), encoding="utf-8")
    asyncio.run(_shoot(shots, html_files))
    for shot in shots:
        size = (PREVIEW_DIR / f"{shot.name}.png").stat().st_size
        print(f"  {shot.name}.png  {size // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
