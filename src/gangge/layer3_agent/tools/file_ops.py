"""File operations tools — read, write, edit files."""

from __future__ import annotations

import difflib
import os
from pathlib import Path
from typing import Any

from gangge.layer3_agent.tools.base import BaseTool, ToolResult


class ReadFileTool(BaseTool):
    """Read file contents."""

    def __init__(self, workspace: str = ""):
        self.workspace = workspace

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
            if ext in image_exts:
                size = file_path.stat().st_size
                return ToolResult(
                    output=f"[图片文件] {path}\n格式: {ext}\n大小: {size} bytes",
                )

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

        try:
            # ── Force relative paths to resolve against workspace ──
            file_path = Path(path)
            if not file_path.is_absolute() and self.workspace:
                file_path = Path(self.workspace) / path
            # ── Snapshot before content for diff ──
            before_content = ""
            if file_path.exists():
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

        try:
            file_path = Path(path)
            if not file_path.is_absolute() and self.workspace:
                file_path = Path(self.workspace) / path
            if not file_path.exists():
                return ToolResult(
                    output=f"文件不存在: {path}\n(已解析为: {file_path}，工作目录: {self.workspace})",
                    is_error=True,
                )

            content = file_path.read_text(encoding="utf-8", errors="replace")

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
            file_path.write_text(new_content, encoding="utf-8")

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
