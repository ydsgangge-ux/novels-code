"""Permission seam implementation — ``ctx.permission``.

The guard stays exactly as it is (rules → danger detection → memory → callback).
What changes is *where it is called from*: it is now a listener on
``permission/check``, so a profile can substitute a different policy without
editing the tool pipeline.

``strict`` swaps in a conservative rule set; ``ask: false`` makes the check
answer ALLOW without consulting the user, which is what a non-interactive run
needs.
"""

from __future__ import annotations

import logging
from typing import Any

from gangge.runtime import events
from gangge.runtime.context import Context, Inject

logger = logging.getLogger(__name__)

name = "permission"
inject = Inject(optional=("telemetry",))

# Commands a strict profile refuses outright. Matched case-insensitively as
# substrings — coarse on purpose, because false negatives here are worse than
# false positives.
STRICT_DENY = (
    "rm -rf",
    "rm -r /",
    "del /f /s /q",
    "format ",
    "mkfs",
    "> /dev/sda",
    "git push --force",
    "git reset --hard",
    "pip install --user",
    "npm publish",
    "chmod 777",
    "curl | sh",
    "curl | bash",
    "iwr | iex",
)


def apply(ctx: Context, config: dict[str, Any]) -> None:
    from gangge.layer4_permission.guard import PermissionDecision, PermissionGuard
    from gangge.layer4_permission.rules import PermissionRule, RuleAction

    ask = bool(config.get("ask", True))
    guard = PermissionGuard(
        ask_callback=config.get("ask_callback"),
        policy_path=config.get("policy_path") or None,
    )
    if guard.policy_error:
        logger.error("[permission] 自定义策略不可用，已降级 fail-closed: %s", guard.policy_error)

    if config.get("strict"):
        for pattern in STRICT_DENY:
            guard.add_rule(PermissionRule(pattern=pattern, action=RuleAction.DENY))
        logger.info("[permission] strict mode — %d deny rules", len(STRICT_DENY))

    ctx.provide("permission", guard)

    # The agent loop already calls ``guard.check()`` before it dispatches a tool,
    # so subscribing to ``permission/check`` as well would ask the user twice.
    # Hosts that hand the pipeline a bare registry (no loop-side check) set
    # ``in_pipeline: true``.
    if not config.get("in_pipeline", False):
        logger.debug("[permission] pipeline listener off — the loop checks directly")
        return

    if not ask:
        ctx.on(events.PERMISSION_CHECK, lambda *_: PermissionDecision.ALLOW)
        logger.info("[permission] non-interactive — every call allowed")
        return

    def check(tool_name: str, action: str, invoke: Any = None) -> Any:
        """Async verdict forwarded to the guard; awaited by ``ctx.serial``."""
        scope = getattr(invoke, "metadata", None) or {}
        return guard.check(tool_name, action, dict(scope))

    ctx.on(events.PERMISSION_CHECK, check)
