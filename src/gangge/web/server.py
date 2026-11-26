"""FastAPI shell: one session per browser tab, all of them running at once.

Design notes, because the threading looks heavier than a plain asyncio app:

* **One OS thread per running task.** ``AgenticLoop`` is async, but its tools are
  not — ``bash`` runs ``subprocess``, file tools do blocking IO, some MCP calls
  wait on pipes. Running several loops on the single server event loop would let
  one slow tool freeze every other tab. Giving each task its own thread (and its
  own loop inside it) keeps tabs independent.

* **Events travel to the server loop via ``call_soon_threadsafe``.** A task
  thread must never touch the ``asyncio.Queue`` directly; it hands the event to
  the loop that owns the queue.

* **Cancel is a per-session flag**, so stopping one task cannot touch another.
  The desktop shell's cancel is per-window; this is per-session.

Binding defaults to ``127.0.0.1`` on purpose. The agent has ``bash`` and free
file access; exposing that on ``0.0.0.0`` is equivalent to handing out a shell.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_PORT = int(os.environ.get("GANGGE_WEB_PORT", "7788") or 7788)

_DEFAULT_MAX_TOKENS: int | None = None


def _default_max_tokens() -> int:
    """生效的默认输出上限：.env / 环境变量 MAX_TOKENS，缺省 8192。

    只算一次并缓存：快照接口会被频繁调用，没必要每次都重读 .env。
    """
    global _DEFAULT_MAX_TOKENS
    if _DEFAULT_MAX_TOKENS is None:
        try:
            from gangge.layer5_llm.registry import _load_env

            _load_env()
        except Exception:  # noqa: BLE001
            logger.debug("[Web] 读取 .env 失败", exc_info=True)
        try:
            _DEFAULT_MAX_TOKENS = max(1, int(os.environ.get("MAX_TOKENS", "8192")))
        except (TypeError, ValueError):
            _DEFAULT_MAX_TOKENS = 8192
    return _DEFAULT_MAX_TOKENS


def _current_provider() -> str:
    """当前默认厂商（.env 的 LLM_PROVIDER），读不到就返回空。"""
    try:
        from gangge.layer5_llm.registry import current_provider

        return current_provider()
    except Exception:  # noqa: BLE001
        logger.debug("[Web] 读取当前厂商失败", exc_info=True)
        return ""


def _resolve_model(provider: str | None = None) -> str:
    """该厂商默认用哪个模型，不构造客户端。"""
    try:
        from gangge.layer5_llm.registry import resolve_model

        return resolve_model(provider or None)
    except Exception:  # noqa: BLE001
        logger.debug("[Web] 读取默认模型失败", exc_info=True)
        return ""


def _trace_failure_summary(output: str, limit: int = 160) -> str:
    """失败工具调用存进 trace 的一行摘要：回看时不用重跑。

    trace.jsonl 以前只记 ``error: true``，排查时仍得重跑一遍才知道错在哪。
    这里挑最能说明问题的一行（失败用例 / 异常 / 权限拒绝），控制在一行内。
    """
    text = (output or "").strip()
    if not text:
        return ""
    markers = (
        "FAILED", "failed", "Error", "error", "Exception", "Traceback",
        "异常", "失败", "拒绝", "✗", "❌",
    )
    for raw in text.splitlines():
        line = raw.strip()
        if line and any(marker in line for marker in markers):
            return line[:limit]
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return first[:limit]

app = FastAPI(title="Gangge Web", version="0.1.0")


# ─────────────────────────────────────────────────────────────
#  Request bodies
# ─────────────────────────────────────────────────────────────
class NewSession(BaseModel):
    workspace: str = ""
    name: str = ""
    auto_allow: bool = False
    max_rounds: int = 50


class OpenSession(BaseModel):
    id: str


class NewMessage(BaseModel):
    text: str
    #: 已上传附件的元信息（name / path / kind），由 /attach 接口返回，这里只透传。
    #: 默认空列表：老客户端不带这个字段也能继续用。
    attachments: list[dict] = []


class Answer(BaseModel):
    value: str = ""
    allow: bool = False
    #: 允许并记住：本次会话不再询问同一个工具。
    remember: bool = False


class SessionConfig(BaseModel):
    max_rounds: int | None = None
    auto_allow: bool | None = None
    model: str | None = None
    #: 厂商；空 = 跟 .env 里的 LLM_PROVIDER。
    provider: str | None = None
    #: 单次回复输出上限；0 / 不传 = 跟随 .env 里的 MAX_TOKENS。
    max_tokens: int | None = None


class RenameBody(BaseModel):
    name: str


class PriceBody(BaseModel):
    provider: str
    model: str
    input_price: float
    output_price: float
    cached_price: float | None = None


# ─────────────────────────────────────────────────────────────
#  Session model
# ─────────────────────────────────────────────────────────────
@dataclass
class WebSession:
    id: str
    name: str
    workspace: str
    auto_allow: bool
    max_rounds: int
    main_loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue
    messages: list = field(default_factory=list)
    #: 已发生的事件，用于刷新/重连后回放（而不是只留一片空白）。
    events: list = field(default_factory=list)
    status: str = "idle"
    cancel: bool = False
    thread: threading.Thread | None = None
    # Question / permission handshake: the task thread waits on the event,
    # the HTTP layer fills the slot and sets it.
    pending_event: threading.Event | None = None
    pending_kind: str = ""
    pending_text: str = ""
    pending_answer: str = ""
    pending_allow: bool = False
    #: 用户选了“本次会话总是允许”，把该工具记下来，后续不再弹窗。
    pending_remember: bool = False
    remembered_tools: set[str] = field(default_factory=set)
    turn_started: float = 0.0
    #: 运行中提交的指令排队，本轮结束后自动发出（不是插话到当前轮）。
    queue_messages: list = field(default_factory=list)
    #: 覆盖默认模型；空 = 用环境变量里的配置。
    model_override: str = ""
    #: 覆盖默认厂商；空 = 用 .env 里的 LLM_PROVIDER。
    provider_override: str = ""
    #: 覆盖单次输出上限；0 = 跟随 .env 里的 MAX_TOKENS。
    max_tokens: int = 0
    #: 当前连着的 SSE 客户端数；为 0 时事件会堆在 queue 里没人消费。
    listeners: int = 0

    def turn_elapsed_ms(self) -> int:
        if not self.turn_started:
            return 0
        return int((time.time() - self.turn_started) * 1000)

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "workspace": self.workspace,
            "status": self.status,
            # 正在等用户回答/授权。刷新页面后标签灯要能恢复成黄灯，只有靠这个字段——
            # question / permission 事件本身不进回放队列。
            "pending": self.pending_kind,
            "auto_allow": self.auto_allow,
            "max_rounds": self.max_rounds,
            # 当前生效的厂商/模型，刷新页面后下拉框按这两个值还原
            "provider": self.provider_override or _current_provider(),
            "model": self.model_override or _resolve_model(self.provider_override or None),
            # 界面上直接显示生效值，用户不用去翻 .env 才知道当前是多少
            "max_tokens": self.max_tokens or _default_max_tokens(),
        }


_SESSIONS: dict[str, WebSession] = {}

#: 会话历史库。桌面端开的是同一份文件（WAL + busy_timeout），所以两边看到
#: 的历史是同一批；网页刷新/服务重启后也不会丢。
_DB: Any = None


def _db() -> Any:
    global _DB
    if _DB is None:
        from gangge.runtime.session_db import SessionDB

        _DB = SessionDB()
        _DB.connect()
        logger.info("[Web] 会话库: %s", getattr(_DB, "_db_path", "?"))
    return _DB


#: Event kinds worth replaying to a client that connects late. Pure status
#: chatter is dropped — replaying "正在注册工具" 40 次 after a refresh is noise.
_REPLAYABLE = {
    "user", "text", "thinking", "tool", "tool_result",
    "status_msg", "done", "error", "todos", "queued", "attachments",
}
_MAX_EVENTS = 4000


def _push(session: WebSession, kind: str, **payload: Any) -> None:
    """Hand an event to the event loop that owns this session's queue."""
    event = {"type": kind, **payload}
    if kind in _REPLAYABLE:
        session.events.append(event)
        if len(session.events) > _MAX_EVENTS:
            del session.events[: _MAX_EVENTS // 4]
    try:
        session.main_loop.call_soon_threadsafe(session.queue.put_nowait, event)
    except RuntimeError:  # loop already closed (server shutting down)
        pass


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _new_sandbox() -> str:
    """A fresh project sandbox, mirroring desktop's ``gangge_projects/project_*``."""
    root = Path(os.environ.get("GANGGE_WORKSPACE_ROOT") or Path.cwd())
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    ws = root / "gangge_projects" / f"web_{stamp}_{uuid.uuid4().hex[:4]}"
    ws.mkdir(parents=True, exist_ok=True)
    return str(ws)


# ─────────────────────────────────────────────────────────────
#  Task execution
# ─────────────────────────────────────────────────────────────
def _start_turn(
    session: WebSession,
    text: str,
    task_text: str = "",
    attachments: list[dict] | None = None,
) -> None:
    """Start one turn on its own thread.

    ``text`` 是用户原话，进界面气泡；``task_text`` 是真正喂给模型的那份
    （原话 + 附件说明），两者分开是为了不让一串绝对路径糊在对话框里。
    """
    session.cancel = False
    session.status = "running"
    session.turn_started = time.time()
    _push(session, "user", text=text)
    if attachments:
        # 紧跟在用户气泡后面，刷新回放时也还在
        _push(session, "attachments", items=[
            {"name": a.get("name"), "icon": a.get("icon"), "label": a.get("label")}
            for a in attachments
        ])
    _push(session, "status", status="running")
    session.thread = threading.Thread(
        target=_thread_main, args=(session, task_text or text), daemon=True,
        name=f"gangge-web-{session.id}",
    )
    session.thread.start()


def _thread_main(session: WebSession, text: str) -> None:
    try:
        asyncio.run(_run_session(session, text))
    except Exception as exc:  # noqa: BLE001 - must reach the UI, not the console
        logger.exception("[Web] 会话 %s 任务异常", session.id)
        _push(session, "error", text=str(exc))
        session.status = "error"
    finally:
        if session.status == "running":
            session.status = "idle"
        _push(session, "status", status=session.status)

        # 排队指令：运行中用户又发了消息，说明他希望接着做，而不是被拒绝。
        if session.queue_messages and not session.cancel and session.id in _SESSIONS:
            nxt = session.queue_messages.pop(0)
            try:
                _start_turn(session, nxt.get("text", ""), nxt.get("task", ""),
                            nxt.get("attachments"))
            except Exception:  # noqa: BLE001
                logger.debug("[Web] 启动排队任务失败", exc_info=True)


async def _run_session(session: WebSession, text: str) -> None:
    from gangge.layer4_permission.guard import PermissionDecision
    from gangge.layer5_llm.base import ContentBlock, ContentType, Message, Role
    from gangge.layer5_llm.registry import create_llm
    from gangge.runtime.session import TaskSpec, build_loop

    async def ask_user(question: str) -> str:
        session.pending_event = threading.Event()
        session.pending_kind = "question"
        session.pending_text = question
        session.pending_answer = ""
        _push(session, "question", text=question)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: session.pending_event.wait(600))
        session.pending_kind = ""      # 不再等用户了，标签页黄灯要灭
        return session.pending_answer

    async def ask_permission(request: Any) -> Any:
        tool_name = str(getattr(request, "tool_name", "") or "")
        if session.auto_allow or tool_name in session.remembered_tools:
            return PermissionDecision.ALLOW
        session.pending_event = threading.Event()
        session.pending_kind = "permission"
        session.pending_text = str(getattr(request, "reason", "") or request)
        session.pending_allow = False
        session.pending_remember = False
        _push(
            session,
            "permission",
            text=session.pending_text,
            tool=tool_name,
        )
        loop = asyncio.get_event_loop()
        answered = await loop.run_in_executor(
            None, lambda: session.pending_event.wait(600)
        )
        session.pending_kind = ""      # 超时或已答复，都别再显示黄灯
        if not answered:
            _push(session, "status_msg", text="⏱ 权限确认超时，已拒绝该操作")
            return PermissionDecision.DENY
        if session.pending_allow and session.pending_remember and tool_name:
            session.remembered_tools.add(tool_name)
        return PermissionDecision.ALLOW if session.pending_allow else PermissionDecision.DENY

    start_idx = len(session.messages)
    session.messages.append(Message(role=Role.USER, content=text))

    spec = TaskSpec(
        task=text,
        workspace=session.workspace,
        max_rounds=session.max_rounds,
        auto_allow=session.auto_allow,
    )

    try:
        llm = create_llm(session.provider_override or None)
    except Exception as exc:  # 该厂商没配 Key / 配置不全：给用户一句人话，别让标签页无声失败
        _push(
            session,
            "error",
            text=f"无法初始化厂商「{session.provider_override or '默认'}」：{exc}",
        )
        session.status = "idle"
        return
    if session.model_override:
        try:
            llm.model = session.model_override
        except Exception:  # noqa: BLE001
            logger.debug("[Web] 覆盖模型失败", exc_info=True)
    if session.max_tokens > 0:
        try:
            llm.max_tokens = session.max_tokens
        except Exception:  # noqa: BLE001
            logger.debug("[Web] 覆盖输出上限失败", exc_info=True)

    bundle = await build_loop(
        spec,
        llm=llm,
        ask_user=ask_user,
        ask_permission=ask_permission,
        cancel_check=lambda: session.cancel,
        emit=lambda message: _push(session, "status_msg", text=message),
    )

    async def stream_cb(block: ContentBlock) -> None:
        if block.type == ContentType.TEXT and block.text:
            _push(session, "text", text=block.text)
        elif block.type == ContentType.THINKING and block.text:
            if not getattr(block, "partial", False):
                _push(session, "thinking", text=block.text)
        elif block.type == ContentType.TOOL_USE:
            rendered = json.dumps(block.tool_input, ensure_ascii=False)
            _push(
                session,
                "tool",
                id=block.tool_call_id,
                name=block.tool_name,
                input=rendered[:2000],
            )
            bundle.record({"kind": "tool", "name": block.tool_name, "input": rendered[:2000]})
        elif block.type == ContentType.TOOL_RESULT:
            _push(
                session,
                "tool_result",
                id=block.tool_call_id,
                name=block.tool_name,
                output=(block.text or "")[:4000],
                error=bool(block.is_error),
                ms=int(getattr(block, "elapsed_ms", 0) or 0),
            )
            event = {
                "kind": "tool_result",
                "name": block.tool_name,
                "ms": int(getattr(block, "elapsed_ms", 0) or 0),
                "error": bool(block.is_error),
            }
            if block.is_error:
                # 失败才存摘要：成功条目保持轻量，异常回看又能直接看到原因。
                event["summary"] = _trace_failure_summary(block.text)
            bundle.record(event)

    bundle.loop.set_stream_callback(stream_cb)
    bundle.record({"kind": "task", "text": text[:500], "workspace": session.workspace})

    try:
        result = await bundle.loop.run(session.messages)
        todos = [
            {
                "content": str(t.get("content", "")),
                "status": str(t.get("status", "pending")),
            }
            for t in bundle.loop.todo_state.get_all()
        ]
        # 落库：网页刷新、服务重启、以及桌面端打开同一个库都能看到这一轮。
        try:
            from gangge.runtime.session import aggregate_turn_messages

            fresh = aggregate_turn_messages(session.messages, start_idx)
            if fresh:
                _db().save_turn(session.id, fresh)
                _db().save_turn_summary(session.id, fresh)
        except Exception:  # noqa: BLE001 - 存历史失败不该影响结果展示
            logger.debug("[Web] 保存会话历史失败", exc_info=True)

        cost_text = ""
        try:
            from gangge.pricing import estimate_cost

            tokens_in = (result.total_tokens or {}).get("input", 0)
            tokens_out = (result.total_tokens or {}).get("output", 0)
            cost_text = estimate_cost(
                os.environ.get("LLM_PROVIDER", ""),
                getattr(llm, "model", "") or "",
                tokens_in,
                tokens_out,
                (result.total_tokens or {}).get("cached", 0),
            )
        except Exception:  # noqa: BLE001 - 算不出费用不该挡住收尾
            logger.debug("[Web] 费用估算失败", exc_info=True)

        _push(session, "todos", todos=todos)
        bundle.record({
            "kind": "done",
            "reason": result.completion_reason,
            "rounds": result.total_rounds,
            "tokens": result.total_tokens,
        })
        _push(
            session,
            "done",
            reason=result.completion_reason,
            rounds=result.total_rounds,
            text=result.final_response or "",
            tokens=result.total_tokens,
            cost=cost_text,
            elapsed_ms=session.turn_elapsed_ms(),
        )
        session.status = "idle"
    finally:
        bundle.dispose()


