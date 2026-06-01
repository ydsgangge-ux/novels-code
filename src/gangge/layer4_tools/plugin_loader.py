"""Plugin loader — load AI-created tools from .gangge/plugins/ at startup.

Called during AgenticLoop initialization to restore tools the AI built
in previous sessions.  A broken plugin is skipped silently — it never
blocks system startup.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gangge.layer3_agent.tools.base import BaseTool
    from gangge.layer3_agent.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def load_plugins(workspace: str, registry: ToolRegistry) -> list[str]:
    plugin_dir = Path(workspace) / ".gangge" / "plugins"
    if not plugin_dir.exists():
        return []

    loaded: list[str] = []

    for py_file in sorted(plugin_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue

        tool_name = py_file.stem
        try:
            spec = importlib.util.spec_from_file_location(tool_name, py_file)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            tool_class = _find_tool_class(module)
            if tool_class is None:
                logger.warning("[Plugin] %s: no valid tool class found (needs name + execute)", tool_name)
                continue

            instance = tool_class(workspace=workspace)
            registry.register(instance)
            loaded.append(tool_name)
            logger.info("[Plugin] loaded: %s", tool_name)

        except Exception as e:
            logger.warning("[Plugin] %s failed (skipped): %s", tool_name, e)

    return loaded


def _find_tool_class(module):
    from gangge.layer3_agent.tools.base import BaseTool

    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if (
            isinstance(attr, type)
            and issubclass(attr, BaseTool)
            and attr is not BaseTool
            and hasattr(attr, "execute")
        ):
            return attr
    return None
