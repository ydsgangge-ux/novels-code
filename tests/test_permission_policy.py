"""Permission policy: strictest-wins, self-testing rules, compound commands."""

from __future__ import annotations

import textwrap

import pytest

from gangge.layer4_permission.guard import (
    PermissionDecision,
    PermissionGuard,
    split_command,
)
from gangge.layer4_permission.rules import (
    PermissionPolicy,
    PermissionRule,
    PolicyError,
    RuleAction,
    scope_for_tool,
)

ROOT_WIPE = "rm -rf /"


# ── Policy loading ──────────────────────────────────────────────


def test_shipped_policy_loads_and_self_tests():
    """Loading the real policy runs every rule's match/not_match samples."""
    policy = PermissionPolicy.default()
    assert policy.rules, "shipped policy must contain rules"
    assert policy.name == "default"
    # Every rule declares at least one positive sample — that is the "rules are
    # tested by construction" property: a rule with no sample is untested.
    for rule in policy.rules:
        assert rule.match, f"rule without a positive sample: {rule.pattern}"


def test_bad_sample_raises_at_load_time():
    data = {
        "rules": [
            {
                "scope": "bash",
                "decision": "deny",
                "pattern": r"^git push$",
                "match": ["git pull"],  # wrong on purpose
            }
        ]
    }
    with pytest.raises(PolicyError, match="实际未命中"):
        PermissionPolicy.from_mapping(data)


def test_uncompilable_pattern_raises():
    with pytest.raises(PolicyError, match="正则无法编译"):
        PermissionPolicy.from_mapping(
            {"rules": [{"scope": "bash", "decision": "deny", "pattern": "([unclosed"}]}
        )


def test_unknown_scope_raises():
    with pytest.raises(PolicyError, match="未知 scope"):
        PermissionPolicy.from_mapping(
            {"rules": [{"scope": "telepathy", "decision": "deny", "pattern": "x"}]}
        )


# ── Strictest-wins ──────────────────────────────────────────────


def _policy(*rules: tuple[str, str]) -> PermissionPolicy:
    return PermissionPolicy.from_mapping(
        {
            "defaults": {"bash": "allow"},
            "rules": [
                {"scope": "bash", "decision": d, "pattern": p} for p, d in rules
            ],
        }
    )


def test_deny_beats_allow_regardless_of_order():
    allow_first = _policy((r"^git", "allow"), (r"git push", "deny"))
    deny_first = _policy((r"git push", "deny"), (r"^git", "allow"))
    for policy in (allow_first, deny_first):
        verdict = policy.evaluate("bash", "git push origin main")
        assert verdict.decision is RuleAction.DENY


def test_ask_beats_allow():
    policy = _policy((r"^rm", "allow"), (r"rm -rf", "ask"))
    assert policy.evaluate("bash", "rm -rf build").decision is RuleAction.ASK


def test_no_match_uses_scope_default():
    policy = _policy((r"^ls$", "deny"))
    verdict = policy.evaluate("bash", "make build")
    assert verdict.decision is RuleAction.ALLOW
    assert "默认" in verdict.reason


def test_shipped_policy_has_no_catch_all_ask():
    """A blanket `.* -> ask` would outrank every allow rule under strictest-wins."""
    for rule in PermissionPolicy.default().rules:
        assert rule.pattern.strip() != ".*", f"catch-all rule reintroduced: {rule}"


# ── Compound commands ──────────────────────────────────────────


def test_split_command_covers_injection_points():
    assert split_command("ls; rm -rf build") == ["ls", "rm -rf build"]
    assert split_command("ls && rm -rf build") == ["ls", "rm -rf build"]
    assert split_command("ls | grep x") == ["ls", "grep x"]
    assert split_command("echo $(whoami)") == ["echo $(whoami)", "whoami"]
    assert split_command("echo `id`") == ["echo `id`", "id"]


@pytest.mark.asyncio
async def test_allow_rule_does_not_launder_second_command():
    guard = PermissionGuard()
    result = await guard.check("bash", f"ls; {ROOT_WIPE}")
    assert result.decision is PermissionDecision.DENY


