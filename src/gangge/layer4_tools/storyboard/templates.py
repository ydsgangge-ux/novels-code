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


_PAGE = Template("""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>$title</title>
<style>
  :root {
    --bg: $bg; --fg: $fg; --fg-rgb: $fg_rgb; --muted: $muted;
    --accent: $accent; --accent-rgb: $accent_rgb;
    --accent2: $accent2; --accent2-rgb: $accent2_rgb;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html, body {
    width: 100%; height: 100%; overflow: hidden; background: var(--bg);
    font-family: "Microsoft YaHei", "PingFang SC", "Hiragino Sans GB",
                 "Segoe UI", system-ui, sans-serif;
    color: var(--fg); -webkit-font-smoothing: antialiased;
  }
  #stage {
    position: relative; width: ${stage_w}px; height: ${stage_h}px;
    transform: scale($scale); transform-origin: top left;
    background: var(--bg); overflow: hidden;
  }
  /* 镜头层：每个版式一套机位，由 __seek 逐帧计算 */
  #camera {
    position: absolute; inset: 0; transform-origin: 50% 50%;
    will-change: transform;
  }
  /* 切镜的曝光闪，压在画面之上、控件之下 */
  #flash {
    position: absolute; inset: 0; pointer-events: none; opacity: 0; z-index: 5;
    background: radial-gradient(circle at 50% 46%,
                rgba(var(--accent-rgb), .85), transparent 72%);
  }

  /* ── decoration ── */
  #decor { position: absolute; inset: 0; pointer-events: none; z-index: 0; }
  .deco { position: absolute; inset: 0; }
  /* 外扩一圈，漂移时边缘不会露出底色 */
  .deco-glow {
    inset: -18%; will-change: transform;
    background:
      radial-gradient(1100px 620px at 18% 12%, rgba(var(--accent-rgb), .22), transparent 62%),
      radial-gradient(900px 560px at 86% 88%, rgba(var(--accent2-rgb), .18), transparent 60%);
  }
  .deco-grid {
    inset: -12%;
    background-image:
      linear-gradient(rgba(var(--fg-rgb), .07) 1px, transparent 1px),
      linear-gradient(90deg, rgba(var(--fg-rgb), .07) 1px, transparent 1px);
    background-size: 64px 64px;
  }
  .deco-grain {
    opacity: .055;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='3'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
  }
  /* 镜头后拉时装饰层会整体缩小，满宽的光带会被裁成一条硬边方块，
     所以左右各外扩一圈，并且用多档渐变让它没有可见的起止线 */
  .deco-scan {
    inset: auto -12% auto -12%; height: 200px;
    background: linear-gradient(180deg,
                transparent, rgba(var(--accent-rgb), .035) 32%,
                rgba(var(--accent-rgb), .07) 50%,
                rgba(var(--accent-rgb), .035) 68%, transparent);
  }
  .deco-particles { overflow: hidden; }
  .particle {
    position: absolute; width: 7px; height: 7px; border-radius: 50%;
    background: var(--accent); filter: blur(1.5px); opacity: .3;
  }

  /* ── scenes ── */
  .scene {
    position: absolute; inset: 0; padding: 96px 132px;
    display: none; flex-direction: column; z-index: 1;
  }
  .scene.tpl-title, .scene.tpl-quote, .scene.tpl-outro {
    justify-content: center; align-items: center; text-align: center;
  }
  .scene.tpl-bullets { justify-content: center; }
  .scene.tpl-flow { justify-content: center; align-items: center; text-align: center; }
  .col { display: flex; flex-direction: column; width: 100%; }
  .col.center { align-items: center; }
  .col.left { align-items: flex-start; }

  /* balance: 中文标题宁可折成两行等长，也不要末行只剩两个字 */
  h1 { font-size: 112px; line-height: 1.16; font-weight: 800; letter-spacing: -1.5px;
       text-wrap: balance; }
  h2 { font-size: 72px; line-height: 1.24; font-weight: 700; margin-bottom: 58px;
       text-wrap: balance; }
  .sub { font-size: 44px; line-height: 1.5; color: var(--muted); margin-top: 36px; max-width: 1240px; }
  .rule {
    width: 132px; height: 5px; border-radius: 3px; margin-top: 38px;
    background: linear-gradient(90deg, var(--accent), var(--accent2));
    transform-origin: center left;
  }
  .col.center .rule { transform-origin: center; }

  .scene.tpl-bullets .col {
    max-width: 1340px; margin: 0 auto; padding-left: 54px;
    border-left: 7px solid rgba(var(--accent-rgb), .5);
  }
  .bullets { list-style: none; display: flex; flex-direction: column; gap: 40px; }
  .bullets li { display: flex; align-items: baseline; gap: 30px; }
  .bullets .idx {
    font-size: 34px; font-weight: 800; color: var(--accent);
    font-variant-numeric: tabular-nums; min-width: 68px;
  }
  .bullets .txt { font-size: 52px; line-height: 1.35; font-weight: 500; }

  .flow { display: flex; align-items: center; justify-content: center; gap: 24px; margin-top: 18px; }
  .flow .node {
    padding: 38px 52px; border-radius: 24px; font-size: 40px; font-weight: 650;
    border: 2px solid rgba(var(--accent-rgb), .55);
    background: rgba(var(--accent-rgb), .12);
    box-shadow: 0 18px 50px rgba(0, 0, 0, .35); white-space: nowrap;
  }
  .flow .arrow { font-size: 44px; color: var(--accent); opacity: .85; }

  blockquote { font-size: 74px; line-height: 1.36; font-weight: 700; max-width: 1500px;
               text-wrap: balance; }
  cite { display: block; margin-top: 34px; font-size: 32px; color: var(--muted); font-style: normal; }

  .cta {
    margin-top: 56px; padding: 26px 56px; border-radius: 999px;
    font-size: 36px; font-weight: 700;
    background: linear-gradient(120deg, var(--accent), var(--accent2));
    box-shadow: 0 20px 60px rgba(var(--accent-rgb), .35);
  }

  $style_extra

  $layout_css

  /* ── preview controls (never captured: hidden before a screenshot) ── */
  #controls {
    position: fixed; left: 50%; bottom: 26px; transform: translateX(-50%);
    display: flex; align-items: center; gap: 14px;
    padding: 12px 20px; border-radius: 999px;
    background: rgba(10, 12, 18, .82); border: 1px solid rgba(255, 255, 255, .14);
    box-shadow: 0 12px 40px rgba(0, 0, 0, .5);
    backdrop-filter: blur(10px); z-index: 999;
    font-size: 13px; color: #dfe6f2; opacity: 0; transition: opacity .18s;
  }
  body:hover #controls { opacity: 1; }
  #controls button {
    width: 34px; height: 34px; border-radius: 50%; cursor: pointer;
    border: 1px solid rgba(255, 255, 255, .2); background: rgba(255, 255, 255, .1);
    color: #fff; font-size: 14px; line-height: 1;
  }
  #controls button:hover { background: rgba(255, 255, 255, .22); }
  #seekbar { width: 320px; accent-color: #5b8cff; cursor: pointer; }
  #clock { font-variant-numeric: tabular-nums; white-space: nowrap; }
</style>
</head>
<body class="$style_class $layout_class">
<div id="stage">
  <div id="camera">
    <div id="decor">$decor</div>
    $sections
  </div>
  <div id="flash"></div>
</div>
<div id="controls">
  <button id="btn-play" title="播放 / 暂停（空格）">▶</button>
  <input id="seekbar" type="range" min="0" max="1000" value="0" step="1">
  <span id="clock">0.0 / 0.0s</span>
</div>
<script>
(function () {
  var total = $total;
  var stage = document.getElementById('stage');
  var scenes = Array.prototype.slice.call(document.querySelectorAll('.scene'));
  var particles = Array.prototype.slice.call(document.querySelectorAll('.particle'));
  var grid = document.querySelector('.deco-grid');
  var scan = document.querySelector('.deco-scan');
  var glow = document.querySelector('.deco-glow');
  var camera = document.getElementById('camera');
  var decorLayer = document.getElementById('decor');
  var flashEl = document.getElementById('flash');
  var bar = document.getElementById('seekbar');
  var clock = document.getElementById('clock');
  var btn = document.getElementById('btn-play');
  var currentT = 0;
  var playing = false;
  var rafId = null;
  var tBase = 0;
  var tStart = 0;

  function ease(p) { return 1 - Math.pow(1 - p, 3); }

  function apply(el, local) {
    var ds = parseFloat(el.dataset.start || '0');
    var dd = parseFloat(el.dataset.dur || '0.55') || 0.55;
    var p = Math.max(0, Math.min(1, (local - ds) / dd));
    var e = ease(p);
    var kind = el.dataset.anim;
    if (kind === 'grow-x') {
      el.style.opacity = 1;
      el.style.transform = 'scaleX(' + e.toFixed(4) + ')';
    } else if (kind === 'fade') {
      // 只改透明度：留空让样式表里的 transform（比如竖屏箭头转 90°）生效
      el.style.opacity = e;
      el.style.transform = '';
    } else if (kind === 'slide-right') {
      el.style.opacity = e;
      el.style.transform = 'translateX(' + ((1 - e) * -46).toFixed(2) + 'px)';
    } else if (kind === 'pop') {
      el.style.opacity = e;
      el.style.transform = 'scale(' + (0.86 + 0.14 * e).toFixed(4) + ')';
    } else {
      el.style.opacity = e;
      el.style.transform = 'translateY(' + ((1 - e) * 30).toFixed(2) + 'px)';
    }
  }

  function decor(t) {
    for (var i = 0; i < particles.length; i++) {
      var p = particles[i];
      var s = parseFloat(p.dataset.seed || '0');
      var x = ((s * 100 + t * (3 + s * 9)) % 108) - 4;
      var y = ((s * 61 + t * (1.5 + s * 4)) % 104);
      p.style.left = x.toFixed(2) + '%';
      p.style.top = ((y + Math.sin(t * 0.7 + s * 6.28) * 3).toFixed(2)) + '%';
      p.style.opacity = (0.12 + 0.22 * (0.5 + 0.5 * Math.sin(t + s * 9))).toFixed(3);
    }
    if (grid) grid.style.backgroundPosition = (t * 7).toFixed(1) + 'px ' + (t * 7).toFixed(1) + 'px';
    if (scan) scan.style.top = (((t * 0.16) % 1) * 100).toFixed(2) + '%';
    // 光斑缓慢漂移 + 呼吸，让背景不再是一张贴图
    if (glow) {
      var gx = Math.sin(t * 0.21) * 2.4;
      var gy = Math.cos(t * 0.16) * 2.0;
      var gs = 1 + 0.06 * Math.sin(t * 0.13);
      glow.style.transform =
        'translate3d(' + gx.toFixed(2) + '%,' + gy.toFixed(2) + '%,0) scale(' + gs.toFixed(3) + ')';
    }
  }

  // 每个版式一种机位。PPT 感来自「固定机位 + 元素淡入」，所以这里给每一类
  // 镜头安排自己的运动：开场拉远、要点推近、流程从局部拉到全貌、金句横移、
  // 结尾后拉收住。数值是相对 stage 的比例，换画幅不用改。
  var SHOTS = {
    title:   { s0: 1.075, s1: 1.000, x0: 0.0, x1: 0.0, y0: 0.6, y1: 0.0 },
    bullets: { s0: 1.000, s1: 1.055, x0: 0.0, x1: 0.0, y0: 0.0, y1: -0.5 },
    flow:    { s0: 1.130, s1: 1.000, x0: 0.0, x1: 0.0, y0: 0.0, y1: 0.0 },
    quote:   { s0: 1.050, s1: 1.010, x0: -0.9, x1: 0.9, y0: 0.0, y1: 0.0 },
    outro:   { s0: 1.000, s1: 0.955, x0: 0.0, x1: 0.0, y0: 0.0, y1: 0.5 }
  };

  function shotOf(scene, local, dur) {
    var cls = scene ? scene.className : '';
    var key = 'title';
    for (var k in SHOTS) { if (cls.indexOf('tpl-' + k) >= 0) { key = k; break; } }
    var c = SHOTS[key];
    var e = ease(Math.max(0, Math.min(1, local / Math.max(0.001, dur))));
    return {
      scale: c.s0 + (c.s1 - c.s0) * e,
      x: c.x0 + (c.x1 - c.x0) * e,
      y: c.y0 + (c.y1 - c.y0) * e
    };
  }

  // 手持感：两三个不同周期的正弦叠加，幅度不到 0.2%，看不出抖，但画面是活的
  function handheld(t) {
    return {
      x: Math.sin(t * 1.7) * 0.10 + Math.sin(t * 2.9) * 0.05,
      y: Math.cos(t * 1.3) * 0.10 + Math.sin(t * 2.1) * 0.05
    };
  }

  function syncBar(t) {
    if (bar && total > 0) bar.value = String(Math.round((t / total) * 1000));
    if (clock) clock.textContent = t.toFixed(1) + ' / ' + total.toFixed(1) + 's';
  }

  // Pure function of t: same t always yields the same pixels, so a slow machine
  // produces the same video as a fast one.
  window.__seek = function (t) {
    currentT = t;
    var active = null, local = 0, dur = 1;
    for (var i = 0; i < scenes.length; i++) {
      var s = scenes[i];
      var st = parseFloat(s.dataset.start);
      var en = parseFloat(s.dataset.end);
      var on = t >= st && t < en;
      s.style.display = on ? 'flex' : 'none';
      if (!on) continue;
      active = s;
      local = t - st;
      dur = en - st;
      var els = s.querySelectorAll('[data-anim]');
      for (var j = 0; j < els.length; j++) apply(els[j], local);
    }
    decor(t);

    // ── 机位 ──
    var cam = active ? shotOf(active, local, dur) : { scale: 1, x: 0, y: 0 };
    var wob = handheld(t);
    if (camera) {
      camera.style.transform =
        'translate3d(' + (cam.x + wob.x).toFixed(3) + '%,' +
        (cam.y + wob.y).toFixed(3) + '%,0) scale(' + cam.scale.toFixed(4) + ')';
    }
    // 视差：背景只走 45% 的位移，推拉幅度也减半，前后景就有了纵深
    if (decorLayer) {
      decorLayer.style.transform =
        'translate3d(' + (cam.x * 0.45).toFixed(3) + '%,' +
        (cam.y * 0.45).toFixed(3) + '%,0) scale(' +
        (1 + (cam.scale - 1) * 0.5).toFixed(4) + ')';
    }
    // 切镜的曝光闪：进入镜头的前 0.18s 亮一下，像换了机位
    if (flashEl) {
      var flashDur = 0.18;
      var lit = active && local < flashDur ? (1 - local / flashDur) * 0.42 : 0;
      flashEl.style.opacity = lit.toFixed(3);
    }
    syncBar(t);
  };

  function frame(ts) {
    if (!playing) return;
    if (!tStart) tStart = ts;
    var t = tBase + (ts - tStart) / 1000;
    if (t >= total) { t = total; playing = false; btn.textContent = '▶'; }
    window.__seek(t);
    if (playing) rafId = requestAnimationFrame(frame);
  }

  window.__play = function () {
    if (playing || total <= 0) return;
    if (currentT >= total) currentT = 0;
    playing = true;
    tBase = currentT;
    tStart = 0;
    btn.textContent = '❚❚';
    rafId = requestAnimationFrame(frame);
  };

  window.__pause = function () {
    playing = false;
    tStart = 0;
    if (rafId) cancelAnimationFrame(rafId);
    btn.textContent = '▶';
  };

  window.__hide_ui = function () {
    var c = document.getElementById('controls');
    if (c) c.style.display = 'none';
  };

  if (btn) btn.addEventListener('click', function () { playing ? window.__pause() : window.__play(); });
  if (bar) bar.addEventListener('input', function () {
    window.__pause();
    window.__seek((parseFloat(bar.value) / 1000) * total);
  });
  document.addEventListener('keydown', function (e) {
    if (e.code === 'Space') { e.preventDefault(); playing ? window.__pause() : window.__play(); }
    else if (e.code === 'ArrowRight') { window.__pause(); window.__seek(Math.min(total, currentT + 1)); }
    else if (e.code === 'ArrowLeft') { window.__pause(); window.__seek(Math.max(0, currentT - 1)); }
  });

  window.__seek(0);
})();
</script>
</body>
</html>
""")
