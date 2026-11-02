"""样式工作台：组合是乘出来的，不是枚举出来的。

守四件事：
1. 5 风格 × 2 画幅都预渲染好，且是能独立播放的真实页面；
2. 每份页面都带全部装饰层，前端才能任意开关（不用为 2^5 组合各存一份）；
3. 注入的 JSON / 枚举真的进了 HTML，且不留占位符；
4. 导出的配置项（style/decor/aspect/fps）在工具和 CLI 里都确实存在。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from gangge.layer4_tools.storyboard import studio
from gangge.layer4_tools.storyboard.demo import demo_storyboard
from gangge.layer4_tools.storyboard.pages import read_page
from gangge.layer4_tools.storyboard.schema import DECOR_NAMES, STYLE_NAMES
from gangge.layer4_tools.storyboard.templates import render_page


def test_preview_pages_cover_every_style_and_aspect():
    pages = studio.preview_pages()

    assert len(pages) == len(STYLE_NAMES) * len(studio.ASPECTS)
    for style in STYLE_NAMES:
        for aspect in studio.ASPECTS:
            key = f"{style}|{studio._aspect_key(aspect)}"
            assert key in pages, f"缺少 {key}"


def test_preview_pages_are_real_playable_pages():
    pages = studio.preview_pages()
    html = base64.b64decode(pages["neon|portrait"]).decode("utf-8")

    assert "window.__seek" in html and "window.__play" in html
    # 竖屏页面必须是竖的排版，否则预览和成片不是一回事
    assert "portrait" in html


def test_preview_pages_ship_every_decoration_layer():
    """装饰全开，前端勾掉哪层就隐藏哪层——这是"任意组合"不膨胀的前提。"""
    html = base64.b64decode(studio.preview_pages()["dark|landscape"]).decode("utf-8")
    for name in DECOR_NAMES:
        assert f'data-decor="{name}"' in html, f"{name} 层没生成"


def test_decor_layers_are_marked_in_normal_render():
    """工作台靠 data-decor 找层，普通渲染也得有，不然两边行为会分叉。"""
    sb = demo_storyboard("neon")
    sb.decor = ["glow", "scanline"]
    html = render_page(sb, sb.timeline())

    assert 'data-decor="glow"' in html
    assert 'data-decor="scanline"' in html
    assert 'data-decor="grid"' not in html


def test_build_studio_injects_config_and_leaves_no_placeholder(tmp_path: Path):
    out = studio.build_studio(tmp_path / "studio.html", fps=24)

    assert out.is_file() and out.stat().st_size > 100_000, "十份页面内联进来，太小就是没渲染"
    html = out.read_text(encoding="utf-8")
    for token in ("__PAGES__", "__STYLES__", "__DECORS__", "__DEFAULTS__", "__FPS__"):
        assert token not in html, f"{token} 没被替换，工作台会拿到字面量"
    assert json.dumps(list(STYLE_NAMES)) in html
    assert "fps: 24" in html


def test_build_studio_clamps_fps(tmp_path: Path):
    out = studio.build_studio(tmp_path / "studio.html", fps=5)
    assert "var state" in out.read_text(encoding="utf-8")
    assert "fps: 10" in out.read_text(encoding="utf-8"), "5 fps 应该被钳到下限 10"


def test_studio_shell_is_registered():
    shell = read_page("studio")
    assert "样式工作台" in shell
    assert "demo_video" in shell, "导出区要给出能直接贴的调用"
