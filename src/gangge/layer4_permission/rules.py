"""Permission rules — user-configurable allow/deny patterns."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class RuleAction(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass
class PermissionRule:
    """A single permission rule."""

    action: RuleAction
    pattern: str
    description: str = ""
    scope: str = "bash"  # "bash" | "file_read" | "file_write" | "network"

    def matches(self, target: str) -> bool:
        try:
            return bool(re.search(self.pattern, target, re.IGNORECASE))
        except re.error:
            return False


# Default rules
DEFAULT_RULES: list[PermissionRule] = [
    # ── bash: auto-allow safe read operations ──
    PermissionRule(RuleAction.ALLOW, r"^cat\s", "cat 读取文件", "bash"),
    PermissionRule(RuleAction.ALLOW, r"^ls\b", "列出目录", "bash"),
    PermissionRule(RuleAction.ALLOW, r"^pwd$", "显示当前目录", "bash"),
    PermissionRule(RuleAction.ALLOW, r"^echo\b", "echo 输出", "bash"),
    PermissionRule(RuleAction.ALLOW, r"^git\s+(status|log|branch|diff|show)", "Git 只读命令", "bash"),
    PermissionRule(RuleAction.ALLOW, r"^(python|python3)\s+.*--version", "查看 Python 版本", "bash"),
    PermissionRule(RuleAction.ALLOW, r"^which\b", "查找命令位置", "bash"),
    PermissionRule(RuleAction.ALLOW, r"^(npm|pip)\s+list", "列出已安装包", "bash"),
    PermissionRule(RuleAction.ALLOW, r"^mkdir\b", "创建目录", "bash"),
    PermissionRule(RuleAction.ALLOW, r"^touch\b", "创建空文件", "bash"),
    PermissionRule(RuleAction.ALLOW, r"^(python|python3|pytest)\s+.*(-m\s+pytest|test)", "运行测试", "bash"),
    PermissionRule(RuleAction.ALLOW, r"^(pip|npm)\s+install\b", "安装依赖", "bash"),

    # ── bash: auto-deny dangerous operations ──
    PermissionRule(RuleAction.DENY, r"\brm\s+(-[rfRF]+\s+)?/", "禁止删除根目录", "bash"),
    PermissionRule(RuleAction.DENY, r":\s*\(\)\s*\{", "禁止 Fork 炸弹", "bash"),

    # ── file_read: auto-allow all reads ──
    PermissionRule(RuleAction.ALLOW, r".*", "自动允许读取文件", "file_read"),

    # ── file_write: auto-allow writes in workspace, deny system paths ──
    PermissionRule(RuleAction.DENY, r"^/etc/", "禁止写入系统配置目录", "file_write"),
    PermissionRule(RuleAction.DENY, r"^C:\\Windows\\", "禁止写入 Windows 系统目录", "file_write"),
    PermissionRule(RuleAction.ALLOW, r".*", "自动允许写入文件", "file_write"),

    # ── bash: everything else requires asking ──
    PermissionRule(RuleAction.ASK, r".*", "其他命令需要确认", "bash"),
]
