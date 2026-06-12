"""
Gangge Code Desktop — PyQt6 GUI for the AI Coding Assistant.

Features:
  - 4 LLM providers (DeepSeek, OpenAI, Anthropic, Ollama)
  - Streaming output with syntax highlighting
  - Session persistence (SQLite — save/resume conversations)
  - File diff panel (see exactly what changed)
  - Project context auto-injection (ARCH.md + directory structure)
  - Test verification (auto-prompt after file modifications)
  - Batch task queue (multi-line input, sequential execution)
  - Plan confirmation dialog (approve/reject LLM's plan)
  - File browser with click-to-preview
"""

import asyncio
import difflib
import json
import logging
import os
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.request import urlopen, Request
from urllib.error import URLError

from PyQt6.QtCore import QSettings, QSize, Qt, QThread, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QColor,
    QFont,
    QKeySequence,
    QPixmap,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
)
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QTextEdit,
    QToolBar,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtGui import QShortcut

# ── Fix import path ──────────────────────────────────────────────
_SRC = str(Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from gangge.layer3_agent.loop import AgenticLoop, LoopConfig, ToolExecution
from gangge.layer3_agent.prompts.system import build_system_prompt
from gangge.layer3_agent.tools.registry import ToolRegistry
from gangge.layer5_llm.base import (
    BaseLLM,
    ContentBlock,
    ContentType,
    Message,
    Role,
)
from gangge.layer5_llm.registry import create_llm
from gangge.layer4_permission.guard import PermissionDecision, PermissionGuard, PermissionRequest
from gangge.i18n import get_language, set_language, t as _t

# ── LLM Provider definitions ─────────────────────────────────────
PROVIDER_CONFIGS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "label": "DeepSeek",
        "api_key_env": "DEEPSEEK_API_KEY",
        "model_default": "deepseek-chat",
        "models": [
            "deepseek-chat", "deepseek-reasoner",
            "deepseek-chat-v4", "deepseek-reasoner-v4",
        ],
        "base_url_editable": True,
        "base_url_default": "https://api.deepseek.com/v1",
    },
    "qwen": {
        "label": _t("qwen_label"),
        "api_key_env": "QWEN_API_KEY",
        "model_default": "qwen-max",
        "models": [
            "qwen-max", "qwen-max-latest",
            "qwen-plus", "qwen-plus-latest",
            "qwen-turbo", "qwen-turbo-latest",
            "qwen-long",
            "qwen-coder-plus", "qwen-coder-plus-latest",
            "qwen-coder-turbo", "qwen-coder-turbo-latest",
            "qwen2.5-72b-instruct", "qwen2.5-32b-instruct",
            "qwen2.5-14b-instruct", "qwen2.5-7b-instruct",
            "qwen2.5-coder-32b-instruct", "qwen2.5-coder-14b-instruct",
            "qwen2.5-coder-7b-instruct",
        ],
        "base_url_editable": True,
        "base_url_default": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    },
    "zhipu": {
        "label": _t("zhipu_label"),
        "api_key_env": "ZHIPU_API_KEY",
        "model_default": "glm-4-plus",
        "models": [
            "glm-4-plus", "glm-4-0520", "glm-4", "glm-4-air", "glm-4-airx",
            "glm-4-long", "glm-4-flash", "glm-4-flashx",
            "glm-4v", "glm-4v-plus",
            "codegeex-4",
        ],
        "base_url_editable": True,
        "base_url_default": "https://open.bigmodel.cn/api/paas/v4",
    },
    "moonshot": {
        "label": _t("moonshot_label"),
        "api_key_env": "MOONSHOT_API_KEY",
        "model_default": "moonshot-v1-auto",
        "models": [
            "moonshot-v1-auto", "moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k",
        ],
        "base_url_editable": True,
        "base_url_default": "https://api.moonshot.cn/v1",
    },
    "baichuan": {
        "label": _t("baichuan_label"),
        "api_key_env": "BAICHUAN_API_KEY",
        "model_default": "Baichuan4",
        "models": [
            "Baichuan4", "Baichuan3-Turbo", "Baichuan3-Turbo-128k",
            "Baichuan2-Turbo", "Baichuan2-53B",
        ],
        "base_url_editable": True,
        "base_url_default": "https://api.baichuan-ai.com/v1",
    },
    "yi": {
        "label": _t("yi_label"),
        "api_key_env": "YI_API_KEY",
        "model_default": "yi-large",
        "models": [
            "yi-large", "yi-large-rag", "yi-large-turbo",
            "yi-medium", "yi-medium-200k", "yi-spark",
            "yi-lightning",
        ],
        "base_url_editable": True,
        "base_url_default": "https://api.lingyiwanwu.com/v1",
    },
    "minimax": {
        "label": _t("minimax_label"),
        "api_key_env": "MINIMAX_API_KEY",
        "model_default": "MiniMax-Text-01",
        "models": [
            "MiniMax-Text-01", "abab6.5s-chat",
            "abab6.5-chat", "abab6.5g-chat",
            "abab5.5-chat", "abab5.5s-chat",
        ],
        "base_url_editable": True,
        "base_url_default": "https://api.minimax.chat/v1",
    },
    "stepfun": {
        "label": _t("stepfun_label"),
        "api_key_env": "STEPFUN_API_KEY",
        "model_default": "step-2-16k",
        "models": [
            "step-2-16k", "step-2-flash",
            "step-1-8k", "step-1-32k", "step-1-128k",
            "step-1v-8k", "step-1v-32k",
        ],
        "base_url_editable": True,
        "base_url_default": "https://api.stepfun.com/v1",
    },
    "siliconflow": {
        "label": _t("siliconflow_label"),
        "api_key_env": "SILICONFLOW_API_KEY",
        "model_default": "deepseek-ai/DeepSeek-V3",
        "models": [
            "deepseek-ai/DeepSeek-V3", "deepseek-ai/DeepSeek-R1",
            "Qwen/Qwen2.5-72B-Instruct", "Qwen/Qwen2.5-Coder-32B-Instruct",
            "Qwen/Qwen2.5-7B-Instruct",
            "THUDM/glm-4-9b-chat",
            "meta-llama/Meta-Llama-3.1-405B-Instruct",
            "meta-llama/Meta-Llama-3.1-70B-Instruct",
            "mistralai/Mixtral-8x7B-Instruct-v0.1",
        ],
        "base_url_editable": True,
        "base_url_default": "https://api.siliconflow.cn/v1",
    },
    "openai": {
        "label": "OpenAI",
        "api_key_env": "OPENAI_API_KEY",
        "model_default": "gpt-4o",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-3.5-turbo"],
        "base_url_editable": False,
        "base_url_default": "https://api.openai.com/v1",
    },
    "anthropic": {
        "label": "Anthropic Claude",
        "api_key_env": "ANTHROPIC_API_KEY",
        "model_default": "claude-sonnet-4-20250514",
        "models": [
            "claude-sonnet-4-20250514",
            "claude-sonnet-4-20250514:thinking",
            "claude-4-20250514",
            "claude-4-20250514:thinking",
            "claude-3-5-sonnet-20241022",
            "claude-3-5-haiku-20241022",
            "claude-3-opus-20240229",
            "claude-3-sonnet-20240229",
            "claude-3-haiku-20240307",
        ],
        "base_url_editable": False,
        "base_url_default": "",
    },
    "ollama": {
        "label": _t("ollama_label"),
        "api_key_env": "OLLAMA_API_KEY",
        "model_default": "llama3.1",
        "models": [
            "llama3.1", "llama3.1:8b", "llama3.1:70b",
            "llama3.2", "llama3.2:1b", "llama3.2:3b",
            "llama3.3", "llama3.3:70b",
            "llama3", "llama3:8b", "llama3:70b",
            "mistral", "mistral:7b", "mistral-nemo",
            "mixtral", "mixtral:8x7b", "mixtral:8x22b",
            "qwen2.5", "qwen2.5:7b", "qwen2.5:14b", "qwen2.5:32b", "qwen2.5:72b",
            "qwen2.5-coder", "qwen2.5-coder:7b", "qwen2.5-coder:14b", "qwen2.5-coder:32b",
            "deepseek-coder", "deepseek-coder:6.7b", "deepseek-coder:33b",
            "deepseek-r1", "deepseek-r1:8b", "deepseek-r1:14b", "deepseek-r1:32b", "deepseek-r1:70b",
            "deepseek-v3", "deepseek-v3:24b",
            "codellama", "codellama:7b", "codellama:13b", "codellama:34b",
            "gemma2", "gemma2:9b", "gemma2:27b",
            "phi3", "phi3:mini", "phi3:medium", "phi3:14b",
            "mistral-large", "mixtral-large:123b",
        ],
        "base_url_editable": True,
        "base_url_default": "http://localhost:11434/v1",
    },
    "custom": {
        "label": _t("custom_label"),
        "api_key_env": "CUSTOM_API_KEY",
        "model_default": "",
        "models": [],
        "base_url_editable": True,
        "base_url_default": "",
    },
}

DARK_STYLESHEET = """
QMainWindow,QDialog,QWidget{background-color:#0d1117;color:#c9d1d9;font-family:"Segoe UI","Microsoft YaHei UI",sans-serif;font-size:13px}
QLabel{color:#c9d1d9;background:transparent;border:none}
QLabel[heading="true"]{color:#58a6ff;font-size:15px;font-weight:bold;padding:4px 0}
QGroupBox{border:1px solid #30363d;border-radius:8px;margin-top:14px;padding:16px 12px 12px;font-weight:600;color:#8b949e}
QGroupBox::title{subcontrol-origin:margin;left:14px;padding:0 6px;color:#58a6ff}
QPushButton{background-color:#238636;color:#fff;border:1px solid rgba(240,246,252,0.1);border-radius:6px;padding:6px 16px;font-size:13px;font-weight:500;min-height:28px}
QPushButton:hover{background-color:#2ea043}
QPushButton:disabled{background-color:#21262d;color:#484f58;border-color:#30363d}
QPushButton[primary="true"]{background-color:#1f6feb}
QPushButton[primary="true"]:hover{background-color:#388bfd}
QPushButton[danger="true"]{background-color:#da3633}
QPushButton[danger="true"]:hover{background-color:#f85149}
QLineEdit,QSpinBox,QPlainTextEdit,QTextEdit,QTextBrowser,QComboBox{background-color:#161b22;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;padding:6px 10px;selection-background-color:#264f78}
QLineEdit:focus,QComboBox:focus,QPlainTextEdit:focus{border-color:#58a6ff}
QComboBox::drop-down{border:none;width:24px}
QComboBox::down-arrow{image:none;border-left:5px solid transparent;border-right:5px solid transparent;border-top:6px solid #8b949e;margin-right:6px}
QComboBox QAbstractItemView{background-color:#161b22;color:#c9d1d9;border:1px solid #30363d;selection-background-color:#1f6feb}
QTabWidget::pane{border:1px solid #30363d;border-radius:6px;background:#0d1117}
QTabBar::tab{background:#161b22;color:#8b949e;border:1px solid #30363d;border-bottom:none;border-top-left-radius:6px;border-top-right-radius:6px;padding:8px 18px;margin-right:2px;font-size:12px}
QTabBar::tab:selected{background:#0d1117;color:#f0f6fc;border-bottom:2px solid #f78166}
QTabBar::tab:hover:!selected{background:#21262d;color:#c9d1d9}
QTableWidget{background-color:#0d1117;alternate-background-color:#161b22;border:1px solid #30363d;gridline-color:#21262d}
QTableWidget::item{padding:4px 8px}
QHeaderView::section{background-color:#161b22;color:#8b949e;padding:6px 8px;border:none;border-bottom:1px solid #30363d;font-weight:600;font-size:12px}
QTreeWidget{background-color:#0d1117;border:1px solid #30363d;border-radius:6px;color:#c9d1d9}
QScrollBar:vertical{background:#0d1117;width:12px;border:none}
QScrollBar::handle:vertical{background:#30363d;border-radius:6px;min-height:30px}
QScrollBar::handle:vertical:hover{background:#484f58}
QProgressBar{background:#161b22;border:1px solid #30363d;border-radius:6px;text-align:center;color:#c9d1d9;height:20px}
QProgressBar::chunk{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #238636,stop:1 #2ea043);border-radius:5px}
QSplitter::handle{background:#21262d;width:2px}
QSplitter::handle:hover{background:#58a6ff}
QCheckBox{color:#c9d1d9;spacing:6px}
QCheckBox::indicator{width:16px;height:16px;border-radius:3px;border:1px solid #30363d;background:#21262d}
QCheckBox::indicator:checked{background:#238636;border-color:#238636}
QStatusBar{background:#161b22;border-top:1px solid #21262d;color:#8b949e;font-size:12px}
QMenuBar{background:#161b22;border-bottom:1px solid #21262d;color:#c9d1d9;padding:2px}
QMenuBar::item:selected{background:#1f6feb}
QMenu{background:#161b22;border:1px solid #30363d;color:#c9d1d9}
QMenu::item:selected{background:#1f6feb}
QListWidget{background-color:#0d1117;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;outline:none}
QListWidget::item{padding:8px 10px;border-bottom:1px solid #21262d}
QListWidget::item:selected{background-color:#1f6feb;color:#fff}
"""


# ═════════════════════════════════════════════════════════════════
# 4. Session: SQLite-backed conversation persistence
# ═════════════════════════════════════════════════════════════════
class SessionDB:
    """SQLite session store — saves/loads conversation history."""

    MAX_LOAD_MESSAGES = 500  # Only load recent N messages to prevent UI freeze

    def __init__(self, db_path: str = ""):
        if not db_path:
            # Try project-local first, fallback to home dir
            local_dir = Path(__file__).resolve().parent.parent / ".gangge_data"
            try:
                local_dir.mkdir(parents=True, exist_ok=True)
                db_path = str(local_dir / "sessions.db")
                # Test write access
                test_path = local_dir / ".write_test"
                test_path.touch()
                test_path.unlink()
            except (OSError, PermissionError):
                # Fallback to home directory
                home_dir = Path.home() / ".gangge"
                home_dir.mkdir(parents=True, exist_ok=True)
                db_path = str(home_dir / "sessions.db")
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def connect(self):
        try:
            self._conn = sqlite3.connect(
                self._db_path,
                check_same_thread=False,  # 允许跨线程访问（Qt 多线程场景）
            )
            self._conn.row_factory = sqlite3.Row

            # ── FIX 1: 性能 PRAGMA ───────────────────────────────
            pragmas = [
                "PRAGMA journal_mode=WAL",          # WAL 模式（已有，保留）
                "PRAGMA synchronous=NORMAL",        # 从 FULL 降为 NORMAL，写入快 3-5x
                "PRAGMA cache_size=-8000",          # 缓存 8MB（默认只有 2MB）
                "PRAGMA temp_store=MEMORY",         # 临时表走内存，不走磁盘
                "PRAGMA mmap_size=268435456",       # 256MB 内存映射 I/O
                "PRAGMA busy_timeout=5000",         # 锁等待超时 5 秒，防止 OperationalError
                "PRAGMA foreign_keys=ON",
            ]
            for pragma in pragmas:
                self._conn.execute(pragma)
            # ────────────────────────────────────────────────────
            self._init_tables()
        except sqlite3.OperationalError as e:
            # Last resort: use a temp file
            import tempfile
            fallback = Path(tempfile.gettempdir()) / "gangge_sessions.db"
            self._db_path = str(fallback)
            self._conn = sqlite3.connect(self._db_path)
            self._conn.execute("PRAGMA journal_mode=DELETE")  # safer mode
            self._init_tables()

    def close(self):
        if self._conn:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            self._conn.close()
            self._conn = None

    def _init_tables(self):
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
        """)
        self._conn.commit()
        # 如果已有旧库，补全新字段
        self._migrate_if_needed()

    def _migrate_if_needed(self):
        """为旧库补上 content_type / tool_use_id / is_error 字段。"""
        try:
            self._conn.execute(
                "ALTER TABLE messages ADD COLUMN content_type TEXT NOT NULL DEFAULT 'text'"
            )
        except Exception:
            pass  # 字段已存在
        try:
            self._conn.execute(
                "ALTER TABLE messages ADD COLUMN tool_use_id TEXT"
            )
        except Exception:
            pass
        try:
            self._conn.execute(
                "ALTER TABLE messages ADD COLUMN is_error INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass

    def create_session(self, title: str = "", workspace: str = "") -> str:
        if not title:
            title = _t("session_new")
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
            "SELECT id, title, created_at, updated_at, workspace, provider, model, task_count FROM sessions WHERE id=?",
            (sid,),
        ).fetchone()
        if not r:
            return None
        return {"id": r[0], "title": r[1], "created_at": r[2], "updated_at": r[3],
                "workspace": r[4] or "", "provider": r[5] or "", "model": r[6] or "",
                "task_count": r[7] or 0}

    def update_session(self, sid: str, **kw):
        sets = ", ".join(f"{k}=?" for k in kw)
        vals = list(kw.values()) + [sid]
        self._conn.execute(f"UPDATE sessions SET {sets} WHERE id=?", vals)
        self._conn.commit()

    # ── CHANGE: 方案C — save_turn 替换 save_message ────────────
    def save_turn(self, sid: str, messages: list[dict]):
        """
        保存一轮对话的所有聚合消息。

        messages 格式：
        [
          {"role": "user", "content": "帮我写SaaS"},
          {"role": "assistant", "content": [...]},  # list=含tool_use，str=纯文字
          {"role": "tool", "tool_use_id": "...", "content": "...", "is_error": False},
        ]
        """
        now = datetime.now().isoformat()
        for msg in messages:
            role = msg["role"]
            content = msg["content"]
            tool_use_id = msg.get("tool_use_id", None)

            # content 如果是 list（assistant 含 tool_use），序列化为 JSON
            if isinstance(content, list):
                content_str = json.dumps(content, ensure_ascii=False)
                content_type = "json"
            else:
                content_str = str(content)
                content_type = "text"

            is_error = 1 if msg.get("is_error", False) else 0

            self._conn.execute(
                "INSERT INTO messages (session_id, role, content, content_type, tool_use_id, is_error, created_at) VALUES (?,?,?,?,?,?,?)",
                (sid, role, content_str, content_type, tool_use_id, is_error, now),
            )
        self._conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, sid))
        self._conn.commit()

    def count_messages(self, sid: str) -> int:
        """快速查询总消息数（不加载内容），用于 UI 提示"""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id=?", (sid,)
        ).fetchone()
        return row[0] if row else 0

    # ── CHANGE: 方案C — load_turns 替换 load_messages ───────────
    def load_turns(self, sid: str, limit: int = 200) -> list[dict]:
        """
        加载会话消息，重建成 LLM API 标准消息格式。

        返回的消息可直接作为 LLM 的 messages 参数。
        tool 角色的消息显示简短摘要，不展开完整输出。
        limit: 最多加载多少条
        """
        # 取最近 limit 条，按时间正序排列
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
            role = r["role"]
            content_str = r["content"]
            content_type = r["content_type"]
            tool_use_id = r["tool_use_id"]
            is_error = r["is_error"]

            if content_type == "json":
                content = json.loads(content_str)
            else:
                content = content_str

            msg = {"role": role, "content": content}
            if tool_use_id:
                msg["tool_use_id"] = tool_use_id
                msg["is_error"] = bool(is_error)

            messages.append(msg)

        return messages

    def save_tool_call(self, sid: str, round_num: int, tool_name: str, tool_input: str,
                       tool_output: str, is_error: bool, diff: str = ""):
        self._conn.execute(
            "INSERT INTO tool_calls (session_id, round_num, tool_name, tool_input, tool_output, is_error, diff, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (sid, round_num, tool_name, tool_input, tool_output, 1 if is_error else 0, diff, datetime.now().isoformat()),
        )
        self._conn.commit()

    def load_tool_calls(self, sid: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT round_num, tool_name, tool_input, tool_output, is_error, diff FROM tool_calls WHERE session_id=? ORDER BY id",
            (sid,),
        ).fetchall()
        return [{"round": r[0], "tool_name": r[1], "input": r[2], "output": r[3],
                 "is_error": bool(r[4]), "diff": r[5]} for r in rows]

    def delete_session(self, sid: str):
        self._conn.execute("DELETE FROM tool_calls WHERE session_id=?", (sid,))
        self._conn.execute("DELETE FROM messages WHERE session_id=?", (sid,))
        self._conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
        self._conn.commit()

    def increment_task_count(self, sid: str):
        self._conn.execute("UPDATE sessions SET task_count=task_count+1, updated_at=? WHERE id=?",
                           (datetime.now().isoformat(), sid))
        self._conn.commit()


# ═════════════════════════════════════════════════════════════════
#  Output Syntax Highlighter
# ═════════════════════════════════════════════════════════════════
class OutputHighlighter(QSyntaxHighlighter):
    def __init__(self, parent=None):
        super().__init__(parent)
        fmt1 = QTextCharFormat()
        fmt1.setForeground(QColor("#d29922"))
        fmt1.setFontWeight(QFont.Weight.Bold)
        self._rules = [(r"[⚡✓✗🔧📁📝🌐ℹ⚠❌✅]", fmt1)]

        fmt_err = QTextCharFormat()
        fmt_err.setForeground(QColor("#f85149"))
        self._rules.append((r"错误|失败|Error|ERROR|Failed|Exception|Traceback", fmt_err))

        fmt_ok = QTextCharFormat()
        fmt_ok.setForeground(QColor("#3fb950"))
        self._rules.append((r"成功|完成|OK|Done|Success|✓|全部通过", fmt_ok))

        fmt_path = QTextCharFormat()
        fmt_path.setForeground(QColor("#79c0ff"))
        self._rules.append((r"`[^`]+`", fmt_path))

    def highlightBlock(self, text: str) -> None:
        import re
        for pattern, fmt in self._rules:
            for m in re.finditer(pattern, text):
                self.setFormat(m.start(), m.end() - m.start(), fmt)


# ═════════════════════════════════════════════════════════════════
#  Plan Confirmation Dialog
# ═════════════════════════════════════════════════════════════════
class PlanConfirmDialog(QDialog):
    """Shows LLM's plan and lets user approve/reject/modify."""

    def __init__(self, plan_text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(_t("plan_title"))
        self.setMinimumSize(600, 400)
        self.approved = False
        self._setup_ui(plan_text)

    def _setup_ui(self, plan_text: str):
        layout = QVBoxLayout(self)

        title = QLabel(_t("plan_heading"))
        title.setProperty("heading", True)
        layout.addWidget(title)

        self._plan_view = QTextBrowser()
        self._plan_view.setPlainText(plan_text)
        self._plan_view.setStyleSheet("background: #161b22; padding: 12px; font-family: Consolas, monospace;")
        layout.addWidget(self._plan_view, 1)

        info = QLabel(_t("plan_info"))
        info.setWordWrap(True)
        info.setStyleSheet("color: #8b949e; padding: 4px 0;")
        layout.addWidget(info)

        btn_row = QHBoxLayout()
        self._edit_btn = QPushButton("✏️")
        self._edit_btn.clicked.connect(self._toggle_edit)
        btn_row.addWidget(self._edit_btn)

        btn_row.addStretch()

        reject_btn = QPushButton("❌")
        reject_btn.setProperty("danger", True)
        reject_btn.clicked.connect(self.reject)
        btn_row.addWidget(reject_btn)

        approve_btn = QPushButton(_t("btn_approve"))
        approve_btn.setProperty("primary", True)
        approve_btn.clicked.connect(self.approve)
        btn_row.addWidget(approve_btn)

        layout.addLayout(btn_row)

    def _toggle_edit(self):
        if self._plan_view.isReadOnly():
            self._plan_view.setReadOnly(False)
            self._edit_btn.setText(_t("btn_save_edit"))
        else:
            self._plan_view.setReadOnly(True)
            self._edit_btn.setText("✏️ 编辑计划")

    def approve(self):
        self.approved = True
        self.accept()

    def reject(self):
        self.approved = False
        self.accept()

    def get_plan_text(self) -> str:
        return self._plan_view.toPlainText()


# ═════════════════════════════════════════════════════════════════
#  Diff Viewer Widget
# ═════════════════════════════════════════════════════════════════
class DiffViewer(QWidget):
    """Displays unified diffs with green/red highlighting and rollback action."""

    rollback_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self._text = QTextBrowser()
        self._text.setReadOnly(True)
        self._text.setStyleSheet(
            "QTextBrowser{font-family:'Consolas','Courier New',monospace;font-size:12px;"
            "background:#0d1117;border:1px solid #21262d;border-radius:4px;color:#c9d1d9;}"
        )
        layout.addWidget(self._text)

        self._action_bar = QHBoxLayout()
        self._action_bar.setContentsMargins(4, 0, 4, 0)

        self._stats_label = QLabel("")
        self._stats_label.setStyleSheet("color:#8b949e;font-size:11px;")
        self._action_bar.addWidget(self._stats_label)
        self._action_bar.addStretch()

        self._rollback_btn = QPushButton("↩️ 回滚此变更")
        self._rollback_btn.setStyleSheet(
            "QPushButton{background:#da3633;border:1px solid #f85149;border-radius:4px;"
            "color:#fff;font-size:11px;padding:3px 10px;font-weight:bold;}"
            "QPushButton:hover{background:#f85149;}"
        )
        self._rollback_btn.clicked.connect(self.rollback_requested.emit)
        self._rollback_btn.setVisible(False)
        self._action_bar.addWidget(self._rollback_btn)

        self._copy_btn = QPushButton("📋 复制")
        self._copy_btn.setStyleSheet(
            "QPushButton{background:#21262d;border:1px solid #30363d;border-radius:4px;"
            "color:#8b949e;font-size:11px;padding:3px 8px;}"
            "QPushButton:hover{background:#30363d;color:#c9d1d9;}"
        )
        self._copy_btn.clicked.connect(self._copy_diff)
        self._copy_btn.setVisible(False)
        self._action_bar.addWidget(self._copy_btn)

        layout.addLayout(self._action_bar)

        self._current_diff = ""

    def show_diff(self, diff_text: str):
        self._current_diff = diff_text
        self._text.clear()
        if not diff_text.strip():
            self._text.setPlainText("(无变更)")
            self._stats_label.setText("")
            self._rollback_btn.setVisible(False)
            self._copy_btn.setVisible(False)
            return

        added = sum(1 for l in diff_text.splitlines() if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in diff_text.splitlines() if l.startswith("-") and not l.startswith("---"))
        self._stats_label.setText(f"+{added} / -{removed} 行变更")
        self._rollback_btn.setVisible(True)
        self._copy_btn.setVisible(True)

        html = []
        for line in diff_text.splitlines():
            escaped = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            if line.startswith("+") and not line.startswith("+++"):
                html.append(f'<span style="background:#1b3a1b;color:#3fb950">{escaped}</span>')
            elif line.startswith("-") and not line.startswith("---"):
                html.append(f'<span style="background:#3a1b1b;color:#f85149">{escaped}</span>')
            elif line.startswith("@@"):
                html.append(f'<span style="color:#58a6ff;font-weight:bold">{escaped}</span>')
            elif line.startswith("---") or line.startswith("+++"):
                html.append(f'<span style="color:#d29922;font-weight:bold">{escaped}</span>')
            else:
                html.append(f'<span style="color:#8b949e">{escaped}</span>')
        self._text.setHtml("<pre style='margin:4px;line-height:1.4;'>" + "<br>".join(html) + "</pre>")

    def set_plain_text(self, text: str):
        self._current_diff = ""
        self._text.setPlainText(text)
        self._stats_label.setText("")
        self._rollback_btn.setVisible(False)
        self._copy_btn.setVisible(False)

    def _copy_diff(self):
        if self._current_diff:
            from PyQt6.QtWidgets import QApplication
            QApplication.clipboard().setText(self._current_diff)


# ═════════════════════════════════════════════════════════════════
#  Novel Direct Worker — 绕过 AgenticLoop，直接执行小说工具
# ═════════════════════════════════════════════════════════════════
class NovelDirectWorker(QThread):
    progress = pyqtSignal(str)
    finished = pyqtSignal(dict)

    def __init__(self, llm, workspace: str, tool_name: str, args: dict):
        super().__init__()
        self._llm = llm
        self._workspace = workspace
        self._tool_name = tool_name
        self._args = args

    def run(self):
        try:
            asyncio.run(self._run_async())
        except Exception as e:
            import traceback
            self.finished.emit({"error": f"{e}\n{traceback.format_exc()}"})

    async def _run_async(self):
        from gangge.layer3_agent.tools.registry import create_tool_registry
        registry = create_tool_registry(
            workspace=self._workspace,
            llm=self._llm,
        )

        tool = None
        for t in registry._tools.values():
            if t.name == self._tool_name:
                tool = t
                break

        if not tool:
            self.finished.emit({"error": f"工具 {self._tool_name} 未找到"})
            return

        self.progress.emit(f"🔧 执行 {self._tool_name}...\n")

        try:
            result = await tool.execute(**self._args)
            book_id = self._args.get("book_id", "")
            output_text = result.output if hasattr(result, 'output') else str(result)
            is_error = result.is_error if hasattr(result, 'is_error') else False

            if is_error:
                self.finished.emit({"error": output_text, "book_id": book_id})
            else:
                self.finished.emit({"output": output_text, "book_id": book_id})
        except Exception as e:
            import traceback
            self.finished.emit({"error": f"{e}\n{traceback.format_exc()}"})


# ═════════════════════════════════════════════════════════════════
#  Async Worker Thread
# ═════════════════════════════════════════════════════════════════
class GanggeWorker(QThread):
    """Runs the AgenticLoop asynchronously in a background thread."""

    text_block = pyqtSignal(str, str)       # (text, role)
    tool_call_sig = pyqtSignal(str, str, bool, str)  # (tool_name, output, is_error, diff)
    finished = pyqtSignal(dict)             # summary
    status = pyqtSignal(str)                # status message
    plan_ready = pyqtSignal(str)            # plan text for confirmation
    # ── CHANGE: 方案C — 每轮聚合消息信号 ──
    turn_complete = pyqtSignal(list)        # list[dict] 聚合后的消息列表
    ask_user_sig = pyqtSignal(str)          # question to ask user

    def __init__(self, llm: BaseLLM, task: str, workspace: str,
                 max_rounds: int = 30, plan_mode: bool = False,
                 project_context: str = "", system_prompt_extra: str = "",
                 auto_allow: bool = True, batch_index: int = 0,
                 batch_total: int = 1,
                 project_map: str = "",
                 file_registry: dict | None = None,
                 ganggerules: str = "",
                 memory_bank_progress: str = "",
                 memory_bank_changelog: str = "",
                 provider: str = "",
                 model_name: str = "",
                 previous_messages: list | None = None,
                 attachments: list | None = None,
                 auto_inject: bool = False,
                 multimodal_llm: Any | None = None):
        super().__init__()
        self.llm = llm
        self.multimodal_llm = multimodal_llm
        self.task = task
        self.workspace = workspace
        self.previous_messages = previous_messages or []
        self.max_rounds = max_rounds
        self.plan_mode = plan_mode
        self.project_context = project_context
        self.system_prompt_extra = system_prompt_extra
        self.auto_allow = auto_allow
        self.batch_index = batch_index
        self.batch_total = batch_total
        self.project_map = project_map
        self.file_registry = file_registry or {}
        self.ganggerules = ganggerules
        self.memory_bank_progress = memory_bank_progress
        self.memory_bank_changelog = memory_bank_changelog
        self._provider = provider
        self._model = model_name or getattr(llm, "model", "")
        self._attachments = attachments or []
        self._auto_inject = auto_inject
        self._cancel = False
        self._approved_plan = ""
        self._ask_user_answer = ""
        self._ask_user_event = threading.Event()

    def cancel(self):
        self._cancel = True

    def set_approved_plan(self, plan: str):
        self._approved_plan = plan

    # ── CHANGE: 方案C — 消息聚合 ──────────────────────────────
    def _aggregate_turn_messages(self, messages: list, start_idx: int = 0) -> list[dict]:
        """
        把 AgenticLoop 产出的标准 Message 列表聚合成 DB 格式。

        按轮次分组：USER → ASSISTANT(含 tool_use+text) → TOOL 结果
        返回符合 LLM API 协议的消息列表。
        """
        from gangge.layer5_llm.base import ContentType
        turn_msgs = []
        new_msgs = messages[start_idx:]  # 只取本轮新增的消息

        i = 0
        while i < len(new_msgs):
            msg = new_msgs[i]
            role = msg.role

            if role.value == "user":
                text = msg.get_text()
                turn_msgs.append({"role": "user", "content": text})

            elif role.value == "assistant":
                content_blocks = msg.content if hasattr(msg, "content") else []
                blocks = []
                for b in content_blocks:
                    if b.type == ContentType.TEXT and b.text.strip():
                        blocks.append({"type": "text", "text": b.text})
                    elif b.type == ContentType.TOOL_USE:
                        blocks.append({
                            "type": "tool_use",
                            "id": getattr(b, "tool_call_id", "") or getattr(b, "id", ""),
                            "name": getattr(b, "tool_name", ""),
                            "input": getattr(b, "tool_input", {}),
                        })
                if blocks:
                    turn_msgs.append({"role": "assistant", "content": blocks})

            elif role.value == "tool":
                for b in msg.content if hasattr(msg, "content") else []:
                    if b.type == ContentType.TOOL_RESULT:
                        turn_msgs.append({
                            "role": "tool",
                            "tool_use_id": getattr(b, "tool_call_id", ""),
                            "content": b.text if hasattr(b, "text") else "",
                            "is_error": False,
                        })

            i += 1

        return turn_msgs
    # ────────────────────────────────────────────────────────────

    def run(self):
        try:
            asyncio.run(self._run_async())
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            logging.getLogger("gangge.worker").critical("Worker 线程异常: %s\n%s", e, tb)
            self.text_block.emit(_t("execution_error", error=f"{e}\n{tb}"), "error")
            self.finished.emit({"error": str(e)})

    async def _run_async(self):
        # ── Build project context in worker thread (not blocking UI) ──
        if self._auto_inject:
            self.text_block.emit("⏳ 正在加载项目上下文...\n", "system")
            project_context = scan_project_context(self.workspace)
            if project_context:
                self.text_block.emit("📁 已自动注入项目上下文\n", "system")
            self.project_context = project_context

            project_map = build_project_map(self.workspace)
            if project_map:
                self.text_block.emit("🗺️ 已生成项目文件索引\n", "system")
            self.project_map = project_map

            file_registry = build_initial_file_registry(self.workspace)
            if file_registry:
                self.text_block.emit(f"📋 已注册 {len(file_registry)} 个现有文件\n", "system")
            self.file_registry = file_registry

            rules_path = Path(self.workspace) / ".ganggerules"
            if rules_path.exists():
                try:
                    self.ganggerules = rules_path.read_text(encoding="utf-8", errors="replace")[:3000]
                    self.text_block.emit("📜 已加载 .ganggerules 项目规则\n", "system")
                except Exception:
                    pass

            memory_bank_progress, memory_bank_changelog = read_memory_bank(self.workspace)
            if memory_bank_progress or memory_bank_changelog:
                self.text_block.emit("📚 已加载 Memory Bank (进度+变更日志)\n", "system")
            self.memory_bank_progress = memory_bank_progress
            self.memory_bank_changelog = memory_bank_changelog

            git_state = detect_git_state(self.workspace)
            if git_state:
                self.project_context += "\n\n## Git 状态\n" + git_state
                self.text_block.emit("🔀 已注入 Git 状态\n", "system")

            self.text_block.emit("✅ 项目上下文加载完成\n", "system")

        # ── TIMING: 开始计时设置阶段 ──
        _t0 = time.monotonic()
        self.text_block.emit("⏱ 正在初始化 (工具注册/代码索引/系统提示)...\n", "system")

        async def ask_callback(req: PermissionRequest) -> PermissionDecision:
            if self.auto_allow and req.risk.level.value in ("safe", "low"):
                return PermissionDecision.ALLOW
            if self._cancel:
                return PermissionDecision.DENY
            return PermissionDecision.ALLOW

        guard = PermissionGuard(ask_callback=ask_callback)

        from gangge.layer3_agent.tools.registry import create_tool_registry

        async def _ask_user_callback(question: str) -> str:
            self._ask_user_answer = ""
            self._ask_user_event.clear()
            self.ask_user_sig.emit(question)
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._ask_user_event.wait)
            return self._ask_user_answer

        self.text_block.emit("⏱ 正在注册工具 (导入模块)...\n", "system")
        registry = create_tool_registry(
            workspace=self.workspace,
            ask_user_callback=_ask_user_callback,
            llm=self.llm,
            multimodal_llm=self.multimodal_llm,
            attachments=self._attachments,
        )
        _t1 = time.monotonic()
        _cost = _t1 - _t0
        logging.getLogger("gangge").info("[Timing] create_tool_registry 耗时: %.1fs", _cost)
        self.text_block.emit(f"⏱ 工具注册完成: {_cost:.1f}s\n", "system")

        extra = self.system_prompt_extra
        system_text = build_system_prompt(
            workspace_dir=self.workspace,
            project_context=self.project_context,
            plan_mode=self.plan_mode,
        )
        if extra:
            system_text += f"\n\n## 额外指令\n\n{extra}"

        # If plan_mode is on, inject a clear instruction to first generate a plan
        if self.plan_mode:
            system_text += _t("plan_mode_prompt")

        config = LoopConfig(
            max_tool_rounds=self.max_rounds,
            workspace_dir=self.workspace,
            system_prompt=system_text,
            plan_mode=self.plan_mode,
            project_map=self.project_map,
            file_registry=self.file_registry,
            memory_bank_progress=self.memory_bank_progress,
            memory_bank_changelog=self.memory_bank_changelog,
            ganggerules=self.ganggerules,
            ask_user_callback=_ask_user_callback,
        )

        if self.workspace:
            self.text_block.emit("⏱ 正在构建代码索引...\n", "system")
            try:
                from gangge.layer4_tools.repo_index import (
                    get_or_build_index, build_dependency_graph,
                    format_symbol_table,
                )
                _t2 = time.monotonic()
                index = get_or_build_index(self.workspace)
                _t3 = time.monotonic()
                _cost = _t3 - _t2
                logging.getLogger("gangge").info("[Timing] get_or_build_index 耗时: %.1fs", _cost)
                self.text_block.emit(f"⏱ 代码索引完成: {_cost:.1f}s (文件数: {len(index.get('files',{}))})\n", "system")
                config.symbol_table = format_symbol_table(index)
                dep_graph = build_dependency_graph(index, self.workspace)
                if dep_graph:
                    dep_lines = ["## 文件依赖关系 (修改文件前请检查影响范围)"]
                    for path, deps in sorted(dep_graph.items()):
                        dep_lines.append(f"- `{path}` ← {', '.join(f'`{d}`' for d in deps)}")
                    config.dep_graph_summary = "\n".join(dep_lines[:80])
            except Exception:
                pass

        # ── 3. Create loop ──
        loop = AgenticLoop(llm=self.llm, tools=registry, permission_guard=guard, config=config)

        # Streaming callback
        async def stream_cb(block: ContentBlock):
            if self._cancel:
                return
            if block.type == ContentType.TEXT:
                self.text_block.emit(block.text, "assistant")
            elif block.type == ContentType.THINKING:
                self.text_block.emit(block.text, "thinking")
            elif block.type == ContentType.TOOL_USE:
                inp = json.dumps(block.tool_input, ensure_ascii=False)[:200]
                self.text_block.emit(_t("execution_tool_call", name=block.tool_name, input=inp), "tool")

        loop.set_stream_callback(stream_cb)

        # ── 4. Build messages ──
        batch_prefix = f"[{self.batch_index + 1}/{self.batch_total}] " if self.batch_total > 1 else ""
        full_task = f"{batch_prefix}{self.task}"
        # Restore previous session context if any
        messages = list(self.previous_messages)

        if self._attachments:
            att_names = ", ".join(a["name"] for a in self._attachments)
            # Don't send image content to main LLM (it may not support images).
            # Mention the attachment name and let the LLM call vision tool.
            enhanced_task = f"{full_task}\n\n[附件: {att_names}] 如需识别图片内容，请使用 vision 工具。"
            messages.append(Message(role=Role.USER, content=enhanced_task))
            self.text_block.emit(f"\n📋 任务: {full_task}\n📎 附件: {att_names}\n", "user")
        else:
            messages.append(Message(role=Role.USER, content=full_task))
            self.text_block.emit(f"\n📋 任务: {full_task}\n", "user")
        # ── TIMING: 总设置耗时 ──
        _t4 = time.monotonic()
        _cost = _t4 - _t0
        self.text_block.emit(f"⏱ 初始化总耗时: {_cost:.1f}s\n", "system")
        logging.getLogger("gangge").info("[Timing] 初始化总耗时: %.1fs", _cost)
        if self.batch_total > 1:
            self.text_block.emit(f"📌 批处理进度: {self.batch_index + 1}/{self.batch_total}\n", "system")
        self.text_block.emit(_t("execution_workspace", path=self.workspace), "system")
        self.text_block.emit("─" * 60 + "\n", "system")

        # If there's a pre-approved plan, inject it after the first LLM response
        plan_injected = False

        # ── 5. Run loop ──
        result = await loop.run(messages)

        # Store raw Message objects for multi-turn context
        self._conversation_messages = messages

        # ── CHANGE: 方案C — 聚合消息并发射到主线程 ──────────
        # messages 现在的格式：USER, ASSISTANT(含tool_use+text), TOOL 交替
        # 按 LLM API 协议格式聚合后通过 turn_complete 信号发射
        turn_msgs = self._aggregate_turn_messages(messages, len(self.previous_messages))
        if turn_msgs:
            self.turn_complete.emit(turn_msgs)
        # ─────────────────────────────────────────────────────

        # ── 6. Summary ──
        self.text_block.emit("\n" + "═" * 60 + "\n", "system")

        mb_update = result.extra.get("memory_bank_update", "")
        cost_display = ""
        try:
            from gangge.pricing import estimate_cost
            cost_display = estimate_cost(self._provider, self._model,
                                          result.total_tokens.get("input", 0),
                                          result.total_tokens.get("output", 0))
        except Exception:
            pass
        summary = {
            "rounds": result.total_rounds,
            "tool_calls": len(result.tool_executions),
            "tokens": result.total_tokens,
            "final_response": result.final_response,
            "memory_bank_update": mb_update,
            "cost": cost_display,
            "shadow_checkpoint_before": result.extra.get("shadow_checkpoint_before", ""),
            "shadow_checkpoint_after": result.extra.get("shadow_checkpoint_after", ""),
        }

        for exc in result.tool_executions:
            diff = exc.metadata.get("diff", "")
            self.tool_call_sig.emit(exc.tool_name, exc.output[:300], exc.is_error, diff)

        inp = result.total_tokens.get("input", 0)
        out = result.total_tokens.get("output", 0)
        cost_part = f" | 费用: {cost_display}" if cost_display else ""
        self.text_block.emit(
            _t("execution_done", rounds=result.total_rounds, tools=len(result.tool_executions), input=inp, output=out, cost=cost_part),
            "system",
        )
        if result.final_response:
            self.text_block.emit(f"\n{result.final_response}\n", "assistant")

        try:
            await self.llm.close()
        except Exception:
            pass

        self.finished.emit(summary)


