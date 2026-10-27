"""Cross-session memory — ``ctx.memory``.

Previously the memory bank lived inside ``loop.py`` as four inline helpers, so
the only way to change it was to edit the engine. As a plugin it subscribes to
``turn/end`` instead, and a profile can drop or replace it entirely.

Three Markdown files under ``.gangge/`` keep the bank human-readable and
diffable — a memory you cannot read is a memory you cannot debug::

    .gangge/progress.md     what the project is currently doing
    .gangge/changelog.md    what changed, turn by turn
    .gangge/decisions.md    why it changed
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from gangge.runtime import events
from gangge.runtime.context import Context, Inject

logger = logging.getLogger(__name__)

name = "memory_bank"
inject = Inject(optional=("telemetry",))

HEADER = "<!-- maintained by the memory-bank plugin; append-only -->"


class MemoryBank:
    """Load / append the three memory files."""

    def __init__(self, ctx: Context, config: dict[str, Any]) -> None:
        self.ctx = ctx
        self.config = config or {}
        base = Path(self.config.get("workspace") or ".")
        self.dir = (base / str(self.config.get("dir") or ".gangge")).resolve()
        self.files = {
            "progress": str(self.config.get("progress_file") or "progress.md"),
            "changelog": str(self.config.get("changelog_file") or "changelog.md"),
            "decisions": str(self.config.get("decisions_file") or "decisions.md"),
        }
        self.auto_save = bool(self.config.get("auto_save", True))
        self._cache: dict[str, str] = {}

    # -- reading -----------------------------------------------

    def path_of(self, section: str) -> Path:
        return self.dir / self.files[section]

    def read(self, section: str) -> str:
        if section in self._cache:
            return self._cache[section]
        path = self.path_of(section)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        except Exception:
            logger.debug("[memory] cannot read %s", path, exc_info=True)
            text = ""
        self._cache[section] = text
        return text

    def load(self) -> dict[str, str]:
        """Everything the prompt builder needs, in one call."""
        return {section: self.read(section) for section in self.files}

    @property
    def brief(self) -> str:
        """Compact form for system-prompt injection (empty sections omitted)."""
        parts = []
        for section, label in (
            ("progress", "当前进度"),
            ("decisions", "已定决策"),
            ("changelog", "最近变更"),
        ):
            text = self.read(section).strip()
            if text:
                parts.append(f"### {label}\n{text[-1500:]}")
        return "\n\n".join(parts)

    # -- writing -----------------------------------------------

    def append(self, section: str, text: str) -> Path | None:
        """Append one dated block. Returns the file written, or None."""
        text = (text or "").strip()
        if not text:
            return None
        path = self.path_of(section)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text(HEADER + "\n\n", encoding="utf-8")
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            with path.open("a", encoding="utf-8") as fh:
                fh.write(f"\n## {stamp}\n{text}\n")
        except Exception:
            logger.warning("[memory] cannot write %s", path, exc_info=True)
            return None
        self._cache.pop(section, None)
        return path

    def on_turn(self, outcome: Any) -> None:
        """Record a finished turn. Called from the ``turn/end`` listener."""
        if not self.auto_save:
            return
        summary = _summarize(outcome)
        if summary:
            self.append("changelog", summary)

    def close(self) -> None:
        self._cache.clear()


def _summarize(outcome: Any) -> str:
    """Turn whatever ``turn/end`` carried into a one-block summary."""
    if outcome is None:
        return ""
    if isinstance(outcome, str):
        return outcome.strip()
    if isinstance(outcome, dict):
        bits = []
        for key in ("summary", "final_response", "progress"):
            value = outcome.get(key)
            if isinstance(value, str) and value.strip():
                bits.append(value.strip())
        files = outcome.get("files") or outcome.get("changed_files")
        if files:
            listed = ", ".join(f"`{f}`" for f in list(files)[:10])
            bits.append(f"改动: {listed}")
        return "\n".join(bits)
    text = getattr(outcome, "final_response", "") or ""
    return text.strip()[:2000]


def apply(ctx: Context, config: dict[str, Any]) -> None:
    workspace = config.get("workspace") or ctx.data.get("workspace") or "."
    merged = {**config, "workspace": str(workspace)}
    bank = MemoryBank(ctx, merged)

    ctx.provide("memory", bank)
    ctx.data["memory_files"] = {s: str(bank.path_of(s)) for s in bank.files}

    ctx.on(events.TURN_END, bank.on_turn)
    ctx.on(events.MEMORY_TURN, bank.on_turn)
    ctx.on_cleanup(bank.close)

    existing = [s for s in bank.files if bank.read(s)]
    logger.info(
        "[memory] bank ready in %s (%s)",
        bank.dir,
        "existing: " + ", ".join(existing) if existing else "empty",
    )
