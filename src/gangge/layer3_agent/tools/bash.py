"""Bash tool — execute shell commands with streaming output.

Uses threaded reading + merged stderr to avoid Windows pipe deadlock.
"""

from __future__ import annotations

import asyncio
import http.client
import logging
import os
import platform
import re
import signal
import subprocess
import threading
import time
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any

from gangge.layer3_agent.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

#: Commands started by this process that have not exited yet, grouped by the
#: workspace (task) that started them. A server the agent launched should not
#: outlive the task that needed it — but it also must not be killed by a
#: *different* task ending. A flat set made that distinction impossible: with
#: two tasks running, the first one to finish killed the second one's dev
#: server, and the second task then reported "server died" for no reason.
_TRACKED_PROCS: dict[str, set[subprocess.Popen]] = {}
_TRACK_LOCK = threading.Lock()

#: Scope used when a caller cannot say which task a command belongs to
#: (single-task hosts, tests, CLI). Keeps the old all-or-nothing behaviour.
_DEFAULT_SCOPE = ""


def _norm_scope(scope: str | None) -> str:
    """One canonical key per workspace, so ``.`` and its absolute path agree."""
    if not scope:
        return _DEFAULT_SCOPE
    try:
        return os.path.abspath(scope)
    except Exception:  # noqa: BLE001 - a weird path must not break tracking
        return scope


def _track(scope: str, proc: subprocess.Popen) -> None:
    with _TRACK_LOCK:
        _TRACKED_PROCS.setdefault(_norm_scope(scope), set()).add(proc)


def _untrack(scope: str, proc: subprocess.Popen) -> None:
    key = _norm_scope(scope)
    with _TRACK_LOCK:
        bucket = _TRACKED_PROCS.get(key)
        if bucket is not None:
            bucket.discard(proc)
            if not bucket:
                _TRACKED_PROCS.pop(key, None)

#: 命令超时的默认值与上限。
#:
#: 默认 120 秒对普通命令足够；模型偶尔会传 ``timeout: "5000"``（83 分钟），
#: 于是 ``reader.join(5000)`` 把整个任务挂死——那次 ``python -m http.server``
#: 就是这么把任务拖了 36 分钟还没结果的。任何"最多等多久"的参数都必须有
#: 上限，否则它会被当成"要等多久"。
DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 600
#: 连续这么久没有任何输出 → 判定"卡住了"，转后台继续任务（验证/构建命令除外）。
IDLE_DETACH_SECONDS = 90

#: 常驻进程：启动成功后**不会退出**，前台等待等价于等死。
#: 注意只匹配"服务/开发服务器"，``npm test`` / ``npm run build`` 不会被误伤。
_SERVICE_CMD_RE = re.compile(
    r"(?:^|[\s;&|(])(?:"
    r"python[0-9.]*\s+-m\s+http\.server|"
    r"python[0-9.]*\s+-m\s+flask\s+run|"
    r"(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:start|dev|serve)|"
    r"npx\s+(?:vite|next\s+dev|nuxt\s+dev|serve)|"
    r"(?:next|nuxt|vite)\s+dev|"
    r"uvicorn|gunicorn|hypercorn|"
    r"flask\s+run|"
    r"manage\.py\s+runserver|"
    r"php\s+-S\b|php\s+artisan\s+serve|"
    r"[a-z0-9_.-]*spring-boot:run|"
    r"rails\s+(?:server|s)|"
    r"streamlit\s+run|"
    r"mkdocs\s+serve|"
    r"hugo\s+(?:server|serve)|"
    r"jekyll\s+serve|"
    r"docker\s+compose\s+up(?!\s+-d)|"
    r"docker-compose\s+up(?!\s+-d)"
    r")",
    re.IGNORECASE,
)

#: 命令自己已经把进程丢到后台了，不要再包一层。
_ALREADY_BACKGROUND_RE = re.compile(
    r"\bStart-Process\b|\bnohup\b|\bstart\s+/B\b|\bsetsid\b|"
    r"docker\s+compose\s+up\s+-d|docker-compose\s+up\s+-d|"
    r"--daemon\b|\bdaemon\s+-d\b",
    re.IGNORECASE,
)


