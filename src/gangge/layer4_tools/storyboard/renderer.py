"""Deterministic frame capture.

The page exposes ``__seek(t)`` as a pure function of time, so capture is simply:
point Playwright at the file and screenshot one frame per tick. Nothing is timed
by the wall clock, which is why a slow machine and a fast one produce byte-similar
video — and why frames can be re-rendered individually when debugging.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

ProgressFn = Callable[[int, int], None]


class RenderError(RuntimeError):
    """Rendering cannot proceed (missing playwright, unreadable page, ...)."""


async def render_frames(
    html_path: Path,
    frames_dir: Path,
    *,
    fps: int,
    duration: float,
    width: int,
    height: int,
    on_progress: ProgressFn | None = None,
) -> int:
    """Screenshot ``duration * fps`` frames into ``frames_dir``; return the count."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise RenderError(
            "需要 playwright：pip install playwright && playwright install chromium"
        ) from e

    frames_dir.mkdir(parents=True, exist_ok=True)
    for stale in frames_dir.glob("*.png"):
        stale.unlink()

    total = max(1, int(round(duration * fps)))
    # 纯 file:// URL：给本地 URL 挂 ?query 在 Chromium 下会随机 net::ERR_FAILED，
    # 逐帧渲染跑到一半才失败代价太大。捕获标记改用 add_init_script 提前注入。
    url = html_path.resolve().as_uri()

    async with async_playwright() as pw:
        # 飞行页是 WebGL 场景：没有显卡的机器（以及新版 Chromium 默认关掉的
        # SwiftShader 回退）会画出一张空画面。强制走软件渲染——慢一点，但一定能出帧。
        # 纯 CSS 的几种页面不受影响。
        browser = await pw.chromium.launch(
            args=[
                "--hide-scrollbars",
                "--use-angle=swiftshader",
                "--enable-unsafe-swiftshader",
                "--ignore-gpu-blocklist",
            ]
        )
        try:
            page = await browser.new_page(
                viewport={"width": width, "height": height},
                device_scale_factor=1,
            )
            # 飞行页靠这个标记进入"被逐帧拍"模式：停掉播放器自播、固定像素比、
            # 隐藏控件，时间只由 __seek 驱动。其他页面不读它，无害。
            await page.add_init_script("window.__CAPTURE_MODE__=true")
            await page.goto(url, wait_until="load")
            # Without this, the first frames can be captured before the CJK
            # font resolves and the headline renders in a fallback face.
            await page.evaluate("() => (document.fonts ? document.fonts.ready : null)")
            # The page ships with a preview player; it must not appear in a frame.
            await page.evaluate("() => window.__hide_ui && window.__hide_ui()")
            # 展示页里还嵌着一层 iframe（srcdoc），它没就绪就 __seek 会拍到空镜。
            # 普通分镜页没有这个钩子，evaluate 立刻返回 true。
            # 飞行页的 __ready 是个函数，返回 READY Promise；别的页面可能直接挂
            # promise / 布尔。两种都要真等到场景 build 完再 __seek，否则拍到空场景。
            await page.evaluate(
                "() => (typeof window.__ready === 'function'"
                " ? window.__ready() : window.__ready)"
            )
            await page.wait_for_timeout(60)

            for i in range(total):
                await page.evaluate("(t) => window.__seek(t)", i / fps)
                shot = frames_dir / f"{i:05d}.png"
                # 软件渲染的长片里，Chromium 合成器偶尔会回一句
                # "Unable to capture screenshot"。这是瞬时故障，重试一下就好；
                # 不重试的话，900 帧渲到第 566 帧作废，整条片子得从头再来。
                for attempt in range(3):
                    try:
                        await page.screenshot(
                            path=str(shot), animations="disabled", caret="hide"
                        )
                        break
                    except Exception:  # noqa: BLE001 - Playwright 的协议错误种类不定
                        if attempt == 2:
                            raise
                        logger.warning("[storyboard] frame %d screenshot retry", i)
                        await page.wait_for_timeout(150)
                if on_progress is not None and (i % 40 == 0 or i == total - 1):
                    on_progress(i + 1, total)
        finally:
            await browser.close()

    logger.info("[storyboard] captured %d frames at %dfps", total, fps)
    return total