# ─────────────────────────────────────────────────────────────
#  API
# ─────────────────────────────────────────────────────────────
def _get(sid: str) -> WebSession:
    session = _SESSIONS.get(sid)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session


@app.get("/api/sessions")
async def list_sessions() -> dict:
    return {"sessions": [s.snapshot() for s in _SESSIONS.values()]}


@app.post("/api/sessions")
async def create_session(body: NewSession) -> dict:
    raw = body.workspace.strip()
    if raw:
        candidate = Path(raw).expanduser()
        # 只给一个名字时放进 gangge_projects/，别把启动目录堆满临时项目；
        # 绝对路径或带分隔符的路径照用户给的位置建。
        if not candidate.is_absolute() and "/" not in raw and "\\" not in raw:
            root = Path(os.environ.get("GANGGE_WORKSPACE_ROOT") or Path.cwd())
            candidate = root / "gangge_projects" / raw
        workspace = str(candidate.resolve())
        Path(workspace).mkdir(parents=True, exist_ok=True)
    else:
        workspace = _new_sandbox()

    name = body.name.strip() or Path(workspace).name
    try:
        sid = _db().create_session(title=name, workspace=workspace)
    except Exception:  # noqa: BLE001 - 落库失败不该让人无法开工
        logger.debug("[Web] 会话落库失败，退回纯内存会话", exc_info=True)
        sid = uuid.uuid4().hex[:8]

    session = WebSession(
        id=sid,
        name=name,
        workspace=workspace,
        auto_allow=body.auto_allow,
        max_rounds=max(1, min(body.max_rounds, 200)),
        main_loop=asyncio.get_running_loop(),
        queue=asyncio.Queue(),
    )
    _SESSIONS[sid] = session
    logger.info("[Web] 新建会话 %s workspace=%s", sid, workspace)
    return session.snapshot()