def is_service_command(command: str) -> bool:
    """这条命令是不是"启动后不会退出"的常驻服务？

    判的是命令**内容**，不是"有没有 timeout"——模型并不知道哪些命令会常驻，
    它只会照着目标写 ``python -m http.server``。
    """
    text = (command or "").strip()
    if not text:
        return False
    if _ALREADY_BACKGROUND_RE.search(text):
        return False
    # POSIX 的结尾 & 已经是后台执行；Windows 的 & 是调用运算符，不能这么判。
    if platform.system() != "Windows" and re.search(r"&\s*$", text):
        return False
    return bool(_SERVICE_CMD_RE.search(text))


#: 裸跑一个 Python 脚本：`python xxx.py`（不带管道/参数）。
_BARE_PY_RE = re.compile(
    r"^\s*(?:python[0-9.]*|py)\s+(?:-\w+\s+)*[\"']?([^\s\"']+\.py)[\"']?\s*$",
    re.IGNORECASE,
)
_GUI_MARKERS = (
    "mainloop(", "tkinter", "PyQt", "PySide", "pygame", "turtle", "wx.App",
    "cv2.imshow",
)


def interactive_reason(command: str, cwd: str) -> str:
    """裸跑 GUI/等待输入的脚本 → 前台必然阻塞到超时。

    实测一次 ``python gui.py`` 把 13 分钟的任务拖了 10 分钟，最后只拿到
    "超时"。判的是脚本内容：开窗口（mainloop）或读 input()，都不该前台等。
    带参数/管道的命令不在此列（比如 `echo x | python x.py` 是喂了输入的）。
    """
    match = _BARE_PY_RE.match(command or "")
    if not match:
        return ""
    script = Path(match.group(1))
    if not script.is_absolute():
        script = Path(cwd) / script
    try:
        text = script.read_text(encoding="utf-8", errors="replace")[:20000]
    except OSError:
        return ""
    for marker in _GUI_MARKERS:
        if marker in text:
            return f"包含 `{marker}`，会打开窗口并进入事件循环"
    if "input(" in text:
        return "会等待 input() 输入"
    return ""


#: PowerShell 里不存在、但模型常写的 Unix 命令 → 等价写法。
_WIN_CMD_ALIASES = {
    "head": "Get-Content <文件> -TotalCount 20",
    "tail": "Get-Content <文件> -Tail 20",
    "cat": "Get-Content <文件>",
    "grep": "Select-String -Pattern <模式> <文件>",
    "touch": "New-Item -ItemType File <文件>",
    "ls": "Get-ChildItem",
    "which": "Get-Command",
    "pip": "python -m pip",
    "python3": "python",
}
_NOT_RECOGNIZED_MARKERS = (
    "is not recognized as the name",
    "CommandNotFoundException",
    "无法将",
)


