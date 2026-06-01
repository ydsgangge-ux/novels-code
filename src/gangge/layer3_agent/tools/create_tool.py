"""create_tool — let the AI build its own tools at runtime.

When the built-in tools cannot efficiently solve a recurring problem,
the AI can invoke this tool to create a new one.  The new tool is
saved under .gangge/plugins/ and immediately registered in the
ToolRegistry so it can be used right away.

Safety: 4-gate check before any tool is created.
  1. Dangerous code patterns  → reject
  2. Interface compliance     → reject
  3. Duplicate detection      → reject
  4. Dynamic load validation  → reject & cleanup
"""

from __future__ import annotations

import importlib.util
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from gangge.layer3_agent.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

FORBIDDEN_PATTERNS = [
    "shutil.rmtree",
    "os.remove",
    "os.unlink",
    "Path.unlink",
    "__import__",
    "eval(",
    "exec(",
    "compile(",
    "subprocess.Popen",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "os.system(",
]

REQUIRED_INTERFACE = [
    "BaseTool",
    "def execute(",
    "name",
]


class CreateToolTool(BaseTool):
    """Create a new tool and register it immediately."""

    def __init__(self, workspace: str = "", registry: Any = None):
        self.workspace = workspace
        self._registry = registry
        self.plugin_dir = Path(workspace) / ".gangge" / "plugins"
        self.meta_file = self.plugin_dir / "meta.json"

    @property
    def name(self) -> str:
        return "create_tool"

    @property
    def description(self) -> str:
        return (
            "Create a new tool when existing tools cannot efficiently handle a recurring task. "
            "The tool is saved to .gangge/plugins/ and activated immediately. "
            "Only create a tool if: (1) the operation is needed 3+ times, "
            "(2) existing tools cannot do it in 2 lines, "
            "(3) it will be reused in future tasks."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": "Tool name in snake_case, e.g. validate_json_format",
                },
                "description": {
                    "type": "string",
                    "description": "One-line description of what the tool does",
                },
                "reason": {
                    "type": "string",
                    "description": "Why this tool is needed (not achievable with existing tools) "
                                   "and in which scenarios it will be reused",
                },
                "code": {
                    "type": "string",
                    "description": "Complete Python tool code. Must define a class inheriting BaseTool "
                                   "with name, description, input_schema, and execute() method.",
                },
            },
            "required": ["tool_name", "description", "reason", "code"],
        }

    def set_registry(self, registry: Any) -> None:
        self._registry = registry

    async def execute(self, **kwargs: Any) -> ToolResult:
        tool_name = kwargs["tool_name"]
        description = kwargs["description"]
        reason = kwargs["reason"]
        code = kwargs["code"]

        if not self._registry:
            return ToolResult(output="Registry not available, cannot create tool", is_error=True)

        safe, msg = self._safety_check(code)
        if not safe:
            return ToolResult(output=f"Rejected by safety check\nReason: {msg}", is_error=True)

        valid, msg = self._interface_check(code)
        if not valid:
            return ToolResult(
                output=f"Rejected by interface check\nReason: {msg}\n"
                       "Tool class must inherit BaseTool and define name + execute()",
                is_error=True,
            )

        duplicate = self._duplicate_check(tool_name, description)
        if duplicate:
            return ToolResult(
                output=f"Duplicate detected — tool not created\n"
                       f"Existing tool: {duplicate}\n"
                       f"Suggestion: use the existing tool or extend it",
                is_error=True,
            )

        self.plugin_dir.mkdir(parents=True, exist_ok=True)
        tool_file = self.plugin_dir / f"{tool_name}.py"
        tool_file.write_text(code, encoding="utf-8")

        load_ok, instance_or_err = self._load_and_validate(tool_name, tool_file)
        if not load_ok:
            tool_file.unlink(missing_ok=True)
            return ToolResult(
                output=f"Dynamic load validation failed — file removed\nError: {instance_or_err}",
                is_error=True,
            )

        self._registry.register(instance_or_err)
        self._update_meta(tool_name, description, reason)

        logger.info("[create_tool] created and registered: %s", tool_name)
        return ToolResult(
            output=f"Tool '{tool_name}' created and activated\n"
                   f"Location: .gangge/plugins/{tool_name}.py\n"
                   f"Description: {description}\n"
                   f"Available immediately; auto-loaded on next startup",
        )

    # ── Gate 1: safety ──────────────────────────────────────

    def _safety_check(self, code: str) -> tuple[bool, str]:
        for pattern in FORBIDDEN_PATTERNS:
            if pattern in code:
                return False, f"Forbidden pattern: {pattern}"
        return True, "OK"

    # ── Gate 2: interface ───────────────────────────────────

    def _interface_check(self, code: str) -> tuple[bool, str]:
        for required in REQUIRED_INTERFACE:
            if required not in code:
                return False, f"Missing required element: {required}"
        return True, "OK"

    # ── Gate 3: duplicate ───────────────────────────────────

    def _duplicate_check(self, tool_name: str, description: str) -> str | None:
        existing_file = self.plugin_dir / f"{tool_name}.py"
        if existing_file.exists():
            return tool_name

        builtin_names = {
            "bash", "read_file", "write_file", "edit_file",
            "grep", "glob", "list_dir", "web_fetch", "web_search",
            "ask_user", "lint_check", "create_tool",
        }
        if tool_name in builtin_names:
            return f"built-in tool '{tool_name}'"

        meta = self._load_meta()
        stop_words = {
            "a", "the", "for", "to", "in", "of", "and", "or",
            "is", "an", "on", "with", "from", "by",
        }
        new_words = set(description.lower().split()) - stop_words
        for existing_name, info in meta.items():
            existing_words = set(info.get("description", "").lower().split()) - stop_words
            if len(new_words & existing_words) >= 3:
                return existing_name

        return None

    # ── Gate 4: dynamic load ────────────────────────────────

    def _load_and_validate(self, tool_name: str, tool_file: Path) -> tuple[bool, Any]:
        try:
            spec = importlib.util.spec_from_file_location(tool_name, tool_file)
            if spec is None or spec.loader is None:
                return False, "Cannot create module spec"
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            tool_class = None
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (
                    isinstance(attr, type)
                    and issubclass(attr, BaseTool)
                    and attr is not BaseTool
                    and hasattr(attr, "execute")
                ):
                    tool_class = attr
                    break

            if tool_class is None:
                return False, "No valid tool class found (must inherit BaseTool with name + execute)"

            instance = tool_class(workspace=self.workspace)
            _ = instance.name
            _ = instance.description
            _ = instance.input_schema
            return True, instance

        except SyntaxError as e:
            return False, f"Syntax error: {e}"
        except ImportError as e:
            return False, f"Import error (missing dependency?): {e}"
        except Exception as e:
            return False, f"Load error: {e}"

    # ── meta.json ───────────────────────────────────────────

    def _load_meta(self) -> dict:
        if self.meta_file.exists():
            try:
                return json.loads(self.meta_file.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _update_meta(self, tool_name: str, description: str, reason: str) -> None:
        meta = self._load_meta()
        meta[tool_name] = {
            "description": description,
            "reason": reason,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "use_count": 0,
            "last_used": None,
        }
        self.meta_file.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def record_usage(self, tool_name: str) -> None:
        meta = self._load_meta()
        if tool_name in meta:
            meta[tool_name]["use_count"] += 1
            meta[tool_name]["last_used"] = datetime.now().strftime("%Y-%m-%d")
            self.meta_file.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
