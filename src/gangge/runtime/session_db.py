"""SQLite session store — conversation history shared by every shell.

This used to live inside ``desktop/app.py``. That made it unreachable from the
web backend without importing PyQt6, so the browser shell had no history: every
refresh (and every server restart) threw the conversation away, and the two
shells could not see each other's sessions.

Nothing here depends on a UI. The desktop window and the web server now open the
same database file, which is safe across processes: WAL journal plus a 5 second
busy timeout.

Schema (all created on first connect):

* ``sessions``  — one row per conversation, grouped by ``workspace``
* ``messages``  — the LLM-facing transcript
* ``tool_calls``— tool name / input / output / error / diff per round
* ``turns``     — per-turn summary + FTS5 index for cross-session recall
"""

from __future__ import annotations

import json
import logging
import sqlite3
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def default_db_path() -> Path:
    """The database every shell should agree on.

    Project-local first (so a checkout keeps its own history); the home
    directory only when that is not writable.
    """
    project_root = Path(__file__).resolve().parents[3]
    local_dir = project_root / ".gangge_data"
    try:
        local_dir.mkdir(parents=True, exist_ok=True)
        probe = local_dir / ".write_test"
        probe.touch()
        probe.unlink()
        return local_dir / "sessions.db"
    except (OSError, PermissionError):
        home_dir = Path.home() / ".gangge"
        home_dir.mkdir(parents=True, exist_ok=True)
        return home_dir / "sessions.db"


