"""Search tools — grep, glob, and Everything file search."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from gangge.layer3_agent.tools.base import BaseTool, ToolResult


class GrepTool(BaseTool):
    """Search file contents using regex (like ripgrep)."""

    def __init__(self, workspace: str = ""):
        self.workspace = workspace

    @property
    def name(self) -> str:
        return "grep"

    @property
    def description(self) -> str:
        return (
            "在文件内容中搜索正则表达式匹配。支持指定搜索目录、文件类型过滤、上下文行数。"
            "返回匹配的文件路径和行号。"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "正则表达式搜索模式",
                },
                "path": {
                    "type": "string",
                    "description": "搜索目录，默认为项目根目录",
                    "default": ".",
                },
                "include": {
                    "type": "string",
                    "description": "文件类型过滤（glob 模式），如 '*.py', '*.ts'",
                },
                "exclude": {
                    "type": "string",
                    "description": "排除目录（glob 模式），如 'node_modules', '.git'",
                },
                "max_results": {
                    "type": "integer",
                    "description": "最大结果数，默认 50",
                    "default": 50,
                },
                "context": {
                    "type": "integer",
                    "description": "显示匹配行前后的上下文行数",
                    "default": 2,
                },
            },
            "required": ["pattern"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        pattern = kwargs.get("pattern") or kwargs.get("regex") or kwargs.get("query") or ""
        search_path = kwargs.get("path", ".")
        include = kwargs.get("include")
        exclude = kwargs.get("exclude")
        max_results = kwargs.get("max_results", 50)
        context_lines = kwargs.get("context", 2)

        if not pattern:
            return ToolResult(
                output=f"❌ grep 缺少搜索模式参数。收到的参数: {list(kwargs.keys())}。请使用 pattern=\"正则表达式\"。",
                is_error=True,
            )

        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return ToolResult(output=f"无效的正则表达式: {e}", is_error=True)

        base = Path(search_path)
        if not base.is_absolute() and self.workspace:
            base = Path(self.workspace) / search_path
        if not base.exists():
            return ToolResult(output=f"路径不存在: {search_path}", is_error=True)

        # Default exclude patterns
        exclude_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build"}
        if exclude:
            exclude_dirs.add(exclude)

        results = []
        files_searched = 0

        for root, dirs, filenames in os.walk(base):
            # Filter excluded directories
            dirs[:] = [d for d in dirs if d not in exclude_dirs and not d.startswith(".")]

            for fname in filenames:
                # Filter by include pattern
                if include:
                    # Support patterns like "*.py" or ".py" or "*.py,*.ts"
                    exts = []
                    for ext in include.split(","):
                        ext = ext.strip()
                        if ext.startswith("*"):
                            ext = ext[1:]  # Remove leading *
                        exts.append(ext)
                    if not any(fname.endswith(ext) for ext in exts):
                        continue

                # Skip binary files
                if fname.endswith((".pyc", ".pyo", ".so", ".dll", ".exe", ".png", ".jpg", ".gif")):
                    continue

                fpath = Path(root) / fname
                try:
                    content = fpath.read_text(encoding="utf-8", errors="replace")
                    lines = content.splitlines()
                except (OSError, PermissionError):
                    continue

                files_searched += 1
                for i, line in enumerate(lines):
                    if regex.search(line):
                        rel_path = fpath.relative_to(base) if base != Path(".") else fpath
                        # Get context
                        start = max(0, i - context_lines)
                        end = min(len(lines), i + context_lines + 1)
                        context = [
                            f"  {j + 1:>4}| {lines[j]}" + ("  ◀" if j == i else "")
                            for j in range(start, end)
                        ]
                        results.append(
                            f"\n{rel_path}:{i + 1}\n" + "\n".join(context)
                        )
                        if len(results) >= max_results:
                            break
                if len(results) >= max_results:
                    break
            if len(results) >= max_results:
                break

        if not results:
            return ToolResult(
                output=f"未找到匹配 '{pattern}' (已搜索 {files_searched} 个文件)",
            )

        output = f"找到 {len(results)} 处匹配:\n" + "".join(results)
        if len(results) >= max_results:
            output += f"\n\n(结果已截断，最多显示 {max_results} 处)"
        return ToolResult(output=output, metadata={"files_searched": files_searched})


class GlobTool(BaseTool):
    """Search for files by name pattern (like find/glob)."""

    def __init__(self, workspace: str = ""):
        self.workspace = workspace

    @property
    def name(self) -> str:
        return "glob"

    @property
    def description(self) -> str:
        return (
            "按文件名模式搜索文件。支持通配符 (*, **, ?)。"
            "例如: '*.py' 搜索所有 Python 文件, '**/test_*.py' 搜索所有测试文件。"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "文件名匹配模式（glob 格式）",
                },
                "path": {
                    "type": "string",
                    "description": "搜索根目录，默认为项目根目录",
                    "default": ".",
                },
                "max_results": {
                    "type": "integer",
                    "description": "最大结果数，默认 50",
                    "default": 50,
                },
            },
            "required": ["pattern"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        pattern = kwargs.get("pattern") or kwargs.get("glob") or kwargs.get("query") or ""
        search_path = kwargs.get("path", ".")
        max_results = kwargs.get("max_results", 50)

        if not pattern:
            return ToolResult(
                output=f"❌ glob 缺少模式参数。收到的参数: {list(kwargs.keys())}。请使用 pattern=\"glob模式\"。",
                is_error=True,
            )

        try:
            base = Path(search_path)
            if not base.is_absolute() and self.workspace:
                base = Path(self.workspace) / search_path
            if not base.exists():
                return ToolResult(output=f"路径不存在: {search_path}", is_error=True)

            matches = sorted(base.glob(pattern))

            # Filter out common unwanted directories
            exclude_dirs = {".git", "__pycache__", "node_modules", ".venv"}
            filtered = [
                m for m in matches
                if not any(part in exclude_dirs for part in m.parts)
                and m.is_file()
            ]

            if not filtered:
                return ToolResult(
                    output=f"未找到匹配 '{pattern}' 的文件",
                )

            results = [str(m.relative_to(base)) for m in filtered[:max_results]]
            output = "\n".join(results)
            if len(filtered) > max_results:
                output += f"\n\n(共 {len(filtered)} 个文件，已截断显示)"

            return ToolResult(
                output=output,
                metadata={"total_files": len(filtered)},
            )
        except Exception as e:
            return ToolResult(output=f"搜索失败: {e}", is_error=True)


class ListDirTool(BaseTool):
    """List directory contents."""

    def __init__(self, workspace: str = ""):
        self.workspace = workspace

    @property
    def name(self) -> str:
        return "list_dir"

    @property
    def description(self) -> str:
        return "列出目录内容（文件和子目录）。返回目录结构树。"

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "目录路径，默认为项目根目录",
                    "default": ".",
                },
                "max_depth": {
                    "type": "integer",
                    "description": "最大递归深度，默认 2",
                    "default": 2,
                },
            },
            "required": [],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        dir_path = kwargs.get("path", ".")
        max_depth = kwargs.get("max_depth", 2)

        base = Path(dir_path)
        if not base.is_absolute() and self.workspace:
            base = Path(self.workspace) / dir_path
        if not base.exists():
            return ToolResult(output=f"目录不存在: {dir_path}", is_error=True)

        if not base.is_dir():
            return ToolResult(output=f"不是目录: {dir_path}", is_error=True)

        exclude_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build"}

        lines = []

        def walk(path: Path, depth: int, prefix: str = ""):
            if depth > max_depth:
                return
            try:
                entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            except PermissionError:
                return

            entries = [e for e in entries if e.name not in exclude_dirs and not e.name.startswith(".")]

            for i, entry in enumerate(entries):
                is_last = (i == len(entries) - 1)
                connector = "└── " if is_last else "├── "
                suffix = "/" if entry.is_dir() else ""
                lines.append(f"{prefix}{connector}{entry.name}{suffix}")

                if entry.is_dir():
                    extension = "    " if is_last else "│   "
                    walk(entry, depth + 1, prefix + extension)

        lines.append(f"{base}/")
        walk(base, 1)

        return ToolResult(output="\n".join(lines))


class EverythingSearchTool(BaseTool):
    """Search files instantly using Everything (Windows).

    Leverages Everything's NTFS index for sub-second file searches across
    the entire filesystem. Requires Everything to be installed and running.
    """

    def __init__(self, workspace: str = "", es_path: str = ""):
        self.workspace = workspace
        self._es_path = es_path or self._find_es()
        self._available = bool(self._es_path)

    @staticmethod
    def _find_es() -> str:
        candidates = [
            r"C:\Program Files\Everything\es.exe",
            r"C:\Program Files (x86)\Everything\es.exe",
        ]
        for p in candidates:
            if os.path.isfile(p):
                return p
        # Try PATH
        try:
            import shutil
            found = shutil.which("es.exe")
            if found:
                return found
        except Exception:
            pass
        return ""

    @property
    def name(self) -> str:
        return "everything_search"

    @property
    def description(self) -> str:
        if not self._available:
            return "（不可用 — 未检测到 Everything 搜索工具）"
        return (
            "【超快】使用 Everything 引擎在 Windows 上按文件名/路径即时搜索文件。"
            "速度比普通文件搜索快 100 倍以上，支持数百万文件毫秒级定位。"
            "支持通配符 (*, ?)、路径筛选 (path:)、扩展名筛选 (ext:)、"
            "大小筛选 (size:)、日期筛选 (dm:) 等 Everything 查询语法。"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Everything 查询语句。支持：关键词模糊匹配、"
                        "通配符 *.py、路径限定 path:C:\\Projects、"
                        "扩展名 ext:pdf、大小 size:>1MB、"
                        "日期 dm:2024-01-01..2024-12-31。"
                        "多个条件用空格分隔。"
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": "最大返回结果数",
                    "default": 30,
                },
                "path": {
                    "type": "string",
                    "description": "限定搜索目录（可选），如 C:\\Projects",
                },
            },
            "required": ["query"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        if not self._available:
            return ToolResult(
                output="❌ Everything 搜索不可用。请安装 Everything (https://www.voidtools.com) 并确保 es.exe 在系统路径中。",
                is_error=True,
            )

        query = (kwargs.get("query") or kwargs.get("pattern") or "").strip()
        max_results = kwargs.get("max_results", 30)
        path_filter = kwargs.get("path", "")

        if not query:
            return ToolResult(
                output=f"❌ everything_search 缺少 query 参数。收到的参数: {list(kwargs.keys())}。请使用 query=\"搜索关键词\"。",
                is_error=True,
            )

        # Build es.exe command
        cmd = [self._es_path, "-s", query, "-n", str(max_results), "-p"]
        if path_filter:
            cmd.extend(["-path", path_filter])

        try:
            loop = asyncio.get_event_loop()
            proc = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                ),
            )
        except subprocess.TimeoutExpired:
            return ToolResult(output="⏱ Everything 搜索超时（30秒）", is_error=True)
        except FileNotFoundError:
            self._available = False
            return ToolResult(output="❌ 找不到 es.exe，Everything 可能未安装", is_error=True)
        except Exception as e:
            return ToolResult(output=f"❌ Everything 搜索失败: {e}", is_error=True)

        if proc.returncode != 0:
            # es.exe returns 1 when no results — that's not an error
            stderr = proc.stderr.strip()
            if stderr:
                return ToolResult(output=f"Everything 搜索错误: {stderr}", is_error=True)
            return ToolResult(output=f"未找到匹配 '{query}' 的文件（Everything 搜索完成）")

        results = [line for line in proc.stdout.splitlines() if line.strip()]
        if not results:
            return ToolResult(output=f"未找到匹配 '{query}' 的文件")

        output = f"🔍 Everything 搜索结果: {len(results)} 个文件\n" + "\n".join(results)
        return ToolResult(
            output=output,
            metadata={"total": len(results), "query": query},
        )
