"""Tool registry — capability-scoped tool exposure.

Two mechanisms, one job: keep the tool list small enough that a weak model can
still pick the right one, without hiding something it legitimately needs.

* **Profile** — the task family (coding / novel / research). Each profile
  declares a ``core`` set that is always visible, plus the full set it may use.
* **tool_search** — the escape hatch. A deferred tool (web, browser, symbols,
  novel, image, …) is revealed by searching for it, which unlocks its full
  schema for the rest of the session.

What this replaces: a keyword-scored "phase" gate that opened write/run tools
when a todo item happened to contain "测试" or when the workspace was empty.
Those heuristics misfired (a todo saying "不要运行测试" unlocked bash; a real
task in a non-empty repo never got write access), and no amount of keyword
tuning fixes guessing. Asking the model to search is a decision, not a guess.

``phase`` survives as a coarse counter for UI display and telemetry only — it
no longer hides anything.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Callable, Awaitable

from gangge.layer3_agent.tools.base import BaseTool, ToolResult
from gangge.layer5_llm.base import ToolDefinition

logger = logging.getLogger(__name__)

_CJK = re.compile(r"[\u4e00-\u9fff]")


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
    "everything_search": PHASE_EXPLORE,
    "find_symbol":    PHASE_EXPLORE,
    "find_references": PHASE_EXPLORE,
    "TodoWrite":      PHASE_EXPLORE,
    "chat_log":       PHASE_EXPLORE,  # 纯聊天记录 — 不需要其他工具时的出口
    "recall_conversation": PHASE_EXPLORE,  # 搜索历史对话 — 跨会话记忆
    "tool_search":    PHASE_EXPLORE,  # 元工具：始终可见，用来揭示其余工具
    # Phase 2: Write
    "write_file":     PHASE_WRITE,
    "edit_file":      PHASE_WRITE,
    "apply_patch":    PHASE_WRITE,
    "create_tool":    PHASE_WRITE,
    # Phase 3: Run
    "bash":           PHASE_RUN,
    "lint_check":     PHASE_RUN,
    "browser_debug":  PHASE_RUN,  # 浏览器调试验证 — 前端代码改完后验证页面
    # Phase 4: Special
    "ask_user":       PHASE_SPECIAL,
    "web_search":     PHASE_SPECIAL,
    "web_fetch":      PHASE_SPECIAL,
    "package_info":   PHASE_SPECIAL,  # 结构化 API 直连（GitHub/npm/PyPI）
    "generate_image": PHASE_SPECIAL,
    "demo_video":     PHASE_SPECIAL,  # storyboard → narrated mp4
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
            "网页", "页面", "html",
            "create", "build", "fix", "implement", "refactor",
            "bug", "错误", "修复", "测试", "test",
            "api", "项目", "project", "app", "应用",
            "安装", "install", "配置", "config",
        ],
        "tools": {
            # Phase 1: Explore
            "list_dir", "read_file", "grep", "glob",
            "everything_search",
            "find_symbol", "find_references", "TodoWrite", "chat_log", "recall_conversation",
            # Phase 2: Write
            "write_file", "edit_file", "apply_patch", "create_tool",
            # Discovery (always visible even though it is listed here)
            "tool_search",
            # Phase 3: Run
            "bash", "lint_check", "browser_debug",
            # Always available
            "ask_user",
            "vision",
            "package_info",  # 查包版本/依赖（开发常见需求）
            # Image generation (ComfyUI)
            "generate_image",
            # Demo video (storyboard → mp4)
            "demo_video",
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
            "read_file", "list_dir", "grep", "glob", "everything_search",
            # Planning + interaction
            "TodoWrite", "ask_user",
            # Image recognition + generation
            "vision", "generate_image",
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
            "web_search", "web_fetch", "package_info", "grep", "glob",
            "everything_search",
            "read_file", "list_dir",
            # Planning + interaction
            "TodoWrite", "ask_user", "chat_log",
            # Can write findings
            "write_file", "edit_file",
            # Image recognition + generation
            "vision", "generate_image",
        },
    },
    "media": {
        "description": "演示动画与短视频制作",
        "detect_keywords": [
            "视频", "短视频", "动画", "配音", "宣传片", "演示视频",
            "分镜", "字幕", "口播", "demo_video", "tts",
            "video", "animation", "narration",
        ],
        "tools": {
            "demo_video",
            "read_file", "list_dir", "grep", "glob", "everything_search",
            "write_file", "edit_file", "apply_patch",
            "TodoWrite", "ask_user", "chat_log", "tool_search",
            # 检查渲染结果
            "vision", "generate_image",
        },
    },
}


def detect_agent_profile(user_message: str) -> str:
    """Detect which agent profile to use based on the user's message.

    Returns the profile with the highest keyword hit count. On ties the
    higher-priority profile wins: novel > research > coding.
    Falls back to "coding" when nothing matches.

    Keyword scoring stays as a *fallback* only. An explicit choice — a GUI mode,
    ``--profile``, or ``GANGGE_AGENT_PROFILE`` — always wins, because guessing a
    task family from substrings ("画" in "画个架构图" vs "画插图") is exactly the
    kind of heuristic that should never override what the user said.
    """
    explicit = (os.environ.get("GANGGE_AGENT_PROFILE") or "").strip().lower()
    if explicit in AGENT_PROFILES:
        return explicit

    msg_lower = user_message.lower()
    # Order is priority: on a tie the earlier profile wins, so the narrow,
    # unambiguous families sit before the catch-all "coding".
    profiles = ("novel", "research", "media", "coding")
    best = "coding"
    best_score = -1
    for p in profiles:
        score = sum(1 for kw in AGENT_PROFILES[p]["detect_keywords"] if kw in msg_lower)
        if score > best_score:  # `>` keeps the earlier (higher-priority) profile on ties
            best_score = score
            best = p
    return best


# ── Profile core sets ──
# What the model sees *before* searching. The rule of thumb: everything needed
# for a self-contained single-file task, so the common case costs zero extra
# rounds. Anything domain-specific (web, browser, symbols, images, novel) is
# deferred and reachable via tool_search.
PROFILE_CORE: dict[str, set[str]] = {
    "coding": {
        "read_file",
        "list_dir",
        "grep",
        "glob",
        "write_file",
        "edit_file",
        "apply_patch",
        "bash",
        "lint_check",
        "TodoWrite",
        "ask_user",
        "chat_log",
        "tool_search",
    },
    # Novel and research sessions are domain sessions: hiding their own domain
    # tools behind a search would only add a round to every single turn.
    "media": {
        "demo_video",
        "read_file", "list_dir", "grep", "glob",
        "write_file", "edit_file", "apply_patch",
        "TodoWrite", "ask_user", "chat_log", "tool_search",
    },
    "novel": {
        "novel_init", "novel_setup", "novel_outline", "novel_chapter_outlines",
        "novel_new_arc", "novel_write_chapter", "novel_audit", "novel_revise",
        "novel_status", "novel_edit", "novel_export", "novel_list_books",
        "novel_graph_query", "novel_consistency_check", "novel_graph_rebuild",
        "novel_import", "novel_imitate_write", "novel_chat", "novel_navigate",
        "read_file", "list_dir", "grep", "everything_search",
        "TodoWrite", "ask_user", "vision", "generate_image", "tool_search",
    },
    "research": {
        "web_search", "web_fetch", "package_info", "grep", "glob",
        "everything_search", "read_file", "list_dir",
        "TodoWrite", "ask_user", "chat_log", "write_file", "edit_file",
        "vision", "generate_image", "tool_search",
    },
}

# Always visible regardless of profile: it is the mechanism that reveals the
# rest, so hiding it would be self-defeating.
ALWAYS_VISIBLE: frozenset[str] = frozenset({"tool_search"})


class ToolRegistry:
    """Registry of all available tools with phase-based layering + agent profiles."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}
        self._current_phase: int = PHASE_EXPLORE
        self._active_profile: str = "coding"  # default profile
        # AI-created plugin tools may live outside any profile's tool set;
        # track them so they are still surfaced to the model after creation.
        self._plugin_names: set[str] = set()
        # Tools revealed by ``tool_search`` this session. Unlocking is
        # permanent on purpose: a tool the model already discovered must not
        # vanish from the next request (that is how you get repeated
        # hallucinations of the same tool name).
        self._unlocked: set[str] = set()

    def mark_plugin(self, name: str) -> None:
        """Mark a tool as an AI-created plugin so it bypasses the profile filter."""
        self._plugin_names.add(name)

    # ── discovery ──────────────────────────────────────────────

    def reveal(self, names: list[str] | set[str], reason: str = "manual") -> list[str]:
        """Reveal tools for the rest of the session; returns the newly added.

        Same effect as :meth:`unlock` but distinguishable in the log: MCP tools
        with a small footprint are revealed up-front instead of waiting for a
        ``tool_search`` round-trip.
        """
        added: list[str] = []
        for name in names:
            if name in self._tools and name not in self._unlocked:
                self._unlocked.add(name)
                added.append(name)
        if added:
            logger.info("[Registry] revealed (%s): %s", reason, ", ".join(added))
        return added

    def unlock(self, names: list[str] | set[str]) -> list[str]:
        """Reveal tools for the rest of the session; returns the newly added."""
        added: list[str] = []
        for name in names:
            if name in self._tools and name not in self._unlocked:
                self._unlocked.add(name)
                added.append(name)
        if added:
            logger.info("[Registry] unlocked by tool_search: %s", ", ".join(added))
        return added

    def is_visible(self, name: str, phase: int | None = None) -> bool:
        """Whether ``name`` is currently part of the model's tool list."""
        if name in ALWAYS_VISIBLE or name in self._plugin_names or name in self._unlocked:
            return True
        if phase is None:
            return True
        return name in PROFILE_CORE.get(self._active_profile, set())

    def search(self, query: str, limit: int = 8) -> list[BaseTool]:
        """Rank tools against ``query``.

        Scoring is intentionally simple and explainable — exact name, name
        substring, tag, description keyword — because the caller is a model that
        can read the result and search again if it missed.
        """
        query = (query or "").strip()
        if not query:
            deferred = [
                t for t in self._tools.values()
                if t.name not in PROFILE_CORE.get(self._active_profile, set())
                and t.name not in ALWAYS_VISIBLE
            ]
            return deferred[:limit]

        lowered = query.lower()
        words = [w for w in lowered.replace("_", " ").split() if w]
        # Chinese queries arrive as one unspaced token ("生成图片"), so plain
        # word matching would miss everything. Bigrams are a cheap stand-in for
        # tokenisation and good enough for a ranked hint list.
        grams: list[str] = []
        for word in words:
            if _CJK.search(word) and len(word) > 2:
                grams.extend(word[i : i + 2] for i in range(len(word) - 1))

        scored: list[tuple[float, str, BaseTool]] = []
        for tool in self._tools.values():
            name = tool.name.lower()
            desc = (tool.description or "").lower()
            tags = " ".join(tool.tags).lower()

            score = 0.0
            if lowered == name:
                score += 100
            elif lowered in name:
                score += 50
            for word in words:
                if word in name:
                    score += 20
                if word in tags:
                    score += 12
                if word in desc:
                    score += 8
            for gram in grams:
                if gram in name:
                    score += 6
                elif gram in desc or gram in tags:
                    score += 4
            if not score and words:
                # Last resort: partial-word overlap with the description
                score = sum(2 for w in words if w[:4] and w[:4] in desc)
            if score > 0:
                scored.append((score, tool.name, tool))

        scored.sort(key=lambda item: (-item[0], item[1]))
        return [tool for _, _, tool in scored[:limit]]

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
        """Get tool definitions for the LLM API.

        Args:
            phase: If provided, restrict the list to the profile's ``core`` set
                   plus anything revealed by ``tool_search``. If None, return
                   every tool the profile may use (introspection / tests).
            profile: Which profile's universe to filter against; defaults to the
                     active one.
        """
        effective_profile = profile or self._active_profile
        profile_tools = AGENT_PROFILES.get(effective_profile, {}).get("tools", None)
        core = PROFILE_CORE.get(effective_profile, set())

        filtered: list[ToolDefinition] = []
        for tool in self._tools.values():
            name = tool.name
            # Escape hatches: the search tool itself, AI-created plugins, and
            # everything the model already discovered.
            escape = name in ALWAYS_VISIBLE or name in self._plugin_names or name in self._unlocked

            if profile_tools is not None and name not in profile_tools and not escape:
                continue

            if phase is not None and not escape and name not in core:
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
    from gangge.layer3_agent.tools.search import GrepTool, GlobTool, ListDirTool, EverythingSearchTool
    from gangge.layer3_agent.tools.web import UsageController, WebFetchTool, WebSearchTool
    from gangge.layer3_agent.tools.ask_user import AskUserTool
    from gangge.layer3_agent.tools.lint_check import LintCheckTool
    from gangge.layer3_agent.tools.create_tool import CreateToolTool
    from gangge.layer3_agent.tools.symbol import FindSymbolTool, FindReferencesTool
    from gangge.layer3_agent.tools.todo import TodoWriteTool, get_todo_state

    usage = UsageController()

    registry = ToolRegistry()

    # ── Discovery: must exist before anything can be hidden from the model ──
    from gangge.layer3_agent.tools.tool_search import ToolSearchTool

    registry.register(ToolSearchTool(registry))

    # ── Phase 1: Explore tools (always available) ──
    registry.register(BashTool(workspace=workspace))  # bash is phase 3 but registered early
    registry.register(ReadFileTool(workspace=workspace))
    registry.register(WriteFileTool(workspace=workspace))
    registry.register(EditFileTool(workspace=workspace))
    try:
        from gangge.layer3_agent.tools.patch import ApplyPatchTool

        registry.register(ApplyPatchTool(workspace=workspace))
    except Exception as e:  # a broken patch tool must not block booting
        logger.debug("[Registry] apply_patch skipped: %s", e)
    registry.register(LintCheckTool(workspace=workspace))
    for cls in [GrepTool, GlobTool, ListDirTool, EverythingSearchTool]:
        registry.register(cls(workspace=workspace))
    registry.register(WebFetchTool(usage=usage))
    registry.register(WebSearchTool(usage=usage))
    # ── PackageInfoTool: 结构化 API 直连（GitHub/npm/PyPI）──
    try:
        from gangge.layer3_agent.tools.package_info import PackageInfoTool
        registry.register(PackageInfoTool())
        logger.info("[Registry] Package info tool activated — GitHub/npm/PyPI 结构化查询")
    except Exception as e:
        logger.debug("[Registry] Package info tool skipped: %s", e)
    registry.register(FindSymbolTool(workspace=workspace))
    registry.register(FindReferencesTool(workspace=workspace))

    # ── TodoWrite: the most important tool for task tracking ──
    todo_state = get_todo_state()
    registry.register(TodoWriteTool(state=todo_state))

    # ── ChatLog: 纯聊天场景的工具出口 ──
    from gangge.layer3_agent.tools.chat_log import ChatLogTool
    registry.register(ChatLogTool())
    logger.info("[Registry] Chat log tool activated — 纯聊天/Q&A 出口")

    # ── RecallConversation: 搜索历史对话 ──
    try:
        from gangge.layer3_agent.tools.recall import RecallConversationTool
        registry.register(RecallConversationTool())
        logger.info("[Registry] Recall conversation tool activated — 历史对话搜索")
    except Exception as e:
        logger.debug("[Registry] Recall tool skipped: %s", e)

    try:
        from gangge.layer3_agent.tools.browser import BrowserTool, _BROWSER_AVAILABLE
        if _BROWSER_AVAILABLE:
            registry.register(BrowserTool(usage=usage))
            logger.info("[Registry] Playwright browser tool activated")
        else:
            logger.debug("[Registry] Playwright not installed — browser tool skipped")
    except Exception as e:
        logger.debug("[Registry] Browser tool skipped: %s", e)

    # ── Browser Debug Tool (前端调试验证) ──
    try:
        from gangge.layer3_agent.tools.browser_debug import BrowserDebugTool, _BROWSER_AVAILABLE as _BD_AVAIL
        if _BD_AVAIL:
            registry.register(BrowserDebugTool())
            logger.info("[Registry] Browser debug tool activated — 前端验证闭环")
        else:
            logger.debug("[Registry] Playwright not installed — browser_debug skipped")
    except Exception as e:
        logger.debug("[Registry] Browser debug tool skipped: %s", e)

    registry.register(AskUserTool(ask_callback=ask_user_callback))

    # ── Vision Tool (multimodal image recognition) ──
    # Registered whenever *some* model can see images: a dedicated vision
    # adapter, or the main model itself when it declares ``supports_vision``.
    # The tool picks the route per call, so the registry needs no knowledge of
    # which one is live.
    if multimodal_llm is not None or llm is not None:
        try:
            from gangge.layer3_agent.tools.vision import VisionTool

            vt = VisionTool(
                multimodal_llm=multimodal_llm,
                fallback_llm=llm,
                attachments=attachments,
            )
            registry.register(vt)
            logger.info(
                "[Registry] Vision tool registered (route=%s)",
                "dedicated" if multimodal_llm is not None else "main-model",
            )
        except Exception as e:
            logger.warning("[Registry] Vision tool skipped: %s", e)

    # ── Demo video (storyboard → narrated mp4) ──
    try:
        from gangge.layer3_agent.tools.demo_video import DemoVideoTool

        registry.register(DemoVideoTool(llm=llm, workspace=workspace))
        logger.info("[Registry] demo_video registered — storyboard → mp4")
    except Exception as e:
        logger.warning("[Registry] demo_video skipped: %s", e)

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
