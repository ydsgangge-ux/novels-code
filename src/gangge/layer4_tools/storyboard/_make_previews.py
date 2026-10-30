"""一次性生成 skill 效果预览图（每个 style 一张 PNG）。

运行（仅开发期，需要 playwright）：

    python -m gangge.layer4_tools.storyboard._make_previews

产出写到 ``previews/<style>.png``，随包分发；运行时不会导入本模块，所以
没有 playwright / 浏览器也能正常使用界面——PNG 已经是死图。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from gangge.layer4_tools.storyboard.demo import title_only
from gangge.layer4_tools.storyboard.schema import STYLE_NAMES
from gangge.layer4_tools.storyboard.templates import render_page

PREVIEW_DIR = Path(__file__).resolve().parent / "previews"

#: 1920×1080 → 768×432：右侧面板最宽 600px，这个尺寸放大看也不糊，体积还可控。
SCALE = 0.4

#: 标题帧元素在 1.9s 前已全部落位（fade-up 0.14+0.65 / rule 0.56+0.55）。
SETTLE = 1.9


async def _shoot(html_files: dict[str, Path]) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--hide-scrollbars"])
        page = await browser.new_page(
            viewport={"width": 1920, "height": 1080}, device_scale_factor=SCALE
        )
        for style, path in html_files.items():
            await page.goto(path.resolve().as_uri(), wait_until="load")
            await page.evaluate("() => document.fonts.ready")
            await page.evaluate("() => window.__hide_ui()")
            scenes = await page.evaluate(
                "() => Array.prototype.map.call(document.querySelectorAll('.scene'),"
                " s => ({start: parseFloat(s.dataset.start),"
                "        end: parseFloat(s.dataset.end)}))"
            )
            start, end = scenes[0]["start"], scenes[0]["end"]
            await page.evaluate(
                "(t) => window.__seek(t)", min(start + SETTLE, end - 0.3)
            )
            await page.wait_for_timeout(60)
            await page.screenshot(path=str(PREVIEW_DIR / f"{style}.png"))
            print("preview:", style)
        await browser.close()


def main() -> int:
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    html_files: dict[str, Path] = {}
    tmp = Path(tempfile.mkdtemp(prefix="skill-preview-"))
    for style in STYLE_NAMES:
        sb = title_only(style)
        html = render_page(sb, sb.timeline())
        path = tmp / f"{style}.html"
        path.write_text(html, encoding="utf-8")
        html_files[style] = path
    asyncio.run(_shoot(html_files))
    for style in html_files:
        size = (PREVIEW_DIR / f"{style}.png").stat().st_size
        print(f"  {style}.png  {size // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
