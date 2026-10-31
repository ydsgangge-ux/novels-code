"""3D 展示页的契约测试——纯字符串，不开浏览器、不联网。"""

from __future__ import annotations

import base64
import json
import re

from gangge.layer4_tools.storyboard.demo import demo_storyboard
from gangge.layer4_tools.storyboard.showcase import (
    chapters_of,
    render_showcase,
    write_showcase,
)


def _inner(page: str) -> str:
    blob = re.search(r'id="inner-page">([A-Za-z0-9+/=]+)<', page)
    assert blob, "内页没有内联进展示页"
    return base64.b64decode(blob.group(1)).decode("utf-8")


def test_chapters_cover_every_scene():
    sb = demo_storyboard("neon")
    chapters = chapters_of(sb)
    assert len(chapters) == len(sb.scenes)
    assert [c["label"] for c in chapters] == ["01", "02", "03", "04", "05"]
    # 时间窗口必须落在各自镜头内部，否则滚动时会截到空镜或切镜闪白
    for (scene, start, end), chapter in zip(sb.timeline(), chapters):
        assert start <= chapter["t0"] < chapter["t1"] <= end


def test_chapters_are_json_safe():
    page = render_showcase(demo_storyboard("neon"))
    match = re.search(r"var CHAPTERS = (\[.*?\]);", page, re.S)
    assert match
    chapters = json.loads(match.group(1))
    assert chapters[0]["title"] == "三步做出演示动画"


def test_showcase_is_self_contained():
    page = render_showcase(demo_storyboard("dark"))
    assert "<link" not in page
    assert "http://" not in page and "https://" not in page
    assert "<script src" not in page
    assert "</script>" in page


def test_showcase_embeds_the_storyboard_page():
    page = render_showcase(demo_storyboard("neon"))
    inner = _inner(page)
    assert 'id="camera"' in inner
    assert "window.__seek" in inner
    # 内页只允许有 data: 内联资源（噪点纹理的 xmlns 不是网络请求）
    assert "<script src" not in inner
    assert '<img src="http' not in inner


def test_showcase_uses_style_palette_and_aspect():
    sb = demo_storyboard("neon")
    page = render_showcase(sb)
    assert "--accent: #22d3ee" in page
    assert "--accent2: #a855f7" in page

    sb.width, sb.height = 1080, 1920
    portrait = render_showcase(sb)
    assert "var AR = 0.5625" in portrait


def test_showcase_reflects_scroll_math():
    page = render_showcase(demo_storyboard("paper"))
    # 滚动条长度 = 镜头数 × 一屏 + 收尾余量
    assert "calc(100vh * 5.55)" in page
    assert "rotateX(" in page and "translate3d(0,0,' + c.z" in page


def test_write_showcase_accepts_a_directory(tmp_path):
    out = write_showcase(demo_storyboard("glass"), tmp_path / "show")
    assert out.name == "index.html"
    assert out.exists()
    assert len(out.read_text(encoding="utf-8")) > 5000


def test_showcase_exposes_the_recording_hooks():
    """逐帧录制靠这三个钩子：__ready / __hide_ui / __seek(t)。"""
    page = render_showcase(demo_storyboard("neon"))
    assert "window.__ready" in page
    assert "window.__hide_ui = function" in page
    assert "window.__seek = function" in page
    # __seek 必须接管驱动，否则 rAF 会把画面拉回滚动位置
    assert "SEEK_MODE" in page
    assert "var TOTAL = 5" in page or "var TOTAL = " in page


def test_chapters_follow_given_slots():
    """录视频用的是配音后的真实时间轴，章节必须跟着它走。"""
    sb = demo_storyboard("neon")
    slots = [(scene, i * 4.0, (i + 1) * 4.0) for i, scene in enumerate(sb.scenes)]
    chapters = chapters_of(sb, slots)
    assert chapters[1]["s0"] == 4.0
    assert chapters[1]["s1"] == 8.0
    assert 4.0 < chapters[1]["t0"] < chapters[1]["t1"] < 8.0
    page = render_showcase(sb, slots)
    assert '"s0": 4.0' in page


def test_source_url_lands_in_the_fake_address_bar():
    page = render_showcase(
        demo_storyboard("neon"), source_url="https://github.com/astral-sh/uv"
    )
    assert "https://github.com/astral-sh/uv" in page


def test_inner_html_replaces_the_storyboard_page():
    """框里放什么由 inner_html 决定——整页截图就是这么塞进去的。"""
    page = render_showcase(
        demo_storyboard("neon"), inner_html="<html><body>SCREENSHOT</body></html>"
    )
    assert "SCREENSHOT" in _inner(page)
    assert 'id="camera"' not in _inner(page)


def test_preview_can_emit_the_showcase(tmp_path):
    from gangge.layer4_tools.storyboard.pipeline import write_preview

    out = write_preview(
        demo_storyboard("dark"), tmp_path, page="showcase", source_url="https://a.cn"
    )
    assert out.name == "showcase.html"
    assert (tmp_path / "storyboard.html").exists()      # 朴素版留着做对照
    assert (tmp_path / "storyboard.json").exists()
    assert 'id="world"' in out.read_text(encoding="utf-8")
