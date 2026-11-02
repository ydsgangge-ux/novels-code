"""DeepSeek Search / Fetch 客户端：Key 从哪来、怎么降级、错误怎么说人话。"""

from __future__ import annotations

import pytest

from gangge.layer4_tools import deepseek_web


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("WEB_SEARCH_API_KEY", "DEEPSEEK_API_KEY", "WEB_SEARCH_BACKEND", "WEB_SEARCH_BASE_URL", "DEEPSEEK_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


# ── Key 与开关 ──────────────────────────────────────────────

def test_key_prefers_the_dedicated_one(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-main")
    assert deepseek_web.api_key() == "sk-main", "主模型是 DeepSeek 时应当零配置复用"

    monkeypatch.setenv("WEB_SEARCH_API_KEY", "sk-search")
    assert deepseek_web.api_key() == "sk-search", "专用 Key 优先级更高"


def test_unavailable_without_any_key():
    assert not deepseek_web.available()
    assert "WEB_SEARCH_API_KEY" in deepseek_web.missing_key_hint()


def test_free_backend_wins_even_with_a_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-main")
    monkeypatch.setenv("WEB_SEARCH_BACKEND", "free")
    assert not deepseek_web.available()


def test_base_url_strips_the_v1_suffix(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    assert deepseek_web.base_url() == "https://api.deepseek.com"

    monkeypatch.setenv("WEB_SEARCH_BASE_URL", "https://proxy.example.com/")
    assert deepseek_web.base_url() == "https://proxy.example.com"


# ── 调用 ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_parses_hits(monkeypatch):
    async def fake_post(path, payload, *, timeout, retries=1):
        assert path == deepseek_web.SEARCH_PATH
        assert payload["count"] == 3
        return {
            "results": [
                {"title": "A", "url": "https://a.example", "summary": "摘要 A", "authority": 4},
                {"title": "无链接", "summary": "该条会被丢弃"},
            ]
        }

    monkeypatch.setattr(deepseek_web, "_post", fake_post)
    hits = await deepseek_web.search("测试", count=3)

    assert len(hits) == 1, "没有 url 的条目对模型没用，直接丢"
    assert hits[0]["title"] == "A"


@pytest.mark.asyncio
async def test_search_clamps_count(monkeypatch):
    seen = {}

    async def fake_post(path, payload, *, timeout, retries=1):
        seen.update(payload)
        return {"results": []}

    monkeypatch.setattr(deepseek_web, "_post", fake_post)
    assert await deepseek_web.search("x", count=999) == []
    assert seen["count"] == 50


@pytest.mark.asyncio
async def test_fetch_returns_markdown(monkeypatch):
    async def fake_post(path, payload, *, timeout, retries=1):
        assert path == deepseek_web.FETCH_PATH
        assert payload["url"] == "https://a.example/post"
        return {"title": "标题", "content": "# 正文"}

    monkeypatch.setattr(deepseek_web, "_post", fake_post)
    page = await deepseek_web.fetch("https://a.example/post")

    assert page == {"title": "标题", "content": "# 正文"}


@pytest.mark.asyncio
async def test_fetch_rejects_empty_url():
    with pytest.raises(deepseek_web.WebApiError, match="URL"):
        await deepseek_web.fetch("  ")


# ── 错误文案 ────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("status", "expect"),
    [
        (401, "Key 无效或缺失"),
        (402, "余额不足"),
        (404, "抓不到可用的正文"),
        (429, "429"),
    ],
)
def test_errors_tell_people_what_to_do(status, expect):
    assert expect in deepseek_web._explain(status, "{}")


@pytest.mark.asyncio
async def test_post_requires_key_before_any_request():
    """没有 Key 就别发请求：省一次 401，也让人更快看到该填哪。"""
    with pytest.raises(deepseek_web.WebApiError, match="Key"):
        await deepseek_web._post("/v1/tools/search", {"query": "x"}, timeout=5)