class SessionDB:
    """SQLite session store — saves/loads conversation history."""

    MAX_LOAD_MESSAGES = 500  # Only load recent N messages to prevent UI freeze

    def __init__(self, db_path: str = ""):
        self._db_path = db_path or str(default_db_path())
        self._conn: sqlite3.Connection | None = None
        self._fts5_enabled = False

    def connect(self) -> None:
        try:
            self._conn = sqlite3.connect(
                self._db_path,
                check_same_thread=False,  # 允许跨线程访问（Qt / web 工作线程）
            )
            self._conn.row_factory = sqlite3.Row

            # ── 性能与并发 PRAGMA ───────────────────────────────
            # WAL + busy_timeout 是桌面端与 Web 端同时读写同一份库的前提：
            # 没有它，两个进程会互相撞 "database is locked"。
            pragmas = [
                "PRAGMA journal_mode=WAL",
                "PRAGMA synchronous=NORMAL",
                "PRAGMA cache_size=-8000",
                "PRAGMA temp_store=MEMORY",
                "PRAGMA mmap_size=268435456",
                "PRAGMA busy_timeout=5000",
                "PRAGMA foreign_keys=ON",
            ]
            for pragma in pragmas:
                self._conn.execute(pragma)
            self._init_tables()
        except sqlite3.OperationalError as exc:
            logger.warning("会话库打开失败，改用临时文件: %s", exc)
            fallback = Path(tempfile.gettempdir()) / "gangge_sessions.db"
            self._db_path = str(fallback)
            self._conn = sqlite3.connect(self._db_path)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=DELETE")  # safer mode
            self._init_tables()

    def close(self) -> None:
        if self._conn:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            self._conn.close()
            self._conn = None

    # ── schema ──
    def _init_tables(self) -> None:
        c = self._conn.cursor()
        c.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT 'New Session',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                workspace TEXT DEFAULT '',
                provider TEXT DEFAULT '',
                model TEXT DEFAULT '',
                task_count INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                content_type TEXT NOT NULL DEFAULT 'text',
                tool_use_id TEXT,
                is_error INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tool_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                round_num INTEGER DEFAULT 0,
                tool_name TEXT NOT NULL,
                tool_input TEXT DEFAULT '',
                tool_output TEXT DEFAULT '',
                is_error INTEGER DEFAULT 0,
                diff TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                turn_num INTEGER DEFAULT 0,
                user_text TEXT DEFAULT '',
                summary TEXT DEFAULT '',
                tools_used TEXT DEFAULT '',
                assistant_text TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );
        """)
        # FTS5 全文搜索（某些 SQLite 构建可能不支持）
        try:
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5("
                "user_text, summary, tools_used, assistant_text, content='turns', content_rowid='id'"
                ")"
            )
            self._fts5_enabled = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("FTS5 不可用，降级到 LIKE 查询: %s", exc)
            self._fts5_enabled = False
        self._conn.commit()
        self._migrate_if_needed()

    def _migrate_if_needed(self) -> None:
        """为旧库补上后加的字段（messages 与 turns）。"""
        for sql in (
            "ALTER TABLE messages ADD COLUMN content_type TEXT NOT NULL DEFAULT 'text'",
            "ALTER TABLE messages ADD COLUMN tool_use_id TEXT",
            "ALTER TABLE messages ADD COLUMN is_error INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE turns ADD COLUMN assistant_text TEXT DEFAULT ''",
        ):
            try:
                self._conn.execute(sql)
            except Exception:
                pass  # 字段已存在

    # ── sessions ──
    def create_session(self, title: str = "", workspace: str = "") -> str:
        if not title:
            title = "新会话"
        sid = uuid.uuid4().hex[:8]
        now = datetime.now().isoformat()
        self._conn.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at, workspace) VALUES (?,?,?,?,?)",
            (sid, title, now, now, workspace),
        )
        self._conn.commit()
        return sid

    def list_sessions(self, limit: int = 50, workspace: str = "") -> list[dict]:
        """列出会话，可选按 workspace 过滤。"""
        if workspace:
            rows = self._conn.execute(
                "SELECT id, title, created_at, updated_at, workspace, task_count "
                "FROM sessions WHERE workspace=? ORDER BY updated_at DESC LIMIT ?",
                (workspace, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, title, created_at, updated_at, workspace, task_count "
                "FROM sessions ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "id": r[0],
                "title": r[1],
                "created_at": r[2],
                "updated_at": r[3],
                "workspace": r[4] or "",
                "task_count": r[5] or 0,
            }
            for r in rows
        ]

    def get_session(self, sid: str) -> dict | None:
        r = self._conn.execute(
            "SELECT id, title, created_at, updated_at, workspace, provider, model, task_count "
            "FROM sessions WHERE id=?",
            (sid,),
        ).fetchone()
        if not r:
            return None
        return {
            "id": r[0], "title": r[1], "created_at": r[2], "updated_at": r[3],
            "workspace": r[4] or "", "provider": r[5] or "", "model": r[6] or "",
            "task_count": r[7] or 0,
        }

    def update_session(self, sid: str, **kw) -> None:
        if not kw:
            return
        sets = ", ".join(f"{k}=?" for k in kw)
        vals = list(kw.values()) + [sid]
        self._conn.execute(f"UPDATE sessions SET {sets} WHERE id=?", vals)
        self._conn.commit()

    def delete_session(self, sid: str) -> None:
        self._conn.execute("DELETE FROM tool_calls WHERE session_id=?", (sid,))
        self._conn.execute("DELETE FROM messages WHERE session_id=?", (sid,))
        self._conn.execute("DELETE FROM turns WHERE session_id=?", (sid,))
        if self._fts5_enabled:
            try:
                self._conn.execute(
                    "DELETE FROM turns_fts WHERE rowid IN "
                    "(SELECT id FROM turns WHERE session_id=?)",
                    (sid,),
                )
            except Exception:
                pass
        self._conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
        self._conn.commit()

    # ── messages ──
    def save_turn(self, sid: str, messages: list[dict]) -> None:
        """保存一轮对话的所有聚合消息。

        ``content`` 是 list 时（assistant 含 tool_use）序列化为 JSON，
        加载时会还原成同样的结构。
        """
        now = datetime.now().isoformat()
        for msg in messages:
            role = msg["role"]
            content = msg["content"]
            tool_use_id = msg.get("tool_use_id", None)

            if isinstance(content, list):
                content_str = json.dumps(content, ensure_ascii=False)
                content_type = "json"
            else:
                content_str = str(content)
                content_type = "text"

            is_error = 1 if msg.get("is_error", False) else 0

            self._conn.execute(
                "INSERT INTO messages (session_id, role, content, content_type, tool_use_id, is_error, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (sid, role, content_str, content_type, tool_use_id, is_error, now),
            )
        self._conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, sid))
        self._conn.commit()

    def count_messages(self, sid: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id=?", (sid,)
        ).fetchone()
        return row[0] if row else 0

    def load_turns(self, sid: str, limit: int = 200) -> list[dict]:
        """加载会话消息，重建成 LLM API 标准消息格式。"""
        rows = self._conn.execute(
            """
            SELECT role, content, content_type, tool_use_id, is_error
            FROM (
                SELECT id, role, content, content_type, tool_use_id, is_error
                FROM messages
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
            ) ORDER BY id ASC
            """,
            (sid, limit),
        ).fetchall()

        messages = []
        for r in rows:
            content = json.loads(r["content"]) if r["content_type"] == "json" else r["content"]
            msg = {"role": r["role"], "content": content}
            if r["tool_use_id"]:
                msg["tool_use_id"] = r["tool_use_id"]
                msg["is_error"] = bool(r["is_error"])
            messages.append(msg)
        return messages

    # ── tool calls ──
    def save_tool_call(
        self, sid: str, round_num: int, tool_name: str, tool_input: str,
        tool_output: str, is_error: bool, diff: str = "",
    ) -> None:
        self._conn.execute(
            "INSERT INTO tool_calls (session_id, round_num, tool_name, tool_input, tool_output, "
            "is_error, diff, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (sid, round_num, tool_name, tool_input, tool_output,
             1 if is_error else 0, diff, datetime.now().isoformat()),
        )
        self._conn.commit()

    def load_tool_calls(self, sid: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT round_num, tool_name, tool_input, tool_output, is_error, diff "
            "FROM tool_calls WHERE session_id=? ORDER BY id",
            (sid,),
        ).fetchall()
        return [
            {"round": r[0], "tool_name": r[1], "input": r[2], "output": r[3],
             "is_error": bool(r[4]), "diff": r[5]}
            for r in rows
        ]

    # ── turn summaries + search ──
    def save_turn_summary(self, sid: str, msgs: list[dict]) -> None:
        """从聚合消息中提取摘要，存入 turns 表 + FTS5 索引。"""
        user_text = ""
        assistant_text = ""
        tools_used: list[dict] = []

        for msg in msgs:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user":
                # 一轮里可能有多条 user 消息（用户中途补一句"别动 X"），
                # 覆盖式写入会让中途的约束连库都进不去。
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = " ".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                if text.strip():
                    user_text = f"{user_text}\n{text}".strip() if user_text else text
            elif role == "assistant":
                if isinstance(content, str):
                    assistant_text = content
                elif isinstance(content, list):
                    for b in content:
                        if not isinstance(b, dict):
                            continue
                        if b.get("type") == "text" and b.get("text"):
                            assistant_text += b["text"]
                        elif b.get("type") == "tool_use":
                            inp = b.get("input", {})
                            inp_str = json.dumps(inp, ensure_ascii=False)[:300] if inp else ""
                            tools_used.append({"name": b.get("name", ""), "input": inp_str})

        # 600 而不是 300：摘要也用于检索和跨会话回放，太短只剩开场白。
        summary = assistant_text[:600] + ("..." if len(assistant_text) > 600 else "")
        tools_json = json.dumps(tools_used, ensure_ascii=False)
        now = datetime.now().isoformat()
        row = self._conn.execute(
            "SELECT COUNT(*) FROM turns WHERE session_id=?", (sid,)
        ).fetchone()
        turn_num = (row[0] if row else 0) + 1

        cursor = self._conn.execute(
            "INSERT INTO turns (session_id, turn_num, user_text, summary, tools_used, "
            "assistant_text, created_at) VALUES (?,?,?,?,?,?,?)",
            (sid, turn_num, user_text, summary, tools_json, assistant_text, now),
        )
        if self._fts5_enabled:
            try:
                self._conn.execute(
                    "INSERT INTO turns_fts (rowid, user_text, summary, tools_used, assistant_text) "
                    "VALUES (?,?,?,?,?)",
                    (cursor.lastrowid, user_text, summary, tools_json, assistant_text),
                )
            except Exception:
                pass
        self._conn.commit()

    def search_turns(self, query: str, sid: str = "", limit: int = 10) -> list[dict]:
        """搜索历史对话轮次（FTS5 优先，降级 LIKE）。"""
        if not query.strip():
            return []
        if self._fts5_enabled:
            try:
                sql = (
                    "SELECT t.id, t.session_id, t.turn_num, t.user_text, t.summary, "
                    "t.tools_used, t.assistant_text, t.created_at "
                    "FROM turns_fts f JOIN turns t ON t.id = f.rowid "
                    "WHERE turns_fts MATCH ? "
                )
                params: list = [query]
                if sid:
                    sql += " AND t.session_id = ?"
                    params.append(sid)
                sql += " ORDER BY t.id DESC LIMIT ?"
                params.append(limit)
                return [dict(r) for r in self._conn.execute(sql, params).fetchall()]
            except Exception:
                pass
        return [dict(r) for r in self._like_search(query, sid, limit)]

    def load_turn_summaries(self, sid: str, exclude_recent: int = 0) -> list[dict]:
        """加载会话的 turn 摘要（用于注入历史上下文）。

        ``exclude_recent``: 排除最近 N 条（这些会用完整消息恢复）。

        在 SQL 里截断 ``assistant_text``：它是完整回复（可能上万字），
        几百轮的老会话直接 SELECT 全文会把几百 MB 拉进内存，切会话就卡住。
        """
        sql = ("SELECT turn_num, user_text, summary, tools_used, created_at, "
               "substr(assistant_text, 1, 400) AS assistant_text "
               "FROM turns WHERE session_id = ?")
        params: list = [sid]
        if exclude_recent > 0:
            count_row = self._conn.execute(
                "SELECT COUNT(*) FROM turns WHERE session_id=?", (sid,)
            ).fetchone()
            total = count_row[0] if count_row else 0
            if total <= exclude_recent:
                return []
            sql += " ORDER BY id ASC LIMIT ?"
            params.append(total - exclude_recent)
        else:
            sql += " ORDER BY id ASC"
        try:
            rows = self._conn.execute(sql, params).fetchall()
        except Exception:
            # 旧库还没迁移出 assistant_text：退回旧字段，别让整段历史消失。
            fallback = sql.replace(
                ", substr(assistant_text, 1, 400) AS assistant_text", ""
            )
            rows = self._conn.execute(fallback, params).fetchall()
        return [dict(r) for r in rows]

    def increment_task_count(self, sid: str) -> None:
        self._conn.execute(
            "UPDATE sessions SET task_count=task_count+1, updated_at=? WHERE id=?",
            (datetime.now().isoformat(), sid),
        )
        self._conn.commit()

    def _like_search(self, query: str, sid: str, limit: int) -> list:
        pattern = f"%{query}%"
        sql = (
            "SELECT id, session_id, turn_num, user_text, summary, tools_used, "
            "assistant_text, created_at FROM turns "
            "WHERE (user_text LIKE ? OR summary LIKE ? OR assistant_text LIKE ? OR tools_used LIKE ?) "
        )
        params = [pattern, pattern, pattern, pattern]
        if sid:
            sql += " AND session_id = ?"
            params.append(sid)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return self._conn.execute(sql, params).fetchall()