def _turns_to_messages(turns: list[dict]) -> list:
    """DB rows → ``Message`` objects the loop can continue from."""
    from gangge.layer5_llm.base import ContentBlock, ContentType, Message, Role

    out: list = []
    for turn in turns:
        role = turn.get("role", "")
        content = turn.get("content", "")
        if role == "user":
            out.append(Message(role=Role.USER, content=content if isinstance(content, str) else str(content)))
        elif role == "assistant":
            blocks: list = []
            if isinstance(content, str):
                if content.strip():
                    blocks.append(ContentBlock(type=ContentType.TEXT, text=content))
            else:
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and b.get("text"):
                        blocks.append(ContentBlock(type=ContentType.TEXT, text=b["text"]))
                    elif b.get("type") == "tool_use":
                        blocks.append(ContentBlock(
                            type=ContentType.TOOL_USE,
                            tool_call_id=b.get("id", ""),
                            tool_name=b.get("name", ""),
                            tool_input=b.get("input") or {},
                        ))
            if blocks:
                out.append(Message(role=Role.ASSISTANT, content=blocks))
        elif role == "tool":
            out.append(Message(role=Role.TOOL, content=[ContentBlock(
                type=ContentType.TOOL_RESULT,
                tool_call_id=turn.get("tool_use_id", ""),
                text=turn.get("content", ""),
                is_error=bool(turn.get("is_error")),
            )]))
    return out


