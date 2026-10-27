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

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from gangge.layer4_tools.storyboard import composer, renderer, tts
from gangge.layer4_tools.storyboard.schema import Storyboard
from gangge.layer4_tools.storyboard.templates import render_page

logger = logging.getLogger(__name__)

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


async def build_video(
    storyboard: Storyboard,
    out_dir: Path | str,
    *,
    narration: bool = True,
    keep_frames: bool = False,
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
    html_path = out_dir / "storyboard.html"
    html_path.write_text(render_page(storyboard, slots), encoding="utf-8")

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


def write_preview(storyboard: Storyboard, out_dir: Path | str) -> Path:
    """Emit the HTML alone — opens in a browser, no encoder needed.

    Useful as a fast look before paying for narration and frame capture.
    """
    slots = storyboard.timeline()
    html_path = Path(out_dir) / "storyboard.html"
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(render_page(storyboard, slots), encoding="utf-8")
    return html_path
