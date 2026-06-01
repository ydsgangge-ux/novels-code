"""LSP / Lint check tool — post-edit syntax verification.

Runs pyright, ruff, or pylint on modified files after AI writes them.
If errors are found, they are fed back to the AI for auto-fix.

This is NOT a full LSP integration — it's a lightweight post-edit
verification step that catches obvious syntax/type errors without
the overhead of running a language server.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

from gangge.layer3_agent.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

_CHECKERS: dict[str, dict[str, Any]] = {
    "pyright": {
        "cmd": "pyright",
        "args": ["--outputjson"],
        "parse": "_parse_pyright",
        "exts": {".py"},
    },
    "ruff": {
        "cmd": "ruff",
        "args": ["check", "--output-format=json"],
        "parse": "_parse_ruff",
        "exts": {".py"},
    },
    "pylint": {
        "cmd": "pylint",
        "args": ["--output-format=json"],
        "parse": "_parse_pylint",
        "exts": {".py"},
    },
}


def _detect_available_checker() -> str | None:
    for name in ("pyright", "ruff", "pylint"):
        if shutil.which(name):
            return name
    return None


def _parse_pyright(output: str, path: str) -> list[dict]:
    if not output:
        return []
    try:
        data = json.loads(output)
        diagnostics = data.get("generalDiagnostics", [])
        results = []
        for d in diagnostics:
            if d.get("file", "").endswith(Path(path).name) or path in d.get("file", ""):
                severity = d.get("severity", "error")
                if severity in ("error", "warning"):
                    results.append({
                        "line": d.get("range", {}).get("start", {}).get("line", 0) + 1,
                        "severity": severity,
                        "message": d.get("message", ""),
                    })
        return results
    except (json.JSONDecodeError, KeyError):
        return []


def _parse_ruff(output: str, path: str) -> list[dict]:
    if not output:
        return []
    try:
        data = json.loads(output)
        results = []
        for d in data:
            fname = d.get("filename", "")
            if Path(path).name in fname or path in fname:
                results.append({
                    "line": d.get("location", {}).get("row", 0),
                    "severity": "error" if d.get("code", "").startswith(("F", "E9")) else "warning",
                    "message": f"{d.get('code', '')}: {d.get('message', '')}",
                })
        return results
    except (json.JSONDecodeError, KeyError):
        return []


def _parse_pylint(output: str, path: str) -> list[dict]:
    if not output:
        return []
    try:
        data = json.loads(output)
        results = []
        for d in data:
            results.append({
                "line": d.get("line", 0),
                "severity": "error" if d.get("type") == "error" else "warning",
                "message": f"{d.get('symbol', '')}: {d.get('message', '')}",
            })
        return results
    except (json.JSONDecodeError, KeyError):
        return []


class LintCheckTool(BaseTool):
    """Run syntax/lint check on a file after AI modification."""

    def __init__(self, workspace: str = ""):
        self.workspace = workspace

    @property
    def name(self) -> str:
        return "lint_check"

    @property
    def description(self) -> str:
        return (
            "对指定文件运行语法检查（pyright/ruff/pylint）。"
            "在 write_file 或 edit_file 之后调用，验证代码是否有语法或类型错误。"
            "如果发现错误，请根据错误信息修复代码。"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要检查的文件路径",
                },
            },
            "required": ["path"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        path = (
            kwargs.get("path")
            or kwargs.get("file_path")
            or kwargs.get("filename")
            or kwargs.get("filepath")
            or ""
        )
        if not path:
            return ToolResult(
                output=f"❌ lint_check 缺少文件路径参数。收到的参数: {list(kwargs.keys())}。请使用 path=\"文件路径\"。",
                is_error=True,
            )
        file_path = Path(path)
        if not file_path.is_absolute() and self.workspace:
            file_path = Path(self.workspace) / path

        if not file_path.exists():
            return ToolResult(output=f"文件不存在: {path}", is_error=True)

        ext = file_path.suffix.lower()
        if ext != ".py":
            return ToolResult(
                output=f"lint_check 目前仅支持 Python 文件，{ext} 文件跳过检查。",
            )

        checker_name = _detect_available_checker()
        if not checker_name:
            return ToolResult(
                output="未检测到 pyright/ruff/pylint，跳过语法检查。"
                       "建议安装: pip install pyright 或 pip install ruff",
            )

        checker = _CHECKERS[checker_name]
        cmd = [checker["cmd"]] + checker["args"] + [str(file_path)]

        try:
            result = subprocess.run(
                cmd,
                cwd=self.workspace or str(file_path.parent),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )

            parse_fn = globals().get(checker["parse"])
            if parse_fn:
                stdout = result.stdout or ""
                diagnostics = parse_fn(stdout, str(file_path))
            else:
                diagnostics = []

            if not diagnostics:
                return ToolResult(
                    output=f"✅ {checker_name} 检查通过，未发现错误。",
                )

            errors = [d for d in diagnostics if d["severity"] == "error"]
            warnings = [d for d in diagnostics if d["severity"] == "warning"]

            lines = [f"🔍 {checker_name} 发现 {len(errors)} 个错误, {len(warnings)} 个警告:\n"]
            for d in diagnostics[:15]:
                icon = "❌" if d["severity"] == "error" else "⚠️"
                lines.append(f"  {icon} 行 {d['line']}: {d['message']}")

            if len(diagnostics) > 15:
                lines.append(f"  ... 还有 {len(diagnostics) - 15} 个问题")

            is_error = len(errors) > 0
            return ToolResult(
                output="\n".join(lines),
                is_error=is_error,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(output=f"{checker_name} 检查超时（30s）", is_error=True)
        except Exception as e:
            return ToolResult(output=f"检查失败: {e}", is_error=True)
