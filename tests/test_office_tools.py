"""office_* 工具的行为测试。

核心断言是那条最贵的问题：同一个 docx，``read_file`` 返回几千字符二进制
乱码（而且 is_error=False），``office_info`` 返回几百字结构摘要。
"""

from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("docx")
pytest.importorskip("openpyxl")
pytest.importorskip("pptx")

from gangge.layer3_agent.tools.file_ops import ReadFileTool, WriteFileTool
from gangge.layer3_agent.tools.office import (
    OfficeCheckTool,
    OfficeInfoTool,
    OfficeReadTool,
    OfficeRecalcTool,
    OfficeRenderTool,
    register_office_tools,
)
from gangge.layer3_agent.tools.registry import PHASE_EXPLORE, ToolRegistry


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    import docx
    import openpyxl
    from pptx import Presentation

    # ── docx：30 段正文 + 一个 2×3 表格 ──
    document = docx.Document()
    for i in range(1, 31):
        document.add_paragraph(f"第 {i} 段：这是正文内容，用来确认范围读取有效。")
    table = document.add_table(rows=2, cols=3)
    for col, text in enumerate(("姓名", "部门", "工号")):
        table.cell(0, col).text = text
    for col, text in enumerate(("张三", "研发", "A001")):
        table.cell(1, col).text = text
    document.sections[0].header.paragraphs[0].text = "内部资料"
    document.sections[0].footer.paragraphs[0].text = "DOC-001"
    document.save(tmp_path / "report.docx")

    # ── xlsx：含公式，其中一个用了兼容性差的函数 ──
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "销售"
    sheet.append(["月份", "金额"])
    sheet.append(["1月", 100])
    sheet.append(["2月", "=B2+50"])
    sheet["D1"] = "备注"
    sheet["E1"] = '=XLOOKUP("1月",A2:A3,B2:B3)'
    workbook.save(tmp_path / "book.xlsx")

    # ── pptx：一页标题 + 一页带表格（表格文字不在 text_frame 里）──
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = "产品介绍"
    slide2 = prs.slides.add_slide(prs.slide_layouts[5])
    slide2.shapes.title.text = "里程碑"
    table = slide2.shapes.add_table(
        2, 2, Inches(1), Inches(2), Inches(6), Inches(1.5)
    ).table
    table.cell(0, 0).text = "阶段"
    table.cell(0, 1).text = "时间"
    table.cell(1, 0).text = "原型"
    table.cell(1, 1).text = "第 1 周"
    prs.save(tmp_path / "deck.pptx")
    return tmp_path


def test_docx_info_replaces_binary_garbage(workdir: Path):
    """同样一个 docx：read_file 被拦下并指路，office_info 给摘要。"""
    raw = run(ReadFileTool(workspace=str(workdir)).execute(path="report.docx"))
    assert raw.is_error, "read_file 必须拒绝，而不是把 zip 当正文返回"
    assert "乱码" in raw.output and "office_info" in raw.output
    assert len(raw.output) < 600, f"拒绝理由不该很长: {len(raw.output)}"

    info = run(OfficeInfoTool(workspace=str(workdir)).execute(path="report.docx"))
    assert not info.is_error, info.output
    assert "段落:" in info.output and "表格:" in info.output
    assert "office_read" in info.output, "摘要要指路，而不是让模型再猜"
    assert len(info.output) < 800, f"摘要不该很长: {len(info.output)}"


def test_docx_read_range_and_table(workdir: Path):
    tool = OfficeReadTool(workspace=str(workdir))
    part = run(tool.execute(path="report.docx", paragraphs="1-5"))
    assert "第 1 段" in part.output and "第 5 段" in part.output
    assert "第 30 段" not in part.output
    assert "表格" in part.output, "段落视图要提醒表格文字不在其中"

    table = run(tool.execute(path="report.docx", table=0))
    assert "张三" in table.output and "研发" in table.output


