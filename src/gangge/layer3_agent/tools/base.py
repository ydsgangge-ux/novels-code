"""Base tool class — every tool extends this."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from gangge.layer3_agent.tools.validation import ToolInputError, validate_arguments
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

    @property
    def tags(self) -> tuple[str, ...]:
        """Search keywords beyond the name, used by ``tool_search``.

        Override to make a tool discoverable by capability ("image", "pdf",
        "单元测试") rather than by its exact name.
        """
        return ()

    def to_definition(self) -> ToolDefinition:
        """Convert to ToolDefinition for LLM API."""
        return ToolDefinition(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
        )

    def validate(self, arguments: dict[str, Any] | None) -> dict[str, Any]:
        """Validate arguments against ``input_schema`` and fill defaults.

        Raises :class:`ToolInputError` with a model-actionable message.
        """
        return validate_arguments(self.name, self.input_schema, arguments)

    async def safe_execute(self, **kwargs: Any) -> ToolResult:
        """Validate, execute, and never raise.

        Validation happens *here* rather than in the registry so that every
        caller — the registry, a plugin pipeline, a test — gets the same
        guarantee: a tool only ever runs with arguments matching its declared
        schema.
        """
        try:
            args = self.validate(kwargs)
        except ToolInputError as exc:
            return ToolResult(
                output=str(exc),
                is_error=True,
                metadata={"validation_errors": exc.problems},
            )
        try:
            return await self.execute(**args)
        except Exception as e:
            return ToolResult(
                output=f"工具执行错误: {type(e).__name__}: {e}",
                is_error=True,
            )
