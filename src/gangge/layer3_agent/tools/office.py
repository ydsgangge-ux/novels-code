"""Office 文档工具组 — docx / xlsx / pptx 的结构化读取与结构校验。

为什么单独做一条路：

* 通用 ``read_file`` 把 .docx 当文本读。它先被 ``read_text_preserving`` 以
  "含 NUL 字节"拒绝，然后退化成 ``errors="replace"`` 的宽松解码，于是把
  zip 二进制当正文返回——实测 11KB 的 docx 吐出 4362 字符乱码，而且
  ``is_error=False``：模型以为读到了内容，其实什么都没读到，这些乱码还会
  在后续每一轮里重复计费。
* ``write_file`` 更糟：它把 UTF-8 文本写进 .docx，生成的文件 Word 打不开
  （BadZipFile），返回的却是"已写入"。
* ``edit_file`` 直接拒绝，等于宣告"这类文件改不了"。

所以这里只做三件不需要 Office 套件的事：

    office_info   结构摘要（sheet 名 / 幻灯片标题 / 段落表格数），几百字
    office_read   按范围精确取内容（区域 / 页码 / 段落区间）
    office_check  结构校验 + 排版 lint（zip/XML 是否良构、表格是否溢出页面、
                  深色底纹配深色字、手打编号、中文缺字体、公式错误值与未计算）

再加两个需要 LibreOffice 的动作（没装就明确说怎么装，不静默降级）：

    office_recalc  用 LibreOffice 重算 xlsx 公式并另存（openpyxl 不算公式，
                   脚本刚生成的表缓存值全是空的，对方打开/转 PDF 就是空白）
    office_render  转 PDF 再渲染成 PNG，用来"看一眼排版"，而不只是猜

**写**仍然交给脚本（python-docx / openpyxl / python-pptx），写完用
``office_check`` 自查。这比让模型相信 ``write_file`` 可靠得多。

依赖是可选的：``pip install novels-code[office]``。缺库时工具本身仍然注册，
但会明确回一句"缺哪个库、怎么装"，而不是让模型去猜、去 pip 试。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from gangge.layer3_agent.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

#: 扩展名 → 格式族。.xlsm 与 .xlsx 的读取路径相同（宏内容不解析）。
#: .pdf 单独一族：用 PyMuPDF 抽文字层，不需要 LibreOffice。
_KIND_BY_EXT = {
    ".docx": "docx",
    ".docm": "docx",
    ".xlsx": "xlsx",
    ".xlsm": "xlsx",
    ".pptx": "pptx",
    ".pptm": "pptx",
    ".pdf": "pdf",
}

#: 旧二进制格式：没有对应解析库，直接说清楚怎么转。
_LEGACY_HINT = {
    ".doc": "旧版 .doc 是二进制格式。请先用 Word/WPS 另存为 .docx，再处理。",
    ".xls": "旧版 .xls 是二进制格式。请先另存为 .xlsx，再处理。",
    ".ppt": "旧版 .ppt 是二进制格式。请先另存为 .pptx，再处理。",
}

_INSTALL_HINT = "pip install novels-code[office]"

#: 每个结构部件解析的上限，避免巨型文件把一次只读操作拖成几分钟。
_MAX_XML_PARTS = 40
_MAX_XML_BYTES = 2 * 1024 * 1024

#: office_read 的默认/最大输出字符数。
_DEFAULT_READ_CHARS = 9000
_MAX_READ_CHARS = 20000

_REQUIRED_PARTS = {
    "docx": ("[Content_Types].xml", "word/document.xml"),
    "xlsx": ("[Content_Types].xml", "xl/workbook.xml"),
    "pptx": ("[Content_Types].xml", "ppt/presentation.xml"),
}

#: 这些函数在 LibreOffice 或旧版 Excel 里算不出来。文件本身不会因此损坏，
#: 但对方打开就是 #NAME?/#VALUE!，所以必须在读取时就点出来——让模型以为
#: "写进公式就完事了"，是这类文档最常见的翻车方式。
_RISKY_FUNCS = {
    "XLOOKUP": "Excel 2021+/365 专属，LibreOffice 与 Excel 2019 及更早算不出来；改用 INDEX+MATCH",
    "XMATCH": "同上，改用 MATCH",
    "FILTER": "动态数组函数，旧版 Excel / LibreOffice 不支持",
    "SORT": "动态数组函数，旧版 Excel / LibreOffice 不支持",
    "SORTBY": "动态数组函数，旧版 Excel / LibreOffice 不支持",
    "UNIQUE": "动态数组函数，旧版 Excel / LibreOffice 不支持",
    "SEQUENCE": "动态数组函数，旧版 Excel / LibreOffice 不支持",
    "LAMBDA": "Excel 365 专属，其它环境算不出来",
    "LET": "Excel 365/2021 专属，旧版不支持",
    "IFS": "Excel 2019+，更早版本不支持",
    "TEXTJOIN": "Excel 2019+，更早版本不支持",
}


def _docx_chrome(doc: Any) -> list[str]:
    """页眉页脚里的文字。

    ``doc.paragraphs`` 不含它们，而合同、报告的公司名和文档编号恰恰都在这里——
    只读正文的模型会漏掉最关键的抬头。
    """
    parts: list[str] = []
    for index, section in enumerate(doc.sections, start=1):
        for label, container in (("页眉", section.header), ("页脚", section.footer)):
            try:
                text = "\n".join(
                    p.text.strip() for p in container.paragraphs if p.text.strip()
                )
            except Exception:
                continue
            if text:
                parts.append(f"[{label}{index}] {text[:200]}")
    return parts


def _risky_formula_hits(formula: str) -> list[str]:
    return [
        name for name in _RISKY_FUNCS
        if re.search(rf"\b{name}\s*\(", formula or "", re.IGNORECASE)
    ]


def _pptx_slide_content(slide: Any) -> tuple[list[str], list[str]]:
    """抽一页的文本，并把表格展开成 markdown；图片只报个数。

    以前只认 ``has_text_frame``，于是"标题 + 表格"的页面只输出一个标题，
    模型会以为这页是空的。静默丢内容比报错更糟。
    """
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    texts: list[str] = []
    extras: list[str] = []

    def walk(shapes: Any) -> None:
        for shape in shapes:
            shape_type = getattr(shape, "shape_type", None)
            if shape_type is not None and shape_type == MSO_SHAPE_TYPE.GROUP:
                walk(shape.shapes)
                continue
            if getattr(shape, "has_table", False):
                table = shape.table
                rows, cols = len(table.rows), len(table.columns)
                lines = [f"[表格] {rows} 行 × {cols} 列"]
                for r in range(min(rows, 12)):
                    cells = [
                        re.sub(r"\s+", " ", table.cell(r, c).text).strip()
                        for c in range(cols)
                    ]
                    lines.append("| " + " | ".join(cells) + " |")
                    if r == 0:
                        lines.append("|" + "---|" * cols)
                if rows > 12:
                    lines.append(f"…（还有 {rows - 12} 行）")
                texts.append("\n".join(lines))
                continue
            if getattr(shape, "has_text_frame", False):
                text = (shape.text_frame.text or "").strip()
                if text:
                    texts.append(re.sub(r"\n{2,}", "\n", text))
                continue
            if shape_type is not None and shape_type == MSO_SHAPE_TYPE.PICTURE:
                extras.append("[图片]")

    walk(slide.shapes)
    return texts, extras


def _kind_of(path: Path) -> tuple[str | None, str | None]:
    """返回 (格式族, 错误说明)。只认扩展名，不猜内容。"""
    ext = path.suffix.lower()
    if ext in _KIND_BY_EXT:
        return _KIND_BY_EXT[ext], None
    if ext in _LEGACY_HINT:
        return None, _LEGACY_HINT[ext]
    return None, (
        f"不支持的文件类型 {ext or '(无扩展名)'}。"
        "本工具只处理 .docx / .xlsx / .pptx / .pdf（含 .docm/.xlsm/.pptm）。"
    )


def _dep_missing(kind: str) -> str | None:
    """缺依赖时返回一句可执行的提示，否则 None。"""
    module, package = {
        "docx": ("docx", "python-docx"),
        "xlsx": ("openpyxl", "openpyxl"),
        "pptx": ("pptx", "python-pptx"),
        "pdf": ("fitz", "pymupdf"),
    }[kind]
    try:
        __import__(module)
    except ImportError:
        return (
            f"处理 {kind} 需要 {package}，当前环境没有安装。\n"
            f"请执行: {_INSTALL_HINT}\n"
            f"（不要用 read_file 读这类文件——它只会返回二进制乱码）"
        )
    return None


def _fmt_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


def _plain_len(text: str) -> int:
    return len(re.sub(r"\s", "", text or ""))


def _parse_span(spec: str | int | None, total: int, default_span: int) -> tuple[int, int]:
    """把 ``"10-40"`` / ``40`` / ``"all"`` 解析成 1-based 的 [start, end]。"""
    if spec is None or spec == "":
        return 1, min(total, default_span)
    if isinstance(spec, int):
        return 1, min(total, max(1, spec))
    text = str(spec).strip().lower()
    if text in ("all", "*"):
        return 1, min(total, max(total, 1))
    match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", text)
    if match:
        start, end = int(match.group(1)), int(match.group(2))
        return max(1, start), max(start, end)
    if text.isdigit():
        return 1, min(total, int(text))
    return 1, min(total, default_span)


def _truncate(text: str, limit: int, hint: str) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已截断，共 {len(text)} 字符）{hint}"


# ── 排版 lint ──────────────────────────────────────────────────────────────
# office_check 原来只回答"能不能打开"。能打开但排版错的文件更隐蔽：表格右边缘
# 被裁掉、深色底纹配深色文字、手打编号增删一条就全乱。这些不需要 Office 套件，
# 扫一遍 XML 就能看出来，比让对方打开再截图反馈便宜得多。
#
# 规则只报"确定会坏"和"几乎一定要改"的，不做审美评判：宁可漏报，也不要报一堆
# "字体不好看"把真正的溢出问题淹掉。

_LINT_MAX_ISSUES = 24          # 一次最多报这么多条，避免输出比正文还长
_LINT_MAX_PARAGRAPHS = 3000    # 段落扫描上限
_LINT_SCAN_ROWS = 2000
_LINT_SCAN_COLS = 60
_LINT_MAX_SHEETS = 12

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
#: 只有拉丁字形的字体。用来写中文会退回系统默认字体，跨机器排版会变。
_LATIN_ONLY_FONTS = {
    "calibri", "arial", "helvetica", "times new roman", "cambria", "georgia",
    "verdana", "tahoma", "segoe ui", "consolas", "courier new", "trebuchet ms",
    "arial narrow", "palatino linotype", "garamond",
}
#: 文本形式的数字（含 "00123" 这种带前导零的工号/编号）。
_NUMERIC_TEXT = re.compile(r"^-?\d{1,15}(?:\.\d+)?$")
#: A:A / A:B 这类整列引用。几百行时没事，几万行时每次改单元格都要重算。
_WHOLE_COLUMN_REF = re.compile(r"(?<![A-Za-z0-9_$])\$?[A-Z]{1,3}\s*:\s*\$?[A-Z]{1,3}(?![A-Za-z0-9_])")
_WHOLE_ROW_REF = re.compile(r"(?<![A-Za-z0-9_.$])\$?\d{1,6}\s*:\s*\$?\d{1,6}(?![A-Za-z0-9_.])")
#: 手打的列表标记：增删一条就要重编全部序号，且无法自动缩进/续行。
_MANUAL_BULLET = re.compile(
    r"^\s*(?:"
    r"[•·▪◦‣●○◆◇■□►▶✔✓→※]"
    r"|[-*—]{1,2}\s+"
    r"|（?\d{1,2}[.、)）](?!\d)"
    r"|[a-zA-Z][.)）]\s"
    r"|[一二三四五六七八九十]{1,2}、"
    r"|[（(][一二三四五六七八九十]{1,2}[）)]"
    r")"
)
_ERR_PREFIXES = (
    "#REF!", "#NAME?", "#VALUE!", "#DIV/0!", "#N/A", "#NULL!",
    "#NUM!", "#SPILL!", "#CALC!", "#GETTING_DATA",
)


def _lint_add(
    bucket: list[dict[str, Any]],
    level: str,
    rule: str,
    where: str,
    detail: str,
    fix: str = "",
) -> None:
    if len(bucket) >= _LINT_MAX_ISSUES:
        return
    item: dict[str, Any] = {"level": level, "rule": rule, "where": where, "detail": detail}
    if fix:
        item["fix"] = fix
    bucket.append(item)


def _lint_summary(issues: list[dict[str, Any]]) -> str:
    errors = sum(1 for i in issues if i.get("level") == "error")
    warns = sum(1 for i in issues if i.get("level") == "warn")
    infos = len(issues) - errors - warns
    parts = []
    if errors:
        parts.append(f"{errors} 严重")
    if warns:
        parts.append(f"{warns} 需改")
    if infos:
        parts.append(f"{infos} 建议")
    return f"{len(issues)} 处排版问题（{' / '.join(parts)}）"


def _luminance(raw: str | None) -> float | None:
    """十六进制颜色的相对亮度；'auto' / 空值 / 主题色返回 None。"""
    text = (raw or "").strip().lstrip("#")
    if len(text) != 6 or any(c not in "0123456789abcdefABCDEF" for c in text):
        return None
    r, g, b = (int(text[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _docx_cell_fill(cell: Any) -> str:
    """单元格底纹填充色；没设置返回空串。"""
    from docx.oxml.ns import qn

    tc_pr = cell._tc.find(qn("w:tcPr"))
    if tc_pr is None:
        return ""
    shd = tc_pr.find(qn("w:shd"))
    if shd is None:
        return ""
    return (shd.get(qn("w:fill")) or "").strip()


def _docx_cell_text_color(cell: Any) -> str:
    """单元格里第一个显式字色的 run；没写就当默认黑。"""
    for para in cell.paragraphs:
        for run in para.runs:
            try:
                color = run.font.color.rgb
            except Exception:
                color = None
            if color is not None:
                return str(color)
    return "000000"


def _docx_run_fonts(run: Any) -> tuple[str | None, str | None]:
    """沿 run → 字符样式 → 段落样式链找 (eastAsia, ascii) 字体名。"""
    from docx.oxml.ns import qn

    holders = [run._element]
    seen: set[int] = set()
    style = run.style
    while style is not None and id(style) not in seen:
        seen.add(id(style))
        element = getattr(style, "element", None)
        if element is not None:
            holders.append(element)
        style = style.base_style

    for holder in holders:
        r_pr = holder.find(qn("w:rPr"))
        if r_pr is None:
            continue
        fonts = r_pr.find(qn("w:rFonts"))
        if fonts is None:
            continue
        east_asia = fonts.get(qn("w:eastAsia"))
        ascii_name = fonts.get(qn("w:ascii"))
        if east_asia or ascii_name:
            return east_asia, ascii_name
    return None, None


def _lint_docx(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    import docx
    from docx.oxml.ns import qn

    issues: list[dict[str, Any]] = []
    try:
        doc = docx.Document(str(path))
    except Exception as exc:  # noqa: BLE001 - lint 失败不能盖过结构校验
        return [], f"docx 解析失败，跳过排版检查（{type(exc).__name__}: {exc}）"

    # 1) 表格列宽合计超过版心宽度：右边缘被裁掉，或者整表被挤到下一页
    try:
        section = doc.sections[0]
        avail_twips = int(
            (
                (section.page_width or 0)
                - (section.left_margin or 0)
                - (section.right_margin or 0)
            )
            / 635
        )
    except Exception:
        avail_twips = 0
    if avail_twips > 0:
        for index, table in enumerate(doc.tables[:10]):
            # autofit 的表由 Word 自己重排列宽，报告"超宽"是误报；只有固定
            # 布局（明确设了列宽）才会真的把右边缘顶到页面外。
            try:
                if table.autofit:
                    continue
            except Exception:
                pass
            total = 0
            for grid_col in table._tbl.tblGrid.gridCol_lst:
                try:
                    total += int(grid_col.get(qn("w:w")) or 0)
                except (TypeError, ValueError):
                    pass
            if total and total > avail_twips * 1.02:
                _lint_add(
                    issues,
                    "error",
                    "表格超出页面宽度",
                    f"表格#{index}",
                    f"列宽合计 {total / 1440:.2f} 英寸 > 版心 {avail_twips / 1440:.2f} 英寸",
                    "按比例缩列宽，或改横向页面 / 缩小页边距",
                )

    # 2) 深色底纹 + 深色字：打印出来是一块黑，屏幕上只剩反白时才看得见
    dark_cells: list[str] = []
    for t_index, table in enumerate(doc.tables[:10]):
        for r_index, row in enumerate(table.rows[:40]):
            for c_index, cell in enumerate(row.cells[:12]):
                fill = _docx_cell_fill(cell)
                fill_lum = _luminance(fill)
                if fill_lum is None or fill_lum > 0.35:
                    continue
                text_lum = _luminance(_docx_cell_text_color(cell))
                if text_lum is not None and text_lum < 0.35:
                    dark_cells.append(f"表{t_index}第{r_index + 1}行第{c_index + 1}列")
                    if len(dark_cells) >= 6:
                        break
            if len(dark_cells) >= 6:
                break
        if len(dark_cells) >= 6:
            break
    if dark_cells:
        _lint_add(
            issues,
            "error",
            "底纹与字色太接近",
            "、".join(dark_cells[:4]) + ("…" if len(dark_cells) > 4 else ""),
            "深色底纹配深色文字，打印或投影时等于看不见",
            "把字改成白色（FFFFFF），或把底纹调浅",
        )

    # 3) 长表格首行既无底纹也没加粗：表头和数据混在一起，阅读时容易串行
    plain_headers = 0
    for index, table in enumerate(doc.tables[:10]):
        if len(table.rows) < 6 or plain_headers >= 2:
            continue
        header_cells = list(table.rows[0].cells)[:12]
        if any(_docx_cell_fill(cell) for cell in header_cells):
            continue
        bold = any(
            run.font.bold
            for cell in header_cells
            for para in cell.paragraphs
            for run in para.runs
        )
        if bold:
            continue
        plain_headers += 1
        _lint_add(
            issues,
            "info",
            "表头缺少视觉区分",
            f"表格#{index}",
            f"{len(table.rows)} 行的表格，首行既无底纹也没加粗",
            "首行加底纹或加粗，否则长表格容易串行",
        )

    # 4) 手打的项目符号/编号：增删一条就要重编全部序号
    manual: list[str] = []
    empty_run = 0
    max_empty_run = 0
    empty_at = 0
    for index, para in enumerate(doc.paragraphs[:_LINT_MAX_PARAGRAPHS]):
        text = (para.text or "").strip()
        if not text:
            empty_run += 1
            if empty_run > max_empty_run:
                max_empty_run = empty_run
                empty_at = index + 1
            continue
        empty_run = 0
        if len(manual) >= 6 or len(text) > 80:
            continue
        try:
            p_pr = para._p.pPr
            has_numbering = p_pr is not None and p_pr.numPr is not None
        except Exception:
            has_numbering = False
        if not has_numbering and _MANUAL_BULLET.match(text):
            manual.append(f"第{index + 1}段「{text[:20]}」")
    if len(manual) >= 3:
        _lint_add(
            issues,
            "warn",
            "手打编号/项目符号",
            "、".join(manual[:3]),
            f"至少 {len(manual)} 处列表标记是手打的，不是真正的编号",
            "改用 Word 的编号/项目符号或样式；手打的序号一增删就全错位",
        )

    # 5) 中文没有指定 eastAsia 字体：换台机器打开，行距和分页都会变。
    # 文档默认值（docDefaults）里设了中文字体就算数，否则会把"正常文档"全报一遍。
    cjk_runs = 0
    missing_ea = 0
    ea_samples: list[str] = []
    wrong_ea: set[str] = set()
    default_east_asia: str | None = None
    try:
        for fonts in doc.styles.element.xpath(
            "./w:docDefaults/w:rPrDefault/w:rPr/w:rFonts"
        ):
            default_east_asia = fonts.get(qn("w:eastAsia")) or default_east_asia
    except Exception:
        default_east_asia = None
    for index, para in enumerate(doc.paragraphs[:_LINT_MAX_PARAGRAPHS]):
        for run in para.runs:
            if not _CJK.search(run.text or ""):
                continue
            cjk_runs += 1
            east_asia, _latin = _docx_run_fonts(run)
            if not east_asia:
                east_asia = default_east_asia
            if not east_asia:
                missing_ea += 1
                if len(ea_samples) < 3:
                    ea_samples.append(f"第{index + 1}段「{run.text.strip()[:14]}」")
            elif east_asia.strip().lower() in _LATIN_ONLY_FONTS:
                wrong_ea.add(east_asia.strip())
    if missing_ea:
        _lint_add(
            issues,
            "warn",
            "中文未指定中文字体",
            "；".join(ea_samples) or "正文",
            f"{missing_ea}/{cjk_runs} 处中文没有设置 eastAsia 字体",
            "在样式里给中文指定字体（如 微软雅黑 / 宋体）的 w:eastAsia，"
            "否则换机器会用默认字体，行距和分页跟着变",
        )
    if wrong_ea:
        _lint_add(
            issues,
            "warn",
            "中文字体用了纯西文字体",
            "、".join(sorted(wrong_ea)),
            "这些字体没有中文字形，中文会退回系统默认字体",
            "改成中文字体，或同时设置 eastAsia",
        )

    # 6) 连续空段落：一改内容就多出一页，或分页位置不可控
    if max_empty_run >= 5:
        _lint_add(
            issues,
            "warn",
            "用空段落凑间距/分页",
            f"第{empty_at}段附近",
            f"连续 {max_empty_run} 个空段落",
            "改用段前/段后间距或分页符；空段落会让分页随内容漂移",
        )

    return issues, None


def _lint_xlsx(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    import openpyxl

    issues: list[dict[str, Any]] = []
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    if size > 30 * 1024 * 1024:
        return [], f"文件 {size / 1024 / 1024:.0f} MB，跳过排版检查（只做结构校验）"

    try:
        wb = openpyxl.load_workbook(str(path), data_only=False)
        wb_values = openpyxl.load_workbook(str(path), data_only=True)
    except Exception as exc:  # noqa: BLE001
        return [], f"xlsx 解析失败，跳过排版检查（{type(exc).__name__}: {exc}）"

    error_cells: list[str] = []
    uncached: list[str] = []
    uncached_total = 0
    text_numbers: list[str] = []
    text_number_total = 0
    whole_refs: list[str] = []
    risky: list[str] = []
    merged: list[str] = []
    mixed: list[str] = []
    formula_total = 0

    try:
        for ws, ws_values in zip(
            wb.worksheets[:_LINT_MAX_SHEETS], wb_values.worksheets[:_LINT_MAX_SHEETS]
        ):
            rows = min(ws.max_row or 0, _LINT_SCAN_ROWS)
            cols = min(ws.max_column or 0, _LINT_SCAN_COLS)
            formula_cols: dict[str, int] = {}
            number_cols: dict[str, int] = {}
            for row in ws.iter_rows(min_row=1, max_row=rows, max_col=cols):
                for cell in row:
                    value = cell.value
                    coord = f"{ws.title}!{cell.coordinate}"
                    if isinstance(value, str) and value.startswith("="):
                        formula_total += 1
                        column = cell.column_letter
                        formula_cols[column] = formula_cols.get(column, 0) + 1
                        for name in _risky_formula_hits(value):
                            if len(risky) < 6:
                                risky.append(f"{coord} 用了 {name}")
                        if _WHOLE_COLUMN_REF.search(value) or _WHOLE_ROW_REF.search(value):
                            if len(whole_refs) < 4:
                                whole_refs.append(f"{coord} {value[:30]}")
                        cached = ws_values[cell.coordinate].value
                        if cached is None:
                            uncached_total += 1
                            if len(uncached) < 4:
                                uncached.append(coord)
                        elif isinstance(cached, str) and cached.strip().upper().startswith("#"):
                            if len(error_cells) < 6:
                                error_cells.append(f"{coord} = {cached}")
                    elif isinstance(value, str):
                        text = value.strip()
                        upper = text.upper()
                        if upper.startswith("#") and upper in _ERR_PREFIXES:
                            if len(error_cells) < 6:
                                error_cells.append(f"{coord} = {text}（文本形式的错误值）")
                        elif len(text) >= 2 and _NUMERIC_TEXT.match(text):
                            text_number_total += 1
                            if len(text_numbers) < 4:
                                text_numbers.append(f"{coord}={text!r}")
                    elif isinstance(value, (int, float)) and not isinstance(value, bool):
                        column = cell.column_letter
                        number_cols[column] = number_cols.get(column, 0) + 1
            for column, count in formula_cols.items():
                if count >= 2 and number_cols.get(column) and len(mixed) < 4:
                    mixed.append(
                        f"{ws.title}!{column} 列（{count} 个公式 + {number_cols[column]} 个硬编码数字）"
                    )
            for merged_range in list(ws.merged_cells.ranges)[:20]:
                # 表头横跨几列（A1:C1）、标题纵向合并（A1:A2）都很常见，
                # 只有在数据区"既跨行又跨列"的块才真会让排序/透视错位。
                spans_rows = merged_range.max_row - merged_range.min_row >= 1
                spans_cols = merged_range.max_col - merged_range.min_col >= 1
                if spans_rows and spans_cols and len(merged) < 4:
                    merged.append(f"{ws.title}!{merged_range.coord}")
    finally:
        wb.close()
        wb_values.close()

    if error_cells:
        _lint_add(
            issues,
            "error",
            "公式错误值",
            "、".join(error_cells[:4]),
            "这些单元格保存的就是错误值（#REF!/#NAME? 等），对方打开直接看到报错",
            "#REF! 是引用被删了；#NAME? 多半是函数名不存在或版本不支持；修好后用 office_recalc 重算",
        )
    if uncached_total:
        _lint_add(
            issues,
            "warn",
            "公式没有缓存结果",
            "、".join(uncached[:3]),
            f"{uncached_total}/{formula_total} 个公式没有缓存值——"
            "文件从未被 Excel/LibreOffice 计算保存过（脚本生成的表都是这样）",
            "用 office_recalc 重算并另存，否则对方打开前预览/转 PDF 是空白或 0",
        )
    if text_numbers:
        _lint_add(
            issues,
            "warn",
            "数字被存成文本",
            "、".join(text_numbers),
            f"共 {text_number_total} 个单元格是文本形式的数字，SUM/排序/透视会漏掉它们",
            "改回数值格式，或确认它们确实是编号（如工号、身份证）再忽略",
        )
    if mixed:
        _lint_add(
            issues,
            "warn",
            "公式列里混入硬编码数字",
            "、".join(mixed),
            "同一列既有公式又有手填数字，统计口径会不一致",
            "把常量挪到单独的输入列，公式列统一引用它",
        )
    if whole_refs:
        _lint_add(
            issues,
            "info",
            "整列/整行引用",
            "、".join(whole_refs),
            "A:A 这类引用会扫描整列，行数一多就明显变慢",
            "改成有界区域，如 A2:A2000",
        )
    if risky:
        _lint_add(
            issues,
            "warn",
            "公式兼容性",
            "、".join(risky[:3]),
            "、".join(sorted({name for item in risky for name in _RISKY_FUNCS if name in item}))
            + " 在 LibreOffice / 旧版 Excel 上算不出来，对方打开是 #NAME?",
            "换成 INDEX+MATCH 等基础函数，或确认对方一定是新版 Excel",
        )
    if merged:
        _lint_add(
            issues,
            "warn",
            "数据区使用合并单元格",
            "、".join(merged),
            "跨行跨列合并会让排序、筛选、透视表和复制粘贴错位",
            "只在标题行合并；数据区拆开（空白就留空，不要合并）",
        )
    return issues, None


def _lint_pptx(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    from pptx.util import Pt

    issues: list[dict[str, Any]] = []
    try:
        prs = Presentation(str(path))
    except Exception as exc:  # noqa: BLE001
        return [], f"pptx 解析失败，跳过排版检查（{type(exc).__name__}: {exc}）"

    slide_w = int(prs.slide_width or 0)
    slide_h = int(prs.slide_height or 0)
    tolerance = int(max(slide_w, slide_h) * 0.01)
    overflow: list[str] = []
    small_fonts: list[str] = []
    empty_titles: list[str] = []

    def label(shape: Any, index: int) -> str:
        name = (getattr(shape, "name", "") or "").strip()
        return f"{name}#{index}" if name else f"形状#{index}"

    def walk(shapes: Any, page: int, prefix: str = "") -> None:
        for index, shape in enumerate(shapes, start=1):
            if getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.GROUP:
                walk(shape.shapes, page, prefix=f"{label(shape, index)}/")
                continue
            has_text = bool(getattr(shape, "has_text_frame", False))
            has_table = bool(getattr(shape, "has_table", False))
            if (has_text or has_table) and slide_w and slide_h:
                left = int(shape.left or 0)
                top = int(shape.top or 0)
                width = int(shape.width or 0)
                height = int(shape.height or 0)
                out_of_bounds = (
                    left < -tolerance
                    or top < -tolerance
                    or left + width > slide_w + tolerance
                    or top + height > slide_h + tolerance
                )
                if out_of_bounds and len(overflow) < 6:
                    overflow.append(f"第{page}页 {prefix}{label(shape, index)}")
            if has_text:
                for para in shape.text_frame.paragraphs:
                    for run in para.runs:
                        size = run.font.size
                        if (
                            size is not None
                            and size < Pt(12)
                            and (run.text or "").strip()
                            and len(small_fonts) < 6
                        ):
                            small_fonts.append(
                                f"第{page}页「{run.text.strip()[:12]}」{size.pt:g}pt"
                            )
            if has_table:
                for row in shape.table.rows:
                    for cell in row.cells:
                        for para in cell.text_frame.paragraphs:
                            for run in para.runs:
                                size = run.font.size
                                if (
                                    size is not None
                                    and size < Pt(10)
                                    and (run.text or "").strip()
                                    and len(small_fonts) < 6
                                ):
                                    small_fonts.append(
                                        f"第{page}页表格「{run.text.strip()[:12]}」{size.pt:g}pt"
                                    )

    for page, slide in enumerate(prs.slides, start=1):
        walk(slide.shapes, page)
        try:
            title_shape = slide.shapes.title
        except Exception:
            title_shape = None
        if title_shape is not None and not (title_shape.text or "").strip():
            empty_titles.append(f"第{page}页")

    if overflow:
        _lint_add(
            issues,
            "error",
            "文本框超出画布",
            "、".join(overflow[:3]) + ("…" if len(overflow) > 3 else ""),
            "形状的右/下边缘落在页面外，投影和导出 PDF 都会被裁掉",
            "把形状缩小或移回页面内；字号过大时先缩字号再调框",
        )
    if small_fonts:
        _lint_add(
            issues,
            "warn",
            "字号偏小",
            "、".join(small_fonts[:3]),
            "字号小于 12pt（表格小于 10pt），投影时后排看不清",
            "正文至少 14-18pt，表格不小于 10-12pt，或删掉塞不下的内容",
        )
    if empty_titles:
        _lint_add(
            issues,
            "info",
            "标题占位符为空",
            "、".join(empty_titles[:5]),
            "这些页有标题框但没写标题，翻页时缺少定位",
            "补标题，或删掉空的标题占位符（导出时不会显示，但大纲里是空的）",
        )
    return issues, None


def _lint_office(kind: str, path: Path) -> tuple[list[dict[str, Any]], str | None]:
    if kind == "docx":
        return _lint_docx(path)
    if kind == "xlsx":
        return _lint_xlsx(path)
    if kind == "pdf":
        # PDF 是最终产物，没有可改的"源码"，排版检查无从谈起
        return [], "PDF 没有可校验的源结构（它是成品），要核对内容请用 office_read(pages=...)。"
    return _lint_pptx(path)


class _OfficeToolBase(BaseTool):
    def __init__(self, workspace: str = ""):
        self.workspace = workspace

    def _resolve(self, raw: str) -> tuple[Path | None, str | None]:
        if not raw:
            return None, "缺少 path 参数"
        path = Path(raw)
        if not path.is_absolute() and self.workspace:
            path = Path(self.workspace) / raw
        if not path.exists():
            return None, f"文件不存在: {raw}（解析为 {path}）"
        if not path.is_file():
            return None, f"不是文件: {raw}"
        return path, None

    @property
    def tags(self) -> tuple[str, ...]:
        return ("office", "docx", "xlsx", "pptx", "pdf", "word", "excel", "ppt",
                "文档", "表格", "幻灯片", "工作簿", "PDF")


class OfficeInfoTool(_OfficeToolBase):
    """结构摘要：先看骨架，再决定读哪一段。"""

    @property
    def name(self) -> str:
        return "office_info"

    @property
    def description(self) -> str:
        return (
            "读取 Office 文件的结构摘要（docx 段落/表格数、xlsx 工作表与行列数、"
            "pptx 页数与每页标题）。几百字，用来代替 read_file——read_file 读这类"
            "文件只会返回二进制乱码。看完摘要再用 office_read 精确取内容。"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径（相对工作目录或绝对路径）"},
            },
            "required": ["path"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        path, err = self._resolve(kwargs.get("path") or kwargs.get("file_path") or "")
        if err:
            return ToolResult(output=f"❌ {err}", is_error=True)
        kind, err = _kind_of(path)
        if err:
            return ToolResult(output=f"❌ {err}", is_error=True)
        missing = _dep_missing(kind)
        if missing:
            return ToolResult(output=f"❌ {missing}", is_error=True)

        try:
            if kind == "docx":
                body = self._info_docx(path)
            elif kind == "xlsx":
                body = self._info_xlsx(path)
            elif kind == "pptx":
                body = self._info_pptx(path)
            else:
                body = self._info_pdf(path)
        except Exception as exc:  # noqa: BLE001 - 损坏文件要给出人话，不是 traceback
            return ToolResult(
                output=(
                    f"❌ 无法解析 {path.name}（{type(exc).__name__}: {exc}）。\n"
                    "文件可能已损坏或不是真正的 Office 文档；"
                    "可以先用 office_check 确认结构。"
                ),
                is_error=True,
            )
        return ToolResult(output=body)

    def _info_docx(self, path: Path) -> str:
        import docx

        doc = docx.Document(str(path))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        chars = sum(_plain_len(t) for t in paragraphs)
        tables = doc.tables
        lines = [
            f"[docx] {path.name}（{_fmt_bytes(path.stat().st_size)}）",
            f"段落: {len(doc.paragraphs)} 段（非空 {len(paragraphs)}）| 正文约 {chars} 字",
            f"表格: {len(tables)} 个",
        ]
        for idx, table in enumerate(tables[:6]):
            rows = len(table.rows)
            cols = len(table.columns)
            lines.append(f"  #{idx} {rows} 行 × {cols} 列")
        if len(tables) > 6:
            lines.append(f"  …另有 {len(tables) - 6} 个表格")
        try:
            lines.append(f"分节: {len(doc.sections)}")
        except Exception:
            pass
        chrome = _docx_chrome(doc)
        if chrome:
            lines.append(f"页眉/页脚: {len(chrome)} 段有内容 — " + "；".join(c[:60] for c in chrome[:3]))
        lines.append(
            f'提示: office_read(paragraphs="1-60") 读正文；'
            f'表格用 office_read(table=0)（表格文字不在段落里）'
        )
        return "\n".join(lines)

    def _info_xlsx(self, path: Path) -> str:
        import openpyxl

        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=False)
        lines = [f"[xlsx] {path.name}（{_fmt_bytes(path.stat().st_size)}）",
                 f"工作表: {len(wb.worksheets)} 个"]
        risky_names: set[str] = set()
        try:
            for ws in wb.worksheets[:8]:
                rows = ws.max_row or 0
                cols = ws.max_column or 0
                formulas = 0
                scan_rows = min(rows, 300)
                scan_cols = min(cols, 50)
                for row in ws.iter_rows(min_row=1, max_row=scan_rows, max_col=scan_cols):
                    for cell in row:
                        value = cell.value
                        if isinstance(value, str) and value.startswith("="):
                            formulas += 1
                            risky_names.update(_risky_formula_hits(value))
                suffix = f"，公式 {formulas} 个" if formulas else ""
                if rows > scan_rows or cols > scan_cols:
                    suffix += f"（公式数仅统计前 {scan_rows}×{scan_cols}）"
                lines.append(f"  {ws.title}  {rows} 行 × {cols} 列{suffix}")
            if len(wb.worksheets) > 8:
                lines.append(f"  …另有 {len(wb.worksheets) - 8} 个表")
        finally:
            wb.close()
        if risky_names:
            lines.append(
                "⚠️ 公式兼容性: " + "、".join(sorted(risky_names))
                + " —— 这些函数在 LibreOffice / 旧版 Excel 上算不出来，"
                "对方打开会是 #NAME?；换 INDEX+MATCH 之类的基础函数"
            )
        lines.append(
            '提示: office_read(sheet="表名", range="A1:H20") 取区域；'
            "公式默认返回原文，加 values=true 读缓存结果"
            "（缓存为空说明文件从未计算过，先跑 office_recalc）"
        )
        return "\n".join(lines)

    def _info_pptx(self, path: Path) -> str:
        from pptx import Presentation

        prs = Presentation(str(path))
        slides = list(prs.slides)
        lines = [f"[pptx] {path.name}（{_fmt_bytes(path.stat().st_size)}）",
                 f"幻灯片: {len(slides)} 页"]
        for idx, slide in enumerate(slides[:15], start=1):
            title = ""
            try:
                if slide.shapes.title is not None:
                    title = (slide.shapes.title.text or "").strip().replace("\n", " ")
            except Exception:
                title = ""
            lines.append(f"  {idx}. {title[:60] or '(无标题)'}")
        if len(slides) > 15:
            lines.append(f"  …另有 {len(slides) - 15} 页")
        lines.append('提示: office_read(slides="1-5") 读指定页的文本框内容')
        return "\n".join(lines)

    def _info_pdf(self, path: Path) -> str:
        import fitz

        document = fitz.open(str(path))
        try:
            total = document.page_count
            chars = []
            for index in range(total):
                text = (document[index].get_text("text") or "").strip()
                chars.append(_plain_len(text))
            empty = sum(1 for c in chars if c == 0)

            lines = [
                f"[pdf] {path.name}（{_fmt_bytes(path.stat().st_size)}）",
                f"页数: {total}，文字总量: {sum(chars)} 字",
            ]
            for index in range(min(15, total)):
                lines.append(f"  {index + 1}. {chars[index]} 字")
            if total > 15:
                lines.append(f"  …另有 {total - 15} 页")
            if total and empty == total:
                lines.append(
                    "⚠️ 整份 PDF 没有文字层（扫描件或图片导出）。office_read 读不到字，"
                    "改用 office_render 渲染成 PNG 再交给 vision。"
                )
            elif empty:
                lines.append(f"⚠️ 其中 {empty} 页没有文字层（插图页或扫描页）。")
            lines.append('提示: office_read(pages="1-10") 读指定页的正文')
            return "\n".join(lines)
        finally:
            document.close()


class OfficeReadTool(_OfficeToolBase):
    """按范围取内容：只把需要的那一块放进上下文。"""

    @property
    def name(self) -> str:
        return "office_read"

    @property
    def description(self) -> str:
        return (
            "按范围读取文档内容。docx 用 paragraphs=\"起始-结束\" 或 table=索引；"
            "xlsx 用 sheet + range（如 range=\"A1:H20\"），values=true 读公式的缓存结果；"
            "pptx 用 slides=\"1-5\"；pdf 用 pages=\"1-10\"。"
            "输出有字符上限，超出会提示缩小范围。读 docx/xlsx/pptx/pdf 必须用本工具，"
            "不要用 read_file（会得到二进制乱码）。"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "paragraphs": {
                    "type": "string",
                    "description": "docx：段落范围，如 \"1-60\" 或 \"all\"（默认 1-60）",
                },
                "table": {
                    "type": "integer",
                    "description": "docx：只读第 N 个表格（0 开始），返回 markdown",
                },
                "sheet": {"type": "string", "description": "xlsx：工作表名，默认第一个"},
                "range": {
                    "type": "string",
                    "description": "xlsx：单元格区域，如 \"A1:H20\"（默认 A1:J30）",
                },
                "values": {
                    "type": "boolean",
                    "description": "xlsx：读公式的计算结果而不是公式原文（默认 false；"
                    "文件没被计算过时缓存为空，需先 office_recalc）",
                },
                "slides": {
                    "type": "string",
                    "description": "pptx：页码范围，如 \"1-5\"（默认 1-10）",
                },
                "pages": {
                    "type": "string",
                    "description": "pdf：页码范围，如 \"1-10\" 或 \"all\"（默认 1-10）",
                },
                "max_chars": {
                    "type": "integer",
                    "description": f"输出字符上限，默认 {_DEFAULT_READ_CHARS}",
                },
            },
            "required": ["path"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        path, err = self._resolve(kwargs.get("path") or kwargs.get("file_path") or "")
        if err:
            return ToolResult(output=f"❌ {err}", is_error=True)
        kind, err = _kind_of(path)
        if err:
            return ToolResult(output=f"❌ {err}", is_error=True)
        missing = _dep_missing(kind)
        if missing:
            return ToolResult(output=f"❌ {missing}", is_error=True)

        limit = kwargs.get("max_chars") or _DEFAULT_READ_CHARS
        try:
            limit = max(500, min(int(limit), _MAX_READ_CHARS))
        except (TypeError, ValueError):
            limit = _DEFAULT_READ_CHARS

        hint = "请缩小范围（更小的 range / 更少的页 / 更短的段落区间）后重试。"
        try:
            if kind == "docx":
                body = self._read_docx(path, kwargs)
            elif kind == "xlsx":
                body = self._read_xlsx(path, kwargs)
            elif kind == "pptx":
                body = self._read_pptx(path, kwargs)
            else:
                body = self._read_pdf(path, kwargs)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                output=f"❌ 读取失败（{type(exc).__name__}: {exc}）。先用 office_info 看结构。",
                is_error=True,
            )
        # 参数类错误（表名不存在、表格索引越界）以 ❌ 前缀返回，同样算失败：
        # 否则模型会拿到一句"正常输出"，然后基于空内容继续推理。
        if body.startswith("❌"):
            return ToolResult(output=body, is_error=True)
        return ToolResult(output=_truncate(body, limit, hint))

    def _read_docx(self, path: Path, kwargs: dict[str, Any]) -> str:
        import docx

        doc = docx.Document(str(path))
        table_index = kwargs.get("table")
        if table_index is not None:
            index = int(table_index)
            if index < 0 or index >= len(doc.tables):
                return f"❌ 表格索引 {index} 越界（共 {len(doc.tables)} 个表）"
            return self._table_markdown(doc.tables[index], index)

        paragraphs = doc.paragraphs
        start, end = _parse_span(kwargs.get("paragraphs"), len(paragraphs), 60)
        selected = paragraphs[start - 1:end]
        lines = [f"[docx] {path.name} 段落 {start}-{min(end, len(paragraphs))} / {len(paragraphs)}"]
        # 页眉页脚不在 paragraphs 里，但合同/报告的抬头就在那儿，别漏
        lines.extend(_docx_chrome(doc))
        empty_run = 0
        for offset, para in enumerate(selected):
            number = start + offset
            text = para.text.strip()
            if not text:
                empty_run += 1
                continue
            empty_run = 0
            lines.append(f"{number:>4}: {text}")
        if empty_run:
            lines.append(f"（末尾 {empty_run} 个空段落已省略）")
        if doc.tables:
            lines.append(
                f"\n注意：还有 {len(doc.tables)} 个表格，文字不在上面的段落里——"
                f"用 office_read(table=0) 读取"
            )
        return "\n".join(lines)

    @staticmethod
    def _table_markdown(table: Any, index: int) -> str:
        rows = table.rows
        lines = [f"[表格 #{index}] {len(rows)} 行 × {len(table.columns)} 列"]
        for r, row in enumerate(rows[:40]):
            cells = [re.sub(r"\s+", " ", cell.text).strip() for cell in row.cells]
            lines.append("| " + " | ".join(cells) + " |")
            if r == 0:
                lines.append("|" + "---|" * len(cells))
        if len(rows) > 40:
            lines.append(f"…（还有 {len(rows) - 40} 行，表格不支持分段，如需全部请说明）")
        return "\n".join(lines)

    def _read_xlsx(self, path: Path, kwargs: dict[str, Any]) -> str:
        import openpyxl

        # 同时打开"公式视图"和"数值视图"：只看公式时无法回答"到底算出来没有"，
        # 而"未计算"和"算出来是空"对模型是完全不同的结论。
        show_values = bool(kwargs.get("values"))
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=False)
        wb_values = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        try:
            sheet_name = kwargs.get("sheet")
            if sheet_name:
                if sheet_name not in wb.sheetnames:
                    return (
                        f"❌ 没有工作表 {sheet_name!r}。"
                        f"现有: {', '.join(wb.sheetnames)}"
                    )
                ws = wb[sheet_name]
                ws_values = wb_values[sheet_name]
            else:
                ws = wb.worksheets[0]
                ws_values = wb_values.worksheets[0]

            cell_range = (kwargs.get("range") or "A1:J30").strip().upper()
            try:
                cells = ws[cell_range]
            except Exception:
                return f"❌ 区域 {cell_range!r} 无效（示例: \"A1:H20\"）"
            try:
                cells_values = ws_values[cell_range]
            except Exception:
                cells_values = None

            if not isinstance(cells, tuple):
                cells = ((cells,),)
            elif cells and not isinstance(cells[0], tuple):
                cells = (cells,)
            if cells_values is not None:
                if not isinstance(cells_values, tuple):
                    cells_values = ((cells_values,),)
                elif cells_values and not isinstance(cells_values[0], tuple):
                    cells_values = (cells_values,)

            header = f"[xlsx] {path.name} · {ws.title} · {cell_range}"
            if show_values:
                header += " · 数值视图"
            lines = [header]
            formula_count = 0
            missing_cache = 0
            risky: list[str] = []
            for row_index, row in enumerate(cells[:60]):
                values = []
                for column_index, cell in enumerate(row):
                    value = getattr(cell, "value", None)
                    cached = None
                    if cells_values is not None:
                        try:
                            cached = cells_values[row_index][column_index].value
                        except (IndexError, TypeError):
                            cached = None
                    if isinstance(value, str) and value.startswith("="):
                        formula_count += 1
                        for name in _risky_formula_hits(value):
                            risky.append(
                                f"  {getattr(cell, 'coordinate', '?')}: {name} — {_RISKY_FUNCS[name]}"
                            )
                        if show_values:
                            if cached is None:
                                missing_cache += 1
                                values.append("(无缓存值)")
                            else:
                                values.append(str(cached))
                            continue
                    values.append("" if value is None else str(value))
                lines.append(" | ".join(values).rstrip(" |"))
            if len(cells) > 60:
                lines.append(f"…（区域还有 {len(cells) - 60} 行，已省略）")
            if missing_cache:
                lines.append(
                    f"\n⚠️ {missing_cache} 个公式没有缓存结果（文件从未被 Excel/LibreOffice "
                    "计算过）。先跑 office_recalc，再用 values=true 读。"
                )
            elif formula_count and not show_values:
                lines.append(
                    f"\n（其中 {formula_count} 个单元格是公式原文——要计算结果请传 values=true；"
                    "如果传了 values 还是空，说明需要先 office_recalc 重算）"
                )
            if risky:
                lines.append("\n⚠️ 公式兼容性（这些函数在 LibreOffice / 旧版 Excel 上算不出来）：")
                lines.extend(risky[:8])
                if len(risky) > 8:
                    lines.append(f"  …另有 {len(risky) - 8} 处")
            return "\n".join(lines)
        finally:
            wb.close()
            wb_values.close()

    def _read_pptx(self, path: Path, kwargs: dict[str, Any]) -> str:
        from pptx import Presentation

        prs = Presentation(str(path))
        slides = list(prs.slides)
        start, end = _parse_span(kwargs.get("slides"), len(slides), 10)
        lines = [f"[pptx] {path.name} 第 {start}-{min(end, len(slides))} 页 / 共 {len(slides)} 页"]
        for idx in range(start - 1, min(end, len(slides))):
            slide = slides[idx]
            lines.append(f"\n── 第 {idx + 1} 页 ──")
            texts, extras = _pptx_slide_content(slide)
            if not texts and not extras:
                lines.append("（本页没有可读内容）")
            for text in texts[:12]:
                lines.append(text)
            if extras:
                lines.append("（本页还有：" + "、".join(sorted(set(extras))) + "，无法转文本）")
            try:
                if slide.has_notes_slide:
                    note = (slide.notes_slide.notes_text_frame.text or "").strip()
                    if note:
                        lines.append(f"[备注] {note[:300]}")
            except Exception:
                pass
        return "\n".join(lines)

    def _read_pdf(self, path: Path, kwargs: dict[str, Any]) -> str:
        """抽 PDF 的文字层。扫描件没有文字层，只能回头让 vision 看图。"""
        import fitz

        document = fitz.open(str(path))
        try:
            total = document.page_count
            start, end = _parse_span(kwargs.get("pages"), total, 10)
            if start > total:
                # 不拦的话会输出"第 9-3 页"这种鬼话，然后正文一片空白
                return f"❌ 页码超出范围：这份 PDF 只有 {total} 页，请求的是第 {start} 页起"
            end = min(end, total)
            lines = [f"[pdf] {path.name} 第 {start}-{end} 页 / 共 {total} 页"]
            blank = 0
            for index in range(start - 1, end):
                text = (document[index].get_text("text") or "").strip()
                lines.append(f"--- 第 {index + 1} 页 ---")
                if not text:
                    blank += 1
                    lines.append("（无文字层）")
                    continue
                lines.append(text)
            if blank and blank == end - start + 1:
                lines.append(
                    "\n⚠️ 这几页都没有文字层，多半是扫描件/图片版 PDF。"
                    "内容读不到文字，请先用 office_render 渲染成 PNG，再用 vision 识别。"
                )
            elif blank:
                lines.append(f"\n（其中 {blank} 页没有文字层，若要读图请用 office_render + vision）")
            return "\n".join(lines)
        finally:
            document.close()


class OfficeCheckTool(_OfficeToolBase):
    """结构校验 + 排版 lint：文件能打开，不等于能直接交出去。"""

    @property
    def name(self) -> str:
        return "office_check"

    @property
    def description(self) -> str:
        return (
            "校验 Office 文件：结构（zip 是否完整、必需部件是否齐全、XML 是否良构）"
            "＋排版 lint（表格超出版心、深色底纹配深色字、手打编号、中文缺 eastAsia 字体、"
            "xlsx 公式错误值/未计算、pptx 文本框出界/字号过小）。"
            "用脚本生成 docx/xlsx/pptx 之后必须跑一次——write_file 写出的看起来像"
            "Office 的文件其实是坏的（Word 打开会报错）。返回紧凑 JSON："
            "ok=结构是否完好，lint_ok=排版是否可交付。lint=false 只看结构。"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要校验的文件路径"},
                "lint": {
                    "type": "boolean",
                    "description": "是否做排版检查（默认 true）。大表或只要结构结论时设 false",
                },
            },
            "required": ["path"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        path, err = self._resolve(kwargs.get("path") or kwargs.get("file_path") or "")
        if err:
            return ToolResult(output=f"❌ {err}", is_error=True)
        kind, err = _kind_of(path)
        if err:
            return ToolResult(output=f"❌ {err}", is_error=True)

        issues: list[str] = []
        result: dict[str, Any] = {
            "path": str(path),
            "kind": kind,
            "size": path.stat().st_size,
        }

        if kind == "pdf":
            # PDF 不是 zip 容器。不提前拦住的话，下面会报"不是有效的 Office 文件"
            # 并暗示是 write_file 写坏了——完全误导。
            result.update({
                "ok": True,
                "note": "PDF 是成品格式，没有可校验的源结构。"
                        "读内容用 office_read(pages=...)，看版面用 office_render。",
            })
            return ToolResult(output=json.dumps(result, ensure_ascii=False))

        if not zipfile.is_zipfile(path):
            result.update({"ok": False, "issues": ["不是 zip 容器：这不是一个有效的 Office 文件（常见原因：用 write_file 写了文本）"]})
            return ToolResult(output=json.dumps(result, ensure_ascii=False), is_error=True)

        try:
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
                bad = archive.testzip()
                if bad:
                    issues.append(f"zip 内部条目损坏: {bad}")
                result["parts"] = len(names)

                missing = [p for p in _REQUIRED_PARTS[kind] if p not in names]
                if missing:
                    issues.append(f"缺少必需部件: {', '.join(missing)}")
                result["required_ok"] = not missing

                broken_xml = []
                for name in names[:200]:
                    if not name.endswith(".xml"):
                        continue
                    if len(broken_xml) >= _MAX_XML_PARTS:
                        break
                    info = archive.getinfo(name)
                    if info.file_size > _MAX_XML_BYTES:
                        continue
                    try:
                        ElementTree.fromstring(archive.read(name))
                    except ElementTree.ParseError as exc:
                        broken_xml.append(f"{name}: {exc}")
                if broken_xml:
                    issues.extend(f"XML 不良构 — {item}" for item in broken_xml[:5])
                result["xml_checked"] = min(_MAX_XML_PARTS, sum(1 for n in names if n.endswith(".xml")))
        except Exception as exc:  # noqa: BLE001
            issues.append(f"读取失败: {type(exc).__name__}: {exc}")

        result["ok"] = not issues
        result["issues"] = issues
        if issues:
            result["fix_hint"] = (
                "文件结构有问题。如果是脚本生成的，检查保存逻辑；"
                "如果是 write_file 写的，请改用 python-docx / openpyxl / python-pptx 重新生成。"
            )

        # 排版 lint 与结构校验分开报告：ok=false 是"打不开"，lint_ok=false 是
        # "能打开但交不出去"。两者混成一个 is_error 会让模型以为文件坏了，
        # 于是去重建一个本来只是列宽不对的文件。
        lint_enabled = kwargs.get("lint", True)
        if lint_enabled is False:
            result["lint_skipped"] = "lint=false"
        elif result["ok"]:
            missing = _dep_missing(kind)
            if missing:
                result["lint_skipped"] = missing.splitlines()[0]
            else:
                try:
                    lint_issues, note = _lint_office(kind, path)
                except Exception as exc:  # noqa: BLE001 - lint 是附加信息，不能拖垮结构结论
                    lint_issues, note = [], f"排版检查异常（{type(exc).__name__}: {exc}）"
                if note:
                    result["lint_skipped"] = note
                if lint_issues:
                    result["lint_ok"] = False
                    result["lint_summary"] = _lint_summary(lint_issues)
                    result["lint"] = lint_issues
                    result["lint_fix_hint"] = (
                        "这些不是文件损坏，是排版/内容问题：对方能打开，但会看到裁掉的表格、"
                        "看不见的文字或 #REF!。按每条 fix 改完再跑一次 office_check。"
                    )
                else:
                    result["lint_ok"] = True

        return ToolResult(
            output=json.dumps(result, ensure_ascii=False),
            is_error=bool(issues),
        )


# ── LibreOffice（可选）──────────────────────────────────────────────────────
# openpyxl 不计算公式，python-pptx 不排版：脚本生成的 xlsx 缓存值是空的，
# 生成的 docx/pptx 到底长什么样只能打开看。这两件事都必须借 LibreOffice，
# 所以没装时给出**一条能直接执行的安装命令**，而不是让模型反复猜参数。

_LIBREOFFICE_HINT = (
    "需要 LibreOffice（soffice）才能做公式重算和 PDF 渲染，当前没有找到。\n"
    "安装方式：\n"
    "  Windows: winget install --id TheDocumentFoundation.LibreOffice -e\n"
    "           （也可以直接运行项目里的 install.bat，它会检测并引导安装）\n"
    "  macOS:   brew install --cask libreoffice\n"
    "  Linux:   sudo apt install libreoffice\n"
    "已经装在非标准位置时，把环境变量 LIBREOFFICE_PATH 指向 soffice 可执行文件。"
)

#: 强制 LibreOffice 在打开 OOXML 表格时重算公式（0=总是重算，1=从不，2=询问）。
#: 默认值是"从不"，所以不写这个配置的话，转换出来的文件仍然是空缓存值。
_RECALC_XCU = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry" \
xmlns:xs="http://www.w3.org/2001/XMLSchema" \
xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <item oor:path="/org.openoffice.Office.Calc/Formula/Load">
    <prop oor:name="OOXMLRecalcMode" oor:op="fuse"><value>0</value></prop>
  </item>
  <item oor:path="/org.openoffice.Office.Calc/Formula/Load">
    <prop oor:name="ODFRecalcMode" oor:op="fuse"><value>0</value></prop>
  </item>
</oor:items>
"""

