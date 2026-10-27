"""Interactive permission prompts for the terminal front-ends.

The guard is transport-agnostic: it only knows "ask a callback". This module is
the terminal implementation of that callback, shared by the one-shot CLI, the
REPL and the Textual TUI, so the wording and the available choices cannot drift
between them.

Choices mirror Codex's approval menu, minus the ones that need a sandbox:

    y  allow once
    n  deny (default — pressing Enter denies)
    a  always allow this exact action for the rest of the session

Non-interactive stdin (pipes, CI) cannot answer, and silently answering "yes"
is exactly how an agent deletes something it should not. Those runs must pass
``--yes`` / ``GANGGE_AUTO_APPROVE=1`` explicitly, otherwise the call is denied.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any, Callable

from gangge.layer4_permission.danger import RiskLevel
from gangge.layer4_permission.guard import (
    PermissionDecision,
    PermissionGuard,
    PermissionRequest,
)

RISK_LABELS: dict[RiskLevel, tuple[str, str]] = {
    RiskLevel.SAFE: ("低", "green"),
    RiskLevel.LOW: ("低", "green"),
    RiskLevel.MEDIUM: ("中", "yellow"),
    RiskLevel.HIGH: ("高", "red"),
    RiskLevel.CRITICAL: ("极高", "bold red"),
}

_SCOPE_LABELS = {
    "bash": "执行命令",
    "file_read": "读取文件",
    "file_write": "写入文件",
    "network": "访问网络",
    "mcp": "调用外部工具",
    "interactive": "交互",
}


def auto_approve_from_env() -> bool:
    """Whether unattended approval was explicitly requested."""
    value = os.environ.get("GANGGE_AUTO_APPROVE", "").strip().lower()
    return value in ("1", "true", "yes", "y", "on")


def format_request(request: PermissionRequest, markdown: bool = True) -> str:
    """Human-readable block describing what the agent wants to do."""
    label, _ = RISK_LABELS.get(request.risk.level, ("低", "green"))
    scope = _SCOPE_LABELS.get(request.scope, request.scope)
    action = request.action if len(request.action) <= 300 else request.action[:300] + " …"

    if not markdown:
        lines = [
            f"需要确认：{scope}（风险 {label}）",
            f"  {action}",
            f"  原因: {request.reason}",
        ]
        if request.risk.reason:
            lines.append(f"  风险: {request.risk.reason}")
        return "\n".join(lines)

    lines = [
        f"[bold yellow]⚠ 需要确认[/bold yellow] [dim]({scope} · 风险 {label})[/dim]",
        f"  [cyan]{action}[/cyan]",
        f"  [dim]原因: {request.reason}[/dim]",
    ]
    if request.risk.reason:
        lines.append(f"  [dim]风险: {request.risk.reason}[/dim]")
    return "\n".join(lines)


def _as_regex(action: str) -> str:
    """Escape an action so it can be replayed as an exact-match allow rule."""
    return "^" + re.escape(action) + "$"


class ConsolePermissionPrompter:
    """Terminal ``ask_callback`` implementation.

    Attach it to a guard with :meth:`attach` so the "always" choice can install
    a session-scoped allow rule instead of poking at private state.
    """

    def __init__(
        self,
        console: Any | None = None,
        auto_approve: bool | None = None,
        input_fn: Callable[[str], str] | None = None,
    ) -> None:
        self.console = console
        self.auto_approve = auto_approve_from_env() if auto_approve is None else auto_approve
        self._input = input_fn or input
        self.guard: PermissionGuard | None = None
        self.asked = 0
        self.denied = 0

    # -- wiring ------------------------------------------------

    def attach(self, guard: PermissionGuard) -> "ConsolePermissionPrompter":
        self.guard = guard
        guard.set_ask_callback(self)
        return self

    # -- callback ----------------------------------------------

    async def __call__(self, request: PermissionRequest) -> PermissionDecision:
        self.asked += 1

        if self.auto_approve:
            self._print("[dim]自动批准（GANGGE_AUTO_APPROVE）[/dim]")
            return PermissionDecision.ALLOW

        if not sys.stdin.isatty():
            # No human on the other end: deny and say how to opt in.
            self._print(
                "非交互环境无法确认，已拒绝。"
                "如需自动批准请显式传入 --yes 或设置 GANGGE_AUTO_APPROVE=1"
            )
            self.denied += 1
            return PermissionDecision.DENY

        self._print(format_request(request))
        answer = self._ask()
        return self._decide(answer, request)

    # -- helpers -----------------------------------------------

    def _decide(self, answer: str, request: PermissionRequest) -> PermissionDecision:
        choice = (answer or "").strip().lower()
        if choice in ("a", "always", "总是"):
            pattern = _as_regex(request.action)
            if self.guard is not None:
                from gangge.layer4_permission.rules import PermissionRule, RuleAction

                try:
                    self.guard.add_rule(
                        PermissionRule(
                            action=RuleAction.ALLOW,
                            pattern=pattern,
                            scope=request.scope,
                            description=f"用户本次会话批准: {request.action[:60]}",
                        )
                    )
                except Exception:  # a pathological action must not crash the turn
                    self.guard.remember_decision(
                        request.scope, pattern, PermissionDecision.ALLOW
                    )
            self._print("[dim]已记住：本次会话内不再询问相同操作[/dim]")
            return PermissionDecision.ALLOW

        if choice in ("y", "yes", "是"):
            return PermissionDecision.ALLOW

        self.denied += 1
        return PermissionDecision.DENY

    def _ask(self) -> str:
        prompt = "允许吗？[y]一次 / [n]拒绝 / [a]总是允许 > "
        try:
            return self._input(prompt)
        except (EOFError, KeyboardInterrupt):
            return ""

    def _print(self, text: str) -> None:
        if self.console is not None:
            try:
                self.console.print(text)
                return
            except Exception:
                pass
        print(re.sub(r"\[/?[a-z0-9 #]+\]", "", text))


async def allow_all_callback(request: PermissionRequest) -> PermissionDecision:
    """Explicit "never ask" policy, for ``--yes`` callers and tests."""
    return PermissionDecision.ALLOW


async def deny_all_callback(request: PermissionRequest) -> PermissionDecision:
    """Fail-closed default for hosts that cannot prompt."""
    return PermissionDecision.DENY
