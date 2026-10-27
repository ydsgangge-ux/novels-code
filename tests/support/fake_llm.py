"""A scripted LLM adapter — the test double the agent loop was missing.

The loop's most important behaviour (does it call the tool? does it retry after
a failed call? does it stop when the todos are done?) lives in a 900-line method
that used to have no end-to-end test, because testing it meant calling a real
model. This adapter removes that excuse: you hand it a script of turns and it
plays them back, in streaming or blocking mode, including usage and tool calls.

Modelled on Codex's approach — every core test there runs the real core against
a fake model server, so the *engine* is under test rather than a mock of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from gangge.layer5_llm.base import (
    BaseLLM,
    ContentBlock,
    ContentType,
    LLMResponse,
    Message,
    ToolDefinition,
)


@dataclass
class Turn:
    """One scripted model response."""

    text: str = ""
    thinking: str = ""
    tool_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    usage: dict[str, int] = field(
        default_factory=lambda: {"input_tokens": 100, "output_tokens": 10}
    )

    @property
    def stop_reason(self) -> str:
        return "tool_use" if self.tool_calls else "end_turn"


def say(text: str, **kwargs: Any) -> Turn:
    return Turn(text=text, **kwargs)


def call(name: str, **arguments: Any) -> Turn:
    """A turn that calls exactly one tool."""
    return Turn(tool_calls=[(name, arguments)])


def calls(*pairs: tuple[str, dict[str, Any]]) -> Turn:
    """A turn that calls several tools in parallel."""
    return Turn(tool_calls=list(pairs))


class ScriptedLLM(BaseLLM):
    """Replays a fixed script; raises when the script runs out."""

    def __init__(
        self,
        script: list[Turn],
        model: str = "fake-model",
        supports_streaming: bool = True,
        chunk_size: int = 4,
        fail_with: BaseException | None = None,
    ):
        super().__init__(model=model)
        self.script = list(script)
        self.supports_streaming = supports_streaming
        self.chunk_size = chunk_size
        self.fail_with = fail_with
        self.requests: list[list[Message]] = []
        self.stream_calls = 0
        self.chat_calls = 0

    # -- helpers -----------------------------------------------

    def _next(self) -> Turn:
        if self.fail_with is not None:
            raise self.fail_with
        if not self.script:
            raise AssertionError(
                "ScriptedLLM ran out of scripted turns — the loop asked for more "
                "model responses than the test expected"
            )
        return self.script.pop(0)

    @staticmethod
    def _blocks(turn: Turn) -> list[ContentBlock]:
        blocks: list[ContentBlock] = []
        if turn.thinking:
            blocks.append(ContentBlock(type=ContentType.THINKING, text=turn.thinking))
        if turn.text:
            blocks.append(ContentBlock(type=ContentType.TEXT, text=turn.text))
        for index, (name, arguments) in enumerate(turn.tool_calls):
            blocks.append(
                ContentBlock(
                    type=ContentType.TOOL_USE,
                    tool_call_id=f"call_{index}",
                    tool_name=name,
                    tool_input=arguments,
                )
            )
        return blocks

    # -- BaseLLM -----------------------------------------------

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        system: str = "",
    ) -> LLMResponse:
        self.chat_calls += 1
        self.requests.append(list(messages))
        turn = self._next()
        return LLMResponse(
            content=self._blocks(turn),
            stop_reason=turn.stop_reason,
            usage=dict(turn.usage),
            model=self.model,
        )

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        system: str = "",
    ) -> AsyncIterator[ContentBlock]:
        self.stream_calls += 1
        self.requests.append(list(messages))
        turn = self._next()

        if turn.thinking:
            yield ContentBlock(type=ContentType.THINKING, text=turn.thinking)
        for start in range(0, len(turn.text), self.chunk_size):
            yield ContentBlock(
                type=ContentType.TEXT, text=turn.text[start : start + self.chunk_size]
            )
        for index, (name, arguments) in enumerate(turn.tool_calls):
            yield ContentBlock(
                type=ContentType.TOOL_USE,
                tool_call_id=f"call_{index}",
                tool_name=name,
                tool_input=arguments,
            )
        yield ContentBlock(type=ContentType.TEXT, text="", usage=dict(turn.usage))

    async def close(self) -> None:
        return None


class RecordingEmitter:
    """Collects what the loop pushed to the UI."""

    def __init__(self) -> None:
        self.blocks: list[ContentBlock] = []
        self.texts: list[str] = []

    async def __call__(self, block: ContentBlock) -> None:
        self.blocks.append(block)
        if block.type == ContentType.TEXT:
            self.texts.append(block.text)

    @property
    def text(self) -> str:
        return "".join(self.texts)

    def tool_names(self) -> list[str]:
        return [b.tool_name for b in self.blocks if b.type == ContentType.TOOL_USE]
