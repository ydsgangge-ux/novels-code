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

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from gangge.layer3_agent.tools.base import BaseTool, ToolResult

MAX_REPORT_CHARS = 2500
MAX_TIMEOUT = 600
#: 受影响测试扫描的上限：大仓库里逐个解析导入会变慢，超过就放弃、回退全量。
MAX_AFFECTED_SCAN = 2000

_TEST_DIR_NAMES = {"tests", "test", "__tests__", "spec", "e2e", "unittests"}
_SKIP_DIRS = {
    ".git", ".gangge", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", "target", ".tox", ".mypy_cache", ".pytest_cache",
    ".idea", ".vscode",
}
_TEST_NAME_RE = re.compile(
    r"(?:^|/)(?:"
    r"test_[^/]+\.py|[^/]+_test\.py|[^/]+_unittest\.py|"
    r"[^/]+\.(?:test|spec)\.(?:ts|tsx|js|jsx|mjs|cjs)|"
    r"[^/]+(?:Test|Tests)\.(?:java|kt|cs|swift)"
    r")$"
)

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


# ── 受影响测试（borrowed from CodeGraph's `affected`） ──────────────
# 改了一个文件就只跑“导入链上能到这个文件”的测试，而不是整套跑一遍。
# 找不到相关测试时一律回退全量：少跑一个测试的代价比多跑十分钟大得多。

def is_test_file(rel: str) -> bool:
    """按命名/所在目录判断是不是测试文件。"""
    parts = rel.split("/")[:-1]
    if any(part.lower() in _TEST_DIR_NAMES for part in parts):
        return True
    return bool(_TEST_NAME_RE.search(rel))


def discover_test_files(root: Path, limit: int = 500) -> list[str]:
    """列出项目里的测试文件（相对路径，posix 分隔符）。"""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        base = Path(dirpath)
        try:
            rel_dir = base.relative_to(root).as_posix()
        except ValueError:
            continue
        for name in filenames:
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            if is_test_file(rel):
                found.append(rel)
                if len(found) >= limit:
                    return found
    return found


def _normalize_rel(root: Path, path: str) -> str | None:
    raw = str(path or "").strip()
    if not raw:
        return None
    candidate = Path(raw)
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            return None
    return raw[2:] if raw.startswith("./") else raw


def _imports_of(path: Path, cache: dict[Path, set[str]] | None = None) -> set[str]:
    """抽出文件里的 import/require 说明符（只做正则级解析，够用且快）。"""
    if cache is not None and path in cache:
        return cache[path]
    text = _safe_read(path)
    specs: set[str] = set()
    if text:
        for match in re.finditer(r"^\s*from\s+([.\w]+)\s+import\b", text, re.MULTILINE):
            specs.add(match.group(1))
        for match in re.finditer(r"^\s*import\s+([.\w]+)", text, re.MULTILINE):
            specs.add(match.group(1))
        for match in re.finditer(
            r"""(?:from\s*|require\(\s*|import\(\s*)['"]([^'"]+)['"]""", text
        ):
            specs.add(match.group(1))
    if cache is not None:
        cache[path] = specs
    return specs


def _resolve_spec(root: Path, importer_rel: str, spec: str) -> list[str]:
    """把 import 说明符解析成项目内真实存在的文件（相对路径）。"""
    spec = spec.strip()
    if not spec:
        return []
    importer_dir = Path(importer_rel).parent
    out: list[str] = []

    def add(rel: str) -> None:
        rel = Path(rel).as_posix()
        if rel.startswith("./"):
            rel = rel[2:]
        if (root / rel).is_file():
            out.append(rel)

    is_js = "/" in spec or spec.endswith(
        (".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs")
    )
    if spec.startswith(".") and is_js:
        base = importer_dir / spec
        for suffix in ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py"):
            add(f"{base}{suffix}")
        for index in ("index.ts", "index.tsx", "index.js", "index.jsx"):
            add((base / index).as_posix())
        return out

    if spec.startswith("."):
        # Python 相对导入：.foo / ..foo.bar
        dots = len(spec) - len(spec.lstrip("."))
        rest = spec.lstrip(".").replace(".", "/")
        base = importer_dir
        for _ in range(max(0, dots - 1)):
            base = base.parent
        target = base / rest if rest else base
        add(f"{target}.py")
        add((target / "__init__.py").as_posix())
        return out

    if spec.startswith(("/", "@", "~")) or ":" in spec:
        return []
    if not re.fullmatch(r"[A-Za-z_][\w.]*", spec):
        return []

    module = spec.replace(".", "/")
    for prefix in ("", "src/"):
        add(f"{prefix}{module}.py")
        add(f"{prefix}{module}/__init__.py")
    return out


def _reaches_changed(
    root: Path,
    start_rel: str,
    changed: set[str],
    max_depth: int,
    cache: dict[Path, set[str]],
) -> bool:
    """从测试文件出发，沿 import 链最多走 max_depth 层，能否到达改动文件。"""
    seen = {start_rel}
    frontier = [start_rel]
    for _ in range(max_depth):
        nxt: list[str] = []
        for current in frontier:
            for spec in _imports_of(root / current, cache):
                for resolved in _resolve_spec(root, current, spec):
                    if resolved in changed:
                        return True
                    if resolved not in seen:
                        seen.add(resolved)
                        nxt.append(resolved)
        if not nxt:
            break
        frontier = nxt
    return False


