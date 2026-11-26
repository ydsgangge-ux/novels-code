"""附件类型判定与给模型的说明文案。

这些判定以前散在桌面端的 if/elif 里，Office 文件落到 else 就被静默丢弃，
现在收敛到 gangge.attachments，两端共用。
"""

from __future__ import annotations

import gangge.attachments as att


def test_office_formats_are_recognised():
    for name in ("报告.docx", "报表.xlsx", "答辩.pptx", "macro.docm", "book.xlsm"):
        assert att.kind_of(name) == "office", name


def test_daily_formats():
    assert att.kind_of("a.PNG") == "image"
    assert att.kind_of("b.mp4") == "video"
    assert att.kind_of("c.mp3") == "audio"
    assert att.kind_of("d.pdf") == "pdf"
    assert att.kind_of("e.md") == "text"
    assert att.kind_of("f.unknown") is None


def test_legacy_office_is_flagged_not_silently_dropped():
    """97-2003 的二进制格式要明确告诉用户另存，而不是当成不认识。"""
    assert att.kind_of("old.doc") == "legacy"
    assert att.kind_of("old.xls") == "legacy"
    assert att.kind_of("old.ppt") == "legacy"
    assert "另存" in att.read_hint("legacy")


def test_office_hint_points_at_office_read():
    """Office 文件必须引导到 office_read —— 用 read_file 只会读到 zip 二进制。"""
    hint = att.read_hint("office")
    assert "office_read" in hint
    assert "read_file" in hint


def test_label_of_uses_full_name():
    """以前 label_of 把 .png 当路径去取后缀，Path('.png').suffix 是空串，
    结果图片被标成"文件"。"""
    assert att.label_of("截图.png") == "图片"
    assert att.label_of("报告.docx") == "Word 文档"
    assert att.label_of("旧.doc") == "Word 97-2003"


def test_media_type():
    assert att.media_type_of("x.jpg") == "image/jpeg"
    assert att.media_type_of("x.png") == "image/png"
    assert att.media_type_of("x.mp3") == "audio/mpeg"
    assert att.media_type_of("x.docx") == "application/octet-stream"


def test_describe_carries_path_and_method():
    text = att.describe([
        {"name": "报告.docx", "path": r"C:\ws\报告.docx", "kind": "office"},
        {"name": "截图.png", "path": r"C:\ws\截图.png", "kind": "image"},
    ])
    assert "[附件]" in text
    assert r"C:\ws\报告.docx" in text
    assert "office_read" in text
    assert "vision" in text
    assert "Word 文档" in text


def test_describe_empty():
    assert att.describe([]) == ""


def test_describe_without_path_still_lists_name():
    text = att.describe([{"name": "a.xlsx", "kind": "office"}])
    assert "a.xlsx" in text
    assert "office_read" in text