def _turns_to_events(turns: list[dict]) -> list[dict]:
    """DB rows → the UI event stream, so a reopened session looks alive."""
    events: list[dict] = []
    for turn in turns:
        role = turn.get("role", "")
        content = turn.get("content", "")
        if role == "user":
            events.append({"type": "user", "text": content if isinstance(content, str) else str(content)})
        elif role == "assistant":
            if isinstance(content, str):
                if content.strip():
                    events.append({"type": "text", "text": content})
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text"):
                    events.append({"type": "text", "text": b["text"]})
                elif b.get("type") == "tool_use":
                    events.append({
                        "type": "tool",
                        "id": b.get("id", ""),
                        "name": b.get("name", ""),
                        "input": json.dumps(b.get("input") or {}, ensure_ascii=False)[:2000],
                    })
        elif role == "tool":
            events.append({
                "type": "tool_result",
                "id": turn.get("tool_use_id", ""),
                "name": "",
                "output": (turn.get("content") or "")[:4000],
                "error": bool(turn.get("is_error")),
                "ms": 0,
            })
    return events


@app.post("/api/sessions/open")
async def open_session(body: OpenSession) -> dict:
    """打开一个已存在的会话（可能来自桌面端，也可能来自上次的浏览器）。"""
    if body.id in _SESSIONS:
        return _SESSIONS[body.id].snapshot()

    row = _db().get_session(body.id)
    if not row:
        raise HTTPException(status_code=404, detail="会话不存在")

    session = WebSession(
        id=body.id,
        name=row.get("title") or "会话",
        workspace=row.get("workspace") or "",
        auto_allow=False,
        max_rounds=50,
        main_loop=asyncio.get_running_loop(),
        queue=asyncio.Queue(),
    )
    store = _db()
    turns = store.load_turns(body.id, limit=store.MAX_LOAD_MESSAGES)
    session.messages = _turns_to_messages(turns)
    session.events = _turns_to_events(turns)
    _SESSIONS[body.id] = session
    return session.snapshot()