@pytest.mark.asyncio
async def test_substitution_is_inspected():
    guard = PermissionGuard()
    result = await guard.check("bash", f"echo $({ROOT_WIPE})")
    assert result.decision is PermissionDecision.DENY


@pytest.mark.asyncio
async def test_plain_read_only_command_is_allowed():
    guard = PermissionGuard()
    result = await guard.check("bash", "ls -la")
    assert result.decision is PermissionDecision.ALLOW
    assert result.rule is not None
    assert result.rule.description


# ── Fail-closed behaviour ──────────────────────────────────────


@pytest.mark.asyncio
async def test_ask_without_callback_stays_ask():
    """No callback must NOT silently become ALLOW — it stays a question."""
    guard = PermissionGuard()
    result = await guard.check("bash", "python train.py")
    assert result.decision is PermissionDecision.ASK_USER


@pytest.mark.asyncio
async def test_callback_is_consulted_and_can_deny():
    seen: list[str] = []

    async def ask(request):
        seen.append(request.action)
        return PermissionDecision.DENY

    guard = PermissionGuard(ask_callback=ask)
    result = await guard.check("bash", "python train.py")
    assert result.decision is PermissionDecision.DENY
    assert seen == ["python train.py"]


@pytest.mark.asyncio
async def test_broken_policy_file_degrades_to_fail_closed(tmp_path):
    bad = tmp_path / "bad.yml"
    bad.write_text("rules: [{scope: bash, decision: nope, pattern: x}]", encoding="utf-8")

    guard = PermissionGuard(policy_path=bad)
    assert guard.policy_error
    # The fallback has no rules, so a write asks instead of being allowed.
    result = await guard.check("write_file", "src/app.py")
    assert result.decision is PermissionDecision.ASK_USER


@pytest.mark.asyncio
async def test_remembered_decision_short_circuits():
    guard = PermissionGuard()
    guard.remember_decision("bash", r"^python train", PermissionDecision.DENY)
    result = await guard.check("bash", "python train.py --fast")
    assert result.decision is PermissionDecision.DENY
    assert "记忆" in result.reason


# ── Scope mapping ──────────────────────────────────────────────


def test_scope_mapping_covers_tool_families():
    assert scope_for_tool("read_file") == "file_read"
    assert scope_for_tool("write_file") == "file_write"
    assert scope_for_tool("apply_patch") == "file_write"
    assert scope_for_tool("bash") == "bash"
    assert scope_for_tool("novel_write_chapter") == "file_write"
    assert scope_for_tool("filesystem__read") == "mcp"
    assert scope_for_tool("TodoWrite") == "interactive"


@pytest.mark.asyncio
async def test_credential_read_asks():
    guard = PermissionGuard()
    result = await guard.check("read_file", ".env")
    assert result.decision is PermissionDecision.ASK_USER


@pytest.mark.asyncio
async def test_system_write_is_denied():
    guard = PermissionGuard()
    result = await guard.check("write_file", "C:\\Windows\\System32\\drivers\\etc\\hosts")
    assert result.decision is PermissionDecision.DENY


@pytest.mark.asyncio
async def test_workspace_write_is_allowed():
    guard = PermissionGuard()
    result = await guard.check("write_file", "src/gangge/cli.py")
    assert result.decision is PermissionDecision.ALLOW


# ── Custom policy via YAML ─────────────────────────────────────


@pytest.mark.asyncio
async def test_project_policy_overrides_defaults(tmp_path):
    policy_file = tmp_path / "policy.yml"
    policy_file.write_text(
        textwrap.dedent(
            """
            defaults:
              bash: deny
            rules:
              - scope: bash
                decision: allow
                pattern: '^make test$'
                match: ['make test']
            """
        ),
        encoding="utf-8",
    )
    guard = PermissionGuard(policy_path=policy_file)
    assert (await guard.check("bash", "make test")).decision is PermissionDecision.ALLOW
    assert (await guard.check("bash", "make deploy")).decision is PermissionDecision.DENY


def test_add_rule_validates():
    policy = PermissionPolicy.default()
    with pytest.raises(PolicyError):
        policy.add(
            PermissionRule(
                action=RuleAction.ALLOW,
                pattern=r"^danger$",
                match=["safe"],
            )
        )
