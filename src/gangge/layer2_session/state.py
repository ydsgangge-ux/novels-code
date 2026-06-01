"""Project state — snapshot of current project context."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ProjectState:
    """Snapshot of the current project state for context injection."""

    workspace_dir: str = "."
    git_branch: str = ""
    recent_commits: list[str] = None  # type: ignore
    detected_languages: list[str] = None  # type: ignore
    project_structure: str = ""

    def __post_init__(self):
        if self.recent_commits is None:
            self.recent_commits = []
        if self.detected_languages is None:
            self.detected_languages = []

    def collect(self) -> None:
        """Gather project state information."""
        self._detect_git()
        self._detect_languages()

    def to_context_string(self) -> str:
        """Format as a string for system prompt injection."""
        parts = []
        if self.git_branch:
            parts.append(f"Git 分支: {self.git_branch}")
        if self.recent_commits:
            parts.append("最近提交:\n" + "\n".join(f"  - {c}" for c in self.recent_commits))
        if self.detected_languages:
            parts.append(f"项目语言: {', '.join(self.detected_languages)}")
        if self.project_structure:
            parts.append(f"项目结构:\n{self.project_structure}")
        return "\n".join(parts)

    def _detect_git(self) -> None:
        """Detect git branch and recent commits."""
        try:
            self.git_branch = subprocess.getoutput(
                "git rev-parse --abbrev-ref HEAD 2>/dev/null"
            ).strip()
            log = subprocess.getoutput(
                "git log --oneline -5 2>/dev/null"
            ).strip()
            if log:
                self.recent_commits = log.splitlines()
        except Exception:
            pass

    def _detect_languages(self) -> None:
        """Detect programming languages used in the project."""
        lang_extensions = {
            "Python": {".py", ".pyi"},
            "TypeScript": {".ts", ".tsx"},
            "JavaScript": {".js", ".jsx", ".mjs"},
            "Rust": {".rs"},
            "Go": {".go"},
            "Java": {".java"},
            "C/C++": {".c", ".cpp", ".h", ".hpp"},
            "HTML/CSS": {".html", ".css", ".scss"},
            "Shell": {".sh", ".bash", ".zsh"},
        }

        try:
            base = Path(self.workspace_dir)
            ext_count: dict[str, int] = {}
            for root, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {
                    "node_modules", "__pycache__", "venv", ".venv", "dist", "build"
                }]
                for f in files:
                    ext = Path(f).suffix.lower()
                    for lang, exts in lang_extensions.items():
                        if ext in exts:
                            ext_count[lang] = ext_count.get(lang, 0) + 1

            self.detected_languages = sorted(
                ext_count.keys(), key=lambda x: ext_count[x], reverse=True
            )[:5]
        except Exception:
            pass