#: 格式族 → LibreOffice 的 PDF 导出过滤器。写死过滤器比只给 "pdf" 稳：
#: 后者要靠 LO 自己按文档类型猜，在精简安装里偶尔猜不出来。
#: 注意不要在过滤器名外面再加引号——subprocess 传的是已经分好的 argv，
#: 引号会变成过滤器名的一部分，直接报 "Error Area:Io Class:Parameter"。
_PDF_FILTER = {
    "docx": "pdf:writer_pdf_Export",
    "xlsx": "pdf:calc_pdf_Export",
    "pptx": "pdf:impress_pdf_Export",
}

_XLSX_FILTER = "xlsx:Calc MS Excel 2007 XML"
_DEFAULT_LO_TIMEOUT = 180
_MAX_RENDER_PAGES = 20


def _soffice_path() -> str | None:
    """找 soffice：环境变量 → PATH → 各平台默认安装位置。"""
    for env_name in ("NOVELS_SOFFICE", "SOFFICE_PATH", "LIBREOFFICE_PATH"):
        raw = os.environ.get(env_name)
        if not raw:
            continue
        candidate = Path(raw)
        if candidate.is_file():
            return str(candidate)
        if candidate.is_dir():
            for relative in ("soffice.exe", "soffice", "program/soffice.exe", "program/soffice"):
                if (candidate / relative).is_file():
                    return str(candidate / relative)

    for executable in ("soffice", "soffice.exe", "libreoffice"):
        found = shutil.which(executable)
        if found:
            return found

    candidates: list[Path] = []
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("ProgramW6432")):
        if base:
            candidates.append(Path(base) / "LibreOffice" / "program" / "soffice.exe")
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates.append(Path(local_appdata) / "Programs" / "LibreOffice" / "program" / "soffice.exe")
    candidates.extend([
        Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"),
        Path("/usr/bin/soffice"),
        Path("/usr/local/bin/soffice"),
        Path("/snap/bin/libreoffice"),
    ])
    opt = Path("/opt")
    if opt.is_dir():
        candidates.extend(sorted(opt.glob("libreoffice*/program/soffice")))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _write_lo_profile(root: Path, recalc: bool) -> Path:
    """每次转换用独立的 UserInstallation：不复用用户配置，也不和已开的窗口抢锁。"""
    profile = root / "lo-profile"
    user_dir = profile / "user"
    user_dir.mkdir(parents=True, exist_ok=True)
    if recalc:
        (user_dir / "registrymodifications.xcu").write_text(_RECALC_XCU, encoding="utf-8")
    return profile


