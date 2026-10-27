"""Narration synthesis — audio first, picture second.

Every shot's on-screen length is derived from its narration. Doing it the other
way round (fix the timing, then fit the voice) is the classic reason these
pipelines drift out of sync by the third scene, so :class:`Clip.duration` is the
authority the renderer obeys.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from gangge.layer4_tools.storyboard import composer
from gangge.layer4_tools.storyboard.schema import estimate_speech_seconds

logger = logging.getLogger(__name__)

# Punctuation that ends a subtitle cue. Commas are deliberately absent: Chinese
# narration uses them mid-clause, and splitting there produces two-line flashes.
_SENTENCE_END = "。！？；.!?;"
_MAX_CUE_CHARS = 34


@dataclass
class Clip:
    """One synthesized narration line."""

    path: Path | None = None          # canonical wav, None for silence
    duration: float = 0.0             # authoritative shot length
    cues: list[tuple[float, float, str]] = field(default_factory=list)
    engine: str = "silent"
    note: str = ""


class BaseTTS:
    name = "base"

    async def synthesize(self, text: str, out_path: Path) -> Clip:
        raise NotImplementedError


class SilentTTS(BaseTTS):
    """Fallback: no audio, length estimated from the text itself."""

    name = "silent"

    async def synthesize(self, text: str, out_path: Path) -> Clip:
        return Clip(
            path=None,
            duration=estimate_speech_seconds(text),
            engine="silent",
            note="未启用配音，时长按文字估算",
        )


class EdgeTTS(BaseTTS):
    """Microsoft Edge neural voices — free, no GPU, emits word timings."""

    name = "edge-tts"

    def __init__(self, voice: str = "zh-CN-XiaoxiaoNeural", rate: str = "+0%"):
        self.voice = voice
        self.rate = rate

    async def synthesize(self, text: str, out_path: Path) -> Clip:
        import edge_tts

        audio = bytearray()
        cues: list[tuple[float, float, str]] = []
        words: list[str] = []
        cue_start: float | None = None
        last_end = 0.0

        async for chunk in edge_tts.Communicate(text, self.voice, rate=self.rate).stream():
            kind = chunk.get("type")
            if kind == "audio":
                audio.extend(chunk["data"])
            elif kind == "SentenceBoundary":
                # edge-tts >= 7.3 reports whole sentences; use them verbatim
                # rather than re-splitting a boundary the engine already found.
                start = float(chunk.get("offset", 0)) / 1e7
                end = start + float(chunk.get("duration", 0)) / 1e7
                sentence = str(chunk.get("text") or "").strip()
                last_end = max(last_end, end)
                if sentence:
                    cues.append((round(start, 3), round(end, 3), sentence))
            elif kind == "WordBoundary":
                # Older versions report per word: accumulate into cues.
                start = float(chunk.get("offset", 0)) / 1e7
                end = start + float(chunk.get("duration", 0)) / 1e7
                word = str(chunk.get("text") or "")
                if cue_start is None:
                    cue_start = start
                words.append(word)
                last_end = max(last_end, end)
                joined = "".join(words)
                if (word and word[-1] in _SENTENCE_END) or len(joined) >= _MAX_CUE_CHARS:
                    cues.append((round(cue_start or start, 3), round(end, 3), joined))
                    words = []
                    cue_start = None

        if words and cue_start is not None:
            cues.append((round(cue_start, 3), round(last_end, 3), "".join(words)))

        if not audio:
            raise RuntimeError("edge-tts 未返回音频数据")

        mp3 = out_path.with_suffix(".mp3")
        mp3.write_bytes(bytes(audio))
        try:
            wav = composer.to_wav(mp3, out_path.with_suffix(".wav"))
        finally:
            mp3.unlink(missing_ok=True)

        return Clip(
            path=wav,
            duration=composer.wav_duration(wav),
            cues=cues,
            engine="edge-tts",
        )


def create_tts(voice: str, enabled: bool = True) -> BaseTTS:
    """Pick a narration engine, degrading to silence rather than failing."""
    if not enabled:
        return SilentTTS()
    try:
        import edge_tts  # noqa: F401 - presence check only
    except ImportError:
        logger.warning("[tts] edge-tts 未安装，改为静音输出")
        return SilentTTS()
    return EdgeTTS(voice)
