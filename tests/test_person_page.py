"""人物页（page="person"）：一条光路穿过一个人的一生。

和展示页那套测试同一个思路——不启动浏览器，只验证"页面拿到的是不是对的
数据"和"页面契约齐不齐"。真跑 Chromium 截帧是端到端的事。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from gangge.layer3_agent.tools.demo_video import DemoVideoTool
from gangge.layer4_tools.storyboard import Storyboard, write_preview
from gangge.layer4_tools.storyboard.pages import PAGE_KINDS, read_page
from gangge.layer4_tools.storyboard.person import (
    PersonPageError,
    assign_photos,
    build_story,
    render_person,
)
from gangge.layer4_tools.storyboard.schema import Scene

PERSON = {
    "title": "袁隆平",
    "subtitle": "一粒种子的一生",
    "scenes": [
        {"template": "title", "title": "袁隆平", "text": "一粒种子的一生"},
        {"template": "quote", "year": "1930", "title": "生于北京", "text": "动荡年代里出生。"},
        {"template": "quote", "year": "1961", "title": "田里那株稻子", "text": "发现天然不育株。"},
        {"template": "outro", "title": "一辈子一件事", "text": "一生所求，不过让更多人吃饱饭。"},
    ],
}


def _sb(data: dict | None = None) -> Storyboard:
    return Storyboard.from_dict(dict(data or PERSON))


def _story_of(html: str) -> dict:
    """从页面里把注入的 STORY 抠回来——它就是渲染层的全部输入。"""
    marker = "var STORY = "
    start = html.index(marker) + len(marker)
    end = html.index(";\n", start)
    return json.loads(html[start:end])


# ── registration ────────────────────────────────────────────────


def test_person_page_is_registered():
    assert "person" in PAGE_KINDS
    assert "STORY" in read_page("person")


def test_page_honours_the_render_contract():
    """renderer 只认这三个钩子：没有 __seek 就拍不出帧。"""
    html = read_page("person")
    assert "window.__seek = function" in html
    assert "window.__hide_ui = function" in html


# ── storyboard → STORY ──────────────────────────────────────────


def test_title_becomes_nameplate_and_outro_becomes_ending():
    sb = _sb()
    story = build_story(sb, sb.timeline())

    assert story["title"] == "袁隆平"
    assert story["subtitle"] == "一粒种子的一生"
    assert story["end"] == "一生所求，不过让更多人吃饱饭。"
    # 开场和结尾不算人生节点
    assert len(story["nodes"]) == 2
    assert story["intro"] > 0
    assert story["ending"] > story["intro"]


def test_every_node_exposes_the_fields_the_page_reads():
    sb = _sb()
    story = build_story(sb, sb.timeline())

    node = story["nodes"][0]
    assert node["year"] == "1930"
    assert node["title"] == "生于北京"
    assert node["short"], "节点小球上那行小字不能是空的"
    assert len(node["short"]) <= 6
    assert node["photos"] == []
    assert len(story["beats"]) == len(story["nodes"])


def test_beats_stay_in_sync_with_the_voiceover_slots():
    """讲解时间轴就是配音时间轴，两边错开就会出现"说到一半人走了"。"""
    sb = _sb()
    slots = sb.timeline()
    story = build_story(sb, slots)

    assert story["beats"][0]["start"] >= story["intro"] - 0.001
    assert story["beats"][-1]["end"] <= story["ending"] + 0.001
    assert story["total"] == pytest.approx(slots[-1][2], abs=0.01)


def test_bullets_items_are_folded_into_the_body():
    """模型常拿 bullets 写人物节点，items 不能就这么丢掉。"""
    sb = _sb(
        {
            "scenes": [
                {"template": "title", "title": "某某"},
                {"template": "bullets", "title": "求学", "items": ["考入农学院", "转学农学"]},
            ]
        }
    )
    story = build_story(sb, sb.timeline())
    assert "考入农学院" in story["nodes"][0]["text"]


def test_lone_title_scene_still_renders():
    """只给一句话也要出片，不能渲染一张空地图。"""
    sb = Storyboard.from_dict({"title": "单人", "scenes": [
        {"template": "title", "title": "单人", "text": "只有一句话"}
    ]})
    story = build_story(sb, sb.timeline())
    assert len(story["nodes"]) == 1


def test_empty_storyboard_is_rejected():
    sb = Storyboard(scenes=[])
    with pytest.raises(PersonPageError):
        build_story(sb, [])


# ── photos ──────────────────────────────────────────────────────


def _touch_images(folder: Path, names: list[str]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_bytes(b"not-a-real-png")


def test_photos_are_handed_out_round_robin(tmp_path: Path):
    """5 张图 2 个节点：多出来的图不该被丢掉，也不该全堆在第一个节点。"""
    _touch_images(tmp_path / "pics", ["a.png", "b.png", "c.png", "d.png", "e.png"])
    sb = _sb()

    notes = assign_photos(sb, tmp_path / "pics")

    nodes = [s for s in sb.scenes if s.template == "quote"]
    assert [len(n.photos) for n in nodes] == [3, 2]
    assert "5" in notes[0]


def test_explicit_photo_list_wins_over_the_directory(tmp_path: Path):
    _touch_images(tmp_path / "pics", ["a.png"])
    data = dict(PERSON)
    data["scenes"] = [dict(s) for s in PERSON["scenes"]]
    data["scenes"][1] = {**data["scenes"][1], "photos": []}
    sb = Storyboard.from_dict(data)
    sb.scenes[1].photos = []

    notes = assign_photos(sb, tmp_path / "pics")
    assert sb.scenes[1].photos, notes


def test_missing_photo_is_dropped_not_fatal(tmp_path: Path):
    """路径写错只丢一张图，不能让整条片子失败。"""
    real = tmp_path / "ok.png"
    real.write_bytes(b"x")
    data = json.loads(json.dumps(PERSON))
    data["scenes"][1]["photos"] = [str(real), str(tmp_path / "nope.png")]

    sb = Storyboard.from_dict(data)
    story = build_story(sb, sb.timeline(), base_dir=tmp_path)

    photos = story["nodes"][0]["photos"]
    assert len(photos) == 1
    assert photos[0]["src"].startswith("file:")


def test_photo_directory_without_images_is_reported(tmp_path: Path):
    (tmp_path / "empty").mkdir()
    notes = assign_photos(_sb(), tmp_path / "empty")
    assert "没有图片" in notes[0]


# ── page output ─────────────────────────────────────────────────


def test_render_replaces_every_placeholder():
    sb = _sb()
    html = render_person(sb, sb.timeline())
    # 模板占位是 __UPPER__ 形状；window.__play 这类钩子不该被误伤
    assert re.findall(r"__[A-Z][A-Z0-9_]*__", html) == []


def test_portrait_switches_the_layout_class():
    sb = _sb()
    sb.width, sb.height = 1080, 1920
    html = render_person(sb, sb.timeline())
    assert "portrait" in html
    assert "1080" in html and "1920" in html


def test_preview_writes_person_html(tmp_path: Path):
    html = write_preview(_sb(), tmp_path, page="person")
    assert html.name == "person.html"
    story = _story_of(html.read_text(encoding="utf-8"))
    assert story["title"] == "袁隆平"


# ── tool ────────────────────────────────────────────────────────


def test_schema_offers_person_and_photos():
    props = DemoVideoTool().input_schema["properties"]
    assert "person" in props["page"]["enum"]
    assert "photos" in props
    assert "人物" in props["page"]["description"]


@pytest.mark.asyncio
async def test_tool_preview_uses_the_person_prompt(tmp_path: Path):
    """给 topic + page=person 时，模型收到的是"时间节点"提示词，不是要点式分镜。"""
    seen: list[str] = []

    class _Recorder:
        async def chat(self, messages=None, **kwargs):
            seen.append(messages[0].content[0].text)
            from types import SimpleNamespace

            return SimpleNamespace(text=json.dumps(PERSON, ensure_ascii=False))

    tool = DemoVideoTool(workspace=str(tmp_path), llm=_Recorder())
    result = await tool.execute(
        action="preview", topic="袁隆平", page="person", output_dir=str(tmp_path / "out")
    )

    assert not result.is_error, result.output
    assert "时间节点" in seen[0]
    assert "bullets" not in seen[0]  # 要点式分镜喂给光路页会变成一串孤立的观点
    assert (tmp_path / "out" / "person.html").is_file()


@pytest.mark.asyncio
async def test_tool_passes_photos_dir_through(tmp_path: Path):
    _touch_images(tmp_path / "pics", ["a.png", "b.png"])
    tool = DemoVideoTool(workspace=str(tmp_path))

    result = await tool.execute(
        action="preview",
        scenes=PERSON["scenes"],
        page="person",
        photos=str(tmp_path / "pics"),
        output_dir=str(tmp_path / "out"),
    )

    assert not result.is_error, result.output
    assert "照片" in result.output
    story = json.loads(
        (tmp_path / "out" / "storyboard.json").read_text(encoding="utf-8")
    )
    assigned = sum(len(s["photos"]) for s in story["scenes"])
    assert assigned == 2


@pytest.mark.asyncio
async def test_bad_photos_dir_is_rejected(tmp_path: Path):
    tool = DemoVideoTool(workspace=str(tmp_path))
    result = await tool.execute(
        action="preview",
        scenes=PERSON["scenes"],
        page="person",
        photos=str(tmp_path / "ghost"),
        output_dir=str(tmp_path / "out"),
    )
    assert result.is_error
    assert "照片目录" in result.output
