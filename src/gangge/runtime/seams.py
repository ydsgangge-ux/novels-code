"""Capability seams — the interfaces plugins agree on, and their service keys.

A **seam** is the boundary where an implementation may be swapped. Each seam is
declared once, implemented by one or more plugins, and consumed by others
through ``ctx.<key>``. Three participants, never two:

    declaration (this module)  →  implementation (a plugin)  →  consumer (the loop)

That is what lets ``storage`` be SQLite today and JSONL tomorrow without the
agent loop changing a line. Keeping the declaration separate from the
implementation is the whole point — do not import a concrete class across a
seam boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Protocol, Sequence, runtime_checkable

# ── Service keys ────────────────────────────────────────────────
# Stable, lowercase, one word. These are the ``ctx.<key>`` names.

LLM = "llm"
TOOLS = "tools"
PERMISSION = "permission"
SESSIONS = "sessions"
STORAGE = "storage"
CREDENTIALS = "credentials"
TELEMETRY = "telemetry"
MEMORY = "memory"
SANDBOX = "sandbox"

# Every key a well-formed runtime should expose. Missing ones are reported by
# ``Runtime.missing()`` so a broken bundle list fails loudly instead of
# surfacing as an AttributeError deep inside the loop.
CORE_SERVICES: tuple[str, ...] = (LLM, TOOLS, PERMISSION)


# ── LLM seam ────────────────────────────────────────────────────


@runtime_checkable
class LLMFactory(Protocol):
    """Creates model adapters. Implementations must not require a key at import."""

    def create(self, provider: str | None = None, model: str | None = None) -> Any:
        """Build a fresh adapter bound to ``provider``/``model``."""

    def default(self) -> Any:
        """Return the cached default adapter, creating it on first use."""

    def close(self) -> None:
        """Release every adapter this factory created."""


# ── Tool seam ───────────────────────────────────────────────────


@dataclass
class ToolInvocation:
    """The shared, mutable decision object passed through the tool pipeline.

    Listeners of ``tools/pre-execute`` cooperative-edit this object and then
    delegate with ``next()``. Setting ``vetoed`` short-circuits the pipeline and
    the call never reaches the tool.
    """

    tool: str
    arguments: dict[str, Any]
    call_id: str = ""
    vetoed: bool = False
    veto_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def veto(self, reason: str) -> "ToolInvocation":
        """Reject the call; the loop turns this into an error tool result."""
        self.vetoed = True
        self.veto_reason = reason
        return self


@dataclass
class ToolOutcome:
    """The shared result object passed through ``tools/post-execute``.

    ``rendered`` is what the model sees; ``value`` is the canonical value kept
    for programmatic callers. Truncating ``rendered`` must never destroy
    ``value`` — that separation is what makes big outputs safe to prune.
    """

    tool: str
    call_id: str = ""
    rendered: str = ""
    is_error: bool = False
    value: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TurnDirective:
    """A verdict on ``agent/turn-stopping``: keep going, or accept the stop.

    Returning ``None`` means "no opinion" — the next listener, or the loop's own
    heuristics, decides. Only a ``continue`` needs a message, because that is
    the text injected as the next user turn.
    """

    action: str                 # "continue" | "stop"
    message: str = ""
    reason: str = ""

    @classmethod
    def keep_going(cls, message: str, reason: str = "") -> "TurnDirective":
        return cls(action="continue", message=message, reason=reason)

    @classmethod
    def accept_stop(cls, reason: str = "") -> "TurnDirective":
        return cls(action="stop", reason=reason)


@runtime_checkable
class ToolSeam(Protocol):
    """Scoped tool registry with a gated execution pipeline."""

    def register(self, tool: Any) -> None: ...

    def unregister(self, name: str) -> None: ...

    def get(self, name: str) -> Any: ...

    def list_tools(self) -> Sequence[Any]: ...

    def definitions(self, **scope: Any) -> Sequence[Any]: ...

    async def execute(self, name: str, arguments: dict[str, Any]) -> Any: ...


# ── Storage seam ────────────────────────────────────────────────


@runtime_checkable
class StorageSeam(Protocol):
    """Non-session key/value storage. Sessions have their own seam."""

    async def get(self, key: str, default: Any = None) -> Any: ...

    async def set(self, key: str, value: Any) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def keys(self, prefix: str = "") -> list[str]: ...


# ── Credentials seam ────────────────────────────────────────────


@runtime_checkable
class CredentialsSeam(Protocol):
    """Where API keys come from: env, a file, or the OS keyring."""

    def get(self, name: str, default: str = "") -> str: ...

    def has(self, name: str) -> bool: ...


# ── Telemetry seam ──────────────────────────────────────────────


@runtime_checkable
class TelemetrySeam(Protocol):
    """Fire-and-forget event sink. Never raises into the caller."""

    def record(self, name: str, **fields: Any) -> None: ...


# ── Session seam ────────────────────────────────────────────────


@runtime_checkable
class SessionSeam(Protocol):
    """Session lifecycle: create, load, append, persist."""

    async def ensure(self) -> None: ...

    async def new(self, title: str = "") -> Any: ...

    async def load(self, session_id: str) -> Any: ...

    async def close(self) -> None: ...


# ── Sandbox seam ────────────────────────────────────────────────


@runtime_checkable
class SandboxSeam(Protocol):
    """Isolation boundary for command execution.

    gangge currently ships only the soft guard (rules + danger detection); a
    sandbox implementation is where process/container isolation would land.
    """

    def allows(self, path: str, mode: str = "write") -> bool: ...

    async def run(self, command: str) -> AsyncIterator[str]: ...


# ── Seam catalogue (for docs / dump-config) ─────────────────────


@dataclass(frozen=True)
class SeamSpec:
    key: str
    interface: str
    summary: str
    builtin: tuple[str, ...] = ()
    accepted: tuple[str, ...] = ()


SEAMS: tuple[SeamSpec, ...] = (
    SeamSpec(LLM, "LLMFactory", "模型适配器工厂", ("gangge.runtime.plugins.llm",),
             ("anthropic", "openai-compat", "ollama")),
    SeamSpec(TOOLS, "ToolSeam", "工具注册表与执行流水线", ("gangge.runtime.plugins.tools",)),
    SeamSpec(PERMISSION, "PermissionGuard", "工具调用审批策略", ("gangge.runtime.plugins.permission",)),
    SeamSpec(SESSIONS, "SessionSeam", "会话生命周期", ("gangge.runtime.plugins.session",)),
    SeamSpec(STORAGE, "StorageSeam", "非会话键值存储", ()),
    SeamSpec(CREDENTIALS, "CredentialsSeam", "密钥来源", ()),
    SeamSpec(TELEMETRY, "TelemetrySeam", "遥测记录出口", ("gangge.runtime.plugins.telemetry",)),
    SeamSpec(MEMORY, "MemoryBank", "跨会话记忆库", ("gangge.runtime.plugins.memory_bank",)),
    SeamSpec(SANDBOX, "SandboxSeam", "命令执行隔离（尚未实现）", ()),
)


def seam_of(key: str) -> SeamSpec | None:
    for spec in SEAMS:
        if spec.key == key:
            return spec
    return None


# Runtime-checkable Protocols only verify attribute presence, which is exactly
# the cheap safety net wanted at mount time.
PROTOCOLS: dict[str, type] = {
    "LLMFactory": LLMFactory,
    "ToolSeam": ToolSeam,
    "StorageSeam": StorageSeam,
    "CredentialsSeam": CredentialsSeam,
    "TelemetrySeam": TelemetrySeam,
    "SessionSeam": SessionSeam,
    "SandboxSeam": SandboxSeam,
}


def check_implementation(key: str, value: Any) -> list[str]:
    """Return the protocol members ``value`` fails to provide."""
    spec = seam_of(key)
    if spec is None:
        return []
    protocol = PROTOCOLS.get(spec.interface)
    if protocol is None:
        return []
    return [
        name
        for name in dir(protocol)
        if not name.startswith("_") and not hasattr(value, name)
    ]


# Type aliases used by plugins that accept a callback.
AskCallback = Callable[[str], Any]
StreamCallback = Callable[..., Any]
