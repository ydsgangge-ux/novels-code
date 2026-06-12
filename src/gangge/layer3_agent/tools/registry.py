"""Tool registry — manage all available tools with phase-based layering.

Tool layering strategy (inspired by Claude Code):
- Phase 1 (Explore): read-only tools + TodoWrite — understand the codebase
- Phase 2 (Write):  + file creation/editing tools — make changes
- Phase 3 (Run):    + bash/lint — execute and verify
- Phase 4 (Special): + novel/web/ask_user — domain-specific tools

The model starts with a small subset and the system opens more tools
as the task progresses. This dramatically improves tool selection accuracy,
especially for smaller models.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Awaitable

from gangge.layer3_agent.tools.base import BaseTool, ToolResult
from gangge.layer5_llm.base import ToolDefinition

logger = logging.getLogger(__name__)


# ── Tool phase definitions ──
# Tools are grouped by phase. The loop starts with phase 1 and
# automatically opens higher phases as the model uses tools.

PHASE_EXPLORE = 1   # Read-only + planning
PHASE_WRITE = 2     # File creation/editing
PHASE_RUN = 3       # Execution/verification
PHASE_SPECIAL = 4   # Domain-specific (novel, web, etc.)

# Map tool names to their minimum phase
TOOL_PHASES: dict[str, int] = {
    # Phase 1: Explore (always available)
    "list_dir":       PHASE_EXPLORE,
    "read_file":      PHASE_EXPLORE,
    "grep":           PHASE_EXPLORE,
    "glob":           PHASE_EXPLORE,
    "find_symbol":    PHASE_EXPLORE,
    "find_references": PHASE_EXPLORE,
    "TodoWrite":      PHASE_EXPLORE,
    # Phase 2: Write
    "write_file":     PHASE_WRITE,
    "edit_file":      PHASE_WRITE,
    "create_tool":    PHASE_WRITE,
    # Phase 3: Run
    "bash":           PHASE_RUN,
    "lint_check":     PHASE_RUN,
    # Phase 4: Special
    "ask_user":       PHASE_SPECIAL,
    "web_search":     PHASE_SPECIAL,
    "web_fetch":      PHASE_SPECIAL,
    "generate_image": PHASE_SPECIAL,
    "browser":        PHASE_SPECIAL,
    "vision":         PHASE_EXPLORE,  # always visible — LLM needs it for image tasks
    # Novel tools — all phase 4
    "novel_init":              PHASE_SPECIAL,
    "novel_setup":             PHASE_SPECIAL,
    "novel_outline":           PHASE_SPECIAL,
    "novel_chapter_outlines":  PHASE_SPECIAL,
    "novel_new_arc":           PHASE_SPECIAL,
    "novel_write_chapter":     PHASE_SPECIAL,
    "novel_audit":             PHASE_SPECIAL,
    "novel_revise":            PHASE_SPECIAL,
    "novel_status":            PHASE_SPECIAL,
    "novel_edit":              PHASE_SPECIAL,
    "novel_export":            PHASE_SPECIAL,
    "novel_list_books":        PHASE_SPECIAL,
    "novel_graph_query":       PHASE_SPECIAL,
    "novel_consistency_check": PHASE_SPECIAL,
    "novel_graph_rebuild":     PHASE_SPECIAL,
    "novel_import":            PHASE_SPECIAL,
    "novel_imitate_write":     PHASE_SPECIAL,
    "novel_chat":              PHASE_SPECIAL,
    "novel_navigate":          PHASE_SPECIAL,
}

# Default phase for tools not in the map
DEFAULT_PHASE = PHASE_WRITE


# ── Agent Profiles ──
# Different task types only see relevant tools.
# This dramatically improves tool selection accuracy for smaller models.

AGENT_PROFILES = {
    "coding": {
        "description": "通用编程任务",
        "detect_keywords": [
            "创建", "写", "修改", "实现", "开发", "构建", "部署",
            "create", "build", "fix", "implement", "refactor",
            "bug", "错误", "修复", "测试", "test",
            "api", "项目", "project", "app", "应用",
            "安装", "install", "配置", "config",
        ],
        "tools": {
            # Phase 1: Explore
            "list_dir", "read_file", "grep", "glob",
            "find_symbol", "find_references", "TodoWrite",
            # Phase 2: Write
            "write_file", "edit_file", "create_tool",
            # Phase 3: Run
            "bash", "lint_check",
            # Always available
            "ask_user",
            "vision",
        },
    },
    "novel": {
        "description": "小说创作任务",
        "detect_keywords": [
            "小说", "写作", "章节", "角色", "大纲", "剧情",
            "novel", "story", "chapter", "character", "outline",
            "写小说", "创作", "novel_init", "novel_setup",
            "伏笔", "人设", "世界观",
        ],
        "tools": {
            # Core novel tools
            "novel_init", "novel_setup", "novel_outline",
            "novel_chapter_outlines", "novel_new_arc",
            "novel_write_chapter", "novel_audit", "novel_revise",
            "novel_status", "novel_edit", "novel_export",
            "novel_list_books", "novel_graph_query",
            "novel_consistency_check", "novel_graph_rebuild",
            "novel_import", "novel_imitate_write",
            "novel_chat", "novel_navigate",
            # Basic file tools (for reading reference)
            "read_file", "list_dir", "grep", "glob",
            # Planning + interaction
            "TodoWrite", "ask_user",
        },
    },
    "research": {
        "description": "研究/搜索任务",
        "detect_keywords": [
            "搜索一下", "查一下", "调研", "研究一下",
            "web_search", "web_fetch",
            "网页", "官网", "在线文档",
            "联网", "上网查",
            "search the web", "look up", "find online",
            "google", "bing",
        ],
        "tools": {
            # Search + web
            "web_search", "web_fetch", "grep", "glob",
            "read_file", "list_dir",
            # Planning + interaction
            "TodoWrite", "ask_user",
            # Can write findings
            "write_file", "edit_file",
            # Image recognition
            "vision",
        },
    },
    "novel": {
        "description": "小说创作任务",
        "detect_keywords": [
            "小说", "写作", "章节", "角色", "大纲", "剧情",
            "novel", "story", "chapter", "character", "outline",
            "写小说", "创作", "novel_init", "novel_setup",
            "伏笔", "人设", "世界观",
        ],
        "tools": {
            # Core novel tools
            "novel_init", "novel_setup", "novel_outline",
            "novel_chapter_outlines", "novel_new_arc",
            "novel_write_chapter", "novel_audit", "novel_revise",
            "novel_status", "novel_edit", "novel_export",
            "novel_list_books", "novel_graph_query",
            "novel_consistency_check", "novel_graph_rebuild",
            "novel_import", "novel_imitate_write",
            "novel_chat", "novel_navigate",
            # Basic file tools (for reading reference)
            "read_file", "list_dir", "grep", "glob",
            # Planning + interaction
            "TodoWrite", "ask_user",
            # Image recognition
            "vision",
        },
    },
}


def detect_agent_profile(user_message: str) -> str:
    """Detect which agent profile to use based on the user's message.

    Returns the profile name. Falls back to "coding" if no match.

    Priority: novel > research > coding
    Novel keywords are strong signals — if any match, use novel profile.
    """
    msg_lower = user_message.lower()

    # Novel keywords are strong signals — check first with priority
    novel_keywords = AGENT_PROFILES["novel"]["detect_keywords"]
    novel_score = sum(1 for kw in novel_keywords if kw in msg_lower)
    if novel_score >= 1:
        return "novel"

    # Research keywords
    research_keywords = AGENT_PROFILES["research"]["detect_keywords"]
    research_score = sum(1 for kw in research_keywords if kw in msg_lower)
    if research_score >= 1:
        return "research"

    # Default to coding
    return "coding"


class ToolRegistry:
    """Registry of all available tools with phase-based layering + agent profiles."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}
        self._current_phase: int = PHASE_EXPLORE
        self._active_profile: str = "coding"  # default profile

    def register(self, tool: BaseTool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)

    def get(self, name: str) -> BaseTool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def list_tools(self) -> list[BaseTool]:
        """List all registered tools."""
        return list(self._tools.values())

    def get_definitions(self, phase: int | None = None, profile: str | None = None) -> list[ToolDefinition]:
        """Get tool definitions for LLM API, filtered by phase and profile.

        Args:
            phase: If provided, only return tools at or below this phase.
                   If None, return all tools (within profile).
            profile: If provided, only return tools in this profile's tool set.
                     If None, use the active profile.
        """
        effective_profile = profile or self._active_profile
        profile_tools = AGENT_PROFILES.get(effective_profile, {}).get("tools", None)

        filtered = []
        for tool in self._tools.values():
            # Profile filter: only include tools in the active profile
            if profile_tools is not None and tool.name not in profile_tools:
                continue

            # Phase filter
            if phase is not None:
                tool_phase = TOOL_PHASES.get(tool.name, DEFAULT_PHASE)
                if tool_phase > phase:
                    continue

            filtered.append(tool.to_definition())
        return filtered

    def get_current_phase(self) -> int:
        """Get the current tool phase."""
        return self._current_phase

    def advance_phase(self, tool_name: str) -> int:
        """Advance the current phase if the tool used is in a higher phase.

        Returns the new phase.
        """
        tool_phase = TOOL_PHASES.get(tool_name, DEFAULT_PHASE)
        if tool_phase > self._current_phase:
            old = self._current_phase
            self._current_phase = tool_phase
            phase_names = {1: "探索", 2: "编写", 3: "运行", 4: "特殊"}
            logger.info(f"[Registry] Phase {old}→{self._current_phase} ({phase_names.get(self._current_phase, '?')}) — triggered by {tool_name}")
        return self._current_phase

    def set_phase(self, phase: int) -> None:
        """Manually set the current phase (e.g., for novel mode)."""
        self._current_phase = phase

    def set_profile(self, profile: str) -> None:
        """Set the active agent profile (e.g., 'coding', 'novel', 'research')."""
        if profile in AGENT_PROFILES:
            self._active_profile = profile
            # Novel mode starts at phase 4 (special tools needed immediately)
            if profile == "novel":
                self._current_phase = PHASE_SPECIAL
            logger.info(f"[Registry] Profile set to '{profile}' — {AGENT_PROFILES[profile]['description']}")
        else:
            logger.warning(f"[Registry] Unknown profile '{profile}', keeping '{self._active_profile}'")

    def get_profile(self) -> str:
        """Get the current active profile name."""
        return self._active_profile

    def reset_phase(self) -> None:
        """Reset to phase 1 for a new task."""
        self._current_phase = PHASE_EXPLORE

    async def execute(self, name: str, input_data: dict[str, Any]) -> ToolResult:
        """Execute a tool by name with given input."""
        tool = self.get(name)
        if not tool:
            return ToolResult(output=f"未知工具: {name}", is_error=True)

        # Auto-advance phase when a tool is used
        self.advance_phase(name)

        return await tool.safe_execute(**input_data)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools


