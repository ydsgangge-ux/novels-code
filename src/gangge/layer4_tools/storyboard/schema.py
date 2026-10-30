"""Storyboard schema — the narrow contract between a model and the renderer.

A model is only ever asked for *this*: which template, what text, how long. It
never writes CSS, SVG or timeline code. That is the whole point — the visual
result is a pure function of this data plus ``t``, so it is reproducible,
reviewable and cheap to regenerate.

Nothing here knows about browsers, audio or ffmpeg; those live one layer out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Templates the renderer can draw. A storyboard naming anything else is rejected
# up front, rather than rendering a blank frame at t=14.2s with no explanation.
TEMPLATE_NAMES: tuple[str, ...] = ("title", "bullets", "flow", "quote", "outro")

# Visual presets. "Which look" is a config value, not generated code — that is
# what lets one word ("华丽一点") change the whole video.
STYLE_NAMES: tuple[str, ...] = ("dark", "neon", "paper", "glass", "brutal")

# Background ornaments, all driven by t so frame capture stays deterministic.
DECOR_NAMES: tuple[str, ...] = ("glow", "grid", "grain", "particles", "scanline")

DEFAULT_THEME: dict[str, str] = {
    "bg": "#0b0f16",
    "fg": "#f0f4fa",
    "muted": "#8fa1b8",
    "accent": "#5b8cff",
}

# Rough Chinese narration pace, used only when no audio is available.
_CHARS_PER_SECOND = 4.5
# Guardrail for a single TTS request; longer text gets truncated, not sent.
_MAX_SPOKEN_CHARS = 600


class StoryboardError(ValueError):
    """Raised when a storyboard cannot be rendered as given."""


def estimate_speech_seconds(text: str) -> float:
    """Seconds of narration for ``text`` at a normal Chinese pace."""
    chars = len((text or "").replace(" ", ""))
    return round(max(2.0, min(20.0, chars / _CHARS_PER_SECOND + 0.9)), 2)


@dataclass
class Scene:
    """One full-screen shot."""

    template: str
    title: str = ""
    text: str = ""
    items: list[str] = field(default_factory=list)
    nodes: list[str] = field(default_factory=list)
    # 0 means "derive it from the narration length"; the pipeline fills this in.
    duration: float = 0.0
    voiceover: str = ""

    def spoken_text(self) -> str:
        """The narration for this shot, or ``""`` when it should stay silent."""
        if self.voiceover:
            return self.voiceover.strip()[:_MAX_SPOKEN_CHARS]
        if self.text:
            return self.text.strip()[:_MAX_SPOKEN_CHARS]
        parts = [p for p in (self.title, *self.items, *self.nodes) if p]
        return "，".join(parts)[:_MAX_SPOKEN_CHARS]

    def estimated_duration(self) -> float:
        """Fallback length when TTS is unavailable: read the text out loud."""
        return estimate_speech_seconds(self.spoken_text())


@dataclass
class Storyboard:
    """An ordered list of shots plus the settings shared by all of them."""

    scenes: list[Scene]
    title: str = ""
    fps: int = 30
    width: int = 1920
    height: int = 1080
    voice: str = "zh-CN-XiaoxiaoNeural"
    # Colour overrides applied on top of the style preset; empty means "use the
    # preset as-is". Keeping it empty by default is what makes `style` work.
    theme: dict[str, str] = field(default_factory=dict)
    gap: float = 0.25  # breathing room between shots
    style: str = "dark"
    decor: list[str] = field(default_factory=list)

    # ── construction ────────────────────────────────────────────

    @classmethod
    def from_dict(cls, data: Any) -> "Storyboard":
        """Parse and validate a model-authored storyboard.

        Accepts either ``{"scenes": [...]}`` or a bare ``[...]`` of scenes. Every
        error message names the offending scene index, because these strings are
        fed straight back to the model for a retry.
        """
        if isinstance(data, list):
            data = {"scenes": data}
        if not isinstance(data, dict):
            raise StoryboardError("分镜必须是对象（含 scenes 数组）或场景数组")

        raw_scenes = data.get("scenes")
        if not isinstance(raw_scenes, list) or not raw_scenes:
            raise StoryboardError("scenes 必须是非空数组")

        scenes: list[Scene] = []
        for i, raw in enumerate(raw_scenes):
            if not isinstance(raw, dict):
                raise StoryboardError(f"第 {i + 1} 个场景必须是对象")
            scene = cls._parse_scene(raw, i)
            scenes.append(scene)

        theme: dict[str, str] = {}
        if isinstance(data.get("theme"), dict):
            for key, value in data["theme"].items():
                if key in DEFAULT_THEME and isinstance(value, str) and value.startswith("#"):
                    theme[key] = value

        style = str(data.get("style") or "dark").strip().lower()
        if style not in STYLE_NAMES:
            raise StoryboardError(
                f"style 无效: {style!r}；可用: {', '.join(STYLE_NAMES)}"
            )
        # Unknown decorations are dropped rather than rejected: they are garnish,
        # and a typo should not cost the caller a whole retry.
        decor = [d for d in _str_list(data.get("decor")) if d in DECOR_NAMES]

        fps = _clamp_int(data.get("fps", 30), 10, 60, 30)
        return cls(
            scenes=scenes,
            title=str(data.get("title") or ""),
            fps=fps,
            width=_clamp_int(data.get("width", 1920), 640, 3840, 1920),
            height=_clamp_int(data.get("height", 1080), 360, 2160, 1080),
            voice=str(data.get("voice") or "zh-CN-XiaoxiaoNeural"),
            theme=theme,
            gap=_clamp_float(data.get("gap", 0.25), 0.0, 2.0, 0.25),
            style=style,
            decor=decor,
        )

    @staticmethod
    def _parse_scene(raw: dict[str, Any], index: int) -> Scene:
        template = str(raw.get("template") or "").strip()
        if template not in TEMPLATE_NAMES:
            raise StoryboardError(
                f"第 {index + 1} 个场景的 template 无效: {template!r}；"
                f"可用: {', '.join(TEMPLATE_NAMES)}"
            )

        items = _str_list(raw.get("items"))
        nodes = _str_list(raw.get("nodes"))
        if template == "bullets" and not items:
            raise StoryboardError(f"第 {index + 1} 个场景是 bullets，必须提供 items 数组")
        if template == "flow" and len(nodes) < 2:
            raise StoryboardError(f"第 {index + 1} 个场景是 flow，nodes 至少需要 2 个")

        return Scene(
            template=template,
            title=str(raw.get("title") or ""),
            text=str(raw.get("text") or ""),
            items=items,
            nodes=nodes,
            duration=max(0.0, _clamp_float(raw.get("duration", 0.0), 0.0, 60.0, 0.0)),
            voiceover=str(raw.get("voiceover") or ""),
        )

    # ── derived ─────────────────────────────────────────────────

    def timeline(self, durations: list[float] | None = None) -> list[tuple[Scene, float, float]]:
        """``[(scene, start, end)]`` in global seconds.

        ``durations`` overrides the per-scene length (the pipeline passes the
        measured narration length, which is what keeps audio and picture in
        sync). Missing entries fall back to ``scene.duration`` then to the
        text-based estimate.
        """
        slots: list[tuple[Scene, float, float]] = []
        cursor = 0.0
        for i, scene in enumerate(self.scenes):
            if durations and i < len(durations) and durations[i] > 0:
                length = durations[i]
            elif scene.duration > 0:
                length = scene.duration
            else:
                length = scene.estimated_duration()
            slots.append((scene, round(cursor, 3), round(cursor + length, 3)))
            cursor += length + self.gap
        return slots

    @property
    def total_duration(self) -> float:
        return self.timeline()[-1][2]

    def to_dict(self) -> dict[str, Any]:
        """Round-trippable form, also used as the tool's return payload."""
        return {
            "title": self.title,
            "fps": self.fps,
            "width": self.width,
            "height": self.height,
            "voice": self.voice,
            "gap": self.gap,
            "theme": dict(self.theme),
            "scenes": [
                {
                    "template": s.template,
                    "title": s.title,
                    "text": s.text,
                    "items": list(s.items),
                    "nodes": list(s.nodes),
                    "duration": s.duration,
                    "voiceover": s.voiceover,
                }
                for s in self.scenes
            ],
        }


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _clamp_float(value: Any, low: float, high: float, default: float) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return default