def windows_command_hint(command: str, output: str) -> str:
    """命令不存在时，给出 PowerShell 等价写法，别让模型反复试 Unix 命令。"""
    if platform.system() != "Windows":
        return ""
    if not any(marker in output for marker in _NOT_RECOGNIZED_MARKERS):
        return ""
    parts = (command or "").strip().split()
    if not parts:
        return ""
    name = parts[0].lower()
    replacement = _WIN_CMD_ALIASES.get(name)
    if not replacement:
        return ""
    return (
        f"\n[Windows 提示] PowerShell 里没有 `{name}`，"
        f"可改用 `{replacement}`；读文件优先用 read_file(offset/limit)。"
    )


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Terminate a command *and everything it spawned*.

    ``proc.kill()`` only reaps the direct child. On Windows the direct child is
    powershell, so anything it launched with ``Start-Process`` becomes an
    orphan — that is how a finished task left a Flask server holding port 5000.
    """
    if proc.poll() is not None:
        return
    if platform.system() == "Windows":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=10,
            )
            return
        except Exception:
            logger.debug("[bash] taskkill 失败，退回 kill()", exc_info=True)
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except Exception:
            logger.debug("[bash] killpg 失败，退回 kill()", exc_info=True)
    try:
        proc.kill()
    except Exception:
        pass


#: Windows NTSTATUS codes that mean "the process died", not "the command failed".
_WIN_CRASH_CODES = {
    0xC0000005: "access violation（内存访问冲突）",
    0xC00000FD: "stack overflow（栈溢出）",
    0xC0000135: "DLL not found（缺少依赖库）",
    0xC0000139: "DLL entry point not found（依赖库版本不匹配）",
    0xC0000409: "stack buffer overrun（/GS 保护触发）",
    0xC0000374: "heap corruption（堆损坏）",
}

_POSIX_SIGNALS = {6: "SIGABRT", 9: "SIGKILL", 11: "SIGSEGV（段错误）", 15: "SIGTERM"}


def describe_abnormal_exit(code: int | None) -> str:
    """Explain a crash-style exit code in words the model can act on.

    ``3221225477`` is just ``0xC0000005`` — an access violation. Reporting it as
    "exit 3221225477, no output" told the model nothing, so it burned six rounds
    bisecting the parameters of a native extension that was simply incompatible
    with the interpreter.
    """
    if not code:
        return ""

    if code < 0:  # POSIX: killed by a signal
        sig = -code
        return (
            f"\n[进程被信号终止] signal {sig} {_POSIX_SIGNALS.get(sig, '')}。"
            "这通常是原生扩展（C/Rust 编译的包）崩溃，而不是脚本逻辑错误。"
        )

    if code >= 0xC0000000:
        name = _WIN_CRASH_CODES.get(code, "未知系统异常")
        return (
            f"\n[进程异常终止] 退出码 {code} = 0x{code:08X} — {name}。"
            "这不是脚本逻辑错误，通常是原生扩展与当前 Python 版本不兼容；"
            "可尝试更换 Python 版本，或改用该工具的 CLI / 独立进程方式运行。"
        )

    return ""


_PORT_CONFLICT_HINTS = (
    "Address already in use",
    "WinError 10048",
    "EADDRINUSE",
    "通常每个套接字地址",
)


def describe_port_conflict(output: str) -> str:
    """Explain a port clash and how to find the culprit.

    The model once spent four rounds on "5000 is taken" with ``netstat`` and
    manual PIDs — and the process holding the port was a server an earlier task
    had left running. Naming the class of error up front shortens that loop.
    """
    if not any(hint in output for hint in _PORT_CONFLICT_HINTS):
        return ""
    return (
        "\n[端口冲突] 目标端口已被其他进程占用，这不是代码错误。"
        "先确认占用者：Windows 用 `netstat -ano | findstr :<端口>` 取 PID，"
        "再 `tasklist /FI \"PID eq <PID>\"` 看是哪个程序；"
        "确认无用后 `taskkill /PID <PID> /F`，或者直接给服务换一个端口。"
        "注意：占用者可能是本会话先前启动、没有关掉的服务。"
    )


def cleanup_tracked_processes(scope: str | None = None) -> int:
    """Kill commands started and never reaped.

    ``scope`` is the workspace of the task that is ending — only that task's
    children are reclaimed, so a finishing task cannot kill the dev server a
    sibling task is still using. ``scope=None`` (the old behaviour) drains
    everything, which is what single-task hosts and tests want.

    Returns how many processes were reclaimed.
    """
    with _TRACK_LOCK:
        if scope is None:
            procs = [
                p for bucket in _TRACKED_PROCS.values()
                for p in bucket if p.poll() is None
            ]
            _TRACKED_PROCS.clear()
        else:
            bucket = _TRACKED_PROCS.pop(_norm_scope(scope), set())
            procs = [p for p in bucket if p.poll() is None]
    for proc in procs:
        _kill_process_tree(proc)
    return len(procs)


def _tail_file(path: Path, limit: int = 2000) -> str:
    """读日志尾部——服务启动失败时，唯一线索都在这里。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    text = text.strip()
    return ("…\n" + text[-limit:]) if len(text) > limit else text


def _service_stop_hint(pid: int) -> str:
    if platform.system() == "Windows":
        return f"taskkill /F /T /PID {pid}"
    return f"kill -TERM -{pid}"


def _guess_port(command: str) -> str:
    """从命令里猜一个端口，给模型一条可直接运行的验证命令。"""
    for pattern in (
        r"http\.server\s+(\d{2,5})",
        r"--port[=\s]+(\d{2,5})",
        r"-p\s+(\d{2,5})",
        r":(\d{2,5})\b",
    ):
        match = re.search(pattern, command or "")
        if match:
            return match.group(1)
    return ""


