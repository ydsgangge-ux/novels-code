"""Anthropic Claude adapter.

Includes automatic retry with exponential backoff for rate limits (429)
and transient server errors (5xx).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator

import anthropic
from anthropic.types import Message as AnthropicMessage

from gangge.layer5_llm.base import (
    BaseLLM,
    ContentBlock,
    ContentType,
    LLMResponse,
    Message,
    Role,
    ToolDefinition,
)
from gangge.layer5_llm.thinking import plan_thinking

logger = logging.getLogger(__name__)

# ── Retry configuration ──
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class AnthropicLLM(BaseLLM):
    """Anthropic Claude LLM adapter."""

    # Every Claude 3+ model accepts image blocks.
    supports_vision = True

    def __init__(self, api_key: str, **kwargs: Any):
        super().__init__(**kwargs)
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.thinking_plan = plan_thinking(
            "anthropic", self.model, self.thinking_level, self.max_tokens
        )

    def _apply_thinking(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """扩展思考：thinking 与 temperature 必须一起设，否则 API 直接报错。"""
        plan = self.thinking_plan
        if plan.thinking:
            kwargs["thinking"] = plan.thinking
            kwargs["temperature"] = plan.temperature if plan.temperature is not None else 1.0
        elif self.temperature is not None:
            kwargs["temperature"] = self.temperature
        return kwargs

    def _convert_messages(
        self, messages: list[Message]
    ) -> list[dict[str, Any]]:
        result = []
        for msg in messages:
            if msg.role == Role.SYSTEM:
                continue  # handled separately
            if msg.role == Role.USER:
                content_parts = []
                for block in msg.content:
                    if block.type == ContentType.TEXT and block.text:
                        content_parts.append({"type": "text", "text": block.text})
                    elif block.type == ContentType.IMAGE and block.media_data:
                        content_parts.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": block.media_type,
                                "data": block.media_data,
                            },
                        })
                if not content_parts:
                    content_parts = [{"type": "text", "text": ""}]
                result.append({"role": "user", "content": content_parts})
                continue
            result.append(msg.to_dict())
        return result

    def _convert_tools(
        self, tools: list[ToolDefinition]
    ) -> list[dict[str, Any]]:
        return [t.to_anthropic_schema() for t in tools]

    @staticmethod
    def _system_param(system: str) -> list[dict[str, Any]]:
        """system 带上 cache_control：缓存断点覆盖它之前的全部内容。

        Anthropic 的缓存前缀顺序是 tools → system → messages，所以标在 system
        上就等于把"工具定义 + 静态 system"整块缓存住（每次请求都一模一样，
        正好是 loop 里刻意保持字节一致的那部分）。命中后这部分按约 1/10 计价，
        首 token 延迟也明显下降。静态段太短时 API 只是不缓存，不会报错。
        """
        return [{
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }]

    def _parse_response(self, raw: AnthropicMessage) -> LLMResponse:
        blocks = []
        for block in raw.content:
            if block.type == "text":
                blocks.append(ContentBlock(type=ContentType.TEXT, text=block.text))
            elif block.type == "tool_use":
                blocks.append(
                    ContentBlock(
                        type=ContentType.TOOL_USE,
                        tool_call_id=block.id,
                        tool_name=block.name,
                        tool_input=block.input,
                    )
                )
            elif block.type == "thinking":
                blocks.append(
                    ContentBlock(type=ContentType.THINKING, text=block.thinking)
                )

        stop_reason = raw.stop_reason or "end_turn"
        if stop_reason == "stop_sequence":
            stop_reason = "end_turn"

        # Anthropic 的口径与 OpenAI 不同：input_tokens **不含**缓存读写，
        # 两者是分开计费的字段。这里归一化成"总输入 + 其中命中缓存多少"，
        # 让上层的费用计算只认一套口径。
        raw_usage = raw.usage
        cache_read = int(getattr(raw_usage, "cache_read_input_tokens", 0) or 0)
        cache_write = int(getattr(raw_usage, "cache_creation_input_tokens", 0) or 0)
        usage = {
            "input_tokens": int(raw_usage.input_tokens or 0) + cache_read + cache_write,
            "output_tokens": raw_usage.output_tokens,
        }
        if cache_read:
            usage["cached_input_tokens"] = cache_read
        if cache_write:
            usage["cache_write_input_tokens"] = cache_write

        return LLMResponse(
            content=blocks,
            stop_reason=stop_reason,
            usage=usage,
            model=raw.model,
        )

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        system: str = "",
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": self._convert_messages(messages),
        }
        if system:
            kwargs["system"] = self._system_param(system)
        if tools:
            kwargs["tools"] = self._convert_tools(tools)
        self._apply_thinking(kwargs)

        # ── Retry with exponential backoff for 429/5xx ──
        last_error = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                raw = await self.client.messages.create(**kwargs)
                return self._parse_response(raw)
            except Exception as e:
                last_error = e

                # Check if this is a retryable error
                status_code = getattr(e, "status_code", None)
                if status_code is None and hasattr(e, "response"):
                    status_code = getattr(e.response, "status_code", None)

                is_retryable = status_code in RETRYABLE_STATUS_CODES

                if not is_retryable or attempt == MAX_RETRIES:
                    if is_retryable:
                        logger.error(
                            f"[LLM] Rate limit/server error after {MAX_RETRIES} retries: {status_code}"
                        )
                    raise

                delay = RETRY_BASE_DELAY * (2 ** attempt)
                jitter = (hash(str(time.time())) % 1000) / 1000.0
                total_delay = delay + jitter

                logger.warning(
                    f"[LLM] Retryable error (status={status_code}), "
                    f"retry {attempt + 1}/{MAX_RETRIES} in {total_delay:.1f}s"
                )
                await asyncio.sleep(total_delay)

        raise last_error

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        system: str = "",
    ) -> AsyncIterator[ContentBlock]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": self._convert_messages(messages),
        }
        if system:
            kwargs["system"] = self._system_param(system)
        if tools:
            kwargs["tools"] = self._convert_tools(tools)
        self._apply_thinking(kwargs)

        current_tool_id = ""
        current_tool_name = ""
        current_tool_input = ""
        usage: dict[str, int] = {}

        async with self.client.messages.stream(**kwargs) as stream:
            async for event in stream:
                if event.type == "message_start":
                    raw_usage = getattr(getattr(event, "message", None), "usage", None)
                    if raw_usage is not None:
                        usage["input_tokens"] = getattr(raw_usage, "input_tokens", 0) or 0
                elif event.type == "message_delta":
                    raw_usage = getattr(event, "usage", None)
                    if raw_usage is not None:
                        usage["output_tokens"] = getattr(raw_usage, "output_tokens", 0) or 0
                elif event.type == "content_block_start":
                    if hasattr(event.content_block, "type"):
                        if event.content_block.type == "tool_use":
                            current_tool_id = event.content_block.id
                            current_tool_name = event.content_block.name
                            current_tool_input = ""
                elif event.type == "content_block_delta":
                    if hasattr(event.delta, "type"):
                        if event.delta.type == "text_delta":
                            yield ContentBlock(
                                type=ContentType.TEXT, text=event.delta.text
                            )
                        elif event.delta.type == "thinking_delta":
                            yield ContentBlock(
                                type=ContentType.THINKING,
                                text=getattr(event.delta, "thinking", ""),
                            )
                        elif event.delta.type == "input_json_delta":
                            current_tool_input += event.delta.partial_json
                elif event.type == "content_block_stop":
                    if current_tool_name:
                        import json

                        try:
                            parsed_input = json.loads(current_tool_input)
                        except json.JSONDecodeError:
                            parsed_input = {}
                        yield ContentBlock(
                            type=ContentType.TOOL_USE,
                            tool_call_id=current_tool_id,
                            tool_name=current_tool_name,
                            tool_input=parsed_input,
                        )
                        current_tool_name = ""
                        current_tool_input = ""

        # Usage is reported out of band; a terminal empty TEXT delta is the only
        # channel the streaming contract has for it.
        if usage:
            yield ContentBlock(type=ContentType.TEXT, text="", usage=usage)

    async def close(self) -> None:
        await self.client.close()
