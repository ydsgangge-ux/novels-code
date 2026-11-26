"""office 工具对 .pdf 的支持。

之前 `.pdf` 不在 `_KIND_BY_EXT` 里，用户传上来的 PDF 任何工具都读不了：
`office_render` 只把 docx/xlsx/pptx **转成** PDF，不接 PDF 当输入。
现在用 PyMuPDF 抽文字层，扫描件再引导到渲染 + vision。
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fitz")

import fitz  # noqa: E402

from gangge.layer3_agent.tools import office  # noqa: E402

BODY = "第 {n} 页：季度经营简报\n本季度收入 128 万元，同比增长 12%。"


def _text_pdf(path, pages=3):
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_textbox(fitz.Rect(72, 72, 520, 700), BODY.format(n=i + 1),
                            fontname="china-s", fontsize=12)
    doc.save(str(path))
    doc.close()
    return path


def _blank_pdf(path, pages=2):
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page()
    doc.save(str(path))
    doc.close()
    return path


def _run(tool, **kwargs):
    return asyncio.run(tool.execute(**kwargs))


def test_pdf_is_a_known_kind():
    assert office._kind_of(__import__("pathlib").Path("a.pdf")) == ("pdf", None)


def test_read_extracts_text(tmp_path):
    pdf = _text_pdf(tmp_path / "报告.pdf")
    res = _run(office.OfficeReadTool(), path=str(pdf), pages="1-2")
    assert res.is_error is False
    assert "第 1 页：季度经营简报" in res.output
    assert "第 2 页" in res.output
    assert "第 3 页" not in res.output      # 只读请求的两页


def test_read_rejects_page_out_of_range(tmp_path):
    """以前越界会输出「第 9-3 页」然后正文空白。"""
    pdf = _text_pdf(tmp_path / "报告.pdf")
    res = _run(office.OfficeReadTool(), path=str(pdf), pages="9-20")
    assert res.is_error is True
    assert "超出范围" in res.output


def test_blank_pdf_points_to_render_and_vision(tmp_path):
    pdf = _blank_pdf(tmp_path / "扫描件.pdf")
    res = _run(office.OfficeReadTool(), path=str(pdf))
    assert "没有文字层" in res.output
    assert "office_render" in res.output
    assert "vision" in res.output


def test_info_reports_page_count(tmp_path):
    pdf = _text_pdf(tmp_path / "报告.pdf", pages=4)
    res = _run(office.OfficeInfoTool(), path=str(pdf))
    assert "页数: 4" in res.output
    assert "文字总量" in res.output


def test_render_accepts_pdf_without_libreoffice(tmp_path, monkeypatch):
    """PDF 已是最终格式，直接渲染；不能因为没装 LibreOffice 就拒绝。"""
    monkeypatch.setattr(office, "_soffice_path", lambda: None)
    pdf = _text_pdf(tmp_path / "报告.pdf")
    res = _run(office.OfficeRenderTool(), path=str(pdf), pages="1", dpi=72,
               output_dir=str(tmp_path / "out"))
    assert res.is_error is False, res.output
    assert "已渲染 1 页" in res.output
    assert list((tmp_path / "out").glob("*.png"))


def test_check_says_pdf_has_nothing_to_lint(tmp_path):
    """PDF 不是 zip 容器，不能被判成「用 write_file 写坏的 Office 文件」。"""
    pdf = _text_pdf(tmp_path / "报告.pdf", pages=1)
    res = _run(office.OfficeCheckTool(), path=str(pdf))
    assert res.is_error is False
    assert "成品格式" in res.output