@app.get("/api/history")
async def list_history(limit: int = 100) -> dict:
    """磁盘上的所有会话（桌面端 + 网页），按工作目录分组供左栏展示。"""
    try:
        rows = _db().list_sessions(limit=max(1, min(limit, 500)))
    except Exception:  # noqa: BLE001
        logger.debug("[Web] 读取会话历史失败", exc_info=True)
        rows = []
    return {"sessions": rows}


@app.delete("/api/sessions/{sid}")
async def delete_session(sid: str) -> dict:
    session = _get(sid)
    session.cancel = True
    _SESSIONS.pop(sid, None)
    return {"ok": True}


@app.post("/api/sessions/{sid}/messages")
async def send_message(sid: str, body: NewMessage) -> dict:
    from gangge import attachments as att

    session = _get(sid)
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="消息不能为空")

    # 附件说明只进任务文本，不进气泡：气泡里是用户原话，模型那边才需要路径
    task_text = text + att.describe(body.attachments or [])

    if session.thread is not None and session.thread.is_alive():
        # 运行中再发 = 排队，本轮结束后自动发出。直接拒绝会让人反复按，
        # 插话到正在跑的推理里则会打乱工具调用序列。
        session.queue_messages.append({
            "text": text, "task": task_text, "attachments": body.attachments,
        })
        position = len(session.queue_messages)
        _push(session, "queued", text=text, position=position)
        return {"queued": True, "position": position}

    _start_turn(session, text, task_text, body.attachments)
    return {"ok": True}


