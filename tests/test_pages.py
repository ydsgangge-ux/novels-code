"""页面模板：HTML 是文件、注册表是唯一入口。

守五件事：
1. 每种页面都有对应的模板文件，且文件真的随包分发（package-data）；
2. 模板里的 ``$占位符`` 不能被搬家搬丢，否则渲染会 KeyError；
3. 未知页面名当场报错，不能悄悄退化成另一种页面；
4. 展示页与网站页共用外壳，但内页可以换；
5. 三种页面都能离线渲染出 HTML（不装浏览器、不联网）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gangge.layer4_tools.storyboard import demo, showcase, site, templates
from gangge.layer4_tools.storyboard.pages import (
    PAGE_DIR,
    PAGE_KINDS,
    TEMPLATES,
    page_names,
    read_page,
    template_path,
)
from gangge.layer4_tools.storyboard.pipeline import write_page

ROOT = Path(__file__).resolve().parent.parent


# ── 模板文件 ────────────────────────────────────────────────────

def test_every_page_has_a_template_file():
    for name, filename in TEMPLATES.items():
        path = PAGE_DIR / filename
        assert path.is_file(), f"{name} 的模板文件不存在: {path}"
        assert path.stat().st_size > 200, f"{filename} 太小，像是空文件"


def test_page_kinds_are_all_registered():
    assert set(PAGE_KINDS) <= set(TEMPLATES)
    assert PAGE_KINDS == page_names()


def test_site_reuses_the_showcase_shell():
    """网站页 = 展示页外壳 + 换内页；两套外壳各写一份会很快漂移。"""
    assert TEMPLATES["site"] == TEMPLATES["showcase"]
    assert TEMPLATES["site_inner"] != TEMPLATES["showcase"]


def test_templates_keep_their_placeholders():
    storyboard_html = read_page("storyboard")
    showcase_html = read_page("showcase")
    inner_html = read_page("site_inner")
    for token in ("$title", "$bg", "$accent"):
        assert token in storyboard_html, f"storyboard.html 少了 {token}"
        assert token in showcase_html, f"showcase.html 少了 {token}"
    for token in ("$img", "$alt", "$t0", "$t1"):
        assert token in inner_html, f"site_inner.html 少了 {token}"


def test_read_page_is_cached_and_typed():
    assert read_page("showcase") is read_page("showcase")
    assert template_path("site").name == "showcase.html"


def test_unknown_page_is_rejected():
    with pytest.raises(KeyError, match="未知页面模板"):
        read_page("hologram")


def test_templates_ship_with_the_package():
    """没进 package-data，别人 pip 装完一渲染就 FileNotFoundError。"""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "pages/*.html" in pyproject


# ── 渲染 ────────────────────────────────────────────────────────

def _inner_page() -> str:
    shot = site.SiteShot(
        url="https://example.com",
        title="示例",
        description="",
        image=b"\xff\xd8\xff\xd9",
        width=1400,
        height=900,
    )
    return site.shot_inner_page(shot, 0.0, 9.0)


def test_all_pages_render_offline():
    sb = demo.demo_storyboard("neon")
    slots = sb.timeline()

    assert "window.__seek" in templates.render_page(sb, slots)
    assert "window.__seek" in showcase.render_showcase(sb, slots)
    assert "window.__seek" in _inner_page()


def test_shell_carries_the_source_url():
    sb = demo.title_only("neon")
    html = showcase.render_showcase(sb, source_url="https://example.com/docs")
    assert "https://example.com/docs" in html


# ── 落地 ────────────────────────────────────────────────────────

def test_write_page_keeps_the_plain_copy(tmp_path: Path):
    sb = demo.title_only("dark")
    out = write_page(sb, sb.timeline(), tmp_path, page="showcase")

    assert out.name == "showcase.html"
    # 展示页拍坏了还能退回这份最轻的
    assert (tmp_path / "storyboard.html").is_file()


def test_write_page_rejects_unknown_page(tmp_path: Path):
    sb = demo.title_only("dark")
    with pytest.raises(ValueError, match="未知页面类型"):
        write_page(sb, sb.timeline(), tmp_path, page="hologram")