def _probe_service(port: str, attempts: int = 2, interval: float = 0.4) -> tuple[bool, str]:
    """自己敲一次门：进程活着不等于服务能用。

    只确认"进程还在"时，模型拿到的是"已启动"，它会当成"已经验证过了"，
    然后直接去写文档。真正该确认的是**这个端口有没有 HTTP 响应**。

    用 http.client 而不是 urllib：后者会读系统代理设置，localhost 请求被
    转发到代理上会直接超时——表现为"服务明明起来了却探不到"。
    """
    if not port:
        return False, ""
    host = "localhost"
    url = f"http://{host}:{port}/"
    last_error = ""
    for _ in range(attempts):
        try:
            conn = http.client.HTTPConnection(host, int(port), timeout=1.5)
            try:
                conn.request("GET", "/")
                resp = conn.getresponse()
                code = resp.status
                resp.read(2048)
            finally:
                conn.close()
            if 200 <= code < 400:
                return True, f"✅ 可达性检查通过：GET {url} → HTTP {code}"
            return True, (
                f"✅ 服务已响应：HTTP {code}（服务正常，页面/路由可能需要调整）"
            )
        except Exception as exc:  # 连接被拒 / 超时：可能还在初始化
            last_error = str(exc)
            time.sleep(interval)
    return False, (
        f"⚠️ 进程在跑，但 {url} 暂未响应（{last_error[:100]}），可能仍在初始化。"
        "稍后再试，不要重复启动服务。"
    )


def _start_service(command: str, cwd: str, scope: str = _DEFAULT_SCOPE) -> tuple[str, int | None]:
    """后台启动常驻服务，确认它还活着就立刻返回。

    前台等一个永不退出的进程，结果只能是等满 timeout 后报"超时"——模型拿到
    的不是"服务起来了"，于是重试、换端口、再等一次。这里把 stdout 落到
    ``.gangge/service.log``，1.5 秒内进程还在就判定启动成功。
    """
    try:
        gangge_dir = Path(cwd) / ".gangge"
        gangge_dir.mkdir(parents=True, exist_ok=True)
        log_path = gangge_dir / "service.log"
    except Exception as e:
        return (f"[错误] 无法创建服务日志目录: {e}", None)

    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    is_windows = platform.system() == "Windows"
    if is_windows:
        cmd = [
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            _translate_to_powershell(command),
        ]
        # DETACHED_PROCESS 配 powershell 会直接退出、命令根本不执行；
        # 必须给一个隐藏控制台（CREATE_NO_WINDOW）。
        popen_kwargs: dict[str, Any] = {
            "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)
        }
    else:
        cmd = ["bash", "-c", command]
        popen_kwargs = {"start_new_session": True}

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(log_path, "a", encoding="utf-8", errors="replace") as log_file:
            log_file.write(f"\n===== {stamp} 服务启动: {command}\n")
            log_file.flush()
            proc = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=cwd,
                env=env,
                **popen_kwargs,
            )
    except Exception as e:
        return (f"[错误] 启动服务失败: {e}", None)

    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline and proc.poll() is None:
        time.sleep(0.1)

    code = proc.poll()
    if code is not None:
        tail = _tail_file(log_path)
        return (
            f"[服务启动失败] 进程在 1.5 秒内就退出了（exit {code}）。\n"
            f"日志尾部：\n{tail or '(无输出)'}\n"
            "常见原因：端口被占用、命令拼写错误、依赖没装。请先修好再启动。",
            code or 1,
        )

    _track(scope, proc)

    port = _guess_port(command)
    # 进程活着只是"起来了"；敲一次门才算"能用"。
    reachable, probe_note = _probe_service(port)

    if port:
        ui_url = f"http://localhost:{port}/"
        next_step = (
            "下一步（必须做，不要停在“已启动”就收尾）：\n"
            f'  1) browser_debug(action="navigate", url="{ui_url}")  打开页面看是否真的渲染\n'
            '  2) browser_debug(action="console_logs")  读 JS 报错\n'
            "  3) 若 browser_debug 不可用，用 web_fetch 抓一次页面文本作为退路"
        )
    else:
        next_step = (
            "端口没从命令里识别出来：先看日志里的实际端口，"
            "再用 browser_debug 打开对应地址验证。"
        )

    lines = [
        "[服务已在后台启动]" if reachable else "[服务进程已启动，但端口还没响应]",
        f"PID {proc.pid}",
        f"日志文件: {log_path}",
    ]
    if probe_note:
        lines.append(probe_note)
    lines += [
        f"停止命令: {_service_stop_hint(proc.pid)}",
        "（端口取自命令；若日志里显示了别的实际端口，以日志为准）",
        next_step,
        "说明：该进程与当前任务绑定，任务结束时会被自动回收；"
        "不要再把服务器命令前台跑一遍。",
    ]
    return ("\n".join(lines), 0)


