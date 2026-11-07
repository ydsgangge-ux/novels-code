"""飞行页（page="flight" / "miniature"）：一条 3D 光路穿过一串时间节点。

和人网页那套测试同一个思路——不开浏览器，只验证"页面拿到的是不是对的数据"
和"页面契约齐不齐"。真跑 Chromium 截帧是端到端的事。
"""

from __future__ import annotations

import json

import pytest

from gangge.layer3_agent.tools.demo_video import DemoVideoTool
from gangge.layer4_tools.storyboard import Storyboard
from gangge.layer4_tools.storyboard.flight import (
    END_MOVE,
    WORLD_OF_PAGE,
    FlightPageError,
    build_story,
    render_flight,
    three_source,
)
from gangge.layer4_tools.storyboard.pages import (
    NODE_PAGES,
    PAGE_KINDS,
    TEMPLATES,
    read_page,
)
from gangge.layer4_tools.storyboard.pipeline import write_page

JOURNEY = {
    "title": "从一间小屋到一座城",
    "subtitle": "十年，一直在往前走",
    "scenes": [
        {"template": "title", "title": "从一间小屋到一座城", "text": "十年，一直在往前走"},
        {"template": "quote", "year": "2015", "title": "山脚下的小屋", "text": "一台旧电脑。"},
        {"template": "quote", "year": "2018", "title": "搬进镇上", "text": "八个人，一间小楼。"},
        {"template": "outro", "title": "", "text": "路还长，继续走。"},
    ],
}


def _sb(data: dict | None = None) -> Storyboard:
    return Storyboard.from_dict(dict(data or JOURNEY))


def _story_of(html: str) -> dict:
    """从页面里把注入的 STORY 抠回来——它就是渲染层的全部输入。"""
    marker = "window.STORY="
    start = html.index(marker) + len(marker)
    return json.JSONDecoder().raw_decode(html[start:])[0]


# ── 注册 ────────────────────────────────────────────────────────


def test_both_worlds_are_registered_on_one_engine():
    assert "flight" in PAGE_KINDS and "miniature" in PAGE_KINDS
    # 两页只换世界，引擎是同一份文件——各写一份会很快漂移
    assert TEMPLATES["flight"] == TEMPLATES["miniature"] == "flight.html"
    assert set(NODE_PAGES) <= set(PAGE_KINDS)


def test_page_honours_the_render_contract():
    """renderer 只认这三个钩子：没有 __seek 就拍不出帧，没有 __hide_ui 会拍到控件。"""
    html = read_page("flight")
    assert "window.__seek=" in html
    assert "window.__hide_ui=" in html
    assert "window.__ready=" in html


def test_explicit_timeline_keeps_flight_before_dwell():
    """回归：箭头必须"飞过来再停站"，一站接一站，中间不能重叠。

    曾经把显式时间轴写成 ``t0 = start - travel``——飞行段缩进上一站的停站里，
    ``uAt`` 一直命中上一站，箭头整段不动，等上一站结束才一下跳到下一站。
    两个世界共用这份引擎，一处写错两页都中。
    """
    html = read_page("flight")
    assert "t0=o.start" in html, "飞行必须从这一站自己的 start 起跑"
    assert "d0=t0+fly" in html, "飞完才进入停站"
    assert "t1:d0" in html, "飞行终点就是停站起点"
    assert "t0=Math.max(0,d0-tr)" not in html


def test_capture_mode_keeps_the_canvas_composited():
    """逐帧拍摄时 playing=false；rAF 一停，Chromium 截图拿到的是没画过的白底。"""
    assert "if(CAPTURE)dirty=true" in read_page("flight")


def test_three_ships_with_the_package():
    """three.js 不随包发，别人 pip 装完一渲染就 FlightPageError。"""
    source = three_source()
    assert len(source) > 100_000, "vendor/three.min.js 太小，像是没下载完"
    assert "THREE" in source


# ── storyboard → STORY ──────────────────────────────────────────


def test_title_becomes_nameplate_and_outro_becomes_ending():
    sb = _sb()
    story = build_story(sb, sb.timeline())

    assert story["title"] == "从一间小屋到一座城"
    assert story["subtitle"] == "十年，一直在往前走"
    assert story["end"] == "路还长，继续走。"
    # 开场和结语不进光路，中间两个才是站点
    assert [node["date"] for node in story["nodes"]] == ["2015", "2018"]


def test_nodes_carry_the_exact_timeline():
    """配音说了多久就停多久：站点时间直接来自 slots，页面不再按字数猜。"""
    sb = _sb()
    slots = sb.timeline()
    story = build_story(sb, slots)

    for node, (_, start, end) in zip(story["nodes"], slots[1:-1]):
        assert node["start"] == pytest.approx(round(start, 3))
        assert node["end"] == pytest.approx(round(end, 3))

    # 名牌演到开场镜头结束；片尾从最后一站之后拉远收尾
    assert story["timing"]["intro"] == pytest.approx(round(slots[0][2], 3))
    last_end = slots[-2][2]
    assert story["timing"]["endHold"] == pytest.approx(
        round(max(1.0, slots[-1][2] - last_end - END_MOVE), 3)
    )


def test_world_and_theme_follow_the_page():
    sb = _sb()
    flight = build_story(sb, sb.timeline(), page="flight")
    mini = build_story(sb, sb.timeline(), page="miniature")

    assert flight["world"] == WORLD_OF_PAGE["flight"] == "grid"
    assert flight["theme"]["preset"] == "night"
    assert mini["world"] == "miniature"
    # 沙盘永远是白天的 meadow，不跟着分镜的 style 走
    assert mini["theme"]["preset"] == "meadow"


def test_style_maps_to_a_preset_and_theme_overrides_it():
    data = {"scenes": JOURNEY["scenes"], "style": "neon", "theme": {"accent": "#123456"}}
    sb = _sb(data)
    story = build_story(sb, sb.timeline())

    assert story["theme"]["preset"] == "aurora"
    assert story["theme"]["accent"] == "#123456"


def test_a_single_shot_storyboard_still_renders():
    """只有一个镜头时不能渲染空场景：把它自己当成唯一站点。"""
    sb = _sb({"scenes": [{"template": "title", "title": "只有一个镜头", "text": "副标题"}]})
    story = build_story(sb, sb.timeline())

    assert story["title"] == "只有一个镜头"
    assert len(story["nodes"]) == 1


def test_unknown_page_is_rejected():
    sb = _sb()
    with pytest.raises(FlightPageError, match="未知飞行页"):
        build_story(sb, sb.timeline(), page="hologram")


# ── 落地 ────────────────────────────────────────────────────────


def test_rendered_page_is_offline_and_single_file():
    sb = _sb()
    html = render_flight(sb, sb.timeline(), page="flight")

    for token in ("__TITLE__", "__STORY__", "__THREE_LIB__"):
        assert token not in html, f"{token} 没被替换掉，渲染出来就是一段死代码"
    assert "cdnjs" not in html and "unpkg" not in html, "飞行页不能联外网，渲染会挂"
    assert _story_of(html)["title"] == "从一间小屋到一座城"


def test_write_page_drops_the_right_file(tmp_path):
    sb = _sb()
    out = write_page(sb, sb.timeline(), tmp_path, page="miniature")

    assert out.name == "miniature.html"
    assert (tmp_path / "storyboard.html").is_file()  # 拍坏了还能退回最轻的那份


def test_tool_schema_lists_both_pages():
    props = DemoVideoTool().input_schema["properties"]
    assert props["page"]["enum"] == list(PAGE_KINDS)
    assert "flight" in props["page"]["description"]
    assert "miniature" in props["page"]["description"]
