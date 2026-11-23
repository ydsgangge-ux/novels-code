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

#: Commands started by this process that have not exited yet. A server the
#: agent launched should not outlive the task that needed it.
_TRACKED_PROCS: set[subprocess.Popen] = set()
_TRACK_LOCK = threading.Lock()

#: 命令超时的默认值与上限。
#:
#: 默认 120 秒对普通命令足够；模型偶尔会传 ``timeout: "5000"``（83 分钟），
#: 于是 ``reader.join(5000)`` 把整个任务挂死——那次 ``python -m http.server``
#: 就是这么把任务拖了 36 分钟还没结果的。任何"最多等多久"的参数都必须有
#: 上限，否则它会被当成"要等多久"。
DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 600

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


def cleanup_tracked_processes() -> int:
    """Kill commands this session started and never reaped.

    Called at the end of a turn. Returns how many processes were reclaimed.
    """
    with _TRACK_LOCK:
        procs = [p for p in _TRACKED_PROCS if p.poll() is None]
        _TRACKED_PROCS.clear()
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


def _start_service(command: str, cwd: str) -> tuple[str, int | None]:
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

    with _TRACK_LOCK:
        _TRACKED_PROCS.add(proc)

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


def _run_command(command: str, cwd: str, timeout: int) -> tuple[str, int | None]:
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

    output_buf = StringIO()

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
                    output_buf.write(line)
            except Exception:
                pass

        with _TRACK_LOCK:
            _TRACKED_PROCS.add(proc)
        try:
            reader = threading.Thread(target=read_output, daemon=True)
            reader.start()
            reader.join(timeout=timeout)

            if reader.is_alive():
                _kill_process_tree(proc)
                return (
                    f"[超时] 命令执行超过 {timeout} 秒被终止\n"
                    f"已获取输出:\n{output_buf.getvalue()[:500]}",
                    None,
                )

            proc.wait()
        finally:
            with _TRACK_LOCK:
                _TRACKED_PROCS.discard(proc)

    except FileNotFoundError:
        return ("[错误] 找不到命令执行器（powershell/bash）", None)
    except Exception as e:
        return (f"[错误] {e}", None)

    exit_code = proc.returncode
    result = output_buf.getvalue().strip()

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

        try:
            # 在默认线程池中运行同步命令，不阻塞 asyncio 事件循环
            loop = asyncio.get_running_loop()
            if is_service_command(command):
                # 常驻服务：分离进程 + 日志文件，确认存活就返回，
                # 不再等一个永远不会退出的进程。
                output, exit_code = await loop.run_in_executor(
                    None, _start_service, command, workdir
                )
            else:
                output, exit_code = await loop.run_in_executor(
                    None, _run_command, command, workdir, timeout
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
