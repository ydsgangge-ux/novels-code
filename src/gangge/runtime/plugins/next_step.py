"""Next-step hints — tell a weak model what the conventional move is.

A small model often stalls *after* a successful tool call, not before it. It
reads a file and then writes a summary of the file. It greps and then stops to
ask what to do next. Nothing in the transcript says "a read is usually followed
by an edit", because a strong model doesn't need to be told.

So the first time each tool is used in a turn, one short line is appended to
its result — right where the model is looking::

    def login(user):
    ...file content...

    ↳ 下一步：要修改就用 edit_file；要看去哪用 grep 找相关引用。

Rules that keep this from becoming noise:

* **Once per tool per turn** — the hint orients, repetition nags.
* **One line, ≤ 60 chars** — anything longer becomes prompt bloat that hurts
  the same model it is meant to help.
* **Errors always get a hint** — failure is exactly where guidance pays off,
  and it is bounded by the turn anyway.
* Only tools with a *conventional* successor appear in the table. There is no
  sensible generic advice for ``TodoWrite``, so it stays silent.
"""

from __future__ import annotations

import logging
from typing import Any

from gangge.runtime import events
from gangge.runtime.context import Context, Inject

logger = logging.getLogger(__name__)

name = "next_step"
inject = Inject()

# tool → advice for the next move. Deliberately short: one line, imperative.
HINTS: dict[str, str] = {
    "read_file": "要修改就用 edit_file；要看去哪用 grep 找相关引用。",
    "grep": "用 read_file 打开命中的文件，再决定改哪一处。",
    "glob": "用 read_file 打开关键文件，不要只列文件。",
    "list_dir": "用 read_file 打开关键文件，不要停在列目录。",
    "find_symbol": "用 read_file 看定义，用 find_references 找调用方。",
    "find_references": "用 read_file 逐个确认调用方，再决定改哪处。",
    "write_file": "如果是代码，用 bash 或 lint_check 验证能跑。",
    "edit_file": "改完用 bash 或 lint_check 验证，再继续下一步。",
    "TodoWrite": "",
    "chat_log": "",
    "ask_user": "",
}

ERROR_HINT = "看上面的报错修正后再重试；不要用完全相同的参数重复调用。"
VERIFY_HINT = "失败时读完整报错，定位到具体行再改，不要猜。"


class NextStepHints:
    """Append at most one orientation line per tool per turn."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        self.hints = {**HINTS, **(config.get("hints") or {})}
        self.on_error_only = bool(config.get("on_error_only", False))
        self.max_chars = int(config.get("max_hint_chars", 80))
        self.hinted: set[str] = set()
        self.added = 0

    def reset(self, *_args: Any) -> None:
        self.hinted.clear()

    def annotate(self, outcome: Any, next: Any) -> Any:
        text = self._hint_for(outcome)
        if text:
            outcome.rendered = (outcome.rendered or "") + f"\n\n↳ {text}"
            outcome.metadata["hinted"] = True
            self.added += 1
        return next(outcome)

    # -- helpers -------------------------------------------------

    def _hint_for(self, outcome: Any) -> str:
        tool = outcome.tool
        is_error = bool(outcome.is_error)

        if is_error:
            # Errors are always worth a hint, but still only once per tool so a
            # retry loop does not grow the transcript.
            key = f"err:{tool}"
            if key in self.hinted:
                return ""
            self.hinted.add(key)
            return self._clip(VERIFY_HINT if tool in ("bash", "lint_check") else ERROR_HINT)

        if self.on_error_only:
            return ""
        if tool in self.hinted:
            return ""
        # Already annotated upstream? Then the slot is taken.
        if outcome.metadata.get("hinted"):
            return ""

        advice = self.hints.get(tool)
        if not advice:
            return ""
        self.hinted.add(tool)
        return self._clip(advice)

    def _clip(self, text: str) -> str:
        return text if len(text) <= self.max_chars else text[: self.max_chars - 1] + "…"


def apply(ctx: Context, config: dict[str, Any]) -> None:
    plugin = NextStepHints(config)
    ctx.provide("next_step", plugin)
    ctx.on(events.TOOLS_POST_EXECUTE, plugin.annotate)
    ctx.on(events.TURN_START, plugin.reset)
    logger.info("[next-step] orientation hints active (%d tools)", len(plugin.hints))
