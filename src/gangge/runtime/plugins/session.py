"""Session seam implementation — ``ctx.sessions``.

Session services need ``await storage.init()``, but ``apply()`` is synchronous.
Rather than forcing every consumer to care, the service initialises lazily on
first use (``ensure()``). That keeps mount synchronous while never opening a
database that nobody asked for.
"""

from __future__ import annotations

import logging
from typing import Any

from gangge.runtime import events
from gangge.runtime.context import Context, Inject

logger = logging.getLogger(__name__)

name = "session"
inject = Inject(optional=("storage", "llm"))


class SessionService:
    """Thin lifecycle wrapper over ``SessionManager``."""

    def __init__(self, ctx: Context, config: dict[str, Any]) -> None:
        self.ctx = ctx
        self.config = config or {}
        self._manager: Any = None
        self._ready = False

    # -- lazy wiring -------------------------------------------

    def manager(self) -> Any:
        if self._manager is None:
            from gangge.layer2_session.context import ContextCompressor
            from gangge.layer2_session.manager import SessionManager
            from gangge.layer2_session.state import ProjectState
            from gangge.layer2_session.storage import SessionStorage

            backend = self.ctx.get("storage")
            storage = backend or SessionStorage(self.config.get("db_path", ".data/gangge.db"))
            compressor = ContextCompressor(llm=self.ctx.get("llm"))
            state = ProjectState(self.config.get("workspace", ""))
            self._manager = SessionManager(
                storage=storage,
                compressor=compressor,
                project_state=state,
                auto_save=bool(self.config.get("auto_save", True)),
            )
        return self._manager

    async def ensure(self) -> Any:
        """Open storage once; safe to call on every use."""
        manager = self.manager()
        if not self._ready:
            await manager.init()
            self._ready = True
            self.ctx.emit(events.SESSION_CREATED, manager)
        return manager

    # -- seam --------------------------------------------------

    async def new(self, title: str = "") -> Any:
        manager = await self.ensure()
        session = await manager.new_session(title or "新会话")
        self.ctx.emit(events.SESSION_TITLE, session)
        return session

    async def load(self, session_id: str) -> Any:
        manager = await self.ensure()
        return await manager.load_session(session_id)

    async def close(self) -> None:
        if self._manager is not None and self._ready:
            await self._manager.close()
        self._ready = False
        self._manager = None


def apply(ctx: Context, config: dict[str, Any]) -> None:
    service = SessionService(ctx, config)
    ctx.provide("sessions", service)

    async def _close() -> None:
        await service.close()

    ctx.on_cleanup(lambda: _close())
