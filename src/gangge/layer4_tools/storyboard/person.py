"""人物页：一条光线穿过一个人的一生。

和另外三种页面（逐镜头分镜 / 3D 展示 / 网站截图）不同，这条片子**不是切镜**，
而是一个连续镜头：光点沿路径行进，停在一个节点上讲一段，讲完继续走。所以：

* 分镜的 ``scenes`` 被重新解释成"浮生节点"——第一个 ``title`` 是开场名牌，
  最后一个 ``outro`` 是结语，中间每个镜头是一个年份节点；
* 时长照旧由配音决定（``slots`` 从 pipeline 来），讲解占一个镜头的前 64%，
  剩下的时间用来走向下一站。配音和画面因此天然同步，不用另做对齐；
* 照片是可选的：给了就按讲解进度轮播，没给就画一个占位框——**没图也能出片**。

模型仍然只写文字和年份，不碰动画；这里做的是把分镜翻译成页面认识的那一份
``STORY`` 数据，和 :mod:`~.templates` 的角色一样。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Sequence

from gangge.layer4_tools.storyboard.pages import read_page
from gangge.layer4_tools.storyboard.schema import Scene, Storyboard
from gangge.layer4_tools.storyboard.templates import (
    BASE_H,
    BASE_W,
    PORTRAIT_H,
    PORTRAIT_W,
    style_palette,
)

logger = logging.getLogger(__name__)

#: 能被浏览器当照片打开的扩展名。视频/PDF 一律跳过，免得渲染出破图。
PHOTO_EXTS: tuple[str, ...] = (
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif",
)

#: 一张照片都没给时的默认底色：深夜绿 + 稻穗金。光线纪录片本来就是这个气质。
PERSON_PALETTE: dict[str, str] = {
    "bg": "#08130f",
    "bg2": "#10241d",
    "fg": "#f2f8f3",
    "muted": "#8ea79a",
    "accent": "#f3c969",
    "glow": "#ffe6a6",
}

#: 节点小球上那行小字最多几个字——再长就压到相邻节点上了。
_SHORT_CHARS = 6


class PersonPageError(ValueError):
    """人物页渲染不了（照片路径写错、分镜里一个节点都没有）。"""


# ── photos ──────────────────────────────────────────────────────


def list_photos(directory: Path | str) -> list[Path]:
    """列出一个目录里能当照片用的文件，按文件名排序。

    排序是刻意的：用户把文件命名成 ``01-童年.jpg`` 就能控制先后，
    不需要再写一份清单。
    """
    folder = Path(directory)
    if not folder.is_dir():
        raise PersonPageError(f"照片目录不存在: {folder}")
    files = sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in PHOTO_EXTS
    )
    return files


def assign_photos(storyboard: Storyboard, directory: Path | str) -> list[str]:
    """把目录里的照片轮流发给每个人生节点，返回给人看的说明。

    已经用 ``scenes[].photos`` 显式指定过照片时不覆盖——手写的清单永远优先于
    按目录瞎猜。开场名牌和结语不算节点，不参与分配。
    """
    if any(scene.photos for scene in storyboard.scenes):
        return ["分镜里已经指定了照片，忽略 --photos 目录"]

    files = list_photos(directory)
    if not files:
        return [f"照片目录里没有图片（支持 {'/'.join(PHOTO_EXTS)}）: {directory}"]

    targets = [
        s for s in storyboard.scenes if s.template not in ("title", "outro")
    ] or list(storyboard.scenes)

    # 存绝对路径：照片目录是相对当前工作目录给的，而页面会相对**输出目录**
    # 解析相对路径，直接存原样会在装进 out 目录后全部失效。
    for i, file in enumerate(files):
        targets[i % len(targets)].photos.append(str(file.resolve()))

    return [f"照片 {len(files)} 张 → {len(targets)} 个节点（轮流分配）"]


def _photo_entry(src: str, base_dir: Path | None) -> dict[str, str] | None:
    """一条照片记录；路径不存在就返回 ``None``（坏路径不该让整条片子失败）。"""
    text = str(src).strip()
    if not text:
        return None
    raw = Path(text)
    if raw.is_absolute():
        candidates = [raw]
    else:
        # 两种相对基准都试一遍：手写的 JSON 多半相对工作目录，而自动分配的照片
        # 已经存成绝对路径。少一个都行，写错的路径才真的丢掉。
        candidates = ([Path(base_dir) / raw] if base_dir else []) + [raw]

    for candidate in candidates:
        try:
            path = candidate.resolve()
        except OSError:
            continue
        if path.is_file() and path.suffix.lower() in PHOTO_EXTS:
            return {"src": path.as_uri(), "caption": path.stem}

    logger.warning("[person] 跳过无效照片: %s", text)
    return None


# ── storyboard → STORY ──────────────────────────────────────────


def _node_of(scene: Scene, slot: tuple[float, float], base_dir: Path | None) -> dict[str, Any]:
    """一个镜头 → 一个浮生节点。任何版式都能当节点用，字段尽量物尽其用。"""
    title = scene.title or scene.text or ""
    body = scene.text if scene.title else ""
    # bullets / flow 的正文在 items / nodes 里，人物分镜也常这么写，
    # 所以合并进来而不是丢掉——模型不用为人物页另学一套模板。
    extra = scene.items or scene.nodes
    if extra:
        joined = " · ".join(extra)
        body = f"{body} {joined}".strip() if body else joined
    if not title:
        title, body = body, ""

    photos = [
        entry for entry in (
            _photo_entry(p, base_dir) for p in scene.photos
        ) if entry is not None
    ]
    return {
        "year": scene.year,
        "short": re.sub(r"\s+", "", title)[:_SHORT_CHARS],
        "title": title,
        "text": body,
        "photos": photos,
        "start": slot[0],
        "end": slot[1],
    }


def build_story(
    storyboard: Storyboard,
    slots: Sequence[tuple[Scene, float, float]],
    base_dir: Path | str | None = None,
) -> dict[str, Any]:
    """把分镜 + 时间轴翻成页面认识的 ``STORY``。

    剥掉开场和结语之后，中间那些才是节点。如果整条分镜只有一个 title 镜头，
    就把它自己当成唯一节点——宁可页面朴素，也不要渲染一张空地图。
    """
    base = Path(base_dir) if base_dir else None
    pairs = list(zip(storyboard.scenes, [(s, e) for _, s, e in slots]))

    name_pair = None
    end_pair = None
    if pairs and pairs[0][0].template == "title":
        name_pair = pairs.pop(0)
    if pairs and pairs[-1][0].template == "outro":
        end_pair = pairs.pop()

    if not pairs:
        fallback = name_pair or end_pair
        if fallback is None:
            raise PersonPageError("分镜里没有任何镜头")
        pairs = [fallback]

    name = (name_pair[0].title if name_pair else "") or storyboard.title or "人物"
    subtitle = storyboard.subtitle or (name_pair[0].text if name_pair else "")
    end_text = ""
    if end_pair is not None:
        end_text = end_pair[0].text or end_pair[0].title

    nodes = [_node_of(scene, (start, end), base) for scene, (start, end) in pairs]
    total = round(slots[-1][2], 3) if slots else 0.0

    return {
        "title": name,
        "subtitle": subtitle,
        "end": end_text,
        "total": total,
        # 名牌演到开场镜头结束为止；没有 title 镜头就当没有开场
        "intro": round(name_pair[1][1], 3) if name_pair else 0.0,
        # 结语从最后一个镜头的起点开始；没有 outro 就是 None，页面不显示结语层
        "ending": round(end_pair[1][0], 3) if end_pair else None,
        "nodes": [
            {
                "year": n["year"],
                "short": n["short"],
                "title": n["title"],
                "text": n["text"],
                "photos": n["photos"],
            }
            for n in nodes
        ],
        "beats": [{"start": n["start"], "end": n["end"]} for n in nodes],
    }


def scene_count(story: dict[str, Any]) -> int:
    """页面里真正的人生节点数——写提示语时用。"""
    return len(story.get("nodes") or [])


# ── page ────────────────────────────────────────────────────────


def _palette(storyboard: Storyboard) -> dict[str, str]:
    """默认是光线纪录片的深绿金；用户显式挑了别的风格/主题就跟着走。"""
    out = dict(PERSON_PALETTE)
    if storyboard.style != "dark" or storyboard.theme:
        p = style_palette(storyboard)
        out.update(
            bg=p["bg"], bg2=p["bg"], fg=p["fg"],
            muted=p["muted"], accent=p["accent"], glow=p["accent2"] or p["accent"],
        )
    return out


def _hex_to_rgb(value: str) -> str:
    v = value.lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    try:
        return "%d,%d,%d" % (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))
    except (ValueError, IndexError):
        return "255,255,255"


def render_person(
    storyboard: Storyboard,
    slots: Sequence[tuple[Scene, float, float]],
    base_dir: Path | str | None = None,
) -> str:
    """写出人物页全文（单文件，图片用 file:// 绝对路径引用）。"""
    story = build_story(storyboard, slots, base_dir)
    portrait = storyboard.height > storyboard.width
    stage_w, stage_h = (PORTRAIT_W, PORTRAIT_H) if portrait else (BASE_W, BASE_H)
    palette = _palette(storyboard)

    payload = json.dumps(story, ensure_ascii=False)
    # </script> 出现在 JSON 里会提前闭合脚本块；\/ 在 JSON 里等价于 /，安全
    payload = payload.replace("</", "<\\/")

    html = read_page("person")
    for token, value in (
        ("__TITLE__", _escape_html(story["title"] or "人物")),
        ("__STORY__", payload),
        ("__BG__", palette["bg"]),
        ("__BG2__", palette["bg2"]),
        ("__FG__", palette["fg"]),
        ("__FG_RGB__", _hex_to_rgb(palette["fg"])),
        ("__MUTED__", palette["muted"]),
        ("__ACCENT__", palette["accent"]),
        ("__ACCENT_RGB__", _hex_to_rgb(palette["accent"])),
        ("__GLOW__", palette["glow"]),
        ("__STAGE_W__", str(stage_w)),
        ("__STAGE_H__", str(stage_h)),
        ("__SCALE__", f"{storyboard.width / stage_w:.4f}"),
        ("__LAYOUT__", "portrait" if portrait else "landscape"),
    ):
        html = html.replace(token, value)
    return html


def _escape_html(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
