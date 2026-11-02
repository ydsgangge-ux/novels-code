"""DeepSeek 官方 Search / Fetch：把"搜网页"和"读网页"换成服务端接口。

自建那套（DDG + Bing 扒 HTML）有两个老毛病：国内经常一条结果都搜不到；拿不到
摘要、权威分、发布时间，模型没法判断该信哪条。官方接口按次计费（5 元 / 1000 次
成功请求），返回的是结构化结果和干净的 Markdown。

Key 的来路，按优先级：

1. ``WEB_SEARCH_API_KEY`` —— 搜索专用。主模型用 Ollama / Claude / OpenAI 时填它；
2. ``DEEPSEEK_API_KEY``   —— 主模型本来就是 DeepSeek 的话，零配置直接生效；
3. 都没有 —— :func:`available` 返回 False，调用方回退到免费通道，不报错。

``WEB_SEARCH_BACKEND=free`` 可以强制只用免费通道（比如想省这点钱，或者在
内网环境里根本连不上）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

#: 官方默认域名；自建代理可以通过 ``WEB_SEARCH_BASE_URL`` 改。
DEFAULT_BASE_URL = "https://api.deepseek.com"
SEARCH_PATH = "/v1/tools/search"
FETCH_PATH = "/v1/tools/fetch"

#: 单次请求超时（秒）。Search 是同步返回的，20 秒足够；Fetch 要等对方站点渲染。
SEARCH_TIMEOUT = 20.0
FETCH_TIMEOUT = 30.0
#: 408 / 429 / 5xx 会重试，最多再试这么多次。
RETRIES = 1


class WebApiError(RuntimeError):
    """接口层错误，文案直接给模型看。"""


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def api_key() -> str:
    """按优先级取 Key：搜索专用 > 主模型的 DeepSeek Key。"""
    return _env("WEB_SEARCH_API_KEY") or _env("DEEPSEEK_API_KEY")


def backend() -> str:
    """``auto``（默认）/ ``deepseek``（强制）/ ``free``（永不调用接口）。"""
    return _env("WEB_SEARCH_BACKEND").lower() or "auto"


def base_url() -> str:
    raw = _env("WEB_SEARCH_BASE_URL") or _env("DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL
    raw = raw.rstrip("/")
    # DEEPSEEK_BASE_URL 习惯写成 https://api.deepseek.com/v1，这里要退回根路径
    if raw.endswith("/v1"):
        raw = raw[:-3]
    return raw


def available() -> bool:
    """能不能走官方接口。没有 Key 或强制 free 时返回 False（调用方回退）。"""
    return backend() != "free" and bool(api_key())


def missing_key_hint() -> str:
    """给"没配 Key"时的提示，出现在工具输出里，告诉人到底该填哪。"""
    return (
        "未配置联网搜索 Key，已回退到免费的 DuckDuckGo / Bing 通道。"
        "想要更稳的搜索和干净的正文：在桌面端「设置 → 联网搜索」填一个 DeepSeek Key"
        "（或写 WEB_SEARCH_API_KEY=... 到 .env），主模型用 DeepSeek 时它会自动复用。"
    )


def _explain(status: int, body: str) -> str:
    """把 HTTP 错误翻译成"该怎么办"。"""
    detail = ""
    try:
        payload = json.loads(body)
        detail = str((payload.get("error") or {}).get("message") or "")
    except (ValueError, AttributeError):
        detail = body[:200]

    if status == 401:
        return "联网搜索 Key 无效或缺失（检查 WEB_SEARCH_API_KEY / DEEPSEEK_API_KEY）"
    if status == 402:
        return "DeepSeek 账户余额不足，联网搜索已停用（充值后自动恢复）"
    if status == 403:
        return "Fetch 被目标站点的安全策略拒绝"
    if status == 404:
        return "抓不到可用的正文：地址不可用、被站点拦截，或页面没有可提取的内容"
    if status == 413:
        return "请求体过大"
    suffix = f"：{detail}" if detail else ""
    return f"HTTP {status}{suffix}"


async def _post(
    path: str,
    payload: dict[str, Any],
    *,
    timeout: float,
    retries: int = RETRIES,
) -> dict[str, Any]:
    try:
        import aiohttp
    except ImportError as e:  # pragma: no cover - aiohttp 是既有依赖
        raise WebApiError("缺少 aiohttp 依赖，无法调用联网接口") from e

    key = api_key()
    if not key:
        raise WebApiError("未配置联网搜索 Key")

    url = f"{base_url()}{path}"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    last_error = ""
    for attempt in range(retries + 1):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    headers=headers,
                    data=json.dumps(payload),
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    body = await resp.text(errors="replace")
                    if resp.status == 200:
                        return json.loads(body)
                    if resp.status in (408, 429) or resp.status >= 500:
                        last_error = _explain(resp.status, body)
                        if attempt < retries:
                            await asyncio.sleep(1.2 * (attempt + 1))
                            continue
                        raise WebApiError(f"{last_error}（已重试 {retries} 次）")
                    raise WebApiError(_explain(resp.status, body))
        except asyncio.TimeoutError:
            last_error = f"请求超时（{timeout:.0f}s）"
            if attempt < retries:
                continue
        except WebApiError:
            raise
        except Exception as e:  # noqa: BLE001 - 网络层异常统一成可读文案
            last_error = f"{type(e).__name__}: {e}"
            if attempt < retries:
                await asyncio.sleep(1.0)
                continue
    raise WebApiError(last_error or "联网搜索请求失败")


async def search(
    query: str,
    *,
    count: int = 5,
    need_content: bool = False,
    ori_question: str = "",
) -> list[dict[str, Any]]:
    """搜索网页。返回结构化结果；无结果时返回空列表（不算错误）。"""
    payload: dict[str, Any] = {
        "query": query,
        "count": max(1, min(50, int(count))),
        "need_content": bool(need_content),
    }
    if ori_question:
        payload["ori_question"] = ori_question
    data = await _post(SEARCH_PATH, payload, timeout=SEARCH_TIMEOUT)
    return [hit for hit in (data.get("results") or []) if hit.get("url")]


async def fetch(url: str) -> dict[str, str]:
    """抓单个网页，返回 ``{"title", "content"}``，content 是 Markdown。"""
    target = (url or "").strip()
    if not target:
        raise WebApiError("缺少要抓取的 URL")
    data = await _post(FETCH_PATH, {"url": target}, timeout=FETCH_TIMEOUT)
    return {
        "title": str(data.get("title") or ""),
        "content": str(data.get("content") or ""),
    }
