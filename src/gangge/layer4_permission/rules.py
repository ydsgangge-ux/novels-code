"""Permission policy — declarative, data-driven allow / ask / deny rules.

The design follows Codex's ``execpolicy`` crate, for three reasons that matter
more than the syntax:

1. **Rules are data, not code.** A policy is a YAML document, so it can be
   reviewed, diffed, shipped per-project, and overridden by a user file. The
   shipped file (``default_policy.yml``) is the single source of truth — there is
   no second Python copy to drift away from it.
2. **The strictest match wins.** With a flat "first rule that matches" list,
   rule order silently becomes part of the semantics and every new rule can
   shadow an existing one. Here the policy collects *all* matching rules and
   keeps the most restrictive (``deny`` > ``ask`` > ``allow``).
3. **Every rule carries its own tests.** ``match`` / ``not_match`` are example
   actions that must (not) be caught by ``pattern``. They are verified when the
   policy loads, so a typo'd regex fails loudly at startup instead of quietly
   letting a dangerous command through at runtime.

Deliberately *no* catch-all ``ask`` rule. Under strictest-wins a blanket
``.*  → ask`` would outrank every ``allow`` rule and turn the policy back into
"prompt for everything". The "nothing matched" case is expressed instead as a
per-scope ``default`` decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping


class RuleAction(str, Enum):
    """What the policy says about a matching action."""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


# Numeric rank used to pick the strictest verdict among all matches.
SEVERITY: dict[RuleAction, int] = {
    RuleAction.ALLOW: 0,
    RuleAction.ASK: 1,
    RuleAction.DENY: 2,
}


class PolicyError(ValueError):
    """A policy document that cannot be trusted (bad regex, failed sample)."""


# ── Scopes ──────────────────────────────────────────────────────
# A scope is the *kind* of side effect an action has. Rules never span scopes:
# a path rule must not accidentally match a shell command.

SCOPE_BASH = "bash"
SCOPE_FILE_READ = "file_read"
SCOPE_FILE_WRITE = "file_write"
SCOPE_NETWORK = "network"
SCOPE_MCP = "mcp"
SCOPE_INTERACTIVE = "interactive"

SCOPES: tuple[str, ...] = (
    SCOPE_BASH,
    SCOPE_FILE_READ,
    SCOPE_FILE_WRITE,
    SCOPE_NETWORK,
    SCOPE_MCP,
    SCOPE_INTERACTIVE,
)

# Fallback decision when no rule matches. Reading is safe by definition;
# interactive tools have no side effects; everything that can reach outside the
# project (shell, network, third-party MCP server) asks.
DEFAULT_DECISIONS: dict[str, RuleAction] = {
    SCOPE_BASH: RuleAction.ASK,
    SCOPE_FILE_READ: RuleAction.ALLOW,
    SCOPE_FILE_WRITE: RuleAction.ALLOW,
    SCOPE_NETWORK: RuleAction.ASK,
    SCOPE_MCP: RuleAction.ASK,
    SCOPE_INTERACTIVE: RuleAction.ALLOW,
}

# Tool name → scope. Prefixes cover families (``novel_*``, ``server__tool``)
# without enumerating every tool, so a newly added tool inherits sane policy.
_TOOL_SCOPES: dict[str, str] = {
    # execution
    "bash": SCOPE_BASH,
    "lint_check": SCOPE_BASH,
    # read-only filesystem
    "read_file": SCOPE_FILE_READ,
    "list_dir": SCOPE_FILE_READ,
    "grep": SCOPE_FILE_READ,
    "glob": SCOPE_FILE_READ,
    "everything_search": SCOPE_FILE_READ,
    "find_symbol": SCOPE_FILE_READ,
    "find_references": SCOPE_FILE_READ,
    "recall_conversation": SCOPE_FILE_READ,
    "vision": SCOPE_FILE_READ,
    "tool_search": SCOPE_FILE_READ,
    # write
    "write_file": SCOPE_FILE_WRITE,
    "edit_file": SCOPE_FILE_WRITE,
    "apply_patch": SCOPE_FILE_WRITE,
    "create_tool": SCOPE_FILE_WRITE,
    "generate_image": SCOPE_FILE_WRITE,
    # network
    "web_fetch": SCOPE_NETWORK,
    "web_search": SCOPE_NETWORK,
    "browser": SCOPE_NETWORK,
    "browser_debug": SCOPE_NETWORK,
    "package_info": SCOPE_NETWORK,
    # no side effects
    "ask_user": SCOPE_INTERACTIVE,
    "chat_log": SCOPE_INTERACTIVE,
    "TodoWrite": SCOPE_INTERACTIVE,
}

_TOOL_PREFIX_SCOPES: tuple[tuple[str, str], ...] = (
    ("novel_", SCOPE_FILE_WRITE),
    ("dramatica_", SCOPE_FILE_WRITE),
)


def scope_for_tool(tool_name: str) -> str:
    """Which scope a tool call belongs to.

    Unknown tools are treated as MCP-style external tools: they are not ours, so
    we cannot reason about their side effects — ask.
    """
    exact = _TOOL_SCOPES.get(tool_name)
    if exact:
        return exact
    for prefix, scope in _TOOL_PREFIX_SCOPES:
        if tool_name.startswith(prefix):
            return scope
    return SCOPE_MCP


# ── Rules ───────────────────────────────────────────────────────


@dataclass
class PermissionRule:
    """A single policy rule.

    ``pattern`` is a regular expression matched against the action string (a
    shell command, a path, a URL). ``match`` / ``not_match`` are examples used to
    self-test the rule at load time.
    """

    action: RuleAction
    pattern: str
    description: str = ""
    scope: str = SCOPE_BASH
    match: tuple[str, ...] = ()
    not_match: tuple[str, ...] = ()
    justification: str = ""

    def matches(self, target: str | None) -> bool:
        if target is None:
            return False
        try:
            return bool(re.search(self.pattern, target, re.IGNORECASE))
        except re.error:
            return False

    def validate(self) -> None:
        """Fail loudly on a regex or a sample that contradicts the rule."""
        try:
            re.compile(self.pattern, re.IGNORECASE)
        except re.error as exc:
            raise PolicyError(
                f"规则正则无法编译 ({self.scope}: {self.pattern!r}): {exc}"
            ) from exc

        for sample in self.match:
            if not self.matches(sample):
                raise PolicyError(
                    f"规则 {self.scope}/{self.pattern!r} 声明应命中 {sample!r}，实际未命中"
                )
        for sample in self.not_match:
            if self.matches(sample):
                raise PolicyError(
                    f"规则 {self.scope}/{self.pattern!r} 声明不应命中 {sample!r}，实际命中了"
                )


@dataclass(frozen=True)
class RuleVerdict:
    """Outcome of evaluating a policy."""

    decision: RuleAction
    reason: str
    rule: PermissionRule | None = None
    matched: tuple[PermissionRule, ...] = ()

    @property
    def action_value(self) -> str:
        return self.decision.value


# ── Policy ──────────────────────────────────────────────────────


@dataclass
class PermissionPolicy:
    """An ordered bag of rules plus per-scope defaults."""

    rules: list[PermissionRule] = field(default_factory=list)
    defaults: dict[str, RuleAction] = field(
        default_factory=lambda: dict(DEFAULT_DECISIONS)
    )
    name: str = "default"

    # -- construction ------------------------------------------

    @classmethod
    def from_mapping(
        cls, data: Mapping[str, Any], name: str = "policy"
    ) -> "PermissionPolicy":
        raw_defaults = data.get("defaults") or {}
        if not isinstance(raw_defaults, Mapping):
            raise PolicyError("policy.defaults 必须是映射")
        defaults = dict(DEFAULT_DECISIONS)
        for scope, decision in raw_defaults.items():
            defaults[str(scope)] = _as_action(decision, f"defaults.{scope}")

        raw_rules = data.get("rules") or []
        if not isinstance(raw_rules, list):
            raise PolicyError("policy.rules 必须是列表")
        rules: list[PermissionRule] = []
        for index, item in enumerate(raw_rules):
            if not isinstance(item, Mapping):
                raise PolicyError(f"policy.rules[{index}] 必须是映射")
            rules.append(
                PermissionRule(
                    action=_as_action(
                        item.get("decision", item.get("action")),
                        f"rules[{index}].decision",
                    ),
                    pattern=str(item.get("pattern") or ""),
                    description=str(item.get("description") or ""),
                    scope=str(item.get("scope") or SCOPE_BASH),
                    match=_as_str_tuple(item.get("match")),
                    not_match=_as_str_tuple(item.get("not_match")),
                    justification=str(item.get("justification") or ""),
                )
            )

        policy = cls(rules=rules, defaults=defaults, name=str(data.get("name") or name))
        policy.validate()
        return policy

    @classmethod
    def from_yaml(cls, path: str | Path) -> "PermissionPolicy":
        import yaml

        file_path = Path(path)
        try:
            data = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError as exc:
            raise PolicyError(f"策略文件不存在: {file_path}") from exc
        except yaml.YAMLError as exc:
            raise PolicyError(f"策略文件不是合法 YAML: {file_path}: {exc}") from exc
        if not isinstance(data, Mapping):
            raise PolicyError(f"策略文件根节点必须是映射: {file_path}")
        return cls.from_mapping(data, name=file_path.stem)

    @classmethod
    def fallback(cls) -> "PermissionPolicy":
        """No rules at all, but fail-closed: anything with side effects asks."""
        defaults = {
            SCOPE_BASH: RuleAction.ASK,
            SCOPE_FILE_READ: RuleAction.ALLOW,
            SCOPE_FILE_WRITE: RuleAction.ASK,
            SCOPE_NETWORK: RuleAction.ASK,
            SCOPE_MCP: RuleAction.ASK,
            SCOPE_INTERACTIVE: RuleAction.ALLOW,
        }
        return cls(rules=[], defaults=defaults, name="fallback")

    @classmethod
    def default(cls) -> "PermissionPolicy":
        """The shipped policy, or a fail-closed stub if the data file is gone."""
        path = Path(__file__).with_name("default_policy.yml")
        if not path.exists():
            return cls.fallback()
        return cls.from_yaml(path)

    # -- use ---------------------------------------------------

    def validate(self) -> None:
        """Validate every rule and every scope name. Raises ``PolicyError``."""
        for rule in self.rules:
            if rule.scope not in SCOPES:
                raise PolicyError(
                    f"未知 scope {rule.scope!r}，可用: {', '.join(SCOPES)}"
                )
            rule.validate()
        for scope in self.defaults:
            if scope not in SCOPES:
                raise PolicyError(f"defaults 中出现未知 scope {scope!r}")

    def add(self, rule: PermissionRule) -> None:
        """Add a rule, validating it first (a bad rule must not enter the set)."""
        rule.validate()
        self.rules.append(rule)

    def default_for(self, scope: str) -> RuleAction:
        return self.defaults.get(scope, DEFAULT_DECISIONS.get(scope, RuleAction.ASK))

    def evaluate(self, scope: str, action: str | None) -> RuleVerdict:
        """Return the strictest matching verdict, or the scope default."""
        hits = [r for r in self.rules if r.scope == scope and r.matches(action)]
        if not hits:
            decision = self.default_for(scope)
            return RuleVerdict(
                decision=decision,
                reason=f"未命中任何 {scope} 规则 → 默认 {decision.value}",
            )

        strictest = hits[0]
        for rule in hits[1:]:
            if SEVERITY[rule.action] > SEVERITY[strictest.action]:
                strictest = rule
            elif SEVERITY[rule.action] == SEVERITY[strictest.action]:
                # Same severity: the more specific pattern explains better.
                if len(rule.pattern) > len(strictest.pattern):
                    strictest = rule

        parts = [strictest.description or strictest.pattern]
        if strictest.justification:
            parts.append(strictest.justification)
        if len(hits) > 1:
            parts.append(f"(共 {len(hits)} 条规则命中，取最严)")
        return RuleVerdict(
            decision=strictest.action,
            reason=" — ".join(parts),
            rule=strictest,
            matched=tuple(hits),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "defaults": {scope: action.value for scope, action in self.defaults.items()},
            "rules": [
                {
                    "scope": r.scope,
                    "decision": r.action.value,
                    "pattern": r.pattern,
                    "description": r.description,
                    "justification": r.justification,
                    "match": list(r.match),
                    "not_match": list(r.not_match),
                }
                for r in self.rules
            ],
        }


def _as_action(value: Any, where: str) -> RuleAction:
    if isinstance(value, RuleAction):
        return value
    text = str(value or "").strip().lower()
    aliases = {
        "prompt": "ask",
        "forbidden": "deny",
        "allowed": "allow",
        "denied": "deny",
    }
    text = aliases.get(text, text)
    try:
        return RuleAction(text)
    except ValueError as exc:
        raise PolicyError(
            f"{where} 取值非法: {value!r}（可用 allow / ask / deny）"
        ) from exc


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Iterable):
        return tuple(str(v) for v in value)
    return (str(value),)


def default_policy() -> PermissionPolicy:
    """The policy a fresh process starts with."""
    return PermissionPolicy.default()


def __getattr__(name: str) -> Any:
    """``DEFAULT_RULES`` stays importable, but is materialised lazily.

    Lazy on purpose: importing this module must never depend on the YAML file
    being present, and a broken policy should surface where the guard is built
    (with a clear error) rather than at import time.
    """
    if name == "DEFAULT_RULES":
        return PermissionPolicy.default().rules
    raise AttributeError(name)