def find_affected_tests(
    root: Path,
    changed: list[str],
    test_files: list[str],
    max_depth: int = 3,
) -> list[str]:
    """返回受改动影响的测试文件；找不到就返回空（调用方回退全量）。"""
    changed_rel = {
        rel for rel in (_normalize_rel(root, c) for c in changed) if rel
    }
    changed_rel = {rel for rel in changed_rel if (root / rel).is_file()}
    if not changed_rel:
        return []

    affected = {t for t in test_files if _normalize_rel(root, t) in changed_rel}
    cache: dict[Path, set[str]] = {}
    scanned = 0
    for test in test_files:
        if test in affected:
            continue
        if scanned >= MAX_AFFECTED_SCAN:
            break
        scanned += 1
        if _reaches_changed(root, test, changed_rel, max_depth, cache):
            affected.add(test)
    return sorted(affected)


def git_changed_files(root: Path) -> list[str]:
    """用 git 推断本次改动文件；不是 git 仓库就返回空。"""
    commands = (
        ["git", "diff", "--name-only", "HEAD"],
        ["git", "diff", "--name-only", "--cached"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    )
    changed: set[str] = set()
    for command in commands:
        try:
            proc = subprocess.run(
                command,
                cwd=str(root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        if proc.returncode != 0:
            return []
        for line in proc.stdout.splitlines():
            rel = line.strip().replace("\\", "/")
            if rel:
                changed.add(rel)
    return sorted(changed)


class RunTestsTool(BaseTool):
    """Run the project's test suite and report only failures."""

    def __init__(self, workspace: str = ""):
        self.workspace = workspace
        #: 本次任务写过的文件（loop 在调用前注入）。为空时退回 git 推断。
        self.changed_files: list[str] = []

    @property
    def name(self) -> str:
        return "run_tests"

    @property
    def description(self) -> str:
        return (
            "运行项目测试并只返回失败信息（自动识别 pytest / npm test / cargo test / go test）。"
            "改完代码后用这个验证，比 bash 拼命令更可靠。"
            "不给 path 时会优先只跑“受本次改动影响”的测试，找不到相关测试再回退全量。"
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

        # 只跑受影响的测试：没指定 path、没加 -k、且确实是 pytest 项目时才启用。
        # 找不到相关测试就继续走下面的全量路径——这是刻意的“安全方向”。
        if not target and not pattern:
            base = self._pytest_base(root)
            if base is not None:
                changed = [c for c in (self.changed_files or []) if str(c).strip()]
                if not changed:
                    changed = git_changed_files(root)
                if changed:
                    affected = find_affected_tests(
                        root, changed, discover_test_files(root)
                    )
                    if affected:
                        note = (
                            f"ℹ️ 只跑受影响的测试：改动 {len(changed)} 个文件，"
                            f"命中 {len(affected)} 个测试文件（找不到相关测试会自动回退全量）。"
                        )
                        return self._run(
                            root, base + affected, "pytest", timeout, note=note
                        )

        cmd, label = self._build_command(root, target, pattern)
        if not cmd:
            return ToolResult(
                output="❌ 没找到可用的测试运行器（pytest / npm test / cargo test / go test 都没有）。",
                is_error=True,
            )
        return self._run(root, cmd, label, timeout)

    def _run(
        self, root: Path, cmd: list[str], label: str, timeout: int, note: str = ""
    ) -> ToolResult:
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
        # 收集到 0 个用例 ≠ 代码失败。脚本式自测（__main__ + assert）在
        # pytest 眼里就是“没测试”，以前一律报 ❌，模型于是反复重写测试文件。
        no_tests = (not passed) and (
            "no tests ran" in output.lower() or "collected 0 items" in output.lower()
        )
        report = self._format(label, cmd, output, passed, proc.returncode)
        if note:
            report = f"{note}\n{report}"
        result = ToolResult(output=report, is_error=not passed and not no_tests)
        result.metadata = {"command": " ".join(cmd), "returncode": proc.returncode,
                           "output": output[:20000]}
        return result

    # ── helpers ──────────────────────────────────────────────

    def _pytest_base(self, root: Path) -> list[str] | None:
        """实际会走 pytest 时返回基础命令；会被其它运行器接手的项目返回 None。"""
        if (root / "Cargo.toml").exists() and shutil.which("cargo"):
            return None
        if (root / "go.mod").exists() and shutil.which("go"):
            return None
        if (root / "package.json").exists() and shutil.which("npm"):
            if '"test"' in _safe_read(root / "package.json"):
                return None
        if shutil.which("pytest"):
            return ["pytest", "-q", "--tb=short"]
        if _looks_like_python_tests(root):
            return ["python", "-m", "pytest", "-q", "--tb=short"]
        return None

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
        base = self._pytest_base(root)
        if base is not None:
            cmd = list(base)
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

        if "no tests ran" in output.lower() or "collected 0 items" in output.lower():
            return (
                f"ℹ️ {label} 没有收集到测试用例（退出码 {exit_code}），这通常不是代码错误。\n"
                f"{header}\n"
                "如果这是脚本式自测（__main__ 里跑断言），请用 bash 执行 "
                "`python <测试文件>` 验证；若要纳入本工具，把断言改写成 test_ 开头的函数。"
            )

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
