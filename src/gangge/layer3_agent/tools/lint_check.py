"""LSP / Lint check tool — post-edit syntax verification.

Runs the checker that matches the file's language on files the AI just wrote:
Python (pyright / ruff / pylint), JS/TS (eslint / tsc), Go (go vet),
Rust (cargo check). Errors are fed back to the AI for auto-fix.

This is NOT a full LSP integration — it's a lightweight post-edit
verification step that catches obvious syntax/type errors without
the overhead of running a language server.

Missing checkers are reported **once per process**: telling a model
"安装 ruff" on every single write wastes tokens and it cannot install
anything anyway. The first message stays in context, later ones are a
one-liner.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from gangge.layer3_agent.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

#: 已经提示过"缺检查器"的语言，避免每次都占一段输出。
_MISSING_REPORTED: set[str] = set()

_PY_EXTS = {".py"}
_JS_EXTS = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}
_GO_EXTS = {".go"}
_RS_EXTS = {".rs"}

# 纯 JS 后缀：tsc 在没开 allowJs 时，无论代码对错都会报"不支持的扩展名/需要 allowJs"，
# 属于保证性假阳性。对这类文件显式开 allowJs 且只做语法检查（checkJs=false）。
_JS_ONLY_EXTS = {".js", ".jsx", ".mjs", ".cjs"}

_PYTHON_CHECKERS: dict[str, dict[str, Any]] = {
    "pyright": {
        "cmd": "pyright",
        "args": ["--outputjson"],
        "parse": "_parse_pyright",
        "exts": _PY_EXTS,
    },
    "ruff": {
        "cmd": "ruff",
        "args": ["check", "--output-format=json"],
        "parse": "_parse_ruff",
        "exts": _PY_EXTS,
    },
    "pylint": {
        "cmd": "pylint",
        "args": ["--output-format=json"],
        "parse": "_parse_pylint",
        "exts": _PY_EXTS,
    },
}

_PY_INSTALL_HINT = "pip install ruff（最快）或 pip install pyright"
_JS_INSTALL_HINT = "npm i -D eslint typescript（或用 npx 装到项目里）"
_GO_INSTALL_HINT = "安装 Go 工具链后 go vet 可用"
_RS_INSTALL_HINT = "安装 Rust 工具链后 cargo check 可用"


def _detect_python_checker() -> str | None:
    for name in ("pyright", "ruff", "pylint"):
        if shutil.which(name):
            return name
    return None


def _missing(lang: str, hint: str) -> ToolResult:
    """缺检查器：第一次说清楚，之后一行带过。"""
    if lang in _MISSING_REPORTED:
        return ToolResult(output=f"（{lang} 检查器缺失，已跳过）")
    _MISSING_REPORTED.add(lang)
    return ToolResult(
        output=f"⚠️ 没找到 {lang} 检查器，这次先跳过语法检查。装上之后后续会自动生效：{hint}",
    )


# ── Parsers ──────────────────────────────────────────────


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
            if Path(path).name not in fname and path not in fname:
                continue
            # ruff 的 JSON 自带 severity；语法错误是 code="invalid-syntax"，
            # 不按这个字段判定会把"根本跑不起来"当成 warning。
            raw_severity = str(d.get("severity") or "").lower()
            code = str(d.get("code") or "")
            if raw_severity in ("error", "warning"):
                severity = raw_severity
            elif not code or code.startswith(("F", "E9")) or "syntax" in code.lower():
                severity = "error"
            else:
                severity = "warning"
            results.append({
                "line": d.get("location", {}).get("row", 0),
                "severity": severity,
                "message": f"{code}: {d.get('message', '')}".strip(": "),
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


_TS_LINE_RE = re.compile(r"\((\d+),(\d+)\):\s*(error|warning)\s+(TS\d+)?:?\s*(.*)")

# tsc 的"配置/输入"类提示与代码质量无关，不能当成代码错误回给模型。
_TS_CONFIG_NOISE_RE = re.compile(
    r"allowJs|unsupported extension|No inputs were found|TS18003|TS6054|TS5053",
    re.I,
)


def _parse_tsc(output: str, path: str) -> list[dict]:
    """tsc --pretty false 的输出：``file(12,5): error TS2322: ...``"""
    results = []
    for line in (output or "").splitlines():
        if Path(path).name not in line and path not in line:
            continue
        if _TS_CONFIG_NOISE_RE.search(line):
            continue
        m = _TS_LINE_RE.search(line)
        if not m:
            continue
        results.append({
            "line": int(m.group(1)),
            "severity": "error" if m.group(3) == "error" else "warning",
            "message": f"{m.group(4) or ''} {m.group(5)}".strip(),
        })
    return results


def _parse_eslint(output: str, path: str) -> list[dict]:
    if not output:
        return []
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    results = []
    for entry in data:
        for m in entry.get("messages", []) or []:
            results.append({
                "line": m.get("line", 0),
                "severity": "error" if m.get("severity") == 2 else "warning",
                "message": f"{m.get('ruleId') or ''}: {m.get('message', '')}".strip(": "),
            })
    return results


_GO_LINE_RE = re.compile(r"^(?P<file>[^:]+\.go):(?P<line>\d+):(?P<col>\d+):\s*(?P<msg>.+)$")


def _parse_go(output: str, path: str) -> list[dict]:
    results = []
    for line in (output or "").splitlines():
        m = _GO_LINE_RE.match(line.strip())
        if not m:
            continue
        if Path(path).name not in m.group("file") and Path(path).name not in line:
            continue
        results.append({
            "line": int(m.group("line")),
            "severity": "error",
            "message": m.group("msg"),
        })
    return results


_CARGO_LINE_RE = re.compile(r"^(?P<file>[^:]+\.rs):(?P<line>\d+):(?P<col>\d+):\s*(?P<sev>error|warning)")


def _parse_cargo(output: str, path: str) -> list[dict]:
    results = []
    for line in (output or "").splitlines():
        m = _CARGO_LINE_RE.match(line.strip())
        if not m:
            continue
        results.append({
            "line": int(m.group("line")),
            "severity": "error" if m.group("sev") == "error" else "warning",
            "message": line.strip()[:300],
        })
    return results


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
            "对指定文件运行语法检查（Python: pyright/ruff/pylint；JS/TS: eslint/tsc；"
            "Go: go vet；Rust: cargo check）。"
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
        if ext in _PY_EXTS:
            return self._check_python(file_path)
        if ext in _JS_EXTS:
            return self._check_js_ts(file_path)
        if ext in _GO_EXTS:
            return self._check_go(file_path)
        if ext in _RS_EXTS:
            return self._check_rust(file_path)
        return ToolResult(output=f"lint_check 暂不支持 {ext} 文件，跳过检查。")

    # ── per-language backends ────────────────────────────────

    def _check_python(self, file_path: Path) -> ToolResult:
        name = _detect_python_checker()
        if not name:
            return _missing("Python", _PY_INSTALL_HINT)
        checker = _PYTHON_CHECKERS[name]
        cmd = [checker["cmd"]] + checker["args"] + [str(file_path)]
        return self._run_and_report(cmd, checker["parse"], name, file_path)

    def _check_js_ts(self, file_path: Path) -> ToolResult:
        npm = shutil.which("npx") or shutil.which("npx.cmd")
        if not npm:
            return _missing("JS/TS", _JS_INSTALL_HINT)

        eslint = shutil.which("eslint") or shutil.which("eslint.cmd")
        if eslint or self._has_local_bin(file_path, "eslint"):
            cmd = [eslint or npm, "-f", "json", str(file_path)] if eslint else [
                npm, "--no-install", "eslint", "-f", "json", str(file_path)
            ]
            return self._run_and_report(cmd, "_parse_eslint", "eslint", file_path)

        tsc = shutil.which("tsc") or shutil.which("tsc.cmd")
        if tsc or self._has_local_bin(file_path, "tsc"):
            if tsc:
                cmd = [tsc, "--noEmit", "--pretty", "false"]
            else:
                cmd = [npm, "--no-install", "tsc", "--noEmit", "--pretty", "false"]
            if file_path.suffix.lower() in _JS_ONLY_EXTS:
                # 不带 allowJs 的话 tsc 对任何 .js 都报错，与代码本身无关
                cmd += ["--allowJs", "--checkJs", "false"]
            cmd.append(str(file_path))
            return self._run_and_report(cmd, "_parse_tsc", "tsc", file_path)

        return _missing("JS/TS", _JS_INSTALL_HINT)

    def _check_go(self, file_path: Path) -> ToolResult:
        go = shutil.which("go")
        if not go:
            return _missing("Go", _GO_INSTALL_HINT)
        return self._run_and_report([go, "vet", str(file_path)], "_parse_go", "go vet", file_path)

    def _check_rust(self, file_path: Path) -> ToolResult:
        cargo = shutil.which("cargo")
        if not cargo:
            return _missing("Rust", _RS_INSTALL_HINT)
        return self._run_and_report(
            [cargo, "check", "--quiet", "--message-format", "short"],
            "_parse_cargo", "cargo check", file_path,
        )

    def _has_local_bin(self, file_path: Path, name: str) -> bool:
        """项目里装了 node_modules/.bin/<name> 也算有。"""
        root = Path(self.workspace) if self.workspace else file_path.parent
        return (root / "node_modules" / ".bin" / f"{name}.cmd").exists() or (
            root / "node_modules" / ".bin" / name
        ).exists()

    def _run_and_report(
        self,
        cmd: list[str],
        parser: str,
        label: str,
        file_path: Path,
    ) -> ToolResult:
        try:
            result = subprocess.run(
                cmd,
                cwd=self.workspace or str(file_path.parent),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(output=f"{label} 检查超时（60s），已跳过。")
        except (OSError, ValueError) as e:
            # 检查器本身跑不起来不算代码错，别把模型带跑偏
            logger.debug("[lint] %s failed to start: %s", label, e)
            return ToolResult(output=f"{label} 无法启动（{e}），已跳过检查。")

        parse_fn = globals().get(parser)
        stdout = result.stdout or ""
        diagnostics = parse_fn(stdout, str(file_path)) if parse_fn else []
        if not diagnostics and result.returncode == 0:
            return ToolResult(output=f"✅ {label} 检查通过，未发现错误。")
        if not diagnostics:
            # 解析不出来但退出码非 0：把原始输出截一段还回去，别装作通过
            snippet = ((result.stdout or "") + (result.stderr or "")).strip()
            if snippet and _TS_CONFIG_NOISE_RE.search(snippet):
                # 检查器自身的配置问题（例如 tsc 不认识 .js），不是代码错误，
                # 报成 error 会让模型反复"修"一份本来正确的代码。
                return ToolResult(output=f"{label} 只报了配置提示，已跳过：{snippet[:200]}")
            return ToolResult(
                output=f"⚠️ {label} 退出码 {result.returncode}，但没能解析出具体位置：\n{snippet[:800]}",
                is_error=True,
            )

        errors = [d for d in diagnostics if d["severity"] == "error"]
        warnings = [d for d in diagnostics if d["severity"] == "warning"]
        lines = [f"🔍 {label} 发现 {len(errors)} 个错误, {len(warnings)} 个警告:\n"]
        for d in diagnostics[:15]:
            icon = "❌" if d["severity"] == "error" else "⚠️"
            lines.append(f"  {icon} 行 {d['line']}: {d['message']}")
        if len(diagnostics) > 15:
            lines.append(f"  ... 还有 {len(diagnostics) - 15} 个问题")
        return ToolResult(output="\n".join(lines), is_error=bool(errors))
