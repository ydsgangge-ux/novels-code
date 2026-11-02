"""工具层接入：官方接口优先，没有 Key 或调用失败就静默回退。

重点守两条：
1. 主模型不是 DeepSeek、没配搜索 Key 时，web_search / web_fetch 必须照常可用；
2. 官方接口报错不能让整次搜索失败，只能降级。
"""

from __future__ import annotations

import pytest

from gangge.layer3_agent.tools import web as web_module
from gangge.layer3_agent.tools.base import ToolResult
from gangge.layer3_agent.tools.web import WebFetchTool, WebSearchTool
from gangge.layer4_tools import deepseek_web


@pytest.fixture(autouse=True)
def _no_quota(monkeypatch):
    """配额是给真实用户防刷的，测试里别挡路。"""
    monkeypatch.setattr(
        web_module.UsageController, "check_web_call", lambda self: (True, "")
    )


# ── 搜索语法 ────────────────────────────────────────────────

def test_search_syntax_is_appended_to_the_query():
    query = web_module._apply_search_syntax(
        "asyncio timeout",
        {"site": "github.com,huggingface.co", "lang": "en", "since": "2025", "until": "2026-06"},
    )

    assert query.startswith("asyncio timeout ")
    assert "site:github.com OR site:huggingface.co" in query
    assert "lang:en" in query
    assert "after:2025" in query
    assert "before:2026-06" in query


def test_search_syntax_is_a_noop_when_empty():
    assert web_module._apply_search_syntax("abc", {}) == "abc"


# ── web_search ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_prefers_the_official_api(monkeypatch):
    monkeypatch.setattr(deepseek_web, "available", lambda: True)

    async def fake_search(query, *, count=5, need_content=False, ori_question=""):
        return [
            {
                "title": "DeepSeek V4",
                "url": "https://example.com/a",
                "summary": "摘要",
                "site_name": "Example",
                "date": "2026-06-01T00:00:00+00:00",
                "rerank_score": 2.4,
                "authority": 4,
            }
        ]

    monkeypatch.setattr(deepseek_web, "search", fake_search)

    result = await WebSearchTool().execute(query="deepseek v4")

    assert not result.is_error
    assert "via DeepSeek Search" in result.output
    assert "相关度 2.4" in result.output and "高权威" in result.output
    assert "2026-06-01" in result.output


@pytest.mark.asyncio
async def test_search_falls_back_when_the_api_errors(monkeypatch):
    monkeypatch.setattr(deepseek_web, "available", lambda: True)

    async def boom(query, *, count=5, need_content=False, ori_question=""):
        raise deepseek_web.WebApiError("HTTP 402：余额不足")

    monkeypatch.setattr(deepseek_web, "search", boom)
    called = {}

    async def fake_free(self, query, max_results, errors=None):
        called["errors"] = list(errors or [])
        return ToolResult(output="免费通道结果")

    monkeypatch.setattr(WebSearchTool, "_free_search", fake_free)

    result = await WebSearchTool().execute(query="x")

    assert result.output == "免费通道结果"
    assert any("402" in e for e in called["errors"]), "降级原因要留着，方便排查"


@pytest.mark.asyncio
async def test_search_without_key_goes_straight_to_free(monkeypatch):
    monkeypatch.setattr(deepseek_web, "available", lambda: False)

    async def must_not_run(**kwargs):  # pragma: no cover - 不该被调用
        raise AssertionError("没有 Key 时不该调用官方接口")

    monkeypatch.setattr(deepseek_web, "search", must_not_run)

    async def fake_free(self, query, max_results, errors=None):
        return ToolResult(output="免费通道结果")

    monkeypatch.setattr(WebSearchTool, "_free_search", fake_free)

    result = await WebSearchTool().execute(query="x")
    assert result.output == "免费通道结果"


# ── web_fetch ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_prefers_the_official_api(monkeypatch):
    monkeypatch.setattr(deepseek_web, "available", lambda: True)

    async def fake_fetch(url):
        return {"title": "页面标题", "content": "# 正文\n内容"}

    monkeypatch.setattr(deepseek_web, "fetch", fake_fetch)

    result = await WebFetchTool().execute(url="https://example.com/a")

    assert not result.is_error
    assert "via DeepSeek Fetch" in result.output
    assert "页面标题" in result.output and "# 正文" in result.output


@pytest.mark.asyncio
async def test_fetch_still_works_when_the_api_fails(monkeypatch):
    monkeypatch.setattr(deepseek_web, "available", lambda: True)

    async def boom(url):
        raise deepseek_web.WebApiError("抓不到可用的正文")

    monkeypatch.setattr(deepseek_web, "fetch", boom)

    async def fake_local(self, url, max_length):
        return ToolResult(output="本地抓取结果")

    monkeypatch.setattr(WebFetchTool, "_local_fetch", fake_local)

    result = await WebFetchTool().execute(url="https://example.com/a")

    assert result.output.endswith("本地抓取结果")
    assert "已改用本地抓取" in result.output
