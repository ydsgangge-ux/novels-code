"""用户意图的统一判定入口。

loop 的问答快速路径、stop_guard 的"该不该动手"，以及后续的任务契约层
都从这里取结论。历史上这三处各写了一份词表且互相不一致：loop 的
``_task_words`` 有"创建"，stop_guard 的 ``CHANGE_WORDS`` 却没有，于是
"创建一个 HTML"在 loop 眼里是任务、在护栏眼里成了闲聊，护栏全程没生效。

判定只做三分类，不做语义理解：

- ``change``  用户要改动工作区（创建 / 修改 / 实现 / 照着参考物改）
- ``answer``  用户只要一段解释
- ``unknown`` 看不出来

``unknown`` 不强制动手——误把提问当任务，会让模型对着"这个项目是做什么的"
去改文件；宁可少拦一次，也不要把闲聊变成改动。
"""

from __future__ import annotations

from typing import Any, Iterable

#: 明确要求改动工作区的说法。
CHANGE_WORDS = (
    "创建", "新建", "生成", "写一个", "写个", "做个", "实现", "添加", "增加", "新增",
    "修改", "改成", "改一下", "改改", "改了吗", "修复", "优化", "重构", "调整", "补上", "加上",
    "删除", "移除", "重命名", "替换", "升级", "完善", "补全", "换掉", "画一个", "画个",
    "create", "write", "add ", "implement", "fix", "refactor", "update", "remove",
    "rename", "build ", "optimi", "generate", "rewrite",
)

#: 给了参考物、要求"照着改"的说法：没写"修改"，但预期是产出改动。
#:
#: 只留动作性明确的词。曾经还有"看看这个"/"你看看这个"/"差在哪"/"学习一下"，
#: 它们把"看看这个项目是做什么的""这两版差在哪"判成了 change，于是 stop_guard
#: 要求模型"立刻用 edit_file 落实修改"——这正是
#: ``test_ambiguous_request_is_allowed_to_stop`` 抓到的回归。模块开头写的是
#: 宁可少拦一次，也不要把闲聊变成改动，所以泛词一律不收。
REFERENCE_WORDS = (
    "照着", "仿照", "像这样", "改进一下",
)

#: 只要一段解释。
ANSWER_WORDS = (
    "解释", "说明一下", "为什么", "怎么理解", "是什么", "什么意思", "怎么回事",
    "分析一下", "讲讲", "介绍一下", "区别",
    "explain", "why ", "what is", "describe", "how does", "difference between",
)


def _is_user_role(role: Any) -> bool:
    value = str(getattr(role, "value", role) or "").lower()
    return value == "user" or value.endswith(".user")


def last_user_text(messages: Iterable[Any]) -> str:
    """取最后一条用户消息的纯文本，跳过系统注入的动态状态。

    多轮对话里第一条用户消息是最初的任务，用它会让提交信息、意图判定、
    进度记录一直停在旧任务上——所以这里从后往前找。
    """
    for msg in reversed(list(messages)):
        if not _is_user_role(getattr(msg, "role", None)):
            continue
        getter = getattr(msg, "get_text", None)
        text = str(getter() if callable(getter) else getattr(msg, "content", "") or "")
        text = text.strip()
        if not text:
            continue
        if text.startswith("[系统提示]") or text.startswith("##"):
            continue
        return text
    return ""


def classify(text: str) -> str:
    """把一条用户消息判成 ``change`` / ``answer`` / ``unknown``。"""
    lowered = (text or "").lower()
    if not lowered.strip():
        return "unknown"

    wants_change = any(w in lowered for w in CHANGE_WORDS) or any(
        w in lowered for w in REFERENCE_WORDS
    )
    wants_answer = any(w in lowered for w in ANSWER_WORDS)

    if wants_change and not wants_answer:
        return "change"
    if wants_answer and not wants_change:
        return "answer"
    if wants_change and wants_answer:
        # 两者都像时按提问处理：误答一句用户会追问，误改一次要回滚。
        return "answer"
    return "unknown"


def wants_change(text: str) -> bool:
    """便捷包装：这条消息是否要求改动工作区。"""
    return classify(text) == "change"


#: 问答路径允许带上的只读工具：能看、能查、能上网，改不了东西。
#:
#: 刻意包含 ``web_search`` / ``web_fetch``：很多"为什么/怎么回事"的答案在
#: 项目之外（版本差异、平台行为），不给联网就只能猜。也包含 ``tool_search``，
#: 让它能自己找到没在核心集里的只读工具。
READ_ONLY_TOOLS: tuple[str, ...] = (
    "read_file",
    "list_dir",
    "grep",
    "glob",
    "everything_search",
    "find_symbol",
    "find_references",
    "tool_search",
    "web_search",
    "web_fetch",
    "recall_conversation",
    "chat_log",
)


def tool_policy(text: str) -> str:
    """这一轮该给模型什么工具面：``"all"`` 还是 ``"read_only"``。

    和 :func:`classify` 分开是有意的。``classify`` 的结论还被 stop_guard 拿去
    判断"要不要催模型动手"，那里的误判代价是"少催一次"；这里的决定是
    "给不给手"，误判的代价是整轮断线——模型看不见文件，只能把工具调用写成
    正文里的 ``<tool_call>``（2026-11-23 的"没做解卦吗？为什么要摇8次？"
    就是这么死的）。两种代价差一个数量级，不该共用同一个结论。

    只读集合不是零成本（仍要带 schema），但它把"判错"从断线降级成多看两个
    文件；这是有意选择的偏保守方向。
    """
    return "read_only" if classify(text) == "answer" else "all"
