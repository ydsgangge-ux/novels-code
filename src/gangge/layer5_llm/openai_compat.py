"""OpenAI-compatible adapter.

Supports OpenAI, DeepSeek, Ollama — any provider that implements
the OpenAI Chat Completions API.

Includes automatic retry with exponential backoff for rate limits (429)
and transient server errors (5xx).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator

from openai import AsyncOpenAI

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
RETRY_BASE_DELAY = 2.0  # seconds
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class OpenAICompatLLM(BaseLLM):
    """OpenAI-compatible LLM adapter (OpenAI / DeepSeek / Ollama)."""

    def __init__(self, base_url: str, api_key: str = "not-needed", **kwargs: Any):
        super().__init__(**kwargs)
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key)

    def _convert_messages(
        self, messages: list[Message]
    ) -> list[dict[str, Any]]:
        """Convert Gangge internal messages to OpenAI API format.

        OpenAI requires every assistant tool_calls to be immediately followed
        by tool messages with matching tool_call_ids.  This method also
        patches orphaned tool_calls with placeholder results so the API
        never rejects the request due to missing tool responses.
        """
        result: list[dict[str, Any]] = []

        for msg in messages:
            if msg.role == Role.SYSTEM:
                continue

            if msg.role == Role.TOOL:
                for block in msg.content:
                    cid = block.tool_call_id
                    if not cid:
                        continue
                    result.append({
                        "role": "tool",
                        "tool_call_id": cid,
                        "content": block.text or "",
                    })
                continue

            if msg.role == Role.USER:
                content_parts: list[dict[str, Any]] = []
                for block in msg.content:
                    if block.type == ContentType.TEXT and block.text:
                        content_parts.append({"type": "text", "text": block.text})
                    elif block.type == ContentType.IMAGE and block.media_data:
                        content_parts.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{block.media_type};base64,{block.media_data}",
                                "detail": "auto",
                            },
                        })
                if not content_parts:
                    content_parts = [{"type": "text", "text": ""}]
                result.append({"role": "user", "content": content_parts})
                continue

            if msg.role == Role.ASSISTANT:
                texts: list[str] = []
                tool_calls: list[dict[str, Any]] = []
                for block in msg.content:
                    if block.type in (ContentType.TEXT, ContentType.THINKING):
                        if block.text:
                            texts.append(block.text)
                    elif block.type == ContentType.TOOL_USE and block.tool_call_id:
                        tool_calls.append({
                            "id": block.tool_call_id,
                            "type": "function",
                            "function": {
                                "name": block.tool_name,
                                "arguments": json.dumps(block.tool_input, ensure_ascii=False),
                            },
                        })
                oa_msg: dict[str, Any] = {"role": "assistant"}
                if tool_calls:
                    oa_msg["tool_calls"] = tool_calls
                elif texts:
                    oa_msg["content"] = "".join(texts)
                else:
                    oa_msg["content"] = ""
                result.append(oa_msg)
                continue

        # ── Patch orphaned tool_calls ──────────────────────────────
        # Walk the converted list; whenever an assistant message has
        # tool_calls, verify that the very next messages are tool
        # results covering *every* call_id.  If any are missing, inject
        # a placeholder so the API never rejects the request.
        patched: list[dict[str, Any]] = []
        i = 0
        while i < len(result):
            patched.append(result[i])
            msg = result[i]

            if msg.get("role") == "assistant" and "tool_calls" in msg:
                expected_ids = {tc["id"] for tc in msg["tool_calls"]}
                answered_ids: set[str] = set()

                # Collect all immediately-following tool messages
                j = i + 1
                while j < len(result) and result[j].get("role") == "tool":
                    patched.append(result[j])
                    tid = result[j].get("tool_call_id", "")
                    if tid:
                        answered_ids.add(tid)
                    j += 1

                # Inject placeholders for any missing tool_call_ids
                missing = expected_ids - answered_ids
                for mid in missing:
                    patched.append({
                        "role": "tool",
                        "tool_call_id": mid,
                        "content": "[工具结果因历史截断而丢失]",
                    })
                    logger.warning(
                        "Patched orphaned tool_call %s with placeholder", mid
                    )

                i = j
            else:
                i += 1

        return patched

    def _convert_tools(
        self, tools: list[ToolDefinition]
    ) -> list[dict[str, Any]]:
        return [t.to_openai_schema() for t in tools]

    def _parse_response(self, raw: Any) -> LLMResponse:
        choice = raw.choices[0]
        blocks = []
        message = choice.message

        # ── Reasoning/thinking content (DeepSeek-R1, OpenAI o1, etc.) ──
        reasoning = getattr(message, "reasoning_content", None) or getattr(message, "reasoning", None)
        if reasoning:
            blocks.append(ContentBlock(type=ContentType.THINKING, text=reasoning))

        if message.content:
            blocks.append(ContentBlock(type=ContentType.TEXT, text=message.content))

        if hasattr(message, "tool_calls") and message.tool_calls:
            for tc in message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, TypeError):
                    args = {}
                blocks.append(
                    ContentBlock(
                        type=ContentType.TOOL_USE,
                        tool_call_id=tc.id,
                        tool_name=tc.function.name,
                        tool_input=args,
                    )
                )

        stop_reason = choice.finish_reason or "stop"
        stop_map = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}
        stop_reason = stop_map.get(stop_reason, "end_turn")

        usage = {}
        if raw.usage:
            usage = {
                "input_tokens": raw.usage.prompt_tokens or 0,
                "output_tokens": raw.usage.completion_tokens or 0,
            }

        return LLMResponse(
            content=blocks,
            stop_reason=stop_reason,
            usage=usage,
            model=raw.model or self.model,
        )

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        system: str = "",
    ) -> LLMResponse:
        converted = self._convert_messages(messages)
        if system:
            converted.insert(0, {"role": "system", "content": system})

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": converted,
        }
        if tools:
            kwargs["tools"] = self._convert_tools(tools)
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature

        # ── Retry with exponential backoff for 429/5xx ──
        last_error = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                raw = await self.client.chat.completions.create(**kwargs)
                return self._parse_response(raw)
            except Exception as e:
                last_error = e

                # Check if this is a retryable error
                status_code = getattr(e, "status_code", None)
                if status_code is None and hasattr(e, "response"):
                    status_code = getattr(e.response, "status_code", None)

                is_retryable = status_code in RETRYABLE_STATUS_CODES

                if not is_retryable or attempt == MAX_RETRIES:
                    # Non-retryable error or max retries exhausted
                    if is_retryable:
                        logger.error(
                            f"[LLM] Rate limit/server error after {MAX_RETRIES} retries: {status_code}"
                        )
                    raise

                # Exponential backoff with jitter
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                # Add jitter: random 0-1s
                jitter = (hash(str(time.time())) % 1000) / 1000.0
                total_delay = delay + jitter

                logger.warning(
                    f"[LLM] Retryable error (status={status_code}), "
                    f"retry {attempt + 1}/{MAX_RETRIES} in {total_delay:.1f}s"
                )
                await asyncio.sleep(total_delay)

        # Should not reach here, but just in case
        raise last_error

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        system: str = "",
    ) -> AsyncIterator[ContentBlock]:
        converted = self._convert_messages(messages)
        if system:
            converted.insert(0, {"role": "system", "content": system})

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": converted,
        }
        if tools:
            kwargs["tools"] = self._convert_tools(tools)
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature

        current_tool_id = ""
        current_tool_name = ""
        current_tool_input = ""

        stream = await self.client.chat.completions.create(**kwargs, stream=True)
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            if delta.content:
                yield ContentBlock(type=ContentType.TEXT, text=delta.content)

            # ── Reasoning/thinking content (DeepSeek-R1, etc.) ──
            reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
            if reasoning:
                yield ContentBlock(type=ContentType.THINKING, text=reasoning)

            if hasattr(delta, "tool_calls") and delta.tool_calls:
                for tc in delta.tool_calls:
                    if tc.id:
                        current_tool_id = tc.id
                    if tc.function and tc.function.name:
                        current_tool_name = tc.function.name
                    if tc.function and tc.function.arguments:
                        current_tool_input += tc.function.arguments

            finish = chunk.choices[0].finish_reason
            if finish == "tool_calls" and current_tool_name:
                try:
                    parsed = json.loads(current_tool_input)
                except json.JSONDecodeError:
                    parsed = {}
                yield ContentBlock(
                    type=ContentType.TOOL_USE,
                    tool_call_id=current_tool_id,
                    tool_name=current_tool_name,
                    tool_input=parsed,
                )
                current_tool_name = ""
                current_tool_input = ""

    async def close(self) -> None:
        await self.client.close()