def test_xlsx_info_and_range(workdir: Path):
    info = run(OfficeInfoTool(workspace=str(workdir)).execute(path="book.xlsx"))
    assert "销售" in info.output and "公式 2 个" in info.output

    tool = OfficeReadTool(workspace=str(workdir))
    part = run(tool.execute(path="book.xlsx", sheet="销售", range="A1:B3"))
    assert "1月" in part.output
    assert "=B2+50" in part.output
    assert "公式原文" in part.output, "公式原文要说明没算过，否则模型会把公式当结果"
    assert "values=true" in part.output, "要指路到数值视图"

    values = run(tool.execute(path="book.xlsx", sheet="销售", range="A1:B3", values=True))
    assert "无缓存值" in values.output and "office_recalc" in values.output, (
        "脚本生成的表没有缓存值，values=true 时必须说清楚，而不是返回空表格"
    )
    assert "数值视图" in values.output

    wrong = run(tool.execute(path="book.xlsx", sheet="不存在", range="A1:B3"))
    assert wrong.is_error and "销售" in wrong.output, "表名写错要列出真实表名"


def test_pptx_info_and_slides(workdir: Path):
    info = run(OfficeInfoTool(workspace=str(workdir)).execute(path="deck.pptx"))
    assert "产品介绍" in info.output and "2 页" in info.output

    part = run(OfficeReadTool(workspace=str(workdir)).execute(path="deck.pptx", slides="1-1"))
    assert "产品介绍" in part.output


def test_pptx_table_is_not_silently_dropped(workdir: Path):
    """带表格的页以前只输出标题，表格整块消失——静默丢内容比报错更糟。"""
    part = run(OfficeReadTool(workspace=str(workdir)).execute(path="deck.pptx", slides="all"))
    assert "[表格]" in part.output and "2 行 × 2 列" in part.output
    assert "原型" in part.output and "第 1 周" in part.output


def test_docx_header_footer_are_read(workdir: Path):
    """页眉页脚不在 doc.paragraphs 里，但抬头信息往往就在那儿。"""
    info = run(OfficeInfoTool(workspace=str(workdir)).execute(path="report.docx"))
    assert "页眉/页脚" in info.output and "内部资料" in info.output

    part = run(OfficeReadTool(workspace=str(workdir)).execute(
        path="report.docx", paragraphs="1-2"))
    assert "[页眉1] 内部资料" in part.output
    assert "[页脚1] DOC-001" in part.output


def test_xlsx_risky_formula_is_flagged(workdir: Path):
    """XLOOKUP 这类函数要当场点出来，不能原样返回让模型以为没事。"""
    info = run(OfficeInfoTool(workspace=str(workdir)).execute(path="book.xlsx"))
    assert "XLOOKUP" in info.output and "算不出来" in info.output

    part = run(OfficeReadTool(workspace=str(workdir)).execute(
        path="book.xlsx", sheet="销售", range="A1:E3"))
    assert "XLOOKUP" in part.output
    assert "兼容性" in part.output and "INDEX+MATCH" in part.output


def test_check_passes_for_real_files_and_fails_for_fake(workdir: Path):
    check = OfficeCheckTool(workspace=str(workdir))
    for name in ("report.docx", "book.xlsx", "deck.pptx"):
        result = run(check.execute(path=name))
        assert not result.is_error, result.output
        assert '"ok": true' in result.output

    # write_file 现在直接拒绝写 Office 容器（否则会静默产出坏文件）
    writer = WriteFileTool(workspace=str(workdir))
    refused = run(writer.execute(path="fake.docx", content="这不是一个真正的 docx"))
    assert refused.is_error and "office_check" in refused.output

    # 绕过 write_file 直接落一个文本文件，office_check 要能识别出来
    (workdir / "fake.docx").write_text("这不是一个真正的 docx", encoding="utf-8")
    bad = run(check.execute(path="fake.docx"))
    assert bad.is_error
    assert '"ok": false' in bad.output and "不是 zip" in bad.output
    with pytest.raises(zipfile.BadZipFile):
        zipfile.ZipFile(workdir / "fake.docx").namelist()


