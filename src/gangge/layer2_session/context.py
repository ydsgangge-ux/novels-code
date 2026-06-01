"""Context compressor — compress old messages to save tokens."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from gangge.layer5_llm.base import BaseLLM, Message, Role, ContentBlock, ContentType

logger = logging.getLogger(__name__)


@dataclass
class CompressionResult:
    """Result of context compression."""

    messages: list[Message]
    original_count: int
    compressed_count: int
    saved_tokens_estimate: int


class ContextCompressor:
    """Compress conversation history to manage token usage.

    Strategy: sliding window + LLM summary
    - Keep recent N messages intact
    - Summarize older messages into a single system-like message
    """

    def __init__(
        self,
        llm: BaseLLM | None = None,
        keep_recent: int = 10,
        max_context_messages: int = 50,
    ):
        self.llm = llm
        self.keep_recent = keep_recent
        self.max_context_messages = max_context_messages

    async def compress(self, messages: list[Message]) -> CompressionResult:
        """Compress messages if they exceed the threshold."""
        if len(messages) <= self.max_context_messages:
            return CompressionResult(
                messages=messages,
                original_count=len(messages),
                compressed_count=len(messages),
                saved_tokens_estimate=0,
            )

        original_count = len(messages)
        old_messages = messages[:-self.keep_recent]
        recent_messages = messages[-self.keep_recent:]

        if not self.llm:
            # No LLM available: just truncate
            return CompressionResult(
                messages=recent_messages,
                original_count=original_count,
                compressed_count=len(recent_messages),
                saved_tokens_estimate=original_count - self.keep_recent,
            )

        # Summarize old messages
        summary = await self._summarize(old_messages)

        if summary:
            summary_msg = Message(
                role=Role.USER,
                content=[ContentBlock(
                    type=ContentType.TEXT,
                    text=f"[之前的对话摘要]\n{summary}\n[摘要结束]",
                )],
            )
            compressed = [summary_msg] + recent_messages
        else:
            compressed = recent_messages

        return CompressionResult(
            messages=compressed,
            original_count=original_count,
            compressed_count=len(compressed),
            saved_tokens_estimate=original_count - len(compressed),
        )

    async def _summarize(self, messages: list[Message]) -> str:
        """Use LLM to summarize a list of messages."""
        # Build a compact text representation
        parts = []
        for msg in messages:
            role_label = {"user": "用户", "assistant": "助手", "tool": "工具"}.get(
                msg.role.value, msg.role.value
            )
            text = msg.get_text()[:500]  # Truncate long messages
            parts.append(f"{role_label}: {text}")

        conversation_text = "\n".join(parts)

        try:
            summary_messages = [
                Message(
                    role=Role.USER,
                    content=[ContentBlock(
                        type=ContentType.TEXT,
                        text=f"请将以下对话历史压缩成简洁的摘要（保留关键信息、工具调用结果、代码修改内容）:\n\n{conversation_text}",
                    )],
                )
            ]
            response = await self.llm.chat(messages=summary_messages)
            return response.text.strip()
        except Exception as e:
            logger.warning(f"Failed to summarize: {e}")
            return ""
