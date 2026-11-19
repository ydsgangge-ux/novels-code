"""Bash tool — execute shell commands with streaming output.

Uses threaded reading + merged stderr to avoid Windows pipe deadlock.
"""

from __future__ import annotations

import asyncio
import os
import platform
import re
import subprocess
import threading
from io import StringIO
from typing import Any

from gangge.layer3_agent.tools.base import BaseTool, ToolResult


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
        )

        # 逐行读取，不等进程结束
        def read_output():
            try:
                for line in proc.stdout:
                    output_buf.write(line)
            except Exception:
                pass

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        reader.join(timeout=timeout)

        if reader.is_alive():
            proc.kill()
            return (
                f"[超时] 命令执行超过 {timeout} 秒被终止\n"
                f"已获取输出:\n{output_buf.getvalue()[:500]}",
                None,
            )

        proc.wait()

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
