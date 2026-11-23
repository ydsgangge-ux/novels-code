"""Base LLM abstraction — all providers must implement this interface."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Awaitable, Callable


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ContentType(str, Enum):
    TEXT = "text"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    THINKING = "thinking"
    IMAGE = "image"


@dataclass
class ContentBlock:
    """A single content block in a message.

    ``usage`` is only meaningful on streaming deltas: providers report token
    counts out-of-band (the final SSE frame, ``message_delta``), and there is no
    other place to carry them without inventing a second event type.
    """

    type: ContentType
    text: str = ""
    tool_name: str = ""
    tool_call_id: str = ""
    tool_input: dict[str, Any] = field(default_factory=dict)
    is_error: bool = False
    media_type: str = ""
    media_data: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    #: True = 流式增量片段（只用于"实时思考"窗口）；
    #: False = 本次调用结束后的完整块（用于主界面的折叠卡片）。
    partial: bool = False
    #: 只用于流式末帧：服务端返回的结束原因。``"length"`` 表示被 max_tokens
    #: 截断——流式只有 ContentBlock 一种载体，不在这里带出来，调用方就只能把
    #: "想到一半被截断"当成"模型自己说完了"，既丢上下文又白花 token。
    stop_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.type.value}
        if self.type == ContentType.TEXT:
            d["text"] = self.text
        elif self.type == ContentType.TOOL_USE:
            d["id"] = self.tool_call_id
            d["name"] = self.tool_name
            d["input"] = self.tool_input
        elif self.type == ContentType.TOOL_RESULT:
            d["tool_use_id"] = self.tool_call_id
            d["content"] = self.text
            if self.is_error:
                d["is_error"] = True
        elif self.type == ContentType.THINKING:
            d["thinking"] = self.text
        elif self.type == ContentType.IMAGE:
            d["source"] = {
                "type": "base64",
                "media_type": self.media_type,
                "data": self.media_data,
            }
        return d


@dataclass
class Message:
    """A chat message."""

    role: Role
    content: list[ContentBlock] = field(default_factory=list)

    def __post_init__(self):
        if isinstance(self.content, str):
            self.content = [ContentBlock(type=ContentType.TEXT, text=self.content)]

    def add_text(self, text: str) -> None:
        self.content.append(ContentBlock(type=ContentType.TEXT, text=text))

    def add_tool_use(self, name: str, call_id: str, input: dict) -> None:
        self.content.append(
            ContentBlock(
                type=ContentType.TOOL_USE,
                tool_name=name,
                tool_call_id=call_id,
                tool_input=input,
            )
        )

    def add_tool_result(self, call_id: str, result: str, is_error: bool = False) -> None:
        self.content.append(
            ContentBlock(
                type=ContentType.TOOL_RESULT,
                tool_call_id=call_id,
                text=result,
                is_error=is_error,
            )
        )

    def get_text(self) -> str:
        return "".join(b.text for b in self.content if b.type == ContentType.TEXT)

    def to_dict(self) -> dict[str, Any]:
        if self.role == Role.TOOL:
            return {
                "role": "user",
                "content": [b.to_dict() for b in self.content],
            }
        return {
            "role": self.role.value,
            "content": [b.to_dict() for b in self.content],
        }


@dataclass
class ToolDefinition:
    """Schema definition for a tool."""

    name: str
    description: str
    input_schema: dict[str, Any]

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }

    def to_anthropic_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass
class ToolCall:
    """A tool call from the LLM."""

    id: str
    name: str
    input: dict[str, Any]


@dataclass
class LLMResponse:
    """Response from the LLM."""

    content: list[ContentBlock]
    stop_reason: str  # "end_turn" | "tool_use" | "max_tokens"
    usage: dict[str, int] = field(default_factory=dict)
    model: str = ""

    @property
    def tool_calls(self) -> list[ToolCall]:
        return [
            ToolCall(id=b.tool_call_id, name=b.tool_name, input=b.tool_input)
            for b in self.content
            if b.type == ContentType.TOOL_USE
        ]

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.content if b.type == ContentType.TEXT)


class BaseLLM(ABC):
    """Abstract base class for all LLM providers."""

    # Default context window — subclasses should override
    CONTEXT_WINDOW_TOKENS: int = 128_000

    # Whether ``stream()`` is a real incremental stream. A provider that can
    # only answer in one shot must set this to False, and callers then keep
    # using ``chat()`` — pretending to stream a finished answer is what makes
    # the UI feel worse than honest blocking output.
    supports_streaming: bool = True

    # Whether the model accepts ``ContentType.IMAGE`` blocks. Defaults to False
    # so an unknown model fails loudly with "configure VISION_MODEL" instead of
    # sending an image block the provider answers with a 400.
    supports_vision: bool = False

    def __init__(
        self,
        model: str,
        max_tokens: int = 8192,
        temperature: float = 0.0,
        thinking_level: str = "",
        **kwargs: Any,
    ):
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.kwargs = kwargs

        # 思考强度：显式参数 > LLM_THINKING_LEVEL > 旧的 LLM_REASONING_EFFORT。
        # 只做归一化，具体怎么落到请求参数由各适配器按厂商决定
        # （见 layer5_llm/thinking.py），这里不假设任何厂商的字段名。
        from gangge.layer5_llm.thinking import normalize_level

        self.thinking_level = normalize_level(
            thinking_level
            or os.environ.get("LLM_THINKING_LEVEL", "")
            or os.environ.get("LLM_REASONING_EFFORT", "")
        )

    @property
    def max_context_tokens(self) -> int:
        """Maximum context window size in tokens."""
        return self.CONTEXT_WINDOW_TOKENS

    @abstractmethod
    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        system: str = "",
    ) -> LLMResponse:
        """Send messages and get a complete response."""
        ...

    @abstractmethod
    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        system: str = "",
    ) -> AsyncIterator[ContentBlock]:
        """Stream response content blocks."""
        ...

    async def chat_stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        system: str = "",
        on_delta: Callable[[ContentBlock], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        """Stream, forward each delta, and still return a complete response.

        This is the bridge that lets a streaming UI and a tool-calling loop share
        one code path: the loop needs the assembled ``LLMResponse`` (tool calls,
        stop reason, usage), while the UI needs the deltas as they arrive.

        Aggregation rules:
        * text deltas are concatenated into one TEXT block
        * thinking deltas into one THINKING block, placed before the text
        * TOOL_USE blocks are passed through in arrival order
        * ``usage`` is merged from any delta that carries it
        """
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_blocks: list[ContentBlock] = []
        usage: dict[str, int] = {}
        reported_stop = ""

        async for delta in self.stream(messages, tools=tools, system=system):
            if delta.usage:
                usage.update(delta.usage)
            if delta.stop_reason:
                # 末帧的 finish_reason（如被 max_tokens 截断）优先于推断值。
                reported_stop = delta.stop_reason
            if delta.type == ContentType.TEXT:
                if not delta.text:
                    continue
                text_parts.append(delta.text)
                if on_delta is not None:
                    await on_delta(delta)
            elif delta.type == ContentType.THINKING:
                if not delta.text:
                    continue
                thinking_parts.append(delta.text)
                if on_delta is not None:
                    await on_delta(delta)
            elif delta.type == ContentType.TOOL_USE:
                tool_blocks.append(delta)

        content: list[ContentBlock] = []
        if thinking_parts:
            content.append(
                ContentBlock(type=ContentType.THINKING, text="".join(thinking_parts))
            )
        if text_parts:
            content.append(ContentBlock(type=ContentType.TEXT, text="".join(text_parts)))
        content.extend(tool_blocks)

        return LLMResponse(
            content=content,
            stop_reason=reported_stop or ("tool_use" if tool_blocks else "end_turn"),
            usage=usage,
            model=self.model,
        )

    async def close(self) -> None:
        """Clean up resources."""
        pass
