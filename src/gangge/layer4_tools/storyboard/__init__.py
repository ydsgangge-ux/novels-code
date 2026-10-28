"""Storyboard → narrated demo video.

The pipeline is deliberately lopsided: narration is synthesized first and the
picture follows it. A model only ever produces :class:`~.schema.Storyboard`
data — template names and text — never animation code, which is what keeps the
output predictable and the cost of a re-render near zero.

    >>> from gangge.layer4_tools.storyboard import Storyboard, build_video
    >>> sb = Storyboard.from_dict({"scenes": [
    ...     {"template": "title", "title": "三步做出演示动画"},
    ... ]})
"""

from __future__ import annotations

from gangge.layer4_tools.storyboard.pipeline import (
    BuildResult,
    build_video,
    save_storyboard,
    write_preview,
)
from gangge.layer4_tools.storyboard.schema import (
    DEFAULT_THEME,
    TEMPLATE_NAMES,
    Scene,
    Storyboard,
    StoryboardError,
    estimate_speech_seconds,
)
from gangge.layer4_tools.storyboard.templates import render_page
from gangge.layer4_tools.storyboard.tts import Clip, create_tts

__all__ = [
    "DEFAULT_THEME",
    "TEMPLATE_NAMES",
    "BuildResult",
    "Clip",
    "Scene",
    "Storyboard",
    "StoryboardError",
    "build_video",
    "create_tts",
    "estimate_speech_seconds",
    "render_page",
    "save_storyboard",
    "write_preview",
]
