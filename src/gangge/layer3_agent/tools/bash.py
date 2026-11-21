"""Bash tool — execute shell commands with streaming output.

Uses threaded reading + merged stderr to avoid Windows pipe deadlock.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
import signal
import subprocess
import threading
from io import StringIO
from typing import Any

from gangge.layer3_agent.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

#: Commands started by this process that have not exited yet. A server the
#: agent launched should not outlive the task that needed it.
_TRACKED_PROCS: set[subprocess.Popen] = set()
_TRACK_LOCK = threading.Lock()


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
            "注意：命令超时（默认 120 秒）后会被强制终止。\n"
            "启动 Web 服务器/后台进程时必须让命令立刻返回，否则会把整条命令挂到超时：\n"
            "  Windows（本机实际执行器是 PowerShell，cmd 的 `start /B` 在这里无效）:\n"
            "    Start-Process -WindowStyle Hidden -FilePath python -ArgumentList 'server.py'"
            " -RedirectStandardOutput server.log -RedirectStandardError server.err\n"
            "  Linux: nohup python server.py > server.log 2>&1 &\n"
            "启动后另开一条命令用 curl / Invoke-WebRequest 验证，不要把服务器前台跑在这条命令里。"
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
                    "description": "超时时间（秒），默认 120",
                    "default": 120,
                },
            },
            "required": ["command"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        command = kwargs.get("command") or kwargs.get("cmd") or ""
        timeout = kwargs.get("timeout", 120)
        workdir = self.workspace or os.getcwd()

        if not command:
            return ToolResult(
                output=f"❌ bash 缺少命令参数。收到的参数: {list(kwargs.keys())}。请使用 command=\"要执行的命令\"。",
                is_error=True,
            )

        try:
            # 在默认线程池中运行同步命令，不阻塞 asyncio 事件循环
            loop = asyncio.get_running_loop()
            output, exit_code = await loop.run_in_executor(
                None, _run_command, command, workdir, timeout
            )

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