def _is_verification(command: str) -> bool:
    """这条命令是不是测试/编译类（超时就该判失败，而不是转后台）。"""
    try:
        from gangge.layer3_agent.loop import is_verification_command

        return is_verification_command(command)
    except Exception:  # noqa: BLE001 - 判不出来时按普通命令处理
        return False


class _OutputSink:
    """边读边写内存缓冲；转后台后同时写日志文件。

    进程的 stdout 已经连在这个读取线程上，没法"重新挂载"到文件；
    所以让 sink 带一个可切换的文件句柄：转后台之后，后续输出继续写进日志。
    """

    def __init__(self) -> None:
        self.buffer = StringIO()
        self._file = None
        self._lock = threading.Lock()

    def write(self, text: str) -> None:
        with self._lock:
            self.buffer.write(text)
            if self._file is not None:
                try:
                    self._file.write(text)
                    self._file.flush()
                except Exception:
                    pass

    def redirect(self, path: Path) -> None:
        """把已有输出和后续输出都落到日志文件。"""
        with self._lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                self._file = path.open("a", encoding="utf-8", errors="replace")
                self._file.write(self.buffer.getvalue())
                self._file.flush()
            except Exception:
                self._file = None

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                try:
                    self._file.close()
                except Exception:
                    pass
                self._file = None

    def getvalue(self) -> str:
        with self._lock:
            return self.buffer.getvalue()


