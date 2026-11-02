"""Scene templates, style presets and the page they render into.

Two ideas keep this controllable:

* **The page is a pure function of time.** ``__seek(t)`` derives every animated
  element's opacity/transform from ``t`` alone — no CSS animations, no external
  libraries, no CDN. Frame capture is therefore exact and offline-safe.
* **Looks are data.** A ``style`` selects a colour/typography preset and a set
  of background ornaments (``decor``); the model never writes CSS.

The same page doubles as a browser preview: press space to play, arrows to
scrub. ``__hide_ui()`` removes the controls so they never land in a frame.
"""

from __future__ import annotations

import html
from string import Template
from typing import Any, Sequence

from gangge.layer4_tools.storyboard.pages import read_page
from gangge.layer4_tools.storyboard.schema import Scene, Storyboard

BASE_W, BASE_H = 1920, 1080
#: 竖屏按手机竖拍的 9:16 排版；字号用容器查询单位，换分辨率不用改 CSS。
PORTRAIT_W, PORTRAIT_H = 1080, 1920


# ── styles ──────────────────────────────────────────────────────

_STYLES: dict[str, dict[str, str]] = {
    "dark": {
        "bg": "#0b0f16", "fg": "#f0f4fa", "muted": "#8fa1b8",
        "accent": "#5b8cff", "accent2": "#7a5cff",
    },
    "neon": {
        "bg": "#05060d", "fg": "#f4f8ff", "muted": "#8d9ccb",
        "accent": "#22d3ee", "accent2": "#a855f7",
    },
    "paper": {
        "bg": "#f7f3ea", "fg": "#23201a", "muted": "#6d6555",
        "accent": "#c2410c", "accent2": "#b45309",
    },
    "glass": {
        "bg": "#0b1424", "fg": "#eef6ff", "muted": "#93a8c9",
        "accent": "#7dd3fc", "accent2": "#38bdf8",
    },
    "brutal": {
        "bg": "#f0ede4", "fg": "#111111", "muted": "#555555",
        "accent": "#ff3b30", "accent2": "#111111",
    },
}

# Extra rules layered on top of the base sheet, keyed by style.
_STYLE_EXTRA: dict[str, str] = {
    "dark": "",
    "neon": """
  /* 三层辉光：贴字的紧光晕负责锐利，外两层负责把颜色铺到背景上 */
  h1, h2, blockquote { text-shadow: 0 0 16px rgba(var(--accent-rgb), .55),
                                       0 0 52px rgba(var(--accent-rgb), .32),
                                       0 0 116px rgba(var(--accent2-rgb), .30); }
  .flow .node { box-shadow: 0 0 34px rgba(var(--accent-rgb), .35),
                          inset 0 0 18px rgba(var(--accent2-rgb), .18); }
  .rule { width: 186px; height: 6px;
          box-shadow: 0 0 20px rgba(var(--accent-rgb), .85),
                      0 0 60px rgba(var(--accent2-rgb), .45); }
""",
    "paper": """
  h1, h2, blockquote { font-family: "Songti SC", "SimSun", "Noto Serif CJK SC", serif; }
  .rule { background: var(--accent); }
  .flow .node { background: rgba(194, 65, 12, .07); border-color: rgba(194, 65, 12, .45);
                box-shadow: 0 10px 26px rgba(35, 32, 26, .12); }
  .bullets .idx { color: var(--accent); }
""",
    "glass": """
  .scene .col {
    background: rgba(255, 255, 255, .055);
    border: 1px solid rgba(255, 255, 255, .14);
    border-radius: 30px; padding: 68px 78px;
    box-shadow: 0 34px 90px rgba(0, 0, 0, .45);
    backdrop-filter: blur(18px);
  }
  .scene.tpl-bullets .col { border-left: 1px solid rgba(255, 255, 255, .14); }
""",
    "brutal": """
  h1, h2 { letter-spacing: -3px; text-transform: none; }
  .scene .col {
    background: #fff; border: 5px solid #111;
    box-shadow: 14px 14px 0 #111; padding: 56px 64px;
  }
  .scene.tpl-bullets .col { border-left: 5px solid #111; }
  .rule { background: #111; height: 9px; border-radius: 0; }
  .flow .node { background: #fff; border: 4px solid #111; border-radius: 0;
                box-shadow: 8px 8px 0 #111; }
  .cta { border-radius: 0; border: 4px solid #111; box-shadow: 8px 8px 0 #111;
         background: var(--accent); background-image: none; }
""",
}

# Ornaments that come with a style unless the storyboard says otherwise.
_STYLE_DEFAULT_DECOR: dict[str, tuple[str, ...]] = {
    "dark": ("glow", "particles"),
    "neon": ("glow", "grid", "particles", "scanline"),
    "paper": ("grain", "glow"),
    "glass": ("glow", "grain", "particles"),
    "brutal": ("glow", "grid"),
}

