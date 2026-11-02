"""Web tools — web search and fetch with usage controls."""

from __future__ import annotations

import asyncio
import os
import re
import time
import logging
from typing import Any
from urllib.parse import quote_plus, urljoin, urlparse

from gangge.layer4_tools import deepseek_web
from html import unescape as html_unescape

import aiohttp

from gangge.layer3_agent.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

# 检查 trafilatura 是否可用（高质量正文提取）
_TRAFILATURA_AVAILABLE = False
try:
    import trafilatura
    _TRAFILATURA_AVAILABLE = True
except ImportError:
    pass

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
    """Fetch content from a URL (static HTTP, no JS rendering).

    升级版：优先尝试 llms.txt → trafilatura 正文提取 → regex 退化方案。
    """

    def __init__(self, usage: UsageController | None = None):
        self._usage = usage or UsageController()

    @property
    def name(self) -> str:
        return "web_fetch"

    @property
    def description(self) -> str:
        return (
            "获取指定 URL 的网页正文并转换为纯文本。仅支持静态 HTML，不渲染 JavaScript。"
            "适用于查阅在线文档、API 参考、博客文章、技术文章等。"
            "✨ 优先尝试 llms.txt（为 LLM 准备的精简版）→ trafilatura 正文提取 → 退化方案。"
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

    async def _try_llms_txt(self, session: aiohttp.ClientSession, url: str) -> str:
        """尝试获取同域名下的 /llms.txt 或 /llms-full.txt。

        llms.txt 是为 LLM 准备的精简文档格式，质量通常优于正文提取。
        参考: https://llmstxt.org
        """
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return ""

        for path in ["/llms-full.txt", "/llms.txt"]:
            llms_url = f"{parsed.scheme}://{parsed.netloc}{path}"
            try:
                get_kwargs: dict[str, Any] = {
                    "url": llms_url,
                    "timeout": aiohttp.ClientTimeout(total=8),
                    "headers": {
                        "User-Agent": "Mozilla/5.0 GanggeBot/1.0",
                        "Accept": "text/plain,*/*",
                    },
                    _AIOHTTP_REDIRECT_KWARG: True,
                }
                async with session.get(**get_kwargs) as resp:
                    if resp.status == 200:
                        text = await resp.text(errors="replace")
                        if text and len(text.strip()) > 100:
                            logger.info("[web_fetch] 命中 llms.txt: %s", llms_url)
                            return text
            except Exception:
                continue
        return ""

    def _extract_with_trafilatura(self, html: str, url: str) -> str:
        """用 trafilatura 提取正文（质量高于 regex）。"""
        if not _TRAFILATURA_AVAILABLE:
            return ""
        try:
            # include_links=True 保留链接（文档场景有用）
            # include_tables=True 保留表格（API 参数表）
            extracted = trafilatura.extract(
                html,
                url=url,
                include_links=True,
                include_tables=True,
                include_images=False,
                output_format="txt",
                no_fallback=False,
            )
            return extracted or ""
        except Exception as e:
            logger.debug("[web_fetch] trafilatura 提取失败: %s", e)
            return ""

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

        # 0. 官方 Fetch：服务端抓取并转 Markdown。SPA、反爬站点也能拿到正文，
        #    比本地 urllib 抓 HTML 再猜正文稳得多。没配 Key 就静默跳过。
        note = ""
        if deepseek_web.available():
            try:
                page = await deepseek_web.fetch(url)
                content = (page.get("content") or "").strip()
                if content:
                    clipped = content[:max_length]
                    if len(content) > max_length:
                        clipped += f"\n\n... (内容已截断，原始共 {len(content)} 字符)"
                    title = f"[{page['title']}]\n" if page.get("title") else ""
                    return ToolResult(
                        output=(
                            f"[来源: {url} | via DeepSeek Fetch]\n{title}{clipped}\n"
                            f"[{self._usage.usage_summary()}]"
                        )
                    )
                note = "[官方 Fetch 没返回正文；已改用本地抓取]\n"
            except deepseek_web.WebApiError as e:
                note = f"[官方 Fetch 不可用：{e}；已改用本地抓取]\n"
            except Exception as e:  # noqa: BLE001 - 抓不到就退化，别让整次抓取失败
                note = f"[官方 Fetch 异常：{type(e).__name__}；已改用本地抓取]\n"

        result = await self._local_fetch(url, max_length)
        if note:
            result.output = note + result.output
        return result

    async def _local_fetch(self, url: str, max_length: int) -> ToolResult:
        try:
            async with aiohttp.ClientSession() as session:
                # 1. 优先尝试 llms.txt（为 LLM 准备的精简文档）
                llms_content = await self._try_llms_txt(session, url)
                if llms_content:
                    text = llms_content[:max_length]
                    if len(llms_content) > max_length:
                        text += f"\n\n... (llms.txt 已截断，原始共 {len(llms_content)} 字符)"
                    return ToolResult(
                        output=f"[来源: {url} | via llms.txt]\n{text}\n[{self._usage.usage_summary()}]"
                    )

                # 2. 常规 HTTP 抓取
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

                    raw_html = await resp.text(errors="replace")

                # 3. 用 trafilatura 提取正文（质量高于 regex）
                extracted = self._extract_with_trafilatura(raw_html, url)
                if extracted and len(extracted.strip()) > 100:
                    text = extracted[:max_length]
                    if len(extracted) > max_length:
                        text += f"\n\n... (内容已截断，原始共 {len(extracted)} 字符)"
                    source_tag = "trafilatura"
                else:
                    # 4. 退化方案：regex 清理
                    text = _strip_html(raw_html, max_length)
                    source_tag = "regex-fallback"

                text = f"[来源: {url} | via {source_tag}]\n{text}\n[{self._usage.usage_summary()}]"
                return ToolResult(output=text)
        except asyncio.TimeoutError:
            return ToolResult(output=f"请求超时 (15s): {url}", is_error=True)
        except Exception as e:
            return ToolResult(output=f"获取失败: {e}", is_error=True)


class WebSearchTool(BaseTool):
    """Search the web — DuckDuckGo API → Bing 抓取双引擎降级。

    优先使用 DuckDuckGo Instant Answer API（返回 JSON，稳定），
    失败时自动降级到 Bing 搜索（国内可访问，抓取 HTML）。
    """

    def __init__(self, usage: UsageController | None = None):
        self._usage = usage or UsageController()

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return (
            "搜索互联网获取最新信息。返回结果包含标题、摘要、链接，"
            "配了搜索 Key 时还带站点、发布时间和权威分。"
            "支持 site / lang / since / until 精确限定（site:github.com 这类语法）。"
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
                "site": {
                    "type": "string",
                    "description": "只搜这些站点，逗号分隔。例如 github.com,huggingface.co",
                },
                "lang": {
                    "type": "string",
                    "description": "限定语言，例如 zh 或 zh,en",
                },
                "since": {
                    "type": "string",
                    "description": "只看这之后发布的，例如 2025 或 2025-01-01",
                },
                "until": {
                    "type": "string",
                    "description": "只看这之前发布的，例如 2026-06-01",
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

        # site / lang / since / until 拼成搜索语法，两家引擎都认
        query = _apply_search_syntax(query, kwargs)

        errors: list[str] = []

        # 0. 官方 Search：一次拿走摘要、权威分、发布时间，模型才判断得出该信哪条。
        #    没有 Key（主模型不是 DeepSeek）就静默回退到免费通道。
        if deepseek_web.available():
            try:
                hits = await deepseek_web.search(
                    query,
                    count=max_results,
                    ori_question=str(kwargs.get("ori_question") or ""),
                )
                if hits:
                    return self._format_deepseek(query, hits)
                errors.append("DeepSeek: 无结果")
            except deepseek_web.WebApiError as e:
                errors.append(f"DeepSeek: {e}")
            except Exception as e:  # noqa: BLE001 - 接口异常不该让搜索整体失败
                errors.append(f"DeepSeek: {type(e).__name__}: {e}")

        return await self._free_search(query, max_results, errors)

    async def _free_search(
        self, query: str, max_results: int, errors: list[str] | None = None
    ) -> ToolResult:
        """免费通道：DDG API → DDG HTML → Bing，三层全挂才算失败。"""
        errors = list(errors or [])

        async with aiohttp.ClientSession() as session:
            # ── 第 1 层: DuckDuckGo Instant Answer API ──
            # 返回 JSON，稳定，但不保证有结果（对中文查询有时返回空）
            try:
                encoded = quote_plus(query)
                ddg_url = f"https://api.duckduckgo.com/?q={encoded}&format=json&no_html=1&skip_disambig=1"
                async with session.get(
                    ddg_url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        results = self._parse_ddg_api(data, max_results)
                        if results:
                            return self._format_results(query, results, "DuckDuckGo")
                    errors.append(f"DDG API: 无结果或 HTTP {resp.status}")
            except asyncio.TimeoutError:
                errors.append("DDG API 超时")
            except Exception as e:
                errors.append(f"DDG API: {e}")

            # ── 第 2 层: DuckDuckGo HTML 抓取 ──
            # 比 API 更完整（有 snippet），但可能被限流
            try:
                encoded = quote_plus(query)
                ddg_html_url = f"https://html.duckduckgo.com/html/?q={encoded}"
                ddg_html_kwargs: dict[str, Any] = {
                    "url": ddg_html_url,
                    "headers": {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 GanggeBot/1.0",
                        "Accept": "text/html",
                        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    },
                    "timeout": aiohttp.ClientTimeout(total=12),
                    _AIOHTTP_REDIRECT_KWARG: True,
                }
                async with session.get(**ddg_html_kwargs) as resp:
                    if resp.status == 200:
                        html = await resp.text(errors="replace")
                        results = _parse_ddg_results(html, max_results)
                        if results:
                            return self._format_results(query, results, "DuckDuckGo-HTML")
                errors.append("DDG HTML: 无结果")
            except asyncio.TimeoutError:
                errors.append("DDG HTML 超时")
            except Exception as e:
                errors.append(f"DDG HTML: {e}")

            # ── 第 3 层: Bing 搜索（国内可访问的兜底）──
            try:
                encoded = quote_plus(query)
                bing_url = f"https://www.bing.com/search?q={encoded}&count={max_results}"
                bing_kwargs: dict[str, Any] = {
                    "url": bing_url,
                    "headers": {
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0.0.0 Safari/537.36"
                        ),
                        "Accept-Language": "zh-CN,zh;q=0.9",
                    },
                    "timeout": aiohttp.ClientTimeout(total=12),
                    _AIOHTTP_REDIRECT_KWARG: True,
                }
                async with session.get(**bing_kwargs) as resp:
                    if resp.status == 200:
                        html = await resp.text(errors="replace")
                        results = self._parse_bing_results(html, max_results)
                        if results:
                            return self._format_results(query, results, "Bing")
                errors.append("Bing: 无结果")
            except asyncio.TimeoutError:
                errors.append("Bing 超时")
            except Exception as e:
                errors.append(f"Bing: {e}")

        # 三层全部失败
        hint = "" if deepseek_web.available() else "\n" + deepseek_web.missing_key_hint()
        return ToolResult(
            output=(
                f"搜索失败，三层引擎均不可用: {query}\n"
                f"尝试: {'; '.join(errors)}\n"
                f"建议: 1. 检查网络 2. 换英文关键词 3. 用 web_fetch 直接访问目标网页"
                f"{hint}"
            ),
            is_error=True,
        )

    def _parse_ddg_api(self, data: dict, max_results: int) -> list[dict[str, str]]:
        """解析 DuckDuckGo Instant Answer API 的 JSON 响应。"""
        results = []
        if data.get("AbstractText"):
            results.append({
                "title": data.get("Heading", "摘要"),
                "snippet": data["AbstractText"][:500],
                "url": data.get("AbstractURL", ""),
            })
        for topic in data.get("RelatedTopics", [])[:max_results]:
            if isinstance(topic, dict) and topic.get("Text"):
                results.append({
                    "title": topic.get("FirstURL", "").split("/")[-1].replace("_", " "),
                    "snippet": topic["Text"][:300],
                    "url": topic.get("FirstURL", ""),
                })
        return results[:max_results]

    def _parse_bing_results(self, html: str, max_results: int) -> list[dict[str, str]]:
        """解析 Bing 搜索结果 HTML。"""
        results = []
        titles = re.findall(r'<h2[^>]*><a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.DOTALL)
        snippets = re.findall(r'<p[^>]*class="[^"]*b_lineclamp[^"]*"[^>]*>(.*?)</p>', html, re.DOTALL)

        for i, (href, title) in enumerate(titles[:max_results]):
            title_clean = re.sub(r"<[^>]+>", "", title).strip()
            snippet_clean = ""
            if i < len(snippets):
                snippet_clean = re.sub(r"<[^>]+>", "", snippets[i]).strip()
            if title_clean and not href.startswith("javascript"):
                results.append({
                    "title": title_clean[:120],
                    "snippet": snippet_clean[:300],
                    "url": href,
                })
        return results

    def _format_results(self, query: str, results: list[dict], engine: str) -> ToolResult:
        """格式化搜索结果输出。"""
        lines = [f"搜索结果 ({len(results)} 条 | via {engine}): {query}\n"]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. [{r['title']}]({r['url']})")
            if r.get("snippet"):
                lines.append(f"   {r['snippet'][:200]}")
            lines.append("")
        lines.append(f"[{self._usage.usage_summary()}]")
        return ToolResult(output="\n".join(lines))

    def _format_deepseek(self, query: str, hits: list[dict[str, Any]]) -> ToolResult:
        """官方结果多带站点、时间、权威分——这几个字段决定了模型信不信它。"""
        lines = [f"搜索结果 ({len(hits)} 条 | via DeepSeek Search): {query}\n"]
        for i, hit in enumerate(hits, 1):
            title = str(hit.get("title") or "(无标题)")
            lines.append(f"{i}. [{title}]({hit.get('url')})")
            meta: list[str] = []
            if hit.get("site_name"):
                meta.append(str(hit["site_name"]))
            if hit.get("date"):
                meta.append(str(hit["date"])[:10])
            score = hit.get("rerank_score")
            if isinstance(score, (int, float)):
                meta.append(f"相关度 {score:.1f}")
            authority = hit.get("authority")
            if isinstance(authority, int) and authority >= 3:
                meta.append("高权威")
            if meta:
                lines.append("   " + " · ".join(meta))
            body = str(hit.get("summary") or hit.get("snippet") or "")
            if body:
                lines.append(f"   {body[:400]}")
            lines.append("")
        lines.append(f"[{self._usage.usage_summary()}]")
        return ToolResult(output="\n".join(lines), metadata={"engine": "deepseek"})


def _apply_search_syntax(query: str, kwargs: dict[str, Any]) -> str:
    """把 site / lang / since / until 拼成搜索语法，官方和免费通道都认。

    这几个是"能不能精确找"的分水岭：``site:github.com`` 比在结果里翻链接快得多。
    """
    parts: list[str] = []
    site = str(kwargs.get("site") or "").strip()
    if site:
        sites = [s.strip() for s in site.replace(";", ",").split(",") if s.strip()]
        if sites:
            parts.append(" OR ".join(f"site:{s}" for s in sites))
    lang = str(kwargs.get("lang") or "").strip()
    if lang:
        parts.append(f"lang:{lang}")
    since = str(kwargs.get("since") or "").strip()
    if since:
        parts.append(f"after:{since}")
    until = str(kwargs.get("until") or "").strip()
    if until:
        parts.append(f"before:{until}")
    return f"{query} {' '.join(parts)}".strip() if parts else query


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