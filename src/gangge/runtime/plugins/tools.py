"""Tool seam implementation — ``ctx.tools``.

Wraps the existing ``ToolRegistry`` in the documented execution pipeline so
policy can be attached without touching the loop::

    tool/call  →  tools/pre-execute  →  permission/check
               →  tools/execute      →  tools/post-execute  →  tool/result

``tools/pre-execute`` is a waterfall over a shared ``ToolInvocation``: a
listener mutates it and calls ``next()``, or calls ``invoke.veto(reason)`` and
returns to short-circuit. Vetoes become ordinary error results — a rejected
call never leaves an orphaned ``tool_use`` in the history.
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from gangge.layer3_agent.tools.base import ToolResult
from gangge.layer3_agent.tools.registry import ToolRegistry, create_tool_registry
from gangge.runtime import events
from gangge.runtime.context import Context, Inject
from gangge.runtime.seams import ToolInvocation, ToolOutcome

logger = logging.getLogger(__name__)

name = "tools"
inject = Inject(optional=("llm", "permission", "telemetry"))


class _LazyLLM:
    """Forwards attribute access to ``ctx.llm.default()`` on first use.

    Tools that take an ``llm`` (the novel pipeline) need one, but resolving it
    at mount time would require an API key just to boot. The proxy defers that
    until a tool actually calls a method.
    """

    def __init__(self, ctx: Context) -> None:
        self._ctx = ctx
        self._target: Any = None

    def _resolve(self) -> Any:
        if self._target is None:
            factory = self._ctx.get("llm")
            if factory is None:
                raise RuntimeError("no 'llm' service is mounted")
            self._target = factory.default()
        return self._target

    def __getattr__(self, item: str) -> Any:
        return getattr(self._resolve(), item)


class ToolService:
    """Execution pipeline around a ``ToolRegistry``."""

    def __init__(self, ctx: Context, registry: ToolRegistry, config: dict[str, Any]) -> None:
        self.ctx = ctx
        self.registry = registry
        self.config = config

    # -- registry passthrough ----------------------------------

    def register(self, tool: Any) -> None:
        self.registry.register(tool)

    def unregister(self, tool_name: str) -> None:
        self.registry.unregister(tool_name)

    def get(self, tool_name: str) -> Any:
        return self.registry.get(tool_name)

    def list_tools(self) -> Sequence[Any]:
        return self.registry.list_tools()

    def definitions(self, **scope: Any) -> Sequence[Any]:
        return self.registry.get_definitions(
            phase=scope.get("phase"), profile=scope.get("profile")
        )

    @property
    def profile(self) -> str:
        return self.registry.get_profile()

    def set_profile(self, profile: str) -> None:
        self.registry.set_profile(profile)

    # No __len__ on purpose: a service that is falsy when empty invites
    # `service or fallback`, which silently discards the real registry.
    # Use `len(service.registry)` when a count is needed.

    def __contains__(self, tool_name: str) -> bool:
        return tool_name in self.registry

    # -- pipeline ----------------------------------------------

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        call_id: str = "",
    ) -> ToolResult:
        """Run one tool call through the gated pipeline."""
        invoke = ToolInvocation(
            tool=tool_name,
            arguments=dict(arguments or {}),
            call_id=call_id,
        )
        self.ctx.emit(events.TOOL_CALL, invoke)

        invoke = self.ctx.waterfall(events.TOOLS_PRE_EXECUTE, invoke)
        if invoke.vetoed:
            logger.info("[tools] vetoed %s — %s", tool_name, invoke.veto_reason)
            # The veto reason *is* the message the model sees: a policy that
            # blocks a call must say what to do instead, so it is passed through
            # verbatim rather than wrapped in boilerplate.
            return self._finish(
                ToolOutcome(
                    tool=tool_name,
                    call_id=call_id,
                    rendered=invoke.veto_reason or f"工具调用未执行: {tool_name}",
                    is_error=True,
                    metadata=dict(invoke.metadata),
                )
            )

        verdict = await self.ctx.serial(
            events.PERMISSION_CHECK, invoke.tool, self._action_of(invoke), invoke
        )
        blocked = _blocked_reason(verdict)
        if blocked is not None:
            return self._finish(
                ToolOutcome(
                    tool=tool_name,
                    call_id=call_id,
                    rendered=f"工具调用未被允许: {blocked}",
                    is_error=True,
                )
            )

        result = await self.registry.execute(invoke.tool, invoke.arguments)
        # Pre-execute notes (schema repairs, repeat counts) ride along with the
        # result so post-execute listeners can react to what happened upstream.
        outcome = ToolOutcome(
            tool=invoke.tool,
            call_id=call_id,
            rendered=result.output,
            is_error=result.is_error,
            value=result.metadata.get("value", result.output),
            metadata={**invoke.metadata, **result.metadata},
        )
        outcome = self.ctx.waterfall(events.TOOLS_EXECUTE, outcome)
        return self._finish(self.ctx.waterfall(events.TOOLS_POST_EXECUTE, outcome))

    def _finish(self, outcome: ToolOutcome) -> ToolResult:
        result = ToolResult(
            output=outcome.rendered,
            is_error=outcome.is_error,
            metadata=dict(outcome.metadata),
        )
        if outcome.value is not None:
            result.metadata.setdefault("value", outcome.value)
        self.ctx.emit(events.TOOL_RESULT, result)
        return result

    @staticmethod
    def _action_of(invoke: ToolInvocation) -> str:
        """The string a permission rule matches against."""
        for key in ("command", "path", "url", "file_path", "pattern"):
            value = invoke.arguments.get(key)
            if isinstance(value, str):
                return value
        return invoke.tool


def _blocked_reason(verdict: Any) -> str | None:
    """Interpret whatever a ``permission/check`` listener returned."""
    if verdict is None:
        return None
    if isinstance(verdict, str):
        lowered = verdict.lower()
        if lowered in ("allow", "allowed", "auto", "ok"):
            return None
        return verdict
    decision = getattr(verdict, "decision", None)
    if decision is None:
        return None
    raw = getattr(decision, "value", decision)
    if str(raw).lower() == "allow":
        return None
    return str(getattr(verdict, "reason", raw))


def apply(ctx: Context, config: dict[str, Any]) -> None:
    workspace = config.get("workspace") or ctx.data.get("workspace") or ""
    ask_callback = config.get("ask_user_callback")

    lazy_llm = _LazyLLM(ctx) if config.get("novel", True) else None
    registry = create_tool_registry(
        workspace=str(workspace),
        ask_user_callback=ask_callback,
        load_plugins=bool(config.get("load_plugins", True)),
        llm=lazy_llm,
        multimodal_llm=config.get("multimodal_llm"),
        attachments=config.get("attachments"),
    )

    for tool_name in config.get("exclude") or []:
        registry.unregister(str(tool_name))
        logger.info("[tools] excluded by profile: %s", tool_name)

    if config.get("profile"):
        registry.set_profile(str(config["profile"]))

    service = ToolService(ctx, registry, config)
    ctx.provide("tools", service)
    ctx.data["tool_registry"] = registry

    if config.get("browser") is False:
        registry.unregister("browser")
        registry.unregister("browser_debug")
    if config.get("comfyui") is False:
        registry.unregister("generate_image")
    if config.get("novel") is False:
        for tool_name in [t.name for t in registry.list_tools() if t.name.startswith("novel_")]:
            registry.unregister(tool_name)

    logger.info("[tools] %d tools registered", len(registry))
