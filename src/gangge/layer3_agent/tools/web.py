"""Web tools — web search and fetch with usage controls."""

from __future__ import annotations

import asyncio
import os
import re
import time
import logging
from typing import Any
from urllib.parse import quote_plus
from html import unescape as html_unescape

import aiohttp

from gangge.layer3_agent.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

_AIOHTTP_REDIRECT_KWARG = "allow_redirects"
try:
    import inspect
    sig = inspect.signature(aiohttp.ClientSession._request)
    if "follow_redirects" in sig.parameters:
        _AIOHTTP_REDIRECT_KWARG = "follow_redirects"
except Exception:
    pass


class UsageController:
    """Tracks and limits web tool calls to prevent token waste.

    Enforces:
      - Max total web calls per task (search + fetch share quota)
      - Dedicated browser call limit
      - Minimum cooldown between calls
      - Configurable via environment variables
    """

    def __init__(self):
        self.max_web_calls = int(os.environ.get("GANGGE_MAX_WEB_CALLS", "8"))
        self.max_browser_calls = int(os.environ.get("GANGGE_MAX_BROWSER_CALLS", "3"))
        self.min_cooldown = float(os.environ.get("GANGGE_WEB_COOLDOWN_SEC", "1.5"))
        self.max_result_chars = int(os.environ.get("GANGGE_WEB_MAX_RESULT_CHARS", "8000"))
        self._web_count = 0
        self._browser_count = 0
        self._last_call_time = 0.0

    def check_web_call(self) -> tuple[bool, str]:
        self._web_count += 1
        if self._web_count > self.max_web_calls:
            return False, (
                f"已达到本次任务网页调用上限 ({self.max_web_calls}次)。"
                f"请基于已获取的信息继续工作，不要再搜索或抓取网页。"
            )
        now = time.monotonic()
        elapsed = now - self._last_call_time
        if self._last_call_time > 0 and elapsed < self.min_cooldown:
            return False, f"调用间隔过短 ({(elapsed):.1f}s)，请等待 {self.min_cooldown - elapsed:.0f}s 后再试"
        self._last_call_time = now
        return True, ""

    def check_browser_call(self) -> tuple[bool, str]:
        self._browser_count += 1
        if self._browser_count > self.max_browser_calls:
            return False, (
                f"已达到本次任务浏览器调用上限 ({self.max_browser_calls}次)。"
                f"浏览器操作消耗大，请用 web_fetch 或基于已有信息继续。"
            )
        now = time.monotonic()
        elapsed = now - self._last_call_time
        if self._last_call_time > 0 and elapsed < self.min_cooldown:
            return False, f"调用间隔过短 ({(elapsed):.1f}s)，请等待 {self.min_cooldown - elapsed:.0f}s 后再试"
        self._last_call_time = now
        return True, ""

    def usage_summary(self) -> str:
        return (
            f"网页调用: {self._web_count}/{self.max_web_calls} | "
            f"浏览器调用: {self._browser_count}/{self.max_browser_calls}"
        )


