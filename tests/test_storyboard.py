"""Storyboard contract tests — no browser, no network, no ffmpeg.

These cover the parts that decide *what* gets rendered: schema validation, the
timeline, and the page contract (``__seek`` + template hooks). Frame capture and
encoding are exercised end-to-end separately because they need Chromium and an
encoder installed.
"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from gangge.layer4_tools.storyboard import (
    Storyboard,
    StoryboardError,
    render_page,
    write_preview,
)
from gangge.layer4_tools.storyboard import composer, templates
from gangge.layer4_tools.storyboard.schema import Scene, estimate_speech_seconds


def _sb(**overrides) -> Storyboard:
    data = {
        "scenes": [
            {"template": "title", "title": "标题", "voiceover": "开场白。"},
            {"template": "outro", "title": "结束"},
        ]
    }
    data.update(overrides)
    return Storyboard.from_dict(data)


def _make_wav(path: Path, seconds: float, amplitude: int = 1000) -> Path:
    frames = int(seconds * composer.SAMPLE_RATE)
    sample = int(amplitude).to_bytes(2, "little", signed=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(composer.CHANNELS)
        w.setsampwidth(composer.SAMPLE_WIDTH)
        w.setframerate(composer.SAMPLE_RATE)
        w.writeframes(sample * frames)
    return path


# ── schema ──────────────────────────────────────────────────────


def test_accepts_bare_scene_list():
    sb = Storyboard.from_dict([{"template": "title", "title": "只有一屏"}])
    assert len(sb.scenes) == 1
    assert sb.scenes[0].title == "只有一屏"


def test_rejects_unknown_template():
    with pytest.raises(StoryboardError, match="template 无效"):
        Storyboard.from_dict([{"template": "explosion", "title": "x"}])


def test_bullets_require_items():
    with pytest.raises(StoryboardError, match="items"):
        Storyboard.from_dict([{"template": "bullets", "title": "空列表"}])


def test_flow_requires_two_nodes():
    with pytest.raises(StoryboardError, match="nodes"):
        Storyboard.from_dict([{"template": "flow", "nodes": ["只有一个"]}])


def test_empty_scenes_rejected():
    with pytest.raises(StoryboardError):
        Storyboard.from_dict({"scenes": []})


def test_spoken_text_prefers_voiceover_then_fields():
    scene = Storyboard.from_dict(
        [{"template": "bullets", "title": "T", "items": ["甲", "乙"], "voiceover": "口播"}]
    ).scenes[0]
    assert scene.spoken_text() == "口播"

    scene.voiceover = ""
    assert scene.spoken_text() == "T，甲，乙"


def test_clamps_out_of_range_settings():
    sb = Storyboard.from_dict({"fps": 999, "scenes": [{"template": "title", "title": "x"}]})
    assert sb.fps == 60


def test_roundtrip_to_dict():
    sb = _sb()
    again = Storyboard.from_dict(sb.to_dict())
    assert [s.template for s in again.scenes] == ["title", "outro"]


# ── timeline ────────────────────────────────────────────────────


def test_timeline_uses_override_then_gap():
    sb = _sb(gap=0.5)
    slots = sb.timeline([2.0, 3.0])
    assert [round(s[1], 3) for s in slots] == [0.0, 2.5]
    assert [round(s[2], 3) for s in slots] == [2.0, 5.5]


def test_timeline_falls_back_to_estimate():
    sb = _sb(scenes=[{"template": "title", "title": "短"}])
    _, start, end = sb.timeline()[0]
    assert start == 0.0
    assert end == pytest.approx(estimate_speech_seconds("短"), abs=0.01)


# ── page contract ───────────────────────────────────────────────


def test_render_page_exposes_seek_and_templates():
    sb = _sb(scenes=[
        {"template": "title", "title": "标题"},
        {"template": "bullets", "title": "要点", "items": ["一", "二"]},
    ])
    page = render_page(sb, sb.timeline([2.0, 3.0]))
    assert "window.__seek" in page
    assert 'class="scene tpl-title"' in page
    assert 'class="scene tpl-bullets"' in page
    assert 'data-anim="slide-right"' in page
    assert page.count('data-anim="') >= 4


def test_stagger_entries_are_monotonic_and_inside_scene():
    """Each item enters after the previous one and before the scene ends.

    A scene can be far longer than its lead-in (voice-over decides the length),
    so a frame grabbed late in the scene still has to show *all* items. If an
    entry time ever exceeded the scene duration, that item would be invisible
    for the whole scene — the classic "third bullet missing" screenshot.
    """
    for count, duration in ((2, 2.0), (3, 9.34), (6, 9.34), (8, 1.2)):
        starts = templates._steps(count, duration)
        assert len(starts) == count
        assert starts == sorted(starts)
        assert starts[0] >= 0.0
        # 0.55 is the entry animation length; the last item must have landed by
        # the cut. (The reserved ``hold`` is 0.5s, hence the small tolerance.)
        assert starts[-1] + 0.55 <= duration + 1e-6


def test_stagger_lead_in_stays_short_on_long_scenes():
    """A long scene must not push the last item's entry out of the first seconds."""
    for count in range(2, 9):
        assert templates._steps(count, 10.0)[-1] <= 2.5


