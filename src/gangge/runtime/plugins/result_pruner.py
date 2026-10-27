"""Result pruner — shrink tool output without losing the canonical value.

A weak model has two problems with a big tool result, and they are different:

* **It runs out of context.** A 200 KB ``bash`` dump or a whole-file read eats
  the window that the actual task needed.
* **It gets distracted.** ANSI colour codes, ``\r`` progress bars and eight
  consecutive blank lines are pure noise that measurably degrades small-model
  output.

So this listener cleans the text and truncates head+tail (keeping the tail,
because that is where errors and tracebacks live), while leaving
``outcome.value`` untouched for anything programmatic. Truncating what the
model *sees* must never truncate what the program *got* — that separation is
the whole point of the ``ToolOutcome`` shape.

Anything already short passes through byte-identical, so this costs nothing on
the common path.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from gangge.runtime import events
from gangge.runtime.context import Context, Inject

logger = logging.getLogger(__name__)

name = "result_pruner"
inject = Inject()

# CSI / OSC escape sequences. bash on Windows emits these freely via `chcp`
# and colour-aware tools; they are pure token waste in a prompt.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")

# Carriage-return progress bars: keep only the final frame of each line.
_CR_PROGRESS = re.compile(r"[^\n]*\r(?!\n)")

_MANY_BLANK = re.compile(r"\n[ \t]*\n(?:[ \t]*\n){2,}")


class ResultPruner:
    """Clean and bound tool output for the model's context window."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        self.max_chars = int(config.get("max_chars", 8000))
        self.head_ratio = float(config.get("head_ratio", 0.4))
        self.strip_ansi = bool(config.get("strip_ansi", True))
        self.collapse_blank = bool(config.get("collapse_blank", True))
        self.pruned = 0

    def prune(self, outcome: Any, next: Any) -> Any:
        text = outcome.rendered or ""
        cleaned = self._clean(text)
        if len(cleaned) > self.max_chars:
            cleaned = self._truncate(cleaned)
            self.pruned += 1
            outcome.metadata["pruned"] = True
            outcome.metadata["original_chars"] = len(text)
        if cleaned != text:
            outcome.rendered = cleaned
        return next(outcome)

    # -- stages --------------------------------------------------

    def _clean(self, text: str) -> str:
        if self.strip_ansi and "\x1b" in text:
            text = _ANSI.sub("", text)
        if "\r" in text and "\n" in text:
            # Drop every partial line that a carriage return overwrote.
            text = _CR_PROGRESS.sub(lambda m: m.group(0).rsplit("\r", 1)[-1], text)
        if self.collapse_blank:
            text = _MANY_BLANK.sub("\n\n\n", text)
        return text

    def _truncate(self, text: str) -> str:
        head = int(self.max_chars * self.head_ratio)
        marker = f"\n\n...[输出过长已截断，原长 {len(text)} 字符，此处保留开头 {head} 与结尾]...\n\n"
        tail = max(self.max_chars - head - len(marker), 0)
        return text[:head] + marker + (text[-tail:] if tail else "")


def apply(ctx: Context, config: dict[str, Any]) -> None:
    pruner = ResultPruner(config)
    ctx.provide("result_pruner", pruner)
    ctx.on(events.TOOLS_POST_EXECUTE, pruner.prune, prepend=bool(config.get("prepend", True)))
    logger.info("[pruner] tool output capped at %d chars", pruner.max_chars)
