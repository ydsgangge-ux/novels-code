"""Storyboard → files on disk.

The order is deliberate and the whole design rests on it:

1. synthesize every narration line → shot lengths become facts, not guesses
2. lay out the timeline            → shots are placed by those facts
3. build the page                  → the page is a function of the timeline
4. capture frames                  → deterministic, no wall clock involved
5. mix audio and mux               → one mp4

Each step degrades instead of exploding: no ffmpeg still yields frames + HTML,
no playwright still yields audio + HTML + subtitles. A half-finished run that
tells you what is missing beats a stack trace after two minutes of TTS.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from gangge.layer4_tools.storyboard import composer, renderer, tts
from gangge.layer4_tools.storyboard.schema import Scene, Storyboard
from gangge.layer4_tools.storyboard.templates import render_page

logger = logging.getLogger(__name__)

#: 能拍成视频的三种页面：朴素分镜页、3D 展示页、3D 展示页里装真实网站截图。
PAGE_KINDS: tuple[str, ...] = ("storyboard", "showcase", "site")

# A shot ends when the voice ends; this keeps the last syllable from being
# clipped and gives the eye somewhere to rest before the cut.
TAIL_PADDING = 0.45


@dataclass
class BuildResult:
    """Everything one run produced, including what it could not produce."""

    html: Path
    duration: float
    fps: int
    scenes: int
    tts_engine: str
    video: Path | None = None
    audio: Path | None = None
    srt: Path | None = None
    frames_dir: Path | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "video": str(self.video) if self.video else "",
            "audio": str(self.audio) if self.audio else "",
            "subtitles": str(self.srt) if self.srt else "",
            "html": str(self.html),
            "frames_dir": str(self.frames_dir) if self.frames_dir else "",
            "duration": self.duration,
            "fps": self.fps,
            "scenes": self.scenes,
            "tts_engine": self.tts_engine,
            "notes": list(self.notes),
        }


async def _synthesize(
    engine: tts.BaseTTS,
    text: str,
    out_path: Path,
    notes: list[str],
) -> tts.Clip:
    if not text.strip():
        return tts.Clip(duration=1.6, engine=engine.name, note="该镜头无台词")
    try:
        return await engine.synthesize(text, out_path)
    except Exception as e:  # noqa: BLE001 - network/engine failures are expected
        logger.warning("[storyboard] TTS failed: %s", e)
        notes.append(f"配音失败，已降级为静音 + 估算时长：{e}")
        return await tts.SilentTTS().synthesize(text, out_path)


def write_page(
    storyboard: Storyboard,
    slots: Sequence[tuple[Scene, float, float]],
    out_dir: Path | str,
    *,
    page: str = "storyboard",
    source_url: str = "",
    site_shot: object | None = None,
) -> Path:
    """写出要拍的那张页面，返回它的路径。

    showcase / site 模式下仍然留一份朴素版 ``storyboard.html``——它最轻、最容易
    对照，展示页拍坏了可以直接退回去。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    plain = out_dir / "storyboard.html"
    plain.write_text(render_page(storyboard, slots), encoding="utf-8")
    if page == "storyboard":
        return plain
    # 到这里才导入：showcase 自己也带命令行入口，模块级导入会让
    # ``python -m ...showcase`` 触发 runpy 的重复导入告警。
    from gangge.layer4_tools.storyboard.showcase import (
        CHAPTER_VH,
        TAIL_VH,
        render_showcase,
    )

    inner_html = None
    scroll_vh = None
    if page == "site":
        if site_shot is None:
            raise ValueError("page='site' 需要 site_shot：先用 site.capture_site 截图")
        from gangge.layer4_tools.storyboard.site import SITE_SCREENS, shot_inner_page

        total = round(slots[-1][2], 3) if slots else 0.0
        # 整段视频就是一次从上到下的平移：t 从 0 走到 total，图走完全长
        inner_html = shot_inner_page(site_shot, 0.0, max(0.2, total))
        scroll_vh = SITE_SCREENS * CHAPTER_VH + TAIL_VH

    html = render_showcase(
        storyboard,
        slots,
        source_url=source_url,
        inner_html=inner_html,
        scroll_vh=scroll_vh,
    )
    fancy = out_dir / ("site.html" if page == "site" else "showcase.html")
    fancy.write_text(html, encoding="utf-8")
    return fancy


