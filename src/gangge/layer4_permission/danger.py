"""Dangerous command / operation detection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class RiskLevel(str, Enum):
    SAFE = "safe"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class RiskAssessment:
    level: RiskLevel
    reason: str
    pattern_matched: str = ""


class DangerDetector:
    """Detect dangerous commands and operations."""

    # Critical: destructive, irreversible
    CRITICAL_PATTERNS: list[tuple[str, str]] = [
        (r"\brm\s+(-[rfRF]+\s+)?/\s*$", "删除根目录"),
        (r"\brm\s+(-[rfRF]+\s+)?\*+\s*$", "递归删除所有文件"),
        (r"\bmkfs\b", "格式化文件系统"),
        (r"\bdd\s+.*of=/dev/", "直接写块设备"),
        (r">\s*/dev/sd[a-z]", "覆盖磁盘分区"),
        (r"\bchmod\s+(-R\s+)?777\s+/\s*$", "开放根目录权限"),
        (r":\s*\(\)\s*\{\s*:\|:\s*&\s*\}\s*;", "Fork 炸弹"),
        (r"\bshred\b.*-z", "安全擦除文件"),
    ]

    # High: dangerous but scoped
    HIGH_PATTERNS: list[tuple[str, str]] = [
        (r"\brm\s+(-[rfRF]+\s+)", "递归删除"),
        (r"\bsudo\b\s", "提权执行"),
        (r"\bpython[23]?\s+.*-c\s+.*exec", "动态执行 Python 代码"),
        (r"\bcurl\b.*\|\s*(ba)?sh\b", "远程脚本管道执行"),
        (r"\bwget\b.*\|\s*(ba)?sh\b", "远程脚本管道执行"),
        (r"\bgit\s+push\s+.*--force\b", "强制推送"),
        (r"\bgit\s+reset\s+--hard\b", "硬重置 Git"),
        (r"\bdrop\s+(table|database)\b", "删除数据库/表"),
    ]

    # Medium: potentially risky
    MEDIUM_PATTERNS: list[tuple[str, str]] = [
        (r"\bnc\b\s+-[el]", "Netcat 监听"),
        (r"\bpython[23]?\s+-m\s+http\.server\b", "启动 HTTP 服务器"),
        (r"\bssh\b.*-R\b", "SSH 反向隧道"),
        (r"\bcurl\b.*-X\s+(POST|PUT|DELETE)", "发送写请求"),
        (r"\b_pip\s+install\b.*--(user|break-system)", "pip 全局安装"),
        (r"\bnpm\s+(install|run)\b", "npm 操作"),
        (r"\bdocker\s+(run|exec)\b", "Docker 执行"),
    ]

    # Low: suspicious but usually fine
    LOW_PATTERNS: list[tuple[str, str]] = [
        (r"\beval\b", "eval 执行"),
        (r"\bsource\b", "source 脚本"),
        (r"\bexec\b", "exec 替换进程"),
        (r"\bwget\b|\bcurl\b", "网络请求"),
    ]

    # Path patterns that should be restricted
    RESTRICTED_PATHS: list[tuple[str, str]] = [
        (r"/etc/(shadow|passwd|sudoers)", "系统关键文件"),
        (r"/etc/(ssh|ssl|pki)/", "系统安全配置"),
        (r"/boot/", "引导分区"),
        (r"/usr/(bin|lib|sbin)/", "系统二进制"),
        (r"C:\\Windows\\(System32|SysWOW64)\\", "Windows 系统目录"),
        (r"HKEY_", "Windows 注册表"),
    ]

    def assess_command(self, command: str) -> RiskAssessment:
        """Assess the risk level of a shell command."""
        command_stripped = command.strip()

        for pattern, reason in self.CRITICAL_PATTERNS:
            if re.search(pattern, command_stripped, re.IGNORECASE):
                return RiskAssessment(RiskLevel.CRITICAL, reason, pattern)

        for pattern, reason in self.HIGH_PATTERNS:
            if re.search(pattern, command_stripped, re.IGNORECASE):
                return RiskAssessment(RiskLevel.HIGH, reason, pattern)

        for pattern, reason in self.MEDIUM_PATTERNS:
            if re.search(pattern, command_stripped, re.IGNORECASE):
                return RiskAssessment(RiskLevel.MEDIUM, reason, pattern)

        for pattern, reason in self.LOW_PATTERNS:
            if re.search(pattern, command_stripped, re.IGNORECASE):
                return RiskAssessment(RiskLevel.LOW, reason, pattern)

        return RiskAssessment(RiskLevel.SAFE, "")

    def assess_path(self, path: str, operation: str = "read") -> RiskAssessment:
        """Assess the risk level of a file path operation."""
        for pattern, reason in self.RESTRICTED_PATHS:
            if re.search(pattern, path, re.IGNORECASE):
                if operation == "write":
                    return RiskAssessment(RiskLevel.CRITICAL, f"写入{reason}: {path}")
                return RiskAssessment(RiskLevel.HIGH, f"读取{reason}: {path}")

        return RiskAssessment(RiskLevel.SAFE, "")

    def is_dangerous(self, command: str) -> bool:
        """Quick check if a command is dangerous (HIGH or above)."""
        assessment = self.assess_command(command)
        return assessment.level in (RiskLevel.HIGH, RiskLevel.CRITICAL)
