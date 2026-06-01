"""Layer 4 — Permission & Security Layer.

权限拦截、危险命令检测、规则引擎。
"""

from gangge.layer4_permission.guard import PermissionGuard, PermissionDecision
from gangge.layer4_permission.danger import DangerDetector

__all__ = ["PermissionGuard", "PermissionDecision", "DangerDetector"]
