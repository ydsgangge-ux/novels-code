"""run_tests — 跑项目测试，只把"失败了什么"还给模型。

弱模型不会自己想到跑测试，就算想到了也常常把命令拼错
（``pytest tests/test_a.py -k "x" -v --tb=short`` 里错一个引号就白跑一轮）。
所以这里把"选测试框架 / 拼命令 / 解析结果"都收进 Python，模型只需给一个
可选的路径和 ``-k`` 过滤。

输出刻意短：一行结论 + 失败用例名 + 每条失败的第一段报错。
完整输出留在 ``metadata`` 里，主上下文只吃摘要——弱模型在 3000 行 pytest
日志里找不到那 3 行断言。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from gangge.layer3_agent.tools.base import BaseTool, ToolResult

MAX_REPORT_CHARS = 2500
MAX_TIMEOUT = 600

_PYTEST_SUMMARY_RE = re.compile(
    r"^=*\s*(?P<parts>(?:\d+\s+(?:passed|failed|error|errors|skipped|xfailed|xpassed|warnings?)[,\s]*)+)"
    r"(?:in\s+[\d.]+s)?\s*=*$",
    re.IGNORECASE,
)
_PYTEST_FAILED_RE = re.compile(r"^(FAILED|ERROR)\s+(\S+)")


def _looks_like_python_tests(root: Path) -> bool:
    if (root / "pytest.ini").exists() or (root / "setup.cfg").exists():
        return True
    if (root / "pyproject.toml").exists() and "pytest" in _safe_read(root / "pyproject.toml"):
        return True
    for pattern in ("test_*.py", "*_test.py", "tests"):
        for _ in root.rglob(pattern):
            return True
    return False


def _safe_read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:20000]
    except OSError:
        return ""


class RunTestsTool(BaseTool):
    """Run the project's test suite and report only failures."""

    def __init__(self, workspace: str = ""):
        self.workspace = workspace

    @property
    def name(self) -> str:
        return "run_tests"

    @property
    def description(self) -> str:
        return (
            "运行项目测试并只返回失败信息（自动识别 pytest / npm test / cargo test / go test）。"
            "改完代码后用这个验证，比 bash 拼命令更可靠。"
            "可选 path（测试文件或目录）、pattern（只跑名字匹配的用例）、timeout（秒）。"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "测试文件或目录，留空则自动选择",
                },
                "pattern": {
                    "type": "string",
                    "description": "只跑名字匹配的用例（pytest 的 -k）",
                },
                "timeout": {
                    "type": "integer",
                    "description": f"超时秒数，默认 120，最大 {MAX_TIMEOUT}",
                },
            },
            "required": [],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        root = Path(self.workspace or ".").resolve()

        target = str(kwargs.get("path") or "").strip()
        if target:
            candidate = Path(target)
            if not candidate.is_absolute():
                candidate = root / target
            try:
                candidate.resolve().relative_to(root)
            except ValueError:
                return ToolResult(
                    output=f"❌ 测试路径必须在项目目录内：{target}",
                    is_error=True,
                )
            if not candidate.exists():
                return ToolResult(output=f"❌ 测试路径不存在：{target}", is_error=True)
            target = str(candidate)

        try:
            timeout = int(kwargs.get("timeout") or 120)
        except (TypeError, ValueError):
            timeout = 120
        timeout = max(10, min(timeout, MAX_TIMEOUT))

        pattern = str(kwargs.get("pattern") or "").strip()

        cmd, label = self._build_command(root, target, pattern)
        if not cmd:
            return ToolResult(
                output="❌ 没找到可用的测试运行器（pytest / npm test / cargo test / go test 都没有）。",
                is_error=True,
            )

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                output=f"⏱ 测试超时（>{timeout}s），可能有用例在等待输入。可以缩小 path 或加 pattern。",
                is_error=True,
            )
        except (OSError, ValueError) as e:
            return ToolResult(output=f"❌ 测试运行器启动失败：{e}", is_error=True)

        output = ((proc.stdout or "") + (proc.stderr or "")).strip()
        passed = proc.returncode == 0
        report = self._format(label, cmd, output, passed, proc.returncode)
        result = ToolResult(output=report, is_error=not passed)
        result.metadata = {"command": " ".join(cmd), "returncode": proc.returncode,
                           "output": output[:20000]}
        return result

    # ── helpers ──────────────────────────────────────────────

    def _build_command(self, root: Path, target: str, pattern: str) -> tuple[list[str], str]:
        if (root / "Cargo.toml").exists() and shutil.which("cargo"):
            return ["cargo", "test", "--quiet"], "cargo test"
        if (root / "go.mod").exists() and shutil.which("go"):
            cmd = ["go", "test", "./..."]
            if target:
                cmd = ["go", "test", target]
            return cmd, "go test"
        if (root / "package.json").exists() and shutil.which("npm"):
            pkg = _safe_read(root / "package.json")
            if '"test"' in pkg:
                return ["npm", "test", "--silent"], "npm test"
        if shutil.which("pytest") or _looks_like_python_tests(root):
            cmd = ["pytest", "-q", "--tb=short"]
            if not shutil.which("pytest"):
                cmd = ["python", "-m", "pytest", "-q", "--tb=short"]
            if target:
                cmd.append(target)
            if pattern:
                cmd += ["-k", pattern]
            return cmd, "pytest"
        return [], ""

    @staticmethod
    def _format(
        label: str,
        cmd: list[str],
        output: str,
        passed: bool,
        exit_code: int | None = None,
    ) -> str:
        header = f"$ {' '.join(cmd)}"
        if passed:
            tail = "\n".join(output.splitlines()[-3:])
            return f"✅ {label} 通过\n{header}\n{tail}".strip()

        summary = ""
        failures: list[str] = []
        for line in output.splitlines():
            stripped = line.strip()
            match = _PYTEST_SUMMARY_RE.match(stripped)
            if match and not summary:
                summary = " ".join(match.group("parts").split())
            failed = _PYTEST_FAILED_RE.match(stripped)
            if failed and failed.group(2) not in failures:
                failures.append(failed.group(2))

        lines = [header, ""]
        lines.append(f"❌ {label} 失败" + (f"：{summary}" if summary else ""))
        if failures:
            lines.append("失败用例：" + "、".join(failures[:8]))
            if len(failures) > 8:
                lines[-1] += f" 等 {len(failures)} 个"
        body = output[-MAX_REPORT_CHARS:] if len(output) > MAX_REPORT_CHARS else output
        lines.append("\n" + body)

        # 原生扩展崩掉时 pytest 一行都不输出，只剩一个 0xC0000005。不解释清楚，
        # 模型会以为是自己参数写错，然后一轮一轮地猜。
        from gangge.layer3_agent.tools.bash import describe_abnormal_exit

        diagnosis = describe_abnormal_exit(exit_code)
        if diagnosis:
            lines.append(diagnosis)

        return "\n".join(lines)[: MAX_REPORT_CHARS + 900]