def test_render_page_escapes_text():
    sb = Storyboard.from_dict([{"template": "title", "title": "<script>x</script>"}])
    page = render_page(sb, sb.timeline())
    assert "<script>x</script>" not in page
    assert "&lt;script&gt;" in page


def test_portrait_gets_its_own_layout():
    """9:16 换一套版式：容器单位 + 纵向流向，横屏那份 CSS 不能跟过来。"""
    sb = Storyboard(
        scenes=[Scene(template="flow", nodes=["甲", "乙"])],
        width=1080,
        height=1920,
    )
    page = render_page(sb, sb.timeline())
    assert "portrait" in page
    assert "container-type: size" in page
    assert "cqw" in page
    # 竖屏的流向要竖着走，否则三个节点会挤出画面
    assert "flex-direction: column" in page
    assert "rotate(90deg)" in page, "竖屏箭头要朝下"


def test_landscape_keeps_the_old_page():
    sb = Storyboard(scenes=[Scene(template="title", title="横屏")], width=1920, height=1080)
    page = render_page(sb, sb.timeline())
    assert "landscape" in page
    assert "container-type: size" not in page, "横屏不该吃到竖屏的容器查询"


def test_camera_layer_wraps_every_scene():
    """镜头推进靠 #camera 这层，内容漏在它外面就推不动。"""
    sb = Storyboard(scenes=[Scene(template="title", title="标题")])
    page = render_page(sb, sb.timeline())
    assert 'id="camera"' in page
    assert "camera.style.transform" in page
    # 场景必须包在 camera 里
    camera_at = page.index('id="camera"')
    assert page.index('class="scene tpl-title"') > camera_at


def test_background_layers_are_actually_emitted():
    """装饰层以前只有 glow 被默认启用，粒子/扫描线写了却从没上过场。"""
    neon = Storyboard(scenes=[Scene(template="title", title="标题")], style="neon")
    page = render_page(neon, neon.timeline())
    assert 'class="particle"' in page
    assert "deco-grid" in page
    assert "deco-scan" in page


def test_write_preview_creates_file(tmp_path: Path):
    path = write_preview(_sb(), tmp_path)
    assert path.exists() and path.suffix == ".html"


# ── audio helpers ───────────────────────────────────────────────


def test_srt_formatting(tmp_path: Path):
    path = composer.write_srt(
        [(0.0, 1.5, "第一句"), (65.25, 66.0, "第二句")], tmp_path / "a.srt"
    )
    text = path.read_text(encoding="utf-8")
    assert "00:00:00,000 --> 00:00:01,500" in text
    assert "00:01:05,250 --> 00:01:06,000" in text


def test_concat_wav_places_clip_at_offset(tmp_path: Path):
    clip = _make_wav(tmp_path / "clip.wav", 0.5, amplitude=5000)
    out = tmp_path / "mix.wav"
    composer.concat_wav([(1.0, clip)], 2.0, out)

    assert composer.wav_duration(out) == pytest.approx(2.0, abs=0.01)
    with wave.open(str(out), "rb") as w:
        data = w.readframes(w.getnframes())

    frame = composer.CHANNELS * composer.SAMPLE_WIDTH
    onset = int(1.0 * composer.SAMPLE_RATE) * frame
    silence = data[0:frame]
    voiced = data[onset : onset + int(0.2 * composer.SAMPLE_RATE) * frame]
    assert silence == b"\x00" * frame
    assert any(byte != 0 for byte in voiced)
