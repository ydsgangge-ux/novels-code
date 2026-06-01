"""Session manager — orchestrates context, state, and storage."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from gangge.layer2_session.context import ContextCompressor
from gangge.layer2_session.state import ProjectState
from gangge.layer2_session.storage import SessionStorage
from gangge.layer5_llm.base import BaseLLM, Message, Role, ContentBlock, ContentType

logger = logging.getLogger(__name__)


@dataclass
class Session:
    """An active conversation session."""

    id: str = ""
    title: str = "新会话"
    messages: list[Message] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


class SessionManager:
    """Manage conversation sessions with compression and persistence."""

    def __init__(
        self,
        storage: SessionStorage | None = None,
        compressor: ContextCompressor | None = None,
        project_state: ProjectState | None = None,
        auto_save: bool = True,
    ):
        self.storage = storage
        self.compressor = compressor
        self.project_state = project_state
        self.auto_save = auto_save
        self.current: Session | None = None

    async def init(self) -> None:
        """Initialize storage."""
        if self.storage:
            await self.storage.init()

    async def close(self) -> None:
        """Close resources."""
        if self.storage:
            await self.storage.close()

    async def new_session(self, title: str = "新会话") -> Session:
        """Create a new session."""
        session = Session(
            id=str(uuid.uuid4())[:8],
            title=title,
        )
        self.current = session

        if self.storage:
            await self.storage.create_session(session.id, title)

        return session

    async def load_session(self, session_id: str) -> Session | None:
        """Load an existing session."""
        if not self.storage:
            return None

        raw_messages = await self.storage.load_messages(session_id)
        if not raw_messages:
            return None

        messages = []
        for rm in raw_messages:
            content = rm.get("content", "")
            if isinstance(content, list):
                blocks = []
                for b in content:
                    if isinstance(b, str):
                        blocks.append(ContentBlock(type=ContentType.TEXT, text=b))
                    elif isinstance(b, dict):
                        ct = ContentType(b.get("type", "text"))
                        blocks.append(ContentBlock(
                            type=ct,
                            text=b.get("text", b.get("content", "")),
                            tool_call_id=b.get("id", b.get("tool_use_id", "")),
                            tool_name=b.get("name", ""),
                            tool_input=b.get("input", {}),
                            is_error=b.get("is_error", False),
                        ))
                messages.append(Message(role=Role(rm["role"]), content=blocks))
            else:
                messages.append(Message(role=Role(rm["role"]), content=str(content)))

        self.current = Session(id=session_id, messages=messages)
        return self.current

    def add_message(self, role: Role, content: str) -> Message:
        """Add a message to the current session."""
        msg = Message(role=role, content=[ContentBlock(type=ContentType.TEXT, text=content)])
        if self.current:
            self.current.messages.append(msg)
        return msg

    def get_messages(self) -> list[Message]:
        """Get all messages in current session."""
        return self.current.messages if self.current else []

    async def save(self) -> None:
        """Save current session to storage."""
        if not self.auto_save or not self.storage or not self.current:
            return

        raw_messages = []
        for msg in self.current.messages:
            raw_messages.append({
                "role": msg.role.value,
                "content": [b.to_dict() for b in msg.content],
            })
        await self.storage.save_messages(self.current.id, raw_messages)

    async def compress_if_needed(self) -> None:
        """Compress context if it's getting too long."""
        if not self.compressor or not self.current:
            return

        result = await self.compressor.compress(self.current.messages)
        if result.compressed_count < result.original_count:
            self.current.messages = result.messages
            logger.info(
                f"Compressed context: {result.original_count} → {result.compressed_count} messages"
            )

    def get_project_context(self) -> str:
        """Get project context string for system prompt."""
        if self.project_state:
            return self.project_state.to_context_string()
        return ""
