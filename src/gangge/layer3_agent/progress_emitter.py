"""
Gangge Code — 进度可视化事件系统
src/gangge/layer3_agent/progress_emitter.py

用法：
  - AgenticLoop 调用 emitter.emit(event_type, data) 发出事件
  - CLI / GUI 订阅事件并渲染
  这样 loop 核心逻辑和 UI 渲染完全解耦
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable


# ─────────────────────────────────────────
# 事件类型
# ─────────────────────────────────────────

class EventType(str, Enum):
    THINKING   = "thinking"    # LLM 正在生成（等待 API）
    PLANNING   = "planning"    # LLM 输出了规划内容
    TOOL_START = "tool_start"  # 开始调用工具
    TOOL_END   = "tool_end"    # 工具调用完成
    PROGRESS   = "progress"    # 任务进度更新（N/M 已完成）
    TEXT       = "text"        # LLM 普通文字输出
    WARNING    = "warning"     # 非致命警告（如强制重试）
    ERROR      = "error"       # 错误
    DONE       = "done"        # 任务完成
    ROUND      = "round"       # 开始新轮次


@dataclass
class ProgressEvent:
    type: EventType
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now().strftime("%H:%M:%S"))


# ─────────────────────────────────────────
# 事件发射器
# ─────────────────────────────────────────

class ProgressEmitter:
    """
    AgenticLoop 持有一个 emitter 实例。
    UI 层注册回调函数来接收事件。
    """

    def __init__(self):
        self._handlers: list[Callable[[ProgressEvent], None]] = []

    def subscribe(self, handler: Callable[[ProgressEvent], None]):
        self._handlers.append(handler)

    def emit(self, event_type: EventType | str, message: str, **data: Any):
        if isinstance(event_type, str):
            event_type = EventType(event_type)
        event = ProgressEvent(type=event_type, message=message, data=data)
        for handler in self._handlers:
            try:
                handler(event)
            except Exception:
                pass  # UI 渲染失败不能影响 loop 执行

    def emit_thinking(self, round_num: int):
        self.emit(EventType.THINKING, f"第 {round_num} 轮正在思考...")

    def emit_tool_start(self, tool_name: str, tool_input: dict):
        import json
        args_str = json.dumps(tool_input, ensure_ascii=False)[:60]
        self.emit(EventType.TOOL_START, f"调用 {tool_name}",
                  tool_name=tool_name, args_preview=args_str)

    def emit_tool_end(self, tool_name: str, success: bool, duration_ms: int = 0):
        self.emit(EventType.TOOL_END, f"{tool_name} 完成",
                  tool_name=tool_name, success=success, duration_ms=duration_ms)

    def emit_progress(self, current: int, total: int, step_name: str = ""):
        self.emit(EventType.PROGRESS, f"{current}/{total} {step_name}",
                  current=current, total=total, step_name=step_name)

    def emit_warning(self, message: str):
        self.emit(EventType.WARNING, message)

    def emit_done(self, total_steps: int = 0, files_created: list[str] | None = None):
        self.emit(EventType.DONE, "任务完成",
                  total_steps=total_steps, files_created=files_created or [])