def test_office_tools_hidden_in_coding_profile(workdir: Path):
    """默认 coding 模式下不占 token；tool_search 揭示后才可见。"""
    registry = ToolRegistry()  # 默认 profile = coding
    register_office_tools(registry, workspace=str(workdir))

    visible = str([d for d in registry.get_definitions(phase=PHASE_EXPLORE)])
    assert "office_info" not in visible, "coding 下不该默认暴露 office 工具"

    registry.unlock(["office_info", "office_read", "office_check"])
    revealed = str([d for d in registry.get_definitions(phase=PHASE_EXPLORE)])
    assert "office_info" in revealed


def test_unsupported_and_legacy_extensions_are_explained(workdir: Path):
    tool = OfficeInfoTool(workspace=str(workdir))
    legacy = run(tool.execute(path="old.doc"))
    assert "另存为 .docx" in legacy.output or "文件不存在" in legacy.output


def test_mixed_task_keeps_coding_toolset():
    """'按 word 文档开发软件' 是开发任务，不能因为文档词更多就裁掉开发工具。"""
    from gangge.layer3_agent.tools.registry import detect_agent_profile

    assert detect_agent_profile("按照《需求说明》word 文档的内容，开发一个桌面宠物软件") == "coding"
    assert detect_agent_profile("读一下这份 xlsx，然后写个脚本导入到数据库") == "coding"
    # 纯文档任务仍然走 office
    assert detect_agent_profile("帮我总结一下这份 docx 的主要内容") == "office"


def test_office_profile_is_superset_of_coding_core():
    """误判成 office 也不能缺开发工具（blender 是可选安装，不计）。"""
    from gangge.layer3_agent.tools.registry import PROFILE_CORE

    missing = PROFILE_CORE["coding"] - PROFILE_CORE["office"] - {"blender"}
    assert not missing, f"office 模式缺这些开发工具: {sorted(missing)}"


# ── 排版 lint ──────────────────────────────────────────────────────────────
# 结构校验只回答"能不能打开"。下面这些文件都能正常打开，但交出去会被退回来：
# 表格右边被裁、深色底纹配黑字、手打编号、中文没字体、公式全是空缓存。

def _bad_docx(path: Path) -> Path:
    import docx
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    document = docx.Document()
    # 中文用纯西文字体，且没有 eastAsia —— 换机器就变形
    para = document.add_paragraph()
    para.add_run("这是一段中文正文，用来确认字体检查生效。").font.name = "Arial"
    # 手打编号：增删一条就要重排
    for i in (1, 2, 3):
        document.add_paragraph(f"{i}. 手打的列表项 {i}")
    # 用空段落凑间距
    for _ in range(5):
        document.add_paragraph("")

    table = document.add_table(rows=2, cols=2)
    table.autofit = False
    for grid_col in table._tbl.tblGrid.gridCol_lst:
        grid_col.set(qn("w:w"), str(5 * 1440))  # 两列各 5 英寸，总共 10 英寸

    cell = table.cell(0, 0)
    tc_pr = cell._tc.get_or_add_tcPr()
    shading = OxmlElement("w:shd")
    shading.set(qn("w:val"), "clear")
    shading.set(qn("w:color"), "auto")
    shading.set(qn("w:fill"), "1F1F1F")
    tc_pr.append(shading)
    cell.text = "深色底纹配黑字"

    document.save(path / "bad.docx")
    return path / "bad.docx"


def _bad_xlsx(path: Path) -> Path:
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "数据"
    sheet.append(["月份", "金额"])
    sheet.append(["1月", 100])
    sheet.append(["2月", "=B2+50"])  # 从未计算，缓存值为空
    sheet["C1"] = "#REF!"            # 文本形式的错误值
    sheet["D1"] = "00123"            # 数字存成文本
    sheet["E1"] = '=XLOOKUP("1月",A2:A3,B2:B3)'  # 旧版算不出来
    sheet.merge_cells("A5:B6")       # 数据区跨行跨列合并
    workbook.save(path / "bad.xlsx")
    return path / "bad.xlsx"


