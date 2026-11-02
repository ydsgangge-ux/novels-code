"""demo_video 的 page 参数：对话里也能直接出 3D 展示页。

这条链是"别人从 GitHub 下载就能用"的关键——技能点击只是预填提示词，真正把
页面拍出来的是工具。所以这里断言的不只是 schema，还有实际落盘。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from gangge.layer3_agent.tools.demo_video import DemoVideoTool
from gangge.layer4_tools.storyboard.pages import PAGE_KINDS

SCENES = [
    {"template": "title", "title": "三步做出演示动画", "text": "给产品做个门面"},
    {"template": "bullets", "title": "三步", "items": ["定主题", "挑风格", "一键出片"]},
    {"template": "outro", "title": "试试看", "text": "选一条技能就能开始"},
]


def test_schema_exposes_page_and_url():
    schema = DemoVideoTool().input_schema
    props = schema["properties"]

    assert props["page"]["enum"] == list(PAGE_KINDS)
    assert props["page"]["default"] == "storyboard"
    assert "url" in props
    assert "3D 展示页" in props["page"]["description"]


def test_schema_accepts_resolve_for_poisoned_dns():
    """抓图一直超时的那种网络，得能钉 IP。"""
    props = DemoVideoTool().input_schema["properties"]
    assert "resolve" in props
    assert "140.82.112.4" in props["resolve"]["description"]


@pytest.mark.asyncio
async def test_preview_renders_the_showcase_page(tmp_path: Path):
    out = tmp_path / "out"
    tool = DemoVideoTool(workspace=str(tmp_path))

    result = await tool.execute(
        action="preview", scenes=SCENES, page="showcase", output_dir=str(out)
    )

    assert not result.is_error, result.output
    page = out / "showcase.html"
    assert page.is_file()
    html = page.read_text(encoding="utf-8")
    assert "iframe" in html, "展示页的浏览器框没了，就不叫展示页"
    assert (out / "storyboard.html").is_file(), "朴素版要留一份，方便对照"
    assert result.metadata["page"] == "showcase"


@pytest.mark.asyncio
async def test_site_page_without_url_is_rejected(tmp_path: Path):
    tool = DemoVideoTool(workspace=str(tmp_path))

    result = await tool.execute(
        action="preview", scenes=SCENES, page="site", output_dir=str(tmp_path / "out")
    )

    assert result.is_error
    assert "url" in result.output


@pytest.mark.asyncio
async def test_unknown_page_is_rejected(tmp_path: Path):
    tool = DemoVideoTool(workspace=str(tmp_path))

    result = await tool.execute(
        action="preview", scenes=SCENES, page="hologram", output_dir=str(tmp_path / "out")
    )

    assert result.is_error
    assert "hologram" in result.output


def test_schema_exposes_fps():
    props = DemoVideoTool().input_schema["properties"]
    assert props["fps"]["default"] == 30
    assert (props["fps"]["minimum"], props["fps"]["maximum"]) == (10, 60)


@pytest.mark.asyncio
async def test_fps_reaches_the_storyboard(tmp_path: Path):
    out = tmp_path / "out"
    tool = DemoVideoTool(workspace=str(tmp_path))

    result = await tool.execute(
        action="preview", scenes=SCENES, fps=24, output_dir=str(out)
    )

    assert not result.is_error, result.output
    data = json.loads((out / "storyboard.json").read_text(encoding="utf-8"))
    assert data["fps"] == 24


class _FakeLLM:
    """只回一段 JSON 的假模型：用来验证"模型自己挑风格"这条路径。"""

    def __init__(self, payload: str):
        self.payload = payload

    async def chat(self, messages=None, **kwargs):
        return SimpleNamespace(text=self.payload)


@pytest.mark.asyncio
async def test_model_picked_style_is_applied(tmp_path: Path):
    draft = json.dumps(
        {
            "title": "示例",
            "style": "paper",
            "decor": ["grain", "glow"],
            "scenes": SCENES,
        },
        ensure_ascii=False,
    )
    tool = DemoVideoTool(workspace=str(tmp_path), llm=_FakeLLM(draft))

    result = await tool.execute(action="write", topic="一个讲历史的短片")

    assert not result.is_error, result.output
    sb = result.metadata["storyboard"]
    assert sb["style"] == "paper"
    assert sb["decor"] == ["grain", "glow"]


@pytest.mark.asyncio
async def test_explicit_style_beats_the_model(tmp_path: Path):
    """用户说了算：显式传 style 时不能被模型挑的覆盖。"""
    draft = json.dumps({"title": "示例", "style": "paper", "scenes": SCENES})
    tool = DemoVideoTool(workspace=str(tmp_path), llm=_FakeLLM(draft))

    result = await tool.execute(action="write", topic="随便", style="neon")

    assert not result.is_error, result.output
    assert result.metadata["storyboard"]["style"] == "neon"


@pytest.mark.asyncio
async def test_illegal_model_style_is_ignored(tmp_path: Path):
    draft = json.dumps({"title": "示例", "style": "hologram", "scenes": SCENES})
    tool = DemoVideoTool(workspace=str(tmp_path), llm=_FakeLLM(draft))

    result = await tool.execute(action="write", topic="随便")

    assert not result.is_error, result.output
    # 非法值不能写进分镜，否则渲染时才炸
    assert result.metadata["storyboard"]["style"] in ("dark", "neon", "paper", "glass", "brutal")


@pytest.mark.asyncio
async def test_default_page_stays_plain(tmp_path: Path):
    """不传 page 时行为必须和以前一模一样，老用户不该被新功能影响。"""
    out = tmp_path / "out"
    tool = DemoVideoTool(workspace=str(tmp_path))

    result = await tool.execute(action="preview", scenes=SCENES, output_dir=str(out))

    assert not result.is_error, result.output
    assert (out / "storyboard.html").is_file()
    assert not (out / "showcase.html").exists()
