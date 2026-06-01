"""Permission guard — intercept and control tool executions."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Awaitable

from gangge.layer4_permission.danger import DangerDetector, RiskLevel, RiskAssessment
from gangge.layer4_permission.rules import PermissionRule, RuleAction, DEFAULT_RULES

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


@dataclass
class PermissionResult:
    """Result of a permission check."""

    decision: PermissionDecision
    reason: str
    risk: RiskAssessment


# Type alias for async callback
PermissionCallback = Callable[[PermissionRequest], Awaitable[PermissionDecision]]


class PermissionGuard:
    """Central permission controller.

    Evaluates each tool call through:
    1. User-defined rules (allow/deny patterns)
    2. Danger detection (risk assessment)
    3. Permission memory (remember past "always allow" choices)
    4. User callback (for ASK decisions)
    """

    def __init__(self, ask_callback: PermissionCallback | None = None):
        self.danger_detector = DangerDetector()
        self.rules: list[PermissionRule] = list(DEFAULT_RULES)
        self.memory: list[tuple[str, str, PermissionDecision]] = []  # (scope, pattern, decision)
        self._ask_callback = ask_callback

    def set_ask_callback(self, callback: PermissionCallback) -> None:
        """Set the callback for asking user permission."""
        self._ask_callback = callback

    def add_rule(self, rule: PermissionRule) -> None:
        """Add a permission rule."""
        self.rules.insert(0, rule)  # Higher priority first

    def remove_rule(self, pattern: str) -> None:
        """Remove rules matching the given pattern."""
        self.rules = [r for r in self.rules if r.pattern != pattern]

    def remember_decision(self, scope: str, pattern: str, decision: PermissionDecision) -> None:
        """Remember a user's permission decision."""
        self.memory.append((scope, pattern, decision))

    def _check_memory(self, scope: str, action: str) -> PermissionDecision | None:
        """Check if we have a remembered decision for this action."""
        for mem_scope, mem_pattern, mem_decision in self.memory:
            if mem_scope == scope:
                try:
                    import re
                    if re.search(mem_pattern, action, re.IGNORECASE):
                        return mem_decision
                except re.error:
                    continue
        return None

    def _check_rules(self, scope: str, action: str) -> PermissionDecision | None:
        """Check against user-defined rules."""
        for rule in self.rules:
            if rule.scope != scope:
                continue
            if rule.matches(action):
                if rule.action == RuleAction.ALLOW:
                    return PermissionDecision.ALLOW
                elif rule.action == RuleAction.DENY:
                    return PermissionDecision.DENY
                elif rule.action == RuleAction.ASK:
                    return PermissionDecision.ASK_USER
        return None

    async def check(
        self,
        tool_name: str,
        action: str,
        context: dict[str, Any] | None = None,
    ) -> PermissionResult:
        """Evaluate permission for a tool call.

        Returns immediately for ALLOW/DENY, or calls the user callback for ASK.
        """
        ctx = context or {}

        # Determine scope based on tool name
        scope_map = {
            "bash": "bash",
            "read_file": "file_read",
            "write_file": "file_write",
            "edit_file": "file_write",
        }
        scope = scope_map.get(tool_name, "bash")

        # 1. Check permission memory first
        mem_decision = self._check_memory(scope, action)
        if mem_decision:
            return PermissionResult(
                decision=mem_decision,
                reason="记忆中的权限决定",
                risk=RiskAssessment(RiskLevel.SAFE, ""),
            )

        # 2. Check user-defined rules
        rule_decision = self._check_rules(scope, action)
        if rule_decision and rule_decision != PermissionDecision.ASK_USER:
            return PermissionResult(
                decision=rule_decision,
                reason="匹配权限规则",
                risk=RiskAssessment(RiskLevel.SAFE, ""),
            )

        # 3. Danger detection
        if tool_name == "bash":
            risk = self.danger_detector.assess_command(action)
        elif tool_name in ("write_file", "edit_file"):
            risk = self.danger_detector.assess_path(action, "write")
        elif tool_name == "read_file":
            risk = self.danger_detector.assess_path(action, "read")
        else:
            risk = RiskAssessment(RiskLevel.SAFE, "")

        # Auto-deny critical risk
        if risk.level == RiskLevel.CRITICAL:
            return PermissionResult(
                decision=PermissionDecision.DENY,
                reason=f"危险操作: {risk.reason}",
                risk=risk,
            )

        # 4. Ask user
        request = PermissionRequest(
            tool_name=tool_name,
            action=action,
            risk=risk,
            context=ctx,
        )

        if self._ask_callback:
            user_decision = await self._ask_callback(request)
            return PermissionResult(
                decision=user_decision,
                reason="用户决定",
                risk=risk,
            )

        # No callback: auto-allow safe, ask for risky
        if risk.level == RiskLevel.SAFE:
            return PermissionResult(
                decision=PermissionDecision.ALLOW,
                reason="安全操作，自动允许",
                risk=risk,
            )
        return PermissionResult(
            decision=PermissionDecision.ASK_USER,
            reason=f"需要确认 ({risk.reason})",
            risk=risk,
        )
