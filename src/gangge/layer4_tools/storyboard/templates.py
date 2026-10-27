"""Scene templates and the page they render into.

The page is a pure function of time: ``__seek(t)`` derives every animated
element's opacity/transform from ``t`` alone. No CSS animations, no external
libraries, no CDN — which is what makes frame-by-frame capture exact (a slow
machine does not shift the animation) and guarantees the pipeline runs offline.
"""

from __future__ import annotations

import html
from typing import Any, Sequence

from gangge.layer4_tools.storyboard.schema import Scene, Storyboard

BASE_W, BASE_H = 1920, 1080


def _esc(value: str) -> str:
    return html.escape(str(value or ""), quote=True)


def _steps(count: int, duration: float, lead: float = 0.3, gap: float = 0.28,
           hold: float = 0.5) -> list[float]:
    """Entry times for ``count`` items, compressed to fit ``duration``.

    A five-bullet shot narrated in two seconds must not finish animating at
    three seconds, so the spacing shrinks rather than overrunning the shot.
    """
    if count <= 0:
        return []
    room = max(0.0, duration - lead - hold)
    step = min(gap, room / count) if room > 0 else 0.06
    return [round(lead + i * step, 3) for i in range(count)]


def _anim(kind: str, start: float, dur: float = 0.55, extra: str = "") -> str:
    return (
        f'data-anim="{kind}" data-start="{start}" data-dur="{dur}"'
        + (f" {extra}" if extra else "")
    )


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


# ── page assembly ───────────────────────────────────────────────


def render_page(sb: Storyboard, slots: Sequence[tuple[Scene, float, float]]) -> str:
    """Build the full standalone HTML for ``slots`` (start/end in seconds)."""
    total = slots[-1][2] if slots else 0.0
    scale = sb.width / BASE_W
    sections = "\n".join(
        f'<section class="scene tpl-{scene.template}" '
        f'data-start="{start}" data-end="{end}">'
        f"{_RENDERERS[scene.template](scene, max(0.1, end - start))}</section>"
        for scene, start, end in slots
    )
    theme = sb.theme
    return _PAGE.format(
        bg=_esc(theme["bg"]),
        fg=_esc(theme["fg"]),
        muted=_esc(theme["muted"]),
        accent=_esc(theme["accent"]),
        width=sb.width,
        height=sb.height,
        scale=f"{scale:.4f}",
        total=f"{total:.3f}",
        sections=sections,
        title=_esc(sb.title or "storyboard"),
    )