def create_tool_registry(
    workspace: str = "",
    ask_user_callback: Callable[[str], Awaitable[str]] | None = None,
    load_plugins: bool = True,
    llm: Any | None = None,
    multimodal_llm: Any | None = None,  # separate multimodal LLM for vision tool
    attachments: list[dict] | None = None,  # pre-loaded attachment data for vision tool
) -> ToolRegistry:
    """Create a ToolRegistry with all built-in tools registered.

    This is the single source of truth for which tools are available.
    Both CLI and GUI call this function — no duplicated registration logic.
    """
    from gangge.layer3_agent.tools.bash import BashTool
    from gangge.layer3_agent.tools.file_ops import ReadFileTool, WriteFileTool, EditFileTool
    from gangge.layer3_agent.tools.search import GrepTool, GlobTool, ListDirTool
    from gangge.layer3_agent.tools.web import UsageController, WebFetchTool, WebSearchTool
    from gangge.layer3_agent.tools.ask_user import AskUserTool
    from gangge.layer3_agent.tools.lint_check import LintCheckTool
    from gangge.layer3_agent.tools.create_tool import CreateToolTool
    from gangge.layer3_agent.tools.symbol import FindSymbolTool, FindReferencesTool
    from gangge.layer3_agent.tools.todo import TodoWriteTool, get_todo_state

    usage = UsageController()

    registry = ToolRegistry()

    # ── Phase 1: Explore tools (always available) ──
    registry.register(BashTool(workspace=workspace))  # bash is phase 3 but registered early
    registry.register(ReadFileTool(workspace=workspace))
    registry.register(WriteFileTool(workspace=workspace))
    registry.register(EditFileTool(workspace=workspace))
    registry.register(LintCheckTool(workspace=workspace))
    for cls in [GrepTool, GlobTool, ListDirTool]:
        registry.register(cls(workspace=workspace))
    registry.register(WebFetchTool(usage=usage))
    registry.register(WebSearchTool(usage=usage))
    registry.register(FindSymbolTool(workspace=workspace))
    registry.register(FindReferencesTool(workspace=workspace))

    # ── TodoWrite: the most important tool for task tracking ──
    todo_state = get_todo_state()
    registry.register(TodoWriteTool(state=todo_state))

    try:
        from gangge.layer3_agent.tools.browser import BrowserTool, _BROWSER_AVAILABLE
        if _BROWSER_AVAILABLE:
            registry.register(BrowserTool(usage=usage))
            logger.info("[Registry] Playwright browser tool activated")
        else:
            logger.debug("[Registry] Playwright not installed — browser tool skipped")
    except Exception as e:
        logger.debug("[Registry] Browser tool skipped: %s", e)

    registry.register(AskUserTool(ask_callback=ask_user_callback))

    # ── Vision Tool (multimodal image recognition) ──
    if multimodal_llm is not None:
        try:
            from gangge.layer3_agent.tools.vision import VisionTool
            vt = VisionTool(multimodal_llm=multimodal_llm)
            if attachments:
                vt.set_attachments(attachments)
            registry.register(vt)
            logger.info("[Registry] Vision tool activated — multimodal LLM available")
        except Exception as e:
            logger.debug("[Registry] Vision tool skipped: %s", e)

    create_tool = CreateToolTool(workspace=workspace, registry=registry)
    registry.register(create_tool)

    if load_plugins and workspace:
        from gangge.layer4_tools.plugin_loader import load_plugins as _load_plugins
        loaded = _load_plugins(workspace, registry)
        if loaded:
            logger.info("[Registry] loaded %d plugins: %s", len(loaded), loaded)

    if workspace:
        try:
            from gangge.layer3_agent.tools.comfyui_tool import is_comfyui_running, ComfyUITool
            if is_comfyui_running():
                registry.register(ComfyUITool(workspace=workspace))
                logger.info("[Registry] ComfyUI detected — image generation tool activated")
                os.environ["GANGGE_COMFYUI_ACTIVE"] = "1"
            else:
                logger.debug("[Registry] ComfyUI not detected — image generation tool skipped")
                os.environ.pop("GANGGE_COMFYUI_ACTIVE", None)
        except Exception as e:
            logger.debug("[Registry] ComfyUI check skipped: %s", e)
            os.environ.pop("GANGGE_COMFYUI_ACTIVE", None)

    # ── Novel Writing Tools (Dramatica-Flow) ──
    try:
        from gangge.layer3_agent.tools.novel import (
            NovelInitTool,
            NovelSetupTool,
            NovelOutlineTool,
            NovelChapterOutlinesTool,
            NovelNewArcTool,
            NovelWriteChapterTool,
            NovelAuditTool,
            NovelReviseTool,
            NovelStatusTool,
            NovelExportTool,
            NovelListBooksTool,
            NovelEditTool,
            NovelGraphQueryTool,
            NovelConsistencyCheckTool,
            NovelGraphRebuildTool,
            NovelImportTool,
            NovelImitateWriteTool,
            NovelChatTool,
            NovelNavigateTool,
            _DRAMATICA_AVAILABLE,
        )
        if _DRAMATICA_AVAILABLE:
            registry.register(NovelInitTool(workspace=workspace))
            registry.register(NovelSetupTool(workspace=workspace))
            registry.register(NovelOutlineTool(workspace=workspace, llm=llm))
            registry.register(NovelChapterOutlinesTool(workspace=workspace, llm=llm))
            registry.register(NovelNewArcTool(workspace=workspace, llm=llm))
            registry.register(NovelWriteChapterTool(workspace=workspace, llm=llm))
            registry.register(NovelAuditTool(workspace=workspace, llm=llm))
            registry.register(NovelReviseTool(workspace=workspace, llm=llm))
            registry.register(NovelStatusTool(workspace=workspace))
            registry.register(NovelEditTool(workspace=workspace))
            registry.register(NovelExportTool(workspace=workspace))
            registry.register(NovelListBooksTool(workspace=workspace))
            registry.register(NovelGraphQueryTool(workspace=workspace))
            registry.register(NovelConsistencyCheckTool(workspace=workspace))
            registry.register(NovelGraphRebuildTool(workspace=workspace))
            registry.register(NovelImportTool(workspace=workspace, llm=llm))
            registry.register(NovelImitateWriteTool(workspace=workspace, llm=llm))
            registry.register(NovelChatTool(workspace=workspace, llm=llm))
            registry.register(NovelNavigateTool(workspace=workspace))
            logger.info("[Registry] Novel writing tools (Dramatica-Flow) activated — 17 tools")
        else:
            logger.debug("[Registry] Dramatica-Flow not available — novel tools skipped")
    except Exception as e:
        logger.debug("[Registry] Novel tools skipped: %s", e)

    return registry
