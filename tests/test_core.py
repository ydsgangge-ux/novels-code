"""Gangge Code core module tests.

Tests:
1. AgenticLoop end-to-end (write_file, multi-tool, error feedback)
2. BashTool Windows compatibility
3. Message chain integrity (ContextCompressor / _trim_history)
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

from gangge.layer5_llm.base import (
    BaseLLM,
    ContentBlock,
    ContentType,
    LLMResponse,
    Message,
    Role,
    ToolCall,
    ToolDefinition,
)


# ═══════════════════════════════════════════════════════════════
#  MockLLM: simulate LLM returning preset response sequences
# ═══════════════════════════════════════════════════════════════


class MockLLM(BaseLLM):
    """Return preset responses in order, implementing BaseLLM.chat().

    After all responses are consumed, returns an empty end_turn response.
    """

    def __init__(self, responses: list[LLMResponse]):
        super().__init__(model="mock", max_tokens=4096)
        self._responses = list(responses)
        self._index = 0
        self.call_count = 0
        self.received_messages: list[list[Message]] = []

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        system: str = "",
    ) -> LLMResponse:
        self.call_count += 1
        self.received_messages.append(list(messages))

        if self._index < len(self._responses):
            resp = self._responses[self._index]
            self._index += 1
            return resp

        return LLMResponse(
            content=[ContentBlock(type=ContentType.TEXT, text="任务完成。")],
            stop_reason="end_turn",
        )

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        system: str = "",
    ) -> AsyncIterator[ContentBlock]:
        resp = await self.chat(messages, tools, system)
        for block in resp.content:
            yield block


def _make_tool_use_response(
    text: str = "",
    tool_calls: list[tuple[str, str, dict]] | None = None,
    stop_reason: str = "tool_use",
) -> LLMResponse:
    """Helper to build an LLMResponse with tool calls.

    tool_calls: list of (tool_id, tool_name, input_dict)
    """
    blocks: list[ContentBlock] = []
    if text:
        blocks.append(ContentBlock(type=ContentType.TEXT, text=text))
    for tid, tname, tinput in (tool_calls or []):
        blocks.append(ContentBlock(
            type=ContentType.TOOL_USE,
            tool_call_id=tid,
            tool_name=tname,
            tool_input=tinput,
        ))
    return LLMResponse(content=blocks, stop_reason=stop_reason)


def _make_text_response(text: str) -> LLMResponse:
    """Helper to build a pure text LLMResponse."""
    return LLMResponse(
        content=[ContentBlock(type=ContentType.TEXT, text=text)],
        stop_reason="end_turn",
    )


# ═══════════════════════════════════════════════════════════════
#  Test 1: AgenticLoop write_file end-to-end
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_loop_write_file():
    """Verify AgenticLoop correctly:
    1. Calls write_file tool after receiving task
    2. Continues loop after tool execution
    3. Exits when LLM returns end_turn
    4. File is actually created in workspace
    """
    from gangge.layer3_agent.loop import AgenticLoop, LoopConfig
    from gangge.layer3_agent.tools.file_ops import WriteFileTool
    from gangge.layer3_agent.tools.registry import ToolRegistry
    from gangge.layer4_permission.guard import PermissionDecision, PermissionGuard, PermissionRequest

    with tempfile.TemporaryDirectory() as tmpdir:
        mock_llm = MockLLM([
            _make_tool_use_response(
                text="好的，我来创建 hello.txt。",
                tool_calls=[("tool_001", "write_file", {
                    "path": "hello.txt", "content": "hello world",
                })],
            ),
            _make_text_response("文件已创建完成。"),
        ])

        async def allow_all(req: PermissionRequest) -> PermissionDecision:
            return PermissionDecision.ALLOW

        registry = ToolRegistry()
        registry.register(WriteFileTool(workspace=tmpdir))

        config = LoopConfig(
            max_tool_rounds=10,
            workspace_dir=tmpdir,
            system_prompt="你是编程助手。",
            enable_sliding_window=False,
            enable_summary_compression=False,
        )
        loop = AgenticLoop(
            llm=mock_llm,
            tools=registry,
            permission_guard=PermissionGuard(ask_callback=allow_all),
            config=config,
        )

        messages = [Message(
            role=Role.USER,
            content=[ContentBlock(type=ContentType.TEXT, text="创建一个叫 hello.txt 的文件，内容是 hello world")],
        )]
        result = await loop.run(messages)

        hello_file = Path(tmpdir) / "hello.txt"
        assert hello_file.exists(), "hello.txt should be created"
        assert hello_file.read_text(encoding="utf-8") == "hello world"

        assert mock_llm.call_count >= 2, f"Expected >= 2 calls, got {mock_llm.call_count}"

        assert len(result.tool_executions) == 1
        assert result.tool_executions[0].tool_name == "write_file"
        assert not result.tool_executions[0].is_error


# ═══════════════════════════════════════════════════════════════
#  Test 2: Multiple tool calls in one round
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_loop_multiple_tools():
    """Verify that when LLM returns multiple tool calls in one round,
    AgenticLoop executes all of them before the next round.
    """
    from gangge.layer3_agent.loop import AgenticLoop, LoopConfig
    from gangge.layer3_agent.tools.file_ops import WriteFileTool
    from gangge.layer3_agent.tools.registry import ToolRegistry
    from gangge.layer4_permission.guard import PermissionDecision, PermissionGuard, PermissionRequest

    with tempfile.TemporaryDirectory() as tmpdir:
        mock_llm = MockLLM([
            _make_tool_use_response(
                text="创建两个文件。",
                tool_calls=[
                    ("t001", "write_file", {"path": "a.txt", "content": "AAA"}),
                    ("t002", "write_file", {"path": "b.txt", "content": "BBB"}),
                ],
            ),
            _make_text_response("两个文件都创建完了。"),
        ])

        async def allow_all(req): return PermissionDecision.ALLOW

        registry = ToolRegistry()
        registry.register(WriteFileTool(workspace=tmpdir))

        config = LoopConfig(
            max_tool_rounds=10,
            workspace_dir=tmpdir,
            system_prompt="你是编程助手。",
            enable_sliding_window=False,
            enable_summary_compression=False,
        )
        loop = AgenticLoop(
            llm=mock_llm,
            tools=registry,
            permission_guard=PermissionGuard(ask_callback=allow_all),
            config=config,
        )

        messages = [Message(
            role=Role.USER,
            content=[ContentBlock(type=ContentType.TEXT, text="创建 a.txt 和 b.txt")],
        )]
        result = await loop.run(messages)

        assert (Path(tmpdir) / "a.txt").read_text(encoding="utf-8") == "AAA"
        assert (Path(tmpdir) / "b.txt").read_text(encoding="utf-8") == "BBB"
        assert len(result.tool_executions) == 2


# ═══════════════════════════════════════════════════════════════
#  Test 3: Tool error feedback to LLM
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_loop_tool_error_feedback():
    """Verify that when a tool execution fails, the error is sent back
    to the LLM as a tool_result (is_error=True), not crashing the loop.
    """
    from gangge.layer3_agent.loop import AgenticLoop, LoopConfig
    from gangge.layer3_agent.tools.bash import BashTool
    from gangge.layer3_agent.tools.registry import ToolRegistry
    from gangge.layer4_permission.guard import PermissionDecision, PermissionGuard

    with tempfile.TemporaryDirectory() as tmpdir:
        mock_llm = MockLLM([
            _make_tool_use_response(
                text="我来运行这个命令。",
                tool_calls=[("t001", "bash", {
                    "command": "nonexistent_command_xyz_12345",
                })],
            ),
            _make_text_response("命令不存在，无法执行。"),
        ])

        async def allow_all(req): return PermissionDecision.ALLOW

        registry = ToolRegistry()
        registry.register(BashTool(workspace=tmpdir))

        config = LoopConfig(
            max_tool_rounds=10,
            workspace_dir=tmpdir,
            system_prompt="你是编程助手。",
            enable_sliding_window=False,
            enable_summary_compression=False,
        )
        loop = AgenticLoop(
            llm=mock_llm,
            tools=registry,
            permission_guard=PermissionGuard(ask_callback=allow_all),
            config=config,
        )

        messages = [Message(
            role=Role.USER,
            content=[ContentBlock(type=ContentType.TEXT, text="运行 nonexistent_command_xyz_12345")],
        )]
        result = await loop.run(messages)

        # The loop should complete without raising
        assert result is not None

        # The LLM should have received tool error results in the second call
        if len(mock_llm.received_messages) >= 2:
            second_call_messages = mock_llm.received_messages[1]
            tool_results = [
                m for m in second_call_messages
                if m.role == Role.TOOL
            ]
            assert len(tool_results) >= 1, "LLM second call should receive tool error result"


# ═══════════════════════════════════════════════════════════════
#  Test 4: BashTool Windows compatibility
# ═══════════════════════════════════════════════════════════════


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
class TestBashWindowsCompat:

    @pytest.mark.asyncio
    async def test_bash_chinese_path(self, tmp_path: Path):
        """Chinese path should not produce garbled output."""
        from gangge.layer3_agent.tools.bash import BashTool

        chinese_dir = tmp_path / "测试目录"
        chinese_dir.mkdir()

        tool = BashTool(workspace=str(chinese_dir))
        result = await tool.execute(command="echo hello")

        assert "hello" in result.output, f"Expected hello, got: {result.output}"
        assert "\ufffd" not in result.output, "Output contains garbled characters"

    @pytest.mark.asyncio
    async def test_bash_timeout(self, tmp_path: Path):
        """Long-running commands should be killed within timeout."""
        from gangge.layer3_agent.tools.bash import BashTool

        tool = BashTool(workspace=str(tmp_path))
        start = time.monotonic()
        result = await tool.execute(command="ping -n 30 127.0.0.1", timeout=3)
        elapsed = time.monotonic() - start

        assert elapsed < 8, f"Timeout not working, waited {elapsed:.1f}s"
        assert result.is_error or "超时" in result.output or elapsed < 8

    @pytest.mark.asyncio
    async def test_bash_noninteractive(self, tmp_path: Path):
        """PowerShell should not pop up interactive prompts."""
        from gangge.layer3_agent.tools.bash import BashTool

        tool = BashTool(workspace=str(tmp_path))
        result = await tool.execute(command="Read-Host '请输入'", timeout=5)

        assert result is not None
        assert result.output is not None

    @pytest.mark.asyncio
    async def test_bash_cwd_locked(self, tmp_path: Path):
        """Files should be created in workspace regardless of cd commands."""
        from gangge.layer3_agent.tools.bash import BashTool

        workspace = tmp_path / "myproject"
        workspace.mkdir()

        tool = BashTool(workspace=str(workspace))
        await tool.execute(command="New-Item -ItemType File -Name test_lock.txt")

        assert (workspace / "test_lock.txt").exists(), \
            "File should be created inside workspace"


# ═══════════════════════════════════════════════════════════════
#  Test 5: Message chain integrity (_trim_history)
# ═══════════════════════════════════════════════════════════════


class TestMessageChainIntegrity:

    def _make_messages_with_tool_calls(self, n_rounds: int = 6) -> list[Message]:
        """Generate n rounds of messages containing tool calls."""
        messages: list[Message] = [
            Message(role=Role.USER, content=[
                ContentBlock(type=ContentType.TEXT, text="初始任务"),
            ]),
        ]
        for i in range(n_rounds):
            tool_id = f"tool_{i:03d}"
            # assistant message with tool_use
            messages.append(Message(role=Role.ASSISTANT, content=[
                ContentBlock(type=ContentType.TEXT, text=f"第{i+1}轮"),
                ContentBlock(
                    type=ContentType.TOOL_USE,
                    tool_call_id=tool_id,
                    tool_name="write_file",
                    tool_input={"path": f"file_{i}.txt"},
                ),
            ]))
            # tool result
            messages.append(Message(role=Role.TOOL, content=[
                ContentBlock(
                    type=ContentType.TOOL_RESULT,
                    tool_call_id=tool_id,
                    text=f"已写入 file_{i}.txt",
                ),
            ]))
            # user follow-up
            if i < n_rounds - 1:
                messages.append(Message(role=Role.USER, content=[
                    ContentBlock(type=ContentType.TEXT, text=f"继续第{i+2}步"),
                ]))
        return messages

    def _validate_pairs(self, messages: list[Message]) -> tuple[bool, str]:
        """Verify every tool_use has a matching tool_result and vice versa."""
        tool_use_ids: set[str] = set()
        tool_result_ids: set[str] = set()

        for msg in messages:
            for block in msg.content:
                if block.type == ContentType.TOOL_USE:
                    tool_use_ids.add(block.tool_call_id)
                elif block.type == ContentType.TOOL_RESULT:
                    tool_result_ids.add(block.tool_call_id)

        orphan_uses = tool_use_ids - tool_result_ids
        orphan_results = tool_result_ids - tool_use_ids

        if orphan_uses:
            return False, f"tool_use without result: {orphan_uses}"
        if orphan_results:
            return False, f"tool_result without use: {orphan_results}"
        return True, "OK"

    def test_trim_history_preserves_tool_pairs(self):
        """Sliding window must not split tool_use/tool_result pairs."""
        from gangge.layer3_agent.loop import AgenticLoop, LoopConfig
        from gangge.layer3_agent.tools.registry import ToolRegistry
        from gangge.layer4_permission.guard import PermissionGuard

        messages = self._make_messages_with_tool_calls(n_rounds=10)
        assert len(messages) > 5

        config = LoopConfig(
            max_tool_rounds=10,
            workspace_dir=".",
            enable_sliding_window=True,
            max_history_rounds=4,
        )
        loop = AgenticLoop(
            llm=MockLLM([]),
            tools=ToolRegistry(),
            permission_guard=PermissionGuard(),
            config=config,
        )

        trimmed = loop._trim_history(messages)

        is_valid, error = self._validate_pairs(trimmed)
        assert is_valid, f"Trimming broke tool pairs: {error}"

    def test_trim_history_never_starts_with_orphan_tool_result(self):
        """After trimming, the first message must not be an orphan tool_result."""
        from gangge.layer3_agent.loop import AgenticLoop, LoopConfig
        from gangge.layer3_agent.tools.registry import ToolRegistry
        from gangge.layer4_permission.guard import PermissionGuard

        messages = self._make_messages_with_tool_calls(n_rounds=8)

        config = LoopConfig(
            max_tool_rounds=10,
            workspace_dir=".",
            enable_sliding_window=True,
            max_history_rounds=3,
        )
        loop = AgenticLoop(
            llm=MockLLM([]),
            tools=ToolRegistry(),
            permission_guard=PermissionGuard(),
            config=config,
        )

        trimmed = loop._trim_history(messages)

        if trimmed:
            first = trimmed[0]
            assert first.role != Role.TOOL, \
                "First message after trim must not be an orphan tool_result"

    def test_trim_history_short_messages_unchanged(self):
        """Short conversations (within max_history_rounds) should not be trimmed."""
        from gangge.layer3_agent.loop import AgenticLoop, LoopConfig
        from gangge.layer3_agent.tools.registry import ToolRegistry
        from gangge.layer4_permission.guard import PermissionGuard

        messages = [
            Message(role=Role.USER, content=[
                ContentBlock(type=ContentType.TEXT, text="你好"),
            ]),
            Message(role=Role.ASSISTANT, content=[
                ContentBlock(type=ContentType.TEXT, text="你好！"),
            ]),
        ]

        config = LoopConfig(
            max_tool_rounds=10,
            workspace_dir=".",
            enable_sliding_window=True,
            max_history_rounds=6,
        )
        loop = AgenticLoop(
            llm=MockLLM([]),
            tools=ToolRegistry(),
            permission_guard=PermissionGuard(),
            config=config,
        )

        result = loop._trim_history(messages)
        assert len(result) == len(messages), "Short conversation should not be trimmed"

    def test_trim_history_empty_messages(self):
        """Empty message list should not crash."""
        from gangge.layer3_agent.loop import AgenticLoop, LoopConfig
        from gangge.layer3_agent.tools.registry import ToolRegistry
        from gangge.layer4_permission.guard import PermissionGuard

        config = LoopConfig(
            max_tool_rounds=10,
            workspace_dir=".",
            enable_sliding_window=True,
        )
        loop = AgenticLoop(
            llm=MockLLM([]),
            tools=ToolRegistry(),
            permission_guard=PermissionGuard(),
            config=config,
        )

        result = loop._trim_history([])
        assert result == []


# ═══════════════════════════════════════════════════════════════
#  Test 6: ContextCompressor preserves tool pairs
# ═══════════════════════════════════════════════════════════════


class TestContextCompressorIntegrity:

    @pytest.mark.asyncio
    async def test_compress_preserves_tool_pairs(self):
        """Compression must not split tool_use/tool_result pairs."""
        from gangge.layer2_session.context import ContextCompressor

        messages: list[Message] = [
            Message(role=Role.USER, content=[
                ContentBlock(type=ContentType.TEXT, text="初始任务"),
            ]),
        ]
        for i in range(15):
            tid = f"tool_{i:03d}"
            messages.append(Message(role=Role.ASSISTANT, content=[
                ContentBlock(type=ContentType.TEXT, text=f"第{i+1}轮"),
                ContentBlock(
                    type=ContentType.TOOL_USE,
                    tool_call_id=tid,
                    tool_name="write_file",
                    tool_input={"path": f"f{i}.txt"},
                ),
            ]))
            messages.append(Message(role=Role.TOOL, content=[
                ContentBlock(
                    type=ContentType.TOOL_RESULT,
                    tool_call_id=tid,
                    text=f"ok {i}",
                ),
            ]))
            messages.append(Message(role=Role.USER, content=[
                ContentBlock(type=ContentType.TEXT, text=f"继续{i+2}"),
            ]))

        compressor = ContextCompressor(keep_recent=6, max_context_messages=10)
        result = await compressor.compress(messages)

        tool_use_ids: set[str] = set()
        tool_result_ids: set[str] = set()
        for msg in result.messages:
            for block in msg.content:
                if block.type == ContentType.TOOL_USE:
                    tool_use_ids.add(block.tool_call_id)
                elif block.type == ContentType.TOOL_RESULT:
                    tool_result_ids.add(block.tool_call_id)

        orphan_uses = tool_use_ids - tool_result_ids
        orphan_results = tool_result_ids - tool_use_ids
        assert not orphan_uses, f"Orphan tool_uses after compress: {orphan_uses}"
        assert not orphan_results, f"Orphan tool_results after compress: {orphan_results}"

    @pytest.mark.asyncio
    async def test_compress_short_messages_unchanged(self):
        """Short conversations should pass through unchanged."""
        from gangge.layer2_session.context import ContextCompressor

        messages = [
            Message(role=Role.USER, content=[
                ContentBlock(type=ContentType.TEXT, text="你好"),
            ]),
            Message(role=Role.ASSISTANT, content=[
                ContentBlock(type=ContentType.TEXT, text="你好！"),
            ]),
        ]

        compressor = ContextCompressor(keep_recent=10, max_context_messages=50)
        result = await compressor.compress(messages)

        assert len(result.messages) == len(messages), "Short conversation should not be compressed"

    @pytest.mark.asyncio
    async def test_compress_empty_messages(self):
        """Empty message list should not crash."""
        from gangge.layer2_session.context import ContextCompressor

        compressor = ContextCompressor()
        result = await compressor.compress([])

        assert result.messages == []
