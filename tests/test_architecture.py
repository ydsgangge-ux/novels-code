"""Tests for the 5-layer architecture."""

import pytest
import asyncio

# ─── Layer 5: LLM Adapter ───────────────────────────────────


class TestMessage:
    """Test Message dataclass."""

    from gangge.layer5_llm.base import Message, ContentBlock, ContentType

    def test_create_text_message(self):
        msg = self.Message(role=self.Role.USER, content="hello")
        assert msg.get_text() == "hello"
        assert len(msg.content) == 1

    def test_add_tool_use(self):
        msg = self.Message(role=self.Role.ASSISTANT)
        msg.add_tool_use("bash", "call_123", {"command": "ls"})
        assert msg.content[0].tool_name == "bash"
        assert msg.content[0].tool_call_id == "call_123"

    def test_to_dict(self):
        msg = self.Message(role=self.Role.USER, content="test")
        d = msg.to_dict()
        assert d["role"] == "user"


from gangge.layer5_llm.base import Role


TestMessage.Role = Role


# ─── Layer 4: Permission ────────────────────────────────────


class TestDangerDetector:
    def test_safe_command(self):
        from gangge.layer4_permission.danger import DangerDetector, RiskLevel
        d = DangerDetector()
        result = d.assess_command("ls -la")
        assert result.level == RiskLevel.SAFE

    def test_dangerous_command(self):
        from gangge.layer4_permission.danger import DangerDetector, RiskLevel
        d = DangerDetector()
        result = d.assess_command("rm -rf /")
        assert result.level in (RiskLevel.HIGH, RiskLevel.CRITICAL)

    def test_sudo_command(self):
        from gangge.layer4_permission.danger import DangerDetector, RiskLevel
        d = DangerDetector()
        result = d.assess_command("sudo apt install something")
        assert result.level == RiskLevel.HIGH

    def test_restricted_path(self):
        from gangge.layer4_permission.danger import DangerDetector, RiskLevel
        d = DangerDetector()
        result = d.assess_path("/etc/shadow", "write")
        assert result.level == RiskLevel.CRITICAL


# ─── Layer 3: Tools ─────────────────────────────────────────


class TestToolRegistry:
    def test_register_and_get(self):
        from gangge.layer3_agent.tools.registry import ToolRegistry
        from gangge.layer3_agent.tools.search import ListDirTool

        reg = ToolRegistry()
        tool = ListDirTool()
        reg.register(tool)
        assert reg.get("list_dir") is tool
        assert "list_dir" in reg

    def test_definitions(self):
        from gangge.layer3_agent.tools.registry import ToolRegistry
        from gangge.layer3_agent.tools.bash import BashTool

        reg = ToolRegistry()
        reg.register(BashTool())
        defs = reg.get_definitions()
        assert len(defs) == 1
        assert defs[0].name == "bash"


# ─── Layer 3: File Tools ────────────────────────────────────


class TestFileTools:
    @pytest.mark.asyncio
    async def test_read_file(self, tmp_path):
        from gangge.layer3_agent.tools.file_ops import ReadFileTool

        f = tmp_path / "test.py"
        f.write_text("hello\nworld\n")

        tool = ReadFileTool()
        result = await tool.execute(path=str(f))
        assert not result.is_error
        assert "hello" in result.output

    @pytest.mark.asyncio
    async def test_write_file(self, tmp_path):
        from gangge.layer3_agent.tools.file_ops import WriteFileTool

        f = tmp_path / "new.txt"
        tool = WriteFileTool()
        result = await tool.execute(path=str(f), content="test content")
        assert not result.is_error
        assert f.read_text() == "test content"

    @pytest.mark.asyncio
    async def test_read_nonexistent(self, tmp_path):
        from gangge.layer3_agent.tools.file_ops import ReadFileTool

        tool = ReadFileTool()
        result = await tool.execute(path=str(tmp_path / "nope.txt"))
        assert result.is_error


# ─── Layer 3: Search Tools ──────────────────────────────────


class TestSearchTools:
    @pytest.mark.asyncio
    async def test_glob(self, tmp_path):
        from gangge.layer3_agent.tools.search import GlobTool

        (tmp_path / "a.py").touch()
        (tmp_path / "b.ts").touch()
        (tmp_path / "c.py").touch()

        tool = GlobTool()
        result = await tool.execute(pattern="*.py", path=str(tmp_path))
        assert not result.is_error
        assert "a.py" in result.output
        assert "c.py" in result.output

    @pytest.mark.asyncio
    async def test_grep(self, tmp_path):
        from gangge.layer3_agent.tools.search import GrepTool

        f = tmp_path / "code.py"
        f.write_text("def foo():\n    return 42\n\ndef bar():\n    return foo()\n")

        tool = GrepTool()
        result = await tool.execute(pattern=r"def \w+", path=str(tmp_path), include="*.py")
        assert not result.is_error
        assert "code.py" in result.output
