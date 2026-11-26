"""Web shell for the agent — one browser tab per task.

The desktop shell is a single-window PyQt app whose ``self._worker`` /
``self._running`` pair admits exactly one running task. A web shell removes that
constraint at the presentation layer: every browser tab is a client, every
client owns a session, every session owns its own loop.

Run with::

    python -m gangge.web.server --port 7788

The FastAPI app lives in :mod:`gangge.web.server` and is imported there, not
here — importing this package must not require fastapi/uvicorn.
"""
