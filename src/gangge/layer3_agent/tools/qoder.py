"""qoder_delegate — 把一件可验证的活外包给 Qoder CLI。

这是一个**综合型**工具：一次调用 = 一个闭环，而不是一个文件操作。这样做的
唯一目的是省上下文——远端的探索过程留在它自己的上下文里，主 agent 只收到
一段 ≤1400 字符的结论 + 验收结果 + 日志路径。

代价是"做到什么程度"必须事前说清，所以 ``scope`` 与 ``dod`` 是硬要求：
没有白名单和不靠机器判定的"完成"，委派出去的结果无法采信。
"""

from __future__ import annotations

import logging
from typing import Any

from gangge.layer3_agent.tools.base import BaseTool, ToolResult
from gangge.layer4_tools.qoder_cli import (
    DelegateSpec,
    QoderConfig,
    delegate,
)

logger = logging.getLogger(__name__)


class QoderDelegateTool(BaseTool):
    """把闭环任务委派给 Qoder CLI（独立进程）。"""

    def __init__(self, workspace: str = ""):
        self.workspace = workspace

    @property
    def name(self) -> str:
        return "qoder_delegate"

    @property
    def description(self) -> str:
        return (
            "把一个【可验证的闭环任务】外包给 Qoder CLI 在独立进程里做，只把结论带回来。\n"
            "适用：动手前要读很多文件（>8 个）的改造；机械且可验证的批量修改（重命名、补测试、"
            "依赖升级适配）；需要跑很久的验证（全量测试/构建）。\n"
            "不适用：需要产品决策或要问用户的任务；改动少于 3 个文件的小修；需要本会话状态的任务。\n"
            "必须同时给出 scope（允许修改的文件白名单）和 dod（验收命令），否则调用会被拒绝——\n"
            "没有机器可判的完成标准，就无法确认它做到什么程度。\n"
            "返回 JSON：status=done/partial/violated/failed、summary、files_changed、checks、log。\n"
            "status=partial 表示改动发生了但验收没全绿：不算完成，必须自己看 checks 再决定。\n"
            "status=violated 表示改到了 scope 之外，应回滚并自己处理。"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "要完成什么，写成工单：目标 + 期望行为，不要写成聊天",
                },
                "mode": {
                    "type": "string",
                    "enum": ["analyze", "edit", "verify"],
                    "description": "analyze=只读分析；edit=改代码（要 scope）；verify=跑验证（要 scope）",
                    "default": "edit",
                },
                "scope": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "允许修改的文件/目录（支持 glob）。edit/verify 必填",
                },
                "forbidden": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "明确禁止修改的文件（如 .env、pyproject.toml、CI 配置）",
                },
                "dod": {
                    "type": "array",
                    "description": "完成定义：验收命令列表，全部满足才算 done",
                    "items": {
                        "oneOf": [
                            {"type": "string", "description": "命令本身，期望退出码 0"},
                            {
                                "type": "object",
                                "properties": {
                                    "cmd": {"type": "string"},
                                    "expect_exit": {"type": "integer", "default": 0},
                                    "timeout_s": {"type": "integer", "default": 300},
                                },
                                "required": ["cmd"],
                                "additionalProperties": False,
                            },
                        ]
                    },
                },
                "current_state": {
                    "type": "object",
                    "description": "动手前的基线，键是命令、值是期望输出（让它先自证前提）",
                    "additionalProperties": {"type": "string"},
                },
                "notes": {
                    "type": "string",
                    "description": "额外约束/背景，别塞无关信息",
                },
                "max_turns": {
                    "type": "integer",
                    "description": "最大轮数，0 = 用设置里的强度预设",
                },
                "timeout_s": {
                    "type": "integer",
                    "description": "超时秒数，0 = 用设置默认（600）",
                },
            },
            "required": ["goal"],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            spec = DelegateSpec.from_dict(
                {
                    "goal": kwargs.get("goal") or "",
                    "mode": kwargs.get("mode") or "edit",
                    "scope": kwargs.get("scope") or (),
                    "forbidden": kwargs.get("forbidden") or (),
                    "dod": kwargs.get("dod") or (),
                    "current_state": kwargs.get("current_state") or {},
                    "notes": kwargs.get("notes") or "",
                    "max_turns": kwargs.get("max_turns") or 0,
                    "timeout_s": kwargs.get("timeout_s") or 0,
                }
            )
        except (ValueError, TypeError) as exc:
            return ToolResult(output=f"❌ 委派参数无法解析：{exc}", is_error=True)

        problems = spec.validate()
        if problems:
            return ToolResult(
                output="❌ 委派被拒绝，先补齐契约：\n- " + "\n- ".join(problems),
                is_error=True,
                metadata={"problems": problems},
            )

        cfg = QoderConfig.from_env(self.workspace)
        if not cfg.enabled:
            return ToolResult(
                output="❌ Qoder CLI 委派已在设置中关闭（QODER_ENABLED=false）。",
                is_error=True,
            )
        if spec.mode != "analyze" and not cfg.can_write:
            return ToolResult(
                output=(
                    "❌ 当前权限档位是「只读分析」，无法改文件。"
                    "请到 设置 → Qoder CLI 把权限调到「限定编辑」，或把 mode 改成 analyze。"
                ),
                is_error=True,
            )

        result = await delegate(spec, cfg)

        if result.status == "violated":
            head = (
                "❌ 改动越界，已判失败（未自动回滚）：\n"
                + "\n".join(f"- {p}" for p in result.scope_violations[:10])
                + "\n请回滚这些文件后自己处理。\n"
            )
            return ToolResult(
                output=head + result.for_model(),
                is_error=True,
                metadata={"status": result.status, "log": result.log_path,
                          "violations": result.scope_violations},
            )

        if result.status == "failed":
            return ToolResult(
                output="❌ 委派失败：" + (result.error or "未知原因") + "\n" + result.for_model(
                    limit=600
                ),
                is_error=True,
                metadata={"status": result.status, "log": result.log_path},
            )

        body = result.for_model()
        if result.status == "partial":
            # 不当作 is_error（改动确实发生了，不该触发回滚），但必须让人无法忽略：
            # 实测有模型看到 partial 仍直接宣布"任务已完成"。
            body = (
                "⚠️ 验收未全绿（partial）："
                + (result.error or "有 dod 未通过")
                + "\n改动已发生但不算完成——先看 checks 里的失败项，自己复核后再向用户报结论。\n"
                + body
            )

        return ToolResult(
            output=body,
            is_error=False,
            metadata={
                "status": result.status,
                "files_changed": result.files_changed,
                "log": result.log_path,
                "duration_s": round(result.duration_s, 1),
            },
        )

    @property
    def tags(self) -> tuple[str, ...]:
        return ("委派", "delegate", "qoder", "外部", "批量", "重构", "省 token")