#: 竖屏版式：9:16 排版换一套单位，横屏那份 CSS 一个字都不用动。
#: 手机竖屏的观看距离和握持方式决定了——字要大、一行要少、底部要留空。
_PORTRAIT_CSS = """
  body.portrait #stage { container-type: size; }
  body.portrait .scene { padding: 11cqh 8cqw 15cqh; }
  body.portrait h1 { font-size: 13.5cqw; line-height: 1.24; letter-spacing: -0.2cqw; }
  body.portrait h2 { font-size: 8.6cqw; line-height: 1.3; margin-bottom: 6cqh; }
  body.portrait .sub {
    font-size: 5cqw; line-height: 1.55; margin-top: 4cqh; max-width: 84cqw;
  }
  body.portrait .rule { width: 26cqw; height: 0.7cqw; margin-top: 4.5cqh; }
  body.portrait .col.center .rule { margin-left: auto; margin-right: auto; }

  body.portrait .scene.tpl-bullets .col {
    max-width: 88cqw; padding-left: 0; border-left: none;
  }
  body.portrait .bullets { gap: 4.2cqh; }
  body.portrait .bullets li { gap: 3.4cqw; }
  body.portrait .bullets .idx { font-size: 4.4cqw; min-width: 9cqw; }
  body.portrait .bullets .txt { font-size: 7cqw; line-height: 1.42; }

  /* 竖屏放不下横排三个节点，改成自上而下的流向 */
  body.portrait .flow { flex-direction: column; gap: 2.4cqh; margin-top: 0; }
  body.portrait .flow .node {
    font-size: 6.2cqw; padding: 3cqh 8cqw; border-radius: 4cqw;
  }
  body.portrait .flow .arrow {
    font-size: 5.2cqw; transform: rotate(90deg); line-height: 1;
  }

  body.portrait blockquote { font-size: 9.4cqw; line-height: 1.42; max-width: 88cqw; }
  body.portrait cite { font-size: 4.2cqw; margin-top: 4cqh; }
  body.portrait .cta {
    margin-top: 6cqh; padding: 2.6cqh 9cqw; border-radius: 99cqw; font-size: 5cqw;
  }

  /* 背景在竖屏里要按画幅换算，否则 1100px 的渐变只盖住左上角 */
  body.portrait .deco-glow {
    background:
      radial-gradient(72cqw 38cqh at 20% 14%, rgba(var(--accent-rgb), .26), transparent 64%),
      radial-gradient(64cqw 32cqh at 84% 86%, rgba(var(--accent2-rgb), .20), transparent 62%);
  }
  body.portrait .deco-scan { height: 11cqh; }
  body.portrait .particle { width: 0.9cqw; height: 0.9cqw; filter: blur(0.25cqw); }
  body.portrait .deco-grid { background-size: 7cqw 7cqw; }

  /* 手机录屏时控制条别压住内容 */
  body.portrait #controls { bottom: 2.4cqh; }
"""


_PARTICLE_COUNT = 26


def _esc(value: str) -> str:
    return html.escape(str(value or ""), quote=True)


def _hex_to_rgb(value: str) -> str:
    v = value.lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    try:
        return "%d,%d,%d" % (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))
    except (ValueError, IndexError):
        return "255,255,255"


def _steps(count: int, duration: float, lead: float = 0.3, gap: float = 0.28,
           hold: float = 0.5) -> list[float]:
    """Entry times for ``count`` items, compressed to fit ``duration``."""
    if count <= 0:
        return []
    room = max(0.0, duration - lead - hold)
    step = min(gap, room / count) if room > 0 else 0.06
    return [round(lead + i * step, 3) for i in range(count)]


def _anim(kind: str, start: float, dur: float = 0.55) -> str:
    return f'data-anim="{kind}" data-start="{start}" data-dur="{dur}"'


# ── templates ───────────────────────────────────────────────────


def _title(scene: Scene, dur: float) -> str:
    headline = scene.title or scene.text
    sub_text = scene.text if (scene.title and scene.text) else ""
    sub = f'<p class="sub" {_anim("fade-up", 0.72, 0.5)}>{_esc(sub_text)}</p>' if sub_text else ""
    return f"""
    <div class="col center">
      <h1 {_anim("fade-up", 0.14, 0.65)}>{_esc(headline)}</h1>
      <div class="rule" {_anim("grow-x", 0.56, 0.55)}></div>
      {sub}
    </div>"""


def _bullets(scene: Scene, dur: float) -> str:
    starts = _steps(len(scene.items), dur)
    rows = "\n".join(
        f'<li {_anim("slide-right", s)}>'
        f'<span class="idx">{i + 1:02d}</span>'
        f'<span class="txt">{_esc(item)}</span></li>'
        for i, (item, s) in enumerate(zip(scene.items, starts))
    )
    head = f'<h2 {_anim("fade-up", 0.04, 0.45)}>{_esc(scene.title)}</h2>' if scene.title else ""
    return f"""
    <div class="col left">
      {head}
      <ul class="bullets">{rows}</ul>
    </div>"""