#: 单个附件上限。文件只落盘、不进请求体，卡得比桌面端宽松。
_MAX_UPLOAD_BYTES = 100 * 1024 * 1024


@app.post("/api/sessions/{sid}/attach")
async def upload_attachment(sid: str, file: UploadFile = File(...)) -> dict:
    """接收一个附件，存到 workspace 下的 ``.gangge/uploads/``。

    文件不进 LLM 的请求体：Office/PDF 交给 ``office_read`` / ``office_render``
    按路径读，图片交给 ``vision`` 按路径读。这里只负责安全落盘。
    """
    from gangge import attachments as att

    session = _get(sid)
    root = Path(session.workspace or ".").resolve()

    # 只取文件名部分：客户端可能带上 ../ 或者整条路径
    name = Path(file.filename or "upload").name
    if not name or name in (".", ".."):
        raise HTTPException(status_code=400, detail="文件名非法")

    kind = att.kind_of(name)
    if kind is None:
        raise HTTPException(status_code=400, detail=f"不支持的格式：{name}")
    if kind == "legacy":
        raise HTTPException(
            status_code=400,
            detail="旧版 Office 二进制格式读不了，请先另存为 .docx / .xlsx / .pptx",
        )

    dest_dir = root / ".gangge" / "uploads"
    dest_dir.mkdir(parents=True, exist_ok=True)

    target = dest_dir / name
    if target.exists():   # 重名不覆盖，加序号
        stem, suffix = target.stem, target.suffix
        for i in range(1, 1000):
            candidate = dest_dir / f"{stem}_{i}{suffix}"
            if not candidate.exists():
                target = candidate
                break

    size = 0
    try:
        with target.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > _MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="附件超过 100MB")
                out.write(chunk)
    except HTTPException:
        target.unlink(missing_ok=True)
        raise
    except OSError as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.info("[Web] 附件 %s → %s (%d bytes)", name, target, size)
    return {
        "name": target.name,
        "path": str(target),
        "size": size,
        "kind": kind,
        "icon": att.icon_of(name),
        "label": att.label_of(name),
    }


@app.post("/api/sessions/{sid}/cancel")
async def cancel_session(sid: str) -> dict:
    session = _get(sid)
    session.cancel = True
    # Unblock a task thread parked on a question/permission wait.
    if session.pending_event is not None:
        session.pending_event.set()
    _push(session, "status_msg", text="⏹ 已请求取消")
    return {"ok": True}


@app.post("/api/sessions/{sid}/reply")
async def reply(sid: str, body: Answer) -> dict:
    session = _get(sid)
    if session.pending_event is None:
        raise HTTPException(status_code=409, detail="当前没有等待中的提问")
    session.pending_answer = body.value
    session.pending_allow = body.allow
    session.pending_remember = body.remember
    session.pending_event.set()
    return {"ok": True}


@app.get("/api/sessions/{sid}/todos")
async def get_todos(sid: str) -> dict:
    session = _get(sid)
    from gangge.layer3_agent.tools.todo import get_state_for

    state = get_state_for(session.workspace)
    return {
        "todos": [
            {"content": t.get("content", ""), "status": t.get("status", "pending")}
            for t in state.get_all()
        ]
    }


@app.post("/api/sessions/{sid}/config")
async def update_config(sid: str, body: SessionConfig) -> dict:
    """改轮数上限 / 自动允许 / 厂商 / 模型 / 输出上限，下一条消息生效。"""
    session = _get(sid)
    if body.max_rounds is not None:
        session.max_rounds = max(1, min(int(body.max_rounds), 200))
    if body.auto_allow is not None:
        session.auto_allow = bool(body.auto_allow)
    if body.provider is not None:
        from gangge.layer5_llm.providers import PROVIDER_SPECS

        provider = body.provider.strip().lower()
        if provider and provider not in PROVIDER_SPECS:
            raise HTTPException(status_code=400, detail=f"未知厂商：{provider}")
        # 换厂商时清掉上一家的模型名，否则会把另一家的模型发给新厂商
        if provider != session.provider_override:
            session.model_override = ""
        session.provider_override = provider
    if body.model is not None:
        session.model_override = body.model.strip()
    if body.max_tokens is not None:
        # 0 表示恢复默认（跟随 .env）；上限给足，但挡住明显的误输入
        session.max_tokens = max(0, min(int(body.max_tokens), 262_144))
    return session.snapshot()


