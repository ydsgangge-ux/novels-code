"""网址 → 摘要 → 分镜：解析逻辑全程离线，只有 fetch_page 碰网络。"""

from __future__ import annotations

import asyncio

import pytest

from gangge.layer4_tools.storyboard import sources
from gangge.layer4_tools.storyboard.schema import Storyboard

PAGE = """<!DOCTYPE html><html><head>
<title>uv · 一个极快的 Python 包管理器</title>
<meta name="description" content="用 Rust 写的 Python 包和项目管理器">
<meta property="og:description" content="不该覆盖已有的摘要">
<style>.x{color:red}</style>
<script>var junk = 1;</script>
</head><body>
<nav><a href="/">首页</a><a href="/docs">文档</a></nav>
<header><h1>站点名字</h1></header>
<main>
  <h1>uv</h1>
  <p>一个极快的 Python 包和项目管理器。</p>
  <h2>安装</h2>
  <p>一条命令装好，不需要预先装 Python。</p>
  <h2>特点</h2>
  <p>比 pip 快 10-100 倍，并且带通用 lockfile。</p>
</main>
<footer>版权所有</footer>
</body></html>"""


def _prompt_of(llm) -> str:
    return "".join(b.text for b in llm.requests[0][0].content)


def test_parser_prefers_main_and_drops_chrome():
    parser = sources._DigestParser()
    parser.feed(PAGE)
    parser.close()

    assert parser.title == "uv · 一个极快的 Python 包管理器"
    assert parser.description == "用 Rust 写的 Python 包和项目管理器"
    text = parser.text(4000)
    assert "一个极快的 Python 包和项目管理器" in text
    assert "通用 lockfile" in text
    # 导航、页头、页脚、脚本都不该进来
    assert "首页" not in text
    assert "站点名字" not in text
    assert "版权所有" not in text
    assert "junk" not in text
    # h1/h2 被收成小标题
    assert "安装" in parser.headings and "特点" in parser.headings


def test_normalize_url():
    assert sources.normalize_url("example.com") == "https://example.com"
    assert sources.normalize_url(" http://a.cn/x ") == "http://a.cn/x"
    with pytest.raises(sources.SourceError):
        sources.normalize_url("")
    with pytest.raises(sources.SourceError):
        sources.normalize_url("ftp://a.cn/x")


def test_strip_markdown_drops_noise():
    md = """# 标题

[![CI](https://img.shields.io/x.svg)](https://ci)
![截图](https://a/b.png)

正文一段，带 `代码` 和 [链接](https://x)。

```python
print("这段要整块丢掉")
```

## 安装
- 第一条
"""
    text, headings = sources._strip_markdown(md)
    assert headings == ["标题", "安装"]
    assert "正文一段" in text and "链接" in text
    assert "img.shields.io" not in text
    assert "整块丢掉" not in text
    assert "```" not in text


def test_github_slug_and_title():
    match = sources._GITHUB_RE.match("https://github.com/astral-sh/uv/tree/main")
    assert match is not None
    assert sources._github_slug(match) == "astral-sh/uv"
    assert sources._pretty_github_title(
        "GitHub - astral-sh/uv: 极快的包管理器 · GitHub"
    ) == "astral-sh/uv: 极快的包管理器"
    assert sources._GITHUB_RE.match("https://gitlab.com/a/b") is None


def test_page_digest_prompt_includes_everything():
    page = sources.PageDigest(
        url="https://a.cn/x",
        title="标题",
        description="摘要",
        headings=("一", "二"),
        text="正文",
    )
    block = page.prompt_block()
    for piece in ("https://a.cn/x", "标题", "摘要", "- 一", "- 二", "正文"):
        assert piece in block
    assert page.slug() == "a-cn-x"


def test_fetch_page_uses_http_get(monkeypatch):
    monkeypatch.setattr(
        sources, "_http_get", lambda url, timeout, **kw: PAGE.encode("utf-8")
    )
    digest = sources.fetch_page("https://a.cn/uv")
    assert digest.title == "uv · 一个极快的 Python 包管理器"
    assert "通用 lockfile" in digest.text


def test_fetch_page_reports_empty_pages(monkeypatch):
    monkeypatch.setattr(
        sources, "_http_get", lambda url, timeout, **kw: b"<html><body></body></html>"
    )
    with pytest.raises(sources.SourceError, match="没抽出可用的内容"):
        sources.fetch_page("https://a.cn/empty")


def test_decode_falls_back_to_gbk():
    raw = "中文".encode("gbk")
    assert sources._decode(raw) == "中文"


GOOD = """
```json
{"title": "uv 介绍", "scenes": [
  {"template": "title", "title": "uv 有多快", "text": "包管理器"},
  {"template": "bullets", "title": "亮点", "items": ["快", "通用 lockfile"]},
  {"template": "outro", "title": "试试看", "items": ["开始使用"]}
]}
```
"""


def test_draft_from_url_returns_storyboard(monkeypatch):
    from tests.support.fake_llm import ScriptedLLM, say

    monkeypatch.setattr(
        sources,
        "fetch_page",
        lambda url, **kw: sources.PageDigest(url=url, title="uv", text="正文"),
    )
    llm = ScriptedLLM([say(GOOD)])
    sb = asyncio.run(sources.draft_from_url("https://a.cn/uv", llm))
    assert isinstance(sb, Storyboard)
    assert sb.title == "uv 介绍"
    # 提示词里必须带上页面内容，否则模型只能瞎编
    assert "正文" in _prompt_of(llm)


def test_draft_from_url_retries_once_on_bad_json(monkeypatch):
    from tests.support.fake_llm import ScriptedLLM, say

    monkeypatch.setattr(
        sources,
        "fetch_page",
        lambda url, **kw: sources.PageDigest(url=url, title="uv", text="正文"),
    )
    llm = ScriptedLLM([say("我不太会写 JSON"), say(GOOD)])
    sb = asyncio.run(sources.draft_from_url("https://a.cn/uv", llm))
    assert sb.title == "uv 介绍"
    assert len(llm.requests) == 2


def test_draft_from_url_gives_up_after_two_bad_answers(monkeypatch):
    from tests.support.fake_llm import ScriptedLLM, say

    monkeypatch.setattr(
        sources,
        "fetch_page",
        lambda url, **kw: sources.PageDigest(url=url, title="uv", text="正文"),
    )
    llm = ScriptedLLM([say("xxx"), say("yyy")])
    with pytest.raises(sources.SourceError, match="两次"):
        asyncio.run(sources.draft_from_url("https://a.cn/uv", llm))