def _flow(scene: Scene, dur: float) -> str:
    starts = _steps(len(scene.nodes), dur, lead=0.35, gap=0.34, hold=0.6)
    parts: list[str] = []
    for i, (node, s) in enumerate(zip(scene.nodes, starts)):
        if i:
            parts.append(f'<div class="arrow" {_anim("fade", max(0.0, s - 0.14), 0.34)}>→</div>')
        parts.append(f'<div class="node" {_anim("pop", s)}>{_esc(node)}</div>')
    head = f'<h2 {_anim("fade-up", 0.04, 0.45)}>{_esc(scene.title)}</h2>' if scene.title else ""
    return f"""
    <div class="col center">
      {head}
      <div class="flow">{"".join(parts)}</div>
    </div>"""


def _quote(scene: Scene, dur: float) -> str:
    body = scene.text or scene.title
    cite = f'<cite {_anim("fade", 0.72, 0.5)}>— {_esc(scene.title)}</cite>' if scene.text and scene.title else ""
    return f"""
    <div class="col center">
      <blockquote {_anim("fade-up", 0.1, 0.7)}>“{_esc(body)}”</blockquote>
      <div class="rule" {_anim("grow-x", 0.62, 0.5)}></div>
      {cite}
    </div>"""


def _outro(scene: Scene, dur: float) -> str:
    cta = scene.items[0] if scene.items else ""
    shown = min(0.42, max(0.0, dur - 0.9))
    return f"""
    <div class="col center">
      <h1 {_anim("fade-up", 0.08, 0.6)}>{_esc(scene.title or scene.text)}</h1>
      {f'<p class="sub" {_anim("fade-up", 0.34, 0.5)}>{_esc(scene.text)}</p>' if scene.text else ""}
      {f'<div class="cta" {_anim("pop", shown)}>{_esc(cta)}</div>' if cta else ""}
    </div>"""


_RENDERERS: dict[str, Any] = {
    "title": _title,
    "bullets": _bullets,
    "flow": _flow,
    "quote": _quote,
    "outro": _outro,
}


# ── decoration layer ────────────────────────────────────────────


def _decor_html(decor: Sequence[str]) -> str:
    """Ornament elements. Position is recomputed from t, never from CSS motion."""
    layers: list[str] = []
    if "glow" in decor:
        layers.append('<div class="deco deco-glow"></div>')
    if "grid" in decor:
        layers.append('<div class="deco deco-grid"></div>')
    if "grain" in decor:
        layers.append('<div class="deco deco-grain"></div>')
    if "scanline" in decor:
        layers.append('<div class="deco deco-scan"></div>')
    if "particles" in decor:
        dots = "".join(
            f'<i class="particle" data-seed="{i * 0.37 % 1:.4f}"></i>'
            for i in range(_PARTICLE_COUNT)
        )
        layers.append(f'<div class="deco deco-particles">{dots}</div>')
    return "".join(layers)


def resolve_decor(sb: Storyboard) -> tuple[str, ...]:
    return tuple(sb.decor) if sb.decor else _STYLE_DEFAULT_DECOR.get(sb.style, ())


def style_palette(sb: Storyboard) -> dict[str, str]:
    """实际用到的颜色：先取风格预设，再叠分镜里的 theme 覆盖。"""
    preset = _STYLES.get(sb.style, _STYLES["dark"])
    return {**preset, **{k: v for k, v in sb.theme.items() if v}}


# ── page assembly ───────────────────────────────────────────────


def render_page(sb: Storyboard, slots: Sequence[tuple[Scene, float, float]]) -> str:
    """Build the full standalone HTML for ``slots`` (start/end in seconds)."""
    total = slots[-1][2] if slots else 0.0
    # 画幅决定基准尺寸：竖屏按 1080×1920 排版，横屏还是 1920×1080，
    # 最后统一 scale 到 sb.width，所以两种画幅共用同一套渲染出口。
    portrait = sb.height > sb.width
    stage_w, stage_h = (PORTRAIT_W, PORTRAIT_H) if portrait else (BASE_W, BASE_H)
    scale = sb.width / stage_w
    sections = "\n".join(
        f'<section class="scene tpl-{scene.template}" '
        f'data-start="{start}" data-end="{end}">'
        f"{_RENDERERS[scene.template](scene, max(0.1, end - start))}</section>"
        for scene, start, end in slots
    )

    palette = style_palette(sb)

    return _PAGE.substitute(
        title=_esc(sb.title or "storyboard"),
        bg=palette["bg"],
        fg=palette["fg"],
        fg_rgb=_hex_to_rgb(palette["fg"]),
        muted=palette["muted"],
        accent=palette["accent"],
        accent_rgb=_hex_to_rgb(palette["accent"]),
        accent2=palette["accent2"],
        accent2_rgb=_hex_to_rgb(palette["accent2"]),
        width=sb.width,
        height=sb.height,
        stage_w=stage_w,
        stage_h=stage_h,
        scale=f"{scale:.4f}",
        layout_class="portrait" if portrait else "landscape",
        layout_css=_PORTRAIT_CSS if portrait else "",
        total=f"{total:.3f}",
        sections=sections,
        decor=_decor_html(resolve_decor(sb)),
        style_extra=_STYLE_EXTRA.get(sb.style, ""),
        style_class=f"style-{sb.style}",
    )


_PAGE = Template(read_page("storyboard"))
