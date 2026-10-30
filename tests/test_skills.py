"""技能（常用工作流）的契约测试。

守的四件事：
1. 内置技能 id 唯一、分组顺序稳定——菜单顺序就是这里的顺序；
2. 视频类技能必须带风格与占位符，否则"填进输入框"这一步等于没做；
3. 用户自定义技能来自 ``<workspace>/.gangge/skills/*.json``，坏文件不能炸菜单；
4. 下拉菜单真的挂在按钮上，且点击后能填进输入框（离屏 Qt，不弹窗）。
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from gangge.layer4_tools.skills import (
    BUILTIN_SKILLS,
    Skill,
    all_skills,
    find,
    grouped,
    load_user_skills,
)

APP_PATH = Path(__file__).resolve().parent.parent / "desktop" / "app.py"


# ── 数据层 ──────────────────────────────────────────────────────

def test_builtin_ids_are_unique():
    ids = [s.id for s in BUILTIN_SKILLS]
    assert len(ids) == len(set(ids))


def test_groups_keep_menu_order():
    groups = [g for g, _ in grouped(list(BUILTIN_SKILLS))]
    assert groups == ["视频出片", "开发", "通用"]


def test_five_styles_all_present_with_hints():
    styles = [s for s in BUILTIN_SKILLS if s.id.startswith("video_")]
    for style in ("dark", "neon", "paper", "glass", "brutal"):
        skill = next((s for s in styles if s.id == f"video_{style}"), None)
        assert skill is not None, f"缺少 {style} 技能"
        assert f"style={style}" in skill.template
        assert skill.hint, f"{style} 缺少适用场景说明（菜单里就没提示了）"
    assert find("video_auto") is not None


def test_video_templates_carry_placeholder_and_structure():
    for skill in BUILTIN_SKILLS:
        if not skill.id.startswith("video_"):
            continue
        assert skill.focus == "＜主题＞"
        assert skill.focus in skill.template
        assert "结尾" in skill.template and "口播" in skill.template


def test_focus_token_always_appears_in_template():
    for skill in BUILTIN_SKILLS:
        if skill.focus:
            assert skill.focus in skill.template, f"{skill.id} 的占位符不在模板里"


# ── 用户自定义 ──────────────────────────────────────────────────

def _write_skill(workspace: Path, name: str, payload) -> None:
    directory = workspace / ".gangge" / "skills"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def test_user_skill_is_loaded_and_can_override_builtin(tmp_path: Path):
    _write_skill(
        tmp_path,
        "mine.json",
        {"id": "my_flow", "label": "我的流程", "template": "做 ＜X＞", "focus": "＜X＞"},
    )
    _write_skill(tmp_path, "neon.json", {"id": "video_neon", "label": "自定义 neon", "template": "x"})

    skills = all_skills(tmp_path)
    assert any(s.id == "my_flow" for s in skills)
    # 同 id 覆盖内置，且不产生重复项
    assert [s.label for s in skills if s.id == "video_neon"] == ["自定义 neon"]


def test_broken_skill_file_is_skipped(tmp_path: Path):
    directory = tmp_path / ".gangge" / "skills"
    directory.mkdir(parents=True)
    (directory / "bad.json").write_text("{ not json", encoding="utf-8")
    _write_skill(tmp_path, "ok.json", {"id": "ok", "label": "OK", "template": "t"})

    ids = [s.id for s in load_user_skills(tmp_path)]
    assert ids == ["ok"]


def test_skill_from_dict_requires_fields():
    with pytest.raises(ValueError, match="id / label / template"):
        Skill.from_dict({"id": "x"})


# ── 菜单（离屏 Qt） ─────────────────────────────────────────────

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6.QtWidgets", reason="需要 PyQt6")

from PyQt6.QtWidgets import QApplication, QLineEdit, QMenu, QPlainTextEdit  # noqa: E402


@pytest.fixture(scope="module")
def gui_module():
    spec = importlib.util.spec_from_file_location("gangge_desktop_app_skills", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_menu_has_groups_and_style_hints(gui_module, qapp, tmp_path: Path):
    stub = SimpleNamespace(_ws_input=QLineEdit(str(tmp_path)), _skill_menu=QMenu())
    gui_module.GanggeDesktop._refresh_skill_menu(stub)

    actions = stub._skill_menu.actions()
    texts = [a.text() for a in actions]
    assert "视频出片" in texts and "开发" in texts and "通用" in texts
    assert any("video" not in t and "neon" in t for t in texts)

    neon = next(a for a in actions if "neon" in a.text())
    assert neon.toolTip(), "菜单项没有适用场景提示，用户还是不知道怎么选"
    headers = [a for a in actions if not a.isEnabled()]
    assert headers, "分组标题应当不可点击"


def _stub(gui_module, edit):
    """只带 _task_input 的壳：把真实方法绑上去，避免造整个主窗口。"""
    stub = SimpleNamespace(_task_input=edit)
    stub._select_focus_token = (
        lambda token: gui_module.GanggeDesktop._select_focus_token(stub, token)
    )
    # 效果面板在别处单独测；这里只要 _apply_skill 不因为找不到面板而炸
    stub._preview_skill = lambda skill, switch=False: None
    return stub


def test_apply_skill_fills_input_and_selects_placeholder(gui_module, qapp):
    edit = QPlainTextEdit()
    stub = _stub(gui_module, edit)
    skill = find("video_neon")
    assert skill is not None

    gui_module.GanggeDesktop._apply_skill(stub, skill)
    assert "style=neon" in edit.toPlainText()
    # 填完就选中占位符，直接打字即可覆盖
    assert edit.textCursor().selectedText() == skill.focus


def test_apply_skill_appends_instead_of_overwriting(gui_module, qapp):
    edit = QPlainTextEdit()
    edit.setPlainText("我自己写的内容")
    stub = _stub(gui_module, edit)

    gui_module.GanggeDesktop._apply_skill(stub, find("code_review"))
    text = edit.toPlainText()
    assert text.startswith("我自己写的内容")
    assert "git diff" in text