def _bad_pptx(path: Path) -> Path:
    from pptx import Presentation
    from pptx.util import Inches, Pt

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # 空白版式
    box = slide.shapes.add_textbox(Inches(9), Inches(1), Inches(3), Inches(1))
    run = box.text_frame.paragraphs[0].add_run()
    run.text = "这一行已经溢出画布"
    run.font.size = Pt(9)
    prs.save(path / "bad.pptx")
    return path / "bad.pptx"


def test_docx_lint_catches_what_structure_check_misses(tmp_path: Path):
    _bad_docx(tmp_path)
    result = run(OfficeCheckTool(workspace=str(tmp_path)).execute(path="bad.docx"))

    assert not result.is_error, "文件能打开，不该报成结构错误"
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["lint_ok"] is False
    rules = {item["rule"] for item in payload["lint"]}
    assert "表格超出页面宽度" in rules
    assert "底纹与字色太接近" in rules
    assert "手打编号/项目符号" in rules
    assert "中文未指定中文字体" in rules
    assert "用空段落凑间距/分页" in rules
    assert all(item.get("fix") for item in payload["lint"]), "每条都要给可执行的改法"


def test_xlsx_lint_catches_uncached_and_error_formulas(tmp_path: Path):
    _bad_xlsx(tmp_path)
    result = run(OfficeCheckTool(workspace=str(tmp_path)).execute(path="bad.xlsx"))

    assert not result.is_error
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["lint_ok"] is False
    rules = {item["rule"] for item in payload["lint"]}
    assert "公式错误值" in rules
    assert "公式没有缓存结果" in rules
    assert "数字被存成文本" in rules
    assert "数据区使用合并单元格" in rules
    assert "公式兼容性" in rules
    joined = json.dumps(payload, ensure_ascii=False)
    assert "office_recalc" in joined, "未计算的公式要指路到重算工具"


def test_pptx_lint_catches_overflow_and_tiny_fonts(tmp_path: Path):
    _bad_pptx(tmp_path)
    result = run(OfficeCheckTool(workspace=str(tmp_path)).execute(path="bad.pptx"))

    assert not result.is_error
    payload = json.loads(result.output)
    assert payload["lint_ok"] is False
    rules = {item["rule"] for item in payload["lint"]}
    assert "文本框超出画布" in rules
    assert "字号偏小" in rules


def test_lint_can_be_switched_off(workdir: Path):
    result = run(OfficeCheckTool(workspace=str(workdir)).execute(
        path="report.docx", lint=False))
    payload = json.loads(result.output)
    assert payload["lint_skipped"] == "lint=false"
    assert "lint" not in payload


# ── LibreOffice 相关 ───────────────────────────────────────────────────────
# 没装 LibreOffice 时必须给出可执行的安装命令，而不是一句"失败"或静默降级。

def test_recalc_and_render_explain_missing_libreoffice(monkeypatch, workdir: Path):
    from gangge.layer3_agent.tools import office as office_module

    monkeypatch.setattr(office_module, "_soffice_path", lambda: None)

    recalc = run(OfficeRecalcTool(workspace=str(workdir)).execute(path="book.xlsx"))
    assert recalc.is_error
    assert "winget install" in recalc.output and "install.bat" in recalc.output

    render = run(OfficeRenderTool(workspace=str(workdir)).execute(path="deck.pptx"))
    assert render.is_error
    assert "LIBREOFFICE_PATH" in render.output


def test_recalc_rejects_non_spreadsheets(workdir: Path):
    result = run(OfficeRecalcTool(workspace=str(workdir)).execute(path="report.docx"))
    assert result.is_error
    assert "office_render" in result.output, "写错工具时要指出该用哪个"


def test_new_tools_registered_and_hidden_by_default(workdir: Path):
    registry = ToolRegistry()
    names = register_office_tools(registry, workspace=str(workdir))
    assert {"office_recalc", "office_render"} <= set(names)

    visible = str([d for d in registry.get_definitions(phase=PHASE_EXPLORE)])
    assert "office_recalc" not in visible

    registry.unlock(["office_recalc", "office_render"])
    revealed = str([d for d in registry.get_definitions(phase=PHASE_EXPLORE)])
    assert "office_recalc" in revealed and "office_render" in revealed
