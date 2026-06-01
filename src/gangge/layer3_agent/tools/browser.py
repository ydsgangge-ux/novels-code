"""Browser tool — Playwright-powered JS page rendering.

Requires: pip install playwright && playwright install chromium
If not installed, the tool reports unavailability gracefully.
"""

from __future__ import annotations

import asyncio
import re
import logging
from dataclasses import dataclass
from typing import Any
from html import unescape as html_unescape

from gangge.layer3_agent.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

_BROWSER_AVAILABLE = False
_playwright = None

try:
    from playwright.async_api import async_playwright as _async_playwright
    _BROWSER_AVAILABLE = True
except ImportError:
    pass


@dataclass
class _BrowserConfig:
    block_ads: bool = True
    block_trackers: bool = True
    max_wait_ms: int = 15000
    viewport_width: int = 1280
    viewport_height: int = 800

    _block_patterns: tuple = (
        "google-analytics.com", "googletagmanager.com", "doubleclick.net",
        "facebook.net", "hotjar.com", "newrelic.com", "scorecardresearch.com",
        "*analytics*", "*tracker*", "*pixel*",
    )


def _is_unavailable() -> str:
    if not _BROWSER_AVAILABLE:
        return "Playwright 未安装。运行: pip install playwright && playwright install chromium"
    return ""


def _extract_main_text(html: str) -> str:
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<noscript[^>]*>.*?</noscript>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<nav[^>]*>.*?</nav>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<footer[^>]*>.*?</footer>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<header[^>]*>.*?</header>", "", text, flags=re.DOTALL | re.IGNORECASE)

    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</?p[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</?(div|section|article|li|h[1-6]|tr|blockquote|pre)[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_unescape(text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = text.strip()
    return text


def _build_block_script(config: _BrowserConfig) -> str:
    patterns = ",".join(f'"{p}"' for p in config._block_patterns)
    return f"""
    const blockPatterns = [{patterns}];
    const origFetch = window.fetch;
    window.fetch = function(...args) {{
        const url = typeof args[0] === 'string' ? args[0] : args[0]?.url || '';
        if (blockPatterns.some(p => url.includes(p.replace(/\\*/g, '')))) {{
            return Promise.reject(new Error('Blocked'));
        }}
        return origFetch.apply(this, args);
    }};
    """


class BrowserTool(BaseTool):
    """Open a URL with a real browser (Playwright), render JavaScript, and extract text.

    Use ONLY when web_fetch fails (JS-rendered content).
    This tool is expensive (resource + token cost).
    """

    def __init__(self, usage=None):
        from gangge.layer3_agent.tools.web import UsageController
        self._usage = usage or UsageController()

    @property
    def name(self) -> str:
        return "browser"

    @property
    def description(self) -> str:
        return (
            "使用真实浏览器（Playwright）打开 URL，渲染 JavaScript 后提取页面文本。"
            "⚠️ 仅在 web_fetch 无法获取内容时使用（如 React/Vue SPA、动态加载页面）。"
            "⚠️ 本工具资源消耗大，有严格调用次数限制，请谨慎使用。"
            "参数 selector 可指定 CSS 选择器只提取页面中某一部分内容（如 '.main-content'、'#article'）。"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要打开的网页 URL",
                },
                "selector": {
                    "type": "string",
                    "description": "CSS 选择器，只提取匹配元素的内容。例如: 'article'、'.content'、'#main'。不填则提取整个页面。",
                },
                "wait_seconds": {
                    "type": "integer",
                    "description": "页面加载后额外等待的秒数（用于等待动态内容），默认 2，最大 8",
                    "default": 2,
                },
                "max_chars": {
                    "type": "integer",
                    "description": "返回文本的最大字符数，默认 8000，最大 20000",
                    "default": 8000,
                },
            },
            "required": ["url"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        unavailable = _is_unavailable()
        if unavailable:
            return ToolResult(output=unavailable, is_error=True)

        url = kwargs["url"].strip()
        selector = kwargs.get("selector", "").strip()
        wait_seconds = min(kwargs.get("wait_seconds", 2), 8)
        max_chars = min(kwargs.get("max_chars", 8000), 20000)

        ok, msg = self._usage.check_browser_call()
        if not ok:
            return ToolResult(output=msg, is_error=True)

        config = _BrowserConfig()

        try:
            async with _async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                    ],
                )
                context = await browser.new_context(
                    viewport={"width": config.viewport_width, "height": config.viewport_height},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/125.0.0.0 Safari/537.36"
                    ),
                )
                page = await context.new_page()

                if config.block_ads:
                    await page.route("**/*", _handle_route_block)

                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=config.max_wait_ms)

                    if config.block_ads:
                        await page.evaluate(_build_block_script(config))

                    await asyncio.sleep(wait_seconds)

                    try:
                        await page.wait_for_load_state("networkidle", timeout=5000)
                    except Exception:
                        pass

                    if selector:
                        try:
                            element = await page.wait_for_selector(selector, timeout=5000)
                            if element:
                                html = await element.inner_html()
                            else:
                                html = await page.content()
                                selector = ""
                        except Exception:
                            html = await page.content()
                            selector = ""
                    else:
                        html = await page.content()

                finally:
                    await browser.close()

            text = _extract_main_text(html)

            if selector:
                text = f"[提取区域: {selector}]\n{text}"

            if len(text) > max_chars:
                text = text[:max_chars] + f"\n\n... (内容已截断，原始共 {len(text)} 字符)"

            text = f"[来源: {url} | 浏览器渲染]\n{text}\n[{self._usage.usage_summary()}]"
            return ToolResult(output=text)

        except asyncio.TimeoutError:
            return ToolResult(output=f"页面加载超时: {url}", is_error=True)
        except Exception as e:
            logger.warning(f"Browser tool failed: {e}")
            return ToolResult(
                output=f"浏览器操作失败: {e}\n提示: 请改用 web_fetch 尝试获取该页面",
                is_error=True,
            )


async def _handle_route_block(route):
    url = route.request.url.lower()
    if any(
        p.replace("*", "") in url
        for p in _BrowserConfig._block_patterns
    ):
        await route.abort()
    else:
        await route.continue_()