async def build_video(
    storyboard: Storyboard,
    out_dir: Path | str,
    *,
    narration: bool = True,
    keep_frames: bool = False,
    page: str = "storyboard",
    source_url: str = "",
    site_shot: Any = None,
    on_progress: Callable[[str], None] | None = None,
) -> BuildResult:
    """Render ``storyboard`` into ``out_dir`` and report what was produced."""

    def report(message: str) -> None:
        logger.info("[storyboard] %s", message)
        if on_progress is not None:
            on_progress(message)

    out_dir = Path(out_dir)
    audio_dir = out_dir / "audio"
    frames_dir = out_dir / "frames"
    audio_dir.mkdir(parents=True, exist_ok=True)

    notes: list[str] = []
    engine = tts.create_tts(storyboard.voice, enabled=narration)
    report(f"配音引擎: {engine.name}")

    # ── 1. narration decides the lengths ────────────────────────
    clips: list[tts.Clip] = []
    durations: list[float] = []
    for i, scene in enumerate(storyboard.scenes):
        report(f"配音 {i + 1}/{len(storyboard.scenes)}")
        clip = await _synthesize(
            engine, scene.spoken_text(), audio_dir / f"scene_{i:02d}.wav", notes
        )
        clips.append(clip)
        durations.append(round(clip.duration + TAIL_PADDING, 3))

    # ── 2. timeline ─────────────────────────────────────────────
    slots = storyboard.timeline(durations)
    total = round(slots[-1][2], 3)

    # ── 3. page ─────────────────────────────────────────────────
    save_storyboard(storyboard, out_dir)
    html_path = write_page(
        storyboard,
        slots,
        out_dir,
        page=page,
        source_url=source_url,
        site_shot=site_shot,
    )

    # ── 4. audio bed + subtitles ────────────────────────────────
    audio_path: Path | None = None
    try:
        composer.concat_wav(
            [(slots[i][1], clip.path) for i, clip in enumerate(clips)],
            total,
            audio_dir / "narration.wav",
        )
        audio_path = audio_dir / "narration.wav"
    except composer.FFmpegMissing as e:
        notes.append(str(e))

    cues: list[tuple[float, float, str]] = []
    for i, clip in enumerate(clips):
        offset, shot_end = slots[i][1], slots[i][2]
        for start, end, text in clip.cues:
            cues.append(
                (round(offset + start, 3), round(min(offset + end, shot_end), 3), text)
            )
    srt_path = composer.write_srt(cues, out_dir / "subtitles.srt") if cues else None

    # ── 5. frames → mp4 ─────────────────────────────────────────
    captured: Path | None = None
    try:
        report("渲染帧")
        await renderer.render_frames(
            html_path,
            frames_dir,
            fps=storyboard.fps,
            duration=total,
            width=storyboard.width,
            height=storyboard.height,
        )
        captured = frames_dir
    except renderer.RenderError as e:
        notes.append(str(e))

    video_path: Path | None = None
    if captured is not None:
        try:
            report("合成视频")
            video_path = composer.mux(
                captured, storyboard.fps, audio_path, out_dir / "video.mp4"
            )
        except (composer.FFmpegMissing, RuntimeError) as e:
            notes.append(f"视频合成失败，帧序列已保留：{e}")

    if video_path is not None and not keep_frames:
        shutil.rmtree(frames_dir, ignore_errors=True)
        captured = None

    report("完成")
    return BuildResult(
        html=html_path,
        duration=total,
        fps=storyboard.fps,
        scenes=len(storyboard.scenes),
        tts_engine=engine.name,
        video=video_path,
        audio=audio_path if audio_path and audio_path.exists() else None,
        srt=srt_path,
        frames_dir=captured,
        notes=notes,
    )


def save_storyboard(storyboard: Storyboard, out_dir: Path | str) -> Path:
    """Persist the storyboard as JSON — the one artifact worth editing by hand."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "storyboard.json"
    path.write_text(
        json.dumps(storyboard.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def write_preview(
    storyboard: Storyboard,
    out_dir: Path | str,
    *,
    page: str = "storyboard",
    source_url: str = "",
    site_shot: Any = None,
) -> Path:
    """Emit the HTML alone — opens in a browser, no encoder needed.

    Also writes ``storyboard.json``: the point of a preview is to decide what to
    change, and that means handing back something editable.
    """
    slots = storyboard.timeline()
    save_storyboard(storyboard, out_dir)
    return write_page(
        storyboard,
        slots,
        out_dir,
        page=page,
        source_url=source_url,
        site_shot=site_shot,
    )
