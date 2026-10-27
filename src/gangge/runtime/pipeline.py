"""Tool pipeline without a profile — for hosts that already own their objects.

The CLI and TUI boot a full profile (``bootstrap``). The PyQt desktop worker
does not: it builds its registry by hand and only wants the guardrails. Making
it boot a whole tree would duplicate services it already holds, so this module
gives it exactly the pipeline and nothing else.

    from gangge.runtime.pipeline import tool_pipeline

    service, ctx = tool_pipeline(registry, workspace=ws)
    loop = AgenticLoop(..., tool_service=service)
    ...
    ctx.dispose()          # or await ctx.aclose()

Deliberately *not* a plugin: it is called with a registry someone else owns, so
there is no bundle to describe it. Everything it assembles is still a plugin,
just mounted directly instead of through a patch document.
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any

from gangge.runtime.context import Context
from gangge.runtime.plugins.tools import ToolService

logger = logging.getLogger(__name__)


def tool_pipeline(
    registry: Any,
    *,
    context: Context | None = None,
    schema: bool = True,
    prune: bool = True,
    repeat: bool = True,
    next_step: bool = True,
    stop_guard: bool = True,
    schema_config: dict[str, Any] | None = None,
    prune_config: dict[str, Any] | None = None,
    repeat_config: dict[str, Any] | None = None,
    next_step_config: dict[str, Any] | None = None,
    stop_guard_config: dict[str, Any] | None = None,
    max_chars: int = 8000,
    repeat_limit: int = 3,
    workspace: str | Path | None = None,
) -> tuple[ToolService, Context]:
    """Wrap ``registry`` in the execution pipeline and attach the guardrails.

    Returns ``(service, ctx)``. Pass ``ctx`` to ``AgenticLoop(context=...)`` so
    ``agent/turn-stopping`` reaches the stop guard, and dispose it when the host
    is finished.

    ``workspace`` is published on ``ctx.data`` (and in the tool service config)
    so plugins mounted later can resolve paths the same way the loop does.
    """
    ctx = context or Context("tool-pipeline")
    if workspace is not None:
        ctx.data["workspace"] = str(Path(workspace).resolve())

    service = ToolService(ctx, registry, {"workspace": ctx.data.get("workspace", "")})

    if not ctx.has("tools"):
        ctx.provide("tools", service)

    if schema:
        _attach(ctx, "gangge.runtime.plugins.schema_guard",
                {"coerce": True, "prepend": True, **(schema_config or {})})
    if prune:
        _attach(ctx, "gangge.runtime.plugins.result_pruner",
                {"max_chars": max_chars, "head_ratio": 0.4, "prepend": True,
                 **(prune_config or {})})
    if repeat:
        _attach(ctx, "gangge.runtime.plugins.repeat_guard",
                {"limit": repeat_limit, **(repeat_config or {})})
    if next_step:
        _attach(ctx, "gangge.runtime.plugins.next_step", next_step_config or {})
    if stop_guard:
        _attach(ctx, "gangge.runtime.plugins.stop_guard", stop_guard_config or {})

    logger.info(
        "[pipeline] guardrails attached (schema=%s prune=%s repeat=%s next_step=%s stop=%s)",
        schema, prune, repeat, next_step, stop_guard,
    )
    return service, ctx


def attach_llm(ctx: Context, provider: str = "", **config: Any) -> Any:
    """Mount the LLM factory on an already-built context.

    Hosts that do not boot a profile (the PyQt worker creates its own adapter)
    still need ``llm/request`` to fire, so the factory is attached standalone
    and the caller passes it to ``AgenticLoop(llm_factory=...)``.
    """
    _attach(ctx, "gangge.runtime.plugins.llm", {"provider": provider, **config})
    return ctx.get("llm")


def _attach(ctx: Context, module_path: str, config: dict[str, Any]) -> None:
    """Apply a guardrail plugin directly on ``ctx``.

    Mounting it as a child would scope its listeners to the child, where the
    service's own ``ctx.waterfall`` cannot see them. Calling ``apply`` on the
    shared context is what a bundle does during ``bootstrap`` anyway.
    """
    try:
        module = importlib.import_module(module_path)
        module.apply(ctx, config)
    except Exception:
        logger.warning("[pipeline] could not attach %s", module_path, exc_info=True)
