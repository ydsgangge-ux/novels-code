"""Repeat guard — break the loop when a model re-issues an identical call.

This is the single most common way a small model burns a turn:

    read_file({"path": "main.py"})  → output
    read_file({"path": "main.py"})  → identical output   (it forgot, or hoped)
    read_file({"path": "main.py"})  → identical output   (now it is stuck)
    ... until max_tool_rounds, then the turn dies with nothing done

A strong model notices the identical output and changes strategy. A weak one
does not, because nothing in the transcript says "this already happened". So
once the same ``(tool, arguments)`` pair has been called ``limit`` times, the
result carries a short, actionable nudge appended to the end — where the model
just read the output — telling it what to do instead.

The nudge is deliberately concrete (change the argument / change the tool / use
what you already have) rather than "try something else", because vague advice is
exactly what a weak model cannot act on.

Counts are per runtime instance and reset on ``turn/start``, so a legitimate
re-read in a later turn is not penalised.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from gangge.runtime import events
from gangge.runtime.context import Context, Inject

logger = logging.getLogger(__name__)

name = "repeat_guard"
inject = Inject()

MAX_LEDGER = 512


class RepeatGuard:
    """Detect identical repeated calls and append a corrective hint."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        self.limit = int(config.get("limit", 3))
        self.hint_once = bool(config.get("hint_once", False))
        self.ledger: dict[str, int] = {}
        self.hinted: set[str] = set()
        self.trips = 0

    # -- pipeline ----------------------------------------------

    def count(self, invoke: Any, next: Any) -> Any:
        key = self._key(invoke)
        if key is None:
            return next(invoke)
        self.ledger[key] = self.ledger.get(key, 0) + 1
        if len(self.ledger) > MAX_LEDGER:
            self.ledger.clear()
            self.ledger[key] = 1
        invoke.metadata["repeat_count"] = self.ledger[key]
        invoke.metadata["call_key"] = key
        return next(invoke)

    def annotate(self, outcome: Any, next: Any) -> Any:
        key = outcome.metadata.get("call_key")
        count = outcome.metadata.get("repeat_count", 0)
        if not key or count < self.limit:
            return next(outcome)
        if self.hint_once and key in self.hinted:
            return next(outcome)

        self.hinted.add(key)
        self.trips += 1
        logger.info("[repeat] %s called %d times with identical args", outcome.tool, count)
        outcome.rendered = (outcome.rendered or "") + self._hint(outcome.tool, count)
        outcome.metadata["repeat_blocked"] = True
        return next(outcome)

    # -- lifecycle ---------------------------------------------

    def reset(self, *_args: Any) -> None:
        self.ledger.clear()
        self.hinted.clear()

    # -- helpers ----------------------------------------------

    @staticmethod
    def _key(invoke: Any) -> str | None:
        try:
            payload = json.dumps(
                invoke.arguments or {}, sort_keys=True, ensure_ascii=False, default=str
            )
        except (TypeError, ValueError):
            return None
        return f"{invoke.tool}\x00{payload}"

    def _hint(self, tool: str, count: int) -> str:
        return (
            f"\n\n⚠ 你已用**完全相同**的参数调用 `{tool}` {count} 次，"
            f"返回值不会变化。不要再这样调用。\n"
            f"请从下面选一个动作继续：\n"
            f"1. 换参数 —— 读不同的文件 / 搜索不同的关键词；\n"
            f"2. 换工具 —— 例如用 `grep` 定位而不要反复 `read_file` 整个文件；\n"
            f"3. 或者停止探索，直接用上面已有的结果开始修改代码。"
        )


def apply(ctx: Context, config: dict[str, Any]) -> None:
    guard = RepeatGuard(config)
    ctx.provide("repeat_guard", guard)
    ctx.on(events.TOOLS_PRE_EXECUTE, guard.count)
    ctx.on(events.TOOLS_POST_EXECUTE, guard.annotate)
    ctx.on(events.TURN_START, guard.reset)
    logger.info("[repeat] identical-call guard active (limit=%d)", guard.limit)
