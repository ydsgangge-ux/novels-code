"""源代码管理面板（Git）— 变更浏览 / 暂存 / 提交 / 历史 / 推送入口。

同时提供提交前的敏感信息安全检查：
  * 文件名规则：.env、私钥、凭据文件等 → 拦截
  * 内容规则：API Key / Token / 私钥 / 带密码的连接串 → 拦截
  * 大文件（>3MB）→ 提醒
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)


# ═════════════════════════════════════════════════════════════════
#  Git 封装
# ═════════════════════════════════════════════════════════════════

def _git(workspace: str, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """在 workspace 里执行 git 命令（不弹黑窗、UTF-8 输出）。"""
    kwargs: dict = {
        "cwd": workspace,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.run(["git", *args], **kwargs)
    except FileNotFoundError:
        return subprocess.CompletedProcess(["git", *args], 127, "", "未找到 git 命令，请先安装 Git")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(["git", *args], 124, "", "git 命令超时")


class GitRepo:
    """项目仓库的最小可用封装。"""

    def __init__(self, workspace: str):
        self.ws = workspace or ""

    # ── 基础信息 ──
    def available(self) -> bool:
        return bool(self.ws) and os.path.isdir(os.path.join(self.ws, ".git"))

    def init(self) -> tuple[bool, str]:
        r = _git(self.ws, ["init"])
        return r.returncode == 0, (r.stderr or "").strip()

    def branch(self) -> str:
        r = _git(self.ws, ["rev-parse", "--abbrev-ref", "HEAD"])
        return (r.stdout or "").strip() or "(无提交)"

    def remote_url(self) -> str:
        r = _git(self.ws, ["remote", "get-url", "origin"])
        return (r.stdout or "").strip()

    def has_upstream(self) -> bool:
        r = _git(self.ws, ["rev-parse", "--abbrev-ref", "@{upstream}"])
        return r.returncode == 0 and bool((r.stdout or "").strip())

    def ahead_behind(self) -> tuple[int, int]:
        if not self.has_upstream():
            return 0, 0
        r = _git(self.ws, ["rev-list", "--left-right", "--count", "@{upstream}...HEAD"])
        out = (r.stdout or "").split()
        if len(out) == 2:
            try:
                return int(out[1]), int(out[0])   # ahead, behind
            except ValueError:
                return 0, 0
        return 0, 0

    # ── 变更 ──
    def status(self) -> list[dict]:
        if not self.available():
            return []
        r = _git(self.ws, ["-c", "core.quotepath=false", "status", "--porcelain=v1", "-uall"])
        rows: list[dict] = []
        for line in (r.stdout or "").splitlines():
            if len(line) < 4:
                continue
            x, y = line[0], line[1]
            path = line[3:].strip()
            if " -> " in path:          # 重命名：旧 -> 新
                path = path.split(" -> ")[-1]
            if path.startswith('"') and path.endswith('"'):
                path = path[1:-1]
            rows.append({
                "path": path,
                "index": x,
                "worktree": y,
                "staged": x not in (" ", "?", "!"),
                "untracked": x == "?",
            })
        return rows

    def changed_paths(self) -> list[str]:
        return [f["path"] for f in self.status()]

    def staged_paths(self) -> list[str]:
        r = _git(self.ws, ["-c", "core.quotepath=false", "diff", "--cached", "--name-only"])
        return [p.strip() for p in (r.stdout or "").splitlines() if p.strip()]

    def stage(self, paths: list[str]) -> None:
        if paths:
            _git(self.ws, ["add", "--", *paths])

    def unstage(self, paths: list[str]) -> None:
        if paths:
            _git(self.ws, ["reset", "HEAD", "--", *paths])

    def stage_all(self) -> None:
        _git(self.ws, ["add", "-A"])

    # ── 差异 / 历史 ──
    def diff(self, path: str = "", staged: bool = False) -> str:
        args = ["-c", "core.quotepath=false", "diff", "--no-color"]
        if staged:
            args.append("--cached")
        if path:
            args += ["--", path]
        r = _git(self.ws, args, timeout=20)
        return (r.stdout or "") or "(没有差异)"

    def log(self, limit: int = 30) -> list[dict]:
        if not self.available():
            return []
        r = _git(self.ws, [
            "log", f"--max-count={limit}",
            "--pretty=format:%h|%ad|%an|%s", "--date=format:%Y-%m-%d %H:%M",
        ])
        rows = []
        for line in (r.stdout or "").splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                rows.append({"hash": parts[0], "date": parts[1], "author": parts[2], "message": parts[3]})
        return rows

    def show(self, ref: str, max_lines: int = 800) -> str:
        r = _git(self.ws, ["-c", "core.quotepath=false", "show", "--stat", "--patch", "--no-color", ref], timeout=30)
        text = (r.stdout or "") + (r.stderr or "")
        lines = text.splitlines()
        if len(lines) > max_lines:
            lines = lines[:max_lines] + [f"... （已截断，共 {len(lines)} 行）"]
        return "\n".join(lines)

    def outgoing_files(self, limit: int = 30) -> list[str]:
        """尚未推送的提交里涉及的文件（用于推送前安全检查）。"""
        if not self.available():
            return []
        if self.has_upstream():
            r = _git(self.ws, ["-c", "core.quotepath=false", "log", "@{upstream}..HEAD", "--name-only", "--pretty=format:"])
        else:
            r = _git(self.ws, ["-c", "core.quotepath=false", "log", f"--max-count={limit}", "--name-only", "--pretty=format:"])
        return sorted({p.strip() for p in (r.stdout or "").splitlines() if p.strip()})

    # ── 提交 / 推送 ──
    def commit(self, message: str) -> dict:
        r = _git(self.ws, ["commit", "-m", message[:4000]], timeout=60)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()
            if "nothing to commit" in err or "no changes added" in err:
                return {"success": True, "files": 0, "message": "nothing to commit"}
            return {"success": False, "error": err}
        h = _git(self.ws, ["rev-parse", "--short", "HEAD"]).stdout.strip()
        files = _git(self.ws, ["show", "--stat", "--oneline", "--format=", "HEAD"]).stdout
        count = len([l for l in files.splitlines() if l.strip() and "|" in l])
        return {"success": True, "hash": h, "files": count}

    def push(self) -> dict:
        if not self.available():
            return {"success": False, "error": "不是 Git 仓库"}
        if not self.remote_url():
            return {"success": False, "error": "NO_REMOTE"}
        branch = self.branch()
        cmd = ["push", "origin", branch] if self.has_upstream() else ["push", "-u", "origin", branch]
        r = _git(self.ws, cmd, timeout=120)
        if r.returncode != 0:
            return {"success": False, "error": (r.stderr or r.stdout or "").strip()}
        return {"success": True, "branch": branch}

    def ignore(self, paths: list[str]) -> None:
        """把文件加入 .gitignore，并把已跟踪的移出索引。"""
        gi = Path(self.ws) / ".gitignore"
        try:
            existing = gi.read_text(encoding="utf-8", errors="replace") if gi.exists() else ""
        except OSError:
            existing = ""
        lines = [l.strip() for l in existing.splitlines()]
        additions = [p.replace("\\", "/") for p in paths if p.replace("\\", "/") not in lines]
        if additions:
            with open(gi, "a", encoding="utf-8") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                for p in additions:
                    f.write(p + "\n")
        tracked = []
        for p in paths:
            r = _git(self.ws, ["ls-files", "--error-unmatch", "--", p])
            if r.returncode == 0:
                tracked.append(p)
        if tracked:
            _git(self.ws, ["rm", "--cached", "--", *tracked])


# ═════════════════════════════════════════════════════════════════
#  安全检查
# ═════════════════════════════════════════════════════════════════

@dataclass
class Issue:
    level: str      # "block" | "warn"
    path: str
    reason: str
    detail: str = ""


# 文件名规则：命中即拦截
_SENSITIVE_NAMES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(^|/)\.env($|\.)", re.I), "环境变量文件（常含 API Key / 数据库密码）"),
    (re.compile(r"\.(pem|key|p12|pfx|jks|keystore|ppk)$", re.I), "证书 / 私钥文件"),
    (re.compile(r"(^|/)id_(rsa|dsa|ecdsa|ed25519)$", re.I), "SSH 私钥"),
    (re.compile(r"(^|/)(credentials|secrets?|secret[_-]?key)\.(json|ya?ml|txt|ini|py|env)$", re.I), "凭据文件"),
    (re.compile(r"(^|/)\.(npmrc|pypirc|netrc|htpasswd|git-credentials)$", re.I), "凭据配置文件"),
    (re.compile(r"(^|/).*service[-_]?account.*\.json$", re.I), "云服务账号密钥文件"),
    (re.compile(r"(^|/)\.ssh/", re.I), "SSH 配置目录"),
]

# 内容规则：命中即拦截（只看文本文件）
_SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("OpenAI / DeepSeek 风格 API Key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    ("GitHub Token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Google API Key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Slack Token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("私钥内容", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("Bearer Token", re.compile(r"Bearer\s+[A-Za-z0-9\-\._~\+/]{30,}")),
    ("带密码的连接串", re.compile(r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp|ftp)://[^\s:@/]+:[^\s@/]{3,}@", re.I)),
    ("硬编码密码 / 密钥", re.compile(
        r"(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?key|token|password|passwd|pwd)\b\s*[:=]\s*[\"'][^\"'\s]{12,}[\"']"
    )),
]

_MAX_SCAN_BYTES = 512 * 1024     # 单个文件最多读 512KB 做内容扫描
_LARGE_FILE_BYTES = 3 * 1024 * 1024
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build"}


def _looks_text(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            chunk = f.read(4096)
    except OSError:
        return False
    return b"\x00" not in chunk


def _mask(value: str) -> str:
    value = value.strip()
    if len(value) <= 12:
        return value[:4] + "***"
    return value[:10] + "***" + value[-4:]


def scan_files(workspace: str, rel_paths: list[str]) -> list[Issue]:
    """扫描待提交文件中的敏感内容，返回按严重程度排列的问题列表。"""
    issues: list[Issue] = []
    ws = Path(workspace)
    for rel in rel_paths:
        rel_norm = rel.replace("\\", "/")
        if not rel_norm or any(seg in _SKIP_DIRS for seg in rel_norm.split("/")):
            continue

        # 1) 文件名规则
        for pattern, reason in _SENSITIVE_NAMES:
            if pattern.search(rel_norm):
                issues.append(Issue("block", rel_norm, reason,
                                    "这类文件通常不该进版本库，建议加入 .gitignore"))
                break

        full = ws / rel_norm
        if not full.is_file():
            continue

        # 2) 大文件
        try:
            size = full.stat().st_size
        except OSError:
            continue
        if size > _LARGE_FILE_BYTES:
            issues.append(Issue("warn", rel_norm, f"文件较大（{size / 1024 / 1024:.1f} MB）",
                                "确认是否应该提交二进制/数据文件"))

        # 3) 内容规则
        if size > _MAX_SCAN_BYTES or not _looks_text(full):
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for name, pattern in _SECRET_PATTERNS:
            m = pattern.search(text)
            if m:
                line_no = text[:m.start()].count("\n") + 1
                issues.append(Issue("block", rel_norm, f"疑似{name}", f"第 {line_no} 行: {_mask(m.group(0))}"))
                break
    return issues


# ═════════════════════════════════════════════════════════════════
#  安全报告对话框
# ═════════════════════════════════════════════════════════════════

class SecurityReportDialog(QDialog):
    """提交前的敏感信息报告：取消 / 仍然提交 / 加入 .gitignore。"""

    def __init__(self, parent, issues: list[Issue]):
        super().__init__(parent)
        self.choice = "cancel"
        self.setWindowTitle("🛡️ 提交前安全检查")
        self.setMinimumSize(620, 420)
        self.setStyleSheet("""
            QDialog{background:#0d1117;}
            QLabel{color:#c9d1d9;}
            QTextBrowser{background:#0d1117;border:1px solid #30363d;border-radius:6px;
                color:#c9d1d9;font-family:Consolas,'Cascadia Mono',monospace;font-size:12px;}
            QPushButton{background:#21262d;border:1px solid #30363d;border-radius:4px;
                color:#c9d1d9;padding:6px 14px;font-size:12px;}
            QPushButton:hover{background:#30363d;}
        """)
        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        blocks = [i for i in issues if i.level == "block"]
        warns = [i for i in issues if i.level == "warn"]
        title = QLabel(f"发现 {len(blocks)} 处可能泄露的敏感信息、{len(warns)} 处提醒")
        title.setStyleSheet("color:#f85149;font-size:14px;font-weight:600;")
        lay.addWidget(title)

        hint = QLabel("以下内容如果推送到 GitHub，可能造成密钥/隐私泄露。建议先加入 .gitignore 再提交。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#8b949e;font-size:12px;")
        lay.addWidget(hint)

        view = QTextBrowser()
        view.setPlainText("\n".join(
            f"[{'拦截' if i.level == 'block' else '提醒'}] {i.path}\n"
            f"      {i.reason}\n"
            + (f"      {i.detail}\n" if i.detail else "")
            for i in issues
        ))
        lay.addWidget(view, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        btn_cancel = QPushButton("取消提交")
        btn_cancel.setDefault(True)
        btn_cancel.clicked.connect(self.reject)
        btn_ignore = QPushButton("加入 .gitignore 并取消")
        btn_ignore.clicked.connect(self._on_ignore)
        btn_force = QPushButton("仍然提交")
        btn_force.setStyleSheet(
            "QPushButton{background:#3d1d1d;border:1px solid #f85149;color:#ffb3ae;}"
            "QPushButton:hover{background:#5c2121;}"
        )
        btn_force.clicked.connect(self._on_force)
        row.addWidget(btn_ignore)
        row.addWidget(btn_cancel)
        row.addWidget(btn_force)
        lay.addLayout(row)

    def _on_ignore(self):
        self.choice = "ignore"
        self.accept()

    def _on_force(self):
        self.choice = "commit"
        self.accept()


def confirm_security(parent, workspace: str, paths: list[str]) -> bool:
    """提交前扫描；返回 True 表示可以继续提交。"""
    if not workspace or not paths:
        return True
    issues = scan_files(workspace, paths)
    if not issues:
        return True

    blocks = [i for i in issues if i.level == "block"]
    if blocks:
        dlg = SecurityReportDialog(parent, issues)
        dlg.exec()
        if dlg.choice == "commit":
            return True
        if dlg.choice == "ignore":
            try:
                GitRepo(workspace).ignore([i.path for i in blocks])
                QMessageBox.information(
                    parent, "已加入 .gitignore",
                    "已把以下文件加入 .gitignore 并移出版本库：\n\n" + "\n".join(i.path for i in blocks),
                )
            except Exception as e:  # noqa: BLE001
                QMessageBox.warning(parent, "操作失败", str(e))
        return False

    text = "\n".join(f"• {i.path}：{i.reason}" for i in issues)
    reply = QMessageBox.question(
        parent, "提交提醒",
        "发现以下需要注意的内容：\n\n" + text + "\n\n仍要继续提交吗？",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return reply == QMessageBox.StandardButton.Yes


# ═════════════════════════════════════════════════════════════════
#  差异查看对话框
# ═════════════════════════════════════════════════════════════════

class DiffDialog(QDialog):
    def __init__(self, parent, title: str, content: str):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(760, 520)
        self.setStyleSheet("QDialog{background:#0d1117;}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        view = QTextBrowser()
        view.setStyleSheet(
            "QTextBrowser{background:#0d1117;border:1px solid #30363d;border-radius:6px;"
            "color:#c9d1d9;font-family:Consolas,'Cascadia Mono',monospace;font-size:12px;}"
        )
        view.setPlainText(content)
        lay.addWidget(view, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        btn = QPushButton("关闭")
        btn.setStyleSheet(
            "QPushButton{background:#21262d;border:1px solid #30363d;border-radius:4px;"
            "color:#c9d1d9;padding:6px 16px;}"
            "QPushButton:hover{background:#30363d;}"
        )
        btn.clicked.connect(self.accept)
        row.addWidget(btn)
        lay.addLayout(row)


# ═════════════════════════════════════════════════════════════════
#  源代码管理面板
# ═════════════════════════════════════════════════════════════════

_PANEL_STYLE = """
QWidget{background:#0d1117;color:#c9d1d9;}
QLabel{color:#8b949e;font-size:11px;}
QListWidget{background:#0d1117;border:1px solid #21262d;border-radius:4px;
    color:#c9d1d9;font-size:12px;}
QListWidget::item{padding:4px 6px;border-bottom:1px solid #161b22;}
QListWidget::item:selected{background:#1f6feb;color:#fff;}
QListWidget::item:hover{background:#161b22;}
QPlainTextEdit{background:#0d1117;border:1px solid #30363d;border-radius:4px;
    color:#c9d1d9;font-size:12px;padding:4px;}
QPlainTextEdit:focus{border:1px solid #58a6ff;}
QPushButton{background:#21262d;border:1px solid #30363d;border-radius:4px;
    color:#c9d1d9;font-size:11px;padding:4px 8px;}
QPushButton:hover{background:#30363d;}
QPushButton:disabled{background:#161b22;color:#484f58;}
"""

_STATUS_COLOR = {
    "M": "#f0883e",
    "A": "#3fb950",
    "D": "#f85149",
    "R": "#a371f7",
    "C": "#a371f7",
    "U": "#f85149",
    "?": "#58a6ff",
}


class SourceControlPanel(QWidget):
    """变更列表 + 提交信息 + 提交/推送 + 最近提交历史。"""

    status_changed = pyqtSignal(str)   # 用于主窗口刷新工具栏分支标签
    notify = pyqtSignal(str)           # 状态栏 / 输出区提示
    push_requested = pyqtSignal()      # 推送交给主窗口（复用远程仓库配置流程）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(_PANEL_STYLE)
        self._ws = ""
        self._repo = GitRepo("")
        self._files: list[dict] = []
        self._build_ui()

    # ── UI ──
    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)

        header = QHBoxLayout()
        self._branch_label = QLabel("🔀 —")
        self._branch_label.setStyleSheet("color:#58a6ff;font-size:12px;font-weight:600;")
        header.addWidget(self._branch_label)
        header.addStretch(1)
        self._btn_refresh = QPushButton("🔄")
        self._btn_refresh.setFixedSize(28, 24)
        self._btn_refresh.setToolTip("刷新状态")
        self._btn_refresh.clicked.connect(lambda: self.refresh(force=True))
        header.addWidget(self._btn_refresh)
        lay.addLayout(header)

        self._remote_label = QLabel("")
        self._remote_label.setStyleSheet("color:#484f58;font-size:10px;")
        self._remote_label.setWordWrap(True)
        lay.addWidget(self._remote_label)

        # 变更区
        self._changes_title = QLabel("更改 (0)")
        self._changes_title.setStyleSheet("color:#8b949e;font-size:11px;font-weight:600;")
        lay.addWidget(self._changes_title)

        self._list = QListWidget()
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_context_menu)
        self._list.itemDoubleClicked.connect(self._on_diff_item)
        lay.addWidget(self._list, 3)

        stage_row = QHBoxLayout()
        self._btn_stage_all = QPushButton("＋ 全部暂存")
        self._btn_stage_all.clicked.connect(self._stage_all)
        self._btn_unstage_all = QPushButton("－ 全部取消")
        self._btn_unstage_all.clicked.connect(self._unstage_all)
        stage_row.addWidget(self._btn_stage_all)
        stage_row.addWidget(self._btn_unstage_all)
        lay.addLayout(stage_row)

        # 提交信息
        self._msg = QPlainTextEdit()
        self._msg.setPlaceholderText("提交信息（Ctrl+Enter 提交）")
        self._msg.setFixedHeight(64)
        lay.addWidget(self._msg)
        # Ctrl+Enter 快速提交（QShortcut 才能收到输入框里的按键）
        _sc = QShortcut(QKeySequence("Ctrl+Return"), self._msg)
        _sc.activated.connect(self._commit)

        commit_row = QHBoxLayout()
        self._btn_commit = QPushButton("✅ 提交")
        self._btn_commit.setStyleSheet(
            "QPushButton{background:#238636;border:1px solid #2ea043;color:#fff;}"
            "QPushButton:hover{background:#2ea043;}"
            "QPushButton:disabled{background:#161b22;color:#484f58;border-color:#30363d;}"
        )
        self._btn_commit.clicked.connect(self._commit)
        self._btn_push = QPushButton("☁️ 推送")
        self._btn_push.clicked.connect(self.push_requested.emit)
        commit_row.addWidget(self._btn_commit, 1)
        commit_row.addWidget(self._btn_push)
        lay.addLayout(commit_row)

        # 历史
        hist_title = QLabel("最近提交")
        hist_title.setStyleSheet("color:#8b949e;font-size:11px;font-weight:600;")
        lay.addWidget(hist_title)
        self._history = QListWidget()
        self._history.itemDoubleClicked.connect(self._on_history_item)
        lay.addWidget(self._history, 2)

        self._btn_init = QPushButton("🔧 初始化 Git 仓库")
        self._btn_init.clicked.connect(self._init_repo)
        self._btn_init.setVisible(False)
        lay.addWidget(self._btn_init)

        self._hint = QLabel("")
        self._hint.setWordWrap(True)
        self._hint.setStyleSheet("color:#8b949e;font-size:11px;")
        lay.addWidget(self._hint)

    # ── 工作目录 ──
    def set_workspace(self, workspace: str):
        ws = (workspace or "").strip()
        if ws and os.path.normcase(os.path.normpath(ws)) == os.path.normcase(os.path.normpath(self._ws)):
            return
        self._ws = ws
        self._repo = GitRepo(ws)
        self.refresh(force=True)

    # ── 刷新 ──
    def refresh(self, force: bool = False):
        if not self._ws:
            self._list.clear()
            self._history.clear()
            self._branch_label.setText("🔀 —")
            self._remote_label.setText("")
            self._changes_title.setText("更改 (0)")
            self._hint.setText("请先在「文件」页选择项目目录。")
            self._btn_init.setVisible(False)
            return

        if not self._repo.available():
            self._list.clear()
            self._history.clear()
            self._branch_label.setText("🔀 非 Git 仓库")
            self._remote_label.setText(self._ws)
            self._changes_title.setText("更改 (0)")
            self._hint.setText("当前项目还不是 Git 仓库，点下面的按钮初始化。")
            self._btn_commit.setEnabled(False)
            self._btn_push.setEnabled(False)
            self._btn_init.setVisible(True)
            self.status_changed.emit("")
            return

        self._btn_init.setVisible(False)
        self._btn_commit.setEnabled(True)
        # 没配置远程也能点「推送」：主窗口会弹出仓库地址绑定向导
        self._btn_push.setEnabled(True)
        self._branch_label.setText(f"🔀 {self._repo.branch()}")
        url = self._repo.remote_url()
        self._remote_label.setText(f"☁ {url}" if url else "☁ 未配置远程仓库（点「推送」可绑定）")

        self._files = self._repo.status()
        self._list.clear()
        for f in self._files:
            x, y = f["index"], f["worktree"]
            flag = (x if x != " " else y).replace("?", "U")
            prefix = "●" if f["staged"] else "○"
            item = QListWidgetItem(f"{prefix} {flag}  {f['path']}")
            item.setData(Qt.ItemDataRole.UserRole, f)
            color = _STATUS_COLOR.get(flag, "#c9d1d9")
            item.setForeground(QColor(color if not f["staged"] else "#3fb950"))
            item.setToolTip(
                f"{f['path']}\n"
                + ("已暂存" if f["staged"] else ("未跟踪" if f["untracked"] else "未暂存"))
                + "\n双击查看差异"
            )
            self._list.addItem(item)

        staged = sum(1 for f in self._files if f["staged"])
        self._changes_title.setText(f"更改 ({len(self._files)}) · 已暂存 {staged}")
        self._hint.setText("" if self._files else "工作区干净，没有待提交的更改。")

        self._history.clear()
        for c in self._repo.log(20):
            item = QListWidgetItem(f"{c['hash']}  {c['message'][:40]}")
            item.setData(Qt.ItemDataRole.UserRole, c)
            item.setToolTip(f"{c['date']} · {c['author']}\n双击查看提交内容")
            item.setForeground(QColor("#8b949e"))
            self._history.addItem(item)

        ahead, behind = self._repo.ahead_behind()
        summary = self._repo.branch()
        if ahead or behind:
            summary = f"{summary} (+{ahead}/-{behind})"
        self.status_changed.emit(summary)

    def _init_repo(self):
        """给当前项目初始化 Git 仓库，并写入基础的 .gitignore。"""
        if not self._ws:
            return
        ok, err = self._repo.init()
        if not ok:
            QMessageBox.warning(self, "初始化失败", err or "未知错误")
            return
        GitRepo(self._ws).ignore([
            ".env", ".env.*", "__pycache__/", "*.pyc", ".venv/", "venv/",
            "node_modules/", "dist/", "build/", ".idea/", ".vscode/",
        ])
        self.notify.emit("🔧 已初始化 Git 仓库，并写入基础 .gitignore")
        self.refresh(force=True)

    # ── 暂存操作 ──
    def _selected_paths(self) -> list[str]:
        paths = []
        for it in self._list.selectedItems():
            data = it.data(Qt.ItemDataRole.UserRole) or {}
            if data.get("path"):
                paths.append(data["path"])
        return paths

    def _stage_all(self):
        if not self._repo.available():
            return
        self._repo.stage_all()
        self.refresh(force=True)

    def _unstage_all(self):
        if not self._repo.available():
            return
        paths = [f["path"] for f in self._files if f["staged"]]
        self._repo.unstage(paths)
        self.refresh(force=True)

    def _stage_selected(self):
        paths = self._selected_paths()
        self._repo.stage(paths)
        self.refresh(force=True)

    def _unstage_selected(self):
        paths = self._selected_paths()
        self._repo.unstage(paths)
        self.refresh(force=True)

    # ── 右键菜单 ──
    def _on_context_menu(self, pos):
        item = self._list.itemAt(pos)
        if item is None:
            return
        data = item.data(Qt.ItemDataRole.UserRole) or {}
        path = data.get("path", "")
        if not path:
            return
        menu = QMenu(self)
        act_diff = menu.addAction("🔍 查看差异")
        act_stage = menu.addAction("＋ 暂存该文件")
        act_unstage = menu.addAction("－ 取消暂存")
        menu.addSeparator()
        act_reveal = menu.addAction("🗂️ 在资源管理器中显示")
        act_ignore = menu.addAction("🚫 加入 .gitignore")
        chosen = menu.exec(self._list.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is act_diff:
            self._show_diff(path, bool(data.get("staged")))
        elif chosen is act_stage:
            self._repo.stage([path])
            self.refresh(force=True)
        elif chosen is act_unstage:
            self._repo.unstage([path])
            self.refresh(force=True)
        elif chosen is act_reveal:
            self._reveal(path)
        elif chosen is act_ignore:
            self._repo.ignore([path])
            self.refresh(force=True)
            self.notify.emit(f"🚫 已加入 .gitignore: {path}")

    def _reveal(self, rel_path: str):
        full = os.path.normpath(os.path.join(self._ws, rel_path))
        try:
            if os.name == "nt":
                if os.path.exists(full):
                    subprocess.Popen(["explorer", "/select,", full])
                else:
                    subprocess.Popen(["explorer", os.path.dirname(full)])
        except Exception:
            pass

    # ── 差异 / 历史 ──
    def _on_diff_item(self, item):
        data = item.data(Qt.ItemDataRole.UserRole) or {}
        path = data.get("path")
        if path:
            self._show_diff(path, bool(data.get("staged")))

    def _show_diff(self, path: str, staged: bool = False):
        text = self._repo.diff(path, staged=staged)
        if not text.strip() or text == "(没有差异)":
            text = self._repo.diff(path, staged=not staged)
        DiffDialog(self, f"差异 — {path}", text).exec()

    def _on_history_item(self, item):
        data = item.data(Qt.ItemDataRole.UserRole) or {}
        h = data.get("hash")
        if h:
            DiffDialog(self, f"提交 {h}", self._repo.show(h)).exec()

    # ── 提交 ──
    def _commit(self):
        if not self._repo.available():
            return
        message = self._msg.toPlainText().strip()
        if not message:
            QMessageBox.information(self, "提交", "请先填写提交信息。")
            self._msg.setFocus()
            return

        staged = [f["path"] for f in self._files if f["staged"]]
        if staged:
            paths = staged
        else:
            # 没有暂存任何文件 → 暂存全部（与工具栏「保存」行为一致）
            self._repo.stage_all()
            self._files = self._repo.status()
            paths = [f["path"] for f in self._files]
        if not paths:
            QMessageBox.information(self, "提交", "没有需要提交的更改。")
            return

        # 🔒 提交前安全检查
        if not confirm_security(self, self._ws, paths):
            self.refresh(force=True)
            return

        result = self._repo.commit(message)
        if not result.get("success"):
            QMessageBox.warning(self, "提交失败", str(result.get("error", ""))[:1500])
            return
        files = result.get("files", len(paths))
        h = result.get("hash", "")
        if files:
            self.notify.emit(f"✅ 已提交 {h}（{files} 个文件）")
        else:
            self.notify.emit("✅ 没有新的变更需要提交")
        self._msg.clear()
        self.refresh(force=True)
