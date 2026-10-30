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
    url = html_path.resolve().as_uri()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--hide-scrollbars"])
        try:
            page = await browser.new_page(
                viewport={"width": width, "height": height},
                device_scale_factor=1,
            )
            await page.goto(url, wait_until="load")
            # Without this, the first frames can be captured before the CJK
            # font resolves and the headline renders in a fallback face.
            await page.evaluate("() => (document.fonts ? document.fonts.ready : null)")
            # The page ships with a preview player; it must not appear in a frame.
            await page.evaluate("() => window.__hide_ui && window.__hide_ui()")
            await page.wait_for_timeout(60)

            for i in range(total):
                await page.evaluate("(t) => window.__seek(t)", i / fps)
                await page.screenshot(
                    path=str(frames_dir / f"{i:05d}.png"),
                    animations="disabled",
                    caret="hide",
                )
                if on_progress is not None and (i % 40 == 0 or i == total - 1):
                    on_progress(i + 1, total)
        finally:
            await browser.close()

    logger.info("[storyboard] captured %d frames at %dfps", total, fps)
    return total
