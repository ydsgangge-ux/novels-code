"""Symbol tools — find_symbol and find_references.

Uses the repo_index to provide symbol-level code navigation,
avoiding the need to repeatedly read entire files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from gangge.layer3_agent.tools.base import BaseTool, ToolResult
from gangge.layer4_tools.repo_index import get_or_build_index, LANG_EXTENSIONS, EXCLUDE_DIRS


class FindSymbolTool(BaseTool):
    """Find a symbol (class, function, method) by name across the project."""

    def __init__(self, workspace: str = ""):
        self.workspace = workspace

    @property
    def name(self) -> str:
        return "find_symbol"

    @property
    def description(self) -> str:
        return (
            "在项目中查找符号（类、函数、方法）。返回符号所在的文件路径、行号和类型。"
            "比 read_file + grep 更高效，直接定位到代码位置。"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "要查找的符号名称（支持部分匹配）",
                },
                "kind": {
                    "type": "string",
                    "description": "符号类型过滤: class, function, method。不填则返回所有类型",
                    "enum": ["class", "function", "method", ""],
                    "default": "",
                },
            },
            "required": ["name"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        name = kwargs["name"].strip()
        kind_filter = kwargs.get("kind", "").strip()

        if not name:
            return ToolResult(output="请提供符号名称", is_error=True)

        if not self.workspace:
            return ToolResult(output="工作目录未设置", is_error=True)

        try:
            index = get_or_build_index(self.workspace)
        except Exception as e:
            return ToolResult(output=f"索引构建失败: {e}", is_error=True)

        files = index.get("files", {})
        results = []

        name_lower = name.lower()

        for path, entry in files.items():
            syms = entry.get("symbols", [])
            for sym in syms:
                sym_name = sym["name"]
                sym_kind = sym["kind"]
                sym_line = sym["line"]

                if kind_filter and sym_kind != kind_filter:
                    continue

                if name_lower in sym_name.lower():
                    results.append({
                        "name": sym_name,
                        "kind": sym_kind,
                        "path": path,
                        "line": sym_line,
                    })

        if not results:
            return ToolResult(
                output=f"未找到符号 '{name}'"
                + (f" (类型: {kind_filter})" if kind_filter else "")
                + f"\n提示: 使用 grep 工具搜索内容，或检查符号名称是否正确"
            )

        lines = [f"找到 {len(results)} 个匹配的符号:\n"]
        for r in results[:30]:
            kind_icon = {"class": "[C]", "function": "[F]", "method": "[M]"}.get(r["kind"], "[?]")
            lines.append(f"  {kind_icon} {r['name']} ({r['kind']}) -> `{r['path']}:{r['line']}`")

        if len(results) > 30:
            lines.append(f"\n... 共 {len(results)} 个结果，仅显示前 30 个")

        return ToolResult(output="\n".join(lines))


class FindReferencesTool(BaseTool):
    """Find all references to a symbol across the project."""

    def __init__(self, workspace: str = ""):
        self.workspace = workspace

    @property
    def name(self) -> str:
        return "find_references"

    @property
    def description(self) -> str:
        return (
            "查找项目中所有引用某个符号的位置。用于修改代码前评估影响范围。"
            "返回引用该符号的文件列表和行号。"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "要查找引用的符号名称",
                },
            },
            "required": ["symbol"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        symbol = kwargs["symbol"].strip()

        if not symbol:
            return ToolResult(output="请提供符号名称", is_error=True)

        if not self.workspace:
            return ToolResult(output="工作目录未设置", is_error=True)

        try:
            index = get_or_build_index(self.workspace)
        except Exception as e:
            return ToolResult(output=f"索引构建失败: {e}", is_error=True)

        files = index.get("files", {})
        results = []

        import re
        pattern = re.compile(r'\b' + re.escape(symbol) + r'\b')

        for path, entry in files.items():
            filepath = Path(self.workspace) / path
            if not filepath.exists():
                continue

            try:
                text = filepath.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            for i, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    s = line.strip()
                    if s.startswith(("#", "//", "/*", "*")):
                        continue
                    results.append({
                        "path": path,
                        "line": i,
                        "text": s[:120],
                    })

        if not results:
            return ToolResult(
                output=f"未找到对 '{symbol}' 的引用\n提示: 该符号可能未被使用，或名称不正确"
            )

        definition_lines = []
        reference_lines = []

        for r in results:
            s = r["text"]
            is_def = (
                (s.startswith("class ") and symbol in s)
                or (s.startswith(("def ", "async def ")) and symbol in s)
                or (s.startswith("function ") and symbol in s)
                or (s.startswith("const ") and symbol in s and "=" in s)
                or (s.startswith("let ") and symbol in s and "=" in s)
                or (s.startswith("var ") and symbol in s and "=" in s)
            )
            if is_def:
                definition_lines.append(r)
            else:
                reference_lines.append(r)

        lines = [f"找到 {len(results)} 处对 '{symbol}' 的引用:\n"]

        if definition_lines:
            lines.append("[DEF] 定义:")
            for r in definition_lines[:5]:
                lines.append(f"  -> `{r['path']}:{r['line']}`  {r['text'][:80]}")

        if reference_lines:
            lines.append(f"\n[REF] 引用 ({len(reference_lines)} 处):")
            for r in reference_lines[:20]:
                lines.append(f"  -> `{r['path']}:{r['line']}`  {r['text'][:80]}")
            if len(reference_lines) > 20:
                lines.append(f"  ... 还有 {len(reference_lines) - 20} 处引用")

        return ToolResult(output="\n".join(lines))
