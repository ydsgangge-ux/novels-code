"""3D 立体展示页：把一个分镜页装进浏览器框里，滚动着推近介绍。

效果由三件事叠加而成：

* **立体**——``#stage`` 给 ``perspective``，``#world`` 保留 3D 上下文，浏览器框和
  漂浮的小标签各自站在不同的 ``translateZ`` 上，镜头一动就有前后景。
* **滚动即机位**——页面只有一屏高，靠一个高的占位块产生滚动条；滚动进度被映射成
  ``translateZ``（从远处推到几乎贴脸）和 ``rotateX/rotateY``（俯视 → 平视），
  于是"上下滑动"本身就是在推镜头。
* **滚动即分镜**——滚动进度同时被切成 N 段（N = 分镜镜头数），第 i 段把框里那页
  ``__seek`` 到第 i 个镜头并从头播到尾。滑到哪儿，里面的动画就走到哪儿。

鼠标再加两层：指针位置驱动整体倾角（带阻尼），以及跟随指针的高光和反光条。全部
只用 ``transform``，不触发重排；``prefers-reduced-motion`` 下自动关掉鼠标倾斜。

输出是**单个 HTML 文件**，内页以 base64 内联进 ``iframe srcdoc``，所以断网、发给
别人、直接双击都能看——这一点跟"推荐项目的那种展示页"是同一个诉求。

运行（纯 Python，不装浏览器也能出页）：

    python -m gangge.layer4_tools.storyboard.showcase
    python -m gangge.layer4_tools.storyboard.showcase -f sb.json -o out/index.html

代码里用：

    from gangge.layer4_tools.storyboard.showcase import write_showcase
    write_showcase(storyboard, "out/index.html")
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path
from string import Template
from typing import Sequence

from gangge.layer4_tools.storyboard.demo import demo_storyboard
from gangge.layer4_tools.storyboard.schema import Scene, Storyboard
from gangge.layer4_tools.storyboard.templates import (
    _hex_to_rgb,
    render_page,
    style_palette,
)

#: 每段镜头滚动多高（视口高度的倍数）。1.0 = 滑一屏换一个镜头。
CHAPTER_VH = 1.0
#: 结尾多留一点滚动距离，最后那个镜头能停住看清楚。
TAIL_VH = 0.55
#: 浏览器框的标题栏高度，布局时要从可视高度里扣掉。
CHROME_H = 38

_TPL_NOTE: dict[str, str] = {
    "title": "开场镜头 · 一句话把结论说完",
    "bullets": "要点镜头 · 一条一条落下来",
    "flow": "流程镜头 · 节点依次点亮",
    "quote": "金句镜头 · 停一拍再说下一句",
    "outro": "收尾镜头 · 给出行动点",
}


def _chapter_label(index: int) -> str:
    return f"{index + 1:02d}"


def _chapter_title(scene: Scene) -> str:
    text = (scene.title or scene.text or scene.template).strip()
    return text[:42]


def _chapter_note(scene: Scene) -> str:
    if scene.voiceover:
        return scene.voiceover.strip()
    return _TPL_NOTE.get(scene.template, scene.template)


def chapters_of(
    sb: Storyboard, slots: Sequence[tuple[Scene, float, float]] | None = None
) -> list[dict[str, object]]:
    """把每个镜头变成一段滚动章节。

    每组都有两套时间：``s0/s1`` 是镜头的真实起止（录视频时按它把 ``t`` 换算成
    滚动进度，配音和画面才严格对齐），``t0/t1`` 内缩了 0.3s，手动滚动时用它，
    免得停在空镜或切镜闪白的那一帧上。
    """
    out: list[dict[str, object]] = []
    for i, (scene, start, end) in enumerate(slots or sb.timeline()):
        t0 = round(start + 0.30, 3)
        t1 = round(max(t0 + 0.2, end - 0.30), 3)
        out.append(
            {
                "label": _chapter_label(i),
                "title": _chapter_title(scene),
                "note": _chapter_note(scene),
                "s0": round(start, 3),
                "s1": round(max(start + 0.2, end), 3),
                "t0": t0,
                "t1": t1,
            }
        )
    return out


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def render_showcase(
    sb: Storyboard,
    slots: Sequence[tuple[Scene, float, float]] | None = None,
    *,
    source_url: str = "",
    inner_html: str | None = None,
    scroll_vh: float | None = None,
) -> str:
    """分镜 → 单文件 3D 展示页（HTML 字符串）。

    ``slots`` 只在渲染视频时传：它是配音合成之后的真实时间轴，展示页内部的分镜
    和章节都按它排，画面才跟配音对得上。``source_url`` 只是显示在假地址栏里。

    ``inner_html`` 用来换掉框里的东西——传一段整页截图页面进去，这个展示页就变成
    "框里装的是那个真实网站"（见 :mod:`gangge.layer4_tools.storyboard.site`）。
    ``scroll_vh`` 覆盖滚动长度，截图那种长内容需要比一屏多滑几屏。
    """
    slots = tuple(slots) if slots else sb.timeline()
    inner = inner_html if inner_html is not None else render_page(sb, slots)
    chapters = chapters_of(sb, slots)
    palette = style_palette(sb)
    n = max(1, len(chapters))
    total = round(slots[-1][2], 3) if slots else 0.0
    spacer = scroll_vh if scroll_vh is not None else n * CHAPTER_VH + TAIL_VH
    return _PAGE.substitute(
        title=_html(sb.title or "showcase"),
        brand=_html(sb.title or "分镜展示"),
        address=_html(source_url or f"{sb.title or '分镜'} · 预览"),
        bg=palette["bg"],
        fg=palette["fg"],
        fg_rgb=_hex_to_rgb(palette["fg"]),
        muted=palette["muted"],
        accent=palette["accent"],
        accent_rgb=_hex_to_rgb(palette["accent"]),
        accent2=palette["accent2"],
        accent2_rgb=_hex_to_rgb(palette["accent2"]),
        aspect=f"{sb.width / max(1, sb.height):.4f}",
        chrome=CHROME_H,
        total=f"{total:.3f}",
        chapters=json.dumps(chapters, ensure_ascii=False),
        # 只有一段时不画进度刻度：一根竖线立在右边反而像没加载完
        rails="".join("<i></i>" for _ in range(n)) if n > 1 else "",
        spacer_vh=f"{spacer:.2f}",
        inner_b64=_b64(inner),
    )


def write_showcase(
    sb: Storyboard,
    out: Path | str | None = None,
    *,
    slots: Sequence[tuple[Scene, float, float]] | None = None,
    source_url: str = "",
) -> Path:
    """写盘，返回生成的 HTML 路径。"""
    target = Path(out) if out else Path(".gangge") / "showcase" / "index.html"
    if target.suffix.lower() not in (".html", ".htm"):
        target = target / "index.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        render_showcase(sb, slots, source_url=source_url), encoding="utf-8"
    )
    return target


def _html(text: str) -> str:
    import html

    return html.escape(str(text), quote=True)


_PAGE = Template("""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>$title</title>
<style>
  :root {
    --bg: $bg; --fg: $fg; --fg-rgb: $fg_rgb; --muted: $muted;
    --accent: $accent; --accent-rgb: $accent_rgb;
    --accent2: $accent2; --accent2-rgb: $accent2_rgb;
    --chrome: ${chrome}px;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background:
      radial-gradient(1200px 780px at 50% -12%, rgba(var(--accent-rgb), .14), transparent 62%),
      radial-gradient(1000px 700px at 84% 112%, rgba(var(--accent2-rgb), .13), transparent 60%),
      #04060b;
    color: var(--fg);
    font-family: "Microsoft YaHei", "PingFang SC", "Hiragino Sans GB",
                 "Segoe UI", system-ui, sans-serif;
    -webkit-font-smoothing: antialiased;
    overflow-x: hidden;
  }
  /* 背景网格：给 3D 一点空间参照，四周用遮罩化开 */
  body::before {
    content: ""; position: fixed; inset: 0; z-index: 0; pointer-events: none;
    background-image:
      linear-gradient(rgba(var(--fg-rgb), .05) 1px, transparent 1px),
      linear-gradient(90deg, rgba(var(--fg-rgb), .05) 1px, transparent 1px);
    background-size: 84px 84px;
    -webkit-mask-image: radial-gradient(circle at 50% 46%, #000 8%, transparent 70%);
    mask-image: radial-gradient(circle at 50% 46%, #000 8%, transparent 70%);
  }

  /* ── 3D 舞台 ── */
  #stage {
    position: fixed; inset: 0; z-index: 1;
    perspective: 1800px; perspective-origin: 50% 42%;
  }
  #world {
    position: absolute; inset: 0;
    transform-style: preserve-3d; will-change: transform;
  }
  .rig {
    position: absolute; left: 50%; top: 50%;
    translate: -50% -50%;
    transform-style: preserve-3d;
  }

  .browser {
    position: relative; width: 100%; height: 100%;
    border-radius: 16px; overflow: hidden;
    background: rgba(9, 13, 21, .74);
    border: 1px solid rgba(255, 255, 255, .16);
    box-shadow:
      0 90px 150px -50px rgba(0, 0, 0, .95),
      0 0 0 1px rgba(255, 255, 255, .04) inset,
      0 0 110px rgba(var(--accent-rgb), .16);
    display: flex; flex-direction: column;
  }
  .chrome {
    flex: 0 0 var(--chrome); height: var(--chrome);
    display: flex; align-items: center; gap: 12px; padding: 0 15px;
    background: linear-gradient(180deg, rgba(255, 255, 255, .10), rgba(255, 255, 255, .03));
    border-bottom: 1px solid rgba(255, 255, 255, .10);
  }
  .dots { display: flex; gap: 6px; }
  .dots i { width: 10px; height: 10px; border-radius: 50%; background: rgba(255, 255, 255, .2); }
  .dots i:nth-child(1) { background: #ff5f57; }
  .dots i:nth-child(2) { background: #febc2e; }
  .dots i:nth-child(3) { background: #28c840; }
  .url {
    flex: 1; height: 22px; border-radius: 7px; padding: 0 10px;
    display: flex; align-items: center; overflow: hidden; white-space: nowrap;
    background: rgba(0, 0, 0, .34); border: 1px solid rgba(255, 255, 255, .07);
    font-size: 12px; color: var(--muted); letter-spacing: .02em;
  }
  .screen { position: relative; flex: 1; overflow: hidden; background: #000; }
  .screen iframe { display: block; width: 100%; height: 100%; border: 0; background: #000; }
  /* 玻璃反光条，角度跟着鼠标走。宁可淡到几乎看不见，也别把里面的内容洗白 */
  .glare {
    position: absolute; inset: 0; z-index: 3; pointer-events: none;
    background: linear-gradient(calc(110deg + var(--gx, .5) * 46deg),
                transparent 32%, rgba(255, 255, 255, .05) 46%, transparent 64%);
    mix-blend-mode: screen;
  }

  /* 漂浮的小标签：不同的 translateZ = 不同的前后景 */
  .chip {
    position: absolute; white-space: nowrap;
    padding: 9px 16px; border-radius: 999px;
    font-size: 13px; font-weight: 650; letter-spacing: .02em;
    background: rgba(10, 16, 26, .66);
    border: 1px solid rgba(var(--accent-rgb), .42);
    box-shadow: 0 16px 38px -12px rgba(0, 0, 0, .85),
                0 0 28px rgba(var(--accent-rgb), .22);
    backdrop-filter: blur(10px);
    transform: translateZ(var(--z, 0px));
  }
  .chip b { color: var(--accent); font-variant-numeric: tabular-nums; margin-right: 6px; }
  .rig.tight .chip { display: none; }

  /* ── HUD ── */
  #brand {
    position: fixed; left: min(5.5vw, 68px); top: min(6vh, 46px); z-index: 4;
    display: flex; align-items: center; gap: 10px;
    font-size: 13px; letter-spacing: .1em; color: var(--muted);
    text-shadow: 0 2px 18px rgba(0, 0, 0, .9);
  }
  #brand b { color: var(--fg); font-weight: 700; letter-spacing: .04em; }
  #brand i {
    width: 8px; height: 8px; border-radius: 50%; background: var(--accent);
    box-shadow: 0 0 16px var(--accent);
  }

  #hud {
    position: fixed; left: min(5.5vw, 68px); bottom: min(7vh, 62px); z-index: 4;
    display: flex; gap: 18px; align-items: flex-start; max-width: 44ch;
  }
  #hud-idx {
    padding-top: 5px; font-size: 13px; font-weight: 800; letter-spacing: .16em;
    color: var(--accent); font-variant-numeric: tabular-nums;
  }
  .hud-body { min-width: 0; }
  #hud-title {
    font-size: clamp(20px, 2.3vw, 30px); font-weight: 800; line-height: 1.2;
    letter-spacing: -.01em; text-shadow: 0 2px 26px rgba(0, 0, 0, .95);
  }
  #hud-note {
    margin-top: 9px; font-size: clamp(12.5px, 1.05vw, 15px); line-height: 1.7;
    color: var(--muted); text-shadow: 0 2px 20px rgba(0, 0, 0, .95);
    display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
    overflow: hidden;
  }
  #hud.swap .hud-body { animation: swap .55s cubic-bezier(.22, .8, .3, 1) both; }
  @keyframes swap {
    from { opacity: 0; transform: translateY(12px); }
    to { opacity: 1; transform: none; }
  }

  #rail {
    position: fixed; right: min(4vw, 46px); top: 50%; translate: 0 -50%; z-index: 4;
    display: flex; flex-direction: column; gap: 11px;
  }
  #rail i {
    width: 3px; height: 22px; border-radius: 2px;
    background: rgba(var(--fg-rgb), .3);
    transition: height .35s ease, background .35s ease, box-shadow .35s ease;
  }
  #rail i.done { background: rgba(var(--accent-rgb), .55); }
  #rail:empty { display: none; }
  #rail i.on {
    height: 42px; background: var(--accent);
    box-shadow: 0 0 14px rgba(var(--accent-rgb), .9);
  }

  #bar {
    position: fixed; left: 0; top: 0; height: 2px; width: 0; z-index: 5;
    background: linear-gradient(90deg, var(--accent), var(--accent2));
    box-shadow: 0 0 14px rgba(var(--accent-rgb), .8);
  }
  #hint {
    position: fixed; left: 50%; bottom: 24px; translate: -50% 0; z-index: 4;
    display: flex; align-items: center; gap: 8px;
    font-size: 11px; letter-spacing: .26em; color: var(--muted);
    transition: opacity .45s ease;
  }
  #hint em { font-style: normal; animation: bob 1.7s ease-in-out infinite; }
  @keyframes bob {
    0%, 100% { transform: translateY(-2px); opacity: .55; }
    50% { transform: translateY(3px); opacity: 1; }
  }

  /* ── 鼠标特效 ── */
  #spot {
    position: fixed; inset: 0; z-index: 2; pointer-events: none;
    background: radial-gradient(520px 520px at var(--px, 50%) var(--py, 42%),
                rgba(var(--accent-rgb), .15), transparent 62%);
    mix-blend-mode: screen;
  }
  #ring {
    position: fixed; left: 0; top: 0; z-index: 9; pointer-events: none;
    width: 40px; height: 40px; border-radius: 50%; translate: -50% -50%;
    border: 1px solid rgba(var(--accent-rgb), .85);
    box-shadow: 0 0 26px rgba(var(--accent-rgb), .45),
                inset 0 0 18px rgba(var(--accent-rgb), .22);
    opacity: 0; transition: opacity .35s ease, scale .25s ease;
  }
  #ring.on { opacity: 1; }
  body:active #ring { scale: 1.35; }

  @media (max-width: 900px) {
    .chip { display: none; }
    #rail { display: none; }
    #hud { max-width: 78vw; }
  }
