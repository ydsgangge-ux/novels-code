"""LLM seam implementation — ``ctx.llm``.

The factory does not resolve credentials at mount time. A profile can be
inspected (``--dump-config``) on a machine with no API key; a missing key only
fails when a real adapter is asked for.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from gangge.runtime import events
from gangge.runtime.context import Context, Inject, MissingServiceError

logger = logging.getLogger(__name__)

name = "llm"
inject = Inject()


class LLMFactory:
    """Creates and caches model adapters for the configured provider."""

    def __init__(self, ctx: Context, config: dict[str, Any] | None = None) -> None:
        self.ctx = ctx
        self.config = config or {}
        self._default: Any = None
        self._adapters: list[Any] = []
        self._calls = 0
        self._latency_ms_total = 0.0

    # -- seam --------------------------------------------------

    def create(self, provider: str | None = None, model: str | None = None) -> Any:
        """Build a fresh adapter. Overrides fall back to the row's config."""
        from gangge.layer5_llm.registry import create_llm

        provider = provider or self.config.get("provider") or None
        adapter = create_llm(provider)
        if model:
            adapter.model = model
        self._adapters.append(adapter)
        logger.debug("[llm] created %s/%s", type(adapter).__name__, adapter.model)
        return adapter

    def default(self) -> Any:
        """The shared adapter, created on first use."""
        if self._default is None:
            self._default = self.create()
        return self._default

    async def close(self) -> None:
        for adapter in {id(a): a for a in self._adapters}.values():
            try:
                await adapter.close()
            except Exception:
                logger.debug("[llm] adapter close failed", exc_info=True)
        self._adapters.clear()
        self._default = None

    # -- convenience: the event-aware call path -----------------

    async def chat(
        self,
        messages: list[Any],
        tools: list[Any] | None = None,
        system: str = "",
        adapter: Any | None = None,
        on_delta: Any | None = None,
    ) -> Any:
        """Call the model through the ``llm/request`` waterfall.

        The waterfall receives a mutable request dict, so a policy listener can
        rewrite the system prompt, drop the tool list or re-route the model
        without importing the loop::

            def cheaper_for_summaries(request, next):
                if "压缩以下对话" in request["messages"][-1].get_text():
                    request["model"] = "glm-4.7-flash"
                return next(request)

            ctx.on("llm/request", cheaper_for_summaries)

        ``adapter`` lets the caller pin the exact instance it will bill against
        (the loop passes the adapter it was constructed with), so a factory
        default is never silently swapped in. ``model`` in the request dict is
        honoured for that one call and restored afterwards.

        ``on_delta`` switches the call to the streaming path: each text /
        thinking delta is forwarded as it arrives and the *assembled* response
        is still returned, so callers never have to choose between a live UI and
        a usable result. Adapters that cannot stream are called normally.
        """
        request: dict[str, Any] = {
            "messages": messages,
            "tools": tools,
            "system": system,
            "model": None,
        }
        result = self.ctx.waterfall(events.LLM_REQUEST, request)
        # A listener may return a new dict, mutate in place, or return nothing.
        if isinstance(result, dict):
            request = result

        target = adapter or self.default()
        call_messages = request.get("messages", messages)
        call_tools = request.get("tools", tools)
        call_system = request.get("system", system)

        requested_model = request.get("model")
        previous = getattr(target, "model", None)
        if requested_model and requested_model != previous:
            target.model = requested_model

        started = time.monotonic()
        try:
            if on_delta is not None and getattr(target, "supports_streaming", False):
                response = await target.chat_stream(
                    call_messages,
                    tools=call_tools,
                    system=call_system,
                    on_delta=on_delta,
                )
            else:
                response = await target.chat(
                    call_messages,
                    tools=call_tools,
                    system=call_system,
                )
        finally:
            # Scoped override only: the loop is sequential, so the window is
            # safe, and a shared adapter must never keep a routed model.
            if requested_model and requested_model != previous:
                target.model = previous

        self._calls += 1
        self._latency_ms_total += (time.monotonic() - started) * 1000
        self.ctx.emit(
            events.LLM_RESPONSE,
            response,
            {
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
                "model": requested_model or previous,
                "adapters": type(target).__name__,
            },
        )
        return response

    @property
    def stats(self) -> dict[str, Any]:
        """Cheap counters for telemetry and ``--dump-config``."""
        return {
            "calls": self._calls,
            "latency_ms_total": round(self._latency_ms_total, 1),
            "adapters_created": len(self._adapters),
        }


def apply(ctx: Context, config: dict[str, Any]) -> None:
    factory = LLMFactory(ctx, config)
    ctx.provide("llm", factory)
    ctx.effect(lambda: factory._adapters.clear())


__all__ = ["apply", "name", "inject", "LLMFactory", "MissingServiceError"]
