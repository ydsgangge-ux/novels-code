"""Permission guard — intercept and control tool executions.

Evaluation order, and why:

1. **Remembered decisions** — an explicit "always allow this" from the user is
   the strongest signal there is, so it wins outright.
2. **Policy** (``rules.py``) — declarative allow / ask / deny, strictest match
   wins. Shell commands are *split* into segments first, because
   ``ls; rm -rf /`` must not be allowed by a rule that only looked at ``ls``.
3. **Danger detector** — a backstop for patterns no rule anticipated. CRITICAL
   is denied even if the policy stayed silent.
4. **Callback** — only for ASK. A guard with no callback is fail-closed: ASK
   stays ASK instead of silently becoming ALLOW.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable

from gangge.layer4_permission.danger import DangerDetector, RiskAssessment, RiskLevel
from gangge.layer4_permission.rules import (
    SEVERITY,
    PermissionPolicy,
    PermissionRule,
    RuleAction,
    RuleVerdict,
    scope_for_tool,
)

logger = logging.getLogger(__name__)


class PermissionDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK_USER = "ask_user"


@dataclass
class PermissionRequest:
    """A request for permission."""

    tool_name: str
    action: str           # e.g. command string, file path
    risk: RiskAssessment
    context: dict[str, Any] = field(default_factory=dict)
    scope: str = "bash"
    reason: str = ""


@dataclass
class PermissionResult:
    """Result of a permission check."""

    decision: PermissionDecision
    reason: str
    risk: RiskAssessment
    scope: str = "bash"
    rule: PermissionRule | None = None

    @property
    def allowed(self) -> bool:
        return self.decision == PermissionDecision.ALLOW


# Type alias for async callback
PermissionCallback = Callable[[PermissionRequest], Awaitable[PermissionDecision]]


_ACTION_TO_DECISION = {
    RuleAction.ALLOW: PermissionDecision.ALLOW,
    RuleAction.ASK: PermissionDecision.ASK_USER,
    RuleAction.DENY: PermissionDecision.DENY,
}

# Segments of a compound shell command. Catches `;`, `&&`, `||`, `|`, newlines,
# and command substitution — every place a second command can hide.
_SEGMENT_SPLIT = re.compile(r"[\n;]|&&|\|\||(?<!\|)\|(?!\|)")
_SUBSTITUTION = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")


def split_command(command: str) -> list[str]:
    """Split a shell line into the commands it actually runs.

    Best-effort and deliberately conservative: a false split only costs an extra
    confirmation, while a missed split could auto-allow a destructive second
    command.
    """
    if not command:
        return []
    segments: list[str] = []
    for part in _SEGMENT_SPLIT.split(command):
        if part and part.strip():
            segments.append(part.strip())
    for match in _SUBSTITUTION.finditer(command):
        inner = match.group(1) or match.group(2) or ""
        for part in _SEGMENT_SPLIT.split(inner):
            if part and part.strip():
                segments.append(part.strip())
    return segments or [command.strip()]


def _split_paths(action: str) -> list[str]:
    """Split a multi-path action (e.g. a multi-file patch) into segments."""
    if not action:
        return [""]
    parts = [part.strip() for part in action.split(";") if part.strip()]
    return parts or [action.strip()]


class PermissionGuard:
    """Central permission controller.

    ``guard.check()`` is the single entry point. It returns a
    ``PermissionResult`` and never raises for policy problems — a policy that
    fails to load degrades to the fail-closed fallback and logs loudly.
    """

    def __init__(
        self,
        ask_callback: PermissionCallback | None = None,
        policy: PermissionPolicy | None = None,
        policy_path: str | Path | None = None,
        remember: bool = True,
    ):
        self.danger_detector = DangerDetector()
        self._ask_callback = ask_callback
        self.remember = remember
        self.memory: list[tuple[str, str, PermissionDecision]] = []  # (scope, pattern, decision)
        self.policy, self.policy_error = self._load_policy(policy, policy_path)

    # ── policy plumbing ────────────────────────────────────────

    @staticmethod
    def _load_policy(
        policy: PermissionPolicy | None, policy_path: str | Path | None
    ) -> tuple[PermissionPolicy, str]:
        if policy is not None:
            return policy, ""
        try:
            if policy_path:
                return PermissionPolicy.from_yaml(policy_path), ""
            return PermissionPolicy.default(), ""
        except Exception as exc:  # PolicyError, YAML/IO errors
            logger.error("[permission] 策略加载失败，降级为 fail-closed: %s", exc)
            return PermissionPolicy.fallback(), str(exc)

    @property
    def rules(self) -> list[PermissionRule]:
        """Backwards-compatible view of the policy's rule list."""
        return self.policy.rules

    def set_ask_callback(self, callback: PermissionCallback) -> None:
        """Set the callback for asking user permission."""
        self._ask_callback = callback

    def add_rule(self, rule: PermissionRule) -> None:
        """Add a rule (validated; order is irrelevant under strictest-wins)."""
        self.policy.add(rule)

    def remove_rule(self, pattern: str) -> None:
        self.policy.rules = [r for r in self.policy.rules if r.pattern != pattern]

    def remember_decision(self, scope: str, pattern: str, decision: PermissionDecision) -> None:
        """Remember a user's permission decision."""
        self.memory.insert(0, (scope, pattern, decision))

    def _check_memory(self, scope: str, action: str) -> PermissionDecision | None:
        for mem_scope, mem_pattern, mem_decision in self.memory:
            if mem_scope != scope:
                continue
            try:
                if re.search(mem_pattern, action, re.IGNORECASE):
                    return mem_decision
            except re.error:
                continue
        return None

    # ── evaluation ─────────────────────────────────────────────

    def _evaluate_scope(self, scope: str, action: str) -> RuleVerdict:
        """Strictest verdict across every segment of a compound action.

        Shell commands are split because `ls; rm -rf /` must not inherit the
        `ls` verdict. Multi-file writes are split for the same reason: a patch
        that touches both `src/app.py` and `C:\\Windows\\...` must be judged on
        the worst path, not the first one.
        """
        if scope not in ("bash", "file_write"):
            return self.policy.evaluate(scope, action)

        segments = split_command(action) if scope == "bash" else _split_paths(action)
        strictest: RuleVerdict | None = None
        for segment in segments:
            verdict = self.policy.evaluate(scope, segment)
            if strictest is None or SEVERITY[verdict.decision] > SEVERITY[strictest.decision]:
                strictest = verdict
            if verdict.decision == RuleAction.DENY:
                break
        if strictest is None:  # pragma: no cover - split_command never empty
            return self.policy.evaluate(scope, action)
        if len(segments) > 1:
            return RuleVerdict(
                decision=strictest.decision,
                reason=f"{strictest.reason}（{len(segments)} 段中最严）",
                rule=strictest.rule,
                matched=strictest.matched,
            )
        return strictest

    async def check(
        self,
        tool_name: str,
        action: str,
        context: dict[str, Any] | None = None,
    ) -> PermissionResult:
        """Evaluate permission for a tool call."""
        ctx = context or {}
        scope = scope_for_tool(tool_name)

        # 1. Remembered decision
        mem_decision = self._check_memory(scope, action)
        if mem_decision is not None:
            return PermissionResult(
                decision=mem_decision,
                reason="记忆中的权限决定",
                risk=RiskAssessment(RiskLevel.SAFE, ""),
                scope=scope,
            )

        # 2. Policy
        verdict = self._evaluate_scope(scope, action)

        # 3. Risk assessment (always computed: the user needs it to decide)
        if scope == "bash":
            risk = self.danger_detector.assess_command(action)
        elif scope == "file_write":
            risk = self.danger_detector.assess_path(action, "write")
        elif scope == "file_read":
            risk = self.danger_detector.assess_path(action, "read")
        else:
            risk = RiskAssessment(RiskLevel.SAFE, "")

        if verdict.decision == RuleAction.DENY:
            return PermissionResult(
                decision=PermissionDecision.DENY,
                reason=verdict.reason,
                risk=risk,
                scope=scope,
                rule=verdict.rule,
            )

        if risk.level == RiskLevel.CRITICAL and verdict.decision != RuleAction.ALLOW:
            return PermissionResult(
                decision=PermissionDecision.DENY,
                reason=f"危险操作: {risk.reason}",
                risk=risk,
                scope=scope,
                rule=verdict.rule,
            )

        if verdict.decision == RuleAction.ALLOW:
            return PermissionResult(
                decision=PermissionDecision.ALLOW,
                reason=verdict.reason,
                risk=risk,
                scope=scope,
                rule=verdict.rule,
            )

        # 4. ASK — the default path for anything the policy does not vouch for
        request = PermissionRequest(
            tool_name=tool_name,
            action=action,
            risk=risk,
            context=ctx,
            scope=scope,
            reason=verdict.reason,
        )
        if self._ask_callback is None:
            return PermissionResult(
                decision=PermissionDecision.ASK_USER,
                reason=f"需要确认：{verdict.reason}",
                risk=risk,
                scope=scope,
                rule=verdict.rule,
            )

        user_decision = await self._ask_callback(request)
        if user_decision == PermissionDecision.ALLOW and self.remember:
            remember_pattern = ctx.get("remember_pattern")
            if remember_pattern:
                self.remember_decision(scope, str(remember_pattern), PermissionDecision.ALLOW)
        return PermissionResult(
            decision=user_decision,
            reason="用户决定",
            risk=risk,
            scope=scope,
            rule=verdict.rule,
        )
