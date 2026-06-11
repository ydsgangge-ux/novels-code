"""Base LLM abstraction — all providers must implement this interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator


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
    """A single content block in a message."""

    type: ContentType
    text: str = ""
    tool_name: str = ""
    tool_call_id: str = ""
    tool_input: dict[str, Any] = field(default_factory=dict)
    is_error: bool = False
    media_type: str = ""
    media_data: str = ""

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

    def __init__(
        self,
        model: str,
        max_tokens: int = 8192,
        temperature: float = 0.0,
        **kwargs: Any,
    ):
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.kwargs = kwargs

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

    async def close(self) -> None:
        """Clean up resources."""
        pass
