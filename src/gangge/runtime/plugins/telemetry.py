"""Telemetry seam implementation — ``ctx.telemetry``.

A profile that sets ``sink: ''`` pays nothing: the listeners are still there
but ``record`` returns immediately. That is why telemetry is a plugin and not a
``if enabled:`` sprinkled through the loop — flipping it is a YAML edit, and an
OSS build can drop the whole bundle.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gangge.runtime import events
from gangge.runtime.context import Context, Inject

logger = logging.getLogger(__name__)

name = "telemetry"
inject = Inject()

# Facts worth a line each. Deliberately excludes llm/stream (one record per
# chunk would dwarf every other signal).
WATCHED = (
    events.TURN_START,
    events.TURN_END,
    events.TURN_ABORTED,
    events.STEP_START,
    events.STEP_END,
    events.TOOL_CALL,
    events.TOOL_RESULT,
    events.LLM_RESPONSE,
    events.SESSION_CREATED,
)


class Telemetry:
    """Fire-and-forget recorder. Never raises into the caller."""

    def __init__(self, ctx: Context, config: dict[str, Any]) -> None:
        self.ctx = ctx
        self.config = config or {}
        self.sink = str(self.config.get("sink") or "").lower()
        self.path = Path(self.config.get("path") or ".gangge/telemetry.jsonl")
        self.records = 0
        if self.sink == "jsonl":
            self.path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def enabled(self) -> bool:
        return self.sink in ("jsonl", "file", "stderr")

    def record(self, name_: str, **fields: Any) -> None:
        self.records += 1
        if not self.enabled:
            return
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": name_,
            "mono": round(time.monotonic(), 3),
            **{k: _plain(v) for k, v in fields.items()},
        }
        try:
            if self.sink == "stderr":
                logger.debug("[telemetry] %s", payload)
                return
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            logger.debug("[telemetry] write failed", exc_info=True)

    def close(self) -> None:
        self.records = 0


def _plain(value: Any) -> Any:
    """Best-effort JSON-safe projection of an arbitrary argument."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value[:20]]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in list(value.items())[:20]}
    for attr in ("tool", "name", "model", "stop_reason"):
        if hasattr(value, attr):
            return {attr: _plain(getattr(value, attr))}
    return type(value).__name__


def apply(ctx: Context, config: dict[str, Any]) -> None:
    telemetry = Telemetry(ctx, config)
    ctx.provide("telemetry", telemetry)

    if not telemetry.enabled:
        logger.debug("[telemetry] sink=none — recording is a no-op")
        return

    def subscribe(event: str) -> None:
        def listener(*args: Any) -> None:
            telemetry.record(event, args=args)

        ctx.on(event, listener)

    for event in WATCHED:
        subscribe(event)

    ctx.on_cleanup(telemetry.close)
    logger.info("[telemetry] sink=%s → %s", telemetry.sink, telemetry.path)