def _strip_html(text: str, max_length: int = 8000) -> str:
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<noscript[^>]*>.*?</noscript>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</?p[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</?(div|section|article|li|h[1-6]|tr)[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = html_unescape(text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = text.strip()
    if len(text) > max_length:
        text = text[:max_length] + f"\n\n... (内容已截断，原始共 {len(text)} 字符)"
    return text


class WebFetchTool(BaseTool):
    """Fetch content from a URL (static HTTP, no JS rendering)."""

    def __init__(self, usage: UsageController | None = None):
        self._usage = usage or UsageController()

    @property
    def name(self) -> str:
        return "web_fetch"

    @property
    def description(self) -> str:
        return (
            "获取指定 URL 的网页内容并转换为纯文本。仅支持静态 HTML，不渲染 JavaScript。"
            "适用于查阅在线文档、API 参考、博客文章、技术文章等。"
            "⚠️ 优先使用此工具，仅在静态抓取无法获取内容时才考虑 browser。"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要获取的 URL",
                },
                "max_length": {
                    "type": "integer",
                    "description": "返回内容的最大字符数，默认 6000，最大 16000",
                    "default": 6000,
                },
            },
            "required": ["url"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        url = (kwargs.get("url") or kwargs.get("link") or "").strip()
        max_length = min(kwargs.get("max_length", 6000), 16000)

        if not url:
            return ToolResult(
                output=f"❌ web_fetch 缺少 URL 参数。收到的参数: {list(kwargs.keys())}。请使用 url=\"https://...\"。",
                is_error=True,
            )

        ok, msg = self._usage.check_web_call()
        if not ok:
            return ToolResult(output=msg, is_error=True)

        try:
            async with aiohttp.ClientSession() as session:
                get_kwargs: dict[str, Any] = {
                    "url": url,
                    "timeout": aiohttp.ClientTimeout(total=15),
                    "headers": {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 GanggeBot/1.0",
                        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
                        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    },
                    _AIOHTTP_REDIRECT_KWARG: True,
                }
                async with session.get(**get_kwargs) as resp:
                    if resp.status != 200:
                        return ToolResult(
                            output=f"HTTP {resp.status}: {url}\n提示: 如果这是 JS 渲染页面，请用 browser 工具打开",
                            is_error=True,
                        )
                    content_type = resp.headers.get("Content-Type", "")
                    if "text/html" not in content_type and "text/plain" not in content_type:
                        return ToolResult(
                            output=f"不支持的内容类型: {content_type}\n提示: 对于 PDF/图片等，请直接告诉用户无法解析",
                            is_error=True,
                        )

                    text = await resp.text(errors="replace")
                    text = _strip_html(text, max_length)
                    text = f"[来源: {url}]\n{text}\n[{self._usage.usage_summary()}]"
                    return ToolResult(output=text)
        except asyncio.TimeoutError:
            return ToolResult(output=f"请求超时 (15s): {url}", is_error=True)
        except Exception as e:
            return ToolResult(output=f"获取失败: {e}", is_error=True)


class WebSearchTool(BaseTool):
    """Search the web using DuckDuckGo (free, no API key required)."""

    def __init__(self, usage: UsageController | None = None):
        self._usage = usage or UsageController()

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return (
            "使用 DuckDuckGo 搜索互联网获取最新信息。返回结果包含标题、摘要和链接。"
            "用于查找技术文档、解决方案、最新资讯等需要联网搜索的场景。"
            "⚠️ 先搜再取: web_search 找到链接后，用 web_fetch 获取详细内容。"
            "⚠️ 搜索短语要精确、具体，避免过于宽泛的查询。"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索查询关键词，尽量精确、具体。例如: 'Python asyncio timeout error solution' 而不是 'python error'",
                },
                "max_results": {
                    "type": "integer",
                    "description": "最大结果数，默认 5，最大 10",
                    "default": 5,
                },
            },
            "required": ["query"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        query = (kwargs.get("query") or kwargs.get("q") or kwargs.get("search") or kwargs.get("keyword") or "").strip()
        max_results = min(kwargs.get("max_results", 5), 10)

        if not query:
            return ToolResult(
                output=f"❌ web_search 缺少搜索关键词。收到的参数: {list(kwargs.keys())}。请使用 query=\"搜索内容\"。",
                is_error=True,
            )

        ok, msg = self._usage.check_web_call()
        if not ok:
            return ToolResult(output=msg, is_error=True)

        encoded = quote_plus(query)
        search_url = f"https://html.duckduckgo.com/html/?q={encoded}"

        try:
            async with aiohttp.ClientSession() as session:
                get_kwargs: dict[str, Any] = {
                    "url": search_url,
                    "timeout": aiohttp.ClientTimeout(total=12),
                    "headers": {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 GanggeBot/1.0",
                        "Accept": "text/html",
                        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    },
                    _AIOHTTP_REDIRECT_KWARG: True,
                }
                async with session.get(**get_kwargs) as resp:
                    if resp.status != 200:
                        return ToolResult(
                            output=f"搜索请求失败 HTTP {resp.status}。请稍后重试或尝试其他关键词。",
                            is_error=True,
                        )
                    html = await resp.text(errors="replace")

                results = _parse_ddg_results(html, max_results)

                if not results:
                    return ToolResult(
                        output=f"未找到与 '{query}' 相关的结果。请尝试:\n"
                        "1. 使用更通用的关键词\n2. 减少查询中的特殊符号\n3. 换成英文关键词重试",
                        is_error=True,
                    )

                lines = [f"搜索结果 ({len(results)} 条): {query}\n"]
                for i, r in enumerate(results, 1):
                    lines.append(f"{i}. [{r['title']}]({r['url']})")
                    if r.get("snippet"):
                        lines.append(f"   {r['snippet'][:200]}")
                    lines.append("")

                lines.append(f"[{self._usage.usage_summary()}]")
                return ToolResult(output="\n".join(lines))

        except asyncio.TimeoutError:
            return ToolResult(output="搜索请求超时，请稍后重试", is_error=True)
        except Exception as e:
            logger.warning(f"DuckDuckGo search failed: {e}")
            return ToolResult(
                output=f"搜索失败: {e}\n提示: 如果持续失败，可能被限流。请等几秒再试。",
                is_error=True,
            )


def _parse_ddg_results(html: str, max_results: int) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []

    result_blocks = re.split(r'<div\s+class="[^"]*result[^"]*"[^>]*>', html, flags=re.IGNORECASE)
    if len(result_blocks) <= 1:
        result_blocks = re.split(r'<div\s+class="[^"]*web-result[^"]*"[^>]*>', html, flags=re.IGNORECASE)

    if len(result_blocks) <= 1:
        result_blocks = re.split(r'<a\s+rel="nofollow"\s+class="result__a"', html)
        for block in result_blocks[1:]:
            if len(results) >= max_results:
                break
            link_match = re.search(r'href="([^"]+)"', block)
            text = _strip_html(block, 300).strip()
            if link_match:
                url = html_unescape(link_match.group(1))
                title = text.split("\n")[0].strip() if text else url
                snippet = text[len(title):].strip()[:200] if len(text) > len(title) else ""
                results.append({"title": title[:120], "url": url, "snippet": snippet})
        return results

    for block in result_blocks[1:]:
        if len(results) >= max_results:
            break

        link_match = re.search(r'<a[^>]*href="([^"]+)"[^>]*class="[^"]*result__a[^"]*"[^>]*>(.*?)</a>', block, re.DOTALL | re.IGNORECASE)
        if not link_match:
            link_match = re.search(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL)

        snippet_match = re.search(r'<a[^>]*class="[^"]*snippet[^"]*"[^>]*>(.*?)</a>', block, re.DOTALL | re.IGNORECASE)
        if not snippet_match:
            snippet_match = re.search(r'<td\s+class="[^"]*"?>\s*(.*?)\s*</td>', block, re.DOTALL)

        if link_match:
            url = html_unescape(link_match.group(1).strip())
            if url.startswith("//"):
                url = "https:" + url

            title_raw = link_match.group(2).strip()
            title = _strip_html(title_raw, 200).strip()
            if not title:
                title = url

            snippet = ""
            if snippet_match:
                snippet = _strip_html(snippet_match.group(1), 300).strip()

            results.append({"title": title[:120], "url": url, "snippet": snippet[:200]})

    return results