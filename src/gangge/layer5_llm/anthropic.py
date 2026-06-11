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

logger = logging.getLogger(__name__)

# ── Retry configuration ──
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class AnthropicLLM(BaseLLM):
    """Anthropic Claude LLM adapter."""

    def __init__(self, api_key: str, **kwargs: Any):
        super().__init__(**kwargs)
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

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

        return LLMResponse(
            content=blocks,
            stop_reason=stop_reason,
            usage={
                "input_tokens": raw.usage.input_tokens,
                "output_tokens": raw.usage.output_tokens,
            },
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
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = self._convert_tools(tools)
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature

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
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = self._convert_tools(tools)
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature

        current_tool_id = ""
        current_tool_name = ""
        current_tool_input = ""

        async with self.client.messages.stream(**kwargs) as stream:
            async for event in stream:
                if event.type == "content_block_start":
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

    async def close(self) -> None:
        await self.client.close()
