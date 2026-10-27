"""Lifecycle event catalogue — the extension points of the runtime.

Every event declares exactly one dispatch mode, and may only be distributed
through the matching ``Context`` method. This mirrors DeepSeek Harness, where
"分发模式是事件公开约定的一部分" — the mode is part of the contract, so a
policy listener knows whether it must delegate with ``next()`` or may return a
verdict, and whether the emitter awaits it.

Three domains, chosen before anything else when adding behaviour:

* **Session** (``turn/*``, ``step/*``, ``*/message``, ``tool/*``) — durable
  facts. Emit these when the fact must survive a restart.
* **Agent** (``agent/*``) — live interception of in-flight work.
* **Capability** (``tools/*``, ``llm/*``, ``fs/*``, ``telemetry/*``) — attach
  policy or adapters to a seam without importing the loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Dispatch modes — must stay in sync with the Context methods.
EMIT = "emit"             # sync, observed, no result
WATERFALL = "waterfall"   # sync, around-middleware, listener must call next()
PARALLEL = "parallel"     # awaited, fanned out
SERIAL = "serial"         # awaited, in order, returns last non-None
BAIL = "bail"             # sync, stops at the first non-None return


@dataclass(frozen=True)
class EventSpec:
    """An event's public contract."""

    name: str
    mode: str
    domain: str           # "session" | "agent" | "capability"
    summary: str


def _spec(name: str, mode: str, domain: str, summary: str) -> tuple[str, EventSpec]:
    return name, EventSpec(name=name, mode=mode, domain=domain, summary=summary)


# ── Session domain: durable facts ───────────────────────────────
_SESSION = [
    _spec("session/created", EMIT, "session", "A session entered the store"),
    _spec("session/title", EMIT, "session", "Session title changed"),
    # A turn opens before the first input is claimed and closes when no work
    # is owed any more. A step is one model request plus the tools it called.
    _spec("turn/start", EMIT, "session", "A turn opened"),
    _spec("turn/end", EMIT, "session", "A turn closed; carries the outcome"),
    _spec("step/start", EMIT, "session", "A model request is about to be assembled"),
    _spec("step/end", EMIT, "session", "A model request and its tools finished"),
    _spec("user/message", EMIT, "session", "A user message was admitted"),
    _spec("assistant/message", EMIT, "session", "A committed assistant message"),
    _spec("tool/call", EMIT, "session", "The model requested a tool call"),
    _spec("tool/result", EMIT, "session", "A tool call produced its result"),
    _spec("turn/aborted", EMIT, "session", "A turn was cancelled; recovery applies"),
    _spec("turn/repaired", EMIT, "session", "Interrupted tool calls were closed with synthetic results"),
]

# ── Agent domain: live interception ─────────────────────────────
_AGENT = [
    _spec("agent/pre-step", WATERFALL, "agent", "Admit, rewrite or reject claimed input"),
    _spec("agent/request", WATERFALL, "agent", "Adjust request controls before binding"),
    _spec("agent/turn-stopping", SERIAL, "agent", "Final say on closing a turn"),
]

# ── Capability domain: policy and adapters on a seam ────────────
_CAPABILITY = [
    _spec("llm/request", WATERFALL, "capability", "Rewrite an outbound LLM request"),
    _spec("llm/stream", EMIT, "capability", "Observe streamed chunks"),
    _spec("llm/response", EMIT, "capability", "Observe a completed LLM response"),
    _spec("tools/pre-execute", WATERFALL, "capability", "Veto or rewrite a tool call"),
    _spec("tools/execute", WATERFALL, "capability", "Wrap tool execution"),
    _spec("tools/post-execute", WATERFALL, "capability", "Rewrite a tool result"),
    # Serial rather than bail: the verdict comes from PermissionGuard.check(),
    # which is async because ASK may await the user.
    _spec("permission/check", SERIAL, "capability", "Verdict on a tool call"),
    _spec("fs/write", BAIL, "capability", "Intercept a file write; skip the default"),
    _spec("telemetry/record", EMIT, "capability", "Fan out a telemetry record"),
    _spec("memory/turn", EMIT, "capability", "A turn's facts for the memory bank"),
]


EVENTS: dict[str, EventSpec] = dict([*_SESSION, *_AGENT, *_CAPABILITY])


# ── Convenience constants ──────────────────────────────────────
# Import these instead of writing literals: a typo in an event name is a
# silently dead listener, which is the worst kind of bug to debug.

SESSION_CREATED = "session/created"
SESSION_TITLE = "session/title"

TURN_START = "turn/start"
TURN_END = "turn/end"
TURN_ABORTED = "turn/aborted"
STEP_START = "step/start"
STEP_END = "step/end"
USER_MESSAGE = "user/message"
ASSISTANT_MESSAGE = "assistant/message"
TOOL_CALL = "tool/call"
TOOL_RESULT = "tool/result"

AGENT_PRE_STEP = "agent/pre-step"
AGENT_REQUEST = "agent/request"
AGENT_TURN_STOPPING = "agent/turn-stopping"

LLM_REQUEST = "llm/request"
LLM_STREAM = "llm/stream"
LLM_RESPONSE = "llm/response"
TOOLS_PRE_EXECUTE = "tools/pre-execute"
TOOLS_EXECUTE = "tools/execute"
TOOLS_POST_EXECUTE = "tools/post-execute"
PERMISSION_CHECK = "permission/check"
FS_WRITE = "fs/write"
TELEMETRY_RECORD = "telemetry/record"
MEMORY_TURN = "memory/turn"


def mode_of(event: str) -> str:
    """Dispatch mode declared for ``event``; unknown events default to emit."""
    spec = EVENTS.get(event)
    return spec.mode if spec else EMIT


def domain_of(event: str) -> str:
    """Domain declared for ``event``; unknown events default to capability."""
    spec = EVENTS.get(event)
    return spec.domain if spec else "capability"


def dispatch(ctx: Any, event: str, *args: Any) -> Any:
    """Distribute an event through the method its declared mode requires.

    Using this instead of calling ``ctx.emit``/``ctx.waterfall`` directly keeps
    the declaration and the call site from drifting apart.
    """
    mode = mode_of(event)
    if mode == WATERFALL:
        return ctx.waterfall(event, *args)
    if mode == BAIL:
        return ctx.bail(event, *args)
    if mode == SERIAL:
        return ctx.serial(event, *args)
    if mode == PARALLEL:
        return ctx.parallel(event, *args)
    return ctx.emit(event, *args)


def catalogue() -> list[dict[str, str]]:
    """Machine-readable event table, for docs and ``--dump-config``."""
    return [
        {
            "event": spec.name,
            "mode": spec.mode,
            "domain": spec.domain,
            "summary": spec.summary,
        }
        for spec in EVENTS.values()
    ]
