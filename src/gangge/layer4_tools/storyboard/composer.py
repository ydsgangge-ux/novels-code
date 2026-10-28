"""Audio/video assembly — the only module that shells out to ffmpeg.

Everything else in the pipeline deals in files and seconds. Keeping the
subprocess boundary here means the rendering path stays testable without a
video encoder, and a missing ffmpeg produces one clear error instead of a
trail of half-written files.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Iterable, Sequence

logger = logging.getLogger(__name__)

# Canonical audio format. Every clip is converted to this before concatenation
# so offsets are plain sample arithmetic instead of a filter graph.
SAMPLE_RATE = 48_000
CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit PCM


class FFmpegMissing(RuntimeError):
    """No usable ffmpeg binary on this machine."""


def ffmpeg_exe() -> str:
    """Path to ffmpeg: ``PATH`` first, then the copy bundled with imageio."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:  # noqa: BLE001 - normalised below
        raise FFmpegMissing(
            "找不到 ffmpeg：请安装 ffmpeg 并加入 PATH，或 pip install imageio-ffmpeg"
        ) from e


def _run(args: Sequence[str]) -> None:
    proc = subprocess.run(
        list(args), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    if proc.returncode != 0:
        text = proc.stderr.decode("utf-8", "ignore")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        # The tail is usually encoder progress noise; the diagnosis is a line
        # containing "error"/"failed"/"invalid". Prefer those, fall back to tail.
        important = [
            line for line in lines
            if re.search(r"error|failed|invalid|no such|denied|cannot|malloc", line, re.I)
        ]
        detail = important[-6:] if important else lines[-6:]
        raise RuntimeError("ffmpeg 失败:\n" + "\n".join(detail))


# ── audio ───────────────────────────────────────────────────────


def to_wav(src: Path, dst: Path) -> Path:
    """Transcode any audio file to the canonical wav format."""
    _run([
        ffmpeg_exe(), "-y", "-i", str(src),
        "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
        "-c:a", "pcm_s16le", str(dst),
    ])
    return dst


def wav_duration(path: Path) -> float:
    """Exact duration from the wav header — no decoding, no guessing."""
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate() or SAMPLE_RATE
        return round(w.getnframes() / float(rate), 4)


def concat_wav(
    segments: Iterable[tuple[float, Path | None]],
    total: float,
    dst: Path,
) -> float:
    """Lay each clip at its timeline offset over a silent bed.

    Doing this in Python rather than ``adelay``/``amix`` keeps the result
    sample-exact and avoids ffmpeg silently resampling one branch of the mix.
    """
    frame_bytes = CHANNELS * SAMPLE_WIDTH
    total_frames = max(0, int(round(total * SAMPLE_RATE)))
    buf = bytearray(total_frames * frame_bytes)

    for offset, path in segments:
        if path is None or not Path(path).exists():
            continue
        with wave.open(str(path), "rb") as w:
            if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (
                SAMPLE_RATE, CHANNELS, SAMPLE_WIDTH,
            ):
                logger.warning("[composer] skipping mismatched clip: %s", path)
                continue
            data = w.readframes(w.getnframes())

        start = int(round(max(0.0, offset) * SAMPLE_RATE)) * frame_bytes
        if start >= len(buf):
            continue
        chunk = data[: len(buf) - start]
        buf[start : start + len(chunk)] = chunk

    with wave.open(str(dst), "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(bytes(buf))
    return round(total_frames / SAMPLE_RATE, 4)


def write_silence(duration: float, dst: Path) -> float:
    """A silent track, used when narration is unavailable but video is wanted."""
    return concat_wav([], duration, dst)


# ── subtitles ───────────────────────────────────────────────────


def _ts(seconds: float) -> str:
    ms = int(round(max(0.0, seconds) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(cues: Sequence[tuple[float, float, str]], dst: Path) -> Path:
    blocks = [
        f"{i}\n{_ts(start)} --> {_ts(end)}\n{text.strip()}\n"
        for i, (start, end, text) in enumerate(cues, 1)
        if text.strip()
    ]
    dst.write_text("\n".join(blocks), encoding="utf-8")
    return dst


# ── video ───────────────────────────────────────────────────────


def mux(
    frames_dir: Path,
    fps: int,
    audio: Path | None,
    dst: Path,
    crf: int = 20,
) -> Path:
    """Turn a numbered PNG sequence (plus optional audio) into an H.264 mp4.

    Two escalating parameter sets: a busy machine can fail x264's internal
    allocation ("malloc of size ... failed") and lose the whole encode, so a
    lighter single-threaded pass is attempted before giving up. Losing a little
    compression efficiency beats losing a three-minute render.
    """
    pattern = frames_dir / "%05d.png"
    if not any(frames_dir.glob("*.png")):
        raise RuntimeError(f"帧目录为空: {frames_dir}")

    base = [ffmpeg_exe(), "-y", "-framerate", str(fps), "-i", str(pattern)]
    if audio is not None and Path(audio).exists():
        base += ["-i", str(audio), "-c:a", "aac", "-b:a", "192k", "-shortest"]

    attempts = [
        # veryfast is indistinguishable from medium on flat UI frames. The real
        # knob is memory: x264 holds every lookahead frame plus its mbtree stats
        # (a 1080p yuv420 frame is ~3 MB), and on a long encode that reservation
        # fails near the end — observed as "malloc of size 7088704 failed" after
        # ~900 frames, which throws away the whole render. Short lookahead and
        # an explicit thread cap keep the peak flat.
        ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
         "-threads", "4", "-x264-params", "rc-lookahead=10",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart"],
        # Last resort: two threads, no B-frames, no second moov pass.
        ["-c:v", "libx264", "-preset", "ultrafast", "-crf", str(crf + 3),
         "-pix_fmt", "yuv420p", "-threads", "2",
         "-x264-params", "ref=1:bframes=0:rc-lookahead=0"],
    ]

    last_error: Exception | None = None
    for extra in attempts:
        try:
            _run(base + extra + [str(dst)])
            return dst
        except RuntimeError as e:
            last_error = e
            logger.warning("[composer] mux failed, retrying lighter params")
    raise last_error if last_error else RuntimeError("视频合成失败")