def _run_command(
    command: str, cwd: str, timeout: int, scope: str = _DEFAULT_SCOPE
) -> tuple[str, int | None]:
    """
    在线程中同步执行命令，流式读取防死锁。

    返回 ``(输出, 退出码)``；退出码为 None 表示命令没正常跑完（超时/找不到执行器）。

    核心改动：
    1. stderr 合并到 stdout，只读一根管道
    2. 线程异步读取 + join(timeout) 控制超时
    3. 把退出码一并返回：以前只看输出前缀，`npm test` 失败（exit 1）也会被
       当成成功，于是"验证状态 ✅ 已通过"和真实失败结果打架，模型反复重跑。
    """
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}

    if platform.system() == "Windows":
        translated = _translate_to_powershell(command)
        # -NonInteractive 会把子进程的非零退出码统一压成 1，必须显式透传
        # $LASTEXITCODE，否则既不知道失败了，也不知道失败码是多少。
        # 先把它复位为 $null，避免 chcp 留下的 0 污染判断；再用 $? 兜住
        # "命令名不存在 / 纯 cmdlet 报错"这类不产生原生退出码的失败。
        translated = (
            "chcp 65001 | Out-Null; [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            "$LASTEXITCODE = $null; "
            f"{translated}; "
            "$ok = $?; "
            "if ($LASTEXITCODE -ne $null) { exit $LASTEXITCODE } "
            "elseif (-not $ok) { exit 1 } else { exit 0 }"
        )
        cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", translated]
    else:
        cmd = ["bash", "-c", command]

    sink = _OutputSink()
    last_output = [time.monotonic()]
    verification = _is_verification(command)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,   # 合并 stderr 到 stdout，只读一个管道
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            env=env,
            # POSIX：单独进程组，超时时能连同子进程一起杀（见 _kill_process_tree）
            **({} if platform.system() == "Windows" else {"start_new_session": True}),
        )

        # 逐行读取，不等进程结束
        def read_output():
            try:
                for line in proc.stdout:
                    sink.write(line)
                    last_output[0] = time.monotonic()
            except Exception:
                pass
            finally:
                sink.close()

        _track(scope, proc)
        detached = False
        try:
            reader = threading.Thread(target=read_output, daemon=True)
            reader.start()
            started = time.monotonic()

            # 分片等待，同时盯两件事：总时长超时、以及长时间没有新输出。
            while True:
                reader.join(0.5)
                if not reader.is_alive():
                    break
                now = time.monotonic()
                timed_out = (now - started) >= timeout
                idle_out = (
                    (not verification)
                    and (now - last_output[0]) >= IDLE_DETACH_SECONDS
                )
                if not (timed_out or idle_out):
                    continue
                if verification:
                    # 测试/构建超时 = 没通过：杀掉并如实报告，不能转后台让模型
                    # 误以为"验证还在跑、大概没问题"。
                    _kill_process_tree(proc)
                    return (
                        f"[超时] 命令执行超过 {timeout} 秒被终止\n"
                        f"已获取输出:\n{sink.getvalue()[:500]}",
                        None,
                    )
                # 普通命令：留在后台继续跑，本次先返回，任务不用陪着卡住。
                log_path = Path(cwd) / ".gangge" / f"bg_{proc.pid}.log"
                sink.redirect(log_path)
                detached = True
                reason = (
                    f"超过 {timeout} 秒仍未结束"
                    if timed_out
                    else f"连续 {IDLE_DETACH_SECONDS} 秒没有任何输出"
                )
                return (
                    f"[已转入后台] 命令{reason}，已留在后台继续运行（PID {proc.pid}）。\n"
                    f"日志: {log_path}（后续输出会写到这里）\n"
                    f"停止: {_service_stop_hint(proc.pid)}\n"
                    "注意：这是“没等到结果”，不等于成功；需要结果就稍后读日志，"
                    "不要重复前台跑同一条命令。",
                    0,
                )

            proc.wait()
        finally:
            if not detached:
                _untrack(scope, proc)

    except FileNotFoundError:
        return ("[错误] 找不到命令执行器（powershell/bash）", None)
    except Exception as e:
        return (f"[错误] {e}", None)

    exit_code = proc.returncode
    result = sink.getvalue().strip()

    MAX_OUTPUT = 8000
    if len(result) > MAX_OUTPUT:
        head = result[:MAX_OUTPUT // 2]
        tail = result[-(MAX_OUTPUT // 2):]
        result = (
            head
            + f"\n\n...[输出已截断，共 {len(result)} 字符，保留首尾各 {MAX_OUTPUT // 2} 字符]...\n\n"
            + tail
        )

    # 空输出 + 奇怪的退出码是最难排查的组合：模型只能反复猜。
    diagnosis = describe_abnormal_exit(exit_code)
    if diagnosis:
        result = (result + diagnosis) if result else f"(无输出){diagnosis}"

    result += describe_port_conflict(result)
    result += windows_command_hint(command, result)

    return (result or "(无输出)", exit_code)


class BashTool(BaseTool):
    """Execute shell commands."""

    def __init__(self, workspace: str = ""):
        self.workspace = workspace

    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return (
            "执行 shell 命令。用于运行编译、测试、安装依赖、启动服务器等。命令在项目工作目录下执行。\n"
            "注意：命令超时（默认 120 秒，上限 600 秒）后会被强制终止。\n"
            "普通命令超时或长时间没有输出时会自动转入后台继续运行（返回 PID 和日志路径），"
            "不会卡住整个任务；测试/构建类命令超时则判为失败。"
            "会弹窗或等待输入的脚本会被直接拦下，不要用 bash 前台跑它们。\n"
            "启动 Web 服务器时直接写普通命令即可（例如 `python -m http.server 8000`、"
            "`npm run dev`、`uvicorn app:app`）：这类常驻命令会被自动改成后台启动，"
            "日志写到 .gangge/service.log，并返回 PID / 验证命令 / 停止命令。\n"
            "拿到返回值后，另起一条命令用 curl / Invoke-WebRequest 验证，不要重复前台启动服务器。\n"
            "如果你自己已经用 Start-Process（Windows）或 nohup ... &（Linux）后台化，也会原样执行。"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的 shell 命令",
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        f"最多等待多少秒（默认 {DEFAULT_TIMEOUT_SECONDS}，"
                        f"上限 {MAX_TIMEOUT_SECONDS}，超过按上限执行）。"
                        "不是每次都要等这么久：命令结束就立刻返回。"
                    ),
                    "default": DEFAULT_TIMEOUT_SECONDS,
                },
            },
            "required": ["command"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        command = kwargs.get("command") or kwargs.get("cmd") or ""
        workdir = self.workspace or os.getcwd()

        if not command:
            return ToolResult(
                output=f"❌ bash 缺少命令参数。收到的参数: {list(kwargs.keys())}。请使用 command=\"要执行的命令\"。",
                is_error=True,
            )

        # timeout 是"最多等多久"，不是"要等多久"。模型传过 "5000"（83 分钟），
        # 必须封顶，否则一条常驻命令就能把整个任务挂死。
        raw_timeout = kwargs.get("timeout", DEFAULT_TIMEOUT_SECONDS)
        try:
            timeout = int(float(str(raw_timeout).strip() or DEFAULT_TIMEOUT_SECONDS))
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT_SECONDS
        if timeout <= 0:
            timeout = DEFAULT_TIMEOUT_SECONDS
        capped_note = ""
        if timeout > MAX_TIMEOUT_SECONDS:
            capped_note = (
                f"\n[timeout 参数 {timeout}s 超过上限，已按 {MAX_TIMEOUT_SECONDS}s 执行]"
            )
            timeout = MAX_TIMEOUT_SECONDS

        # GUI / 等 input() 的脚本：前台跑必然阻塞到超时。直接拦下并给替代做法，
        # 别把十分钟耗在等一个不会自己结束的窗口上。
        reason = interactive_reason(command, workdir)
        if reason:
            return ToolResult(
                output=(
                    f"⚠️ 检测到交互式/GUI 程序（{reason}），没有前台运行。\n"
                    f"它会一直等输入或进入事件循环，直到底层超时（最长 {MAX_TIMEOUT_SECONDS}s），"
                    "拿到的基本只有一句“超时”。\n"
                    "正确做法：\n"
                    "1) 自测：把逻辑拆成可直接调用的函数，用 run_tests 或 "
                    "`python -c \"import <模块>; <模块>.<函数>()\"` 调用具体函数；\n"
                    "2) 给用户看界面：Windows 用 `Start-Process pythonw <脚本>` 后台打开，不阻塞。\n"
                    "这一步不算验证，也不要再重复前台运行。"
                ),
                metadata={"interactive_guard": True},
            )

        try:
            # 在默认线程池中运行同步命令，不阻塞 asyncio 事件循环
            loop = asyncio.get_running_loop()
            if is_service_command(command):
                # 常驻服务：分离进程 + 日志文件，确认存活就返回，
                # 不再等一个永远不会退出的进程。
                output, exit_code = await loop.run_in_executor(
                    None, _start_service, command, workdir, workdir
                )
            else:
                output, exit_code = await loop.run_in_executor(
                    None, _run_command, command, workdir, timeout, workdir
                )
            if capped_note:
                output += capped_note

            # 退出码才是"命令到底成没成功"的唯一依据。只看输出前缀会把
            # `npm test`（exit 1、输出全是 ✖）判成成功。
            is_error = exit_code is None or exit_code != 0
            if exit_code not in (None, 0):
                output = f"{output}\n[exit {exit_code}]"

            return ToolResult(
                output=output,
                is_error=is_error,
                metadata={"exit_code": exit_code if exit_code is not None else -1},
            )
        except Exception as e:
            return ToolResult(output=f"执行失败: {e}", is_error=True)


def _translate_to_powershell(cmd: str) -> str:
    """Translate common Unix shell syntax to PowerShell-compatible commands."""
    # mkdir -p → mkdir (PowerShell mkdir creates parent paths by default)
    cmd = cmd.replace("mkdir -p ", "mkdir ")
    # && → ; (PowerShell 5.x does not support &&, use ; for sequential execution)
    cmd = cmd.replace(" && ", " ; ")
    # cmd.exe 的 `start /B prog args` 在 PowerShell 里会被当成 Start-Process 的
    # 参数，典型表现是命令一直挂到超时。改成后台隐藏启动，立即返回。
    parse_start = re.compile(r"^\s*start\s+/[Bb]\s+(.+)$")
    segments = cmd.split(" ; ")
    for i, seg in enumerate(segments):
        m = parse_start.match(seg)
        if m:
            segments[i] = (
                "Start-Process -WindowStyle Hidden -FilePath powershell -ArgumentList "
                f"'-NoProfile','-Command','{m.group(1).strip()}'; Start-Sleep -Seconds 2"
            )
    cmd = " ; ".join(segments)
    # Remove leading/trailing whitespace
    cmd = cmd.strip()
    return cmd
