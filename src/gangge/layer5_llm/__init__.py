"""Layer 5 — LLM Adapter Layer.

统一抽象所有 LLM Provider，屏蔽不同 SDK 的差异。
"""

from gangge.layer5_llm.base import (
    BaseLLM,
    Message,
    Role,
    ContentType,
    ContentBlock,
    ToolDefinition,
    ToolCall,
    LLMResponse,
)
from gangge.layer5_llm.registry import create_llm

__all__ = [
    "BaseLLM",
    "Message",
    "Role",
    "ContentType",
    "ContentBlock",
    "ToolDefinition",
    "ToolCall",
    "LLMResponse",
    "create_llm",
]