@app.post("/api/sessions/{sid}/rename")
async def rename_session(sid: str, body: RenameBody) -> dict:
    session = _get(sid)
    name = body.name.strip()[:80] or "未命名任务"
    session.name = name
    try:
        _db().update_session(sid, title=name)
    except Exception:  # noqa: BLE001
        logger.debug("[Web] 重命名落库失败", exc_info=True)
    _push(session, "renamed", name=name)
    return session.snapshot()


@app.get("/api/pricing")
async def get_pricing() -> dict:
    """内置价表 + 用户自定义价 + 当前模型实际命中哪一档。

    价格单位是**每千 token**（= 官方每百万价 ÷ 1000），和内置表一致。
    """
    from gangge.pricing import (
        _match,
        builtin_price,
        load_custom_pricing,
        pricing_file_path,
    )

    # 只读模型名，别 create_llm()：那会建 httpx/SSL 连接池，白白多花 ~220ms。
    provider = ""
    model = ""
    try:
        from gangge.layer5_llm.registry import current_provider, resolve_model

        provider = current_provider()
        model = resolve_model(provider) or ""
    except Exception:  # noqa: BLE001
        logger.debug("[Web] 读取当前模型失败", exc_info=True)
        provider = provider or (os.environ.get("LLM_PROVIDER", "") or "").strip().lower()

    prices, source = _match(provider, model.lower())
    # 内置价单独给一份：自定义价覆盖之后，只有它还能告诉用户"官方价是多少"，
    # 设置面板的「回填内置价」按钮依赖它。
    builtin, builtin_source = builtin_price(provider, model.lower())
    return {
        "file": str(pricing_file_path()),
        "custom": load_custom_pricing(),
        "current": {
            "provider": provider,
            "model": model,
            "prices": prices or {},
            "source": source,
        },
        "builtin": {
            "prices": builtin or {},
            "source": builtin_source,
        },
    }


@app.post("/api/pricing")
async def set_pricing(body: PriceBody) -> dict:
    """保存一个模型的价格。写盘后立刻生效，不用重启。"""
    from gangge.pricing import set_model_price

    if not body.provider.strip() or not body.model.strip():
        raise HTTPException(status_code=400, detail="provider 和 model 都不能为空")
    if body.input_price < 0 or body.output_price < 0:
        raise HTTPException(status_code=400, detail="价格不能为负")

    path = set_model_price(
        body.provider.strip(),
        body.model.strip(),
        body.input_price,
        body.output_price,
        body.cached_price,
    )
    logger.info("[Web] 更新定价 %s/%s → %s", body.provider, body.model, path)
    return {"ok": True, "file": str(path)}


@app.get("/api/models")
async def list_models() -> dict:
    """当前厂商/模型 + 每家厂商各自的可选模型。

    ``providers`` 是给界面的完整目录：切厂商时模型下拉要跟着换；
    ``configured`` 表示该厂商在 .env 里有没有配 Key（Ollama 本地不需要）。
    """
    from gangge.layer5_llm.providers import PROVIDER_SPECS
    from gangge.layer5_llm.registry import _load_env

    try:
        _load_env()
    except Exception:  # noqa: BLE001 - 没配 .env 也要能打开界面
        logger.debug("[Web] 读取 .env 失败", exc_info=True)

    provider = _current_provider()
    current = _resolve_model(provider or None)

    providers = []
    for pid, spec in PROVIDER_SPECS.items():
        key = (os.environ.get(spec.get("api_key_env", ""), "") or "").strip()
        configured = pid == "ollama" or bool(key)
        if pid == "custom":
            configured = bool((os.environ.get("CUSTOM_BASE_URL", "") or "").strip())
        models = list(spec.get("models", []))
        env_model = (os.environ.get(f"{pid.upper()}_MODEL", "") or "").strip()
        if env_model and env_model not in models:
            models.insert(0, env_model)
        providers.append({
            "id": pid,
            "label": spec.get("label", pid),
            "configured": configured,
            "default_model": spec.get("model_default", ""),
            "models": models,
        })

    return {
        "current": current,
        "current_provider": provider,
        # 兼容旧字段：当前厂商的模型候选
        "options": next((p["models"] for p in providers if p["id"] == provider), []),
        "providers": providers,
    }


#: 文件浏览的安全边界：只能在该会话的 workspace 内。
_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    "dist", "build", ".pytest_cache", ".ruff_cache", ".mypy_cache",
}
_MAX_FILE_BYTES = 512 * 1024