</style>
</head>
<body>
<div id="stage">
  <div id="world">
    <div class="rig" id="rig">
      <div class="browser">
        <div class="chrome">
          <span class="dots"><i></i><i></i><i></i></span>
          <span class="url">$address</span>
        </div>
        <div class="screen">
          <iframe id="frame" title="分镜页" scrolling="no"></iframe>
          <div class="glare"></div>
        </div>
      </div>
      <span class="chip" style="left:-6%; top:14%; --z:210px">立体画框</span>
      <span class="chip" style="right:-7%; top:56%; --z:300px">滚动推镜</span>
      <span class="chip" style="right:5%; bottom:-6%; --z:150px">鼠标视差</span>
    </div>
  </div>
</div>

<div id="spot"></div>
<div id="ring"></div>
<div id="bar"></div>

<div id="brand"><i></i><b>$brand</b><span>· 3D 展示</span></div>

<div id="hud">
  <div id="hud-idx">01</div>
  <div class="hud-body">
    <div id="hud-title"></div>
    <div id="hud-note"></div>
  </div>
</div>

<div id="rail">$rails</div>
<div id="hint">向下滚动 <em>↓</em></div>

<div id="spacer" style="height: calc(100vh * $spacer_vh)"></div>

<script type="application/octet-stream" id="inner-page">$inner_b64</script>
<script>
(function () {
  var CHAPTERS = $chapters;   // [{label,title,note,t0,t1}, ...]
  var AR = $aspect;           // 画幅宽高比，框子按它算尺寸
  var REDUCE = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  var worldEl = document.getElementById('world');
  var rig = document.getElementById('rig');
  var frame = document.getElementById('frame');
  var hud = document.getElementById('hud');
  var hudIdx = document.getElementById('hud-idx');
  var hudTitle = document.getElementById('hud-title');
  var hudNote = document.getElementById('hud-note');
  var rail = document.getElementById('rail');
  var bar = document.getElementById('bar');
  var hint = document.getElementById('hint');
  var spot = document.getElementById('spot');
  var ring = document.getElementById('ring');

  var N = Math.max(1, CHAPTERS.length);
  var TOTAL = $total;              // 内页总时长，__seek 的合法区间
  var lastIdx = -1;
  var SEEK_MODE = false;           // 逐帧录制时接管驱动，rAF 循环让位
  var readyResolve = null;
  // 录制器会等这个 Promise：内页（iframe srcdoc）没就绪就开始 __seek 会拍到空镜
  window.__ready = new Promise(function (resolve) { readyResolve = resolve; });
  setTimeout(function () { if (readyResolve) readyResolve(false); }, 3000);

  // ── 内页：base64 内联，避免任何外部请求 ──
  var raw = atob(document.getElementById('inner-page').textContent.trim());
  var buf = new Uint8Array(raw.length);
  for (var i = 0; i < raw.length; i++) buf[i] = raw.charCodeAt(i);
  frame.srcdoc = new TextDecoder('utf-8').decode(buf);

  // ── 布局：框子按分镜画幅算尺寸，内页再按框宽等比缩放 ──
  function fitInner() {
    try {
      var doc = frame.contentDocument;
      if (!doc) return;
      var stage = doc.getElementById('stage');
      if (!stage) return;
      var k = frame.clientWidth / stage.offsetWidth;
      stage.style.transformOrigin = 'top left';
      stage.style.transform = 'scale(' + k.toFixed(5) + ')';
    } catch (e) { /* 还没加载完，下次再说 */ }
  }

  function layout() {
    var tall = AR < 1;                       // 竖屏画幅靠高度定尺寸
    var maxW = Math.min(window.innerWidth * 0.74, 1220);
    var maxH = window.innerHeight * (tall ? 0.84 : 0.72) - $chrome;
    var w = Math.max(320, Math.min(maxW, maxH * AR));
    var h = w / AR;
    rig.style.width = w + 'px';
    rig.style.height = (h + $chrome) + 'px';
    // 框子很窄时（竖屏手机）标签会压到里面的内容，直接收起来
    rig.classList.toggle('tight', w < 620);
    fitInner();
  }

  frame.addEventListener('load', function () {
    try {
      var w = frame.contentWindow;
      if (w && w.__hide_ui) w.__hide_ui();
    } catch (e) {}
    fitInner();
    if (readyResolve) { readyResolve(true); readyResolve = null; }
  });

  // ── 滚动 → 机位进度 ──
  function progress() {
    var max = document.documentElement.scrollHeight - window.innerHeight;
    if (max <= 0) return 0;
    return Math.max(0, Math.min(1, window.scrollY / max));
  }

  // 先快后慢：推进的前 2/3 走完大部分行程，剩下的时间只轻轻靠过去。
  // 用 ease-in-out 的话前几秒画面一直很远，录出来像卡住了。
  function easeOutCubic(p) {
    return 1 - Math.pow(1 - p, 3);
  }

  // 远处的俯视 → 贴脸的平视：z 从 -940 推到 +140，角度回正
  function camAt(p) {
    var e = easeOutCubic(p);
    return {
      z: -940 + 1080 * e,
      rx: 15 - 16 * e,
      ry: -13 + 15 * e
    };
  }

  function seekTo(i, lp) {
    var c = CHAPTERS[i];
    var t = c.t0 + (c.t1 - c.t0) * lp;
    try {
      var w = frame.contentWindow;
      if (w && w.__seek) w.__seek(t);
    } catch (e) {}
  }

  function setChapter(i) {
    var c = CHAPTERS[i];
    hudIdx.textContent = c.label;
    hudTitle.textContent = c.title;
    hudNote.textContent = c.note;
    hud.classList.remove('swap');
    void hud.offsetWidth;          // 强制回流，动画重放
    hud.classList.add('swap');
    var dots = rail.children;
    for (var k = 0; k < dots.length; k++) {
      dots[k].className = k < i ? 'done' : (k === i ? 'on' : '');
    }
  }

  // ── 指针：倾角 + 光斑 + 反光 + 光环（都带阻尼） ──
  var ptrX = 0, ptrY = 0, wantX = 0, wantY = 0;
  var curX = 0, curY = 0, ringX = 0, ringY = 0;

  window.addEventListener('pointermove', function (ev) {
    wantX = (ev.clientX / window.innerWidth) * 2 - 1;
    wantY = (ev.clientY / window.innerHeight) * 2 - 1;
    curX = ev.clientX; curY = ev.clientY;
    spot.style.setProperty('--px', curX.toFixed(0) + 'px');
    spot.style.setProperty('--py', curY.toFixed(0) + 'px');
    ring.classList.add('on');
  }, { passive: true });

  window.addEventListener('pointerleave', function () { ring.classList.remove('on'); });
  window.addEventListener('resize', layout);

  // ── 一帧：给定进度和鼠标偏移，把画面摆到位（滚动与录制共用同一段代码） ──
  function paint(p, mx, my) {
    var c = camAt(p);
    worldEl.style.transform =
      'translate3d(0,0,' + c.z.toFixed(1) + 'px) ' +
      'rotateX(' + (c.rx - my * 5).toFixed(2) + 'deg) ' +
      'rotateY(' + (c.ry + mx * 7).toFixed(2) + 'deg)';
    worldEl.style.setProperty('--gx', ((mx + 1) / 2).toFixed(3));

    var idx = Math.min(N - 1, Math.max(0, Math.floor(p * N + 0.0001)));
    var lp = Math.min(1, Math.max(0, p * N - idx));
    if (idx !== lastIdx) { lastIdx = idx; setChapter(idx); }

    bar.style.width = (p * 100).toFixed(2) + '%';
    hint.style.opacity = p > 0.02 ? 0 : 1;
    return { idx: idx, lp: lp };
  }

  function seekInner(t) {
    try {
      var w = frame.contentWindow;
      if (w && w.__seek) w.__seek(t);
    } catch (e) {}
  }

  var smooth = 0;
  function tick() {
    requestAnimationFrame(tick);
    if (SEEK_MODE) return;          // 录制期间画面只认 __seek(t)

    var p = progress();
    smooth += (p - smooth) * 0.15;
    ptrX += (wantX - ptrX) * 0.08;
    ptrY += (wantY - ptrY) * 0.08;
    ringX += (curX - ringX) * 0.2;
    ringY += (curY - ringY) * 0.2;

    var at = paint(smooth, REDUCE ? 0 : ptrX, REDUCE ? 0 : ptrY);
    seekTo(at.idx, at.lp);
    ring.style.transform = 'translate3d(' + ringX.toFixed(1) + 'px,' +
                           ringY.toFixed(1) + 'px,0)';
  }

  // ── 逐帧录制接口：t（秒） → 滚动进度 + 内页时间 ──
  // 纯函数，和滚动/鼠标完全解耦，所以同一段 t 序列永远截出同一批画面。
  window.__seek = function (t) {
    SEEK_MODE = true;
    t = Math.max(0, t || 0);
    var i = 0;
    for (var k = 0; k < N; k++) { if (t >= CHAPTERS[k].s0) i = k; }
    var c = CHAPTERS[i];
    var local = Math.max(0, Math.min(1, (t - c.s0) / Math.max(0.001, c.s1 - c.s0)));
    paint((i + local) / N, 0, 0);
    // 内页直接吃真实时间：配音说到哪儿，里面那页就演到哪儿
    seekInner(Math.min(t, TOTAL - 0.001));
  };

  window.__hide_ui = function () {
    // 录制时去掉只对"人"有意义的三样：滚动提示、鼠标光环、跟随光斑
    ['hint', 'ring', 'spot'].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.style.display = 'none';
    });
  };

  layout();
  setChapter(0);
  requestAnimationFrame(tick);
})();
</script>
</body>
</html>
""")


def _from_url(url: str) -> Storyboard:
    """抓页面 + 让主模型写分镜；任何配置/抓取问题都给一句能照做的提示。"""
    import asyncio

    from gangge.layer4_tools.storyboard.sources import SourceError, draft_from_url

    try:
        from gangge.layer5_llm.registry import create_llm

        llm = create_llm(None)
    except Exception as e:  # noqa: BLE001 - 配置问题，不是崩溃
        raise SystemExit(
            f"无法初始化主模型: {e}\n"
            "检查 .env 里的 LLM_PROVIDER 和对应 *_API_KEY；"
            "也可以先用 -f 传一份手写分镜。"
        )
    try:
        return asyncio.run(draft_from_url(url, llm))
    except SourceError as e:
        raise SystemExit(str(e))
    except Exception as e:  # noqa: BLE001 - 网络/模型都算可解释失败
        raise SystemExit(f"从网址写分镜失败: {e}")


def _load(args: argparse.Namespace) -> tuple[Storyboard, str]:
    """返回 (分镜, 来源网址)——网址只用于假地址栏，没给就是空串。"""
    if args.url:
        return _from_url(args.url), args.url
    if args.file:
        path = Path(args.file)
        if not path.exists():
            raise SystemExit(f"分镜文件不存在: {path}")
        return Storyboard.from_dict(json.loads(path.read_text(encoding="utf-8"))), ""
    sb = demo_storyboard(args.style or "neon")
    if args.portrait:
        sb.width, sb.height = 1080, 1920
    return sb, ""


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    p = argparse.ArgumentParser(
        prog="python -m gangge.layer4_tools.storyboard.showcase",
        description="把分镜页做成 3D 立体展示页（单文件 HTML，可断网打开）。",
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument("-f", "--file", metavar="JSON", help="分镜 JSON；不给就用内置样片")
    src.add_argument("-u", "--url", metavar="URL", help="网址：抓页面后由主模型写分镜")
    p.add_argument("--style", help="视觉风格，如 neon/dark/paper/glass/brutal")
    p.add_argument("--portrait", action="store_true", help="竖屏画幅 1080×1920")
    p.add_argument(
        "--shot",
        action="store_true",
        help="配合 --url：抓整页截图，框里放那个网站本身（而不是从它写分镜）",
    )
    p.add_argument("-o", "--out", metavar="PATH", help="输出 HTML 或目录")
    args = p.parse_args()

    if args.shot:
        if not args.url:
            raise SystemExit("--shot 需要配合 --url 使用")
        from gangge.layer4_tools.storyboard.site import SiteError, build_site_page

        try:
            target = build_site_page(args.url, args.out, style=args.style or "neon")
        except SiteError as e:
            raise SystemExit(f"截图失败: {e}")
        print(f"展示页: {target}   （框内是该站点的整页截图，滚动时整页上移）")
        return 0

    sb, source_url = _load(args)
    if args.style:
        sb.style = args.style
    target = write_showcase(sb, args.out, source_url=source_url)
    print(f"展示页: {target}   ({len(sb.scenes)} 个镜头，滚动 {len(sb.scenes)} 段)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