_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  :root {{
    --bg: {bg}; --fg: {fg}; --muted: {muted}; --accent: {accent};
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  html, body {{
    width: 100%; height: 100%; overflow: hidden; background: var(--bg);
    font-family: "Microsoft YaHei", "PingFang SC", "Hiragino Sans GB",
                 "Segoe UI", system-ui, sans-serif;
    color: var(--fg); -webkit-font-smoothing: antialiased;
  }}
  #stage {{
    position: relative; width: 1920px; height: 1080px;
    transform: scale({scale}); transform-origin: top left;
    background:
      radial-gradient(1100px 620px at 18% 12%, rgba(91,140,255,.20), transparent 62%),
      radial-gradient(900px 560px at 86% 88%, rgba(122,92,255,.16), transparent 60%),
      var(--bg);
  }}
  .scene {{
    position: absolute; inset: 0; padding: 96px 132px;
    display: none; flex-direction: column;
  }}
  .scene.tpl-title, .scene.tpl-quote, .scene.tpl-outro {{
    justify-content: center; align-items: center; text-align: center;
  }}
  .scene.tpl-bullets {{ justify-content: center; }}
  .scene.tpl-flow {{ justify-content: center; align-items: center; text-align: center; }}
  .col {{ display: flex; flex-direction: column; width: 100%; }}
  .col.center {{ align-items: center; }}
  .col.left {{ align-items: flex-start; }}

  h1 {{ font-size: 96px; line-height: 1.18; font-weight: 800; letter-spacing: -1px; }}
  h2 {{ font-size: 64px; line-height: 1.25; font-weight: 700; margin-bottom: 58px; }}
  .sub {{ font-size: 44px; line-height: 1.5; color: var(--muted); margin-top: 36px; max-width: 1240px; }}
  .eyebrow {{
    font-size: 24px; letter-spacing: 6px; text-transform: uppercase;
    color: var(--accent); font-weight: 700; margin-bottom: 30px;
  }}
  .rule {{
    width: 132px; height: 5px; border-radius: 3px; margin-top: 38px;
    background: linear-gradient(90deg, var(--accent), rgba(122,92,255,.9));
    transform-origin: center left;
  }}
  .col.center .rule {{ transform-origin: center; }}

  /* The list block is centred as a whole, not pinned to the left edge: on a
     16:9 canvas a left-hugging column leaves a dead right half. The rule gives
     the column an edge so short lists do not look like a stray paragraph. */
  .scene.tpl-bullets .col {{
    max-width: 1340px; margin: 0 auto; padding-left: 54px;
    border-left: 7px solid rgba(91,140,255,.5);
  }}
  .bullets {{ list-style: none; display: flex; flex-direction: column; gap: 40px; }}
  .bullets li {{ display: flex; align-items: baseline; gap: 30px; }}
  .bullets .idx {{
    font-size: 34px; font-weight: 800; color: var(--accent);
    font-variant-numeric: tabular-nums; min-width: 68px;
  }}
  .bullets .txt {{ font-size: 52px; line-height: 1.35; font-weight: 500; }}

  .flow {{ display: flex; align-items: center; justify-content: center; gap: 24px; margin-top: 18px; }}
  .flow .node {{
    padding: 38px 52px; border-radius: 24px; font-size: 40px; font-weight: 650;
    border: 2px solid rgba(91,140,255,.55); background: rgba(91,140,255,.12);
    box-shadow: 0 18px 50px rgba(0,0,0,.35); white-space: nowrap;
  }}
  .flow .arrow {{ font-size: 44px; color: var(--accent); opacity: .85; }}

  blockquote {{ font-size: 74px; line-height: 1.36; font-weight: 700; max-width: 1500px; }}
  cite {{ display: block; margin-top: 34px; font-size: 32px; color: var(--muted); font-style: normal; }}

  .cta {{
    margin-top: 56px; padding: 26px 56px; border-radius: 999px;
    font-size: 36px; font-weight: 700;
    background: linear-gradient(120deg, var(--accent), #7a5cff);
    box-shadow: 0 20px 60px rgba(91,140,255,.35);
  }}

  #progress {{
    position: absolute; left: 0; bottom: 0; height: 5px; width: 0;
    background: linear-gradient(90deg, var(--accent), #7a5cff);
  }}
</style>
</head>
<body>
<div id="stage" data-total="{total}">
  {sections}
  <div id="progress"></div>
</div>
<script>
(function () {{
  var stage = document.getElementById('stage');
  var total = parseFloat(stage.dataset.total || '0');
  var bar = document.getElementById('progress');
  var scenes = Array.prototype.slice.call(document.querySelectorAll('.scene'));

  function ease(p) {{ return 1 - Math.pow(1 - p, 3); }}

  function apply(el, local) {{
    var ds = parseFloat(el.dataset.start || '0');
    var dd = parseFloat(el.dataset.dur || '0.55') || 0.55;
    var p = Math.max(0, Math.min(1, (local - ds) / dd));
    var e = ease(p);
    var kind = el.dataset.anim;
    if (kind === 'grow-x') {{
      el.style.opacity = 1;
      el.style.transform = 'scaleX(' + e.toFixed(4) + ')';
    }} else if (kind === 'fade') {{
      el.style.opacity = e;
      el.style.transform = 'none';
    }} else if (kind === 'slide-right') {{
      el.style.opacity = e;
      el.style.transform = 'translateX(' + ((1 - e) * -46).toFixed(2) + 'px)';
    }} else if (kind === 'pop') {{
      el.style.opacity = e;
      el.style.transform = 'scale(' + (0.86 + 0.14 * e).toFixed(4) + ')';
    }} else {{
      el.style.opacity = e;
      el.style.transform = 'translateY(' + ((1 - e) * 30).toFixed(2) + 'px)';
    }}
  }}

  // Pure function of t: same t always yields the same pixels, so a slow
  // machine produces the same video as a fast one.
  window.__seek = function (t) {{
    for (var i = 0; i < scenes.length; i++) {{
      var s = scenes[i];
      var st = parseFloat(s.dataset.start);
      var en = parseFloat(s.dataset.end);
      var on = t >= st && t < en;
      s.style.display = on ? 'flex' : 'none';
      if (!on) continue;
      var local = t - st;
      var els = s.querySelectorAll('[data-anim]');
      for (var j = 0; j < els.length; j++) apply(els[j], local);
    }}
    if (bar && total > 0) {{
      bar.style.width = (Math.min(1, t / total) * 100).toFixed(2) + '%';
    }}
  }};

  window.__seek(0);
}})();
</script>
</body>
</html>
"""