def _safe_path(session: WebSession, rel: str) -> Path:
    root = Path(session.workspace).resolve()
    target = (root / (rel or "")).resolve()
    if target != root and root not in target.parents:
        raise HTTPException(status_code=403, detail="路径越界")
    return target


def _open_with_system(path: Path) -> None:
    """用运行 Web 服务这台机器的默认程序打开文件。"""
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


@app.get("/api/sessions/{sid}/files")
async def list_files(sid: str, path: str = "") -> dict:
    """列目录。只读、限定在 workspace 内、跳过依赖与缓存目录。"""
    session = _get(sid)
    target = _safe_path(session, path)
    if not target.is_dir():
        raise HTTPException(status_code=404, detail="目录不存在")

    entries = []
    try:
        for entry in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if entry.name.startswith(".") and entry.name not in {".ganggerules", ".gangge"}:
                continue
            if entry.is_dir() and entry.name in _SKIP_DIRS:
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            entries.append({
                "name": entry.name,
                "path": str(entry.relative_to(Path(session.workspace).resolve())).replace("\\", "/"),
                "dir": entry.is_dir(),
                "size": 0 if entry.is_dir() else stat.st_size,
                "mtime": int(stat.st_mtime),
            })
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"path": path, "entries": entries}


@app.get("/api/sessions/{sid}/file")
async def read_workspace_file(sid: str, path: str) -> dict:
    """读一个文件的内容（只读，超过 512KB 截断）。"""
    session = _get(sid)
    target = _safe_path(session, path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    try:
        size = target.stat().st_size
        with target.open("rb") as handle:
            raw = handle.read(_MAX_FILE_BYTES)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    truncated = size > _MAX_FILE_BYTES
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "path": path, "binary": True, "size": size, "truncated": truncated,
            "content": "(二进制文件，暂不预览)",
        }
    return {
        "path": path, "binary": False, "size": size,
        "truncated": truncated, "content": text,
    }


class FileOpenRequest(BaseModel):
    path: str


@app.post("/api/sessions/{sid}/file/open")
async def open_workspace_file(sid: str, body: FileOpenRequest) -> dict:
    """用系统默认程序打开文件（作用于运行 Web 服务的这台机器）。

    网页里点“系统打开”走这里：Word / Excel / 图片 / PDF 都会交给本机
    关联程序，不用先下载再找路径。
    """
    session = _get(sid)
    target = _safe_path(session, body.path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    try:
        _open_with_system(target)
    except Exception as exc:  # noqa: BLE001 - 打不开就把原因给用户
        raise HTTPException(status_code=400, detail=f"无法打开：{exc}") from exc
    return {"ok": True, "path": str(target)}


@app.get("/api/sessions/{sid}/file/raw")
async def raw_workspace_file(sid: str, path: str) -> FileResponse:
    """原样返回文件，供浏览器新标签页直接查看（图片/PDF/HTML）。"""
    session = _get(sid)
    target = _safe_path(session, path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(target)


@app.get("/api/sessions/{sid}/history")
async def get_history(sid: str) -> dict:
    """已发生的事件，供刷新/新标签页回放。"""
    return {"events": _get(sid).events}


@app.get("/api/sessions/{sid}/events")
async def events(sid: str, request: Request) -> StreamingResponse:
    session = _get(sid)

    async def generator():
        first_listener = session.listeners == 0
        session.listeners += 1
        try:
            if first_listener and session.status != "running":
                # 没有客户端在听的时候，事件会一直堆在 queue 里。新标签页连上来
                # 若把上一轮任务残留的 done/error 当新事件发出去，页面会立刻亮红
                # 灯（明明什么都没跑）。已完成的事件已经进了 session.events，
                # 走 /history 回放即可，这里直接丢掉队列里的陈货。
                while not session.queue.empty():
                    try:
                        session.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
            yield _sse({"type": "hello", "id": sid, "workspace": session.workspace})
            while True:
                if await request.is_disconnected():
                    return
                try:
                    event = await asyncio.wait_for(session.queue.get(), timeout=10)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield _sse(event)
        finally:
            session.listeners = max(0, session.listeners - 1)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/logo.svg", include_in_schema=False)
async def logo() -> FileResponse:
    """标签页图标（产品 LOGO）。"""
    return FileResponse(STATIC_DIR / "gangge-logo.svg", media_type="image/svg+xml")


# ─────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────
def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Gangge Web — 每标签一个任务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--workspace", default="", help="新会话的默认工作目录")
    args = parser.parse_args()

    if args.workspace:
        os.environ["GANGGE_WORKSPACE_ROOT"] = args.workspace

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    print(f"\n  Gangge Web: http://{args.host}:{args.port}\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
