"""Stop guard — refuse a premature stop from a timid model.

A weak model's most expensive failure is not a bad tool call; it is **finishing
without doing the work**:

    用户: 帮我修复登录接口的 bug
    模型: 我已经分析了代码，问题出在 auth.py 的 validate 方法，
          你可以在那里加一个空值判断。        ← 什么都没改，turn 结束

A strong model would have opened the file and edited it. A weak one is
"helpful" in the chat sense: it describes the fix and stops. The user is then
left doing the work by hand while believing the agent did it.

The second pattern is just as common and more dangerous — **claiming success
without verifying**:

    模型: 已修复！                    ← 改了 3 个文件，一次都没跑过
    实际: 语法错误，项目根本起不来

Both are turn-level policy, not tool-level, so this listens on
``agent/turn-stopping``. Returning ``keep_going`` makes the loop inject one
user message and continue; returning ``None`` lets the loop's own rules decide.

Bounded on purpose: each condition fires at most ``max_nudges`` times per turn,
so a stubborn model still terminates instead of fighting the guard until
``max_tool_rounds``.
"""

from __future__ import annotations

import logging
from typing import Any

from gangge.runtime import events
from gangge.runtime.context import Context, Inject
from gangge.runtime.seams import TurnDirective

logger = logging.getLogger(__name__)

name = "stop_guard"
inject = Inject()

# Words that mean "change my project", as opposed to "explain something".
CHANGE_WORDS = (
    "修复", "修改", "改成", "改一下", "优化", "重构", "实现", "添加", "增加", "新增",
    "删除", "移除", "重命名", "调整", "补上", "加上", "写一个", "写个", "做个",
    "fix", "change", "refactor", "implement", "add ", "create ", "write ", "update ",
    "remove", "rename", "optimi", "build ",
)

# Words that mean the user only wants an answer.
QUESTION_WORDS = (
    "解释", "为什么", "怎么理解", "是什么", "什么意思", "说明一下", "分析一下",
    "explain", "why ", "what is", "describe", "how does",
)

# Read-only tools: a turn made only of these changed nothing.
READ_TOOLS = ("read_file", "grep", "glob", "list_dir", "find_symbol",
              "find_references", "everything_search", "recall_conversation")


class StopGuard:
    """Reject unproductive or unverified stops, a bounded number of times."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        self.max_nudges = int(config.get("max_nudges", 2))
        self.require_verification = bool(config.get("require_verification", True))
        self.require_action = bool(config.get("require_action", True))
        self._fired: set[str] = set()
        self._nudges = 0
        self._last_round = -1
        self.trips = 0

    # -- event handlers ------------------------------------------

    def reset(self, *_args: Any) -> None:
        self._fired.clear()
        self._nudges = 0
        self._last_round = -1

    def decide(self, state: dict[str, Any]) -> TurnDirective | None:
        round_num = int(state.get("round", 0))
        # A turn is bracketed by turn/start; the round going backwards is the
        # belt-and-braces signal that a previous turn left state behind.
        if round_num < self._last_round:
            self.reset()
        self._last_round = round_num

        if self._nudges >= self.max_nudges:
            return None

        user_task = str(state.get("user_task") or "")
        if not _wants_change(user_task):
            return None  # the user asked a question; stopping is correct

        if self.require_action:
            verdict = self._check_action(state)
            if verdict is not None:
                return verdict

        if self.require_verification:
            verdict = self._check_verification(state)
            if verdict is not None:
                return verdict

        return None

    # -- conditions ----------------------------------------------

    def _check_action(self, state: dict[str, Any]) -> TurnDirective | None:
        """Did anything actually get written?"""
        if state.get("has_modified_files"):
            return None
        if "action" in self._fired:
            return None

        history = state.get("tool_history") or []
        read_only = [h for h in history if h.get("name") in READ_TOOLS]
        # Nothing at all, or nothing but reading: the model described work
        # instead of doing it.
        if not history:
            message = (
                "你还没有调用任何工具。请立刻开始动手：\n"
                "1. 用 read_file / grep 定位要改的地方；\n"
                "2. 用 edit_file 或 write_file 真正修改；\n"
                "3. 用 bash 或 lint_check 验证。\n"
                "不要只描述方案，直接调用工具执行。"
            )
            reason = "只输出方案、没有动手，要求继续执行"
        else:
            message = (
                f"你已经调用了 {len(read_only)} 次只读工具，但**没有修改任何文件**，"
                "任务还没有完成。\n"
                "请立刻用 edit_file 或 write_file 落实修改。"
                "如果你认为不需要改代码，请在回复里明确说明原因。"
            )
            reason = "只读不写，要求落实修改"

        self._fired.add("action")
        self._nudges += 1
        self.trips += 1
        logger.info("[stop] refusing stop: no file modified (round %s)", state.get("round"))
        return TurnDirective.keep_going(message, reason)

    def _check_verification(self, state: dict[str, Any]) -> TurnDirective | None:
        """Were the changes verified before declaring victory?"""
        if not state.get("has_modified_files"):
            return None
        if state.get("verified_after_write"):
            return None
        if "verify" in self._fired:
            return None

        files = state.get("files_touched") or []
        listed = "、".join(f"`{f}`" for f in files[:3]) or "修改过的文件"
        message = (
            f"你已经修改了 {listed}，但**还没有验证**。\n"
            "请立刻运行一次验证，例如：\n"
            "- `bash`: python -m pytest -q / python -c \"import 模块名\" / 项目启动命令\n"
            "- `lint_check`: 检查语法错误\n"
            "只有验证通过之后，才能说任务完成。如果验证失败，请根据报错继续修复。"
        )
        self._fired.add("verify")
        self._nudges += 1
        self.trips += 1
        logger.info("[stop] refusing stop: changes unverified (round %s)", state.get("round"))
        return TurnDirective.keep_going(message, "改了文件但未验证，要求验证")


def _wants_change(text: str) -> bool:
    """Does this request ask for a code change rather than an explanation?"""
    lowered = (text or "").lower()
    if not lowered.strip():
        return False
    if any(word in lowered for word in CHANGE_WORDS):
        return True
    if any(word in lowered for word in QUESTION_WORDS):
        return False
    # Ambiguous requests ("看看这个项目") are treated as no-change: the cost of
    # nagging on a question is worse than the cost of missing one change.
    return False


def apply(ctx: Context, config: dict[str, Any]) -> None:
    guard = StopGuard(config)
    ctx.provide("stop_guard", guard)
    ctx.on(events.AGENT_TURN_STOPPING, guard.decide)
    ctx.on(events.TURN_START, guard.reset)
    logger.info(
        "[stop] premature-stop guard active (max_nudges=%d, verify=%s)",
        guard.max_nudges,
        guard.require_verification,
    )
