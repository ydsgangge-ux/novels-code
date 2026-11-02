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

from gangge.layer4_tools.storyboard.pages import PAGE_KINDS, read_page, template_path
from gangge.layer4_tools.storyboard.pipeline import (
    BuildResult,
    build_video,
    save_storyboard,
    write_page,
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
from gangge.layer4_tools.storyboard.script import chunk_script, storyboard_from_script
from gangge.layer4_tools.storyboard.showcase import render_showcase, write_showcase
from gangge.layer4_tools.storyboard.site import SiteShot, build_site_page, capture_site
from gangge.layer4_tools.storyboard.templates import render_page
from gangge.layer4_tools.storyboard.tts import Clip, create_tts

__all__ = [
    "DEFAULT_THEME",
    "PAGE_KINDS",
    "TEMPLATE_NAMES",
    "BuildResult",
    "Clip",
    "Scene",
    "SiteShot",
    "Storyboard",
    "StoryboardError",
    "build_site_page",
    "build_video",
    "capture_site",
    "chunk_script",
    "create_tts",
    "estimate_speech_seconds",
    "read_page",
    "render_page",
    "render_showcase",
    "save_storyboard",
    "storyboard_from_script",
    "template_path",
    "write_page",
    "write_preview",
    "write_showcase",
]
