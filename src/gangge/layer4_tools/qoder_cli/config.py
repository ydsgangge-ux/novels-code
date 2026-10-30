"""Qoder CLI（CN 版）的配置与档位定义。

Qoder CLI 在本项目里被当成**一个综合型委派工具**用：一次调用 = 一个可验证的
工作闭环。因此这里只负责三件事：

* 可执行文件在哪（CN 版装在 ``~/.qoder-cn/bin/qoderclicn/qoderclicn.exe``）；
* 调用者能调的三个旋钮（模型 / 思考强度 / 权限）怎么翻译成 argv；
* 超时、工作区这类默认值。

参数以 **qoderclicn v1.1.80 的 ``--help`` 实测为准**，不是照抄文档：
官方文档写的 ``--model ultimate`` 那套档位别名和 ``--max-turns`` 在 CN 版里都
不存在，真实可用的是 ``--model Auto`` 这类模型名 + ``--reasoning-effort``。
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, replace
from pathlib import Path

logger = logging.getLogger(__name__)

#: CN 版装出来的名字是 ``qoderclicn``，国际版是 ``qodercli``。
EXE_CANDIDATES: tuple[str, ...] = ("qoderclicn", "qodercli", "qoder", "qoder-cn")

DEFAULT_TIMEOUT_S = 600
DEFAULT_MODEL = "Auto"

#: ``--list-models`` 实测结果（v1.1.80）。前端下拉可编辑，允许手填新模型名。
MODELS: tuple[str, ...] = (
    "Auto",
    "Fast",
    "Qwen3.8-Max",
    "Qwen3.8-Flash",
    "DeepSeek-V4-Pro",
    "DeepSeek-Flash",
    "GLM-5.3",
    "GLM-5.3-Flash",
    "Kimi-K3.1-Pro",
    "Kimi-K2.8-Preview",
    "MiniMax-M3.1-Flash",
)

#: ``--reasoning-effort`` 的合法取值（非法值触发校验，从报错里读出来的）。
EFFORTS: dict[str, str] = {
    "auto": "auto — 由服务端决定",
    "none": "none — 不思考",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
    "ultracode": "ultracode — 最狠（贵）",
}

#: "强度"落地成 ``--reasoning-effort`` + 输出上限。
STRENGTHS: dict[str, dict[str, object]] = {
    "low": {"label": "低 — 简单改动，最省", "effort": "low", "max_output_tokens": "16k"},
    "medium": {"label": "中 — 默认，日常任务", "effort": "medium", "max_output_tokens": "32k"},
    "high": {"label": "高 — 复杂改造", "effort": "high", "max_output_tokens": "32k"},
    "max": {"label": "极高 — 架构级/长任务（贵）", "effort": "max", "max_output_tokens": "32k"},
}

#: 权限档位。headless 模式下**没人应答确认框**，权限必须预先声明：
#: 不声明就等于"任何需要确认的操作自动拒绝"。
PERMISSIONS: dict[str, dict[str, object]] = {
    "analyze": {
        "label": "只读分析 — 不改文件",
        "permission_mode": None,
        "allowed_tools": "Read,Grep,Glob",
        "writes": False,
    },
    "edit": {
        "label": "限定编辑 — 可改代码，禁止跑命令",
        "permission_mode": "accept_edits",
        "allowed_tools": "Read,Grep,Glob,Write,Edit",
        "writes": True,
    },
    "full": {
        "label": "完全放行 — 可执行命令（高风险）",
        "permission_mode": "bypass_permissions",
        "allowed_tools": "",
        "writes": True,
    },
}


class QoderNotAvailable(RuntimeError):
    """CLI 没装 / 没登录，无法委派。"""


def _suffixes() -> tuple[str, ...]:
    """Windows 上装出来的是 ``qoderclicn.exe``，用户手填路径时常常漏后缀。"""
    if os.name != "nt":
        return ("",)
    return (".exe", ".cmd", ".bat", "")


def _extra_search_dirs() -> list[Path]:
    """PATH 之外的常见安装位置。

    ``~/.qoder-cn/bin/qoderclicn`` 是 CN 版 installer 的实际落点；npm 全局
    目录和 ``~/.qoder/bin`` 覆盖国际版与 npm 安装。
    """
    home = Path.home()
    dirs: list[Path] = [
        home / ".qoder-cn" / "bin" / "qoderclicn",
        home / ".qoder-cn" / "bin",
        home / ".qoder" / "bin",
        home / ".local" / "bin",
        home / "scoop" / "shims",
    ]
    appdata = os.environ.get("APPDATA", "")
    local = os.environ.get("LOCALAPPDATA", "")
    if appdata:
        dirs.append(Path(appdata) / "npm")
    if local:
        dirs.append(Path(local) / "Programs" / "Qoder")
        dirs.append(Path(local) / "Programs" / "Qoder" / "bin")
        dirs.append(Path(local) / "pnpm")
    return [d for d in dirs if d.is_dir()]


def resolve_executable(configured: str = "") -> str:
    """把配置里的命令解析成真实路径；解析不到抛 :class:`QoderNotAvailable`。"""
    names: list[str] = []
    if configured.strip():
        names.append(configured.strip())
    names.extend(EXE_CANDIDATES)

    for name in names:
        # 用户填了路径（含分隔符或盘符）时 which 不会查，直接按后缀试
        looks_like_path = os.sep in name or "/" in name or (os.name == "nt" and ":" in name)
        if looks_like_path:
            # "" 在候选后缀里，所以已经带后缀的路径也能命中
            for suffix in _suffixes():
                cand = Path(name + suffix)
                if cand.is_file():
                    return str(cand)
            continue
        resolved = shutil.which(name)
        if resolved:
            return resolved

    for directory in _extra_search_dirs():
        for exe in EXE_CANDIDATES:
            for suffix in _suffixes():
                cand = directory / f"{exe}{suffix}"
                if cand.is_file():
                    logger.debug("[qoder] 在 PATH 外找到 CLI: %s", cand)
                    return str(cand)

    raise QoderNotAvailable(
        "找不到 Qoder CLI。已查 PATH、~/.qoder-cn/bin/qoderclicn、npm 全局目录。\n"
        "CN 版安装：irm https://qoder.com.cn/install.ps1 | iex\n"
        "若已安装，把完整路径填到 设置 → Qoder CLI → 可执行文件，或设 QODER_CLI_PATH。"
    )


@dataclass
class QoderConfig:
    """一次委派的可调参数。空值一律回落到强度预设。"""

    executable: str = ""
    model: str = ""  # 空 = DEFAULT_MODEL
    strength: str = "medium"
    permission: str = "edit"
    max_output_tokens: str = ""  # 空 = 跟随强度
    timeout_s: int = 0  # 0 = DEFAULT_TIMEOUT_S
    #: 仅当 CLI 支持 ``--max-turns`` 时才下发（v1.1.80 不支持，靠 timeout 兜底）
    max_turns: int = 0
    workspace: Path = Path(".")
    enabled: bool = True

    # ── 派生值 ──────────────────────────────────────────────
    def preset(self) -> dict[str, object]:
        return STRENGTHS.get(self.strength, STRENGTHS["medium"])

    @property
    def model_name(self) -> str:
        return self.model or DEFAULT_MODEL

    @property
    def effort(self) -> str:
        return str(self.preset()["effort"])

    @property
    def tokens(self) -> str:
        return self.max_output_tokens or str(self.preset()["max_output_tokens"])

    @property
    def timeout(self) -> int:
        return self.timeout_s or DEFAULT_TIMEOUT_S

    @property
    def permission_spec(self) -> dict[str, object]:
        return PERMISSIONS.get(self.permission, PERMISSIONS["edit"])

    @property
    def can_write(self) -> bool:
        return bool(self.permission_spec["writes"])

    def resolved_executable(self) -> str:
        return resolve_executable(self.executable)

    # ── 构造 ────────────────────────────────────────────────
    @classmethod
    def from_env(cls, workspace: str | Path | None = None) -> QoderConfig:
        """从 ``QODER_*`` 环境变量读取（GUI 会把这些写进 .env）。"""

        def _env(name: str, default: str = "") -> str:
            return (os.environ.get(name) or default).strip()

        def _int(name: str, default: int = 0) -> int:
            try:
                return int(_env(name) or default)
            except ValueError:
                return default

        ws = Path(workspace or _env("WORKSPACE_DIR") or os.getcwd())
        strength = _env("QODER_STRENGTH", "medium").lower()
        permission = _env("QODER_PERMISSION", "edit").lower()
        enabled = _env("QODER_ENABLED", "true").lower() not in ("0", "false", "no", "off")
        return cls(
            executable=_env("QODER_CLI_PATH"),
            model=_env("QODER_MODEL"),
            strength=strength if strength in STRENGTHS else "medium",
            permission=permission if permission in PERMISSIONS else "edit",
            max_output_tokens=_env("QODER_MAX_OUTPUT_TOKENS"),
            timeout_s=_int("QODER_TIMEOUT_S"),
            max_turns=_int("QODER_MAX_TURNS"),
            workspace=ws,
            enabled=enabled,
        )

    def with_workspace(self, workspace: str | Path) -> QoderConfig:
        return replace(self, workspace=Path(workspace))


def auth_token() -> str:
    """CI / 无登录态场景用的 Access Token（本机已登录时可为空）。"""
    return (os.environ.get("QODER_PERSONAL_ACCESS_TOKEN") or "").strip()


def _run_cli(exe: str, args: list[str], timeout_s: int = 20) -> tuple[int, str]:
    import subprocess

    try:
        proc = subprocess.run(
            [exe, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, f"启动失败: {type(exc).__name__}: {exc}"
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode, out


def probe_version(executable: str = "", timeout_s: int = 20) -> tuple[bool, str]:
    """``--version`` 探测，给设置页的"检测"按钮用。返回 (是否可用, 版本或错误)。"""
    try:
        exe = resolve_executable(executable)
    except QoderNotAvailable as exc:
        return False, str(exc)
    code, out = _run_cli(exe, ["--version"], timeout_s)
    first = out.splitlines()[0] if out else ""
    if code != 0:
        return False, first or f"退出码 {code}"
    return True, first or exe


_FLAG_CACHE: dict[str, frozenset[str]] = {}


def help_flags(executable: str, timeout_s: int = 20) -> frozenset[str]:
    """解析 ``--help`` 里出现的长参数名（结果缓存）。

    参数会随版本漂移：文档说有 ``--max-turns``，v1.1.80 实测没有。与其相信
    文档，不如每次按真实 help 过滤 argv —— 传一个不认识的 flag 会让 CLI 直接
    报错退出，等于整个委派失败。
    """
    if executable in _FLAG_CACHE:
        return _FLAG_CACHE[executable]
    import re

    code, out = _run_cli(executable, ["--help"], timeout_s)
    flags = frozenset(re.findall(r"--[a-z0-9][a-z0-9-]+", out)) if code == 0 or out else frozenset()
    _FLAG_CACHE[executable] = flags
    return flags


def list_models(executable: str = "", timeout_s: int = 30) -> tuple[bool, list[str] | str]:
    """``--list-models``：确认登录态的同时拿到当前账号可用的模型名。"""
    try:
        exe = resolve_executable(executable)
    except QoderNotAvailable as exc:
        return False, str(exc)
    code, out = _run_cli(exe, ["--list-models"], timeout_s)
    if code != 0:
        return False, out or f"退出码 {code}"
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    models = [ln for ln in lines if ln.lower() != "model"]
    return (True, models) if models else (False, "没有返回任何模型（可能未登录）")
