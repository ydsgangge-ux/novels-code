"""Workspace context loaders — shared by the desktop shell and the web shell.

These used to live in ``desktop/app.py``, which meant the web backend could not
reuse them without importing PyQt6. They are pure functions over a workspace
path (read files, run ``git``, query the repo index), so they belong in the
engine, not in a UI file.

Keeping them here also gives the two shells one definition of "what the agent
sees about a project before it starts": ARCH.md, README, ``.ganggerules``,
directory tree, symbol map, file registry, memory bank, git state. If desktop
and web built that differently, the same task would behave differently
depending on which window you typed it into.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

MEMORY_BANK_DIR = ".gangge"

#: Directories that never belong in a prompt: generated, vendored, or huge.
_EXCLUDED_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    "dist", "build", ".idea", ".vscode", ".egg-info",
}


def scan_project_context(workspace: str) -> str:
    """Scan workspace for ARCH.md, README.md, .ganggerules, and directory structure."""
    parts = []
    ws = Path(workspace)
    if not ws.is_dir():
        return ""

    arch_path = ws / "ARCH.md"
    if arch_path.exists():
        try:
            content = arch_path.read_text(encoding="utf-8", errors="replace")[:3000]
            parts.append(f"## 架构文档 (ARCH.md)\n{content}")
        except Exception:
            pass

    readme_path = ws / "README.md"
    if readme_path.exists():
        try:
            lines = readme_path.read_text(encoding="utf-8", errors="replace").splitlines()[:100]
            parts.append("## 项目说明 (README.md)\n" + "\n".join(lines))
        except Exception:
            pass

    rules_path = ws / ".ganggerules"
    if rules_path.exists():
        try:
            content = rules_path.read_text(encoding="utf-8", errors="replace")[:3000]
            parts.append(f"## 项目规则 (.ganggerules)\n{content}")
        except Exception:
            pass

    try:
        struct_lines = [f"{ws.name}/"]
        entries = sorted(ws.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        for e in entries:
            if e.name.startswith(".") or e.name in _EXCLUDED_DIRS:
                continue
            prefix = "├── " if e != entries[-1] else "└── "
            suffix = "/" if e.is_dir() else ""
            struct_lines.append(f"{prefix}{e.name}{suffix}")
            if e.is_dir() and e.name not in _EXCLUDED_DIRS:
                try:
                    sub = sorted(e.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))[:10]
                    for s2 in sub:
                        if s2.name.startswith(".") or s2.name in _EXCLUDED_DIRS:
                            continue
                        p2 = "│   ├── " if s2 != sub[-1] else "│   └── "
                        s2_suffix = "/" if s2.is_dir() else ""
                        struct_lines.append(f"{p2}{s2.name}{s2_suffix}")
                except Exception:
                    pass
        parts.append("## 目录结构\n" + "\n".join(struct_lines))
    except Exception:
        pass

    return "\n\n".join(parts)


def build_project_map(workspace: str, max_entries: int = 120) -> str:
    """Generate a project file index using repo_index (multi-language)."""
    from gangge.layer4_tools.repo_index import (
        build_dependency_graph,
        format_project_map,
        get_or_build_index,
    )

    try:
        index = get_or_build_index(workspace)
        dep_graph = build_dependency_graph(index, workspace)
        return format_project_map(index, dep_graph, max_entries)
    except Exception:
        pass

    ws = Path(workspace)
    if not ws.is_dir():
        return ""

    lines = []
    for py_file in sorted(ws.rglob("*.py")):
        if any(p in _EXCLUDED_DIRS for p in py_file.parts):
            continue
        try:
            rel = py_file.relative_to(ws)
            head = ""
            with open(py_file, encoding="utf-8", errors="replace") as f:
                for _ in range(20):
                    line = f.readline()
                    if not line:
                        break
                    s = line.strip()
                    if s.startswith(("class ", "def ", "async def ")):
                        sig = s.split("(")[0].strip().rstrip(":")
                        head += sig + "; "
            lines.append(f"- `{rel}`: {head[:150]}" if head else f"- `{rel}`")
        except Exception:
            pass

    if not lines:
        return ""

    truncated = lines[:max_entries]
    result = "\n".join(truncated)
    if len(lines) > max_entries:
        result += f"\n... 共 {len(lines)} 个文件，仅显示前 {max_entries} 个"
    return result


def build_initial_file_registry(workspace: str) -> dict[str, dict]:
    """Scan existing files to build initial file registry using repo_index."""
    from gangge.layer4_tools.repo_index import get_or_build_index

    registry: dict[str, dict] = {}
    try:
        index = get_or_build_index(workspace)
        for path, entry in index.get("files", {}).items():
            syms = entry.get("symbols", [])
            registry[path] = {
                "classes": [s["name"] for s in syms if s["kind"] == "class"][:10],
                "functions": [s["name"] for s in syms if s["kind"] in ("function", "method")][:15],
                "last_action": "existing",
                "round": 0,
            }
        return registry
    except Exception:
        pass

    ws = Path(workspace)
    if not ws.is_dir():
        return registry

    for py_file in sorted(ws.rglob("*.py")):
        if any(p in _EXCLUDED_DIRS for p in py_file.parts):
            continue
        try:
            rel = str(py_file.relative_to(ws))
            text = py_file.read_text(encoding="utf-8", errors="replace")
            classes: list[str] = []
            functions: list[str] = []
            for line in text.splitlines():
                s = line.strip()
                if s.startswith("class ") and ":" in s:
                    classes.append(s.split("(")[0].replace("class ", "").replace(":", "").strip())
                elif s.startswith(("def ", "async def ")):
                    functions.append(s.replace("async def ", "").replace("def ", "").split("(")[0].strip())
            registry[rel] = {
                "classes": classes[:10],
                "functions": functions[:15],
                "last_action": "existing",
                "round": 0,
            }
        except Exception:
            pass

    return registry


def read_memory_bank(workspace: str) -> tuple[str, str]:
    """Read .gangge/progress.md and .gangge/changelog.md content."""
    mb_dir = Path(workspace) / MEMORY_BANK_DIR
    progress = ""
    changelog = ""
    if mb_dir.exists():
        for name, setter in (("progress.md", "p"), ("changelog.md", "c")):
            path = mb_dir / name
            if not path.exists():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")[:3000]
            except Exception:
                continue
            if setter == "p":
                progress = text
            else:
                changelog = text
    return progress, changelog


def detect_git_state(workspace: str) -> str:
    """Detect current Git state: branch, uncommitted files, recent commits."""
    if not (Path(workspace) / ".git").exists():
        return ""

    parts: list[str] = []

    def _git(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", *args],
                cwd=workspace, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=5,
            ).stdout.strip()
        except Exception:
            return ""

    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    if branch and branch != "HEAD":
        parts.append(f"分支: {branch}")

    status = _git("status", "--short")
    if status:
        lines = status.splitlines()[:10]
        parts.append(f"未提交变更 ({len(lines)} 文件):\n" + "\n".join(f"  {line}" for line in lines))

    log = _git("log", "--oneline", "-5")
    if log:
        parts.append("最近提交:\n" + "\n".join(f"  {line}" for line in log.splitlines()))

    return "\n".join(parts) if parts else ""
