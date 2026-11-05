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


def _portrait(style: str = "neon") -> Storyboard:
    sb = title_only(style)
    sb.width, sb.height = 1080, 1920
    return sb


def _site(style: str = "neon") -> Storyboard:
    sb = title_only(style)
    sb.width, sb.height = 1920, 1200  # 16:10 更像一块屏幕
    return sb


def _shots() -> list[Shot]:
    shots = [
        # 1920×1080 → 768×432：右侧面板最宽 600px，这个尺寸放大看也不糊
        Shot(style, title_only(style), 1920, 1080, 0.4) for style in STYLE_NAMES
    ]
    shots.append(Shot("portrait", _portrait(), 1080, 1920, 0.5))
    shots.append(Shot("showcase", demo_storyboard("neon"), 1920, 1080, 0.4, page="showcase"))
    shots.append(Shot("site", _site(), 1920, 1200, 0.36, page="site"))
    return shots


def _html_for(shot: Shot) -> str:
    """按页面类型渲染预览用的 HTML。"""
    sb = shot.storyboard
    slots = sb.timeline()
    if shot.page == "storyboard":
        return render_page(sb, slots)

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
        browser = await pw.chromium.launch(args=["--hide-scrollbars"])
        for shot in shots:
            page = await browser.new_page(
                viewport={"width": shot.width, "height": shot.height},
                device_scale_factor=shot.scale,
            )
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
                # 展示页：等内页就绪再定位，否则拍到的是空框
                await page.evaluate("() => window.__ready")
                await page.evaluate("() => window.__hide_ui()")
                await page.evaluate("(t) => window.__seek(t)", SHOWCASE_AT)
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