def _soffice_convert(
    src: Path,
    target_filter: str,
    out_dir: Path,
    *,
    recalc: bool = False,
    timeout: int = _DEFAULT_LO_TIMEOUT,
) -> tuple[Path | None, str]:
    """跑一次 ``soffice --convert-to``，返回 (输出文件, 错误说明)。"""
    soffice = _soffice_path()
    if not soffice:
        return None, _LIBREOFFICE_HINT
    if not src.is_file():
        return None, f"源文件不存在: {src}"
    out_dir.mkdir(parents=True, exist_ok=True)
    extension = target_filter.split(":", 1)[0].strip()

    with tempfile.TemporaryDirectory(prefix="gangge-lo-") as tmp:
        profile = _write_lo_profile(Path(tmp), recalc)
        command = [
            soffice,
            "--headless",
            "--norestore",
            "--nologo",
            "--nodefault",
            "--nolockcheck",
            "--nofirststartwizard",
            f"-env:UserInstallation={profile.resolve().as_uri()}",
            "--convert-to",
            target_filter,
            "--outdir",
            str(out_dir),
            str(src),
        ]
        # 不弹黑框：Windows 上每次转换都会闪一个控制台窗口，很吓人。
        creation_flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=creation_flags,
            )
        except subprocess.TimeoutExpired:
            return None, (
                f"LibreOffice 转换超过 {timeout} 秒仍未结束（文件太大，或后台残留进程）。"
                "先完全退出 LibreOffice 再重试，或调大 timeout。"
            )
        except OSError as exc:
            return None, f"无法启动 LibreOffice（{soffice}）：{exc}"

        output = out_dir / f"{src.stem}.{extension}"
        if not output.is_file():
            produced = list(out_dir.glob(f"*.{extension}"))
            output = produced[0] if produced else None
        if output is None:
            detail = ((proc.stdout or "") + (proc.stderr or "")).strip()
            return None, (
                "LibreOffice 没有生成输出文件。"
                + (f"\n它的输出：{detail[-400:]}" if detail else "")
                + "\n如果 LibreOffice 有窗口开着，先完全退出再重试。"
            )
        return output, ""


