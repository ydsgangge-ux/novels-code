"""Bash tool — execute shell commands with streaming output.

Uses threaded reading + merged stderr to avoid Windows pipe deadlock.
"""

from __future__ import annotations

import asyncio
import os
import platform
import subprocess
import threading
from io import StringIO
from typing import Any

from gangge.layer3_agent.tools.base import BaseTool, ToolResult


def _run_command(command: str, cwd: str, timeout: int) -> str:
    """
    在线程中同步执行命令，流式读取防死锁。

    核心改动：
    1. stderr 合并到 stdout，只读一根管道
    2. 线程异步读取 + join(timeout) 控制超时
    """
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}

    if platform.system() == "Windows":
        translated = _translate_to_powershell(command)
        translated = f"chcp 65001 | Out-Null; [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; {translated}"
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
                f"已获取输出:\n{output_buf.getvalue()[:500]}"
            )

        proc.wait()

    except FileNotFoundError:
        return "[错误] 找不到命令执行器（powershell/bash）"
    except Exception as e:
        return f"[错误] {e}"

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

    return result or "(无输出)"


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
            "启动 Web 服务器/后台进程时，请加上后台运行标记：\n"
            "  Windows: start /B python server.py\n"
            "  Linux:   python server.py &\n"
            "然后用 curl/wget 单独验证服务器是否正常启动。"
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
            output = await loop.run_in_executor(
                None, _run_command, command, workdir, timeout
            )

            # 检测错误标记
            is_error = output.startswith("[错误]") or output.startswith("[超时]")
            exit_code = 1 if is_error else 0

            return ToolResult(
                output=output,
                is_error=is_error,
                metadata={"exit_code": exit_code},
            )
        except Exception as e:
            return ToolResult(output=f"执行失败: {e}", is_error=True)


def _translate_to_powershell(cmd: str) -> str:
    """Translate common Unix shell syntax to PowerShell-compatible commands."""
    # mkdir -p → mkdir (PowerShell mkdir creates parent paths by default)
    cmd = cmd.replace("mkdir -p ", "mkdir ")
    # && → ; (PowerShell 5.x does not support &&, use ; for sequential execution)
    cmd = cmd.replace(" && ", " ; ")
    # Remove leading/trailing whitespace
    cmd = cmd.strip()
    return cmd
