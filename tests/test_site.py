"""整页截图展示页：内页结构、标题拆分、落盘——都不碰网络和浏览器。"""

from __future__ import annotations

import base64
import re

import pytest

from gangge.layer4_tools.storyboard.site import (
    PAN_SECONDS,
    SiteShot,
    _headline,
    build_site_page,
    shot_inner_page,
)

# 一个最小的"JPEG"：内容无所谓，这里只校验它被内联进页面
FAKE_JPEG = b"\xff\xd8\xff\xe0fake-jpeg-payload"


def _shot(title: str = "owner/repo: 一句话介绍", desc: str = "摘要") -> SiteShot:
    return SiteShot(
        url="https://github.com/owner/repo",
        title=title,
        description=desc,
        image=FAKE_JPEG,
        width=1400,
        height=5200,
    )


def _inner(page: str) -> str:
    blob = re.search(r'id="inner-page">([A-Za-z0-9+/=]+)<', page)
    assert blob, "内页没有内联"
    return base64.b64decode(blob.group(1)).decode("utf-8")


def test_inner_page_pans_the_screenshot():
    inner = shot_inner_page(_shot(), 0.3, 9.7)
    assert "data:image/jpeg;base64," in inner
    assert "window.__seek" in inner and "window.__hide_ui" in inner
    assert "T0 = 0.300" in inner and "T1 = 9.700" in inner
    # 时间轴两端必须来自外层给的时间窗，否则滚到底也不会走完整页
    assert "translate3d(0,' + (-pan" in inner


def test_headline_splits_brand_from_tagline():
    head, note = _headline(
        "GitHub - astral-sh/uv: An extremely fast package manager · GitHub", "", ""
    )
    assert head == "astral-sh/uv"
    assert note.startswith("An extremely fast")
    # 没有冒号的标题原样留着，注解退回摘要
    assert _headline("Example Domain", "示例站点", "https://a.cn") == (
        "Example Domain",
        "示例站点",
    )
    # 太长又切不开的标题截断，别把 HUD 撑成两行英文
    head, note = _headline("A" * 50, "", "https://a.cn")
    assert head.endswith("…") and len(head) == 34
    assert note == "https://a.cn"


def test_build_site_page_wires_everything(tmp_path):
    out = build_site_page("https://github.com/owner/repo", tmp_path / "site.html",
                          style="neon", pan_screens=3.0, shot=_shot())
    page = out.read_text(encoding="utf-8")

    inner = _inner(page)
    assert "data:image/jpeg;base64," in inner
    assert base64.b64encode(FAKE_JPEG).decode() in inner

    # 假地址栏、HUD 文案、滚动长度、单段不画刻度
    assert "https://github.com/owner/repo" in page
    assert '"title": "owner/repo"' in page
    assert '"note": "一句话介绍"' in page
    assert "calc(100vh * 3.55)" in page
    assert '<div id="rail"></div>' in page

    # 画幅按 16:10 算，帧更接近笔记本屏幕
    assert "var AR = 1.6" in page


def test_video_pipeline_uses_the_shot_as_inner_page(tmp_path):
    """--page site：整段视频 = 一次从上到下的平移，时间窗覆盖全长。"""
    from gangge.layer4_tools.storyboard.pipeline import write_page
    from gangge.layer4_tools.storyboard.script import storyboard_from_script

    sb = storyboard_from_script("第一段话要说清楚。\n\n第二段话接着讲。")
    slots = sb.timeline()
    out = write_page(sb, slots, tmp_path, page="site", site_shot=_shot())

    assert out.name == "site.html"
    assert (tmp_path / "storyboard.html").exists()
    page = out.read_text(encoding="utf-8")
    inner = _inner(page)
    total = round(slots[-1][2], 3)
    assert f"T0 = 0.000" in inner
    assert f"T1 = {max(0.2, total):.3f}" in inner


def test_site_page_without_shot_is_rejected(tmp_path):
    from gangge.layer4_tools.storyboard.pipeline import write_page
    from gangge.layer4_tools.storyboard.script import storyboard_from_script

    sb = storyboard_from_script("只有一段话。")
    with pytest.raises(ValueError, match="site_shot"):
        write_page(sb, sb.timeline(), tmp_path, page="site")


def test_build_site_page_defaults_to_four_screens(tmp_path):
    out = build_site_page("https://a.cn", tmp_path / "s.html", shot=_shot())
    page = out.read_text(encoding="utf-8")
    expected = f"calc(100vh * {4.0 + 0.55:.2f})"
    assert expected in page
    # 时间窗两端各让 0.3s，内页不会停在空档上
    inner = _inner(page)
    assert "T0 = 0.300" in inner
    assert f"T1 = {PAN_SECONDS - 0.3:.3f}" in inner