def _formula_state(path: Path) -> tuple[int, int, list[str]]:
    """(公式数, 有缓存值的公式数, 错误值样例)。只扫前 12 个表的可见区域。"""
    import openpyxl

    formulas = cached = 0
    errors: list[str] = []
    wb = openpyxl.load_workbook(str(path), data_only=False)
    try:
        wb_values = openpyxl.load_workbook(str(path), data_only=True)
    except Exception:
        wb.close()
        raise
    try:
        for ws, ws_values in zip(wb.worksheets[:12], wb_values.worksheets[:12]):
            rows = min(ws.max_row or 0, 2000)
            cols = min(ws.max_column or 0, 60)
            for row in ws.iter_rows(min_row=1, max_row=rows, max_col=cols):
                for cell in row:
                    if not (isinstance(cell.value, str) and cell.value.startswith("=")):
                        continue
                    formulas += 1
                    value = ws_values[cell.coordinate].value
                    if value is None:
                        continue
                    cached += 1
                    if isinstance(value, str) and value.strip().upper().startswith("#"):
                        if len(errors) < 8:
                            errors.append(f"{ws.title}!{cell.coordinate} = {value}")
    finally:
        wb.close()
        wb_values.close()
    return formulas, cached, errors


class OfficeRecalcTool(_OfficeToolBase):
    """用 LibreOffice 重算公式并另存：让"写进去的公式"变成"看得到的数值"。"""

    @property
    def name(self) -> str:
        return "office_recalc"

    @property
    def description(self) -> str:
        return (
            "用 LibreOffice 重新计算公式并另存一份 xlsx（需要装 LibreOffice）。"
            "openpyxl 从来不计算公式：脚本生成的表虽然公式是对的，但缓存值全空，"
            "对方打开前预览、转 PDF 都是空白或 0。写完表之后跑一次即可。"
            "返回公式数/有值数/错误值（#REF!、#NAME? 等）。"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要重算的 .xlsx / .xlsm"},
                "output": {
                    "type": "string",
                    "description": "输出路径，默认同目录 <名字>.recalc.xlsx",
                },
                "overwrite": {
                    "type": "boolean",
                    "description": "覆盖源文件（默认 false，另存副本；.xlsm 不能覆盖）",
                },
                "timeout": {
                    "type": "integer",
                    "description": f"LibreOffice 超时秒数，默认 {_DEFAULT_LO_TIMEOUT}",
                },
            },
            "required": ["path"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        path, err = self._resolve(kwargs.get("path") or kwargs.get("file_path") or "")
        if err:
            return ToolResult(output=f"❌ {err}", is_error=True)
        kind, err = _kind_of(path)
        if err:
            return ToolResult(output=f"❌ {err}", is_error=True)
        if kind != "xlsx":
            return ToolResult(
                output=(
                    "❌ office_recalc 只处理 .xlsx / .xlsm。"
                    "docx / pptx 想确认排版请用 office_render 转 PDF 看。"
                ),
                is_error=True,
            )
        if not _soffice_path():
            return ToolResult(output=f"❌ {_LIBREOFFICE_HINT}", is_error=True)

        overwrite = bool(kwargs.get("overwrite"))
        if overwrite:
            if path.suffix.lower() == ".xlsm":
                return ToolResult(
                    output=(
                        "❌ .xlsm 不能覆盖：LibreOffice 另存时会丢掉 VBA 宏。"
                        "请改用 output 参数指定一个新的 .xlsx。"
                    ),
                    is_error=True,
                )
            target = path
        else:
            raw_output = kwargs.get("output")
            if raw_output:
                target = Path(str(raw_output))
                if not target.is_absolute() and self.workspace:
                    target = Path(self.workspace) / target
                if target.exists():
                    return ToolResult(
                        output=f"❌ {target} 已存在。换个 output，或加 overwrite=true 覆盖。",
                        is_error=True,
                    )
            else:
                suffix = ".xlsx" if path.suffix.lower() == ".xlsm" else path.suffix
                target = path.with_name(f"{path.stem}.recalc{suffix}")

        try:
            timeout = max(30, min(int(kwargs.get("timeout") or _DEFAULT_LO_TIMEOUT), 600))
        except (TypeError, ValueError):
            timeout = _DEFAULT_LO_TIMEOUT

        with tempfile.TemporaryDirectory(prefix="gangge-recalc-") as tmp:
            converted, err = _soffice_convert(
                path, _XLSX_FILTER, Path(tmp), recalc=True, timeout=timeout
            )
            if converted is None:
                return ToolResult(output=f"❌ 重算失败。\n{err}", is_error=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(converted, target)

        formulas, cached, errors = _formula_state(target)
        lines = [f"✅ 已重算并另存: {target}"]
        if overwrite:
            lines.append(f"源文件已被覆盖: {path.name}")
        else:
            lines.append(f"源文件未改动: {path.name}")
        if path.suffix.lower() == ".xlsm":
            lines.append("⚠️ .xlsm 的 VBA 宏不会保留（另存为 .xlsx）")
        lines.append(f"公式 {formulas} 个 | 有结果 {cached} 个 | 错误值 {len(errors)} 个")
        if formulas and not cached:
            lines.append(
                "⚠️ 重算后仍然没有缓存值：检查文件是否被 LibreOffice 正常打开（可能已被占用）。"
            )
        if errors:
            lines.append("⚠️ 这些公式算出来是错误值（公式本身有问题，不是没算）：")
            lines.extend(f"  {item}" for item in errors)
        lines.append(
            "后续：需要数值请读这份 .recalc 文件（office_read 仍返回公式原文）；"
            "需要看图用 office_render。"
        )
        return ToolResult(output="\n".join(lines))


class OfficeRenderTool(_OfficeToolBase):
    """转 PDF 再渲染 PNG：用眼睛确认排版，而不是靠猜坐标系。"""

    @property
    def name(self) -> str:
        return "office_render"

    @property
    def description(self) -> str:
        return (
            "把 docx / xlsx / pptx 转成 PDF 并渲染为 PNG，也可以直接把现成的 .pdf 渲染成 PNG"
            "（后者不需要 LibreOffice，只要 pymupdf）。"
            "只渲染指定页（默认前 3 页），返回 PDF 路径和每张 PNG 的绝对路径——"
            "接着用 vision 工具看 PNG，就能确认表格有没有溢出、文字有没有被裁、"
            "分页是否合理。这是唯一能真正'看到排版'的方式，不要靠读 XML 猜。"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要渲染的 docx / xlsx / pptx"},
                "pages": {
                    "type": "string",
                    "description": "页码范围，如 \"1-3\" / \"2\" / \"all\"（默认 1-3，单次最多 20 页）",
                },
                "dpi": {
                    "type": "integer",
                    "description": "渲染分辨率，默认 110（范围 48-200；字太小时用 150）",
                },
                "output_dir": {
                    "type": "string",
                    "description": "输出目录，默认同目录 <名字>_preview/",
                },
                "timeout": {
                    "type": "integer",
                    "description": f"LibreOffice 超时秒数，默认 {_DEFAULT_LO_TIMEOUT}",
                },
            },
            "required": ["path"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        path, err = self._resolve(kwargs.get("path") or kwargs.get("file_path") or "")
        if err:
            return ToolResult(output=f"❌ {err}", is_error=True)
        kind, err = _kind_of(path)
        if err:
            return ToolResult(output=f"❌ {err}", is_error=True)
        # PDF 已经是最终格式，PyMuPDF 直接渲染，不需要 LibreOffice
        if kind != "pdf" and not _soffice_path():
            return ToolResult(output=f"❌ {_LIBREOFFICE_HINT}", is_error=True)
        try:
            import fitz  # noqa: F401 - PyMuPDF
        except ImportError:
            return ToolResult(
                output=(
                    "❌ 渲染 PNG 需要 PyMuPDF（模块名 fitz），当前没有安装。\n"
                    f"请执行: {_INSTALL_HINT}\n"
                    "（不装也能转 PDF，但没法在这里看图）"
                ),
                is_error=True,
            )

        raw_dir = kwargs.get("output_dir")
        if raw_dir:
            out_dir = Path(str(raw_dir))
            if not out_dir.is_absolute() and self.workspace:
                out_dir = Path(self.workspace) / out_dir
        else:
            out_dir = path.parent / f"{path.stem}_preview"
        out_dir.mkdir(parents=True, exist_ok=True)

        try:
            dpi = max(48, min(int(kwargs.get("dpi") or 110), 200))
        except (TypeError, ValueError):
            dpi = 110
        try:
            timeout = max(30, min(int(kwargs.get("timeout") or _DEFAULT_LO_TIMEOUT), 600))
        except (TypeError, ValueError):
            timeout = _DEFAULT_LO_TIMEOUT

        if kind == "pdf":
            pdf_path = path          # 本来就是 PDF，跳过转换
        else:
            pdf_path = out_dir / f"{path.stem}.pdf"
            if pdf_path.exists():
                try:
                    pdf_path.unlink()
                except OSError as exc:
                    return ToolResult(
                        output=(
                            f"❌ 无法覆盖旧的预览文件 {pdf_path}（{exc}）。"
                            "它可能正被 PDF 阅读器打开，关闭后重试。"
                        ),
                        is_error=True,
                    )

            converted, err = _soffice_convert(
                path, _PDF_FILTER[kind], out_dir, timeout=timeout
            )
            if converted is None:
                return ToolResult(output=f"❌ 转 PDF 失败。\n{err}", is_error=True)
            if converted.resolve() != pdf_path.resolve():
                shutil.move(str(converted), str(pdf_path))

        import fitz

        document = fitz.open(str(pdf_path))
        try:
            total = document.page_count
            start, end = _parse_span(kwargs.get("pages"), total, 3)
            if end - start + 1 > _MAX_RENDER_PAGES:
                end = start + _MAX_RENDER_PAGES - 1
                truncated = True
            else:
                truncated = False
            rendered: list[str] = []
            for index in range(start - 1, min(end, total)):
                page = document[index]
                pixmap = page.get_pixmap(dpi=dpi)
                png_path = out_dir / f"{path.stem}-p{index + 1}.png"
                pixmap.save(str(png_path))
                rendered.append(f"  {index + 1}. {png_path}（{pixmap.width}×{pixmap.height}）")
        finally:
            document.close()

        lines = [
            f"✅ 已渲染 {len(rendered)} 页（文档共 {total} 页，DPI {dpi}）",
            f"PDF: {pdf_path}",
            "PNG:",
            *rendered,
        ]
        if truncated:
            lines.append(f"（单次最多渲染 {_MAX_RENDER_PAGES} 页，其余请缩小 pages 范围）")
        lines.append(
            "下一步：用 vision 工具看这些 PNG（path 传上面的绝对路径），"
            "或把 PDF 直接交给用户。"
        )
        return ToolResult(output="\n".join(lines))


def register_office_tools(registry: Any, workspace: str = "") -> list[str]:
    """把五个工具注册进 registry，返回成功注册的名字。

    依赖缺失也注册：工具自己要能回一句"装哪个包"，比"工具不存在"更有用
    （否则模型会退回去用 read_file，又一次拿到乱码）。
    """
    names: list[str] = []
    for cls in (OfficeInfoTool, OfficeReadTool, OfficeCheckTool,
                OfficeRecalcTool, OfficeRenderTool):
        try:
            tool = cls(workspace=workspace)
            registry.register(tool)
            names.append(tool.name)
        except Exception as exc:  # noqa: BLE001 - 一个坏工具不能拦住启动
            logger.debug("[office] %s 注册失败: %s", cls.__name__, exc)
    return names
