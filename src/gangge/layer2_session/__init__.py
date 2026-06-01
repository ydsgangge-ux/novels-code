"""Layer 2 — Session Management.

上下文管理、历史压缩、项目状态快照、SQLite 持久化。
"""

from gangge.layer2_session.manager import SessionManager
from gangge.layer2_session.context import ContextCompressor
from gangge.layer2_session.state import ProjectState
from gangge.layer2_session.storage import SessionStorage

__all__ = ["SessionManager", "ContextCompressor", "ProjectState", "SessionStorage"]