# ═════════════════════════════════════════════════════════════════
#  File Browser Widget
# ═════════════════════════════════════════════════════════════════
class FileBrowserWidget(QWidget):
    file_selected = pyqtSignal(str)

    FILE_ICONS = {
        ".py": "🐍", ".js": "📜", ".ts": "📘", ".jsx": "⚛️", ".tsx": "⚛️",
        ".html": "🌐", ".css": "🎨", ".scss": "🎨", ".sass": "🎨",
        ".json": "📋", ".yaml": "⚙️", ".yml": "⚙️", ".toml": "⚙️",
        ".md": "📝", ".rst": "📝", ".txt": "📄",
        ".sql": "🗄️", ".db": "🗄️", ".sqlite": "🗄️",
        ".sh": "💻", ".bat": "💻", ".ps1": "💻",
        ".dockerfile": "🐳", ".env": "🔐", ".gitignore": "🔀",
        ".cpp": "🔧", ".c": "🔧", ".h": "🔧", ".hpp": "🔧",
        ".rs": "🦀", ".go": "🐹", ".java": "☕", ".kt": "📱",
        ".rb": "💎", ".php": "🐘", ".swift": "🦅",
        ".png": "🖼️", ".jpg": "🖼️", ".jpeg": "🖼️", ".gif": "🖼️", ".svg": "🖼️",
        ".mp4": "🎬", ".mp3": "🎵", ".wav": "🎵",
        ".zip": "📦", ".tar": "📦", ".gz": "📦", ".rar": "📦",
        ".pdf": "📑", ".doc": "📑", ".docx": "📑",
        ".xml": "📰", ".csv": "📊", ".xlsx": "📊",
    }
    FOLDER_ICON = "📁"
    FOLDER_OPEN_ICON = "📂"
    DEFAULT_FILE_ICON = "📄"

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setAnimated(True)
        self._tree.setIndentation(16)
        self._tree.itemExpanded.connect(self._on_expand)
        self._tree.itemCollapsed.connect(self._on_collapse)
        self._tree.itemClicked.connect(self._on_click)
        self._tree.setStyleSheet(
            "QTreeWidget{background:#0d1117;border:1px solid #21262d;border-radius:6px;"
            "color:#c9d1d9;outline:none;}"
            "QTreeWidget::item{padding:3px 4px;border-radius:3px;}"
            "QTreeWidget::item:selected{background:#1f6feb;color:#fff;}"
            "QTreeWidget::item:hover{background:#161b22;}"
        )
        layout.addWidget(self._tree)
        self._root_path = ""

    def _get_icon(self, entry: Path) -> str:
        if entry.is_dir():
            return self.FOLDER_ICON
        ext = entry.suffix.lower()
        return self.FILE_ICONS.get(ext, self.DEFAULT_FILE_ICON)

    def set_root(self, path: str):
        self._root_path = path
        self._tree.clear()
        if not path or not os.path.isdir(path):
            return
        root_item = QTreeWidgetItem([f"{self.FOLDER_OPEN_ICON} {os.path.basename(path) or path}"])
        root_item.setData(0, Qt.ItemDataRole.UserRole, path)
        f = root_item.font(0)
        f.setBold(True)
        root_item.setFont(0, f)
        self._tree.addTopLevelItem(root_item)
        root_item.setExpanded(True)
        self._populate(root_item, path, 0)

    def _populate(self, parent_item, dir_path, depth):
        if depth > 4:
            return
        try:
            entries = sorted(Path(dir_path).iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            return
        hidden = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build", ".idea"}
        for entry in entries:
            if entry.name.startswith(".") and entry.name != ".env":
                continue
            if entry.name in hidden:
                continue
            icon = self._get_icon(entry)
            child = QTreeWidgetItem([f"{icon} {entry.name}"])
            child.setData(0, Qt.ItemDataRole.UserRole, str(entry))
            if entry.is_dir():
                child.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
                child.addChild(QTreeWidgetItem(["(loading)"]))
            parent_item.addChild(child)

    def _on_expand(self, item):
        text = item.text(0)
        if self.FOLDER_ICON in text and self.FOLDER_OPEN_ICON not in text:
            item.setText(0, text.replace(self.FOLDER_ICON, self.FOLDER_OPEN_ICON, 1))
        path = item.data(0, Qt.ItemDataRole.UserRole) or ""
        if not path or not os.path.isdir(path):
            return
        if item.childCount() == 1 and item.child(0).text(0) == "(loading)":
            item.removeChild(item.child(0))
        if item.childCount() == 0:
            self._populate(item, path, self._depth(item))

    def _on_collapse(self, item):
        text = item.text(0)
        if self.FOLDER_OPEN_ICON in text:
            item.setText(0, text.replace(self.FOLDER_OPEN_ICON, self.FOLDER_ICON, 1))

    def _depth(self, item):
        d = 0
        while item.parent():
            d += 1
            item = item.parent()
        return d

    def _on_click(self, item, col):
        path = item.data(0, Qt.ItemDataRole.UserRole) or ""
        if path and os.path.isfile(path):
            self.file_selected.emit(path)


# ═════════════════════════════════════════════════════════════════
#  Novel Writing Panel — 小说创作专属面板（参考 Novel Helper 设计）
# ═════════════════════════════════════════════════════════════════

_NOVEL_STYLE = """
QGroupBox{font-size:12px;font-weight:600;color:#58a6ff;border:1px solid #30363d;
    border-radius:6px;margin-top:12px;padding:12px 8px 8px;}
QGroupBox::title{subcontrol-origin:margin;left:10px;padding:0 4px;}
QLabel{color:#c9d1d9;font-size:11px;}
QPushButton{background:#21262d;border:1px solid #30363d;border-radius:4px;
    color:#c9d1d9;font-size:11px;padding:4px 10px;}
QPushButton:hover{background:#30363d;}
QPushButton:pressed{background:#1f6feb;}
QPushButton:disabled{background:#161b22;color:#484f58;}
QLineEdit{background:#0d1117;border:1px solid #30363d;border-radius:4px;
    padding:4px 8px;color:#c9d1d9;font-size:11px;}
QLineEdit:focus{border:1px solid #58a6ff;}
QSpinBox{background:#0d1117;border:1px solid #30363d;border-radius:4px;
    padding:2px 6px;color:#c9d1d9;font-size:11px;}
QComboBox{background:#0d1117;border:1px solid #30363d;border-radius:4px;
    padding:2px 6px;color:#c9d1d9;font-size:11px;}
QComboBox QAbstractItemView{background:#161b22;color:#c9d1d9;selection-background-color:#1f6feb;}
QTextEdit{background:#0d1117;border:1px solid #21262d;border-radius:4px;
    color:#c9d1d9;font-size:11px;padding:4px;}
QListWidget{background:#0d1117;border:1px solid #21262d;border-radius:4px;
    color:#c9d1d9;font-size:11px;}
QListWidget::item{padding:4px 6px;border-bottom:1px solid #161b22;}
QListWidget::item:selected{background:#1f6feb;color:#fff;}
QTreeWidget{background:#0d1117;border:1px solid #21262d;border-radius:4px;
    color:#c9d1d9;font-size:11px;}
QTreeWidget::item{padding:2px 4px;}
QTreeWidget::item:selected{background:#1f6feb;color:#fff;}
QTableWidget{background:#0d1117;border:1px solid #21262d;border-radius:4px;
    color:#c9d1d9;font-size:11px;gridline-color:#161b22;}
QTableWidget::item{padding:3px 6px;}
QTableWidget::item:selected{background:#1f6feb;}
QProgressBar{background:#161b22;border:1px solid #30363d;border-radius:4px;
    text-align:center;color:#c9d1d9;font-size:10px;min-height:14px;}
QProgressBar::chunk{background:#238636;border-radius:3px;}
"""


def _novel_card_html(title: str, subtitle: str, icon: str = "") -> str:
    return (
        f'<div style="background:#161b22;border:1px solid #30363d;border-radius:6px;'
        f'padding:8px 10px;margin:2px 0;">'
        f'<span style="font-size:13px;color:#f0f6fc;">{icon} {title}</span><br/>'
        f'<span style="font-size:10px;color:#8b949e;">{subtitle}</span></div>'
    )


class NovelSidebarPanel(QWidget):
    novel_action = pyqtSignal(str, dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(_NOVEL_STYLE)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)

        header = QLabel("我的小说")
        header.setStyleSheet("color:#f0f6fc;font-size:14px;font-weight:bold;padding:6px 0;")
        lay.addWidget(header)

        self._book_list = QListWidget()
        self._book_list.setMaximumHeight(180)
        self._book_list.currentItemChanged.connect(self._on_book_selected)
        lay.addWidget(self._book_list)

        btn_row = QHBoxLayout()
        self._btn_new = QPushButton("+ 新建")
        self._btn_new.setToolTip("创建一本新小说")
        self._btn_new.clicked.connect(lambda: self._emit_action("new"))
        btn_row.addWidget(self._btn_new)
        self._btn_refresh = QPushButton("刷新")
        self._btn_refresh.setFixedSize(40, 24)
        self._btn_refresh.setToolTip("刷新列表")
        self._btn_refresh.clicked.connect(self.refresh_books)
        btn_row.addWidget(self._btn_refresh)
        btn_row.addStretch()
        lay.addLayout(btn_row)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color:#21262d;")
        lay.addWidget(sep)

        workflow_label = QLabel("创作流程")
        workflow_label.setStyleSheet("color:#8b949e;font-size:11px;font-weight:bold;")
        lay.addWidget(workflow_label)

        self._btn_outline = QPushButton("1. 生成大纲")
        self._btn_outline.setStyleSheet(
            "QPushButton{background:#1f3a1f;border:1px solid #238636;color:#3fb950;}"
            "QPushButton:hover{background:#238636;color:#fff;}"
        )
        self._btn_outline.clicked.connect(lambda: self._emit_action("outline"))
        lay.addWidget(self._btn_outline)

        self._btn_ch_outline = QPushButton("2. 展开章纲")
        self._btn_ch_outline.clicked.connect(lambda: self._emit_action("generate_chapter_outlines"))
        lay.addWidget(self._btn_ch_outline)

        self._btn_write = QPushButton("3. 写下一章")
        self._btn_write.setStyleSheet(
            "QPushButton{background:#1f3a1f;border:1px solid #238636;color:#3fb950;}"
            "QPushButton:hover{background:#238636;color:#fff;}"
        )
        self._btn_write.clicked.connect(lambda: self._emit_action("write_next"))
        lay.addWidget(self._btn_write)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet("color:#21262d;")
        lay.addWidget(sep2)

        tools_label = QLabel("工具箱")
        tools_label.setStyleSheet("color:#8b949e;font-size:11px;font-weight:bold;")
        lay.addWidget(tools_label)

        self._btn_audit = QPushButton("审计最新章")
        self._btn_audit.clicked.connect(lambda: self._emit_action("audit_latest"))
        lay.addWidget(self._btn_audit)

        self._btn_export = QPushButton("导出全书")
        self._btn_export.clicked.connect(lambda: self._emit_action("export"))
        lay.addWidget(self._btn_export)

        self._btn_status = QPushButton("查看状态")
        self._btn_status.clicked.connect(lambda: self._emit_action("status"))
        lay.addWidget(self._btn_status)

        self._btn_load = QPushButton("📂 载入小说")
        self._btn_load.setToolTip("从其他文件夹加载已有小说")
        self._btn_load.clicked.connect(lambda: self._emit_action("load"))
        lay.addWidget(self._btn_load)

        lay.addStretch()
        self._workspace = ""

    def set_workspace(self, workspace: str):
        self._workspace = workspace
        self.refresh_books()

    def refresh_books(self):
        self._book_list.clear()
        if not self._workspace:
            return
        books_dir = Path(self._workspace) / "books"
        if not books_dir.exists():
            return
        for book_path in sorted(books_dir.iterdir()):
            if book_path.is_dir():
                config_path = book_path / "state" / "config.json"
                if config_path.exists():
                    try:
                        cfg = json.loads(config_path.read_text(encoding="utf-8"))
                        genre = cfg.get("genre", "")
                        ch = cfg.get("target_chapters", "?")
                        item = QListWidgetItem(f"{cfg.get('title', '?')} [{genre}] {ch}章")
                        item.setData(Qt.ItemDataRole.UserRole, cfg.get("id", ""))
                        self._book_list.addItem(item)
                    except Exception:
                        pass

    def get_selected_book_id(self) -> str:
        items = self._book_list.selectedItems()
        if items:
            return items[0].data(Qt.ItemDataRole.UserRole) or ""
        return ""

    def select_book(self, book_id: str):
        for i in range(self._book_list.count()):
            item = self._book_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == book_id:
                self._book_list.setCurrentItem(item)
                break

    def _on_book_selected(self, current, previous):
        if current:
            book_id = current.data(Qt.ItemDataRole.UserRole) or ""
            self.novel_action.emit("select", {"book_id": book_id})

    def _emit_action(self, action: str):
        book_id = self.get_selected_book_id()
        self.novel_action.emit(action, {"book_id": book_id})


class NovelRightPanel(QWidget):
    novel_action = pyqtSignal(str, dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(_NOVEL_STYLE)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(0)

        self._header = QLabel("选择一本书开始创作")
        self._header.setStyleSheet(
            "color:#f0f6fc;font-size:14px;font-weight:bold;padding:6px 4px;"
            "background:#161b22;border-bottom:1px solid #30363d;"
        )
        lay.addWidget(self._header)

        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(True)
        self._tabs.setStyleSheet(
            "QTabWidget::pane{border:none;background:#0d1117;}"
            "QTabBar::tab{background:#161b22;color:#8b949e;border:none;"
            "border-bottom:2px solid transparent;padding:6px 8px;font-size:10px;}"
            "QTabBar::tab:selected{color:#f0f6fc;border-bottom:2px solid #f78166;}"
        )

        self._tabs.addTab(self._create_dashboard_tab(), "仪表盘")
        self._tabs.addTab(self._create_characters_tab(), "角色")
        self._tabs.addTab(self._create_arcs_tab(), "篇章")
        self._tabs.addTab(self._create_outline_tab(), "大纲")
        self._tabs.addTab(self._create_chapters_tab(), "章节")
        self._tabs.addTab(self._create_world_tab(), "世界观")
        self._tabs.addTab(self._create_tracking_tab(), "追踪")
        self._tabs.addTab(self._create_graph_tab(), "图谱")
        self._tabs.addTab(self._create_wordbank_tab(), "词库")

        lay.addWidget(self._tabs)
        self._workspace = ""
        self._book_id = ""

    def _create_dashboard_tab(self) -> QWidget:
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(8)

        stats_group = QGroupBox("写作统计")
        stats_lay = QGridLayout(stats_group)
        stats_lay.setSpacing(6)

        self._stat_title = QLabel("--")
        self._stat_title.setStyleSheet("color:#f0f6fc;font-size:16px;font-weight:bold;")
        stats_lay.addWidget(self._stat_title, 0, 0, 1, 3)

        self._stat_genre = QLabel("题材: --")
        stats_lay.addWidget(self._stat_genre, 1, 0)
        self._stat_chapters = QLabel("进度: --")
        stats_lay.addWidget(self._stat_chapters, 1, 1)
        self._stat_words = QLabel("总字数: --")
        stats_lay.addWidget(self._stat_words, 1, 2)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setFormat("%v%")
        stats_lay.addWidget(self._progress_bar, 2, 0, 1, 3)

        self._stat_hooks = QLabel("伏笔: --")
        stats_lay.addWidget(self._stat_hooks, 3, 0)
        self._stat_causal = QLabel("因果链: --")
        stats_lay.addWidget(self._stat_causal, 3, 1)
        self._stat_threads = QLabel("线程: --")
        stats_lay.addWidget(self._stat_threads, 3, 2)

        lay.addWidget(stats_group)

        action_group = QGroupBox("快捷操作")
        action_lay = QVBoxLayout(action_group)
        action_lay.setSpacing(4)

        row1 = QHBoxLayout()
        btn = QPushButton("生成大纲")
        btn.setStyleSheet("QPushButton{background:#238636;color:#fff;font-weight:bold;}"
                          "QPushButton:hover{background:#2ea043;}")
        btn.clicked.connect(lambda: self._emit_action("generate_outline"))
        row1.addWidget(btn)
        btn = QPushButton("展开章纲")
        btn.clicked.connect(lambda: self._emit_action("generate_chapter_outlines"))
        row1.addWidget(btn)
        btn_arc = QPushButton("📖 新篇章")
        btn_arc.setToolTip("为当前事件追加一个新的篇章（独立3幕结构）")
        btn_arc.clicked.connect(lambda: self._emit_action("new_arc"))
        row1.addWidget(btn_arc)
        action_lay.addLayout(row1)

        row2 = QHBoxLayout()
        btn = QPushButton("写下一章")
        btn.setStyleSheet("QPushButton{background:#238636;color:#fff;font-weight:bold;}"
                          "QPushButton:hover{background:#2ea043;}")
        btn.clicked.connect(lambda: self._emit_action("write_next_chapter"))
        row2.addWidget(btn)
        btn = QPushButton("审计")
        btn.clicked.connect(lambda: self._emit_action("audit_selected"))
        row2.addWidget(btn)
        action_lay.addLayout(row2)

        row3 = QHBoxLayout()
        btn = QPushButton("导出全书")
        btn.clicked.connect(lambda: self._emit_action("export"))
        row3.addWidget(btn)
        btn = QPushButton("查看完整状态")
        btn.clicked.connect(lambda: self._emit_action("status"))
        row3.addWidget(btn)
        action_lay.addLayout(row3)

        row4 = QHBoxLayout()
        self._fast_mode_cb = QCheckBox("快速写作模式")
        self._fast_mode_cb.setChecked(True)
        self._fast_mode_cb.setToolTip("跳过建筑师规划和审计修订闭环，速度提升约50%\n关闭后使用完整五层管线")
        self._fast_mode_cb.setStyleSheet("QCheckBox{color:#58a6ff;font-size:11px;}")
        row4.addWidget(self._fast_mode_cb)
        action_lay.addLayout(row4)

        row5 = QHBoxLayout()
        btn = QPushButton("导入参考小说")
        btn.setStyleSheet("QPushButton{background:#8957e5;color:#fff;font-weight:bold;}"
                          "QPushButton:hover{background:#a371f7;}")
        btn.setToolTip("导入一本 TXT 小说，自动分析文风，用于仿写")
        btn.clicked.connect(lambda: self._emit_action("import_reference"))
        row5.addWidget(btn)
        btn = QPushButton("仿写下一章")
        btn.setStyleSheet("QPushButton{background:#8957e5;color:#fff;font-weight:bold;}"
                          "QPushButton:hover{background:#a371f7;}")
        btn.setToolTip("基于参考小说的文风仿写新章节")
        btn.clicked.connect(lambda: self._emit_action("imitate_write_next"))
        row5.addWidget(btn)
        action_lay.addLayout(row5)

        lay.addWidget(action_group)

        self._dashboard_log = QTextBrowser()
        self._dashboard_log.setMaximumHeight(120)
        self._dashboard_log.setStyleSheet(
            "QTextBrowser{background:#0d1117;border:1px solid #21262d;border-radius:4px;"
            "color:#8b949e;font-size:10px;padding:4px;}"
        )
        lay.addWidget(self._dashboard_log)
        lay.addStretch()
        return tab

    def _create_characters_tab(self) -> QWidget:
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(6)

        self._char_table = QTableWidget(0, 7)
        self._char_table.setHorizontalHeaderLabels(["ID", "名称", "弧线", "外部目标", "内在渴望", "势力", "主角团"])
        self._char_table.horizontalHeader().setStretchLastSection(True)
        self._char_table.setColumnWidth(0, 60)
        self._char_table.setColumnWidth(1, 70)
        self._char_table.setColumnWidth(2, 50)
        self._char_table.setColumnWidth(3, 100)
        self._char_table.setColumnWidth(4, 100)
        self._char_table.setColumnWidth(5, 70)
        self._char_table.setColumnWidth(6, 55)
        self._char_table.setAlternatingRowColors(True)
        self._char_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._char_table.verticalHeader().setVisible(False)
        self._char_table.setEditTriggers(QTableWidget.EditTrigger.DoubleClicked | QTableWidget.EditTrigger.EditKeyPressed)
        self._char_table.cellChanged.connect(self._on_char_cell_edited)
        lay.addWidget(self._char_table)

        self._char_detail = QTextBrowser()
        self._char_detail.setMaximumHeight(160)
        self._char_detail.setStyleSheet(
            "QTextBrowser{background:#0d1117;border:1px solid #21262d;border-radius:4px;"
            "color:#c9d1d9;font-size:11px;padding:6px;}"
        )
        self._char_table.currentCellChanged.connect(self._on_char_selected)
        lay.addWidget(self._char_detail)

        btn_row = QHBoxLayout()
        btn = QPushButton("刷新")
        btn.clicked.connect(self._refresh_characters)
        btn_row.addWidget(btn)
        btn = QPushButton("添加角色")
        btn.clicked.connect(lambda: self._emit_action("edit_characters"))
        btn_row.addWidget(btn)
        btn_edit = QPushButton("编辑选中")
        btn_edit.setStyleSheet(
            "QPushButton{background:#1f6feb;border:none;border-radius:4px;"
            "color:#fff;padding:4px 12px;font-size:11px;}"
            "QPushButton:hover{background:#388bfd;}"
        )
        btn_edit.clicked.connect(self._edit_selected_character)
        btn_row.addWidget(btn_edit)
        btn_del = QPushButton("删除选中")
        btn_del.setStyleSheet(
            "QPushButton{background:#da3633;border:none;border-radius:4px;"
            "color:#fff;padding:4px 12px;font-size:11px;}"
            "QPushButton:hover{background:#f85149;}"
        )
        btn_del.clicked.connect(self._delete_selected_character)
        btn_row.addWidget(btn_del)
        btn_row.addStretch()
        lay.addLayout(btn_row)

        self._char_data_cache = []
        self._char_editing = False
        return tab

    def _create_outline_tab(self) -> QWidget:
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(6)

        filter_row = QHBoxLayout()
        filter_label = QLabel("篇章筛选：")
        filter_label.setStyleSheet("color:#8b949e;font-size:11px;")
        filter_row.addWidget(filter_label)
        self._outline_arc_filter = QComboBox()
        self._outline_arc_filter.addItem("全部篇章", "")
        self._outline_arc_filter.setStyleSheet(
            "QComboBox{background:#0d1117;border:1px solid #30363d;border-radius:4px;"
            "color:#c9d1d9;padding:4px 8px;min-width:120px;}"
            "QComboBox::drop-down{border:none;}"
            "QComboBox QAbstractItemView{background:#161b22;color:#c9d1d9;selection-background-color:#1f6feb;}"
        )
        self._outline_arc_filter.currentIndexChanged.connect(self._refresh_outline)
        filter_row.addWidget(self._outline_arc_filter)
        filter_row.addStretch()
        lay.addLayout(filter_row)

        self._outline_info = QLabel("尚未生成大纲")
        self._outline_info.setStyleSheet("color:#8b949e;font-size:11px;padding:4px;")
        lay.addWidget(self._outline_info)

        self._outline_tree = QTreeWidget()
        self._outline_tree.setHeaderLabels(["序列/章节", "幕", "摘要", "功能"])
        self._outline_tree.setColumnWidth(0, 140)
        self._outline_tree.setColumnWidth(1, 40)
        self._outline_tree.setColumnWidth(2, 200)
        self._outline_tree.setColumnWidth(3, 80)
        self._outline_tree.setAlternatingRowColors(True)
        self._outline_tree.itemDoubleClicked.connect(self._on_outline_double_clicked)
        lay.addWidget(self._outline_tree)

        btn_row = QHBoxLayout()
        btn = QPushButton("生成大纲")
        btn.setStyleSheet("QPushButton{background:#238636;color:#fff;font-weight:bold;}"
                          "QPushButton:hover{background:#2ea043;}")
        btn.clicked.connect(lambda: self._emit_action("generate_outline"))
        btn_row.addWidget(btn)
        btn = QPushButton("展开章纲")
        btn.clicked.connect(lambda: self._emit_action("generate_chapter_outlines"))
        btn_row.addWidget(btn)
        btn_edit = QPushButton("编辑选中")
        btn_edit.setStyleSheet(
            "QPushButton{background:#1f6feb;border:none;border-radius:4px;"
            "color:#fff;padding:4px 12px;font-size:11px;}"
            "QPushButton:hover{background:#388bfd;}"
        )
        btn_edit.clicked.connect(self._edit_selected_outline)
        btn_row.addWidget(btn_edit)
        btn = QPushButton("刷新")
        btn.clicked.connect(self._refresh_outline)
        btn_row.addWidget(btn)
        btn_row.addStretch()
        lay.addLayout(btn_row)
        return tab

    def _create_arcs_tab(self) -> QWidget:
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(6)

        self._arcs_info = QLabel("")
        self._arcs_info.setStyleSheet("color:#8b949e;font-size:11px;padding:4px;")
        lay.addWidget(self._arcs_info)

        self._arcs_table = QTableWidget(0, 5)
        self._arcs_table.setHorizontalHeaderLabels(["序号", "篇章名称", "目标", "序列数", "状态"])
        self._arcs_table.horizontalHeader().setStretchLastSection(True)
        self._arcs_table.setColumnWidth(0, 40)
        self._arcs_table.setColumnWidth(1, 120)
        self._arcs_table.setColumnWidth(2, 160)
        self._arcs_table.setColumnWidth(3, 50)
        self._arcs_table.setAlternatingRowColors(True)
        self._arcs_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._arcs_table.verticalHeader().setVisible(False)
        self._arcs_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._arcs_table.currentCellChanged.connect(self._on_arc_selected)
        lay.addWidget(self._arcs_table)

        self._arc_detail = QTextBrowser()
        self._arc_detail.setMaximumHeight(160)
        self._arc_detail.setStyleSheet(
            "QTextBrowser{background:#0d1117;border:1px solid #21262d;border-radius:4px;"
            "color:#c9d1d9;font-size:11px;padding:6px;}"
        )
        lay.addWidget(self._arc_detail)

        btn_row = QHBoxLayout()
        btn_add = QPushButton("添加篇章")
        btn_add.setStyleSheet("QPushButton{background:#238636;color:#fff;font-weight:bold;}"
                              "QPushButton:hover{background:#2ea043;}")
        btn_add.clicked.connect(self._add_arc_dialog)
        btn_row.addWidget(btn_add)
        btn_edit = QPushButton("编辑篇章")
        btn_edit.setStyleSheet(
            "QPushButton{background:#1f6feb;border:none;border-radius:4px;"
            "color:#fff;padding:4px 12px;font-size:11px;}"
            "QPushButton:hover{background:#388bfd;}"
        )
        btn_edit.clicked.connect(self._edit_selected_arc)
        btn_row.addWidget(btn_edit)
        btn_del = QPushButton("删除篇章")
        btn_del.setStyleSheet(
            "QPushButton{background:#da3633;border:none;border-radius:4px;"
            "color:#fff;padding:4px 12px;font-size:11px;}"
            "QPushButton:hover{background:#f85149;}"
        )
        btn_del.clicked.connect(self._delete_selected_arc)
        btn_row.addWidget(btn_del)

        btn_row.addStretch()

        btn_gen_outline = QPushButton("生成大纲")
        btn_gen_outline.setStyleSheet(
            "QPushButton{background:#da8b1a;border:none;border-radius:4px;"
            "color:#fff;padding:4px 12px;font-weight:bold;font-size:11px;}"
            "QPushButton:hover{background:#e6a030;}"
        )
        btn_gen_outline.setToolTip("为选中的篇章生成3幕结构大纲")
        btn_gen_outline.clicked.connect(self._generate_arc_outline)
        btn_row.addWidget(btn_gen_outline)
        btn_expand = QPushButton("展开章纲")
        btn_expand.setStyleSheet(
            "QPushButton{background:#8957e5;border:none;border-radius:4px;"
            "color:#fff;padding:4px 12px;font-size:11px;}"
            "QPushButton:hover{background:#a371f7;}"
        )
        btn_expand.setToolTip("为选中的篇章展开章节大纲（消耗较多Token）")
        btn_expand.clicked.connect(self._expand_arc_chapter_outlines)
        btn_row.addWidget(btn_expand)
        btn = QPushButton("刷新")
        btn.clicked.connect(self._refresh_arcs)
        btn_row.addWidget(btn)
        lay.addLayout(btn_row)

        self._arc_data_cache = []
        return tab

    def _create_chapters_tab(self) -> QWidget:
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(6)

        self._chapter_table = QTableWidget(0, 4)
        self._chapter_table.setHorizontalHeaderLabels(["章节", "标题", "字数", "状态"])
        self._chapter_table.horizontalHeader().setStretchLastSection(True)
        self._chapter_table.setColumnWidth(0, 50)
        self._chapter_table.setColumnWidth(1, 160)
        self._chapter_table.setColumnWidth(2, 60)
        self._chapter_table.setAlternatingRowColors(True)
        self._chapter_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._chapter_table.verticalHeader().setVisible(False)
        self._chapter_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        lay.addWidget(self._chapter_table)

        self._chapter_preview = QTextBrowser()
        self._chapter_preview.setMaximumHeight(180)
        self._chapter_preview.setStyleSheet(
            "QTextBrowser{background:#0d1117;border:1px solid #21262d;border-radius:4px;"
            "color:#c9d1d9;font-size:11px;padding:6px;}"
        )
        self._chapter_table.currentCellChanged.connect(self._on_chapter_selected)
        lay.addWidget(self._chapter_preview)

        btn_row = QHBoxLayout()
        self._btn_write_next = QPushButton("写下一章")
        self._btn_write_next.setStyleSheet("QPushButton{background:#238636;color:#fff;font-weight:bold;}"
                                           "QPushButton:hover{background:#2ea043;}")
        self._btn_write_next.clicked.connect(lambda: self._emit_action("write_next_chapter"))
        btn_row.addWidget(self._btn_write_next)
        btn_edit = QPushButton("编辑章节")
        btn_edit.setStyleSheet(
            "QPushButton{background:#1f6feb;border:none;border-radius:4px;"
            "color:#fff;padding:4px 12px;font-size:11px;}"
            "QPushButton:hover{background:#388bfd;}"
        )
        btn_edit.clicked.connect(self._edit_selected_chapter)
        btn_row.addWidget(btn_edit)
        btn_revise = QPushButton("AI修订")
        btn_revise.setStyleSheet(
            "QPushButton{background:#8957e5;border:none;border-radius:4px;"
            "color:#fff;padding:4px 12px;font-size:11px;}"
            "QPushButton:hover{background:#a371f7;}"
        )
        btn_revise.clicked.connect(self._revise_selected_chapter)
        btn_row.addWidget(btn_revise)
        btn = QPushButton("审计")
        btn.clicked.connect(lambda: self._emit_action("audit_selected"))
        btn_row.addWidget(btn)
        btn = QPushButton("刷新")
        btn.clicked.connect(self._refresh_chapters)
        btn_row.addWidget(btn)
        btn_row.addStretch()
        lay.addLayout(btn_row)
        return tab

    def _create_world_tab(self) -> QWidget:
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(6)

        self._world_tree = QTreeWidget()
        self._world_tree.setHeaderLabels(["名称", "类型"])
        self._world_tree.setColumnWidth(0, 160)
        self._world_tree.setColumnWidth(1, 60)
        self._world_tree.setAlternatingRowColors(True)
        self._world_tree.itemDoubleClicked.connect(self._on_world_double_clicked)
        lay.addWidget(self._world_tree)

        self._world_detail = QTextBrowser()
        self._world_detail.setMaximumHeight(140)
        self._world_detail.setStyleSheet(
            "QTextBrowser{background:#0d1117;border:1px solid #21262d;border-radius:4px;"
            "color:#c9d1d9;font-size:11px;padding:6px;}"
        )
        self._world_tree.currentItemChanged.connect(self._on_world_item_selected)
        lay.addWidget(self._world_detail)

        btn_row = QHBoxLayout()
        btn = QPushButton("刷新")
        btn.clicked.connect(self._refresh_world)
        btn_row.addWidget(btn)
        btn_edit = QPushButton("编辑选中")
        btn_edit.setStyleSheet(
            "QPushButton{background:#1f6feb;border:none;border-radius:4px;"
            "color:#fff;padding:4px 12px;font-size:11px;}"
            "QPushButton:hover{background:#388bfd;}"
        )
        btn_edit.clicked.connect(self._edit_selected_world_item)
        btn_row.addWidget(btn_edit)
        btn_del = QPushButton("删除选中")
        btn_del.setStyleSheet(
            "QPushButton{background:#da3633;border:none;border-radius:4px;"
            "color:#fff;padding:4px 12px;font-size:11px;}"
            "QPushButton:hover{background:#f85149;}"
        )
        btn_del.clicked.connect(self._delete_selected_world_item)
        btn_row.addWidget(btn_del)
        btn_add_loc = QPushButton("+地点")
        btn_add_loc.setToolTip("添加新地点")
        btn_add_loc.clicked.connect(self._add_world_location)
        btn_row.addWidget(btn_add_loc)
        btn_add_fac = QPushButton("+势力")
        btn_add_fac.setToolTip("添加新势力")
        btn_add_fac.clicked.connect(self._add_world_faction)
        btn_row.addWidget(btn_add_fac)
        btn_row.addStretch()
        lay.addLayout(btn_row)
        return tab

    def _create_tracking_tab(self) -> QWidget:
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(6)

        self._tracking_text = QTextBrowser()
        self._tracking_text.setStyleSheet(
            "QTextBrowser{background:#0d1117;border:1px solid #21262d;border-radius:4px;"
            "color:#c9d1d9;font-size:11px;padding:6px;}"
        )
        lay.addWidget(self._tracking_text)

        btn_row = QHBoxLayout()
        btn = QPushButton("刷新状态")
        btn.clicked.connect(self._refresh_tracking)
        btn_row.addWidget(btn)
        btn_resolve = QPushButton("标记伏笔回收")
        btn_resolve.setStyleSheet(
            "QPushButton{background:#1f6feb;border:none;border-radius:4px;"
            "color:#fff;padding:4px 12px;font-size:11px;}"
            "QPushButton:hover{background:#388bfd;}"
        )
        btn_resolve.clicked.connect(self._resolve_hook_dialog)
        btn_row.addWidget(btn_resolve)
        btn_rel = QPushButton("调整关系")
        btn_rel.setStyleSheet(
            "QPushButton{background:#8957e5;border:none;border-radius:4px;"
            "color:#fff;padding:4px 12px;font-size:11px;}"
            "QPushButton:hover{background:#a371f7;}"
        )
        btn_rel.clicked.connect(self._adjust_relationship_dialog)
        btn_row.addWidget(btn_rel)
        btn_row.addStretch()
        lay.addLayout(btn_row)
        return tab

    def _create_graph_tab(self) -> QWidget:
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(6)

        info = QLabel("叙事知识图谱 — 基于 CodeGraph 架构思想构建，可视化角色关系、因果链和伏笔网络")
        info.setStyleSheet("color:#8b949e;font-size:10px;")
        info.setWordWrap(True)
        lay.addWidget(info)

        summary_group = QGroupBox("图谱概览")
        summary_lay = QGridLayout(summary_group)
        summary_lay.setSpacing(4)

        self._graph_nodes_label = QLabel("节点: --")
        self._graph_nodes_label.setStyleSheet("color:#c9d1d9;font-size:11px;")
        summary_lay.addWidget(self._graph_nodes_label, 0, 0)
        self._graph_edges_label = QLabel("边: --")
        self._graph_edges_label.setStyleSheet("color:#c9d1d9;font-size:11px;")
        summary_lay.addWidget(self._graph_edges_label, 0, 1)
        self._graph_hooks_label = QLabel("未闭合伏笔: --")
        self._graph_hooks_label.setStyleSheet("color:#c9d1d9;font-size:11px;")
        summary_lay.addWidget(self._graph_hooks_label, 1, 0)
        self._graph_threads_label = QLabel("活跃线程: --")
        self._graph_threads_label.setStyleSheet("color:#c9d1d9;font-size:11px;")
        summary_lay.addWidget(self._graph_threads_label, 1, 1)
        lay.addWidget(summary_group)

        query_group = QGroupBox("图谱查询")
        query_lay = QVBoxLayout(query_group)
        query_lay.setSpacing(4)

        query_row = QHBoxLayout()
        self._graph_query_combo = QComboBox()
        self._graph_query_combo.addItems([
            "角色档案", "因果链追踪", "关系网络", "未闭合伏笔",
            "叙事线程", "全文搜索", "图谱概览",
        ])
        self._graph_query_combo.setStyleSheet(
            "QComboBox{background:#0d1117;border:1px solid #30363d;border-radius:4px;"
            "color:#c9d1d9;padding:4px;font-size:11px;}"
            "QComboBox::drop-down{border:none;}"
            "QComboBox QAbstractItemView{background:#161b22;color:#c9d1d9;selection-background-color:#1f6feb;}"
        )
        query_row.addWidget(self._graph_query_combo)
        self._graph_query_input = QLineEdit()
        self._graph_query_input.setPlaceholderText("角色ID / 事件ID / 搜索关键词...")
        self._graph_query_input.setStyleSheet(
            "QLineEdit{background:#0d1117;border:1px solid #30363d;border-radius:4px;"
            "color:#c9d1d9;padding:4px;font-size:11px;}"
        )
        query_row.addWidget(self._graph_query_input)
        btn_query = QPushButton("查询")
        btn_query.setStyleSheet(
            "QPushButton{background:#1f6feb;border:none;border-radius:4px;"
            "color:#fff;padding:4px 12px;font-size:11px;}"
            "QPushButton:hover{background:#388bfd;}"
        )
        btn_query.clicked.connect(self._graph_query)
        query_row.addWidget(btn_query)
        query_lay.addLayout(query_row)
        lay.addWidget(query_group)

        result_group = QGroupBox("查询结果")
        result_lay = QVBoxLayout(result_group)
        self._graph_result = QTextEdit()
        self._graph_result.setReadOnly(True)
        self._graph_result.setMaximumHeight(200)
        self._graph_result.setStyleSheet(
            "QTextEdit{background:#0d1117;border:1px solid #30363d;border-radius:4px;"
            "color:#c9d1d9;font-size:11px;padding:4px;}"
        )
        result_lay.addWidget(self._graph_result)

        node_edit_row = QHBoxLayout()
        btn_edit_node = QPushButton("编辑选中节点")
        btn_edit_node.setStyleSheet(
            "QPushButton{background:#1f6feb;border:none;border-radius:4px;"
            "color:#fff;padding:4px 12px;font-size:11px;}"
            "QPushButton:hover{background:#388bfd;}"
        )
        btn_edit_node.clicked.connect(self._edit_graph_node)
        node_edit_row.addWidget(btn_edit_node)
        btn_del_node = QPushButton("删除选中节点")
        btn_del_node.setStyleSheet(
            "QPushButton{background:#da3633;border:none;border-radius:4px;"
            "color:#fff;padding:4px 12px;font-size:11px;}"
            "QPushButton:hover{background:#f85149;}"
        )
        btn_del_node.clicked.connect(self._delete_graph_node)
        node_edit_row.addWidget(btn_del_node)
        node_edit_row.addStretch()
        result_lay.addLayout(node_edit_row)
        lay.addWidget(result_group)

        consistency_group = QGroupBox("一致性检查")
        consistency_lay = QVBoxLayout(consistency_group)
        consistency_lay.setSpacing(4)

        btn_row = QHBoxLayout()
        btn_check = QPushButton("运行一致性检查")
        btn_check.setStyleSheet(
            "QPushButton{background:#da3633;border:none;border-radius:4px;"
            "color:#fff;padding:6px 16px;font-size:11px;}"
            "QPushButton:hover{background:#f85149;}"
        )
        btn_check.clicked.connect(self._graph_consistency_check)
        btn_row.addWidget(btn_check)

        btn_rebuild = QPushButton("重建图谱")
        btn_rebuild.setStyleSheet(
            "QPushButton{background:#6e40c9;border:none;border-radius:4px;"
            "color:#fff;padding:6px 16px;font-size:11px;}"
            "QPushButton:hover{background:#8957e5;}"
        )
        btn_rebuild.clicked.connect(self._graph_rebuild)
        btn_row.addWidget(btn_rebuild)
        consistency_lay.addLayout(btn_row)

        self._consistency_result = QTextEdit()
        self._consistency_result.setReadOnly(True)
        self._consistency_result.setMaximumHeight(150)
        self._consistency_result.setStyleSheet(
            "QTextEdit{background:#0d1117;border:1px solid #30363d;border-radius:4px;"
            "color:#c9d1d9;font-size:11px;padding:4px;}"
        )
        consistency_lay.addWidget(self._consistency_result)
        lay.addWidget(consistency_group)

        lay.addStretch()
        return tab

    def _graph_query(self):
        if not self._workspace or not self._book_id:
            self._graph_result.setText("请先选择一本书")
            return
        query_map = {
            "角色档案": "character_profile",
            "因果链追踪": "causal_chain",
            "关系网络": "relationship_network",
            "未闭合伏笔": "open_hooks",
            "叙事线程": "thread_overview",
            "全文搜索": "search",
            "图谱概览": "graph_summary",
        }
        query_type = query_map.get(self._graph_query_combo.currentText(), "graph_summary")
        query_input = self._graph_query_input.text().strip()

        try:
            from gangge.dramatica.narrative_graph import (
                NarrativeGraphDB, NarrativeQueries, NarrativeTraversal,
            )
        except ImportError:
            self._graph_result.setText("叙事图谱模块未加载")
            return

        db_path = Path(self._workspace) / "books" / self._book_id / "state" / "narrative_graph.db"
        if not db_path.exists():
            self._graph_result.setText("图谱数据库不存在，请先写章节以自动建立图谱")
            return

        db = NarrativeGraphDB(db_path)
        queries = NarrativeQueries(db)

        try:
            if query_type == "character_profile":
                if query_input:
                    profile = queries.get_character_profile(query_input)
                    if "error" in profile:
                        self._graph_result.setText(profile["error"])
                        return
                    lines = [
                        f"<b>{profile['name']}</b><br>",
                        f"<i>{profile['description'][:100]}</i><br><br>",
                        f"<b>关系 ({len(profile['relationships'])})</b>:<br>",
                    ]
                    for r in profile["relationships"]:
                        color = "#3fb950" if r["strength"] > 0 else "#f85149" if r["strength"] < 0 else "#8b949e"
                        lines.append(f"  <span style='color:{color}'>{r['with']}: {r['type']} ({r['strength']:+d})</span><br>")
                    lines.append(f"<br><b>位置历史 ({len(profile['location_history'])})</b>:<br>")
                    for loc in profile["location_history"]:
                        lines.append(f"  Ch.{loc['chapter']}: {loc['location']}<br>")
                    self._graph_result.setHtml("".join(lines))
                else:
                    chars = db.get_nodes_by_kind("character")
                    lines = ["<b>角色列表</b>:<br>"]
                    for c in chars:
                        lines.append(f"  {c['id']}: {c['name']}<br>")
                    self._graph_result.setHtml("".join(lines))

            elif query_type == "causal_chain":
                if query_input:
                    chain = queries.get_causal_chain(query_input, "downstream")
                    lines = [f"<b>因果链追踪</b> (从 {query_input}):<br>"]
                    for entry in chain:
                        lines.append(f"  → {entry['name']} (Ch.{entry['chapter']})<br>")
                        for conn in entry.get("connections", []):
                            lines.append(f"    <span style='color:#8b949e'>[{conn['kind']}] → {conn['to_name']}</span><br>")
                    self._graph_result.setHtml("".join(lines))
                else:
                    events = db.get_nodes_by_kind("event")
                    lines = ["<b>事件列表</b> (最近10个):<br>"]
                    for e in events[-10:]:
                        lines.append(f"  {e['id']}: {e['name']} (Ch.{e['chapter']})<br>")
                    self._graph_result.setHtml("".join(lines))

            elif query_type == "relationship_network":
                if not query_input:
                    self._graph_result.setText("请输入角色ID")
                    return
                network = queries.get_relationship_network(query_input, 2)
                lines = [f"<b>关系网络</b> (中心: {query_input}):<br>"]
                for n in network["nodes"]:
                    depth_color = "#3fb950" if n["depth"] == 0 else "#58a6ff" if n["depth"] == 1 else "#8b949e"
                    lines.append(f"  <span style='color:{depth_color}'>{'●' * (n['depth'] + 1)} {n['name']} (深度{n['depth']})</span><br>")
                lines.append(f"<br><b>关系边 ({len(network['edges'])})</b>:<br>")
                for e in network["edges"]:
                    color = "#3fb950" if e["strength"] > 0 else "#f85149" if e["strength"] < 0 else "#8b949e"
                    lines.append(f"  <span style='color:{color}'>{e['source_name']} → {e['target_name']}: {e['type']} ({e['strength']:+d})</span><br>")
                self._graph_result.setHtml("".join(lines))

            elif query_type == "open_hooks":
                hooks = queries.get_open_hooks()
                if not hooks:
                    self._graph_result.setHtml("<span style='color:#3fb950'>✅ 没有未闭合的伏笔</span>")
                    return
                lines = [f"<b>未闭合伏笔 ({len(hooks)})</b>:<br>"]
                for h in hooks:
                    lines.append(f"  <span style='color:#d29922'>📌 {h['description']}</span><br>")
                    lines.append(f"    <span style='color:#8b949e'>类型:{h['type']} 埋设:Ch.{h['planted_in_chapter']} 预期回收:Ch.{h['expected_range'][0]}-{h['expected_range'][1]}</span><br>")
                self._graph_result.setHtml("".join(lines))

            elif query_type == "thread_overview":
                threads = queries.get_thread_overview()
                if not threads:
                    self._graph_result.setHtml("没有叙事线程")
                    return
                lines = [f"<b>叙事线程 ({len(threads)})</b>:<br>"]
                for t in threads:
                    status_color = "#3fb950" if t["status"] == "active" else "#8b949e"
                    lines.append(f"  <span style='color:{status_color}'>● {t['name']}</span> (权重:{t['weight']})<br>")
                    participants = "、".join(p["name"] for p in t["participants"])
                    if participants:
                        lines.append(f"    <span style='color:#8b949e'>参与者: {participants}</span><br>")
                self._graph_result.setHtml("".join(lines))

            elif query_type == "search":
                if not query_input:
                    self._graph_result.setText("请输入搜索关键词")
                    return
                results = queries.search_narrative(query_input)
                if not results:
                    self._graph_result.setHtml(f"未找到匹配「{query_input}」的内容")
                    return
                lines = [f"<b>搜索「{query_input}」</b> ({len(results)} 结果):<br>"]
                for r in results:
                    kind_colors = {"character": "#3fb950", "event": "#58a6ff", "location": "#d29922", "hook": "#f85149", "thread": "#bc8cff"}
                    color = kind_colors.get(r["kind"], "#8b949e")
                    lines.append(f"  <span style='color:{color}'>[{r['kind']}]</span> {r['name']}: {r['description'][:60]}<br>")
                self._graph_result.setHtml("".join(lines))

            elif query_type == "graph_summary":
                summary = queries.get_graph_summary()
                lines = [
                    "<b>图谱概览</b><br>",
                    f"  节点: {summary['nodes']} ",
                    f"(角色 {summary['node_kinds'].get('character', 0)}, ",
                    f"事件 {summary['node_kinds'].get('event', 0)}, ",
                    f"地点 {summary['node_kinds'].get('location', 0)}, ",
                    f"伏笔 {summary['node_kinds'].get('hook', 0)})<br>",
                    f"  边: {summary['edges']} ",
                    f"(因果 {summary['edge_kinds'].get('causes', 0)}, ",
                    f"关系 {summary['edge_kinds'].get('relationship', 0)}, ",
                    f"参与 {summary['edge_kinds'].get('participates', 0)})<br>",
                    f"  已索引章节: {summary['chapters_indexed']}<br>",
                    f"  未闭合伏笔: <span style='color:#d29922'>{summary['open_hooks']}</span><br>",
                    f"  活跃线程: {summary['active_threads']}, 休眠: {summary['dormant_threads']}<br>",
                ]
                self._graph_result.setHtml("".join(lines))
                self._graph_nodes_label.setText(f"节点: {summary['nodes']}")
                self._graph_edges_label.setText(f"边: {summary['edges']}")
                self._graph_hooks_label.setText(f"未闭合伏笔: {summary['open_hooks']}")
                self._graph_threads_label.setText(f"活跃线程: {summary['active_threads']}")
        except Exception as e:
            self._graph_result.setText(f"查询失败: {e}")
        finally:
            db.close()

    def _graph_consistency_check(self):
        if not self._workspace or not self._book_id:
            self._consistency_result.setText("请先选择一本书")
            return
        try:
            from gangge.dramatica.narrative_graph import (
                NarrativeGraphDB, ConsistencyChecker,
            )
        except ImportError:
            self._consistency_result.setText("叙事图谱模块未加载")
            return

        db_path = Path(self._workspace) / "books" / self._book_id / "state" / "narrative_graph.db"
        if not db_path.exists():
            self._consistency_result.setText("图谱数据库不存在，请先写章节以自动建立图谱")
            return

        db = NarrativeGraphDB(db_path)
        checker = ConsistencyChecker(db)

        try:
            ws_path = Path(self._workspace) / "books" / self._book_id / "state" / "world_state.json"
            current_chapter = 0
            if ws_path.exists():
                ws_data = json.loads(ws_path.read_text(encoding="utf-8"))
                current_chapter = ws_data.get("current_chapter", 0)

            issues = checker.check_all(current_chapter)
            if not issues:
                self._consistency_result.setHtml("<span style='color:#3fb950'>✅ 一致性检查通过，未发现叙事矛盾</span>")
                return

            critical = [i for i in issues if i.severity.value == "critical"]
            warnings = [i for i in issues if i.severity.value == "warning"]
            infos = [i for i in issues if i.severity.value == "info"]

            lines = [f"发现 {len(critical)} 严重 / {len(warnings)} 警告 / {len(infos)} 提示<br>"]
            if critical:
                lines.append("<br><span style='color:#f85149'><b>🔴 严重问题</b></span>:<br>")
                for i in critical:
                    lines.append(f"  <span style='color:#f85149'>[{i.category.value}] {i.description}</span><br>")
                    if i.suggestion:
                        lines.append(f"  <span style='color:#8b949e'>建议: {i.suggestion}</span><br>")
            if warnings:
                lines.append("<br><span style='color:#d29922'><b>🟡 警告</b></span>:<br>")
                for i in warnings:
                    lines.append(f"  <span style='color:#d29922'>[{i.category.value}] {i.description}</span><br>")
            if infos:
                lines.append("<br><span style='color:#58a6ff'><b>🔵 提示</b></span>:<br>")
                for i in infos:
                    lines.append(f"  <span style='color:#58a6ff'>[{i.category.value}] {i.description}</span><br>")

            self._consistency_result.setHtml("".join(lines))
        except Exception as e:
            self._consistency_result.setText(f"检查失败: {e}")
        finally:
            db.close()

    def _graph_rebuild(self):
        if not self._workspace or not self._book_id:
            return
        try:
            from gangge.dramatica.narrative_graph import (
                NarrativeGraphDB, NarrativeIndexer,
            )
        except ImportError:
            self._graph_result.setText("叙事图谱模块未加载")
            return

        book_dir = Path(self._workspace) / "books" / self._book_id
        db_path = book_dir / "state" / "narrative_graph.db"
        if db_path.exists():
            db_path.unlink()

        db = NarrativeGraphDB(db_path)
        db.initialize()
        indexer = NarrativeIndexer(db)

        try:
            stats = indexer.rebuild_from_book(book_dir)
            self._graph_result.setHtml(
                f"<b>图谱重建完成</b><br>"
                f"  节点: {stats['nodes']}<br>"
                f"  边: {stats['edges']}<br>"
                f"  已索引章节: {stats['chapters']}"
            )
            self._graph_nodes_label.setText(f"节点: {stats['nodes']}")
            self._graph_edges_label.setText(f"边: {stats['edges']}")
        except Exception as e:
            self._graph_result.setText(f"重建失败: {e}")
        finally:
            db.close()

    def _edit_graph_node(self):
        if not self._workspace or not self._book_id:
            return

        query_input = self._graph_query_input.text().strip()
        if not query_input:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.information(self, "提示", "请在查询输入框中输入要编辑的节点ID")
            return

        try:
            from gangge.dramatica.narrative_graph import NarrativeGraphDB
        except ImportError:
            return

        db_path = Path(self._workspace) / "books" / self._book_id / "state" / "narrative_graph.db"
        if not db_path.exists():
            return

        db = NarrativeGraphDB(db_path)
        try:
            node = db.get_node(query_input)
            if not node:
                self._graph_result.setText(f"节点 {query_input} 不存在")
                return

            from PyQt6.QtWidgets import QDialog, QFormLayout, QLineEdit, QTextEdit, QDialogButtonBox
            dlg = QDialog(self)
            dlg.setWindowTitle(f"编辑图谱节点 - {node['name']}")
            dlg.setMinimumWidth(400)
            dlg.setStyleSheet("QDialog{background:#161b22;color:#c9d1d9;} QLabel{color:#c9d1d9;} "
                              "QLineEdit,QTextEdit{background:#0d1117;border:1px solid #30363d;border-radius:4px;"
                              "color:#c9d1d9;padding:4px;}")
            form = QFormLayout(dlg)

            le_name = QLineEdit(node.get("name", ""))
            form.addRow("名称", le_name)
            te_desc = QTextEdit()
            te_desc.setPlainText(node.get("description", ""))
            te_desc.setMaximumHeight(80)
            form.addRow("描述", te_desc)

            btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            btns.accepted.connect(dlg.accept)
            btns.rejected.connect(dlg.reject)
            form.addRow(btns)

            if dlg.exec() == QDialog.DialogCode.Accepted:
                db.upsert_node(
                    node_id=query_input,
                    kind=node.get("kind", ""),
                    name=le_name.text(),
                    description=te_desc.toPlainText(),
                    chapter=node.get("chapter", 0),
                    metadata=node.get("metadata"),
                )
                self._graph_result.setHtml(f"<span style='color:#3fb950'>✅ 节点 {query_input} 已更新</span>")
        except Exception as e:
            self._graph_result.setText(f"编辑失败: {e}")
        finally:
            db.close()

    def _delete_graph_node(self):
        if not self._workspace or not self._book_id:
            return

        query_input = self._graph_query_input.text().strip()
        if not query_input:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.information(self, "提示", "请在查询输入框中输入要删除的节点ID")
            return

        from PyQt6.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self, "确认删除", f"确定要删除节点「{query_input}」及其所有关联边吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            from gangge.dramatica.narrative_graph import NarrativeGraphDB
        except ImportError:
            return

        db_path = Path(self._workspace) / "books" / self._book_id / "state" / "narrative_graph.db"
        if not db_path.exists():
            return

        db = NarrativeGraphDB(db_path)
        try:
            db.delete_node(query_input)
            self._graph_result.setHtml(f"<span style='color:#f85149'>🗑 节点 {query_input} 已删除</span>")
            self._refresh_graph()
        except Exception as e:
            self._graph_result.setText(f"删除失败: {e}")
        finally:
            db.close()

    def _create_wordbank_tab(self) -> QWidget:
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(6)

        info = QLabel("自定义敏感词和替换词库，写作时自动检测")
        info.setStyleSheet("color:#8b949e;font-size:10px;")
        lay.addWidget(info)

        wb_group = QGroupBox("敏感词列表")
        wb_lay = QVBoxLayout(wb_group)
        self._word_list = QListWidget()
        self._word_list.setAlternatingRowColors(True)
        wb_lay.addWidget(self._word_list)
        wb_btn_row = QHBoxLayout()
        self._word_input = QLineEdit()
        self._word_input.setPlaceholderText("输入敏感词...")
        wb_btn_row.addWidget(self._word_input)
        btn_add = QPushButton("添加")
        btn_add.clicked.connect(self._add_word)
        wb_btn_row.addWidget(btn_add)
        btn_del = QPushButton("删除")
        btn_del.clicked.connect(self._del_word)
        wb_btn_row.addWidget(btn_del)
        wb_lay.addLayout(wb_btn_row)
        lay.addWidget(wb_group)

        style_group = QGroupBox("风格指南")
        style_lay = QVBoxLayout(style_group)
        self._style_edit = QPlainTextEdit()
        self._style_edit.setMaximumHeight(100)
        self._style_edit.setPlaceholderText("输入风格指南（如：不用网络用语、避免西式句式...）")
        self._style_edit.setStyleSheet(
            "QPlainTextEdit{background:#0d1117;border:1px solid #30363d;border-radius:4px;"
            "color:#c9d1d9;font-size:11px;padding:4px;}"
        )
        style_lay.addWidget(self._style_edit)
        btn_save_style = QPushButton("保存风格指南")
        btn_save_style.clicked.connect(self._save_style_guide)
        style_lay.addWidget(btn_save_style)
        lay.addWidget(style_group)

        lay.addStretch()
        return tab

    def set_workspace(self, workspace: str, book_id: str = ""):
        self._workspace = workspace
        self._book_id = book_id
        if book_id:
            self._refresh_all()

    def _refresh_all(self):
        self._refresh_dashboard()
        self._refresh_characters()
        self._refresh_outline()
        self._refresh_arcs()
        self._refresh_chapters()
        self._refresh_world()
        self._refresh_tracking()
        self._refresh_graph()
        self._refresh_wordbank()

    def _refresh_dashboard(self):
        if not self._workspace or not self._book_id:
            return
        sm_dir = Path(self._workspace) / "books" / self._book_id / "state"
        config_path = sm_dir / "config.json"
        ws_path = sm_dir / "world_state.json"

        if config_path.exists():
            try:
                cfg = json.loads(config_path.read_text(encoding="utf-8"))
                self._header.setText(f"{cfg.get('title', '?')} — {cfg.get('genre', '')}")
                self._stat_title.setText(cfg.get("title", "--"))
                self._stat_genre.setText(f"题材: {cfg.get('genre', '--')}")
                target = cfg.get("target_chapters", 0)
                target_words = cfg.get("target_words_per_chapter", 0)

                current_ch = 0
                total_words = 0
                if ws_path.exists():
                    ws_data = json.loads(ws_path.read_text(encoding="utf-8"))
                    current_ch = ws_data.get("current_chapter", 0)
                    hooks = ws_data.get("hooks", [])
                    open_h = sum(1 for h in hooks if h.get("status") == "open")
                    closed_h = sum(1 for h in hooks if h.get("status") == "closed")
                    causal = ws_data.get("causal_chain", [])
                    threads = ws_data.get("threads", [])
                    self._stat_hooks.setText(f"伏笔: {open_h}开/{closed_h}闭")
                    self._stat_causal.setText(f"因果链: {len(causal)}")
                    self._stat_threads.setText(f"线程: {len(threads)}")

                ch_dir = Path(self._workspace) / "books" / self._book_id / "chapters"
                if ch_dir.exists():
                    for f in ch_dir.glob("ch*_final.md"):
                        total_words += len(f.read_text(encoding="utf-8"))

                self._stat_chapters.setText(f"进度: {current_ch}/{target}章")
                self._stat_words.setText(f"总字数: {total_words:,}")
                if target > 0:
                    self._progress_bar.setValue(int(current_ch / target * 100))
            except Exception:
                pass

        self._dashboard_log.setHtml(
            "<p style='color:#484f58;'>操作日志将在此显示。点击左侧快捷操作开始创作。</p>"
        )

        # ── Show guidance if book is skeleton-only (no outline yet) ──
        outline_path = sm_dir / "outline.json"
        if not outline_path.exists():
            self._dashboard_log.setHtml(
                "<div style='color:#8b949e;font-size:11px;line-height:1.6;padding:12px;'>"
                "<p style='color:#58a6ff;font-size:13px;font-weight:bold;'>📖 小说骨架已就绪</p>"
                "<p>目前只有基本配置，还没有角色、世界观和大纲内容。</p>"
                "<p>💬 <b>推荐方式</b>：在中间的聊天窗口描述你的故事想法，AI 会自动帮你配置。</p>"
                "<p style='color:#484f58;'>例如：「我想写一个穿越到异世界的冒险故事，主角是个普通学生…」</p>"
                "<p>🎨 或者点击左侧「生成大纲」按钮直接使用默认模板生成。</p>"
                "</div>"
            )

    def _refresh_characters(self):
        self._char_table.setRowCount(0)
        self._char_data_cache = []
        if not self._workspace or not self._book_id:
            return
        setup_path = Path(self._workspace) / "books" / self._book_id / "state" / "setup_state.json"
        if not setup_path.exists():
            self._char_detail.setHtml(
                "<div style='color:#8b949e;font-size:11px;padding:8px;'>"
                "<p>暂无角色数据</p>"
                "<p style='color:#484f58;'>在聊天窗口描述你的故事想法，AI 会自动生成角色。</p>"
                "</div>"
            )
            return
        try:
            data = json.loads(setup_path.read_text(encoding="utf-8"))
            chars = data.get("characters", {})
            for cid, c in chars.items():
                row = self._char_table.rowCount()
                self._char_table.insertRow(row)
                self._char_table.setItem(row, 0, QTableWidgetItem(c.get("id", cid)))
                self._char_table.setItem(row, 1, QTableWidgetItem(c.get("name", "")))
                self._char_table.setItem(row, 2, QTableWidgetItem(c.get("arc", "")))
                need = c.get("need", {})
                self._char_table.setItem(row, 3, QTableWidgetItem(need.get("external", "") if isinstance(need, dict) else ""))
                self._char_table.setItem(row, 4, QTableWidgetItem(need.get("internal", "") if isinstance(need, dict) else ""))
                self._char_table.setItem(row, 5, QTableWidgetItem(c.get("faction", "")))
                is_main = "✅" if c.get("is_main_cast", False) else ""
                self._char_table.setItem(row, 6, QTableWidgetItem(is_main))
                self._char_data_cache.append(c)
        except Exception:
            pass

    def _on_char_selected(self, row, col, prev_row, prev_col):
        if 0 <= row < len(self._char_data_cache):
            c = self._char_data_cache[row]
            html = f"<h4>{c.get('name', '')}（{c.get('id', '')}）</h4>"
            html += f"<p><b>弧线:</b> {c.get('arc', '')}</p>"
            need = c.get("need", {})
            if isinstance(need, dict):
                html += f"<p><b>外部目标:</b> {need.get('external', '')}</p>"
                html += f"<p><b>内在渴望:</b> {need.get('internal', '')}</p>"
            faction = c.get("faction", "")
            if faction:
                html += f"<p><b>势力:</b> {faction}</p>"
            if c.get("is_main_cast", False):
                html += "<p><b>主角团:</b> ✅ 是</p>"
            html += f"<p><b>简介:</b> {c.get('profile', '')}</p>"
            wv = c.get("worldview", {})
            if isinstance(wv, dict):
                html += f"<p><b>世界观:</b> 权力={wv.get('power', '')} 信任={wv.get('trust', '')} 应对={wv.get('coping', '')}</p>"
            bl = c.get("behavior_lock", [])
            if bl:
                html += f"<p><b>行为锁定:</b> {'、'.join(bl)}</p>"
            obs = c.get("obstacles", [])
            if obs:
                html += "<p><b>障碍:</b></p>"
                for o in obs:
                    html += f"<p style='margin-left:12px;'>{o.get('type', '')}: {o.get('description', '')}</p>"
            self._char_detail.setHtml(html)

    def _on_char_cell_edited(self, row, col):
        if self._char_editing or not self._workspace or not self._book_id:
            return
        if row >= len(self._char_data_cache):
            return

        self._char_editing = True
        try:
            c = self._char_data_cache[row]
            char_id = c.get("id", "")
            if not char_id:
                return

            field_map = {1: "name", 2: "arc", 3: "need_external", 4: "need_internal", 5: "faction", 6: "is_main_cast"}
            field = field_map.get(col)
            if not field:
                return

            new_value = self._char_table.item(row, col).text() if self._char_table.item(row, col) else ""
            if field == "need_external":
                old_value = c.get("need", {}).get("external", "") if isinstance(c.get("need"), dict) else ""
            elif field == "need_internal":
                old_value = c.get("need", {}).get("internal", "") if isinstance(c.get("need"), dict) else ""
            elif field == "is_main_cast":
                old_value = c.get("is_main_cast", False)
                new_value = new_value.strip() in ("✅", "true", "True", "是", "1", "yes", "Yes")
            else:
                old_value = c.get(field, "")

            if new_value == old_value:
                return

            edit_data = {"character_id": char_id, field: new_value}
            cmd = f'novel_edit(book_id="{self._book_id}", edit_type="update_character", data={json.dumps(edit_data, ensure_ascii=False)})'
            self._send_command(cmd)
        finally:
            self._char_editing = False

    def _edit_selected_character(self):
        row = self._char_table.currentRow()
        if row < 0 or row >= len(self._char_data_cache):
            return
        c = self._char_data_cache[row]
        char_id = c.get("id", "")
        if not char_id:
            return

        from PyQt6.QtWidgets import QDialog, QFormLayout, QLineEdit, QDialogButtonBox
        dlg = QDialog(self)
        dlg.setWindowTitle(f"编辑角色 - {c.get('name', char_id)}")
        dlg.setStyleSheet("QDialog{background:#161b22;color:#c9d1d9;} QLabel{color:#c9d1d9;} "
                          "QLineEdit{background:#0d1117;border:1px solid #30363d;border-radius:4px;"
                          "color:#c9d1d9;padding:4px;}")
        form = QFormLayout(dlg)

        fields = {}
        for key, label in [("name", "名称"), ("arc", "弧线"), ("profile", "简介"), ("backstory", "背景"), ("faction", "所属势力")]:
            le = QLineEdit(c.get(key, ""))
            form.addRow(label, le)
            fields[key] = le

        need = c.get("need", {})
        if isinstance(need, dict):
            le_ext = QLineEdit(need.get("external", ""))
            form.addRow("外部目标", le_ext)
            fields["need_external"] = le_ext
            le_int = QLineEdit(need.get("internal", ""))
            form.addRow("内在渴望", le_int)
            fields["need_internal"] = le_int

        from PyQt6.QtWidgets import QCheckBox
        cb_main = QCheckBox("是主角团成员")
        cb_main.setChecked(c.get("is_main_cast", False))
        cb_main.setStyleSheet("QCheckBox{color:#c9d1d9;}")
        form.addRow("主角团", cb_main)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            edit_data = {"character_id": char_id}
            for key, le in fields.items():
                edit_data[key] = le.text()
            edit_data["is_main_cast"] = cb_main.isChecked()
            cmd = f'novel_edit(book_id="{self._book_id}", edit_type="update_character", data={json.dumps(edit_data, ensure_ascii=False)})'
            self._send_command(cmd)
            self._refresh_characters()

    def _delete_selected_character(self):
        row = self._char_table.currentRow()
        if row < 0 or row >= len(self._char_data_cache):
            return
        c = self._char_data_cache[row]
        char_id = c.get("id", "")
        char_name = c.get("name", char_id)

        from PyQt6.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self, "确认删除", f"确定要删除角色「{char_name}」吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            edit_data = {"character_id": char_id}
            cmd = f'novel_edit(book_id="{self._book_id}", edit_type="delete_character", data={json.dumps(edit_data, ensure_ascii=False)})'
            self._send_command(cmd)
            self._refresh_characters()

    def _refresh_outline(self):
        self._outline_tree.clear()
        self._outline_info.setText("尚未生成大纲")
        if not self._workspace or not self._book_id:
            return

        selected_arc = ""
        if hasattr(self, '_outline_arc_filter'):
            idx = self._outline_arc_filter.currentIndex()
            if idx > 0:
                selected_arc = self._outline_arc_filter.currentData() or ""

        outline_path = Path(self._workspace) / "books" / self._book_id / "state" / "outline.json"
        if not outline_path.exists():
            return
        try:
            data = json.loads(outline_path.read_text(encoding="utf-8"))

            # 自动迁移旧格式：将顶层 sequences 包装为第一个篇章
            if not data.get("arcs") and data.get("sequences"):
                title = data.get("title", "我的小说")
                default_arc = {
                    "name": f"{title}·第一篇",
                    "order": 1,
                    "goal": data.get("total_goal", ""),
                    "summary": data.get("logline", ""),
                    "sequences": data["sequences"],
                    "status": "outlined",
                }
                data["arcs"] = [default_arc]
                outline_path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

            total_goal = data.get("total_goal", "")
            info_parts = [f"<b>{data.get('title', '大纲')}</b> | Logline: {data.get('logline', '')}"]
            if total_goal:
                info_parts.append(f" | 🎯 总目标: {total_goal}")
            if selected_arc:
                info_parts.append(f" | 📖 筛选: {selected_arc}")
            self._outline_info.setText("".join(info_parts))

            if hasattr(self, '_outline_arc_filter'):
                prev_arc = self._outline_arc_filter.currentData() or ""
                self._outline_arc_filter.blockSignals(True)
                self._outline_arc_filter.clear()
                self._outline_arc_filter.addItem("全部篇章", "")
                arcs = data.get("arcs", [])
                for arc in arcs:
                    arc_name = arc.get("name", "")
                    self._outline_arc_filter.addItem(f"📖 {arc_name}", arc_name)
                target_idx = 0
                for i in range(self._outline_arc_filter.count()):
                    if self._outline_arc_filter.itemData(i) == prev_arc:
                        target_idx = i
                        break
                if selected_arc:
                    for i in range(self._outline_arc_filter.count()):
                        if self._outline_arc_filter.itemData(i) == selected_arc:
                            target_idx = i
                            break
                self._outline_arc_filter.setCurrentIndex(target_idx)
                self._outline_arc_filter.blockSignals(False)

            arcs = data.get("arcs", [])
            if arcs:
                for arc in arcs:
                    arc_order = arc.get("order", "?")
                    arc_name = arc.get("name", f"篇{arc_order}")

                    if selected_arc and arc_name != selected_arc:
                        continue

                    arc_item = QTreeWidgetItem([
                        f"📖 {arc_name}",
                        "",
                        arc.get("summary", ""),
                        f"第{arc_order}篇",
                    ])
                    arc_item.setForeground(0, QColor("#d2a8ff"))
                    arc_item.setForeground(3, QColor("#f78166"))
                    arc_item.setData(0, Qt.ItemDataRole.UserRole, {"type": "arc", "data": arc})
                    self._outline_tree.addTopLevelItem(arc_item)

                    for seq in arc.get("sequences", []):
                        act = str(seq.get("act", "?"))
                        seq_item = QTreeWidgetItem([
                            f"序列{seq.get('number', '?')}",
                            f"第{act}幕",
                            seq.get("summary", ""),
                            seq.get("dramatic_function", ""),
                        ])
                        seq_item.setForeground(0, QColor("#58a6ff"))
                        arc_item.addChild(seq_item)

                        end_hook = seq.get("end_hook", "")
                        if end_hook:
                            hook_item = QTreeWidgetItem(seq_item, ["结尾钩子", "", end_hook, ""])
                            hook_item.setForeground(0, QColor("#f78166"))
                        for evt in seq.get("key_events", []):
                            evt_item = QTreeWidgetItem(seq_item, ["事件", "", evt, ""])
                            evt_item.setForeground(0, QColor("#8b949e"))
            else:
                for seq in data.get("sequences", []):
                    act = str(seq.get("act", "?"))
                    seq_item = QTreeWidgetItem([
                        f"序列{seq.get('number', '?')}",
                        f"第{act}幕",
                        seq.get("summary", ""),
                        seq.get("dramatic_function", ""),
                    ])
                    seq_item.setForeground(0, QColor("#58a6ff"))
                    self._outline_tree.addTopLevelItem(seq_item)

                    end_hook = seq.get("end_hook", "")
                    if end_hook:
                        hook_item = QTreeWidgetItem(seq_item, ["结尾钩子", "", end_hook, ""])
                        hook_item.setForeground(0, QColor("#f78166"))
                    for evt in seq.get("key_events", []):
                        evt_item = QTreeWidgetItem(seq_item, ["事件", "", evt, ""])
                        evt_item.setForeground(0, QColor("#8b949e"))
        except Exception:
            pass

        ch_outline_path = Path(self._workspace) / "books" / self._book_id / "state" / "chapter_outlines.json"
        if ch_outline_path.exists():
            try:
                ch_data = json.loads(ch_outline_path.read_text(encoding="utf-8"))
                for co in ch_data:
                    ch_item = QTreeWidgetItem([
                        f"第{co.get('chapter_number', '?')}章",
                        "",
                        co.get("title", ""),
                        co.get("summary", "")[:60],
                    ])
                    ch_item.setForeground(0, QColor("#3fb950"))
                    root = self._outline_tree.invisibleRootItem()
                    for i in range(root.childCount()):
                        seq_item = root.child(i)
                        if co.get("sequence_id", "") in seq_item.text(0):
                            seq_item.addChild(ch_item)
                            break
                    else:
                        self._outline_tree.addTopLevelItem(ch_item)
            except Exception:
                pass

    def _on_outline_double_clicked(self, item, col):
        self._edit_outline_item(item)

    def _edit_selected_outline(self):
        item = self._outline_tree.currentItem()
        if not item:
            return
        self._edit_outline_item(item)

    def _edit_outline_item(self, item):
        if not self._workspace or not self._book_id:
            return

        from PyQt6.QtWidgets import QDialog, QFormLayout, QLineEdit, QTextEdit, QDialogButtonBox
        item_text = item.text(0)

        if item_text.startswith("第") and "章" in item_text:
            ch_num_str = item_text.replace("第", "").replace("章", "")
            try:
                ch_num = int(ch_num_str)
            except ValueError:
                return
            self._edit_chapter_outline_dialog(ch_num)
        elif item_text.startswith("序列"):
            seq_id_str = item_text.replace("序列", "")
            try:
                seq_idx = int(seq_id_str) - 1
            except ValueError:
                return
            self._edit_sequence_dialog(seq_idx, item)

    def _edit_chapter_outline_dialog(self, ch_num):
        from PyQt6.QtWidgets import QDialog, QFormLayout, QLineEdit, QTextEdit, QDialogButtonBox
        co_path = Path(self._workspace) / "books" / self._book_id / "state" / "chapter_outlines.json"
        if not co_path.exists():
            return

        co_data = json.loads(co_path.read_text(encoding="utf-8"))
        target = None
        for item in co_data:
            if item.get("chapter_number") == ch_num:
                target = item
                break
        if not target:
            return

        dlg = QDialog(self)
        dlg.setWindowTitle(f"编辑第 {ch_num} 章章纲")
        dlg.setMinimumWidth(400)
        dlg.setStyleSheet("QDialog{background:#161b22;color:#c9d1d9;} QLabel{color:#c9d1d9;} "
                          "QLineEdit,QTextEdit{background:#0d1117;border:1px solid #30363d;border-radius:4px;"
                          "color:#c9d1d9;padding:4px;}")
        form = QFormLayout(dlg)

        le_title = QLineEdit(target.get("title", ""))
        form.addRow("标题", le_title)
        te_summary = QTextEdit()
        te_summary.setPlainText(target.get("summary", ""))
        te_summary.setMaximumHeight(80)
        form.addRow("摘要", te_summary)
        le_pov = QLineEdit(target.get("pov", ""))
        form.addRow("视角角色", le_pov)
        le_thread = QLineEdit(target.get("thread_id", ""))
        form.addRow("叙事线程", le_thread)
        te_notes = QTextEdit()
        te_notes.setPlainText(target.get("writing_notes", ""))
        te_notes.setMaximumHeight(60)
        form.addRow("写作备注", te_notes)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            edit_data = {
                "chapter_number": ch_num,
                "title": le_title.text(),
                "summary": te_summary.toPlainText(),
                "pov": le_pov.text(),
                "thread_id": le_thread.text(),
                "writing_notes": te_notes.toPlainText(),
            }
            cmd = f'novel_edit(book_id="{self._book_id}", edit_type="update_chapter_outline", data={json.dumps(edit_data, ensure_ascii=False)})'
            self._send_command(cmd)
            self._refresh_outline()

    def _edit_sequence_dialog(self, seq_idx, tree_item):
        from PyQt6.QtWidgets import QDialog, QFormLayout, QLineEdit, QTextEdit, QDialogButtonBox
        outline_path = Path(self._workspace) / "books" / self._book_id / "state" / "outline.json"
        if not outline_path.exists():
            return

        outline = json.loads(outline_path.read_text(encoding="utf-8"))
        sequences = outline.get("sequences", outline.get("acts", []))
        if seq_idx >= len(sequences):
            return
        seq = sequences[seq_idx]

        dlg = QDialog(self)
        dlg.setWindowTitle(f"编辑序列 {seq_idx + 1}")
        dlg.setMinimumWidth(400)
        dlg.setStyleSheet("QDialog{background:#161b22;color:#c9d1d9;} QLabel{color:#c9d1d9;} "
                          "QLineEdit,QTextEdit{background:#0d1117;border:1px solid #30363d;border-radius:4px;"
                          "color:#c9d1d9;padding:4px;}")
        form = QFormLayout(dlg)

        le_title = QLineEdit(seq.get("title", ""))
        form.addRow("标题", le_title)
        te_summary = QTextEdit()
        te_summary.setPlainText(seq.get("summary", ""))
        te_summary.setMaximumHeight(80)
        form.addRow("摘要", te_summary)
        le_func = QLineEdit(seq.get("dramatic_function", ""))
        form.addRow("戏剧功能", le_func)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            edit_data = {
                "sequence_index": seq_idx,
                "title": le_title.text(),
                "summary": te_summary.toPlainText(),
                "dramatic_function": le_func.text(),
            }
            cmd = f'novel_edit(book_id="{self._book_id}", edit_type="update_outline", data={json.dumps(edit_data, ensure_ascii=False)})'
            self._send_command(cmd)
            self._refresh_outline()

    def _refresh_arcs(self):
        self._arcs_table.setRowCount(0)
        self._arc_data_cache = []
        if not self._workspace or not self._book_id:
            return

        outline_path = Path(self._workspace) / "books" / self._book_id / "state" / "outline.json"
        if not outline_path.exists():
            self._arcs_info.setText("尚未生成大纲，请先生成大纲")
            return

        try:
            data = json.loads(outline_path.read_text(encoding="utf-8"))
            arcs = data.get("arcs", [])
            total_goal = data.get("total_goal", "")

            # 自动迁移旧格式：将顶层 sequences 包装为第一个篇章
            if not arcs and data.get("sequences"):
                title = data.get("title", "我的小说")
                default_arc = {
                    "name": f"{title}·第一篇",
                    "order": 1,
                    "goal": total_goal or "",
                    "summary": data.get("logline", ""),
                    "sequences": data["sequences"],
                    "status": "outlined",
                }
                arcs = [default_arc]
                data["arcs"] = arcs
                outline_path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

            info_parts = []
            if total_goal:
                info_parts.append(f"总目标：{total_goal}")
            info_parts.append(f"共 {len(arcs)} 个篇章")
            self._arcs_info.setText(" | ".join(info_parts))

            if not arcs:
                self._arcs_info.setText("尚无篇章，请点击「添加篇章」创建第一个篇章。")
                return

            for arc in arcs:
                row = self._arcs_table.rowCount()
                self._arcs_table.insertRow(row)
                order = arc.get("order", "?")
                self._arcs_table.setItem(row, 0, QTableWidgetItem(str(order)))
                self._arcs_table.setItem(row, 1, QTableWidgetItem(arc.get("name", f"篇{order}")))
                self._arcs_table.setItem(row, 2, QTableWidgetItem(arc.get("goal", "")))
                seq_count = len(arc.get("sequences", []))
                self._arcs_table.setItem(row, 3, QTableWidgetItem(str(seq_count)))
                status = arc.get("status", "pending")
                self._arcs_table.setItem(row, 4, QTableWidgetItem(status))
                self._arc_data_cache.append(arc)
        except Exception:
            self._arcs_info.setText("大纲加载失败")

    def _on_arc_selected(self, row, col, prev_row, prev_col):
        if 0 <= row < len(self._arc_data_cache):
            arc = self._arc_data_cache[row]
            html = f"<h4>{arc.get('name', '')}（第{arc.get('order', '?')}篇）</h4>"
            html += f"<p><b>目标:</b> {arc.get('goal', '未设定')}</p>"
            html += f"<p><b>概要:</b> {arc.get('summary', '无')}</p>"
            html += f"<p><b>状态:</b> {arc.get('status', 'pending')}</p>"
            seqs = arc.get("sequences", [])
            if seqs:
                html += f"<p><b>包含 {len(seqs)} 个序列:</b></p>"
                for s in seqs:
                    html += f"<p style='margin-left:12px;'>• {s.get('name', s.get('id', '?'))}</p>"
            self._arc_detail.setHtml(html)

    def _add_arc_dialog(self):
        if not self._workspace or not self._book_id:
            return
        from PyQt6.QtWidgets import QDialog, QFormLayout, QLineEdit, QTextEdit, QDialogButtonBox
        dlg = QDialog(self)
        dlg.setWindowTitle("添加新篇章")
        dlg.setStyleSheet("QDialog{background:#161b22;color:#c9d1d9;} QLabel{color:#c9d1d9;} "
                          "QLineEdit,QTextEdit{background:#0d1117;border:1px solid #30363d;border-radius:4px;"
                          "color:#c9d1d9;padding:4px;}")
        form = QFormLayout(dlg)
        le_name = QLineEdit()
        le_name.setPlaceholderText("如：东海篇")
        form.addRow("篇章名称", le_name)
        le_goal = QLineEdit()
        le_goal.setPlaceholderText("这个篇章的小目标，服务于总目标")
        form.addRow("篇章目标", le_goal)
        te_summary = QTextEdit()
        te_summary.setMaximumHeight(120)
        te_summary.setPlaceholderText("简述这个篇章要讲什么故事")
        form.addRow("篇章概要", te_summary)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            edit_data = {
                "arc_name": le_name.text().strip(),
                "arc_goal": le_goal.text().strip(),
                "arc_summary": te_summary.toPlainText().strip(),
            }
            if not edit_data["arc_name"]:
                return
            cmd = f'novel_new_arc(book_id="{self._book_id}", arc_name="{edit_data["arc_name"]}", arc_goal="{edit_data["arc_goal"]}", arc_summary="{edit_data["arc_summary"]}")'
            self._send_command(cmd)
            self._refresh_arcs()

    def _edit_selected_arc(self):
        row = self._arcs_table.currentRow()
        if row < 0 or row >= len(self._arc_data_cache):
            return
        arc = self._arc_data_cache[row]
        from PyQt6.QtWidgets import QDialog, QFormLayout, QLineEdit, QTextEdit, QDialogButtonBox
        dlg = QDialog(self)
        dlg.setWindowTitle(f"编辑篇章 - {arc.get('name', '')}")
        dlg.setStyleSheet("QDialog{background:#161b22;color:#c9d1d9;} QLabel{color:#c9d1d9;} "
                          "QLineEdit,QTextEdit{background:#0d1117;border:1px solid #30363d;border-radius:4px;"
                          "color:#c9d1d9;padding:4px;}")
        form = QFormLayout(dlg)
        le_name = QLineEdit(arc.get("name", ""))
        form.addRow("篇章名称", le_name)
        le_goal = QLineEdit(arc.get("goal", ""))
        form.addRow("篇章目标", le_goal)
        te_summary = QTextEdit()
        te_summary.setMaximumHeight(120)
        te_summary.setPlainText(arc.get("summary", ""))
        form.addRow("篇章概要", te_summary)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            edit_data = {
                "arc_name": arc.get("name", ""),
                "new_name": le_name.text().strip(),
                "arc_goal": le_goal.text().strip(),
                "arc_summary": te_summary.toPlainText().strip(),
            }
            cmd = f'novel_edit(book_id="{self._book_id}", edit_type="update_arc", data={json.dumps(edit_data, ensure_ascii=False)})'
            self._send_command(cmd)
            self._refresh_arcs()

    def _delete_selected_arc(self):
        row = self._arcs_table.currentRow()
        if row < 0 or row >= len(self._arc_data_cache):
            return
        arc = self._arc_data_cache[row]
        arc_name = arc.get("name", f"第{arc.get('order', '?')}篇")
        from PyQt6.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self, "确认删除",
            f"确定要删除篇章「{arc_name}」吗？\n该篇章下的所有序列和大纲数据将被移除。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            cmd = f'novel_edit(book_id="{self._book_id}", edit_type="delete_arc", data={json.dumps({"arc_name": arc_name}, ensure_ascii=False)})'
            self._send_command(cmd)
            self._refresh_arcs()

    def _generate_arc_outline(self):
        row = self._arcs_table.currentRow()
        if row < 0 or row >= len(self._arc_data_cache):
            self._append_system_msg("⚠️ 请先选择一个篇章")
            return
        arc = self._arc_data_cache[row]
        arc_name = arc.get("name", f"第{arc.get('order', '?')}篇")
        arc_goal = arc.get("goal", "")
        arc_summary = arc.get("summary", "")
        cmd = f'novel_outline(book_id="{self._book_id}", arc_name="{arc_name}")'
        self._append_system_msg(
            f"📝 正在为篇章「{arc_name}」生成3幕结构大纲...\n"
            f"目标：{arc_goal}\n"
            f"概要：{arc_summary[:80]}{'…' if len(arc_summary) > 80 else ''}"
        )
        self._send_command(cmd)

    def _expand_arc_chapter_outlines(self):
        row = self._arcs_table.currentRow()
        if row < 0 or row >= len(self._arc_data_cache):
            self._append_system_msg("⚠️ 请先选择一个篇章")
            return
        arc = self._arc_data_cache[row]
        arc_name = arc.get("name", f"第{arc.get('order', '?')}篇")
        from PyQt6.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self, "确认展开章纲",
            f"确定要为篇章「{arc_name}」展开章节大纲吗？\n"
            f"这会消耗较多 Token，请确认大纲内容已审核无误。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            cmd = f'novel_chapter_outlines(book_id="{self._book_id}", arc_name="{arc_name}")'
            self._append_system_msg(f"📋 正在为篇章「{arc_name}」展开章节大纲...")
            self._send_command(cmd)

    def _refresh_chapters(self):
        self._chapter_table.setRowCount(0)
        if not self._workspace or not self._book_id:
            return
        chapter_dir = Path(self._workspace) / "books" / self._book_id / "chapters"
        if not chapter_dir.exists():
            return

        # 兼容多种章节文件名格式：
        # ch0001_final.md, ch0001_draft.md, chapter_001.md, 第01章_标题.md
        import re
        chapters_found: dict[int, dict] = {}

        for ch_path in sorted(chapter_dir.glob("*.md")):
            name = ch_path.stem
            ch_num = None
            status = "已完成"

            if name.startswith("ch") and ("_final" in name or "_draft" in name):
                num_str = name.replace("ch", "").replace("_final", "").replace("_draft", "")
                try:
                    ch_num = int(num_str)
                except ValueError:
                    pass
                if "_draft" in name:
                    status = "草稿"
            elif name.startswith("chapter_"):
                num_str = name.replace("chapter_", "")
                try:
                    ch_num = int(num_str)
                except ValueError:
                    pass
            elif name.startswith("第"):
                m = re.match(r"第(\d+)章", name)
                if m:
                    ch_num = int(m.group(1))

            if ch_num is None:
                continue

            if ch_num in chapters_found:
                existing = chapters_found[ch_num]
                if existing["status"] == "已完成" and status == "草稿":
                    continue
                if existing["status"] == "草稿" and status == "已完成":
                    pass
                else:
                    continue

            try:
                content = ch_path.read_text(encoding="utf-8")
            except Exception:
                continue
            word_count = len(content)
            title = ""
            for line in content.split("\n")[:5]:
                if line.strip().startswith("#"):
                    title = line.strip().lstrip("#").strip()
                    break

            chapters_found[ch_num] = {
                "path": ch_path,
                "status": status,
                "title": title or name,
                "word_count": word_count,
            }

        for ch_num in sorted(chapters_found.keys()):
            info = chapters_found[ch_num]
            row = self._chapter_table.rowCount()
            self._chapter_table.insertRow(row)
            self._chapter_table.setItem(row, 0, QTableWidgetItem(str(ch_num)))
            self._chapter_table.setItem(row, 1, QTableWidgetItem(info["title"]))
            self._chapter_table.setItem(row, 2, QTableWidgetItem(f"{info['word_count']:,}"))
            self._chapter_table.setItem(row, 3, QTableWidgetItem(info["status"]))

    def _find_chapter_file(self, ch_num: int) -> Path | None:
        """查找章节文件，兼容多种命名格式。优先级：final > draft > chapter_N > 第N章"""
        if not self._workspace or not self._book_id:
            return None
        ch_dir = Path(self._workspace) / "books" / self._book_id / "chapters"
        if not ch_dir.exists():
            return None
        # 按优先级查找
        candidates = [
            ch_dir / f"ch{ch_num:04d}_final.md",
            ch_dir / f"ch{ch_num:04d}_draft.md",
            ch_dir / f"chapter_{ch_num:03d}.md",
            ch_dir / f"chapter_{ch_num}.md",
        ]
        for p in candidates:
            if p.exists():
                return p
        # 模糊匹配：第N章_*.md
        import re
        for f in ch_dir.glob("*.md"):
            m = re.match(r"第(\d+)章", f.stem)
            if m and int(m.group(1)) == ch_num:
                return f
        return None

    def _on_chapter_selected(self, row, col, prev_row, prev_col):
        item = self._chapter_table.item(row, 0)
        if not item:
            return
        try:
            ch_num = int(item.text())
        except ValueError:
            return
        ch_path = self._find_chapter_file(ch_num)
        if ch_path and ch_path.exists():
            content = ch_path.read_text(encoding="utf-8")
            preview = content[:2000]
            if len(content) > 2000:
                preview += "\n\n...(点击导出查看完整内容)"
            self._chapter_preview.setPlainText(preview)
            return
        self._chapter_preview.setPlainText("章节内容不存在")

    def _edit_selected_chapter(self):
        row = self._chapter_table.currentRow()
        if row < 0:
            return
        item = self._chapter_table.item(row, 0)
        if not item:
            return
        try:
            ch_num = int(item.text())
        except ValueError:
            return

        ch_path = self._find_chapter_file(ch_num)
        if not ch_path:
            return

        content = ch_path.read_text(encoding="utf-8")

        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QTextEdit, QDialogButtonBox
        dlg = QDialog(self)
        dlg.setWindowTitle(f"编辑第 {ch_num} 章")
        dlg.setMinimumSize(600, 500)
        dlg.setStyleSheet("QDialog{background:#161b22;color:#c9d1d9;} "
                          "QTextEdit{background:#0d1117;border:1px solid #30363d;border-radius:4px;"
                          "color:#c9d1d9;font-size:13px;padding:8px;font-family:'Microsoft YaHei',sans-serif;}")
        vlay = QVBoxLayout(dlg)

        editor = QTextEdit()
        editor.setPlainText(content)
        vlay.addWidget(editor)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        vlay.addWidget(btns)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_content = editor.toPlainText()
            edit_data = {"chapter_number": int(ch_num), "content": new_content}
            cmd = f'novel_edit(book_id="{self._book_id}", edit_type="rewrite_chapter", data={json.dumps(edit_data, ensure_ascii=False)})'
            self._send_command(cmd)
            self._refresh_chapters()

    def _revise_selected_chapter(self):
        row = self._chapter_table.currentRow()
        if row < 0:
            return
        item = self._chapter_table.item(row, 0)
        if not item:
            return
        ch_num = item.text()
        if not self._workspace or not self._book_id:
            return

        from PyQt6.QtWidgets import QDialog, QFormLayout, QLineEdit, QDialogButtonBox
        dlg = QDialog(self)
        dlg.setWindowTitle(f"AI 修订第 {ch_num} 章")
        dlg.setMinimumWidth(400)
        dlg.setStyleSheet("QDialog{background:#161b22;color:#c9d1d9;} QLabel{color:#c9d1d9;} "
                          "QLineEdit{background:#0d1117;border:1px solid #30363d;border-radius:4px;"
                          "color:#c9d1d9;padding:4px;}")
        form = QFormLayout(dlg)

        le_focus = QLineEdit("")
        form.addRow("修订重点（如：加强悬念、优化对话）", le_focus)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            focus = le_focus.text()
            cmd = f'novel_revise(book_id="{self._book_id}", chapter_number={int(ch_num)}'
            if focus:
                cmd += f', focus="{focus}"'
            cmd += ")"
            self._send_command(cmd, auto_run=False)

    def _refresh_world(self):
        self._world_tree.clear()
        if not self._workspace or not self._book_id:
            return
        sm_dir = Path(self._workspace) / "books" / self._book_id / "state"
        bible_path = sm_dir / "story_bible.md"
        setup_path = sm_dir / "setup_state.json"
        if not bible_path.exists() and not setup_path.exists():
            item = QTreeWidgetItem(["💡 暂无世界观数据", ""])
            item.setForeground(0, QColor("#8b949e"))
            self._world_tree.addTopLevelItem(item)
            self._world_detail.setHtml(
                "<div style='color:#8b949e;font-size:11px;padding:8px;'>"
                "<p>暂无世界观数据</p>"
                "<p style='color:#484f58;'>在聊天窗口描述你的故事设定，AI 会自动生成世界观。</p>"
                "</div>"
            )
            return
        if bible_path.exists():
            content = bible_path.read_text(encoding="utf-8")
            lines = content.split("\n")
            current_parent = None
            for line in lines:
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.startswith("## "):
                    item = QTreeWidgetItem([stripped[3:], "分类"])
                    item.setForeground(0, QColor("#58a6ff"))
                    self._world_tree.addTopLevelItem(item)
                    current_parent = item
                elif stripped.startswith("### ") and current_parent:
                    item = QTreeWidgetItem(current_parent, [stripped[4:], "子类"])
                    item.setForeground(0, QColor("#c9d1d9"))
                elif stripped.startswith("- ") and current_parent:
                    item = QTreeWidgetItem(current_parent, [stripped[2:], "条目"])
                    item.setForeground(0, QColor("#8b949e"))

        setup_path = sm_dir / "setup_state.json"
        if setup_path.exists():
            try:
                setup = json.loads(setup_path.read_text(encoding="utf-8"))
                world = setup.get("world", {})

                loc_parent = QTreeWidgetItem(["地点", "分类"])
                loc_parent.setForeground(0, QColor("#f78166"))
                self._world_tree.addTopLevelItem(loc_parent)
                for loc in world.get("locations", []):
                    loc_item = QTreeWidgetItem(loc_parent, [loc.get("name", loc.get("id", "?")), "地点"])
                    loc_item.setData(0, Qt.ItemDataRole.UserRole, {"type": "location", "id": loc.get("id", ""), "data": loc})
                    loc_item.setForeground(0, QColor("#c9d1d9"))

                fac_parent = QTreeWidgetItem(["势力", "分类"])
                fac_parent.setForeground(0, QColor("#d2a8ff"))
                self._world_tree.addTopLevelItem(fac_parent)
                for fac in world.get("factions", []):
                    fac_item = QTreeWidgetItem(fac_parent, [fac.get("name", fac.get("id", "?")), "势力"])
                    fac_item.setData(0, Qt.ItemDataRole.UserRole, {"type": "faction", "id": fac.get("id", ""), "data": fac})
                    fac_item.setForeground(0, QColor("#c9d1d9"))
            except Exception:
                pass

    def _on_world_double_clicked(self, item, col):
        self._edit_world_item(item)

    def _on_world_item_selected(self, current, previous):
        if not current:
            self._world_detail.clear()
            return
        role_data = current.data(0, Qt.ItemDataRole.UserRole)
        if not role_data:
            self._world_detail.clear()
            return

        if role_data["type"] == "location":
            d = role_data["data"]
            html = f"<h4>📍 {d.get('name', '')}</h4>"
            html += f"<p>{d.get('description', '')}</p>"
            dp = d.get("dramatic_potential", "")
            if dp:
                html += f"<p><b>戏剧潜力:</b> {dp}</p>"
            self._world_detail.setHtml(html)
        elif role_data["type"] == "faction":
            d = role_data["data"]
            html = f"<h4>⚔ {d.get('name', '')}</h4>"
            html += f"<p>{d.get('description', '')}</p>"
            ci = d.get("core_interest", "")
            if ci:
                html += f"<p><b>核心利益:</b> {ci}</p>"
            self._world_detail.setHtml(html)

    def _add_world_location(self):
        if not self._workspace or not self._book_id:
            return
        from PyQt6.QtWidgets import QDialog, QFormLayout, QLineEdit, QTextEdit, QDialogButtonBox
        dlg = QDialog(self)
        dlg.setWindowTitle("添加新地点")
        dlg.setMinimumWidth(400)
        dlg.setStyleSheet("QDialog{background:#161b22;color:#c9d1d9;} QLabel{color:#c9d1d9;} "
                          "QLineEdit,QTextEdit{background:#0d1117;border:1px solid #30363d;border-radius:4px;"
                          "color:#c9d1d9;padding:4px;}")
        form = QFormLayout(dlg)
        le_id = QLineEdit(f"loc_{id(self) % 10000}")
        form.addRow("ID", le_id)
        le_name = QLineEdit("")
        form.addRow("名称", le_name)
        te_desc = QTextEdit()
        te_desc.setMaximumHeight(80)
        form.addRow("描述", te_desc)
        le_dp = QLineEdit("")
        form.addRow("戏剧潜力", le_dp)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            edit_data = {
                "location_id": le_id.text(),
                "name": le_name.text(),
                "description": te_desc.toPlainText(),
                "dramatic_potential": le_dp.text(),
            }
            cmd = f'novel_edit(book_id="{self._book_id}", edit_type="update_location", data={json.dumps(edit_data, ensure_ascii=False)})'
            self._send_command(cmd)
            self._refresh_world()

    def _add_world_faction(self):
        if not self._workspace or not self._book_id:
            return
        from PyQt6.QtWidgets import QDialog, QFormLayout, QLineEdit, QTextEdit, QDialogButtonBox
        dlg = QDialog(self)
        dlg.setWindowTitle("添加新势力")
        dlg.setMinimumWidth(400)
        dlg.setStyleSheet("QDialog{background:#161b22;color:#c9d1d9;} QLabel{color:#c9d1d9;} "
                          "QLineEdit,QTextEdit{background:#0d1117;border:1px solid #30363d;border-radius:4px;"
                          "color:#c9d1d9;padding:4px;}")
        form = QFormLayout(dlg)
        le_id = QLineEdit(f"fac_{id(self) % 10000}")
        form.addRow("ID", le_id)
        le_name = QLineEdit("")
        form.addRow("名称", le_name)
        te_desc = QTextEdit()
        te_desc.setMaximumHeight(80)
        form.addRow("描述", te_desc)
        le_ci = QLineEdit("")
        form.addRow("核心利益", le_ci)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            edit_data = {
                "faction_id": le_id.text(),
                "name": le_name.text(),
                "description": te_desc.toPlainText(),
                "core_interest": le_ci.text(),
            }
            cmd = f'novel_edit(book_id="{self._book_id}", edit_type="update_faction", data={json.dumps(edit_data, ensure_ascii=False)})'
            self._send_command(cmd)
            self._refresh_world()

    def _edit_selected_world_item(self):
        item = self._world_tree.currentItem()
        if not item:
            return
        self._edit_world_item(item)

    def _edit_world_item(self, item):
        if not self._workspace or not self._book_id:
            return
        role_data = item.data(0, Qt.ItemDataRole.UserRole)
        if not role_data:
            return

        from PyQt6.QtWidgets import QDialog, QFormLayout, QLineEdit, QTextEdit, QDialogButtonBox
        if role_data["type"] == "location":
            d = role_data["data"]
            dlg = QDialog(self)
            dlg.setWindowTitle(f"编辑地点 - {d.get('name', '')}")
            dlg.setMinimumWidth(400)
            dlg.setStyleSheet("QDialog{background:#161b22;color:#c9d1d9;} QLabel{color:#c9d1d9;} "
                              "QLineEdit,QTextEdit{background:#0d1117;border:1px solid #30363d;border-radius:4px;"
                              "color:#c9d1d9;padding:4px;}")
            form = QFormLayout(dlg)

            le_name = QLineEdit(d.get("name", ""))
            form.addRow("名称", le_name)
            te_desc = QTextEdit()
            te_desc.setPlainText(d.get("description", ""))
            te_desc.setMaximumHeight(80)
            form.addRow("描述", te_desc)
            le_dp = QLineEdit(d.get("dramatic_potential", ""))
            form.addRow("戏剧潜力", le_dp)

            btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            btns.accepted.connect(dlg.accept)
            btns.rejected.connect(dlg.reject)
            form.addRow(btns)

            if dlg.exec() == QDialog.DialogCode.Accepted:
                edit_data = {
                    "location_id": role_data["id"],
                    "name": le_name.text(),
                    "description": te_desc.toPlainText(),
                    "dramatic_potential": le_dp.text(),
                }
                cmd = f'novel_edit(book_id="{self._book_id}", edit_type="update_location", data={json.dumps(edit_data, ensure_ascii=False)})'
                self._send_command(cmd)
                self._refresh_world()

        elif role_data["type"] == "faction":
            d = role_data["data"]
            dlg = QDialog(self)
            dlg.setWindowTitle(f"编辑势力 - {d.get('name', '')}")
            dlg.setMinimumWidth(400)
            dlg.setStyleSheet("QDialog{background:#161b22;color:#c9d1d9;} QLabel{color:#c9d1d9;} "
                              "QLineEdit,QTextEdit{background:#0d1117;border:1px solid #30363d;border-radius:4px;"
                              "color:#c9d1d9;padding:4px;}")
            form = QFormLayout(dlg)

            le_name = QLineEdit(d.get("name", ""))
            form.addRow("名称", le_name)
            te_desc = QTextEdit()
            te_desc.setPlainText(d.get("description", ""))
            te_desc.setMaximumHeight(80)
            form.addRow("描述", te_desc)
            le_ci = QLineEdit(d.get("core_interest", ""))
            form.addRow("核心利益", le_ci)

            btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            btns.accepted.connect(dlg.accept)
            btns.rejected.connect(dlg.reject)
            form.addRow(btns)

            if dlg.exec() == QDialog.DialogCode.Accepted:
                edit_data = {
                    "faction_id": role_data["id"],
                    "name": le_name.text(),
                    "description": te_desc.toPlainText(),
                    "core_interest": le_ci.text(),
                }
                cmd = f'novel_edit(book_id="{self._book_id}", edit_type="update_faction", data={json.dumps(edit_data, ensure_ascii=False)})'
                self._send_command(cmd)
                self._refresh_world()

    def _delete_selected_world_item(self):
        item = self._world_tree.currentItem()
        if not item:
            return
        role_data = item.data(0, Qt.ItemDataRole.UserRole)
        if not role_data:
            return

        from PyQt6.QtWidgets import QMessageBox
        item_name = item.text(0)
        reply = QMessageBox.question(
            self, "确认删除", f"确定要删除「{item_name}」吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            if role_data["type"] == "location":
                edit_data = {"location_id": role_data["id"]}
                cmd = f'novel_edit(book_id="{self._book_id}", edit_type="delete_location", data={json.dumps(edit_data, ensure_ascii=False)})'
            elif role_data["type"] == "faction":
                edit_data = {"faction_id": role_data["id"]}
                cmd = f'novel_edit(book_id="{self._book_id}", edit_type="delete_faction", data={json.dumps(edit_data, ensure_ascii=False)})'
            else:
                return
            self._send_command(cmd)
            self._refresh_world()

    def _refresh_tracking(self):
        self._tracking_text.clear()
        if not self._workspace or not self._book_id:
            return
        state_path = Path(self._workspace) / "books" / self._book_id / "state" / "world_state.json"
        if not state_path.exists():
            self._tracking_text.setHtml("<p style='color:#484f58;'>尚无状态数据</p>")
            return
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            html_parts = [f"<h4>当前进度：第 {data.get('current_chapter', 0)} 章</h4>"]

            hooks = data.get("hooks", [])
            open_hooks = [h for h in hooks if h.get("status") == "open"]
            closed_hooks = [h for h in hooks if h.get("status") == "closed"]
            html_parts.append(f"<p><b>伏笔</b>: {len(open_hooks)} 未闭合 / {len(closed_hooks)} 已闭合</p>")
            for h in open_hooks:
                html_parts.append(f"<p style='margin-left:12px;'>{h.get('id', '?')}: {h.get('description', '')}</p>")

            causal = data.get("causal_chain", [])
            html_parts.append(f"<p><b>因果链</b>: {len(causal)} 条</p>")
            for cl in causal[-8:]:
                html_parts.append(
                    f"<p style='margin-left:12px;'>Ch.{cl.get('chapter', '?')}: "
                    f"{cl.get('cause', '')} -> {cl.get('event', '')} -> {cl.get('consequence', '')}</p>"
                )

            threads = data.get("threads", [])
            html_parts.append(f"<p><b>叙事线程</b>: {len(threads)} 条</p>")
            for t in threads:
                html_parts.append(
                    f"<p style='margin-left:12px;'>{t.get('name', '?')}（{t.get('status', '?')}）"
                    f"权重 {t.get('weight', '?')}</p>"
                )

            timeline = data.get("timeline", [])
            if timeline:
                html_parts.append(f"<p><b>时间线</b>: {len(timeline)} 个事件</p>")
                for te in timeline[-10:]:
                    html_parts.append(
                        f"<p style='margin-left:12px;'>Ch.{te.get('chapter', '?')}: "
                        f"{te.get('character_id', '')} - {te.get('action', '')[:50]}</p>"
                    )

            self._tracking_text.setHtml("".join(html_parts))
        except Exception as e:
            self._tracking_text.setHtml(f"<p style='color:#f85149;'>读取状态失败: {e}</p>")

    def _resolve_hook_dialog(self):
        if not self._workspace or not self._book_id:
            return

        state_path = Path(self._workspace) / "books" / self._book_id / "state" / "world_state.json"
        if not state_path.exists():
            return

        data = json.loads(state_path.read_text(encoding="utf-8"))
        open_hooks = [h for h in data.get("hooks", []) if h.get("status") == "open"]
        if not open_hooks:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.information(self, "提示", "没有未闭合的伏笔")
            return

        from PyQt6.QtWidgets import QDialog, QFormLayout, QComboBox, QSpinBox, QDialogButtonBox
        dlg = QDialog(self)
        dlg.setWindowTitle("标记伏笔回收")
        dlg.setMinimumWidth(350)
        dlg.setStyleSheet("QDialog{background:#161b22;color:#c9d1d9;} QLabel{color:#c9d1d9;} "
                          "QComboBox,QSpinBox{background:#0d1117;border:1px solid #30363d;border-radius:4px;"
                          "color:#c9d1d9;padding:4px;}")
        form = QFormLayout(dlg)

        combo = QComboBox()
        for h in open_hooks:
            combo.addItem(f"{h.get('id', '?')}: {h.get('description', '')[:40]}", h.get("id", ""))
        form.addRow("选择伏笔", combo)

        ch_spin = QSpinBox()
        ch_spin.setRange(1, 999)
        ch_spin.setValue(data.get("current_chapter", 1))
        form.addRow("回收章节", ch_spin)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            hook_id = combo.currentData()
            chapter = ch_spin.value()
            edit_data = {"hook_id": hook_id, "chapter": chapter}
            cmd = f'novel_edit(book_id="{self._book_id}", edit_type="resolve_hook", data={json.dumps(edit_data, ensure_ascii=False)})'
            self._send_command(cmd)
            self._refresh_tracking()

    def _adjust_relationship_dialog(self):
        if not self._workspace or not self._book_id:
            return

        setup_path = Path(self._workspace) / "books" / self._book_id / "state" / "setup_state.json"
        if not setup_path.exists():
            return

        setup = json.loads(setup_path.read_text(encoding="utf-8"))
        chars = setup.get("characters", {})
        char_names = {cid: c.get("name", cid) for cid, c in chars.items()}
        if len(char_names) < 2:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.information(self, "提示", "至少需要两个角色才能调整关系")
            return

        from PyQt6.QtWidgets import QDialog, QFormLayout, QComboBox, QSpinBox, QLineEdit, QDialogButtonBox
        dlg = QDialog(self)
        dlg.setWindowTitle("调整角色关系")
        dlg.setMinimumWidth(400)
        dlg.setStyleSheet("QDialog{background:#161b22;color:#c9d1d9;} QLabel{color:#c9d1d9;} "
                          "QComboBox,QSpinBox,QLineEdit{background:#0d1117;border:1px solid #30363d;border-radius:4px;"
                          "color:#c9d1d9;padding:4px;}")
        form = QFormLayout(dlg)

        combo_a = QComboBox()
        combo_b = QComboBox()
        for cid, name in char_names.items():
            combo_a.addItem(name, cid)
            combo_b.addItem(name, cid)
        if combo_b.count() > 1:
            combo_b.setCurrentIndex(1)
        form.addRow("角色A", combo_a)
        form.addRow("角色B", combo_b)

        delta_spin = QSpinBox()
        delta_spin.setRange(-100, 100)
        delta_spin.setValue(0)
        form.addRow("关系变化值", delta_spin)

        le_reason = QLineEdit("手动调整")
        form.addRow("原因", le_reason)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            char_a = combo_a.currentData()
            char_b = combo_b.currentData()
            delta = delta_spin.value()
            reason = le_reason.text()
            if char_a == char_b:
                return
            edit_data = {"character_a": char_a, "character_b": char_b, "delta": delta, "reason": reason}
            cmd = f'novel_edit(book_id="{self._book_id}", edit_type="update_relationship", data={json.dumps(edit_data, ensure_ascii=False)})'
            self._send_command(cmd)
            self._refresh_tracking()

    def _refresh_graph(self):
        if not self._workspace or not self._book_id:
            self._graph_nodes_label.setText("节点: --")
            self._graph_edges_label.setText("边: --")
            self._graph_hooks_label.setText("未闭合伏笔: --")
            self._graph_threads_label.setText("活跃线程: --")
            return
        try:
            from gangge.dramatica.narrative_graph import NarrativeGraphDB, NarrativeQueries
            db_path = Path(self._workspace) / "books" / self._book_id / "state" / "narrative_graph.db"
            if not db_path.exists():
                self._graph_nodes_label.setText("节点: 0")
                self._graph_edges_label.setText("边: 0")
                self._graph_hooks_label.setText("未闭合伏笔: 0")
                self._graph_threads_label.setText("活跃线程: 0")
                return
            db = NarrativeGraphDB(db_path)
            queries = NarrativeQueries(db)
            try:
                summary = queries.get_graph_summary()
                self._graph_nodes_label.setText(f"节点: {summary['nodes']}")
                self._graph_edges_label.setText(f"边: {summary['edges']}")
                self._graph_hooks_label.setText(f"未闭合伏笔: {summary['open_hooks']}")
                self._graph_threads_label.setText(f"活跃线程: {summary['active_threads']}")
            finally:
                db.close()
        except Exception:
            self._graph_nodes_label.setText("节点: --")
            self._graph_edges_label.setText("边: --")
            self._graph_hooks_label.setText("未闭合伏笔: --")
            self._graph_threads_label.setText("活跃线程: --")

    def _refresh_wordbank(self):
        self._word_list.clear()
        self._style_edit.clear()
        if not self._workspace or not self._book_id:
            return
        config_path = Path(self._workspace) / "books" / self._book_id / "state" / "config.json"
        if config_path.exists():
            try:
                cfg = json.loads(config_path.read_text(encoding="utf-8"))
                for w in cfg.get("custom_forbidden_words", []):
                    self._word_list.addItem(w)
                self._style_edit.setPlainText(cfg.get("style_guide", ""))
            except Exception:
                pass

    def _add_word(self):
        word = self._word_input.text().strip()
        if not word:
            return
        self._word_list.addItem(word)
        self._word_input.clear()
        self._save_wordbank_to_config()

    def _del_word(self):
        row = self._word_list.currentRow()
        if row >= 0:
            self._word_list.takeItem(row)
            self._save_wordbank_to_config()

    def _save_wordbank_to_config(self):
        if not self._workspace or not self._book_id:
            return
        config_path = Path(self._workspace) / "books" / self._book_id / "state" / "config.json"
        if not config_path.exists():
            return
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            words = []
            for i in range(self._word_list.count()):
                words.append(self._word_list.item(i).text())
            cfg["custom_forbidden_words"] = words
            config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _save_style_guide(self):
        if not self._workspace or not self._book_id:
            return
        config_path = Path(self._workspace) / "books" / self._book_id / "state" / "config.json"
        if not config_path.exists():
            return
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            cfg["style_guide"] = self._style_edit.toPlainText()
            config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _emit_action(self, action: str, extra: dict | None = None):
        data = {"book_id": self._book_id}
        if extra:
            data.update(extra)
        self.novel_action.emit(action, data)

    def _send_command(self, cmd: str, auto_run: bool = True):
        action = "custom_command" if auto_run else "custom_command_preview"
        self.novel_action.emit(action, {"book_id": self._book_id, "command": cmd})


class NovelNewBookDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("新建小说")
        self.setMinimumWidth(520)
        self.setStyleSheet(
            "QDialog{background:#0d1117;}"
            "QLabel{color:#c9d1d9;font-size:12px;}"
            "QLineEdit{background:#161b22;border:1px solid #30363d;border-radius:4px;"
            "padding:6px 10px;color:#c9d1d9;font-size:12px;}"
            "QLineEdit:focus{border:1px solid #58a6ff;}"
            "QTextEdit{background:#161b22;border:1px solid #30363d;border-radius:4px;"
            "padding:6px 10px;color:#c9d1d9;font-size:12px;}"
            "QTextEdit:focus{border:1px solid #58a6ff;}"
            "QSpinBox{background:#161b22;border:1px solid #30363d;border-radius:4px;"
            "padding:4px 8px;color:#c9d1d9;font-size:12px;}"
            "QComboBox{background:#161b22;border:1px solid #30363d;border-radius:4px;"
            "padding:4px 8px;color:#c9d1d9;font-size:12px;}"
            "QPushButton{background:#238636;color:#fff;border:none;border-radius:6px;"
            "font-size:12px;font-weight:bold;padding:8px 20px;}"
            "QPushButton:hover{background:#2ea043;}"
        )

        lay = QVBoxLayout(self)
        lay.setSpacing(12)

        title_label = QLabel("创建新小说")
        title_label.setStyleSheet("color:#f0f6fc;font-size:16px;font-weight:bold;")
        lay.addWidget(title_label)

        form = QFormLayout()
        form.setSpacing(8)

        self._title_input = QLineEdit()
        self._title_input.setPlaceholderText("输入小说书名...")
        form.addRow("书名:", self._title_input)

        self._genre_combo = QComboBox()
        self._genre_combo.setEditable(True)
        self._genre_combo.addItems([
            "玄幻", "都市", "科幻", "悬疑", "武侠", "仙侠",
            "历史", "军事", "游戏", "体育", "现实", "奇幻",
            "恐怖", "言情", "轻小说",
        ])
        form.addRow("题材:", self._genre_combo)

        self._chapters_spin = QSpinBox()
        self._chapters_spin.setRange(10, 2000)
        self._chapters_spin.setValue(100)
        form.addRow("目标章数:", self._chapters_spin)

        self._words_spin = QSpinBox()
        self._words_spin.setRange(1000, 10000)
        self._words_spin.setValue(4000)
        self._words_spin.setSingleStep(500)
        form.addRow("每章字数:", self._words_spin)

        lay.addLayout(form)

        ideas_label = QLabel("💡 创作构思（选填）")
        ideas_label.setStyleSheet("color:#58a6ff;font-size:12px;font-weight:bold;margin-top:4px;")
        lay.addWidget(ideas_label)

        ideas_hint = QLabel(
            "描述你的故事想法，AI 会根据你的构思自动配置角色和世界观。\n"
            "如果暂时没有想法，留空即可，后续随时可以在聊天窗口补充。"
        )
        ideas_hint.setWordWrap(True)
        ideas_hint.setStyleSheet("color:#484f58;font-size:10px;margin-bottom:2px;")
        lay.addWidget(ideas_hint)

        self._ideas_input = QTextEdit()
        self._ideas_input.setPlaceholderText(
            "例如：\n"
            "  • 一个程序员穿越到魔法世界，用编程思维破解魔法体系\n"
            "  • 主角表面是普通学生，实际上是隐藏的特工\n"
            "  • 末世废土背景，主角拥有植物操控能力，重建文明\n\n"
            "提示：越具体，AI 生成的角色和大纲越贴合你的想法"
        )
        self._ideas_input.setMaximumHeight(120)
        self._ideas_input.setMinimumHeight(80)
        lay.addWidget(self._ideas_input)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.setStyleSheet(
            "QPushButton{background:#21262d;color:#c9d1d9;border:1px solid #30363d;}"
            "QPushButton:hover{background:#30363d;}"
        )
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        ok_btn = QPushButton("创建")
        ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(ok_btn)
        lay.addLayout(btn_row)

        footer_hint = QLabel("💡 创建后仅建立书籍骨架，不消耗 Token。在聊天窗口说出想法即可开始创作。")
        footer_hint.setWordWrap(True)
        footer_hint.setStyleSheet("color:#484f58;font-size:9px;padding:2px;")
        lay.addWidget(footer_hint)

    def get_data(self) -> dict:
        ideas = self._ideas_input.toPlainText().strip()
        return {
            "title": self._title_input.text().strip(),
            "genre": self._genre_combo.currentText().strip(),
            "target_chapters": self._chapters_spin.value(),
            "words_per_chapter": self._words_spin.value(),
            "ideas": ideas,
        }


# ═════════════════════════════════════════════════════════════════
#  Tool Call Table + Inline Diff Viewer
# ═════════════════════════════════════════════════════════════════
class ToolCallPanel(QWidget):
    """Tool call table with inline diff viewer — saves vertical space."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # Header with count
        header = QHBoxLayout()
        self._count_label = QLabel(_t("tool_calls", n=0))
        self._count_label.setProperty("heading", True)
        header.addWidget(self._count_label)
        header.addStretch()
        clear_btn = QPushButton(_t("btn_clear"))
        clear_btn.setStyleSheet(
            "QPushButton{background:#21262d;border:1px solid #30363d;border-radius:4px;"
            "color:#8b949e;font-size:11px;padding:2px 8px;}"
            "QPushButton:hover{background:#30363d;color:#c9d1d9;}"
        )
        clear_btn.clicked.connect(self.clear_entries)
        header.addWidget(clear_btn)
        layout.addLayout(header)

        # Table
        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels([_t("tool_header_num"), _t("tool_header_name"), _t("tool_header_status"), _t("tool_header_output"), ""])
        self._table.horizontalHeader().setStretchLastSection(False)
        self._table.setColumnWidth(0, 36)
        self._table.setColumnWidth(1, 90)
        self._table.setColumnWidth(2, 50)
        self._table.setColumnWidth(4, 40)
        self._table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setStyleSheet(
            "QTableWidget{background-color:#0d1117;border:1px solid #21262d;border-radius:6px;"
            "gridline-color:#161b22;color:#c9d1d9;}"
            "QTableWidget::item{padding:4px 6px;font-size:12px;}"
            "QTableWidget::item:selected{background:#1f6feb;}"
        )
        self._table.cellClicked.connect(self._on_cell_clicked)
        layout.addWidget(self._table)

        # Inline diff viewer (hidden by default)
        self._diff_frame = QFrame()
        self._diff_frame.setStyleSheet(
            "QFrame{background:#0d1117;border:1px solid #30363d;border-radius:6px;}"
        )
        diff_layout = QVBoxLayout(self._diff_frame)
        diff_layout.setContentsMargins(8, 6, 8, 6)
        diff_layout.setSpacing(4)

        diff_header = QHBoxLayout()
        diff_title = QLabel("📝 文件变更")
        diff_title.setStyleSheet("color:#58a6ff;font-size:12px;font-weight:bold;")
        diff_header.addWidget(diff_title)
        diff_header.addStretch()
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(22, 22)
        close_btn.setStyleSheet(
            "QPushButton{background:transparent;color:#8b949e;font-size:12px;border:none;}"
            "QPushButton:hover{color:#f85149;}"
        )
        close_btn.clicked.connect(lambda: self._diff_frame.setVisible(False))
        diff_header.addWidget(close_btn)
        diff_layout.addLayout(diff_header)

        self._diff_viewer = DiffViewer()
        self._diff_viewer.setMinimumHeight(120)
        self._diff_viewer.setMaximumHeight(300)
        self._diff_viewer.rollback_requested.connect(self._on_rollback_diff)
        diff_layout.addWidget(self._diff_viewer)
        self._diff_frame.setVisible(False)
        layout.addWidget(self._diff_frame)

        self._entries: list[dict] = []
        self._expanded_row: int = -1

    def add_entry(self, tool_name: str, output: str, is_error: bool, diff: str = ""):
        row = self._table.rowCount()
        self._table.insertRow(row)

        # Row height
        self._table.setRowHeight(row, 28)

        # #
        num = QTableWidgetItem(str(row + 1))
        num.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        num.setForeground(QColor("#484f58"))
        self._table.setItem(row, 0, num)

        # Tool name with icon
        icon_map = {
            "write_file": "📝", "edit_file": "✏️", "read_file": "📄",
            "bash": "💻", "grep": "🔍", "glob": "📂", "list_dir": "📁",
            "web_fetch": "🌐", "ask_user": "❓", "lint_check": "🔍",
        }
        icon = icon_map.get(tool_name, "🔧")
        tool_item = QTableWidgetItem(f"{icon} {tool_name}")
        tool_item.setForeground(QColor("#d29922"))
        self._table.setItem(row, 1, tool_item)

        # Status
        status_text = "❌" if is_error else "✅"
        status_item = QTableWidgetItem(status_text)
        status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        status_item.setForeground(QColor("#f85149" if is_error else "#3fb950"))
        self._table.setItem(row, 2, status_item)

        # Output (truncated)
        out_text = output[:120].replace("\n", " ")
        out_item = QTableWidgetItem(out_text)
        out_item.setToolTip(output[:500])
        out_item.setForeground(QColor("#8b949e"))
        self._table.setItem(row, 3, out_item)

        # Diff indicator
        has_diff = bool(diff and diff.strip())
        diff_btn = QTableWidgetItem("📋" if has_diff else "")
        diff_btn.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        diff_btn.setToolTip(_t("tip_diff") if has_diff else _t("tip_no_diff"))
        diff_btn.setForeground(QColor("#58a6ff" if has_diff else "#30363d"))
        self._table.setItem(row, 4, diff_btn)

        self._entries.append({
            "tool_name": tool_name,
            "output": output,
            "is_error": is_error,
            "diff": diff,
            "has_diff": has_diff,
        })
        self._count_label.setText(_t("tool_calls", n=len(self._entries)))
        self._table.scrollToBottom()

    def _on_cell_clicked(self, row: int, col: int):
        if row < 0 or row >= len(self._entries):
            return
        entry = self._entries[row]

        # Click diff column or any row to toggle diff
        if col == 4 or entry.get("has_diff"):
            if self._expanded_row == row and self._diff_frame.isVisible():
                self._diff_frame.setVisible(False)
                self._expanded_row = -1
            else:
                self._diff_viewer.show_diff(entry.get("diff", ""))
                self._diff_frame.setVisible(True)
                self._expanded_row = row
        else:
            # Show output in diff viewer for non-diff entries
            self._diff_viewer.set_plain_text(entry.get("output", "") or "(无输出)")
            self._diff_frame.setVisible(True)
            self._expanded_row = row

    def clear_entries(self):
        self._table.setRowCount(0)
        self._entries.clear()
        self._diff_frame.setVisible(False)
        self._expanded_row = -1
        self._count_label.setText(_t("tool_calls", n=0))

    def _on_rollback_diff(self):
        from PyQt6.QtWidgets import QMessageBox
        window = self.window()
        workspace = ""
        if hasattr(window, "_ws_input"):
            workspace = window._ws_input.text().strip()
        if not workspace:
            return
        from gangge.layer4_tools.shadow_git import ShadowGit
        sg = ShadowGit(workspace)
        if not sg.is_available():
            return
        reply = QMessageBox.warning(
            window, _t("rollback_confirm"),
            _t("rollback_done_simple"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        if sg.rollback("HEAD~1"):
            if hasattr(window, "_append_output"):
                window._append_output(_t("rollback_done_simple"), "system")
            if hasattr(window, "_file_browser"):
                window._file_browser.set_root(workspace)
            if hasattr(window, "_status_label"):
                window._status_label.setText(_t("status_rollback_ok"))
            self._diff_frame.setVisible(False)
        else:
            if hasattr(window, "_status_label"):
                window._status_label.setText(_t("status_rollback_fail"))


# ═════════════════════════════════════════════════════════════════
#  Project Context Scanner
# ═════════════════════════════════════════════════════════════════
def scan_project_context(workspace: str) -> str:
    """Scan workspace for ARCH.md, README.md, .ganggerules, and directory structure."""
    parts = []
    ws = Path(workspace)
    if not ws.is_dir():
        return ""

    # 1. Read ARCH.md if exists
    arch_path = ws / "ARCH.md"
    if arch_path.exists():
        try:
            content = arch_path.read_text(encoding="utf-8", errors="replace")[:3000]
            parts.append(f"## 架构文档 (ARCH.md)\n{content}")
        except Exception:
            pass

    # 2. Read README.md if exists (first 100 lines)
    readme_path = ws / "README.md"
    if readme_path.exists():
        try:
            lines = readme_path.read_text(encoding="utf-8", errors="replace").splitlines()[:100]
            parts.append(f"## 项目说明 (README.md)\n" + "\n".join(lines))
        except Exception:
            pass

    # 3. Read .ganggerules if exists
    rules_path = ws / ".ganggerules"
    if rules_path.exists():
        try:
            content = rules_path.read_text(encoding="utf-8", errors="replace")[:3000]
            parts.append(f"## 项目规则 (.ganggerules)\n{content}")
        except Exception:
            pass

    # 4. Directory structure (top 2 levels)
    try:
        struct_lines = [f"{ws.name}/"]
        exclude = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build", ".idea", ".vscode"}
        entries = sorted(ws.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        for e in entries:
            if e.name.startswith(".") or e.name in exclude:
                continue
            prefix = "├── " if e != entries[-1] else "└── "
            suffix = "/" if e.is_dir() else ""
            struct_lines.append(f"{prefix}{e.name}{suffix}")
            if e.is_dir() and e.name not in exclude:
                try:
                    sub = sorted(e.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))[:10]
                    for s2 in sub:
                        if s2.name.startswith(".") or s2.name in exclude:
                            continue
                        p2 = "│   ├── " if s2 != sub[-1] else "│   └── "
                        s2_suffix = "/" if s2.is_dir() else ""
                        struct_lines.append(f"{p2}{s2.name}{s2_suffix}")
                except Exception:
                    pass
        parts.append("## 目录结构\n" + "\n".join(struct_lines))
    except Exception:
        pass

    return "\n\n".join(parts)


# ═════════════════════════════════════════════════════════════════
#  Project Map Builder — scans workspace for all .py files,
#  extracts class/function names from file headers.
# ═════════════════════════════════════════════════════════════════
def build_project_map(workspace: str, max_entries: int = 120) -> str:
    """Generate a project file index using repo_index (multi-language)."""
    from gangge.layer4_tools.repo_index import (
        get_or_build_index, build_dependency_graph, format_project_map,
    )
    try:
        index = get_or_build_index(workspace)
        dep_graph = build_dependency_graph(index, workspace)
        return format_project_map(index, dep_graph, max_entries)
    except Exception:
        pass

    ws = Path(workspace)
    if not ws.is_dir():
        return ""

    lines = []
    exclude_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build", ".idea", ".vscode", ".egg-info"}

    for py_file in sorted(ws.rglob("*.py")):
        if any(p in exclude_dirs for p in py_file.parts):
            continue
        if ".egg-info" in str(py_file):
            continue
        try:
            rel = py_file.relative_to(ws)
            head = ""
            with open(py_file, encoding="utf-8", errors="replace") as f:
                for _ in range(20):
                    line = f.readline()
                    if not line:
                        break
                    s = line.strip()
                    if s.startswith(("class ", "def ", "async def ")):
                        sig = s.split("(")[0].strip().rstrip(":")
                        head += sig + "; "
            if head:
                lines.append(f"- `{rel}`: {head[:150]}")
            else:
                lines.append(f"- `{rel}`")
        except Exception:
            pass

    if not lines:
        return ""

    truncated = lines[:max_entries]
    result = "\n".join(truncated)
    if len(lines) > max_entries:
        result += f"\n... 共 {len(lines)} 个文件，仅显示前 {max_entries} 个"

    return result


# ═════════════════════════════════════════════════════════════════
#  File Registry Builder — initializes registry from existing files
# ═════════════════════════════════════════════════════════════════
def build_initial_file_registry(workspace: str) -> dict[str, dict]:
    """Scan existing files to build initial file registry using repo_index."""
    from gangge.layer4_tools.repo_index import get_or_build_index

    registry: dict[str, dict] = {}
    try:
        index = get_or_build_index(workspace)
        files = index.get("files", {})
        for path, entry in files.items():
            syms = entry.get("symbols", [])
            classes = [s["name"] for s in syms if s["kind"] == "class"][:10]
            functions = [s["name"] for s in syms if s["kind"] in ("function", "method")][:15]
            registry[path] = {
                "classes": classes,
                "functions": functions,
                "last_action": "existing",
                "round": 0,
            }
        return registry
    except Exception:
        pass

    ws = Path(workspace)
    if not ws.is_dir():
        return registry

    exclude_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build", ".idea", ".vscode"}
    for py_file in sorted(ws.rglob("*.py")):
        if any(p in exclude_dirs for p in py_file.parts):
            continue
        try:
            rel = str(py_file.relative_to(ws))
            text = py_file.read_text(encoding="utf-8", errors="replace")
            classes = []
            functions = []
            for line in text.splitlines():
                s = line.strip()
                if s.startswith("class ") and ":" in s:
                    name = s.split("(")[0].replace("class ", "").replace(":", "").strip()
                    classes.append(name)
                elif s.startswith(("def ", "async def ")):
                    name = s.replace("async def ", "").replace("def ", "").split("(")[0].strip()
                    functions.append(name)
            registry[rel] = {
                "classes": classes[:10],
                "functions": functions[:15],
                "last_action": "existing",
                "round": 0,
            }
        except Exception:
            pass

    return registry


# ═════════════════════════════════════════════════════════════════
#  Memory Bank — project-level progress tracking via .md files
# ═════════════════════════════════════════════════════════════════
MEMORY_BANK_DIR = ".gangge"

def read_memory_bank(workspace: str) -> tuple[str, str]:
    """Read .gangge/progress.md and .gangge/changelog.md content."""
    ws = Path(workspace)
    mb_dir = ws / MEMORY_BANK_DIR
    progress = ""
    changelog = ""
    if mb_dir.exists():
        p = mb_dir / "progress.md"
        if p.exists():
            try:
                progress = p.read_text(encoding="utf-8", errors="replace")[:3000]
            except Exception:
                pass
        c = mb_dir / "changelog.md"
        if c.exists():
            try:
                changelog = c.read_text(encoding="utf-8", errors="replace")[:3000]
            except Exception:
                pass
    return progress, changelog


# ═════════════════════════════════════════════════════════════════
#  Git Status Detector — branch, uncommitted changes, recent commits
# ═════════════════════════════════════════════════════════════════
def detect_git_state(workspace: str) -> str:
    """Detect current Git state: branch, uncommitted files, recent commits."""
    ws = Path(workspace)
    git_dir = ws / ".git"
    if not git_dir.exists():
        return ""

    import subprocess
    parts = []

    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=workspace, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
        ).stdout.strip()
        if branch and branch != "HEAD":
            parts.append(f"分支: {branch}")
    except Exception:
        pass

    try:
        status = subprocess.run(
            ["git", "status", "--short"],
            cwd=workspace, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
        ).stdout.strip()
        if status:
            lines = status.splitlines()[:10]
            parts.append(
                f"未提交变更 ({len(lines)} 文件):\n"
                + "\n".join(f"  {l}" for l in lines)
            )
    except Exception:
        pass

    try:
        log = subprocess.run(
            ["git", "log", "--oneline", "-5"],
            cwd=workspace, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
        ).stdout.strip()
        if log:
            log_lines = log.splitlines()
            parts.append("最近提交:\n" + "\n".join(f"  {l}" for l in log_lines))
    except Exception:
        pass

    return "\n".join(parts) if parts else ""


def auto_git_commit(workspace: str, message: str) -> str:
    """Auto git add + commit. Returns commit hash or error."""
    import subprocess
    try:
        subprocess.run(
            ["git", "add", "-A"],
            cwd=workspace, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
        result = subprocess.run(
            ["git", "commit", "-m", message[:80]],
            cwd=workspace, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
        if result.returncode == 0:
            hash_match = ""
            for line in (result.stdout or "").splitlines():
                if "commit" in line:
                    hash_match = line.strip()
                    break
            return hash_match or "committed"
        elif "nothing to commit" in (result.stdout or "") or "nothing to commit" in (result.stderr or ""):
            return "(无变更)"
        else:
            return f"commit failed: {(result.stderr or '')[:200]}"
    except Exception as e:
        return f"git error: {e}"



def update_memory_bank(workspace: str, progress_update: str, changelog_update: str):
    """Write update to .gangge/progress.md and .gangge/changelog.md."""
    ws = Path(workspace)
    mb_dir = ws / MEMORY_BANK_DIR
    mb_dir.mkdir(parents=True, exist_ok=True)

    if progress_update:
        p = mb_dir / "progress.md"
        try:
            # Append to existing
            existing = ""
            if p.exists():
                existing = p.read_text(encoding="utf-8", errors="replace")
            p.write_text(existing + "\n" + progress_update, encoding="utf-8")
        except Exception:
            pass

    if changelog_update:
        c = mb_dir / "changelog.md"
        try:
            # Prepend to changelog (newest first)
            existing = ""
            if c.exists():
                existing = c.read_text(encoding="utf-8", errors="replace")
            c.write_text(changelog_update + "\n---\n" + existing, encoding="utf-8")
        except Exception:
            pass


# ═════════════════════════════════════════════════════════════════
#  Git Remote Dialog — bind existing repo or create new on GitHub
# ═════════════════════════════════════════════════════════════════

class GitRemoteDialog(QDialog):
    def __init__(self, parent, github_token: str = "", project_name: str = ""):
        super().__init__(parent)
        self.setWindowTitle(_t("git_remote_title"))
        self.setMinimumWidth(520)
        self.setStyleSheet("QDialog{background:#0d1117;}")
        self._result_url = ""
        self._token = github_token

        layout = QVBoxLayout(self)
        layout.setSpacing(16)

        title = QLabel(_t("git_remote_heading"))
        title.setStyleSheet("color:#f0f6fc;font-size:15px;font-weight:bold;")
        layout.addWidget(title)

        # ── Tab: Existing repo ──
        tabs = QTabWidget()
        tabs.setStyleSheet(
            "QTabWidget::pane{border:1px solid #30363d;border-radius:6px;background:#0d1117;}"
            "QTabBar::tab{background:#161b22;color:#8b949e;border:1px solid #30363d;"
            "border-bottom:none;padding:8px 18px;font-size:12px;border-radius:4px 4px 0 0;}"
            "QTabBar::tab:selected{background:#0d1117;color:#58a6ff;border-bottom:2px solid #58a6ff;}"
        )

        # Tab 1: Enter existing URL
        tab_existing = QWidget()
        te_lay = QVBoxLayout(tab_existing)
        te_lay.setSpacing(10)

        te_hint = QLabel(_t("git_remote_existing_hint"))
        te_hint.setStyleSheet("color:#8b949e;font-size:12px;")
        te_hint.setWordWrap(True)
        te_lay.addWidget(te_hint)

        self._url_input = QLineEdit()
        self._url_input.setPlaceholderText("https://github.com/user/repo.git")
        self._url_input.setStyleSheet(
            "QLineEdit{background:#161b22;color:#c9d1d9;border:1px solid #30363d;"
            "border-radius:6px;padding:8px 12px;font-size:13px;}"
            "QLineEdit:focus{border-color:#58a6ff;}"
        )
        te_lay.addWidget(self._url_input)

        open_browser_btn = QPushButton(_t("git_remote_open_browser"))
        open_browser_btn.setStyleSheet(
            "QPushButton{background:#21262d;color:#58a6ff;border:1px solid #30363d;"
            "border-radius:6px;padding:6px 14px;font-size:12px;}"
            "QPushButton:hover{background:#30363d;}"
        )
        open_browser_btn.clicked.connect(self._open_github_new)
        te_lay.addWidget(open_browser_btn)
        te_lay.addStretch()
        tabs.addTab(tab_existing, _t("git_remote_tab_existing"))

        # Tab 2: Create new repo (only useful with token)
        tab_create = QWidget()
        tc_lay = QVBoxLayout(tab_create)
        tc_lay.setSpacing(10)

        if github_token:
            tc_hint = QLabel(_t("git_remote_create_hint"))
            tc_hint.setStyleSheet("color:#8b949e;font-size:12px;")
            tc_hint.setWordWrap(True)
            tc_lay.addWidget(tc_hint)

            name_lay = QHBoxLayout()
            name_label = QLabel(_t("git_remote_repo_name"))
            name_label.setStyleSheet("color:#c9d1d9;font-size:13px;")
            name_lay.addWidget(name_label)
            self._repo_name_input = QLineEdit()
            self._repo_name_input.setText(project_name)
            self._repo_name_input.setStyleSheet(
                "QLineEdit{background:#161b22;color:#c9d1d9;border:1px solid #30363d;"
                "border-radius:6px;padding:8px 12px;font-size:13px;}"
                "QLineEdit:focus{border-color:#58a6ff;}"
            )
            name_lay.addWidget(self._repo_name_input, 1)
            tc_lay.addLayout(name_lay)

            desc_lay = QHBoxLayout()
            desc_label = QLabel(_t("git_remote_repo_desc"))
            desc_label.setStyleSheet("color:#c9d1d9;font-size:13px;")
            desc_lay.addWidget(desc_label)
            self._repo_desc_input = QLineEdit()
            self._repo_desc_input.setPlaceholderText(_t("git_remote_repo_desc_ph"))
            self._repo_desc_input.setStyleSheet(self._repo_name_input.styleSheet())
            desc_lay.addWidget(self._repo_desc_input, 1)
            tc_lay.addLayout(desc_lay)

            self._private_cb = QCheckBox(_t("git_remote_private"))
            self._private_cb.setChecked(True)
            self._private_cb.setStyleSheet("color:#c9d1d9;font-size:13px;")
            tc_lay.addWidget(self._private_cb)

            self._create_status = QLabel("")
            self._create_status.setStyleSheet("color:#f0883e;font-size:12px;")
            self._create_status.setWordWrap(True)
            tc_lay.addWidget(self._create_status)
        else:
            no_token_hint = QLabel(_t("git_remote_no_token"))
            no_token_hint.setStyleSheet("color:#f0883e;font-size:13px;")
            no_token_hint.setWordWrap(True)
            tc_lay.addWidget(no_token_hint)

            go_settings_btn = QPushButton(_t("git_remote_go_settings"))
            go_settings_btn.setStyleSheet(
                "QPushButton{background:#238636;color:#fff;border-radius:6px;"
                "padding:8px 18px;font-size:13px;}"
                "QPushButton:hover{background:#2ea043;}"
            )
            go_settings_btn.clicked.connect(self._go_settings)
            tc_lay.addWidget(go_settings_btn)
            tc_lay.addStretch()

        tabs.addTab(tab_create, _t("git_remote_tab_create"))
        layout.addWidget(tabs)

        # ── Buttons ──
        btn_bar = QHBoxLayout()
        btn_bar.addStretch()

        cancel_btn = QPushButton(_t("btn_cancel"))
        cancel_btn.setStyleSheet(
            "QPushButton{background:#21262d;color:#c9d1d9;border:1px solid #30363d;"
            "border-radius:6px;padding:8px 20px;font-size:13px;}"
            "QPushButton:hover{background:#30363d;}"
        )
        cancel_btn.clicked.connect(self.reject)
        btn_bar.addWidget(cancel_btn)

        self._ok_btn = QPushButton(_t("git_remote_bind"))
        self._ok_btn.setStyleSheet(
            "QPushButton{background:#238636;color:#fff;border-radius:6px;padding:8px 24px;"
            "font-size:13px;font-weight:600;}"
            "QPushButton:hover{background:#2ea043;}"
        )
        self._ok_btn.clicked.connect(self._on_accept)
        btn_bar.addWidget(self._ok_btn)
        layout.addLayout(btn_bar)

    def _open_github_new(self):
        import webbrowser
        webbrowser.open("https://github.com/new")

    def _go_settings(self):
        self.reject()
        parent = self.parent()
        if parent and hasattr(parent, "_open_settings"):
            parent._open_settings()

    def _on_accept(self):
        from gangge.layer4_tools.shadow_git import ShadowGit
        current_tab_idx = self.findChild(QTabWidget).currentIndex()

        if current_tab_idx == 0:
            url = self._url_input.text().strip()
            if not url:
                return
            self._result_url = url
            self.accept()
        else:
            if not self._token:
                return
            repo_name = self._repo_name_input.text().strip()
            if not repo_name:
                return
            self._create_status.setText(_t("git_remote_creating"))
            self._create_status.setStyleSheet("color:#58a6ff;font-size:12px;")
            QApplication.processEvents()

            result = ShadowGit.create_github_repo(
                self._token,
                repo_name,
                private=self._private_cb.isChecked(),
                description=self._repo_desc_input.text().strip(),
            )
            if result["success"]:
                self._result_url = result["clone_url"]
                self._create_status.setText(_t("git_remote_created", url=result["html_url"]))
                self._create_status.setStyleSheet("color:#3fb950;font-size:12px;")
                QApplication.processEvents()
                self.accept()
            else:
                self._create_status.setText(_t("git_remote_create_fail", error=result.get("error", "")))
                self._create_status.setStyleSheet("color:#f85149;font-size:12px;")

    def get_result_url(self) -> str:
        return self._result_url


# ── Helper: fetch Ollama models ─────────────────────────────
def _fetch_ollama_models(base_url: str) -> list[str]:
    """Try to fetch installed models from a running Ollama instance.

    Derives the Ollama API URL from the OpenAI-compatible base_url.
    Falls back to empty list if connection fails.
    """
    try:
        # Convert "http://host:port/v1" -> "http://host:port/api/tags"
        api_url = base_url.rstrip("/")
        if api_url.endswith("/v1"):
            api_url = api_url[:-3]
        api_url = api_url.rstrip("/") + "/api/tags"

        req = Request(api_url, method="GET")
        with urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
        models = []
        for m in data.get("models", []):
            name = m.get("name", "")
            if name:
                name = name.split(":latest")[0]  # remove :latest suffix
                if name not in models:
                    models.append(name)
        return sorted(models)
    except (URLError, json.JSONDecodeError, OSError, Exception):
        return []


# ═════════════════════════════════════════════════════════════════
#  Main Window
# ═════════════════════════════════════════════════════════════════
class GanggeDesktop(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{_t('app_title')} — {_t('app_subtitle')}")
        self.setMinimumSize(1200, 800)
        self.resize(1500, 920)

        self._settings = QSettings("Gangge", "GanggeCode")
        self._llm: BaseLLM | None = None
        self._worker: GanggeWorker | None = None
        self._running = False
        self._current_session_id: str = ""
        self._batch_tasks: list[str] = []
        self._batch_queue: list[str] = []

        # Session DB
        self._db = SessionDB()
        self._db.connect()

        self._setup_menu()
        self._setup_ui()
        self._load_settings()
        self._refresh_session_list()
        self._update_provider_fields()

        # Status bar — multi-section with real-time stats
        sb = QStatusBar()
        self.setStatusBar(sb)
        sb.setStyleSheet("QStatusBar{background:#161b22;border-top:1px solid #21262d;padding:2px 8px;}")

        # Left: main status message
        self._status_label = QLabel(_t("status_ready"))
        self._status_label.setStyleSheet("color:#c9d1d9;font-size:12px;padding:2px 8px;")
        sb.addWidget(self._status_label, 1)

        # Center: live stats (rounds, tokens)
        self._stats_label = QLabel("")
        self._stats_label.setStyleSheet("color:#8b949e;font-size:11px;padding:2px 8px;")
        sb.addPermanentWidget(self._stats_label)

        # Right: progress bar
        self._status_progress = QProgressBar()
        self._status_progress.setMaximumWidth(120)
        self._status_progress.setMaximumHeight(14)
        self._status_progress.setStyleSheet(
            "QProgressBar{background:#21262d;border:1px solid #30363d;border-radius:6px;"
            "text-align:center;color:#8b949e;font-size:10px;height:14px;}"
            "QProgressBar::chunk{background:#1f6feb;border-radius:5px;}"
        )
        self._status_progress.setVisible(False)
        sb.addPermanentWidget(self._status_progress)

        # Far right: timer
        self._timer_label = QLabel("")
        self._timer_label.setStyleSheet("color:#484f58;font-size:11px;padding:2px 4px;")
        sb.addPermanentWidget(self._timer_label)

    # ── Menu ──────────────────────────────────────────────────
    def _setup_menu(self):
        mb = self.menuBar()
        fm = mb.addMenu(_t("menu_file"))
        a = QAction(_t("menu_new_session"), self)
        a.setShortcut(QKeySequence("Ctrl+N"))
        a.triggered.connect(self._new_session)
        fm.addAction(a)
        fm.addSeparator()
        a = QAction(_t("menu_clear_output"), self)
        a.setShortcut(QKeySequence("Ctrl+L"))
        a.triggered.connect(self._clear_output)
        fm.addAction(a)
        fm.addSeparator()
        a = QAction(_t("menu_quit"), self)
        a.setShortcut(QKeySequence("Ctrl+Q"))
        a.triggered.connect(self.close)
        fm.addAction(a)

        tm = mb.addMenu(_t("menu_tools"))
        a = QAction(_t("menu_open_workspace"), self)
        a.setShortcut(QKeySequence("Ctrl+O"))
        a.triggered.connect(self._browse_workspace)
        tm.addAction(a)

        hm = mb.addMenu(_t("menu_help"))
        a = QAction(_t("menu_about"), self)
        a.triggered.connect(lambda: QMessageBox.about(
            self, _t("menu_about"), _t("about_text")))
        hm.addAction(a)

    # ── Config widgets (created early, displayed in settings dialog) ──
    def _init_config_widgets(self):
        """Create config widgets that need to exist before _load_settings."""
        # These are parented to self but not in any layout until _open_settings
        self._api_key_input = QLineEdit(self)
        self._api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key_input.setPlaceholderText("API Key...")
        self._api_key_input.hide()

        self._model_combo = QComboBox(self)
        self._model_combo.setEditable(True)
        self._model_combo.hide()

        self._base_url_input = QLineEdit(self)
        self._base_url_input.setPlaceholderText("API Base URL (如需要)")
        self._base_url_input.hide()

        self._max_rounds_spin = QSpinBox(self)
        self._max_rounds_spin.setRange(5, 100)
        self._max_rounds_spin.setValue(30)
        self._max_rounds_spin.hide()

        self._auto_allow_cb = QCheckBox(_t("cb_auto_allow"), self)
        self._auto_allow_cb.setChecked(True)
        self._auto_allow_cb.hide()

        self._auto_inject_cb = QCheckBox(_t("cb_auto_inject"), self)
        self._auto_inject_cb.setChecked(True)
        self._auto_inject_cb.hide()

        self._test_verify_cb = QCheckBox(_t("cb_test_verify"), self)
        self._test_verify_cb.setChecked(True)
        self._test_verify_cb.hide()

        self._git_commit_cb = QCheckBox(_t("cb_git_commit"), self)
        self._git_commit_cb.setChecked(True)
        self._git_commit_cb.hide()

        self._lang_combo = QComboBox(self)
        self._lang_combo.addItem("中文", "zh")
        self._lang_combo.addItem("English", "en")
        self._lang_combo.hide()

        self._plan_mode_cb = QCheckBox(_t("cb_plan_mode"), self)
        self._plan_mode_cb.hide()

        self._thinking_mode_cb = QCheckBox("思考模式", self)
        self._thinking_mode_cb.hide()

        self._mm_enable_cb = QCheckBox("启用多模态模型", self)
        self._mm_enable_cb.hide()

        self._mm_provider_combo = QComboBox(self)
        for k, cfg in PROVIDER_CONFIGS.items():
            self._mm_provider_combo.addItem(cfg["label"], k)
        self._mm_provider_combo.hide()

        self._mm_api_key_input = QLineEdit(self)
        self._mm_api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._mm_api_key_input.setPlaceholderText("Multimodal API Key")
        self._mm_api_key_input.hide()

        self._mm_model_combo = QComboBox(self)
        self._mm_model_combo.setEditable(True)
        self._mm_model_combo.hide()

        self._mm_base_url_input = QLineEdit(self)
        self._mm_base_url_input.setPlaceholderText("Multimodal Base URL")
        self._mm_base_url_input.hide()

        self._extra_prompt = QPlainTextEdit(self)
        self._extra_prompt.setPlaceholderText(_t("extra_prompt_placeholder"))
        self._extra_prompt.setMaximumHeight(100)
        self._extra_prompt.hide()

    # ── UI ────────────────────────────────────────────────────
    def _setup_ui(self):
        # ── Init config widgets (needed before _load_settings) ──
        self._init_config_widgets()
        # Connect multimodal provider change to update model presets
        self._mm_provider_combo.currentIndexChanged.connect(self._update_mm_model_combo)
        # Initialize multimodal model combo with presets
        self._update_mm_model_combo()

        c = QWidget()
        self.setCentralWidget(c)
        ml = QVBoxLayout(c)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(0)

        # ── Toolbar (compact, VS Code-style) ──
        tb = QToolBar()
        tb.setMovable(False)
        tb.setIconSize(QSize(14, 14))
        tb.setStyleSheet(
            "QToolBar{background:#161b22;border-bottom:1px solid #21262d;padding:2px 6px;spacing:4px;}"
        )
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, tb)

        def _tb_btn(text, tip, callback):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.clicked.connect(lambda: callback())
            b.setStyleSheet(
                "QToolButton{color:#c9d1d9;padding:4px 10px;border-radius:4px;font-size:12px;}"
                "QToolButton:hover{background:#30363d;}"
            )
            return b

        tb.addWidget(_tb_btn(_t("btn_new"), _t("tip_new"), self._new_session))
        self._btn_cancel = _tb_btn(_t("btn_stop"), _t("tip_stop"), self._cancel_task)
        self._btn_cancel.setEnabled(False)
        tb.addWidget(self._btn_cancel)
        tb.addWidget(_tb_btn(_t("btn_clear"), _t("tip_clear"), self._clear_output))
        tb.addSeparator()
        tb.addWidget(_tb_btn(_t("btn_dir"), _t("tip_dir"), self._browse_workspace))
        tb.addWidget(_tb_btn(_t("btn_export"), _t("tip_export"), self._save_output))
        self._btn_rollback = _tb_btn(_t("btn_rollback"), _t("tip_rollback"), self._rollback_checkpoint)
        self._btn_rollback.setEnabled(False)
        tb.addWidget(self._btn_rollback)
        tb.addSeparator()
        tb.addWidget(_tb_btn(_t("btn_git_save"), _t("tip_git_save"), self._git_save))
        tb.addWidget(_tb_btn(_t("btn_git_push"), _t("tip_git_push"), self._git_push))
        self._git_branch_label = QLabel("")
        self._git_branch_label.setStyleSheet("color:#58a6ff;font-size:11px;padding:0 6px;")
        self._git_branch_label.setVisible(False)
        tb.addWidget(self._git_branch_label)
        tb.addSeparator()

        tb.addWidget(_tb_btn("📖", "新建小说", self._novel_new_book))

        sp = QWidget()
        sp.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(sp)

        lbl = QLabel("Provider:")
        lbl.setStyleSheet("color:#8b949e;font-size:12px;padding:0 4px;")
        tb.addWidget(lbl)
        self._provider_combo = QComboBox()
        self._provider_combo.setFixedWidth(130)
        for k, cfg in PROVIDER_CONFIGS.items():
            self._provider_combo.addItem(cfg["label"], k)
        self._provider_combo.currentIndexChanged.connect(self._update_provider_fields)
        tb.addWidget(self._provider_combo)

        tb.addSeparator()
        tb.addWidget(_tb_btn(_t("btn_settings"), _t("tip_settings"), self._open_settings))

        # ── Main splitter: Sidebar | Center | Preview ──
        main_sp = QSplitter(Qt.Orientation.Horizontal)
        main_sp.setHandleWidth(2)

        # ════════════════════════════════════════
        #  LEFT SIDEBAR — Sessions + Files
        # ════════════════════════════════════════
        sidebar = QWidget()
        sl = QVBoxLayout(sidebar)
        sl.setContentsMargins(4, 4, 0, 4)
        sl.setSpacing(4)

        sidebar_tabs = QTabWidget()
        sidebar_tabs.setDocumentMode(True)
        sidebar_tabs.setStyleSheet(
            "QTabWidget::pane{border:1px solid #21262d;border-radius:4px;background:#0d1117;}"
            "QTabBar::tab{background:#161b22;color:#8b949e;border:1px solid #21262d;"
            "border-bottom:none;padding:6px 16px;font-size:11px;}"
            "QTabBar::tab:selected{background:#0d1117;color:#f0f6fc;border-bottom:2px solid #f78166;}"
        )

        # ── Sessions tab ──
        sess_tab = QWidget()
        sess_lay = QVBoxLayout(sess_tab)
        sess_lay.setContentsMargins(4, 4, 4, 4)
        sess_lay.setSpacing(4)
        self._session_list = QListWidget()
        self._session_list.setStyleSheet(
            "QListWidget{background:#0d1117;border:1px solid #21262d;border-radius:4px;color:#c9d1d9;}"
            "QListWidget::item{padding:6px 8px;border-bottom:1px solid #161b22;font-size:12px;}"
            "QListWidget::item:selected{background:#1f6feb;color:#fff;}"
        )
        self._session_list.itemClicked.connect(self._on_session_clicked)
        sess_lay.addWidget(self._session_list)
        sess_btn_row = QHBoxLayout()
        for text, tip, cb in [
            ("➕", _t("sidebar_new"), lambda: self._new_session()),
            ("✏️", _t("sidebar_rename"), lambda: self._rename_session()),
            ("🗑️", _t("sidebar_delete"), lambda: self._delete_session()),
        ]:
            b = QPushButton(text)
            b.setFixedSize(28, 24)
            b.setToolTip(tip)
            b.setStyleSheet(
                "QPushButton{background:#21262d;border:1px solid #30363d;border-radius:4px;"
                "color:#c9d1d9;font-size:11px;padding:0;}"
                "QPushButton:hover{background:#30363d;}"
            )
            b.clicked.connect(cb)
            sess_btn_row.addWidget(b)
        sess_btn_row.addStretch()
        sess_lay.addLayout(sess_btn_row)
        sidebar_tabs.addTab(sess_tab, _t("tab_sessions"))

        # ── Files tab ──
        files_tab = QWidget()
        fl = QVBoxLayout(files_tab)
        fl.setContentsMargins(4, 4, 4, 4)
        fl.setSpacing(4)
        ws_row = QHBoxLayout()
        self._ws_input = QLineEdit()
        self._ws_input.setPlaceholderText(_t("workspace_placeholder"))
        self._ws_input.setStyleSheet("font-size:11px;padding:4px 8px;")
        ws_row.addWidget(self._ws_input)
        ws_btn = QPushButton("📂")
        ws_btn.setFixedSize(28, 24)
        ws_btn.setToolTip(_t("tip_select_dir"))
        ws_btn.setStyleSheet(
            "QPushButton{background:#21262d;border:1px solid #30363d;border-radius:4px;"
            "color:#c9d1d9;font-size:11px;padding:0;}"
            "QPushButton:hover{background:#30363d;}"
        )
        ws_btn.clicked.connect(self._browse_workspace)
        ws_row.addWidget(ws_btn)
        fl.addLayout(ws_row)
        self._file_browser = FileBrowserWidget()
        self._file_browser.setStyleSheet(
            "FileBrowserWidget{background:#0d1117;border:1px solid #21262d;border-radius:4px;color:#c9d1d9;}"
        )
        self._file_browser.file_selected.connect(self._on_file_selected)
        fl.addWidget(self._file_browser)
        sidebar_tabs.addTab(files_tab, "📂 文件")

        # ── Novel tab ──
        self._novel_sidebar = NovelSidebarPanel()
        self._novel_sidebar.novel_action.connect(self._on_novel_action)
        sidebar_tabs.addTab(self._novel_sidebar, "📖 小说")

        sl.addWidget(sidebar_tabs)

        # ════════════════════════════════════════
        #  CENTER AREA — Chat + Input (VSCode-style)
        # ════════════════════════════════════════
        center = QWidget()
        center_lay = QVBoxLayout(center)
        center_lay.setContentsMargins(0, 0, 0, 0)
        center_lay.setSpacing(0)

        # ── Chat output ──
        self._output = QTextBrowser()
        self._output.setReadOnly(True)
        self._output.setOpenLinks(False)
        self._output.setStyleSheet(
            "QTextBrowser{background:#0d1117;border:none;"
            "padding:16px 20px;color:#c9d1d9;font-size:13px;}"
        )
        self._output.anchorClicked.connect(self._on_thinking_link)
        self._thinking_bubbles: dict[str, str] = {}  # id → full text
        self._highlighter = OutputHighlighter(self._output.document())
        center_lay.addWidget(self._output, 1)

        # ── Input area (compact, VSCode-style bottom bar) ──
        input_frame = QFrame()
        input_frame.setStyleSheet(
            "QFrame{background:#161b22;border-top:1px solid #21262d;}"
        )
        input_outer_lay = QVBoxLayout(input_frame)
        input_outer_lay.setContentsMargins(12, 4, 12, 4)
        input_outer_lay.setSpacing(4)

        self._attachment_bar = QWidget()
        self._attachment_bar.setStyleSheet("background:transparent;")
        self._attachment_lay = QHBoxLayout(self._attachment_bar)
        self._attachment_lay.setContentsMargins(0, 0, 0, 0)
        self._attachment_lay.setSpacing(4)
        self._attachment_bar.hide()
        input_outer_lay.addWidget(self._attachment_bar)

        self._attachments: list[dict[str, str]] = []
        self._conversation_history: list = []  # accumulated messages across tasks

        input_lay = QHBoxLayout()
        input_lay.setSpacing(8)

        self._attach_btn = QPushButton("📎")
        self._attach_btn.setToolTip(_t("tip_attach"))
        self._attach_btn.setFixedSize(36, 36)
        self._attach_btn.setStyleSheet(
            "QPushButton{background:#21262d;border:1px solid #30363d;border-radius:6px;"
            "color:#8b949e;font-size:16px;}"
            "QPushButton:hover{background:#30363d;color:#c9d1d9;}"
        )
        self._attach_btn.clicked.connect(self._pick_attachment)
        input_lay.addWidget(self._attach_btn)

        self._task_input = QPlainTextEdit()
        self._task_input.setPlaceholderText(
            _t("input_placeholder")
        )
        self._task_input.setMaximumHeight(100)
        self._task_input.setMinimumHeight(36)
        self._task_input.setStyleSheet(
            "QPlainTextEdit{background:#0d1117;border:1px solid #30363d;"
            "border-radius:6px;padding:6px 10px;color:#c9d1d9;font-size:13px;}"
            "QPlainTextEdit:focus{border:1px solid #58a6ff;outline:none;}"
        )
        self._task_input.textChanged.connect(self._auto_resize_input)
        input_lay.addWidget(self._task_input, 1)

        # Button column: Stop/Run toggle + Batch
        btn_col = QVBoxLayout()
        btn_col.setSpacing(4)
        btn_col.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self._batch_btn = QPushButton(_t("btn_batch"))
        self._batch_btn.setToolTip(_t("tip_batch"))
        self._batch_btn.setFixedHeight(28)
        self._batch_btn.setStyleSheet(
            "QPushButton{background:#21262d;border:1px solid #30363d;border-radius:4px;"
            "color:#8b949e;font-size:11px;padding:2px 8px;}"
            "QPushButton:hover{background:#30363d;color:#c9d1d9;}"
        )
        self._batch_btn.clicked.connect(self._run_batch)
        btn_col.addWidget(self._batch_btn)

        self._btn_run = QPushButton(_t("btn_send"))
        self._btn_run.setToolTip(_t("tip_send"))
        self._btn_run.setFixedHeight(34)
        self._btn_run.setStyleSheet(
            "QPushButton{background:#238636;color:#fff;border:none;"
            "border-radius:6px;font-size:13px;font-weight:bold;padding:4px 14px;}"
            "QPushButton:hover{background:#2ea043;}"
            "QPushButton:disabled{background:#21262d;color:#484f58;}"
        )
        self._btn_run.clicked.connect(self._run_task)
        btn_col.addWidget(self._btn_run)

        # Stop button (hidden by default, shown during execution)
        self._btn_stop = QPushButton(_t("btn_stop"))
        self._btn_stop.setToolTip(_t("tip_stop"))
        self._btn_stop.setFixedHeight(34)
        self._btn_stop.setStyleSheet(
            "QPushButton{background:#da3633;color:#fff;border:none;"
            "border-radius:6px;font-size:13px;font-weight:bold;padding:4px 14px;}"
            "QPushButton:hover{background:#f85149;}"
        )
        self._btn_stop.clicked.connect(self._cancel_task)
        self._btn_stop.setVisible(False)
        btn_col.addWidget(self._btn_stop)

        input_lay.addLayout(btn_col)

        input_outer_lay.addLayout(input_lay)

        self._task_input.installEventFilter(self)

        center_lay.addWidget(input_frame)

        # ════════════════════════════════════════
        #  RIGHT PANEL — Preview + Tool Calls (VSCode-style side panel)
        # ════════════════════════════════════════
        right_panel = QWidget()
        right_panel.setMinimumWidth(280)
        right_panel.setMaximumWidth(600)
        right_lay = QVBoxLayout(right_panel)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(0)

        right_tabs = QTabWidget()
        right_tabs.setDocumentMode(True)
        right_tabs.setStyleSheet(
            "QTabWidget::pane{border:none;background:#0d1117;}"
            "QTabBar::tab{background:#161b22;color:#8b949e;border:none;"
            "border-bottom:2px solid transparent;padding:8px 14px;font-size:11px;}"
            "QTabBar::tab:selected{color:#f0f6fc;border-bottom:2px solid #f78166;}"
        )

        # ── Preview tab (file content viewer) ──
        preview_tab = QWidget()
        preview_lay = QVBoxLayout(preview_tab)
        preview_lay.setContentsMargins(4, 4, 4, 4)
        preview_lay.setSpacing(4)

        self._preview_path = QLabel(_t("preview_click"))
        self._preview_path.setStyleSheet(
            "color:#8b949e;font-size:11px;padding:4px 8px;"
            "background:#161b22;border-radius:4px;"
        )
        preview_lay.addWidget(self._preview_path)

        self._preview_image_scroll = QScrollArea()
        self._preview_image_scroll.setWidgetResizable(True)
        self._preview_image_scroll.setStyleSheet(
            "QScrollArea{background:#0d1117;border:1px solid #21262d;border-radius:4px;}"
        )
        self._preview_image_label = QLabel()
        self._preview_image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_image_label.setStyleSheet("background:#0d1117;")
        self._preview_image_scroll.setWidget(self._preview_image_label)
        self._preview_image_scroll.hide()
        preview_lay.addWidget(self._preview_image_scroll)

        self._preview_output = QTextBrowser()
        self._preview_output.setReadOnly(True)
        self._preview_output.setStyleSheet(
            "QTextBrowser{background:#0d1117;border:1px solid #21262d;border-radius:4px;"
            "font-family:'Consolas','Courier New',monospace;font-size:12px;"
            "color:#c9d1d9;padding:8px;}"
        )
        preview_lay.addWidget(self._preview_output)

        right_tabs.addTab(preview_tab, _t("tab_preview"))

        # ── Tool Calls tab ──
        tool_tab = QWidget()
        tool_lay = QVBoxLayout(tool_tab)
        tool_lay.setContentsMargins(0, 0, 0, 0)
        self._tool_panel = ToolCallPanel()
        tool_lay.addWidget(self._tool_panel)
        right_tabs.addTab(tool_tab, _t("tab_tools"))

        # ── Novel tab ──
        self._novel_right = NovelRightPanel()
        self._novel_right.novel_action.connect(self._on_novel_action)
        right_tabs.addTab(self._novel_right, "📖 小说")

        right_lay.addWidget(right_tabs)

        # ── Assemble splitter ──
        main_sp.addWidget(sidebar)
        main_sp.addWidget(center)
        main_sp.addWidget(right_panel)
        main_sp.setSizes([220, 600, 320])
        main_sp.setStretchFactor(0, 0)
        main_sp.setStretchFactor(1, 1)
        main_sp.setStretchFactor(2, 0)

        ml.addWidget(main_sp)

        # Sync: workspace change → 刷新文件浏览器 + 会话列表
        self._ws_input.textChanged.connect(
            lambda: self._file_browser.set_root(self._ws_input.text())
        )
        self._ws_input.editingFinished.connect(
            lambda: self._refresh_session_list()
        )
        self._ws_input.textChanged.connect(
            lambda: self._novel_sidebar.set_workspace(self._ws_input.text())
        )
        self._ws_input.textChanged.connect(
            lambda: self._on_workspace_changed()
        )

    # ── Settings Dialog ───────────────────────────────────────
    def _open_settings(self):
        """Open a settings dialog (like VS Code's settings panel)."""
        dlg = QDialog(self)
        dlg.setWindowTitle(_t("settings_title"))
        dlg.setMinimumSize(520, 520)
        dlg.setStyleSheet("""
            QDialog{background:#0d1117;}
            QComboBox{background:#161b22;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;padding:4px 8px;}
            QComboBox::drop-down{border:none;width:24px;}
            QComboBox::down-arrow{image:none;border-left:5px solid transparent;border-right:5px solid transparent;border-top:6px solid #8b949e;}
            QComboBox QAbstractItemView{background:#161b22;color:#c9d1d9;border:1px solid #30363d;outline:none;padding:2px;}
            QComboBox QAbstractItemView::item{padding:4px 8px;}
            QComboBox QAbstractItemView::item:selected{background:#1f6feb;color:#fff;}
        """)

        layout = QVBoxLayout(dlg)
        layout.setSpacing(12)

        title = QLabel(_t("settings_heading"))
        title.setStyleSheet("color:#f0f6fc;font-size:16px;font-weight:bold;padding:4px 0;")
        layout.addWidget(title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")

        sw = QWidget()
        sw.setStyleSheet("background:transparent;")
        form = QVBoxLayout(sw)
        form.setSpacing(16)

        # ── LLM ──
        llm_g = QGroupBox("LLM 配置")
        llm_g.setStyleSheet(
            "QGroupBox{font-size:13px;font-weight:600;color:#58a6ff;border:1px solid #30363d;"
            "border-radius:8px;margin-top:16px;padding:16px 12px 12px;}"
            "QGroupBox::title{subcontrol-origin:margin;left:14px;padding:0 6px;}"
        )
        lf = QFormLayout(llm_g)
        lf.setSpacing(8)
        # Use local widgets, sync with member widgets on accept
        _api_key = QLineEdit()
        _api_key.setEchoMode(QLineEdit.EchoMode.Password)
        _api_key.setPlaceholderText("API Key...")
        _api_key.setText(self._api_key_input.text())
        lf.addRow(_t("settings_api_key"), _api_key)

        _model = QComboBox()
        _model.setEditable(True)
        for i in range(self._model_combo.count()):
            _model.addItem(self._model_combo.itemText(i), self._model_combo.itemData(i))
        _model.setCurrentText(self._model_combo.currentText())
        lf.addRow(_t("settings_model"), _model)

        _base_url = QLineEdit()
        _base_url.setPlaceholderText("API Base URL (如需要)")
        _base_url.setText(self._base_url_input.text())
        lf.addRow(_t("settings_base_url"), _base_url)

        _show_key = QCheckBox(_t("settings_show_key"))
        _show_key.toggled.connect(
            lambda chk: _api_key.setEchoMode(
                QLineEdit.EchoMode.Normal if chk else QLineEdit.EchoMode.Password
            )
        )
        lf.addRow("", _show_key)

        _thinking_mode = QCheckBox("🧠 思考模式 (Reasoning)")
        _thinking_mode.setChecked(self._thinking_mode_cb.isChecked())
        _thinking_mode.setToolTip("启用后使用推理模型（如 DeepSeek-R1 / OpenAI o1），响应更慢但推理更强")
        lf.addRow("", _thinking_mode)

        form.addWidget(llm_g)

        # ── Multimodal LLM ──
        mm_g = QGroupBox("多模态模型 (图片识别)")
        mm_g.setStyleSheet(llm_g.styleSheet())
        mm_lay = QFormLayout(mm_g)
        mm_lay.setSpacing(8)
        mm_lay.addRow(QLabel("主模型不支持图片时自动切换到此模型"))

        _mm_enable = QCheckBox("启用独立多模态模型")
        _mm_enable.setChecked(self._mm_enable_cb.isChecked())
        mm_lay.addRow("", _mm_enable)

        _mm_provider = QComboBox()
        for k, cfg in PROVIDER_CONFIGS.items():
            _mm_provider.addItem(cfg["label"], k)
        idx = _mm_provider.findData(self._mm_provider_combo.currentData())
        if idx >= 0:
            _mm_provider.setCurrentIndex(idx)
        mm_lay.addRow("Provider", _mm_provider)

        _mm_api_key = QLineEdit()
        _mm_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        _mm_api_key.setPlaceholderText("API Key (留空则复用主模型 Key)")
        _mm_api_key.setText(self._mm_api_key_input.text())
        mm_lay.addRow("API Key", _mm_api_key)

        _mm_model = QComboBox()
        _mm_model.setEditable(True)
        mm_provider_key = _mm_provider.currentData()
        for m in self._MM_MODEL_PRESETS.get(mm_provider_key, []):
            _mm_model.addItem(m)
        _mm_model.setCurrentText(self._mm_model_combo.currentText())
        mm_lay.addRow("模型", _mm_model)
        # Auto-update model list when provider changes
        def _update_mm_combo(prov, model_combo):
            model_combo.clear()
            pk = prov.currentData()
            for m in self._MM_MODEL_PRESETS.get(pk, []):
                model_combo.addItem(m)
        _mm_provider.currentIndexChanged.connect(
            lambda: _update_mm_combo(_mm_provider, _mm_model)
        )

        _mm_base_url = QLineEdit()
        _mm_base_url.setPlaceholderText("Base URL（可选，默认使用 Provider 地址）")
        _mm_base_url.setText(self._mm_base_url_input.text())
        mm_lay.addRow("Base URL", _mm_base_url)

        _mm_show_key = QCheckBox(_t("settings_show_key"))
        _mm_show_key.toggled.connect(
            lambda chk: _mm_api_key.setEchoMode(
                QLineEdit.EchoMode.Normal if chk else QLineEdit.EchoMode.Password
            )
        )
        mm_lay.addRow("", _mm_show_key)

        form.addWidget(mm_g)

        # ── Advanced ──
        ad_g = QGroupBox(_t("settings_advanced"))
        ad_g.setStyleSheet(llm_g.styleSheet())
        af = QFormLayout(ad_g)
        af.setSpacing(6)

        _rounds = QSpinBox()
        _rounds.setRange(5, 100)
        _rounds.setValue(self._max_rounds_spin.value())
        af.addRow(_t("settings_max_rounds"), _rounds)

        _auto_allow = QCheckBox(_t("settings_auto_allow"))
        _auto_allow.setChecked(self._auto_allow_cb.isChecked())
        af.addRow("", _auto_allow)

        _auto_inject = QCheckBox(_t("settings_auto_inject"))
        _auto_inject.setChecked(self._auto_inject_cb.isChecked())
        af.addRow("", _auto_inject)

        _test_verify = QCheckBox(_t("settings_test_verify"))
        _test_verify.setChecked(self._test_verify_cb.isChecked())
        af.addRow("", _test_verify)

        _git_commit = QCheckBox(_t("settings_git_commit"))
        _git_commit.setChecked(self._git_commit_cb.isChecked())
        af.addRow("", _git_commit)

        _plan_mode = QCheckBox(_t("settings_plan_mode"))
        _plan_mode.setChecked(self._plan_mode_cb.isChecked())
        af.addRow("", _plan_mode)

        _lang = QComboBox()
        _lang.addItem("中文", "zh")
        _lang.addItem("English", "en")
        current_lang = get_language()
        idx = _lang.findData(current_lang)
        if idx >= 0:
            _lang.setCurrentIndex(idx)
        af.addRow(_t("settings_language"), _lang)
        form.addWidget(ad_g)

        # ── Extra Prompt ──
        ex_g = QGroupBox(_t("settings_extra_prompt"))
        ex_g.setStyleSheet(llm_g.styleSheet())
        ex_lay = QVBoxLayout(ex_g)
        _extra = QPlainTextEdit()
        _extra.setPlaceholderText(_t("extra_prompt_placeholder"))
        _extra.setMaximumHeight(100)
        _extra.setPlainText(self._extra_prompt.toPlainText())
        ex_lay.addWidget(_extra)
        form.addWidget(ex_g)

        # ── GitHub ──
        gh_g = QGroupBox(_t("settings_github"))
        gh_g.setStyleSheet(llm_g.styleSheet())
        gh_lay = QFormLayout(gh_g)
        gh_lay.setSpacing(8)

        _gh_username = QLineEdit()
        _gh_username.setPlaceholderText("your-name")
        saved_username = self._settings.value("github_username", "")
        _gh_username.setText(saved_username)
        gh_lay.addRow(_t("settings_github_username"), _gh_username)

        _gh_email = QLineEdit()
        _gh_email.setPlaceholderText("you@example.com")
        saved_email = self._settings.value("github_email", "")
        _gh_email.setText(saved_email)
        gh_lay.addRow(_t("settings_github_email"), _gh_email)

        _gh_token = QLineEdit()
        _gh_token.setEchoMode(QLineEdit.EchoMode.Password)
        _gh_token.setPlaceholderText("ghp_xxxxxxxxxxxx")
        saved_token = self._settings.value("github_token", "")
        _gh_token.setText(saved_token)
        gh_lay.addRow(_t("settings_github_token"), _gh_token)

        _gh_show_token = QCheckBox(_t("settings_show_key"))
        _gh_show_token.toggled.connect(
            lambda chk: _gh_token.setEchoMode(
                QLineEdit.EchoMode.Normal if chk else QLineEdit.EchoMode.Password
            )
        )
        gh_lay.addRow("", _gh_show_token)

        _gh_token_hint = QLabel(_t("settings_github_token_hint"))
        _gh_token_hint.setStyleSheet("color:#8b949e;font-size:11px;")
        _gh_token_hint.setWordWrap(True)
        gh_lay.addRow("", _gh_token_hint)
        form.addWidget(gh_g)

        form.addStretch()
        scroll.setWidget(sw)
        layout.addWidget(scroll, 1)

        # Buttons
        btn_bar = QHBoxLayout()
        btn_bar.addStretch()
        cancel_btn = QPushButton(_t("btn_cancel"))
        cancel_btn.setStyleSheet(
            "QPushButton{background:#21262d;color:#c9d1d9;border:1px solid #30363d;"
            "border-radius:6px;padding:8px 20px;font-size:13px;}"
            "QPushButton:hover{background:#30363d;}"
        )
        cancel_btn.clicked.connect(dlg.reject)
        btn_bar.addWidget(cancel_btn)

        ok_btn = QPushButton(_t("btn_save"))
        ok_btn.setStyleSheet(
            "QPushButton{background:#238636;color:#fff;border-radius:6px;padding:8px 24px;"
            "font-size:13px;font-weight:600;}"
            "QPushButton:hover{background:#2ea043;}"
        )
        ok_btn.clicked.connect(dlg.accept)
        btn_bar.addWidget(ok_btn)
        layout.addLayout(btn_bar)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            # Sync back to member widgets
            self._api_key_input.setText(_api_key.text())
            self._model_combo.clear()
            for i in range(_model.count()):
                self._model_combo.addItem(_model.itemText(i), _model.itemData(i))
            self._model_combo.setCurrentText(_model.currentText())
            self._base_url_input.setText(_base_url.text())
            self._thinking_mode_cb.setChecked(_thinking_mode.isChecked())
            self._mm_enable_cb.setChecked(_mm_enable.isChecked())
            self._mm_provider_combo.setCurrentIndex(_mm_provider.currentIndex())
            self._mm_api_key_input.setText(_mm_api_key.text())
            self._mm_model_combo.setCurrentText(_mm_model.currentText())
            self._mm_base_url_input.setText(_mm_base_url.text())
            self._max_rounds_spin.setValue(_rounds.value())
            self._auto_allow_cb.setChecked(_auto_allow.isChecked())
            self._auto_inject_cb.setChecked(_auto_inject.isChecked())
            self._test_verify_cb.setChecked(_test_verify.isChecked())
            self._git_commit_cb.setChecked(_git_commit.isChecked())
            self._plan_mode_cb.setChecked(_plan_mode.isChecked())
            self._extra_prompt.setPlainText(_extra.toPlainText())
            self._lang_combo.setCurrentIndex(_lang.currentIndex())
            self._save_settings()
            self._settings.setValue("github_token", _gh_token.text())
            self._settings.setValue("github_username", _gh_username.text())
            self._settings.setValue("github_email", _gh_email.text())
            # Save multimodal settings
            self._settings.setValue("mm_enable", _mm_enable.isChecked())
            self._settings.setValue("mm_provider", _mm_provider.currentData())
            self._settings.setValue("mm_api_key", _mm_api_key.text())
            self._settings.setValue("mm_model", _mm_model.currentText())
            self._settings.setValue("mm_base_url", _mm_base_url.text())
            self._update_provider_fields()
            self._sync_env_file()
            # Apply language change
            new_lang = _lang.currentData()
            if new_lang != get_language():
                set_language(new_lang)
                QMessageBox.information(
                    dlg, _t("settings_heading"),
                    "语言已切换，重启应用后完全生效。\nLanguage changed. Restart to apply fully."
                )

    def _sync_env_file(self):
        """Sync desktop settings back to .env file so CLI also picks them up."""
        env_path = Path(__file__).resolve().parent.parent / ".env"
        provider = self._provider_combo.currentData()

        # Map provider to env keys
        api_key_map = {
            "deepseek": "DEEPSEEK_API_KEY",
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "ollama": "OLLAMA_API_KEY",
        }
        model_map = {
            "deepseek": "DEEPSEEK_MODEL",
            "openai": "OPENAI_MODEL",
            "anthropic": "ANTHROPIC_MODEL",
            "ollama": "OLLAMA_MODEL",
        }
        base_url_map = {
            "deepseek": "DEEPSEEK_BASE_URL",
            "ollama": "OLLAMA_BASE_URL",
        }

        updates = {
            "LLM_PROVIDER": provider,
            "GANGGE_LANG": self._lang_combo.currentData(),
            api_key_map.get(provider, ""): self._api_key_input.text(),
            model_map.get(provider, ""): self._model_combo.currentText(),
        }
        if provider in base_url_map and self._base_url_input.text().strip():
            updates[base_url_map[provider]] = self._base_url_input.text().strip()

        # ── Multimodal LLM settings ──
        if self._mm_enable_cb.isChecked():
            mm_provider = self._mm_provider_combo.currentData()
            mm_model = self._mm_model_combo.currentText().strip()
            mm_key = self._mm_api_key_input.text().strip()
            mm_url = self._mm_base_url_input.text().strip()
            if mm_model:
                updates["MM_ENABLED"] = "true"
                updates["MM_PROVIDER"] = mm_provider
                updates["MM_MODEL"] = mm_model
                if mm_key:
                    updates["MM_API_KEY"] = mm_key
                if mm_url:
                    updates["MM_BASE_URL"] = mm_url

        # ── Thinking mode ──
        if self._thinking_mode_cb.isChecked():
            updates["THINKING_MODE"] = "true"

        try:
            # Read existing .env
            lines = []
            if env_path.exists():
                lines = env_path.read_text(encoding="utf-8").splitlines()

            # Update or append
            updated_keys = set()
            new_lines = []
            for line in lines:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    new_lines.append(line)
                    continue
                if "=" in stripped:
                    key = stripped.split("=", 1)[0].strip()
                    if key in updates:
                        new_lines.append(f"{key}={updates[key]}")
                        updated_keys.add(key)
                    else:
                        new_lines.append(line)
                else:
                    new_lines.append(line)

            # Add missing keys
            for key, val in updates.items():
                if key and key not in updated_keys:
                    new_lines.append(f"{key}={val}")

            env_path.write_text("\n".join(new_lines), encoding="utf-8")
        except Exception:
            pass  # .env is optional, don't crash if it fails

    # ── Events ────────────────────────────────────────────────
    def closeEvent(self, event):
        self._cancel_task()
        self._save_settings()
        self._db.close()
        event.accept()

    # ── Session ───────────────────────────────────────────────
    def _new_session(self):
        self._clear_output()
        self._tool_panel.clear_entries()
        ws = self._ws_input.text().strip()
        sid = self._db.create_session(_t("session_new"), ws)
        self._current_session_id = sid
        self._refresh_session_list()
        # Select the new session in the list
        for i in range(self._session_list.count()):
            item = self._session_list.item(i)
            if item and item.data(Qt.ItemDataRole.UserRole) == sid:
                self._session_list.setCurrentItem(item)
                break
        self._status_label.setText(_t("status_new_session", sid=sid))

    def _refresh_session_list(self):
        self._session_list.clear()
        # 按当前工作目录过滤，只显示属于该目录的会话
        current_ws = self._ws_input.text().strip()
        sessions = self._db.list_sessions(50, workspace=current_ws)
        for s in sessions:
            ws_tag = " 📁" if s["workspace"] else ""
            label = f"{s['title'][:30]}  ({s['updated_at'][:16]}){ws_tag}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, s["id"])
            item.setData(Qt.ItemDataRole.UserRole + 1, s["title"])
            self._session_list.addItem(item)

        # 如果当前工作目录有会话，在列表底部加分隔提示
        if current_ws and sessions:
            sep = QListWidgetItem(_t("session_count", n=len(sessions)))
            sep.setFlags(sep.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            sep.setForeground(QColor("#666666"))
            self._session_list.addItem(sep)

    def _on_session_clicked(self, item):
        sid = item.data(Qt.ItemDataRole.UserRole)
        if not sid or sid == self._current_session_id:
            return

        # Save current session messages
        self._save_current_session_messages()

        # Load new session
        self._current_session_id = sid
        self._clear_output()

        # ── CHANGE: 方案C — load_turns 重建对话历史 ──────────
        turn_msgs = self._db.load_turns(sid, limit=self._db.MAX_LOAD_MESSAGES)
        total = self._db.count_messages(sid)

        if total > self._db.MAX_LOAD_MESSAGES:
            hint = _t("session_load_with_hint", total=total, max=self._db.MAX_LOAD_MESSAGES)
            self._append_output(_t("session_load", name=item.text()) + f"\n{hint}\n", "system")
        else:
            self._append_output(_t("session_load_count", name=item.text(), total=total) + "\n", "system")
        self._append_output("─" * 60 + "\n", "system")

        # 渲染消息 — tool 角色只显示 80 字符摘要
        for msg in turn_msgs:
            role = msg["role"]
            content = msg["content"]
            if role == "user":
                self._append_output(f"👤 {content}", "user")
            elif role == "assistant" and isinstance(content, list):
                for block in content:
                    if block.get("type") == "text" and block.get("text"):
                        self._append_output(block["text"], "assistant")
                    elif block.get("type") == "tool_use":
                        name = block.get("name", "?")
                        self._append_output(f"  ▶ {name}(...)", "tool")
            elif role == "assistant":
                self._append_output(str(content), "assistant")
            elif role == "tool":
                summary = str(content)[:80].replace("\n", " ")
                is_error = msg.get("is_error", False)
                icon = "✗" if is_error else "✓"
                tid = (msg.get("tool_use_id", "") or "")[:8]
                self._append_output(f"    {icon} [{tid}] {summary}...", "tool")

        # Load tool calls
        self._tool_panel.clear_entries()
        calls = self._db.load_tool_calls(sid)
        for c in calls:
            self._tool_panel.add_entry(c["tool_name"], c["output"][:200], c["is_error"], c["diff"])

        # Restore session workspace if set
        sess = self._db.get_session(sid)
        if sess and sess["workspace"]:
            old_ws = self._ws_input.text().strip()
            if sess["workspace"] != old_ws:
                self._ws_input.setText(sess["workspace"])
                self._file_browser.set_root(sess["workspace"])
                self._refresh_session_list()  # 工作目录变了 → 刷新会话列表

        self._status_label.setText(_t("status_session", title=sess['title'] if sess else sid))

    def _delete_session(self):
        item = self._session_list.currentItem()
        if not item:
            return
        sid = item.data(Qt.ItemDataRole.UserRole)
        if QMessageBox.question(self, _t("session_confirm_delete"), _t("session_delete_msg", name=item.text()), QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self._db.delete_session(sid)
            if self._current_session_id == sid:
                self._current_session_id = ""
                self._clear_output()
                self._tool_panel.clear_entries()
            self._refresh_session_list()

    def _rename_session(self):
        item = self._session_list.currentItem()
        if not item:
            return
        sid = item.data(Qt.ItemDataRole.UserRole)
        title = item.data(Qt.ItemDataRole.UserRole + 1)
        new_title, ok = QInputDialog.getText(self, _t("session_rename"), _t("session_rename_label"), text=title)
        if ok and new_title:
            self._db.update_session(sid, title=new_title)
            self._refresh_session_list()

    def _save_current_session_messages(self):
        """Save current output to session DB."""
        if not self._current_session_id:
            return
        # We already save incrementally in _append_output, so this is a no-op
        # But we update the session timestamp
        self._db.update_session(self._current_session_id, updated_at=datetime.now().isoformat())

    # ── Attachment handling ──────────────────────────────────
    def _pick_attachment(self):
        file_filter = "Media Files (*.png *.jpg *.jpeg *.gif *.bmp *.webp *.mp4 *.avi *.mov *.mkv *.webm *.mp3 *.wav *.ogg *.flac *.aac *.m4a);;Images (*.png *.jpg *.jpeg *.gif *.bmp *.webp);;Video (*.mp4 *.avi *.mov *.mkv *.webm);;Audio (*.mp3 *.wav *.ogg *.flac *.aac *.m4a);;All Files (*)"
        paths, _ = QFileDialog.getOpenFileNames(self, _t("attach_select"), "", file_filter)
        for path in paths:
            self._add_attachment(path)

    def _add_attachment(self, file_path: str):
        p = Path(file_path)
        if not p.exists():
            return
        ext = p.suffix.lower()
        image_exts = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
        video_exts = {".mp4", ".avi", ".mov", ".mkv", ".webm"}
        audio_exts = {".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a"}
        if ext in image_exts:
            media_type = f"image/{ext.lstrip('.')}"
            if ext == ".jpg":
                media_type = "image/jpeg"
            icon = "🖼️"
        elif ext in video_exts:
            media_type = f"video/{ext.lstrip('.')}"
            icon = "🎬"
        elif ext in audio_exts:
            media_type = f"audio/{ext.lstrip('.')}"
            if ext == ".mp3":
                media_type = "audio/mpeg"
            icon = "🎵"
        else:
            return
        size_kb = p.stat().st_size / 1024
        if size_kb > 20480:
            QMessageBox.warning(self, _t("attach_too_large_title"), _t("attach_too_large_msg", size=f"{size_kb:.0f}"))
            return
        import base64
        with open(file_path, "rb") as f:
            b64_data = base64.b64encode(f.read()).decode("ascii")
        att = {"path": file_path, "name": p.name, "media_type": media_type, "data": b64_data, "icon": icon}
        self._attachments.append(att)
        self._refresh_attachment_bar()

    def _remove_attachment(self, index: int):
        if 0 <= index < len(self._attachments):
            self._attachments.pop(index)
            self._refresh_attachment_bar()

    def _refresh_attachment_bar(self):
        while self._attachment_lay.count():
            item = self._attachment_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not self._attachments:
            self._attachment_bar.hide()
            return
        self._attachment_bar.show()
        for i, att in enumerate(self._attachments):
            chip = QFrame()
            chip.setStyleSheet(
                "QFrame{background:#21262d;border:1px solid #30363d;border-radius:4px;padding:2px 6px;}"
            )
            chip_lay = QHBoxLayout(chip)
            chip_lay.setContentsMargins(4, 2, 4, 2)
            chip_lay.setSpacing(4)
            label = QLabel(f"{att['icon']} {att['name']}")
            label.setStyleSheet("color:#c9d1d9;font-size:11px;background:transparent;border:none;")
            chip_lay.addWidget(label)
            remove_btn = QPushButton("✕")
            remove_btn.setFixedSize(18, 18)
            remove_btn.setStyleSheet(
                "QPushButton{background:transparent;border:none;color:#8b949e;font-size:11px;padding:0;}"
                "QPushButton:hover{color:#f85149;}"
            )
            idx = i
            remove_btn.clicked.connect(lambda _, x=idx: self._remove_attachment(x))
            chip_lay.addWidget(remove_btn)
            self._attachment_lay.addWidget(chip)
        self._attachment_lay.addStretch()

    # ── Input auto-resize & Enter handling ──────────────────
    def _auto_resize_input(self):
        doc = self._task_input.document()
        h = int(doc.size().height()) + 16
        h = max(36, min(h, 100))
        self._task_input.setFixedHeight(h)

    def eventFilter(self, obj, event):
        if obj == self._task_input and event.type() == event.Type.KeyPress:
            from PyQt6.QtCore import QEvent
            from PyQt6.QtGui import QKeyEvent
            ke = event
            # Shift+Enter = insert newline
            if ke.key() == Qt.Key.Key_Return and ke.modifiers() == Qt.KeyboardModifier.ShiftModifier:
                cursor = self._task_input.textCursor()
                cursor.insertText("\n")
                return True
            # Ctrl+Enter or plain Enter (single line) = run
            if ke.key() == Qt.Key.Key_Return:
                if ke.modifiers() == Qt.KeyboardModifier.ControlModifier:
                    self._run_task()
                    return True
                # If single line (no newline in text), Enter runs
                text = self._task_input.toPlainText()
                if "\n" not in text:
                    self._run_task()
                    return True
                # Multi-line: Enter inserts newline (like Shift+Enter)
                cursor = self._task_input.textCursor()
                cursor.insertText("\n")
                return True
        return super().eventFilter(obj, event)

    # ── Actions ───────────────────────────────────────────────
    def _clear_output(self):
        self._output.clear()

    def _rollback_checkpoint(self):
        from PyQt6.QtWidgets import QMessageBox
        workspace = self._ws_input.text().strip()
        if not workspace:
            self._status_label.setText(_t("status_no_workspace"))
            return
        from gangge.layer4_tools.shadow_git import ShadowGit
        sg = ShadowGit(workspace)
        if not sg.is_available():
            self._status_label.setText(_t("status_no_git"))
            return
        checkpoints = sg.list_checkpoints(limit=10)
        gangge_cps = [c for c in checkpoints if c.get("is_checkpoint")]
        if not gangge_cps:
            self._status_label.setText(_t("status_no_checkpoint"))
            return
        items = [f"{c['hash']} — {c['date'][:16]} {c['message']}" for c in gangge_cps[:8]]
        item, ok = QInputDialog.getItem(
            self, _t("rollback_title"), _t("rollback_select"),
            items, 0, False,
        )
        if not ok or not item:
            return
        selected_hash = item.split(" ")[0]
        reply = QMessageBox.warning(
            self, _t("rollback_confirm"),
            _t("rollback_confirm_msg", hash=selected_hash),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        if sg.rollback(selected_hash):
            self._append_output(_t("rollback_done", hash=selected_hash), "system")
            self._file_browser.set_root(workspace)
            self._status_label.setText(_t("status_rollback_ok_hash", hash=selected_hash))
        else:
            self._status_label.setText(_t("status_rollback_fail"))

    def _git_save(self):
        workspace = self._ws_input.text().strip()
        if not workspace:
            self._status_label.setText(_t("status_no_workspace"))
            return
        from gangge.layer4_tools.shadow_git import ShadowGit
        sg = ShadowGit(workspace)
        gh_user = self._settings.value("github_username", "")
        gh_email = self._settings.value("github_email", "")
        if not sg.is_available() and not sg.ensure_init(user_name=gh_user, user_email=gh_email):
            self._status_label.setText(_t("status_git_no_repo"))
            return
        message, ok = QInputDialog.getText(
            self, _t("git_save_title"),
            _t("git_save_prompt"),
            text="save: ",
        )
        if not ok:
            return
        result = sg.user_commit(message or "")
        if result["success"]:
            if result.get("files", 1) == 0:
                last_hash = result.get("last_hash", "")
                last_msg = result.get("last_message", "")
                if last_hash:
                    self._status_label.setText(_t("status_git_uptodate", hash=last_hash))
                    self._append_output(_t("git_save_uptodate", hash=last_hash, msg=last_msg), "system")
                else:
                    self._status_label.setText(_t("status_git_no_changes"))
            else:
                h = result.get("hash", "")
                n = result.get("files", 0)
                ins = result.get("insertions", 0)
                dels = result.get("deletions", 0)
                msg = result.get("message", "")
                file_details = result.get("file_details", [])
                ns_map = result.get("name_status", {})

                self._status_label.setText(_t("status_git_saved", hash=h, files=n))

                status_icons = {"A": "🆕", "M": "✏️", "D": "🗑️", "R": "📦"}
                tree_lines = [msg]
                for i, fd in enumerate(file_details):
                    fname = fd["file"]
                    stat = fd["stat"]
                    ns = ns_map.get(fname, "M")
                    icon = status_icons.get(ns, "✏️")
                    is_last = (i == len(file_details) - 1)
                    connector = "└──" if is_last else "├──"
                    tree_lines.append(f"{connector} {icon} {fname}  →  {stat}")
                tree_text = "\n".join(tree_lines)

                summary = _t("git_save_summary", hash=h, files=n, insertions=ins, deletions=dels)
                self._append_output(f"\n{summary}\n```\n{tree_text}\n```\n", "system")
        else:
            self._status_label.setText(_t("status_git_save_fail"))
            self._append_output(_t("git_save_fail", error=result.get("error", "")), "system")
        self._update_git_status()

    def _git_push(self):
        workspace = self._ws_input.text().strip()
        if not workspace:
            self._status_label.setText(_t("status_no_workspace"))
            return
        from gangge.layer4_tools.shadow_git import ShadowGit
        sg = ShadowGit(workspace)
        gh_user = self._settings.value("github_username", "")
        gh_email = self._settings.value("github_email", "")

        # Step 1: Ensure git repo exists (auto init if needed)
        if not sg.is_available():
            reply = QMessageBox.question(
                self, _t("git_init_title"),
                _t("git_init_msg"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            if not sg.ensure_init(user_name=gh_user, user_email=gh_email):
                self._status_label.setText(_t("status_git_no_repo"))
                return
            self._append_output(_t("git_init_done"), "system")

        # Step 2: Auto-commit uncommitted changes
        status_info = sg.status()
        if status_info.get("changed_files", 0) > 0:
            reply = QMessageBox.question(
                self, _t("git_autocommit_title"),
                _t("git_autocommit_msg", n=status_info["changed_files"]),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Yes:
                commit_result = sg.user_commit("")
                if commit_result["success"] and commit_result.get("files", 0) > 0:
                    h = commit_result.get("hash", "")
                    n = commit_result.get("files", 0)
                    self._append_output(_t("git_autocommit_done", hash=h, files=n), "system")
                elif not commit_result["success"]:
                    self._append_output(_t("git_save_fail", error=commit_result.get("error", "")), "system")
                    return
            else:
                self._append_output(_t("git_push_skip_commit"), "system")

        # Step 3: Ensure remote is configured
        remote_url = sg.get_remote_url("origin")
        if not remote_url:
            gh_token = self._settings.value("github_token", "")
            project_name = Path(workspace).name if workspace else "my-project"
            dlg = GitRemoteDialog(self, github_token=gh_token, project_name=project_name)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            url = dlg.get_result_url().strip()
            if not url:
                return
            add_result = sg.add_remote("origin", url)
            if not add_result["success"]:
                self._status_label.setText(_t("status_git_remote_fail"))
                self._append_output(_t("git_remote_fail", error=add_result.get("error", "")), "system")
                return
            remote_url = url
            self._append_output(_t("git_remote_set", url=remote_url), "system")

        # Step 4: Push
        branch = sg.get_current_branch()
        self._status_label.setText(_t("status_git_pushing"))
        QApplication.processEvents()
        result = sg.user_push("origin", branch)
        if result["success"]:
            is_first = result.get("first_push", False)
            if is_first:
                self._status_label.setText(_t("status_git_pushed_first", branch=branch))
                self._append_output(_t("git_push_done_first", branch=branch, remote=remote_url), "system")
            else:
                self._status_label.setText(_t("status_git_pushed", branch=branch))
                self._append_output(_t("git_push_done", branch=branch, remote=remote_url), "system")
        else:
            err = result.get("error", "")
            if err == "NO_REMOTE":
                self._status_label.setText(_t("status_git_no_remote"))
                self._append_output(_t("git_push_no_remote"), "system")
            else:
                self._status_label.setText(_t("status_git_push_fail"))
                self._append_output(_t("git_push_fail", error=err), "system")
        self._update_git_status()

    def _update_git_status(self):
        workspace = self._ws_input.text().strip()
        if not workspace:
            self._git_branch_label.setVisible(False)
            return
        from gangge.layer4_tools.shadow_git import ShadowGit
        sg = ShadowGit(workspace)
        if not sg.is_available():
            self._git_branch_label.setVisible(False)
            return
        info = sg.status()
        if info.get("available"):
            branch = info.get("branch", "?")
            changed = info.get("changed_files", 0)
            if changed > 0:
                self._git_branch_label.setText(f"🔀 {branch} (+{changed})")
                self._git_branch_label.setStyleSheet("color:#f0883e;font-size:11px;padding:0 6px;")
            else:
                self._git_branch_label.setText(f"🔀 {branch}")
                self._git_branch_label.setStyleSheet("color:#58a6ff;font-size:11px;padding:0 6px;")
            self._git_branch_label.setVisible(True)
        else:
            self._git_branch_label.setVisible(False)

    def _save_output(self):
        path, _ = QFileDialog.getSaveFileName(self, "保存输出", "gangge_output.txt", "文本文件 (*.txt);;所有文件 (*)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self._output.toPlainText())
            self._status_label.setText(f"已保存: {path}")

    def _browse_workspace(self):
        path = QFileDialog.getExistingDirectory(self, "选择工作目录", self._ws_input.text())
        if path:
            self._ws_input.setText(path)
            self._file_browser.set_root(path)
            self._status_label.setText(f"工作目录: {path}")
            self._update_git_status()

    # ── Novel action handler ───────────────────────────────────
    def _novel_new_book(self):
        self._on_novel_action("new", {})

    def _import_novel_folder(self, workspace: str, folder: str):
        import shutil
        src = Path(folder)
        if not src.is_dir():
            self._append_system_msg("选择的路径不是文件夹")
            return
        books_dir = Path(workspace) / "books"
        books_dir.mkdir(parents=True, exist_ok=True)
        dst = books_dir / src.name
        if dst.exists():
            self._append_system_msg(f"小说文件夹已存在: {dst}")
            self._novel_sidebar.refresh_books()
            return
        try:
            shutil.copytree(str(src), str(dst))
            self._append_system_msg(f"✅ 已载入小说: {src.name}")
            self._novel_sidebar.refresh_books()
        except Exception as e:
            self._append_system_msg(f"❌ 载入失败: {e}")

    def _select_sidebar_book(self, book_id: str):
        self._novel_sidebar.select_book(book_id)

    def _on_novel_action(self, action: str, data: dict):
        book_id = data.get("book_id", "")
        ws = self._ws_input.text().strip()

        if not ws:
            self._append_system_msg("请先设置工作目录")
            return

        if action == "new":
            dlg = NovelNewBookDialog(self)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                d = dlg.get_data()
                if d["title"]:
                    cmd = f'novel_init(title="{d["title"]}", genre="{d["genre"]}", target_chapters={d["target_chapters"]}, words_per_chapter={d["words_per_chapter"]})'
                    ideas = d.get("ideas", "").strip()
                    if ideas:
                        cmd += (
                            f'\n\n小说已经创建好了。这是我的故事构思：\n{ideas}\n\n'
                            f'请根据以上构思，按顺序执行：\n'
                            f'1. 先用 novel_setup 配置角色和世界观\n'
                            f'⚠️ 重要：到此为止！不要生成大纲，也不要写正文！\n'
                            f'用户需要先在「篇章」Tab 中创建和管理篇章，\n'
                            f'然后选择一个篇章，点击「生成大纲」为该篇章生成大纲。\n'
                            f'确认大纲合理后，再点击「展开章纲」生成章节大纲。'
                        )
                        self._append_system_msg(
                            f"📖 已收到你的创作构思，AI 将自动配置角色和世界观。\n"
                            f"完成后请在「篇章」Tab 中创建篇章（如：东海篇、阿拉巴斯坦篇），\n"
                            f"然后选择篇章 → 点击「生成大纲」→ 确认后 → 点击「展开章纲」。\n"
                            f"构思摘要：{ideas[:100]}{'…' if len(ideas) > 100 else ''}"
                        )
                    else:
                        self._append_system_msg(
                            f"📖 小说《{d['title']}》骨架已创建，尚未消耗 Token。\n"
                            f"在聊天窗口描述你的故事想法，AI 会自动帮你配置内容。"
                        )
                    self._task_input.setPlainText(cmd)
                    self._run_task()
            return

        if action == "select":
            if book_id:
                self._novel_right.set_workspace(ws, book_id)
            return

        if action == "load":
            folder = QFileDialog.getExistingDirectory(self, "选择小说文件夹", ws)
            if folder:
                self._import_novel_folder(ws, folder)
            return

        if not book_id:
            self._append_system_msg("请先在左侧小说面板选择一本书")
            return

        if action in ("outline", "generate_outline"):
            cmd = f'novel_outline(book_id="{book_id}")'
        elif action in ("write_next", "write_next_chapter"):
            sm_dir = Path(ws) / "books" / book_id / "state"
            outline_path = sm_dir / "outline.json"
            co_path = sm_dir / "chapter_outlines.json"
            if not outline_path.exists():
                self._append_system_msg("大纲尚未生成，正在自动生成大纲...")
                cmd = f'novel_outline(book_id="{book_id}")'
            elif not co_path.exists():
                self._append_system_msg("章纲尚未展开，正在自动展开章纲...")
                cmd = f'novel_chapter_outlines(book_id="{book_id}")'
            else:
                ws_path = sm_dir / "world_state.json"
                ch = 1
                if ws_path.exists():
                    try:
                        ws_data = json.loads(ws_path.read_text(encoding="utf-8"))
                        ch = ws_data.get("current_chapter", 0) + 1
                    except Exception:
                        pass
                cmd = f'novel_write_chapter(book_id="{book_id}", chapter_number={ch}, fast_mode={self._novel_right._fast_mode_cb.isChecked()})'
        elif action == "audit_latest":
            sm_dir = Path(ws) / "books" / book_id / "state"
            ws_path = sm_dir / "world_state.json"
            ch = 1
            if ws_path.exists():
                try:
                    ws_data = json.loads(ws_path.read_text(encoding="utf-8"))
                    ch = ws_data.get("current_chapter", 1)
                except Exception:
                    pass
            cmd = f'novel_audit(book_id="{book_id}", chapter_number={ch})'
        elif action == "export":
            cmd = f'novel_export(book_id="{book_id}")'
        elif action == "generate_chapter_outlines":
            cmd = f'novel_chapter_outlines(book_id="{book_id}")'
        elif action == "new_arc":
            self._append_system_msg("📖 请在聊天窗口描述新篇章的背景和任务目标，AI 将自动生成。\n"
                                   "例如：「这是第2个篇章，主角团来到新大陆，要寻找传说中的宝藏」")
            cmd = f'novel_new_arc(book_id="{book_id}")'
            self._task_input.setPlainText(cmd)
            return
        elif action == "audit_selected":
            sm_dir = Path(ws) / "books" / book_id / "state"
            ws_path = sm_dir / "world_state.json"
            ch = 1
            if ws_path.exists():
                try:
                    ws_data = json.loads(ws_path.read_text(encoding="utf-8"))
                    ch = ws_data.get("current_chapter", 1)
                except Exception:
                    pass
            cmd = f'novel_audit(book_id="{book_id}", chapter_number={ch})'
        elif action == "edit_characters":
            cmd = f'novel_status(book_id="{book_id}", detail="all")'
        elif action == "custom_command":
            cmd = data.get("command", "")
            if cmd:
                self._task_input.setPlainText(cmd)
                self._run_task()
            return
        elif action == "custom_command_preview":
            cmd = data.get("command", "")
            if cmd:
                self._task_input.setPlainText(cmd)
            return
        elif action == "status":
            cmd = f'novel_status(book_id="{book_id}", detail="all")'
        elif action == "import_reference":
            file_path, _ = QFileDialog.getOpenFileName(
                self, "选择参考小说文件", "", "文本文件 (*.txt *.md);;所有文件 (*)"
            )
            if file_path:
                cmd = f'novel_import(book_id="{book_id}", file_path="{file_path}")'
            else:
                return
        elif action == "imitate_write_next":
            sm_dir = Path(ws) / "books" / book_id / "state"
            ref_dir = sm_dir / "reference"
            if not ref_dir.exists():
                self._append_system_msg("尚未导入参考小说，请先点击「导入参考小说」按钮")
                return
            ws_path = sm_dir / "world_state.json"
            ch = 1
            if ws_path.exists():
                try:
                    ws_data = json.loads(ws_path.read_text(encoding="utf-8"))
                    ch = ws_data.get("current_chapter", 0) + 1
                except Exception:
                    pass
            cmd = f'novel_imitate_write(book_id="{book_id}", chapter_number={ch}, fast_mode={self._novel_right._fast_mode_cb.isChecked()})'
        else:
            cmd = f'novel_status(book_id="{book_id}", detail="basic")'

        self._task_input.setPlainText(cmd)
        self._run_task()

        self._novel_right.set_workspace(ws, book_id)
        self._novel_sidebar.refresh_books()

    def _build_novel_context(self, workspace: str) -> str:
        book_id = self._novel_sidebar.get_selected_book_id()
        if not book_id or not workspace:
            return ""

        sm_dir = Path(workspace) / "books" / book_id / "state"
        if not (sm_dir / "config.json").exists():
            return ""

        try:
            cfg = json.loads((sm_dir / "config.json").read_text(encoding="utf-8"))
        except Exception:
            return ""

        title = cfg.get("title", "?")
        genre = cfg.get("genre", "")
        target_ch = cfg.get("target_chapters", 0)
        current_ch = 0

        ws_data = {}
        ws_path = sm_dir / "world_state.json"
        if ws_path.exists():
            try:
                ws_data = json.loads(ws_path.read_text(encoding="utf-8"))
                current_ch = ws_data.get("current_chapter", 0)
            except Exception:
                pass

        characters_text = ""
        setup_path = sm_dir / "setup_state.json"
        if setup_path.exists():
            try:
                setup = json.loads(setup_path.read_text(encoding="utf-8"))
                chars = setup.get("characters", [])
                if chars:
                    characters_text = "\n".join(
                        f"  - {c.get('name', '?')}（{c.get('role', '?')}）：{c.get('personality', '')[:50]}"
                        for c in chars[:20]
                    )
            except Exception:
                pass

        outline_brief = ""
        outline_path = sm_dir / "outline.json"
        if outline_path.exists():
            try:
                outline = json.loads(outline_path.read_text(encoding="utf-8"))
                arcs = outline.get("arcs", [])
                if arcs:
                    outline_brief = "\n".join(
                        f"  - {a.get('name', '?')}（{a.get('chapters', '?')}章）"
                        for a in arcs[:10]
                    )
                else:
                    outline_brief = json.dumps(outline, ensure_ascii=False)[:800]
            except Exception:
                pass

        hooks_text = ""
        try:
            hooks = ws_data.get("hooks", [])
            open_hooks = [h for h in hooks if h.get("status") == "open"]
            if open_hooks:
                hooks_text = "\n".join(
                    f"  - {h.get('description', '?')[:60]}"
                    for h in open_hooks[:10]
                )
        except Exception:
            pass

        ch_dir = Path(workspace) / "books" / book_id / "chapters"
        existing_chapters = []
        if ch_dir.exists():
            for f in sorted(ch_dir.glob("chapter_*.md")):
                existing_chapters.append(f.name)

        context = f"""## 📖 小说创作模式

当前正在创作小说《{title}》，以下是完整上下文信息：

### 基本信息
- 书名：《{title}》
- 题材：{genre}
- 目标：{target_ch} 章，每章 {cfg.get('target_words_per_chapter', '?')} 字
- 当前进度：已写至第 {current_ch} 章

### 角色
{characters_text or '  （尚未配置）'}

### 大纲弧线
{outline_brief or '  （尚未生成大纲）'}

### 未闭合伏笔
{hooks_text or '  （无）'}

### 项目文件结构
```
{workspace}/books/{book_id}/
  state/
    config.json           ← 书籍配置
    setup_state.json      ← 角色/势力/地点配置
    outline.json          ← 大纲
    chapter_outlines.json ← 章纲（每章详细节拍）
    world_state.json      ← 世界状态/伏笔/因果链
    truth/
      chapter_summaries.md ← 章节摘要
      pending_hooks.md     ← 未闭合伏笔
      current_state.md     ← 当前世界状态
  chapters/
    chapter_001.md ~ chapter_{current_ch:03d}.md  ← 已写章节
```

### 可用的小说工具
- novel_edit(book_id="{book_id}", target_type, action, data) — 修改角色/关系/伏笔/大纲/地点/势力/章节
- novel_setup(book_id="{book_id}", ...) — 配置角色/势力/地点
- novel_outline(book_id="{book_id}") — 生成/重新生成大纲
- novel_chapter_outlines(book_id="{book_id}") — 展开章纲
- novel_write_chapter(book_id="{book_id}", chapter_number=N, fast_mode=True) — 写一章
- novel_audit(book_id="{book_id}", chapter_number=N) — 审计章节
- novel_revise(book_id="{book_id}", chapter_number=N, feedback="...") — 修订章节
- novel_status(book_id="{book_id}", detail="all") — 查看详细状态
- novel_export(book_id="{book_id}") — 导出全书
- novel_navigate(book_id="{book_id}", target="...") — 快速定位和读取小说文件
- novel_graph_query(book_id="{book_id}", query="...") — 查询叙事知识图谱
- novel_consistency_check(book_id="{book_id}") — 一致性检查

### 操作指引
- 用户说"加角色/改大纲/加伏笔"等 → 调用 novel_edit
- 用户说"写下一章/重写第N章" → 调用 novel_write_chapter
- 用户说"看看状态/进度" → 调用 novel_status
- 用户说"看看第N章内容" → 调用 novel_navigate(target="chapter_N") 或直接 read_file
- 用户说"改一下第N章" → 先 novel_navigate 或 read_file 看内容，再 novel_edit 或 novel_revise
- 用户也可以让你做代码相关的任务，此时忽略小说上下文即可"""

        return context

    def _append_system_msg(self, msg: str):
        self._output.append(f'<p style="color:#8b949e;font-style:italic;">{msg}</p>')

    def _on_file_selected(self, file_path: str):
        try:
            p = Path(file_path)
            if not p.exists() or not p.is_file():
                return
            size_kb = p.stat().st_size / 1024
            ext = p.suffix.lower()
            image_exts = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", ".webp"}
            video_exts = {".mp4", ".avi", ".mov", ".mkv", ".webm"}
            audio_exts = {".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a"}
            binary_exts = {".exe", ".dll", ".so", ".pyc", ".pyd", ".zip", ".tar", ".gz", ".rar", ".7z", ".pdf", ".doc", ".docx", ".xlsx", ".pptx", ".db", ".sqlite"}

            self._preview_path.setText(f"📄 {p.name} ({size_kb:.1f} KB)")

            if ext in binary_exts:
                self._preview_image_scroll.hide()
                self._preview_output.show()
                self._preview_output.setPlainText(f"[二进制文件] {file_path}\n大小: {size_kb:.1f} KB")
                return
            if ext in image_exts:
                self._preview_output.hide()
                self._preview_image_scroll.show()
                pixmap = QPixmap(file_path)
                if pixmap.isNull():
                    self._preview_image_scroll.hide()
                    self._preview_output.show()
                    self._preview_output.setPlainText(f"[图片加载失败] {file_path}")
                    return
                scroll_w = self._preview_image_scroll.viewport().width()
                scroll_h = self._preview_image_scroll.viewport().height()
                scaled = pixmap.scaled(
                    scroll_w, scroll_h,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self._preview_image_label.setPixmap(scaled)
                self._preview_image_label.setMinimumSize(1, 1)
                self._preview_path.setText(f"🖼️ {p.name} ({size_kb:.1f} KB, {pixmap.width()}×{pixmap.height()})")
                return
            if ext in video_exts:
                self._preview_image_scroll.hide()
                self._preview_output.show()
                self._preview_output.setPlainText(f"[视频文件] {file_path}\n大小: {size_kb:.1f} KB\n提示: 可通过附件功能发送给多模态模型")
                return
            if ext in audio_exts:
                self._preview_image_scroll.hide()
                self._preview_output.show()
                self._preview_output.setPlainText(f"[音频文件] {file_path}\n大小: {size_kb:.1f} KB\n提示: 可通过附件功能发送给多模态模型")
                return
            if size_kb > 500:
                self._preview_image_scroll.hide()
                self._preview_output.show()
                self._preview_output.setPlainText(f"[文件过大] {file_path}\n大小: {size_kb:.1f} KB\n请使用 read_file 工具读取")
                return

            self._preview_image_scroll.hide()
            self._preview_output.show()
            content = p.read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()
            total = len(lines)
            max_show = 500
            shown = lines[:max_show]
            truncated = total > max_show

            numbered = "\n".join(f"{i+1:>5}│ {line}" for i, line in enumerate(shown))
            if truncated:
                numbered += f"\n\n... 省略 {total - max_show} 行 (共 {total} 行)"

            self._preview_output.setPlainText(numbered)
        except Exception as e:
            self._preview_image_scroll.hide()
            self._preview_output.show()
            self._preview_output.setPlainText(f"读取失败: {e}")

    def _update_provider_fields(self):
        key = self._provider_combo.currentData()
        cfg = PROVIDER_CONFIGS.get(key, PROVIDER_CONFIGS["deepseek"])

        # ── Reload API key for the new provider ──
        saved_key = self._settings.value(f"api_key_{key}", "")
        if saved_key:
            self._api_key_input.setText(saved_key)
        else:
            self._api_key_input.clear()

        self._model_combo.clear()
        if key == "ollama":
            base_url = self._base_url_input.text().strip() or cfg["base_url_default"]
            ollama_models = _fetch_ollama_models(base_url)
            if ollama_models:
                self._model_combo.addItems(ollama_models)
            else:
                self._model_combo.addItems(cfg["models"])
        elif key == "custom":
            pass
        else:
            self._model_combo.addItems(cfg["models"])
        sv = self._settings.value(f"model_{key}", cfg["model_default"])
        self._model_combo.setCurrentText(sv)
        if key == "custom":
            self._model_combo.setPlaceholderText("输入模型名称...")
        else:
            self._model_combo.setPlaceholderText("")
        if cfg.get("base_url_editable", False):
            self._base_url_input.setReadOnly(False)
            sv_url = self._settings.value(f"base_url_{key}", cfg["base_url_default"])
            self._base_url_input.setText(sv_url)
            self._base_url_input.setPlaceholderText(cfg["base_url_default"])
        else:
            self._base_url_input.setReadOnly(True)
            self._base_url_input.setText("")
            self._base_url_input.setPlaceholderText("(默认地址)")

    _MM_MODEL_PRESETS = {
        "qwen": ["qwen-vl-max", "qwen-vl-plus", "qwen2-vl-72b-instruct"],
        "openai": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo"],
        "anthropic": ["claude-3-5-sonnet-20241022", "claude-3-opus-20240229"],
        "deepseek": ["deepseek-vl2", "deepseek-vl2-small"],
        "siliconflow": ["Qwen/Qwen2-VL-72B-Instruct", "Qwen/Qwen-VL-Plus", "OpenGVLab/InternVL2-8B"],
        "ollama": ["llava", "llava:13b", "bakllava"],
        "custom": [],
    }

    def _update_mm_model_combo(self):
        """Update multimodal model combo when provider changes."""
        self._mm_model_combo.clear()
        provider_key = self._mm_provider_combo.currentData()
        presets = self._MM_MODEL_PRESETS.get(provider_key, [])
        if presets:
            self._mm_model_combo.addItems(presets)
            self._mm_model_combo.setCurrentText(presets[0])
        else:
            self._mm_model_combo.setPlaceholderText("输入模型名称...")

    def _get_thinking_model(self, provider_key: str, model: str) -> str:
        """Map to thinking/reasoning model variant when thinking mode is on."""
        thinking_map = {
            "deepseek": {
                "deepseek-chat": "deepseek-reasoner",
                "deepseek-chat-v4": "deepseek-reasoner-v4",
                "deepseek-reasoner": "deepseek-reasoner",
                "deepseek-reasoner-v4": "deepseek-reasoner-v4",
            },
            "openai": {
                "gpt-4o": "o1",
                "gpt-4o-mini": "o3-mini",
                "gpt-4-turbo": "o1",
                "gpt-3.5-turbo": "o3-mini",
            },
            "siliconflow": {
                "deepseek-ai/DeepSeek-V3": "deepseek-ai/DeepSeek-R1",
                "Qwen/Qwen2.5-72B-Instruct": "Qwen/QwQ-32B-Preview",
            },
        }
        provider_map = thinking_map.get(provider_key, {})
        return provider_map.get(model, model)

    def _build_llm(self) -> BaseLLM | None:
        provider_key = self._provider_combo.currentData()
        api_key = self._api_key_input.text().strip()
        model = self._model_combo.currentText().strip()
        cfg = PROVIDER_CONFIGS.get(provider_key, {})

        # Apply thinking mode model mapping
        if self._thinking_mode_cb.isChecked():
            mapped = self._get_thinking_model(provider_key, model)
            if mapped != model:
                logger.info(f"思考模式: 模型 {model} → {mapped}")
            model = mapped

        temperature = 0.0
        # Reasoning models don't use temperature
        if self._thinking_mode_cb.isChecked() and provider_key in ("openai", "deepseek"):
            temperature = 1.0  # reasoning models require temp=1

        if provider_key == "anthropic":
            if not api_key:
                api_key = os.environ.get("ANTHROPIC_API_KEY", "")
            if not api_key:
                self._status_label.setText("⚠️ 请输入 Anthropic API Key")
                return None
            from gangge.layer5_llm.anthropic import AnthropicLLM
            return AnthropicLLM(api_key=api_key, model=model, max_tokens=8192, temperature=temperature)
        elif provider_key == "ollama":
            url = self._base_url_input.text().strip() or "http://localhost:11434/v1"
            from gangge.layer5_llm.openai_compat import OpenAICompatLLM
            return OpenAICompatLLM(base_url=url, api_key="ollama", model=model, max_tokens=8192, temperature=temperature)
        elif provider_key == "custom":
            base_url = self._base_url_input.text().strip()
            if not base_url:
                self._status_label.setText("⚠️ 自定义模式需要填写 Base URL")
                return None
            if not model:
                self._status_label.setText("⚠️ 自定义模式需要填写模型名称")
                return None
            from gangge.layer5_llm.openai_compat import OpenAICompatLLM
            return OpenAICompatLLM(base_url=base_url, api_key=api_key or "not-needed", model=model, max_tokens=8192, temperature=temperature)
        else:
            api_key_env = cfg.get("api_key_env", "")
            if not api_key and api_key_env:
                api_key = os.environ.get(api_key_env, "")
            if not api_key and provider_key != "ollama":
                label = cfg.get("label", provider_key)
                self._status_label.setText(f"⚠️ 请输入 {label} API Key")
                return None
            base_url = self._base_url_input.text().strip() or cfg.get("base_url_default", "")
            if not base_url:
                self._status_label.setText("⚠️ 请填写 Base URL")
                return None
            from gangge.layer5_llm.openai_compat import OpenAICompatLLM
            return OpenAICompatLLM(base_url=base_url, api_key=api_key, model=model, max_tokens=8192, temperature=temperature)

    def _build_multimodal_llm(self) -> BaseLLM | None:
        """Build a separate multimodal LLM for image recognition tasks."""
        if not self._mm_enable_cb.isChecked():
            return None
        provider_key = self._mm_provider_combo.currentData()
        api_key = self._mm_api_key_input.text().strip()
        model = self._mm_model_combo.currentText().strip()
        if not model:
            return None
        cfg = PROVIDER_CONFIGS.get(provider_key, {})
        base_url = self._mm_base_url_input.text().strip() or cfg.get("base_url_default", "")
        if not base_url:
            return None
        if not api_key:
            # Fall back to main model's API key for the same provider
            if provider_key == self._provider_combo.currentData():
                api_key = self._api_key_input.text().strip()
            if not api_key:
                api_key_env = cfg.get("api_key_env", "")
                if api_key_env:
                    api_key = os.environ.get(api_key_env, "")
        if not api_key and provider_key not in ("ollama",):
            return None
        from gangge.layer5_llm.openai_compat import OpenAICompatLLM
        return OpenAICompatLLM(base_url=base_url, api_key=api_key or "not-needed", model=model, max_tokens=4096, temperature=0.0)

    # ── Run / Batch / Cancel ──────────────────────────────────
    _NOVEL_TOOL_NAMES = frozenset([
        "novel_init", "novel_setup", "novel_outline",
        "novel_chapter_outlines", "novel_write_chapter",
        "novel_audit", "novel_revise", "novel_status",
        "novel_edit", "novel_export", "novel_list_books",
        "novel_graph_query", "novel_consistency_check", "novel_graph_rebuild",
        "novel_navigate",
    ])

    def _parse_novel_tool_call(self, task: str) -> tuple[str, dict] | None:
        import re
        m = re.match(r'^(\w+)\((.*)\)$', task.strip(), re.DOTALL)
        if not m:
            return None
        tool_name, args_str = m.group(1), m.group(2).strip()
        if tool_name not in self._NOVEL_TOOL_NAMES:
            return None
        try:
            if args_str:
                args = json.loads(f"{{{args_str}}}")
            else:
                args = {}
        except json.JSONDecodeError:
            return None
        return tool_name, args

    def _run_task(self):
        if self._running:
            self._status_label.setText("⏳ 任务执行中...")
            return

        task = self._task_input.toPlainText().strip()
        if not task:
            self._status_label.setText("⚠️ 请输入任务")
            return

        parsed = self._parse_novel_tool_call(task)
        if parsed:
            self._run_novel_direct(parsed[0], parsed[1])
            return

        llm = self._build_llm()
        if not llm:
            return
        self._execute_single(llm, task)

    def _run_novel_direct(self, tool_name: str, args: dict):
        if self._running:
            self._status_label.setText("⏳ 任务执行中...")
            return

        llm = self._build_llm()
        if not llm:
            return

        workspace = self._ws_input.text().strip()
        if not workspace:
            from datetime import datetime
            ws_dir = Path.cwd() / "gangge_projects" / f"project_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            ws_dir.mkdir(parents=True, exist_ok=True)
            workspace = str(ws_dir)
            self._ws_input.setText(workspace)
            self._file_browser.set_root(workspace)

        self._running = True
        self._btn_run.setVisible(False)
        self._batch_btn.setEnabled(False)
        self._btn_stop.setVisible(True)
        self._btn_cancel.setEnabled(True)
        self._task_input.setEnabled(False)
        self._status_progress.setVisible(True)
        self._status_progress.setRange(0, 0)
        self._stats_label.setText("")
        self._timer_label.setText("⏱ 00:00")
        self._start_time = time.monotonic()
        from PyQt6.QtCore import QTimer
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.timeout.connect(self._update_elapsed)
        self._elapsed_timer.start(1000)

        self._status_label.setText(f"⚡ 快捷模式: {tool_name}")
        self._append_output(f"\n⚡ 快捷模式执行: {tool_name}({json.dumps(args, ensure_ascii=False)})\n", "system")
        self._append_output("─" * 60 + "\n", "system")

        self._novel_worker = NovelDirectWorker(
            llm=llm, workspace=workspace,
            tool_name=tool_name, args=args,
        )
        self._novel_worker.progress.connect(lambda msg: self._append_output(msg, "assistant"))
        self._novel_worker.finished.connect(self._on_novel_direct_finished)
        self._novel_worker.start()

    def _on_novel_direct_finished(self, result: dict):
        self._running = False
        self._btn_run.setVisible(True)
        self._batch_btn.setEnabled(True)
        self._btn_stop.setVisible(False)
        self._btn_cancel.setEnabled(False)
        self._task_input.setEnabled(True)
        self._status_progress.setVisible(False)
        if hasattr(self, '_elapsed_timer') and self._elapsed_timer:
            self._elapsed_timer.stop()

        if result.get("error"):
            self._append_output(f"\n❌ 错误: {result['error']}\n", "error")
            self._status_label.setText("❌ 执行失败")
        else:
            output = result.get("output", "")
            if output:
                self._append_output(f"\n{output}\n", "assistant")
            self._status_label.setText("✅ 快捷执行完成")

        self._append_output("\n" + "═" * 60 + "\n", "system")

        elapsed = int(time.monotonic() - self._start_time) if hasattr(self, "_start_time") else 0
        mins, secs = divmod(elapsed, 60)
        self._stats_label.setText(f"耗时 {mins:02d}:{secs:02d}")

        ws = self._ws_input.text().strip()
        book_id = result.get("book_id", "")
        if book_id:
            self._novel_right.set_workspace(ws, book_id)
        self._novel_sidebar.refresh_books()
        if book_id:
            self._select_sidebar_book(book_id)

    def _run_batch(self):
        if self._running:
            return
        text = self._task_input.toPlainText().strip()
        tasks = [t.strip() for t in text.split("\n") if t.strip()]
        if len(tasks) < 2:
            self._run_task()
            return

        llm = self._build_llm()
        if not llm:
            return

        self._batch_queue = tasks
        self._status_label.setText(f"📋 批量任务: {len(tasks)} 个")
        self._execute_batch_next(llm)

    def _execute_batch_next(self, llm: BaseLLM):
        if not self._batch_queue:
            self._status_label.setText("✅ 批量任务全部完成")
            return

        task = self._batch_queue[0]
        remaining = len(self._batch_queue)
        total = len(self._batch_queue) + (1 if self._running else 0)

        # Create a new session for batch if none exists
        if not self._current_session_id:
            ws = self._ws_input.text().strip()
            self._current_session_id = self._db.create_session("批量任务", ws)
            self._refresh_session_list()

        self._execute_single(llm, task, batch_index=total - remaining, batch_total=total)

    def _on_workspace_changed(self):
        """Clear conversation history when the workspace changes."""
        self._conversation_history = []

    def _execute_single(self, llm: BaseLLM, task: str, batch_index: int = 0, batch_total: int = 1):
        workspace = self._ws_input.text().strip()
        if not workspace:
            # Auto-create project folder
            from datetime import datetime
            ws_dir = Path.cwd() / "gangge_projects" / f"project_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            ws_dir.mkdir(parents=True, exist_ok=True)
            workspace = str(ws_dir)
            self._ws_input.setText(workspace)
            self._file_browser.set_root(workspace)
            self._status_label.setText(f"📁 自动创建项目: {workspace}")
        auto_allow = self._auto_allow_cb.isChecked()
        plan_mode = self._plan_mode_cb.isChecked()
        extra_prompt = self._extra_prompt.toPlainText().strip()

        # ── Auto-inject novel context if a book is selected ──
        novel_context = self._build_novel_context(workspace)
        if novel_context:
            extra_prompt = (extra_prompt + "\n\n" + novel_context).strip()
            self._append_output("📖 已注入小说创作上下文\n", "system")

        # ── Project context will be built in Worker thread to avoid blocking UI ──
        auto_inject = self._auto_inject_cb.isChecked()

        # Create session if none
        if not self._current_session_id:
            self._current_session_id = self._db.create_session(task[:40], workspace)
            self._refresh_session_list()

        self._llm = llm
        self._running = True
        self._btn_run.setVisible(False)
        self._batch_btn.setEnabled(False)
        self._btn_stop.setVisible(True)
        self._btn_cancel.setEnabled(True)
        self._task_input.setEnabled(False)
        self._status_progress.setVisible(True)
        self._status_progress.setRange(0, 0)
        self._stats_label.setText("")
        self._timer_label.setText("⏱ 00:00")

        # Start elapsed timer
        self._start_time = time.monotonic()
        from PyQt6.QtCore import QTimer
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.timeout.connect(self._update_elapsed)
        self._elapsed_timer.start(1000)

        batch_text = f" [{batch_index + 1}/{batch_total}]" if batch_total > 1 else ""
        self._status_label.setText(f"🚀 执行{batch_text}...")

        # ── Auto-detect file:/// URLs in task text as attachments ──
        if task:
            import re
            file_urls = re.findall(r'file:///([^\s\n]+)', task)
            for fpath in file_urls:
                fpath = fpath.strip().rstrip(".,;:!?")
                p = Path(fpath)
                if p.exists() and p.suffix.lower() in ('.png','.jpg','.jpeg','.gif','.bmp','.webp'):
                    import base64
                    try:
                        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
                        ext = p.suffix.lower()
                        mt = f"image/{ext.lstrip('.')}"
                        if ext == ".jpg": mt = "image/jpeg"
                        att = {"path": str(p), "name": p.name, "media_type": mt, "data": b64, "icon": "🖼️"}
                        self._attachments.append(att)
                        self._append_output(f"🖼️ 自动加载图片附件: {p.name}\n", "system")
                    except Exception as e:
                        self._append_output(f"⚠️ 无法加载图片 {p.name}: {e}\n", "system")
            # Strip file:/// URLs from task text for clean LLM input
            if file_urls:
                task = re.sub(r'file:///[^\s\n]+', '', task).strip()

        # ── Build multimodal LLM for VisionTool (if configured) ──
        multimodal_llm = None
        if self._mm_enable_cb.isChecked():
            mm_llm = self._build_multimodal_llm()
            if mm_llm:
                multimodal_llm = mm_llm
                logging.getLogger("gangge").info("多模态模型已就绪，通过 vision 工具调用")

        self._worker = GanggeWorker(
            llm=llm, task=task, workspace=workspace,
            multimodal_llm=multimodal_llm,
            previous_messages=self._conversation_history,
            max_rounds=self._max_rounds_spin.value(),
            plan_mode=plan_mode, project_context="",  # built in worker thread
            system_prompt_extra=extra_prompt, auto_allow=auto_allow,
            batch_index=batch_index, batch_total=batch_total,
            project_map="",
            file_registry={},
            ganggerules="",
            memory_bank_progress="",
            memory_bank_changelog="",
            provider=self._provider_combo.currentData(),
            model_name=self._model_combo.currentText(),
            attachments=list(self._attachments),
            auto_inject=auto_inject,
        )
        self._worker.text_block.connect(self._append_output)
        self._worker.tool_call_sig.connect(self._on_tool_call)
        self._worker.finished.connect(lambda s: self._on_finished(s, llm))
        self._worker.ask_user_sig.connect(self._on_ask_user)
        # ── Store turn messages for conversation continuity ──
        self._worker.turn_complete.connect(
            lambda msgs: self._on_turn_complete(msgs)
        )
        if self._current_session_id:
            sid = self._current_session_id
            db = self._db
            self._worker.turn_complete.connect(
                lambda msgs: db.save_turn(sid, msgs)
            )
        # ────────────────────────────────────────────────────────
        self._worker.start()

        self._attachments.clear()
        self._refresh_attachment_bar()

    def _cancel_task(self):
        if self._worker and self._running:
            self._worker.cancel()
            self._append_output("\n⏹ 任务已取消\n", "system")
            self._batch_queue.clear()
            self._on_finished({}, None)

    def _on_turn_complete(self, msgs: list):
        """Store raw conversation history for multi-turn context."""
        # Get raw Message objects from the Worker (not aggregated dicts)
        if hasattr(self, "_worker") and self._worker is not None:
            raw = getattr(self._worker, "_conversation_messages", [])
            if raw:
                self._conversation_history = raw

    def _update_elapsed(self):
        if hasattr(self, "_start_time"):
            elapsed = int(time.monotonic() - self._start_time)
            mins, secs = divmod(elapsed, 60)
            self._timer_label.setText(f"⏱ {mins:02d}:{secs:02d}")

    def _on_finished(self, summary: dict, llm: BaseLLM | None):
        self._running = False
        self._btn_run.setVisible(True)
        self._btn_stop.setVisible(False)
        self._batch_btn.setEnabled(True)
        self._btn_cancel.setEnabled(False)
        self._task_input.setEnabled(True)
        self._status_progress.setVisible(False)
        self._status_progress.setRange(0, 100)
        self._status_progress.setValue(0)

        # Stop elapsed timer
        if hasattr(self, "_elapsed_timer") and self._elapsed_timer:
            self._elapsed_timer.stop()

        # Enable rollback button if checkpoint exists
        has_checkpoint = bool(summary and summary.get("shadow_checkpoint_before"))
        self._btn_rollback.setEnabled(has_checkpoint)
        self._update_git_status()

        # Update final stats
        if summary and not summary.get("error"):
            r = summary.get("rounds", 0)
            c = summary.get("tool_calls", 0)
            tokens = summary.get("tokens", {})
            inp = tokens.get("input", 0)
            out = tokens.get("output", 0)
            cost = summary.get("cost", "")
            cost_str = f" | {cost}" if cost else ""
            self._stats_label.setText(f"🔄 {r} 轮 | 🔧 {c} 次 | 📥 {inp} | 📤 {out}{cost_str}")

        # ── Plan mode: show confirmation dialog ──
        if summary.get("plan_mode") and summary.get("final_response"):
            dlg = PlanConfirmDialog(summary["final_response"], self)
            if dlg.exec() == QDialog.DialogCode.Accepted and dlg.approved:
                plan_text = dlg.get_plan_text()
                self._append_output("\n📋 计划已批准，开始执行...\n", "system")
                # Re-run with approved plan injected
                task = self._task_input.toPlainText().strip()
                task += "\n\n按照以下已批准的计划执行:\n" + plan_text
                self._task_input.setPlainText(task)
                # Trigger execution in next event loop iteration
                from PyQt6.QtCore import QTimer
                QTimer.singleShot(100, self._run_task)
                return
            elif dlg.exec() == QDialog.DialogCode.Accepted and not dlg.approved:
                self._append_output("\n⏹ 计划被拒绝\n", "system")
                self._status_label.setText("计划被拒绝")
                return

        # ── Memory Bank update ──
        mb_update = summary.get("memory_bank_update", "")
        if mb_update and self._ws_input.text():
            workspace = self._ws_input.text().strip()
            # Split update into progress and changelog parts
            progress_part = ""
            changelog_part = mb_update
            if "progress" in mb_update.lower() or "## 进度" in mb_update:
                progress_part = mb_update
            update_memory_bank(workspace, progress_part, changelog_part)
            if progress_part or changelog_part:
                self._append_output("📚 Memory Bank 已更新\n", "system")

        # ── Git auto-commit ──
        if (
            self._git_commit_cb.isChecked()
            and summary
            and not summary.get("error")
            and self._ws_input.text()
        ):
            workspace = self._ws_input.text().strip()
            git_dir = Path(workspace) / ".git"
            if git_dir.exists():
                task_text = self._task_input.toPlainText().strip()[:60]
                commit_msg = f"gangge: {task_text or 'auto commit'}"
                result = auto_git_commit(workspace, commit_msg)
                self._append_output(f"🔀 Git: {result}\n", "system")

        if summary.get("error"):
            self._status_label.setText(f"❌ {summary['error']}")
        elif summary:
            r = summary.get("rounds", 0)
            c = summary.get("tool_calls", 0)
            self._status_label.setText(f"✅ 完成: {r} 轮, {c} 次工具调用")

        # ── Refresh novel panels after task completion ──
        if self._ws_input.text():
            ws = self._ws_input.text().strip()
            book_id = self._novel_sidebar.get_selected_book_id()
            if book_id:
                self._novel_right.set_workspace(ws, book_id)
            self._novel_sidebar.refresh_books()

        self._worker = None

        # Continue batch
        if self._batch_queue:
            self._batch_queue.pop(0)
            if self._batch_queue and llm:
                self._execute_batch_next(llm)

    def _on_ask_user(self, question: str):
        answer, ok = QInputDialog.getText(
            self, "AI 需要你的输入", question,
        )
        if ok:
            self._worker._ask_user_answer = answer.strip()
        else:
            self._worker._ask_user_answer = ""
        self._worker._ask_user_event.set()

    def _on_tool_call(self, tool_name: str, output: str, is_error: bool, diff: str):
        self._tool_panel.add_entry(tool_name, output, is_error, diff)
        # Persist to DB
        if self._current_session_id:
            self._db.save_tool_call(
                self._current_session_id,
                round_num=self._tool_panel._table.rowCount(),
                tool_name=tool_name,
                tool_input="",
                tool_output=output,
                is_error=is_error,
                diff=diff,
            )

        # ── Auto-refresh novel panels after novel tool calls ──
        # 必须在主线程执行GUI操作，用 QTimer 延迟确保线程安全
        if tool_name in self._NOVEL_TOOL_NAMES and not is_error:
            refresh_map = {
                "novel_init": [self._refresh_dashboard],
                "novel_setup": [self._refresh_characters, self._refresh_world, self._refresh_arcs],
                "novel_outline": [self._refresh_outline, self._refresh_arcs],
                "novel_chapter_outlines": [self._refresh_outline, self._refresh_chapters],
                "novel_write_chapter": [self._refresh_chapters, self._refresh_dashboard],
                "novel_audit": [],
                "novel_revise": [self._refresh_chapters],
                "novel_edit": [self._refresh_characters, self._refresh_world, self._refresh_outline, self._refresh_arcs, self._refresh_chapters],
                "novel_new_arc": [self._refresh_arcs, self._refresh_outline],
                "novel_export": [],
                "novel_status": [],
                "novel_list_books": [],
                "novel_navigate": [],
                "novel_graph_query": [self._refresh_graph],
                "novel_consistency_check": [],
                "novel_graph_rebuild": [self._refresh_graph],
            }
            fns = refresh_map.get(tool_name, [])
            if fns:
                from PyQt6.QtCore import QTimer
                def _safe_refresh():
                    for fn in fns:
                        try:
                            fn()
                        except Exception as e:
                            logging.getLogger("gangge").warning("面板刷新失败: %s - %s", fn.__name__, e)
                QTimer.singleShot(0, _safe_refresh)

    def _on_thinking_link(self, url):
        """Handle click on a thinking bubble's 'expand' link."""
        bubble_id = url.toString()
        full_text = self._thinking_bubbles.get(bubble_id, "")
        if not full_text:
            return
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QPushButton, QTextBrowser
        dlg = QDialog(self)
        dlg.setWindowTitle("🧠 完整思考过程")
        dlg.resize(680, 540)
        dlg.setStyleSheet("QDialog{background:#0d1117;}")
        lay = QVBoxLayout(dlg)
        tb = QTextBrowser()
        tb.setPlainText(full_text)
        tb.setStyleSheet(
            "QTextBrowser{background:#161b22;color:#c9d1d9;border:1px solid #30363d;"
            "border-radius:6px;padding:12px;font-family:Consolas,monospace;font-size:12px;}"
        )
        lay.addWidget(tb)
        btn = QPushButton("关闭")
        btn.setStyleSheet(
            "QPushButton{background:#21262d;color:#c9d1d9;border:1px solid #30363d;"
            "border-radius:6px;padding:8px 20px;}"
            "QPushButton:hover{background:#30363d;}"
        )
        btn.clicked.connect(dlg.accept)
        lay.addWidget(btn, alignment=Qt.AlignmentFlag.AlignRight)
        dlg.exec()

    def _append_output(self, text: str, role: str = ""):
        """Render message as a styled bubble card."""
        cursor = self._output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        # ── Thinking content: render as collapsible block ──
        if role == "thinking":
            import hashlib
            bubble_id = hashlib.md5(text.encode()).hexdigest()[:12]
            self._thinking_bubbles[bubble_id] = text
            escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            # Truncate preview to ~300 chars
            preview = escaped[:300]
            truncated = len(escaped) > 300
            if truncated:
                preview = preview[:297] + "..."
            expand_link = ""
            if truncated:
                expand_link = (
                    f'<br><a href="{bubble_id}" style="color:#58a6ff;text-decoration:none;'
                    f'font-size:12px;">📖 查看完整思考过程 ({len(text)} 字符)</a>'
                )
            bubble_html = (
                f'<div style="margin:6px 40px 6px 8px;padding:10px 14px;'
                f'background:#0d1117;border:1px solid #30363d;border-left:3px solid #58a6ff;'
                f'border-radius:8px;">'
                f'<div style="font-size:11px;color:#8b949e;margin-bottom:4px;">'
                f'🧠 <strong style="color:#58a6ff;">思考过程</strong>'
                f'<span style="float:right;color:#484f58;">{datetime.now().strftime("%H:%M")}</span>'
                f'</div>'
                f'<div style="color:#8b949e;font-size:12px;line-height:1.6;font-style:italic;">'
                f'{preview}{expand_link}</div></div>'
            )
            cursor.insertHtml(bubble_html)
            self._output.setTextCursor(cursor)
            self._output.ensureCursorVisible()
            return

        # ── Bubble styling by role ──
        bubble_cfg = {
            "user": {
                "icon": "👤",
                "title": "你",
                "bg": "#1f2937",
                "border": "#374151",
                "text_color": "#e5e7eb",
                "align": "left",
            },
            "assistant": {
                "icon": "🤖",
                "title": "AI",
                "bg": "#111827",
                "border": "#1f6feb",
                "text_color": "#c9d1d9",
                "align": "left",
            },
            "tool": {
                "icon": "🔧",
                "title": "工具",
                "bg": "#1a1500",
                "border": "#d29922",
                "text_color": "#d29922",
                "align": "left",
            },
            "system": {
                "icon": "ℹ️",
                "title": "系统",
                "bg": "#0d1117",
                "border": "#30363d",
                "text_color": "#8b949e",
                "align": "center",
            },
            "error": {
                "icon": "❌",
                "title": "错误",
                "bg": "#3a1b1b",
                "border": "#f85149",
                "text_color": "#f85149",
                "align": "left",
            },
        }.get(role, {
            "icon": "",
            "title": "",
            "bg": "#0d1117",
            "border": "#30363d",
            "text_color": "#c9d1d9",
            "align": "left",
        })

        cfg = bubble_cfg
        escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        # Code block handling with syntax highlighting hints
        lines = escaped.split("\n")
        parts = []
        in_code = False
        code_lang = ""
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("```"):
                if in_code:
                    parts.append("</code></pre>")
                    in_code = False
                    code_lang = ""
                else:
                    code_lang = stripped[3:].strip()
                    lang_label = f'<div style="color:#8b949e;font-size:11px;padding:2px 8px;background:#161b22;border-bottom:1px solid #30363d;">{code_lang}</div>' if code_lang else ""
                    parts.append(f'{lang_label}<pre style="background:#161b22;padding:8px 12px;margin:4px 0;border-radius:4px;overflow-x:auto;"><code style="color:#c9d1d9;font-family:Consolas,monospace;font-size:12px;line-height:1.5;">')
                    in_code = True
                continue
            if in_code:
                parts.append(line + "\n")
            elif line == "":
                parts.append("<br>")
            else:
                # Inline code `...`
                import re
                line = re.sub(r'`([^`]+)`', r'<code style="background:#161b22;padding:1px 4px;border-radius:3px;color:#79c0ff;font-family:Consolas,monospace;font-size:12px;">\1</code>', line)
                # Bold **...**
                line = re.sub(r'\*\*([^*]+)\*\*', r'<strong style="color:#f0f6fc;">\1</strong>', line)
                parts.append(line + "<br>")
        if in_code:
            parts.append("</code></pre>")

        content_html = "".join(parts)

        # Build bubble HTML
        if role == "system" and not text.strip().startswith("📋"):
            # Compact system messages (dividers, separators)
            bubble_html = (
                f'<div style="text-align:center;margin:6px 0;">'
                f'<span style="color:#484f58;font-size:12px;">{content_html}</span>'
                f'</div>'
            )
        else:
            margin = "margin:8px 40px 8px 8px;" if cfg["align"] == "left" else "margin:8px;"
            bubble_html = (
                f'<div style="{margin}padding:10px 14px;background:{cfg["bg"]};'
                f'border:1px solid {cfg["border"]};border-radius:10px;'
                f'border-left:3px solid {cfg["border"]};">'
                f'<div style="font-size:11px;color:#8b949e;margin-bottom:4px;">'
                f'{cfg["icon"]} <strong style="color:{cfg["text_color"]};">{cfg["title"]}</strong>'
                f'<span style="float:right;color:#484f58;">{datetime.now().strftime("%H:%M")}</span>'
                f'</div>'
                f'<div style="color:{cfg["text_color"]};font-size:13px;line-height:1.6;">'
                f'{content_html}</div></div>'
            )

        cursor.insertHtml(bubble_html)
        self._output.setTextCursor(cursor)
        self._output.ensureCursorVisible()

    # ── Settings ──────────────────────────────────────────────
    def _save_settings(self):
        key = self._provider_combo.currentData()
        self._settings.setValue("provider", key)
        self._settings.setValue(f"api_key_{key}", self._api_key_input.text())
        self._settings.setValue(f"model_{key}", self._model_combo.currentText())
        self._settings.setValue(f"base_url_{key}", self._base_url_input.text())
        self._settings.setValue("workspace", self._ws_input.text())
        self._settings.setValue("max_rounds", self._max_rounds_spin.value())
        self._settings.setValue("plan_mode", self._plan_mode_cb.isChecked())
        self._settings.setValue("thinking_mode", self._thinking_mode_cb.isChecked())
        self._settings.setValue("auto_allow", self._auto_allow_cb.isChecked())
        self._settings.setValue("auto_inject", self._auto_inject_cb.isChecked())
        self._settings.setValue("test_verify", self._test_verify_cb.isChecked())
        self._settings.setValue("git_commit", self._git_commit_cb.isChecked())
        self._settings.setValue("extra_prompt", self._extra_prompt.toPlainText())
        self._settings.setValue("language", self._lang_combo.currentData())
        self._settings.setValue("window_geometry", self.saveGeometry())
        self._settings.setValue("window_state", self.saveState())

    def _load_settings(self):
        p = self._settings.value("provider", "deepseek")
        idx = self._provider_combo.findData(p)
        if idx >= 0:
            self._provider_combo.setCurrentIndex(idx)
        # Restore per-provider settings
        api_key = self._settings.value(f"api_key_{p}", "")
        if api_key:
            self._api_key_input.setText(api_key)
        model_val = self._settings.value(f"model_{p}", "")
        if model_val:
            self._model_combo.setCurrentText(model_val)
        base_url = self._settings.value(f"base_url_{p}", "")
        if base_url:
            self._base_url_input.setText(base_url)
        ws = self._settings.value("workspace", "")
        if ws:
            self._ws_input.setText(ws)
            self._file_browser.set_root(ws)
        else:
            # No saved workspace → auto-create a project folder
            from datetime import datetime
            projects_root = Path.cwd() / "gangge_projects"
            project_name = f"project_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            auto_ws = projects_root / project_name
            auto_ws.mkdir(parents=True, exist_ok=True)
            self._ws_input.setText(str(auto_ws))
            self._file_browser.set_root(str(auto_ws))
        self._max_rounds_spin.setValue(int(self._settings.value("max_rounds", 30)))
        self._plan_mode_cb.setChecked(self._settings.value("plan_mode", "false") == "true")
        self._thinking_mode_cb.setChecked(self._settings.value("thinking_mode", "false") == "true")
        self._auto_allow_cb.setChecked(self._settings.value("auto_allow", "true") == "true")
        self._auto_inject_cb.setChecked(self._settings.value("auto_inject", "true") != "false")
        self._test_verify_cb.setChecked(self._settings.value("test_verify", "true") != "false")
        self._git_commit_cb.setChecked(self._settings.value("git_commit", "true") != "false")
        # Restore multimodal settings
        self._mm_enable_cb.setChecked(self._settings.value("mm_enable", "false") == "true")
        mm_p = self._settings.value("mm_provider", "qwen")
        mm_idx = self._mm_provider_combo.findData(mm_p)
        if mm_idx >= 0:
            self._mm_provider_combo.setCurrentIndex(mm_idx)
        mm_key = self._settings.value("mm_api_key", "")
        if mm_key:
            self._mm_api_key_input.setText(mm_key)
        mm_model = self._settings.value("mm_model", "")
        if mm_model:
            self._mm_model_combo.setCurrentText(mm_model)
        mm_url = self._settings.value("mm_base_url", "")
        if mm_url:
            self._mm_base_url_input.setText(mm_url)
        ep = self._settings.value("extra_prompt", "")
        if ep:
            self._extra_prompt.setPlainText(ep)
        lang = self._settings.value("language", "")
        if lang:
            idx = self._lang_combo.findData(lang)
            if idx >= 0:
                self._lang_combo.setCurrentIndex(idx)
                set_language(lang)
        geo = self._settings.value("window_geometry")
        if geo:
            self.restoreGeometry(geo)
        st = self._settings.value("window_state")
        if st:
            self.restoreState(st)


# ═════════════════════════════════════════════════════════════════
#  Entry Point
# ═════════════════════════════════════════════════════════════════
def main():
    # ── 全局日志：崩溃时写入文件 ──
    log_dir = Path.home() / ".gangge" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"crash_{datetime.now().strftime('%Y%m%d')}.log"
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(str(log_file), encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    logger = logging.getLogger("gangge")

    def global_exception_handler(exc_type, exc_value, exc_tb):
        logger.critical("未捕获异常，应用即将崩溃", exc_info=(exc_type, exc_value, exc_tb))
        import traceback
        tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            from PyQt6.QtWidgets import QMessageBox
            msg = QMessageBox()
            msg.setIcon(QMessageBox.Icon.Critical)
            msg.setWindowTitle("Gangge Code 崩溃")
            msg.setText(f"程序发生未捕获异常：\n{exc_value}")
            msg.setDetailedText(f"日志文件：{log_file}\n\n{tb_text}")
            msg.exec()
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = global_exception_handler

    # ── 捕获线程异常 ──
    threading.excepthook = lambda args: logger.critical(
        "线程异常: %s", args.exc_value, exc_info=(args.exc_type, args.exc_value, args.exc_traceback)
    )

    logger.info("Gangge Code 启动，日志文件: %s", log_file)

    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("Gangge Code")
    app.setApplicationVersion("0.1.0")
    app.setOrganizationName("Gangge")
    app.setStyleSheet(DARK_STYLESHEET)
    font = QFont("Segoe UI", 10)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(font)

    w = GanggeDesktop()
    w.show()
    logger.info("主窗口已显示")
    exit_code = app.exec()
    logger.info("应用退出，退出码: %s", exit_code)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
