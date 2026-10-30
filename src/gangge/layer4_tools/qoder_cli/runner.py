"""执行委派：起进程、收结果、验范围、跑 completion checks。

设计上只做"一次性 headless 调用"（``qodercli -p ... -o json``），因为项目没有
worker 基础设施（``AgenticLoop`` 是单循环 + 线程池），硬造常驻协议层不划算。
长任务靠 ``timeout_s`` 兜底，取消能力留到以后。

结果**必须裁剪**：远端的过程输出动辄几万 token，主 agent 只该看到结论。完整
输出落盘到 ``<workspace>/.gangge/logs/qoder/``，需要细节时按路径去读。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gangge.layer4_tools.qoder_cli.config import (
    QoderConfig,
    QoderNotAvailable,
    auth_token,
    help_flags,
)
from gangge.layer4_tools.qoder_cli.spec import CheckSpec, DelegateSpec

logger = logging.getLogger(__name__)

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
SUMMARY_LIMIT = 300
LOG_TAIL = 8000  # 落盘时保留的输出上限（字符）

SUMMARY_KEYS = ("result", "summary", "response", "message", "text", "output", "content")


@dataclass
class CheckOutcome:
    cmd: str
    exit_code: int
    expect_exit: int = 0
    tail: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == self.expect_exit

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"cmd": self.cmd, "exit": self.exit_code}
        if self.exit_code != self.expect_exit:
            d["expect"] = self.expect_exit
            d["tail"] = self.tail
        return d


@dataclass
class DelegationResult:
    status: str  # done | partial | violated | failed
    summary: str = ""
    files_changed: list[str] = field(default_factory=list)
    scope_violations: list[str] = field(default_factory=list)
    checks: list[CheckOutcome] = field(default_factory=list)
    exit_code: int = 0
    turns: int = 0  # 远端用掉多少轮（贴近上限说明任务可能没做完）
    workspace: str = ""  # 实际生效的工作区（出错时第一件要确认的事）
    duration_s: float = 0.0
    log_path: str = ""
    error: str = ""
    argv: list[str] = field(default_factory=list)

    @property
    def is_error(self) -> bool:
        return self.status == "failed"

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "status": self.status,
            "summary": self.summary,
            "files_changed": self.files_changed[:40],
            "checks": [c.to_dict() for c in self.checks],
            "duration_s": round(self.duration_s, 1),
            "exit_code": self.exit_code,
        }
        if self.turns:
            d["turns"] = self.turns
        if self.workspace:
            d["workspace"] = self.workspace
        if self.scope_violations:
            d["violations"] = self.scope_violations[:20]
        if self.log_path:
            d["log"] = self.log_path
        if self.error:
            d["error"] = self.error
        return d

    def for_model(self, limit: int = 1400) -> str:
        """紧凑 JSON，给模型看的最终形态。"""
        text = json.dumps(self.to_dict(), ensure_ascii=False)
        return clip(text, limit)


# ── 纯函数（便于单测） ───────────────────────────────────────


def clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head - 20
    return f"{text[:head]}…[中略 {len(text) - head - tail} 字]…{text[-tail:]}"


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text or "")


def build_argv(
    cfg: QoderConfig,
    spec: DelegateSpec,
    executable: str | None = None,
    supported: frozenset[str] | None = None,
) -> list[str]:
    """把配置 + spec 翻译成 argv。不做 IO，方便断言。

    ``supported`` 是 ``--help`` 里实际存在的 flag 集合；只对**可选**参数做过滤
    （如 ``--max-turns``），核心参数缺了就该报错，不该静默降级。
    """
    exe = executable or cfg.resolved_executable()
    # analyze 一律按只读跑：即使设置里给了可写权限，也不该让"看一眼"的任务动文件
    permission = "analyze" if spec.mode == "analyze" else cfg.permission
    perm = cfg.permission_spec if permission == cfg.permission else _permission(permission)

    # 只有"完全放行"档才真的能跑命令；其余档位别让 prompt 要求它自验
    can_run = perm["permission_mode"] == "bypass_permissions"

    argv = [
        exe,
        "-p", spec.prompt(can_run_commands=can_run),
        "-o", "json",
        "-w", str(Path(cfg.workspace).resolve()),
        "-m", cfg.model_name,
        "--reasoning-effort", cfg.effort,
        "--max-output-tokens", cfg.tokens,
    ]

    # 一次性委派不该污染会话历史（否则 --resume 会捡到半截任务）
    if supported is None or "--no-session-persistence" in supported:
        argv.append("--no-session-persistence")

    turns = spec.max_turns or cfg.max_turns
    if turns and (supported is None or "--max-turns" in supported):
        argv += ["--max-turns", str(turns)]

    if perm["permission_mode"]:
        argv += ["--permission-mode", str(perm["permission_mode"])]
    if perm["allowed_tools"]:
        argv += ["--allowed-tools", str(perm["allowed_tools"])]
    return argv


def _permission(name: str) -> dict[str, object]:
    from gangge.layer4_tools.qoder_cli.config import PERMISSIONS

    return PERMISSIONS.get(name, PERMISSIONS["edit"])


def parse_output(stdout: str) -> tuple[str, dict[str, Any]]:
    """从 ``-o json`` 的产物里抠出结论。

    ``-o json`` 的字段文档没写死，所以这里做兼容：单个对象、对象数组
    （stream-json 退化成一行一事件）都吃，找不到已知键就退回纯文本。
    """
    text = strip_ansi(stdout).strip()
    if not text:
        return "", {}

    payload: dict[str, Any] = {}
    candidate: Any = None
    try:
        candidate = json.loads(text)
    except json.JSONDecodeError:
        for line in reversed(text.splitlines()):
            line = line.strip()
            if not line.startswith(("{", "[")):
                continue
            try:
                candidate = json.loads(line)
                break
            except json.JSONDecodeError:
                continue

    if isinstance(candidate, list):
        for item in reversed(candidate):
            if isinstance(item, dict):
                candidate = item
                break
    if isinstance(candidate, dict):
        payload = candidate
        for key in SUMMARY_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return clip(value.strip(), SUMMARY_LIMIT), payload
        # 有 JSON 但没有已知结论字段：把整个对象压成一行当摘要
        return clip(json.dumps(payload, ensure_ascii=False), SUMMARY_LIMIT), payload

    return clip(text, SUMMARY_LIMIT), {}


def shell_argv(cmd: str) -> list[str]:
    """验收命令用哪个 shell 跑 —— 与项目的 ``bash`` 工具保持一致。

    Windows 上原先用 ``shell=True``（cmd.exe），而项目其它地方都用 PowerShell。
    壳不一致的后果很具体：模型照 Linux 习惯写 ``test -f index.html``，
    cmd 里根本没有 ``test``，于是一个完全正确的产出被判成 partial。
    """
    if os.name == "nt":
        wrapped = (
            "chcp 65001 | Out-Null; "
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; " + cmd
        )
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", wrapped]
    return ["bash", "-c", cmd]


def _run_check(check: CheckSpec, cwd: Path) -> CheckOutcome:
    try:
        proc = subprocess.run(
            shell_argv(check.cmd),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=check.timeout_s,
        )
    except subprocess.TimeoutExpired:
        return CheckOutcome(check.cmd, -1, check.expect_exit, f"[超时] 超过 {check.timeout_s}s")
    except OSError as exc:
        return CheckOutcome(check.cmd, -2, check.expect_exit, f"[错误] {exc}")
    tail = strip_ansi(((proc.stdout or "") + (proc.stderr or "")).strip())
    return CheckOutcome(check.cmd, proc.returncode, check.expect_exit, clip(tail, 400))


def _git(ws: Path, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(ws), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return -1, ""
    # 只去掉结尾换行：``git status --porcelain`` 的 " M path" 首位是有效信息
    # （索引区状态），整体 strip() 会把路径前移一位、解析成 ".py" 这种垃圾。
    return proc.returncode, (proc.stdout or "").rstrip("\n")


@dataclass
class WorktreeSnapshot:
    """委派前/后的工作区状态，用来算**本次**改了什么。

    为什么不能用 ``git diff HEAD``：那会把工作区本来就有的未提交改动一并算进
    来。开发中跑一次只读委派，就会把几十个"自己改的文件"当成越界，误判
    ``violated`` 并害人回滚。只有差集才是"这次委派造成的改动"。
    """

    status: dict[str, str] = field(default_factory=dict)  # path -> porcelain XY
    hashes: dict[str, str] = field(default_factory=dict)  # path -> blob sha
    is_repo: bool = False


def _hash_file(ws: Path, path: str) -> str:
    code, out = _git(ws, "hash-object", "--", path)
    return out if code == 0 else ""


def snapshot_worktree(ws: Path) -> WorktreeSnapshot:
    """记录 git 已报告改动的文件及其内容哈希（不做全仓遍历）。"""
    code, top = _git(ws, "rev-parse", "--show-toplevel")
    if code != 0 or not top:
        return WorktreeSnapshot(is_repo=False)

    code, out = _git(ws, "status", "--porcelain=v1")
    if code != 0:
        return WorktreeSnapshot(is_repo=False)

    status: dict[str, str] = {}
    for line in out.splitlines():
        if len(line) < 4:
            continue
        code_xy, rest = line[:2], line[3:]
        # 重命名显示为 "old -> new"，只关心新路径
        path = rest.split(" -> ", 1)[-1].strip().strip('"')
        if path:
            status[path] = code_xy
    hashes = {p: _hash_file(ws, p) for p in status}
    return WorktreeSnapshot(status=status, hashes=hashes, is_repo=True)


def changed_between(pre: WorktreeSnapshot, post: WorktreeSnapshot) -> list[str]:
    """本次委派造成的改动：新增、被删、状态变化、或内容变化。"""
    changed: set[str] = set()
    for path, code in post.status.items():
        if path not in pre.status or pre.status[path] != code:
            changed.add(path)
        elif post.hashes.get(path) != pre.hashes.get(path):
            # 本来就脏、这次又被改：状态码不变，只能靠内容哈希发现
            changed.add(path)
    for path in pre.status:
        if path not in post.status:
            changed.add(path)
    return sorted(changed)


def _write_log(ws: Path, argv: list[str], stdout: str, stderr: str,
               spec: DelegateSpec, result: DelegationResult) -> str:
    try:
        log_dir = Path(ws) / ".gangge" / "logs" / "qoder"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = log_dir / f"{stamp}-{os.getpid()}.json"
        path.write_text(
            json.dumps(
                {
                    "argv": argv,
                    "goal": spec.goal,
                    "mode": spec.mode,
                    "status": result.status,
                    "exit_code": result.exit_code,
                    "duration_s": round(result.duration_s, 2),
                    "stdout": clip(strip_ansi(stdout), LOG_TAIL),
                    "stderr": clip(strip_ansi(stderr), LOG_TAIL),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return str(path)
    except OSError as exc:  # 日志失败不该让委派失败
        logger.debug("[qoder] 日志写入失败: %s", exc)
        return ""


def _explain_failure(stderr: str, exit_code: int) -> str:
    low = (stderr or "").lower()
    if "not logged in" in low or "unauthorized" in low or "401" in low:
        return "未登录或无权限：先在本机跑 qodercli /login，或设置 QODER_PERSONAL_ACCESS_TOKEN"
    if "permission" in low and "den" in low:
        return "权限被拒绝：headless 下需预先声明权限（设置页把权限调到「限定编辑」）"
    if "max turns" in low or "max_turns" in low:
        return "达到最大轮数上限：任务太大，拆小或调高强度"
    return clip(strip_ansi(stderr or ""), 300) or f"退出码 {exit_code}"


# ── 主流程 ──────────────────────────────────────────────────

_GLOB_CHARS = ("*", "?", "[")


def preflight(spec: DelegateSpec, ws: Path) -> list[str]:
    """委派前的落点检查：工作区选错是最贵的一类错误。

    在错误的工作区里跑，"给 tests/x.py 加测试"会被执行成"新建一个空
    tests/x.py"——远端说完成了，本地验收也在错的目录里跑，两边都"成功"，
    实际什么也没做成。所以 scope 的路径一个都对不上时，先停下来报错。

    只判父目录是否存在（而不是文件本身）：新建文件是合法任务，但父目录不存在
    基本说明工作区搞错了。
    """
    if not ws.is_dir():
        return [f"工作区不存在: {ws}"]

    concrete = [p for p in spec.scope if not any(c in p for c in _GLOB_CHARS)]
    if not concrete:
        return []
    missing = [p for p in concrete if not (ws / p).parent.is_dir()]
    if len(missing) == len(concrete):
        return [
            "scope 里的路径在工作区中一个都找不到（父目录都不存在）: "
            + ", ".join(missing[:5])
            + f"｜工作区 = {ws}"
            + "｜请确认工作区是否正确（GUI 默认是 gangge_projects/project_*）"
        ]
    return []


def delegate_sync(
    spec: DelegateSpec,
    cfg: QoderConfig | None = None,
    *,
    executable: str | None = None,
) -> DelegationResult:
    cfg = cfg or QoderConfig.from_env()
    ws = Path(cfg.workspace)
    started = time.monotonic()

    problems = preflight(spec, ws)
    if problems:
        return DelegationResult(
            status="failed",
            error="；".join(problems),
            workspace=str(ws),
            duration_s=time.monotonic() - started,
        )

    try:
        exe = executable or cfg.resolved_executable()
        # 参数随版本漂移，按真实 --help 过滤可选 flag（结果有缓存）
        supported = help_flags(exe)
        argv = build_argv(cfg, spec, executable=exe, supported=supported)
    except QoderNotAvailable as exc:
        return DelegationResult(status="failed", error=str(exc), duration_s=0.0)

    env = dict(os.environ)
    token = auth_token()
    if token:
        env["QODER_PERSONAL_ACCESS_TOKEN"] = token

    # 跑之前拍一次快照：工作区本来就有的未提交改动不能算到这次委派头上
    pre = snapshot_worktree(ws)

    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(ws),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        try:
            stdout, stderr = proc.communicate(timeout=spec.timeout_s or cfg.timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            timed_out = True
        exit_code = proc.returncode
    except FileNotFoundError:
        return DelegationResult(
            status="failed",
            error="找不到 Qoder CLI 可执行文件（npm i -g @qoder-ai/qodercli）",
            duration_s=time.monotonic() - started,
        )
    except OSError as exc:
        return DelegationResult(
            status="failed",
            error=f"启动失败: {type(exc).__name__}: {exc}",
            duration_s=time.monotonic() - started,
        )

    result = DelegationResult(status="done", exit_code=exit_code, argv=argv)
    result.workspace = str(ws)
    result.duration_s = time.monotonic() - started

    if timed_out:
        result.status = "failed"
        result.error = f"[超时] 超过 {spec.timeout_s or cfg.timeout}s，已终止"
        result.summary = clip(strip_ansi((stdout or "")[-1000:]), SUMMARY_LIMIT)

    # 范围合规：改动越界比"没做完"更严重，优先判 violated。
    # 用前后快照的差集，而不是 git diff HEAD —— 后者会把工作区原有的未提交
    # 改动当成这次委派的结果，导致误判 violated + 误导人回滚别人的改动。
    post = snapshot_worktree(ws)
    changed = changed_between(pre, post)
    result.files_changed = changed
    if spec.mode != "analyze":
        if post.is_repo:
            result.scope_violations = spec.violations(changed)
            if result.scope_violations:
                result.status = "violated"
        elif spec.scope:
            result.error = result.error or "工作区不是 git 仓库，无法校验改动范围"

    # 跑 completion checks（由主 agent 复核，不信远端自述）
    if spec.dod:
        result.checks = [_run_check(c, ws) for c in spec.dod]

    # 结论：先取远端自述，再被机器判定覆盖
    summary, payload = parse_output(stdout or "")
    if not result.summary:
        result.summary = summary
    if payload:
        # 实测结构：{"type":"result","subtype":"success","is_error":false,"num_turns":3,...}
        # is_error 比退出码可靠：CLI 自己出错时仍可能退出 0。
        try:
            result.turns = int(payload.get("num_turns") or 0)
        except (TypeError, ValueError):
            result.turns = 0
        subtype = str(payload.get("subtype") or "")
        if payload.get("is_error") or (subtype and subtype != "success"):
            if result.status == "done":
                result.status = "failed"
                result.error = result.error or f"远端自报失败（subtype={subtype or 'error'}）"
    if not timed_out and exit_code != 0 and result.status == "done":
        result.status = "failed"
        result.error = _explain_failure(stderr or "", exit_code)

    if result.status in ("done", "partial") and result.checks:
        failed = [c for c in result.checks if not c.ok]
        if failed:
            result.status = "partial"
            first = failed[0]
            result.error = f"验收未全绿: {first.cmd} 退出码 {first.exit_code}（期望 {first.expect_exit}）"
            if first.tail:
                result.summary = clip(f"{result.summary} | {first.tail}", SUMMARY_LIMIT)

    result.log_path = _write_log(ws, argv, stdout or "", stderr or "", spec, result)
    logger.info(
        "[qoder] %s status=%s exit=%s %.1fs changed=%d",
        spec.mode, result.status, exit_code, result.duration_s, len(changed),
    )
    return result


async def delegate(
    spec: DelegateSpec,
    cfg: QoderConfig | None = None,
    *,
    executable: str | None = None,
) -> DelegationResult:
    """异步入口：阻塞的进程等待丢给默认线程池（同 mcp_proxy 的做法）。"""
    return await asyncio.to_thread(delegate_sync, spec, cfg, executable=executable)
