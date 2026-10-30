"""技能效果预览（右侧「✨ 效果」页签）的契约测试。

守四件事：
1. 视频类技能都有图，且图确实落在 ``previews/`` 里；
2. 用户自定义的 ``preview`` 字段不能把界面引到包外去读文件；
3. 图片页任何人可用；动画页在环境不满足时只解释原因，不拖累图片；
4. 点技能会切到效果页签并换图——点了没反应等于没做。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from gangge.layer4_tools.skills import PREVIEW_DIR, Skill, find, preview_image

DESKTOP = Path(__file__).resolve().parent.parent / "desktop"
APP_PATH = DESKTOP / "app.py"

VIDEO_IDS = ["video_dark", "video_neon", "video_paper", "video_glass", "video_brutal"]


# ── 图片资源 ────────────────────────────────────────────────────

def test_every_video_skill_ships_a_preview_png():
    for skill_id in VIDEO_IDS:
        skill = find(skill_id)
        assert skill is not None, f"缺少技能 {skill_id}"
        path = preview_image(skill)
        assert path is not None and path.is_file(), f"{skill_id} 没有效果图"
        assert path.parent == PREVIEW_DIR.resolve()


def test_auto_style_skill_uses_some_preview():
    """自动选风格也得有图，否则菜单里它看着像坏的。"""
    skill = find("video_auto")
    assert skill is not None
    assert preview_image(skill) is not None


def test_portrait_skill_ships_a_portrait_preview(qapp):
    """竖屏技能的示例图必须是竖的，横图会让人以为是横屏片。"""
    from PyQt6.QtGui import QPixmap

    skill = find("video_portrait")
    assert skill is not None
    assert "aspect=9:16" in skill.template

    path = preview_image(skill)
    assert path is not None
    pixmap = QPixmap(str(path))
    assert pixmap.height() > pixmap.width(), "竖屏预览图不该是横的"


def test_plain_skill_has_no_preview():
    skill = find("code_review")
    assert skill is not None
    assert skill.preview == ""
    assert preview_image(skill) is None


def test_preview_cannot_escape_package_dir():
    """一条 json 就能读写任意文件的话，预览就是个漏洞。"""
    for evil in ("../../../etc/passwd", "..\\..\\secret.png", "/etc/hosts"):
        assert preview_image(Skill(id="x", label="x", template="t", preview=evil)) is None


def test_user_skill_may_declare_preview(tmp_path: Path):
    skill = Skill.from_dict(
        {"id": "mine", "label": "我的", "template": "做 ＜X＞", "preview": "dark.png"}
    )
    assert skill.preview == "dark.png"
    assert preview_image(skill) is not None


# ── 动画层的环境判断 ────────────────────────────────────────────

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6.QtWidgets", reason="需要 PyQt6")

from PyQt6.QtWidgets import QApplication, QPlainTextEdit, QTabWidget, QWidget  # noqa: E402

if str(DESKTOP) not in sys.path:
    sys.path.insert(0, str(DESKTOP))

import skill_preview  # noqa: E402  （与 app.py 里是同一个模块）


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def app_module(qapp):
    spec = importlib.util.spec_from_file_location("gangge_desktop_app_effect", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_low_memory_blocks_animation_but_keeps_images(monkeypatch):
    """内存不够时只解释原因，图片不受影响——这正是两层分开的意义。"""
    monkeypatch.setattr(skill_preview, "free_commit_mb", lambda: 120)
    ok, reason = skill_preview.webengine_status()
    assert ok is False
    assert "内存" in reason and "图片" in reason


def test_animation_html_has_timeline_and_seek(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(skill_preview, "_ANIM_DIR", tmp_path / "anim")
    path = skill_preview.animation_html(find("video_neon"))

    assert path is not None and path.is_file()
    html = path.read_text(encoding="utf-8")
    assert "window.__seek" in html, "动画页要能定位时间轴，没有 __seek 就没法播"
    assert html.count('class="scene') == 5, "示例片是五帧，少一帧预览就不完整"
    # 同一份缓存复用，不重复渲染
    assert skill_preview.animation_html(find("video_neon")) == path


def test_portrait_skill_animates_in_9_by_16(tmp_path: Path, monkeypatch):
    """竖屏技能的动画页也得是竖的，否则预览和成片不是一回事。"""
    monkeypatch.setattr(skill_preview, "_ANIM_DIR", tmp_path / "anim")
    path = skill_preview.animation_html(find("video_portrait"))

    assert path is not None and path.is_file()
    html = path.read_text(encoding="utf-8")
    assert "portrait" in html
    assert "container-type: size" in html


def test_animation_html_rejects_unknown_style(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(skill_preview, "_ANIM_DIR", tmp_path / "anim")
    odd = Skill(id="x", label="x", template="t", preview="../../evil.png")
    assert skill_preview.animation_html(odd) is None


def test_panel_offers_both_layers(app_module, qapp):
    panel = app_module.SkillEffectPanel()
    titles = [panel._views.tabText(i) for i in range(panel._views.count())]
    assert any("图片" in t for t in titles)
    assert any("动画" in t for t in titles)


def test_play_refuses_politely_without_memory(app_module, monkeypatch, qapp):
    panel = app_module.SkillEffectPanel()
    panel.show_skill(find("video_dark"))
    monkeypatch.setattr(skill_preview, "free_commit_mb", lambda: 64)

    panel._play_animation()

    assert "内存" in panel._anim_status.text()
    assert panel._web_view is None, "环境不满足时不该把 Chromium 拉起来"


def test_browser_fallback_opens_system_browser(app_module, monkeypatch, qapp):
    """没有 WebEngine 也能看动画：交给系统浏览器，零风险。"""
    opened: list[str] = []
    monkeypatch.setattr(
        skill_preview.QDesktopServices,
        "openUrl",
        staticmethod(lambda url: opened.append(url.toLocalFile())),
    )

    panel = app_module.SkillEffectPanel()
    panel.show_skill(find("video_neon"))
    panel._open_in_browser()

    assert opened and opened[0].endswith("neon.html")
    assert "浏览器" in panel._anim_status.text()


# ── 面板与主窗口联动 ────────────────────────────────────────────

def test_panel_loads_matching_image(app_module, qapp):
    panel = app_module.SkillEffectPanel()
    panel.resize(360, 420)
    skill = find("video_neon")
    panel.show_skill(skill)

    assert panel._title.text() == skill.label
    assert panel._hint.text() == skill.hint
    assert not panel.current_pixmap.isNull()


def test_panel_explains_missing_image(app_module, qapp):
    panel = app_module.SkillEffectPanel()
    panel.show_skill(find("code_review"))

    assert panel.current_pixmap.isNull()
    assert "没有视觉预览" in panel._empty.text()


def test_panel_clear_resets_to_hint(app_module, qapp):
    panel = app_module.SkillEffectPanel()
    panel.show_skill(find("video_dark"))
    panel.clear()

    assert panel.current_pixmap.isNull()
    assert "⚡ 技能" in panel._hint.text()


def test_applying_skill_switches_to_effect_tab(app_module, qapp):
    panel = app_module.SkillEffectPanel()
    tabs = QTabWidget()
    tabs.addTab(QWidget(), "别的页")
    tabs.addTab(panel, "✨ 效果")
    tabs.setCurrentIndex(0)

    stub = SimpleNamespace(
        _task_input=QPlainTextEdit(), _skill_effect=panel, _right_tabs=tabs
    )
    stub._select_focus_token = lambda token: None
    stub._preview_skill = lambda skill, switch=False: (
        app_module.GanggeDesktop._preview_skill(stub, skill, switch)
    )

    app_module.GanggeDesktop._apply_skill(stub, find("video_neon"))

    assert tabs.currentWidget() is panel, "点了技能却没把效果页签翻出来"
    assert not panel.current_pixmap.isNull()


def test_hovering_menu_entry_only_updates_panel(app_module, qapp):
    """划过去只换图，不抢页签——正在看文件的人不该被拽走。"""
    panel = app_module.SkillEffectPanel()
    tabs = QTabWidget()
    tabs.addTab(QWidget(), "别的页")
    tabs.addTab(panel, "✨ 效果")
    tabs.setCurrentIndex(0)

    stub = SimpleNamespace(_skill_effect=panel, _right_tabs=tabs)
    app_module.GanggeDesktop._preview_skill(stub, find("video_paper"))

    assert not panel.current_pixmap.isNull()
    assert tabs.currentIndex() == 0
