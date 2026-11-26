"""File operations tools — read, write, edit files."""

from __future__ import annotations

import difflib
import os
import re
from pathlib import Path
from typing import Any

from gangge.layer3_agent.tools.base import BaseTool, ToolResult

# ── 文本读写：保留编码、BOM 和换行风格 ─────────────────────────
#
# edit_file / apply_patch 原来用 read_text(encoding="utf-8", errors="replace")
# 读、write_text(encoding="utf-8") 写。两个后果，都不报错：
#
# 1. GBK 文件里的中文在读取时变成 U+FFFD（""），再以 UTF-8 写回去 —— 文件既坏
#    了，编码也从 GBK 变成了 UTF-8。
# 2. read_text 会把换行统一成 "\n"，write_text 再按 os.linesep 写回。于是 LF
#    文件在 Windows 上被写成 CRLF，整个文件在 git 里显示为全量改动。
#
# 现在按字节读写：能无损解码才允许编辑，写回时还原原编码、BOM 与换行风格。

_UTF8_BOM = b"\xef\xbb\xbf"
_UTF16_BOMS = ((b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"))
_FIRST_NEWLINE_RE = re.compile(r"\r\n|\r|\n")

# ── Office 容器：不是文本，通用读写工具必须让路 ──────────────────
#
# read_text_preserving 会因为 NUL 字节拒绝它们，然后调用方退化成
# errors="replace" 的宽松解码 —— 于是 zip 二进制被当成正文返回（实测一个
# 35KB 的 docx 吐出 4 万字符乱码，is_error 还是 False）。write_file 更糟：
# 把 UTF-8 文本写进 .docx，Word 打开直接报"内容有问题"，但它返回"已写入"。
#
# 所以在这里拦下来，把模型导向 office_info / office_read / office_check。
_OFFICE_CONTAINER_EXTS = frozenset({
    ".docx", ".docm", ".xlsx", ".xlsm", ".pptx", ".pptm",
})
_LEGACY_OFFICE_EXTS = frozenset({".doc", ".xls", ".ppt"})

_OFFICE_READ_HINT = (
    "read_file 不能按文本读 {ext}：这是 zip 容器，读出来是二进制乱码。\n"
    "请改用：\n"
    "  office_info(path)   看结构（段落 / 工作表 / 页数）\n"
    "  office_read(...)    按范围取内容（段落区间 / 单元格区域 / 页码）\n"
    "  office_check(path)  校验结构是否完好\n"
    "当前模式下若没有这些工具，用 tool_search(\"office 文档\") 揭示，"
    "或直接用 bash 跑 python-docx / openpyxl / python-pptx 脚本。"
)

_OFFICE_WRITE_HINT = (
    "write_file 不能写 {ext}：这类文件是 zip 容器，按文本写会得到一个"
    "Word/Excel 打不开的坏文件（返回却是“已写入”）。\n"
    "正确做法：用 bash 跑脚本生成（python-docx / openpyxl / python-pptx），"
    "生成后用 office_check(path) 校验结构。"
)


class TextEncodingError(ValueError):
    """文件无法在无损前提下解码，拒绝编辑。"""


def read_text_preserving(path: Path) -> tuple[str, str, str, bytes]:
    """Read text without losing its encoding, BOM or newline style.

    Returns ``(text, encoding, newline, bom)`` where ``text`` has ``\\n`` line
    endings so callers can match on it normally. Raises :class:`TextEncodingError`
    rather than guessing: a silent wrong guess corrupts the file.
    """
    raw = path.read_bytes()

    bom = b""
    encoding = ""
    body = raw
    if raw.startswith(_UTF8_BOM):
        bom, body, encoding = _UTF8_BOM, raw[len(_UTF8_BOM):], "utf-8"
    else:
        for prefix, enc in _UTF16_BOMS:
            if raw.startswith(prefix):
                bom, body, encoding = prefix, raw[len(prefix):], enc
                break

    if encoding:
        try:
            text = body.decode(encoding)
        except UnicodeDecodeError as exc:
            raise TextEncodingError(f"文件带 {encoding} BOM 但无法解码: {exc}") from exc
    else:
        if b"\x00" in raw:
            raise TextEncodingError("文件含 NUL 字节，看起来是二进制文件，拒绝按文本编辑")
        for candidate in ("utf-8", "gb18030"):
            try:
                text = raw.decode(candidate)
            except UnicodeDecodeError:
                continue
            encoding = candidate
            break
        else:
            raise TextEncodingError(
                "无法可靠解码（既不是 UTF-8 也不是 GB18030），拒绝编辑以免损坏文件"
            )

    match = _FIRST_NEWLINE_RE.search(text)
    newline = match.group(0) if match else "\n"

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")

    # 无损校验：把归一化文本按原编码 / 原换行还原，必须得到一模一样的字节。
    # 这一条把"猜错编码"从"静默损坏"变成"拒绝编辑"。
    if normalized.replace("\n", newline).encode(encoding) != body:
        raise TextEncodingError("解码后无法无损还原原文件，拒绝编辑")

    return normalized, encoding, newline, bom


def write_text_preserving(
    path: Path, text: str, encoding: str, newline: str, bom: bytes
) -> None:
    """Write ``text`` (with ``\\n`` endings) back in the original style."""
    data = text.replace("\n", newline) if newline != "\n" else text
    path.write_bytes(bom + data.encode(encoding))


#: read_file 的显示格式：`123: 代码` 或 `123| 代码`。
_NUMBERED_LINE_RE = re.compile(r"^\s*\d+\s*[:|]\s?")

_NUMBERED_DUMP_HINT = (
    "❌ 你写入的内容每一行都带着行号前缀（形如 `123: `）。\n"
    "那是 read_file 的显示格式，原样写进源码会立刻变成语法错误"
    "（典型报错：Expected ',' found ':'）。\n"
    "请去掉行号前缀重新提交，只保留真正的代码内容。"
)


def looks_like_numbered_dump(text: str) -> bool:
    """内容是不是把 read_file 的输出整段回写了。

    实测踩过：模型把带行号的文件片段当 content 写回，428 行的文件因此报
    ``Expected ',' found ':'``，然后下一轮整文件重写一遍来"修"。检测成本
    几乎为零，代价却是一整轮输出。

    阈值取"至少 4 行且过半带前缀"，避免把 `1: 说明` 这类正文误判成污染。
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 4:
        return False
    hits = sum(1 for ln in lines if _NUMBERED_LINE_RE.match(ln))
    return hits >= 4 and hits >= len(lines) * 0.5


_SYMBOL_LINE_RE = re.compile(
    r"^\s*(?:export\s+|pub(?:\([^)]*\))?\s+|async\s+|static\s+|public\s+|"
    r"private\s+|protected\s+|final\s+|abstract\s+)*"
    r"(?:def|fn|class|struct|interface|enum|trait|impl|function|type)\s+"
    r"([A-Za-z_$][\w$]*)"
)


def _outline_of(text: str, limit: int = 40) -> str:
    """给一个文件的“目录”：定义了哪些函数/类，带行号。

    重复读取时不再只回“已读过”——历史滑窗可能已经把正文裁掉了，模型手里
    其实什么都没有，只回一句提示会让它反复重读。给目录足够它决定改哪里。
    """
    lines = text.splitlines()
    picked = [
        f"{i:>6}: {ln.strip()[:100]}"
        for i, ln in enumerate(lines, 1)
        if _SYMBOL_LINE_RE.match(ln)
    ]
    if not picked:
        # 没有可识别的定义（JSON/纯文本）：退回开头几行，别给空目录。
        picked = [f"{i:>6}: {ln[:100]}" for i, ln in enumerate(lines[:12], 1)]
    if len(picked) > limit:
        picked = picked[:limit] + [f"…（其余 {len(picked) - limit} 个定义省略）"]
    return "\n".join(picked)


class ReadFileTool(BaseTool):
    """Read file contents."""

    def __init__(self, workspace: str = ""):
        self.workspace = workspace
        # 同一任务里对同一文件、同一范围的重复读取先回目录；再重复就重新给
        # 正文——历史滑窗裁掉后，模型手里其实没有内容，一味省略只会逼它死读。
        # key -> (mtime_ns, size, total_lines, show_lines, duplicate_hits)
        self._read_cache: dict[tuple[str, int, int], tuple[int, int, int, int, int]] = {}

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return (
            "读取文件内容。支持指定偏移量和行数限制来读取大文件的部分内容。\n"
            "自动检测图片格式并返回基本信息。path 不能为空。\n"
            "示例: read_file(path=\"src/main.py\") 或 read_file(path=\"big.log\", offset=1, limit=50)"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径（绝对路径或相对于项目根目录）",
                },
                "offset": {
                    "type": "integer",
                    "description": "从第几行开始读取（1-based），默认 1",
                    "default": 1,
                },
                "limit": {
                    "type": "integer",
                    "description": "最多读取多少行，默认读取全部",
                },
            },
            "required": ["path"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        path = kwargs.get("path") or kwargs.get("file_path") or kwargs.get("filename")
        offset = kwargs.get("offset", 1)
        limit = kwargs.get("limit", None)

        if not path:
            return ToolResult(
                output=f"❌ read_file 缺少文件路径参数。收到的参数: {list(kwargs.keys())}。请使用 path=\"文件路径\"。",
                is_error=True,
            )

        try:
            file_path = Path(path)
            if not file_path.is_absolute() and self.workspace:
                file_path = Path(self.workspace) / path
            if not file_path.exists():
                return ToolResult(
                    output=f"文件不存在: {path}\n(已解析为: {file_path}，工作目录: {self.workspace})",
                    is_error=True,
                )

            if not file_path.is_file():
                return ToolResult(output=f"不是文件: {path}", is_error=True)

            # Check image files
            ext = file_path.suffix.lower()
            image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
            if ext in _OFFICE_CONTAINER_EXTS:
                return ToolResult(output=f"❌ {_OFFICE_READ_HINT.format(ext=ext)}", is_error=True)
            if ext in _LEGACY_OFFICE_EXTS:
                return ToolResult(
                    output=(
                        f"❌ {ext} 是旧版二进制格式，本工具无法解析。"
                        f"请先另存为 {ext}x 再处理。"
                    ),
                    is_error=True,
                )
            if ext in image_exts:
                size = file_path.stat().st_size
                return ToolResult(
                    output=f"[图片文件] {path}\n格式: {ext}\n大小: {size} bytes",
                )

            cache_key = (
                str(file_path.resolve()),
                int(offset or 1),
                -1 if limit is None else int(limit),
            )
            try:
                stat = file_path.stat()
                stamp = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                stamp = None
            if stamp is not None:
                cached = self._read_cache.get(cache_key)
                if cached and cached[0] == stamp[0] and cached[1] == stamp[1]:
                    if cached[4] < 1:
                        # 第一次重复：回目录而不是空话。历史滑窗可能已经把上次的
                        # 正文裁掉，只回“已读过”会让模型反复重读、直到被判定停滞。
                        try:
                            preview, *_ = read_text_preserving(file_path)
                        except TextEncodingError:
                            preview = file_path.read_text(encoding="utf-8", errors="replace")
                        self._read_cache[cache_key] = (
                            cached[0], cached[1], cached[2], cached[3], cached[4] + 1,
                        )
                        return ToolResult(
                            output=(
                                f"ℹ️ {path} 共 {cached[2]} 行，本次任务已读过、文件未修改。"
                                "正文不再重复，下面是它的结构目录：\n"
                                f"{_outline_of(preview)}\n\n"
                                "改动直接 edit_file；确需某段正文就用 offset/limit 读该区间"
                                "（同一区间再次请求会返回全文）。"
                            ),
                            metadata={"total_lines": cached[2], "duplicate_read": True},
                        )
                    # 第二次重复：模型显然没拿到正文，重新返回全文，别让它卡死。

            try:
                content, *_ = read_text_preserving(file_path)
            except TextEncodingError:
                # 只读路径：退化成宽松解码，让用户至少能看见内容
                content = file_path.read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()

            start = max(0, offset - 1)
            end = len(lines) if limit is None else start + limit
            selected = lines[start:end]

            # Add line numbers
            numbered = [
                f"{i + start + 1:>6}:{line}" for i, line in enumerate(selected)
            ]
            result = "\n".join(numbered)
            total_lines = len(lines)
            if stamp is not None:
                self._read_cache[cache_key] = (
                    stamp[0], stamp[1], total_lines, len(selected), 0,
                )
            metadata = {
                "total_lines": total_lines,
                "show_lines": len(selected),
                "encoding": "utf-8",
            }
            return ToolResult(
                output=result,
                metadata=metadata,
            )
        except Exception as e:
            return ToolResult(output=f"读取失败: {e}", is_error=True)


class WriteFileTool(BaseTool):
    """Write content to a file (create or overwrite)."""

    def __init__(self, workspace: str = ""):
        self.workspace = workspace

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return (
            "将内容写入文件。如果文件已存在则覆盖，父目录会自动创建。\n"
            "调用前务必检查 path 和 content 是否已填充，禁止传空对象 {}。\n"
            "正确示例: write_file(path=\"src/main.py\", content=\"print('hello')\")\n"
            "错误示例: write_file(path=\"\", content=\"\") 或 write_file()"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径（绝对路径或相对于项目根目录）",
                },
                "content": {
                    "type": "string",
                    "description": "要写入的内容",
                },
            },
            "required": ["path", "content"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        path = (
            kwargs.get("path")
            or kwargs.get("file_path")
            or kwargs.get("filename")
            or kwargs.get("filepath")
        )
        content = (
            kwargs.get("content")
            or kwargs.get("text")
            or kwargs.get("file_content")
            or kwargs.get("body")
            or ""
        )

        if not path:
            return ToolResult(
                output=f"❌ write_file 缺少文件路径参数。收到的参数: {list(kwargs.keys())}。请使用 path=\"文件路径\", content=\"内容\"。",
                is_error=True,
            )
        if not content:
            return ToolResult(
                output=f"❌ write_file 缺少内容参数。收到的参数: {list(kwargs.keys())}。请使用 path=\"{path}\", content=\"文件内容\"。",
                is_error=True,
            )
        if looks_like_numbered_dump(content):
            return ToolResult(output=_NUMBERED_DUMP_HINT, is_error=True)

        try:
            # ── Force relative paths to resolve against workspace ──
            file_path = Path(path)
            if not file_path.is_absolute() and self.workspace:
                file_path = Path(self.workspace) / path
            write_ext = file_path.suffix.lower()
            if write_ext in _OFFICE_CONTAINER_EXTS:
                return ToolResult(
                    output=f"❌ {_OFFICE_WRITE_HINT.format(ext=write_ext)}",
                    is_error=True,
                )
            # ── Snapshot before content for diff ──
            before_content = ""
            if file_path.exists():
                try:
                    before_content, *_ = read_text_preserving(file_path)
                except TextEncodingError:
                    before_content = file_path.read_text(encoding="utf-8", errors="replace")

            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            line_count = content.count("\n") + 1

            # ── Compute diff ──
            diff = _compute_diff(before_content, content, path)

            return ToolResult(
                output=f"已写入 {path} ({line_count} 行)",
                metadata={
                    "diff": diff,
                    "before_content": before_content[:5000],
                    "after_content": content[:5000],
                },
            )
        except Exception as e:
            return ToolResult(output=f"写入失败: {e}", is_error=True)


class EditFileTool(BaseTool):
    """Edit a file using search-and-replace."""

    def __init__(self, workspace: str = ""):
        self.workspace = workspace

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "使用搜索替换方式编辑文件。提供 old_str 和 new_str，会精确匹配 old_str 并替换为 new_str。\n"
            "适合局部修改，避免重写整个文件。path、old_str、new_str 都不能为空。\n"
            "示例: edit_file(path=\"src/main.py\", old_str=\"print('old')\", new_str=\"print('new')\")"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径",
                },
                "old_str": {
                    "type": "string",
                    "description": "要被替换的原始文本（必须精确匹配）",
                },
                "new_str": {
                    "type": "string",
                    "description": "替换后的新文本",
                },
            },
            "required": ["path", "old_str", "new_str"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        path = (
            kwargs.get("path")
            or kwargs.get("file_path")
            or kwargs.get("filename")
            or kwargs.get("filepath")
        )
        old_str = (
            kwargs.get("old_str")
            or kwargs.get("old_string")
            or kwargs.get("search")
            or kwargs.get("search_str")
            or ""
        )
        new_str = (
            kwargs.get("new_str")
            or kwargs.get("new_string")
            or kwargs.get("replace")
            or kwargs.get("replace_str")
            or ""
        )

        if not path:
            return ToolResult(
                output=f"❌ edit_file 缺少文件路径参数。收到的参数: {list(kwargs.keys())}。请使用 path=\"文件路径\", old_str=\"...\", new_str=\"...\"。",
                is_error=True,
            )
        if not old_str:
            return ToolResult(
                output=f"❌ edit_file 缺少 old_str 参数。收到的参数: {list(kwargs.keys())}。请使用 old_str=\"要替换的文本\"。",
                is_error=True,
            )
        # old_str 允许带行号（用于匹配已经被污染的文件），但写回去的必须是干净代码。
        if new_str and looks_like_numbered_dump(new_str):
            return ToolResult(output=_NUMBERED_DUMP_HINT, is_error=True)

        try:
            file_path = Path(path)
            if not file_path.is_absolute() and self.workspace:
                file_path = Path(self.workspace) / path
            if not file_path.exists():
                return ToolResult(
                    output=f"文件不存在: {path}\n(已解析为: {file_path}，工作目录: {self.workspace})",
                    is_error=True,
                )

            content, _enc, _nl, _bom = read_text_preserving(file_path)

            if old_str not in content:
                # Try stripping common whitespace differences
                old_stripped = old_str.strip()
                content_stripped_lines = []
                for line in content.splitlines():
                    content_stripped_lines.append(line.strip())
                content_stripped = "\n".join(content_stripped_lines)

                if old_stripped in content_stripped:
                    return ToolResult(
                        output="未找到精确匹配，但发现忽略空白后可匹配。请确保 old_str 与文件内容完全一致（包括缩进）。",
                        is_error=True,
                    )
                return ToolResult(
                    output=f"在文件中未找到:\n{old_str[:200]}...",
                    is_error=True,
                )

            count = content.count(old_str)
            if count > 1:
                return ToolResult(
                    output=f"找到 {count} 处匹配，请提供更多上下文使匹配唯一。",
                    is_error=True,
                )

            new_content = content.replace(old_str, new_str, 1)
            write_text_preserving(file_path, new_content, _enc, _nl, _bom)

            old_lines = old_str.count("\n") + 1
            new_lines = new_str.count("\n") + 1

            # ── Compute diff ──
            diff = _compute_diff(content, new_content, path)

            return ToolResult(
                output=f"已替换 {path}: {old_lines} 行 → {new_lines} 行",
                metadata={
                    "diff": diff,
                    "before_content": content[:5000],
                    "after_content": new_content[:5000],
                },
            )
        except Exception as e:
            return ToolResult(output=f"编辑失败: {e}", is_error=True)


def _compute_diff(before: str, after: str, path: str) -> str:
    """Compute unified diff between before and after content."""
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    diff_lines = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=path,
            tofile=path,
            lineterm="",
        )
    )
    return "\n".join(diff_lines)
