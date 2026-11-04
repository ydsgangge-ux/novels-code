"""人物纪录片「填写」页：表格 → 分镜，草稿能存能读。

这块的价值在"人能改"：所以测试盯的是转化是否正确、增删是否跟得上，
以及草稿能不能真的接上——而不是像素长什么样。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6.QtWidgets", reason="需要 PyQt6")

from PyQt6.QtWidgets import QApplication  # noqa: E402

DESKTOP = Path(__file__).resolve().parent.parent / "desktop"
if str(DESKTOP) not in sys.path:
    sys.path.insert(0, str(DESKTOP))

import person_form  # noqa: E402
from person_form import PersonFormWidget  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def form(qapp, tmp_path: Path):
    """每个用例一份干净表单：草稿指到 tmp，不污染仓库。"""
    widget = PersonFormWidget(draft_path=tmp_path / "draft.json")
    widget.load_dict({"name": "", "subtitle": "", "ending": "", "nodes": []})
    return widget


def _fill(form: PersonFormWidget, nodes: list[tuple[str, str, str]]) -> None:
    # 先清空——表单默认留了一行空白，不清掉会多出一个空节点
    form._nodes = []
    form._refresh_table()
    form._name.setText("钱学森")
    form._subtitle.setText("他让中国有了箭")
    for year, title, text in nodes:
        form._add_node()
        row = form._table.currentRow()
        form._table.item(row, 0).setText(year)
        form._table.item(row, 1).setText(title)
        form._body.setPlainText(text)


# ── 校验 ────────────────────────────────────────────────────────


def test_empty_form_asks_for_a_name(form):
    assert "人名" in form.validate()


def test_name_without_nodes_is_rejected(form):
    form._name.setText("钱学森")
    assert "节点" in form.validate()


def test_too_many_nodes_is_discouraged(form):
    form._name.setText("某某")
    for i in range(13):
        form._nodes.append({"year": str(1900 + i), "title": "事", "text": "", "photos": []})
    assert "太多" in form.validate()


# ── 表格 → 分镜 ─────────────────────────────────────────────────


def test_nodes_become_open_quote_and_end(form):
    """用户只填了一串节点，开场和结尾是表单替他补的。"""
    _fill(form, [("1911", "生于上海", "祖籍杭州。"), ("1929", "考进交通大学", "读机械工程。")])
    form._ending.setText("他把自己交给了这个国家最需要的地方。")
    form._table.setCurrentCell(0, 1)

    data = form.to_storyboard()

    assert [s["template"] for s in data["scenes"]] == ["title", "quote", "quote", "outro"]
    assert data["scenes"][0]["title"] == "钱学森"
    assert data["scenes"][0]["text"] == "他让中国有了箭"
    assert data["scenes"][1]["year"] == "1911"
    assert data["scenes"][1]["text"] == "祖籍杭州。"
    assert data["scenes"][-1]["text"].startswith("他把自己")


def test_blank_ending_means_no_outro(form):
    _fill(form, [("1911", "生于上海", "")])
    form._table.setCurrentCell(0, 1)
    assert [s["template"] for s in form.to_storyboard()["scenes"]] == ["title", "quote"]


def test_empty_rows_are_skipped(form):
    _fill(form, [("1911", "生于上海", "")])
    form._add_node()  # 留一行空白
    form._table.setCurrentCell(0, 1)
    assert len(form.to_storyboard()["scenes"]) == 2


def test_portrait_switches_the_canvas(form):
    _fill(form, [("1911", "生于上海", "")])
    form._table.setCurrentCell(0, 1)
    assert (form.to_storyboard()["width"], form.to_storyboard()["height"]) == (1920, 1080)

    form._aspect.setCurrentIndex(form._aspect.findData("9:16"))
    data = form.to_storyboard()
    assert (data["width"], data["height"]) == (1080, 1920)


def test_storyboard_dict_really_parses(form):
    """表单产出必须能被 schema 吃下——不然点预览才炸就太晚了。"""
    from gangge.layer4_tools.storyboard import Storyboard

    _fill(form, [("1911", "生于上海", "祖籍杭州。")])
    form._ending.setText("结语。")
    form._table.setCurrentCell(0, 1)

    sb = Storyboard.from_dict(form.to_storyboard())
    assert len(sb.scenes) == 3
    assert sb.subtitle == "他让中国有了箭"


# ── 增删改 ──────────────────────────────────────────────────────


def test_add_inserts_after_the_current_row(form):
    _fill(form, [("1911", "甲", ""), ("1929", "乙", "")])
    form._table.setCurrentCell(0, 1)
    form._add_node()

    titles = [n["title"] for n in form.nodes]
    assert titles == ["甲", "", "乙"]


def test_remove_keeps_at_least_one_row(form):
    _fill(form, [("1911", "甲", "")])
    form._remove_node()

    assert len(form.nodes) == 1
    assert form.nodes[0]["title"] == ""


def test_move_swaps_neighbours(form):
    _fill(form, [("1911", "甲", ""), ("1929", "乙", "")])
    form._table.setCurrentCell(1, 1)
    form._move(-1)

    assert [n["title"] for n in form.nodes] == ["乙", "甲"]
    assert form._table.currentRow() == 0, "移动后焦点要跟着走，否则连按两下会错位"


def test_body_editing_follows_the_selected_row(form):
    _fill(form, [("1911", "甲", "第一段"), ("1929", "乙", "第二段")])

    form._table.setCurrentCell(0, 1)
    assert form._body.toPlainText() == "第一段"

    form._table.setCurrentCell(1, 1)
    assert form._body.toPlainText() == "第二段"

    form._body.setPlainText("改过的第二段")
    assert form.nodes[1]["text"] == "改过的第二段"


# ── 草稿 ────────────────────────────────────────────────────────


def test_draft_round_trip(qapp, tmp_path: Path):
    draft = tmp_path / "draft.json"
    first = PersonFormWidget(draft_path=draft)
    first._name.setText("钱学森")
    first._nodes = [{"year": "1911", "title": "生于上海", "text": "祖籍杭州。", "photos": []}]
    first._ending.setText("结语。")
    first._refresh_table(select=0)
    first.save_draft()

    assert draft.is_file()
    saved = json.loads(draft.read_text(encoding="utf-8"))
    assert saved["name"] == "钱学森"
    assert saved["nodes"][0]["year"] == "1911"

    second = PersonFormWidget(draft_path=draft)
    assert second._name.text() == "钱学森"
    assert second.nodes[0]["title"] == "生于上海"
    assert second._ending.text() == "结语。"


def test_broken_draft_does_not_break_the_panel(qapp, tmp_path: Path):
    draft = tmp_path / "draft.json"
    draft.write_text("{ 这不是 json", encoding="utf-8")

    widget = PersonFormWidget(draft_path=draft)
    assert len(widget.nodes) == 1, "坏草稿要退化成一份空表单，而不是打不开"


# ── 预览 ────────────────────────────────────────────────────────


def test_preview_writes_the_person_page(form, tmp_path: Path, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(
        person_form.QDesktopServices,
        "openUrl",
        staticmethod(lambda url: opened.append(url.toLocalFile())),
    )
    monkeypatch.setattr(person_form, "PREVIEW_DIR", tmp_path / "preview")

    _fill(form, [("1911", "生于上海", "祖籍杭州。")])
    form._table.setCurrentCell(0, 1)

    html = form.preview()

    assert html is not None and html.is_file()
    assert html.name == "person.html"
    assert "var STORY" in html.read_text(encoding="utf-8")
    assert opened and opened[0].endswith("person.html"), "预览不打开浏览器，用户看不到"
    assert "已生成" in form._status.text()


def test_preview_refuses_without_name(form, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(person_form, "PREVIEW_DIR", tmp_path / "preview")
    assert form.preview() is None
    assert "人名" in form._status.text()


# ── 和「效果」面板的接线 ────────────────────────────────────────


def test_person_skill_switches_to_the_form_tab(qapp, tmp_path: Path):
    """点人物纪录片技能，右侧要直接停在填写页。"""
    import skill_preview
    from gangge.layer4_tools.skills import find

    panel = skill_preview.SkillEffectPanel(draft_path=tmp_path / "draft.json")
    panel.show_skill(find("person_doc"))

    assert panel._views.currentWidget() is panel._form


def test_other_skills_stay_on_the_image_tab(qapp, tmp_path: Path):
    import skill_preview
    from gangge.layer4_tools.skills import find

    panel = skill_preview.SkillEffectPanel(draft_path=tmp_path / "draft.json")
    panel.show_skill(find("video_neon"))

    assert panel._views.currentWidget() is panel._image_tab


def test_person_skill_has_a_playable_animation(tmp_path: Path, monkeypatch):
    """「动画」页签也要能播——四个节点足够看出节奏。"""
    import skill_preview
    from gangge.layer4_tools.skills import find

    monkeypatch.setattr(skill_preview, "_ANIM_DIR", tmp_path / "anim")
    path = skill_preview.animation_html(find("person_doc"))

    assert path is not None and path.is_file()
    html = path.read_text(encoding="utf-8")
    assert "window.__seek" in html
    assert "var STORY" in html


# ── 点技能时的行为 ──────────────────────────────────────────────

APP_PATH = DESKTOP / "app.py"


@pytest.fixture(scope="module")
def app_module(qapp):
    import importlib.util

    spec = importlib.util.spec_from_file_location("gangge_desktop_app_form", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stub(app_module, panel, tmp_path: Path):
    from types import SimpleNamespace

    from PyQt6.QtWidgets import QPlainTextEdit, QTabWidget, QWidget

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
    return stub, tabs


def test_person_skill_does_not_write_into_the_prompt(app_module, qapp, tmp_path: Path):
    """人物纪录片靠右侧表单，不靠提示词——输入框必须保持干净。"""
    import skill_preview
    from gangge.layer4_tools.skills import find

    panel = skill_preview.SkillEffectPanel(draft_path=tmp_path / "draft.json")
    stub, tabs = _stub(app_module, panel, tmp_path)

    app_module.GanggeDesktop._apply_skill(stub, find("person_doc"))

    assert stub._task_input.toPlainText() == ""
    assert tabs.currentWidget() is panel
    assert panel._views.currentWidget() is panel._form


def test_plain_skills_still_fill_the_prompt(app_module, qapp, tmp_path: Path):
    """别把例外做成常态：其它技能点一下还是照旧填模板。"""
    import skill_preview
    from gangge.layer4_tools.skills import find

    panel = skill_preview.SkillEffectPanel(draft_path=tmp_path / "draft.json")
    stub, _ = _stub(app_module, panel, tmp_path)

    app_module.GanggeDesktop._apply_skill(stub, find("video_neon"))

    assert "demo_video" in stub._task_input.toPlainText()
    assert panel._views.currentWidget() is panel._image_tab
