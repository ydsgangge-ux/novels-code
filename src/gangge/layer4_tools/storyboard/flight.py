"""时间线飞行页：一条光路在 3D 空间里延伸，镜头跟着飞，到站停下讲一段。

和人物页（:mod:`~.person`）的血缘相同——都是"一条线穿过一串节点"，但这一页是
真的三维场景，光路在空间里起伏，镜头绕着节点转：

* ``page='flight'``    夜色空间：网格地面 + 光尘 + 发光光管，适合人物、公司、
  项目的大事记（科技感、仪式感强）；
* ``page='miniature'`` 白天迷你沙盘：小路从乡村木屋走向城市高楼，箭头沿路前进，
  到站点落下图钉，治愈系、适合品牌故事和旅行回顾。

时间轴不靠字数猜：分镜的每个镜头都是一个站点，配音说了多久，这一站就停多久。
页面里每个节点直接收 ``start``/``end`` 秒数（见 ``pages/flight.html`` 的
``build()``），所以画面和旁白天然对齐。

Three.js r128 随包分发（``pages/vendor/three.min.js``），渲染时内联进 HTML：
产物是单文件、不联外网，和其余页面一样可以离线逐帧拍。
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Sequence

from gangge.layer4_tools.storyboard.pages import PAGE_DIR, read_page
from gangge.layer4_tools.storyboard.person import PHOTO_EXTS, _photo_entry
from gangge.layer4_tools.storyboard.schema import Scene, Storyboard

logger = logging.getLogger(__name__)

#: 页面名 → 场景世界。两页共用一份引擎，只有世界（和它带的默认镜头）不同。
WORLD_OF_PAGE: dict[str, str] = {
    "flight": "grid",       # 夜色网格、光尘
    "miniature": "miniature",  # 白天迷你沙盘
}

#: 五种风格 → 引擎里的配色预设。glass 跟 neon 同一套冷光，brutal 借 ember 的暖橙。
PRESET_OF_STYLE: dict[str, str] = {
    "dark": "night",
    "neon": "aurora",
    "glass": "aurora",
    "paper": "paper",
    "brutal": "ember",
}

#: 迷你沙盘永远是白天的 meadow：它讲的不是"在星空里飞行"，是"从小木屋走到高楼"。
MINIATURE_PRESET = "meadow"

#: 上一站讲完到这一站站稳，光要走多久（秒）。太短像瞬移，太长丢节奏。
TRAVEL = 2.4
#: 最后一个站点讲完，镜头拉远收尾要多久。
END_MOVE = 1.2

#: 光路上那行小标签最多几个字——再长就压到相邻站点上了。
_SHORT_CHARS = 6

#: 分镜的配色词汇 → 引擎的配色词汇。其余键同名，直接透传。
_THEME_KEYS: dict[str, str] = {"fg": "ink", "muted": "dim", "accent2": "line2"}


class FlightPageError(ValueError):
    """飞行页渲染不了（一个节点都没有、three.js 没随包发出去）。"""


# ── storyboard → STORY ──────────────────────────────────────────


def _node_of(scene: Scene, slot: tuple[float, float], base_dir: Path | None) -> dict[str, Any]:
    """一个镜头 → 一个站点。字段名跟着引擎走：date / label / title / text。"""
    title = scene.title or scene.text or ""
    body = scene.text if scene.title else ""
    # bullets / flow 的正文在 items / nodes 里，合并进来而不是丢掉——
    # 模型不用为飞行页另学一套模板。
    extra = scene.items or scene.nodes
    if extra:
        joined = " · ".join(extra)
        body = f"{body} {joined}".strip() if body else joined
    if not title:
        title, body = body, ""

    photos = [
        entry
        for entry in (_photo_entry(p, base_dir) for p in scene.photos)
        if entry is not None
    ]
    return {
        "date": scene.year,
        "label": re.sub(r"\s+", "", title)[:_SHORT_CHARS],
        "title": title,
        "text": body,
        "photos": photos,
        "start": round(slot[0], 3),
        "end": round(slot[1], 3),
    }


def _theme(storyboard: Storyboard, page: str) -> dict[str, str]:
    """页面配色：先定预设，再叠用户显式的 theme 覆盖。"""
    preset = (
        MINIATURE_PRESET
        if page == "miniature"
        else PRESET_OF_STYLE.get(storyboard.style, "night")
    )
    theme: dict[str, str] = {"preset": preset}
    for key, value in storyboard.theme.items():
        if not value:
            continue
        theme[_THEME_KEYS.get(key, key)] = value
    return theme


def build_story(
    storyboard: Storyboard,
    slots: Sequence[tuple[Scene, float, float]],
    *,
    page: str = "flight",
    base_dir: Path | str | None = None,
) -> dict[str, Any]:
    """把分镜 + 时间轴翻成引擎认识的 ``STORY``。

    开场镜头演成名牌，结语镜头演成片尾字幕，中间那些才是站点——和人物页同一套
    约定，模型写的东西不用改。整条分镜只有一个镜头时，就把它自己当成唯一站点。
    """
    if page not in WORLD_OF_PAGE:
        raise FlightPageError(f"未知飞行页: {page!r}，可选: {', '.join(WORLD_OF_PAGE)}")

    base = Path(base_dir) if base_dir else None
    pairs = list(zip(storyboard.scenes, [(s, e) for _, s, e in slots]))

    head = None
    tail = None
    if pairs and pairs[0][0].template == "title":
        head = pairs.pop(0)
    if pairs and pairs[-1][0].template == "outro":
        tail = pairs.pop()

    if not pairs:
        fallback = head or tail
        if fallback is None:
            raise FlightPageError("分镜里没有任何镜头")
        pairs = [fallback]

    total = round(slots[-1][2], 3) if slots else 0.0
    first_start = round(pairs[0][1][0], 3)
    # 名牌演到开场镜头结束为止；没有开场镜头就快速交代一两秒，别让画面空着。
    intro = round(head[1][1], 3) if head else max(0.8, min(2.0, first_start))
    last_end = round(pairs[-1][1][1], 3)
    end_hold = round(max(1.0, total - last_end - END_MOVE), 3)

    return {
        "title": (head[0].title if head else "") or storyboard.title or "故事",
        "subtitle": storyboard.subtitle or (head[0].text if head else ""),
        "end": (tail[0].text or tail[0].title) if tail else "",
        "credits": "",
        "watermark": "",
        "world": WORLD_OF_PAGE[page],
        "path": "wave",
        "layout": "card",
        "progress": False,
        "theme": _theme(storyboard, page),
        "timing": {
            "intro": intro,
            "travel": TRAVEL,
            "endMove": END_MOVE,
            "endHold": end_hold,
        },
        "nodes": [_node_of(scene, (start, end), base) for scene, (start, end) in pairs],
    }


# ── page ────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def three_source() -> str:
    """three.js r128 全文。随包分发，读一次就缓存——600KB 不该每次渲染都读盘。"""
    path = PAGE_DIR / "vendor" / "three.min.js"
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        raise FlightPageError(
            f"three.js 缺失: {path}（安装包时检查 package-data 是否包含 "
            "pages/vendor/*.js）"
        ) from e


def render_flight(
    storyboard: Storyboard,
    slots: Sequence[tuple[Scene, float, float]],
    *,
    page: str = "flight",
    base_dir: Path | str | None = None,
) -> str:
    """写出飞行页全文：单文件，three.js 与 STORY 都已内联。"""
    story = build_story(storyboard, slots, page=page, base_dir=base_dir)
    payload = json.dumps(story, ensure_ascii=False)
    # </script> 出现在 JSON 里会提前闭合脚本块；\/ 在 JSON 里等价于 /，安全
    payload = payload.replace("</", "<\\/")

    html = read_page(page)
    html = html.replace("__TITLE__", _escape_html(story["title"]))
    html = html.replace("__STORY__", payload)
    # 放最后：three.min.js 自己就含有 __THREE_ 开头的符号，先替换会被再扫一遍
    return html.replace("__THREE_LIB__", three_source())


def _escape_html(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


__all__ = [
    "PHOTO_EXTS",
    "FlightPageError",
    "WORLD_OF_PAGE",
    "build_story",
    "render_flight",
    "three_source",
]
