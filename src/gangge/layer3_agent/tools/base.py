"""Base tool class — every tool extends this."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from gangge.layer5_llm.base import ToolDefinition


@dataclass
class ToolResult:
    """Result of a tool execution."""

    output: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseTool(ABC):
    """Abstract base for all tools.

    Subclasses must define: name, description, input_schema, execute().
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique tool name (used by LLM to call this tool)."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description for the LLM."""
        ...

    @property
    @abstractmethod
    def input_schema(self) -> dict[str, Any]:
        """JSON Schema for tool input parameters."""
        ...

    @abstractmethod
    async def execute(self, **kwargs: Any) -> ToolResult:
        """Execute the tool with given parameters."""
        ...

    def to_definition(self) -> ToolDefinition:
        """Convert to ToolDefinition for LLM API."""
        return ToolDefinition(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
        )

    async def safe_execute(self, **kwargs: Any) -> ToolResult:
        """Execute with automatic error handling."""
        try:
            return await self.execute(**kwargs)
        except Exception as e:
            return ToolResult(
                output=f"工具执行错误: {type(e).__name__}: {e}",
                is_error=True,
            )
