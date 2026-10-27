"""
Gangge Code — MCP 客户端模块
src/gangge/layer4_tools/mcp_client.py

职责：
  - 连接 MCP Server（stdio / SSE 两种传输）
  - 拉取工具列表，动态注册进 ToolRegistry
  - 转发 LLM 的工具调用，返回结果
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gangge.layer5_llm.base import ToolDefinition

logger = logging.getLogger(__name__)

# 所有失败路径统一用这个前缀，上层据此置 is_error（见 loop.py / pipeline.py）。
_ERROR_PREFIX = "[错误]"

#: 客户端真正实现的传输。DSH 生态里的 streamable-http 暂不支持，
#: 写进配置只会在连接时报"不支持的传输类型"。
SUPPORTED_TRANSPORTS = ("stdio", "sse")


# ─────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────

@dataclass
class MCPServerConfig:
    """单个 MCP Server 的配置"""
    name: str                          # 如 "autocad"、"freecad"
    transport: str                     # "stdio" 或 "sse"
    # stdio 专用
    command: str = ""                  # 如 "node"
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    # sse 专用
    url: str = ""                      # 如 "http://localhost:3000/sse"
    # 通用
    enabled: bool = True
    timeout: int = 30                  # 工具调用超时（秒）


@dataclass
class MCPTool:
    """从 MCP Server 拉取到的单个工具描述"""
    server_name: str
    name: str                          # 工具原始名，如 "draw_circle"
    full_name: str                     # 注册到 ToolRegistry 的名，如 "autocad__draw_circle"
    description: str
    input_schema: dict                 # JSON Schema，直接透传给 LLM


# ─────────────────────────────────────────
# JSON-RPC 2.0 协议帮助函数
# ─────────────────────────────────────────

def _rpc_request(method: str, params: dict, req_id: int = 1) -> bytes:
    msg = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
    line = json.dumps(msg, ensure_ascii=False)
    return (line + "\n").encode()


def _parse_response(raw: bytes) -> dict:
    return json.loads(raw.decode().strip())


def _resolve_executable(command: str) -> str:
    """把 ``npx`` 这类命令解析成真实路径。

    Windows 上 ``npx`` / ``npm`` 是 ``.cmd`` 垫片，``CreateProcess`` 不做 PATHEXT
    解析，直接传 "npx" 会得到 ``[WinError 2] 系统找不到指定的文件``——所有 stdio
    MCP server 都起不来。这里先按 PATH 解析，解析不到就原样返回让错误照常抛出。
    """
    if not command:
        return command
    import shutil
    resolved = shutil.which(command)
    if resolved and resolved != command:
        logger.debug(f"[MCP] 命令解析: {command} → {resolved}")
        return resolved
    return command


# ─────────────────────────────────────────
# stdio 传输（最常见：本地 Node.js / Python MCP Server）
# ─────────────────────────────────────────

class StdioMCPConnection:
    """
    通过 stdin/stdout 与本地 MCP Server 进程通信。
    协议：每行一条 JSON-RPC 2.0 消息。

    stdout 由后台线程独占读取并投进队列，好处是：
      - 每次 ``_call`` 能按 ``config.timeout`` 超时，而不是无限期阻塞；
      - stderr 同时被排空，Server 大量写日志时不会写满管道导致双方死锁。
    """

    def __init__(self, config: MCPServerConfig):
        self.config = config
        self._proc: subprocess.Popen | None = None
        self._req_id = 0
        self._lines: queue.Queue[bytes] = queue.Queue()

    def connect(self):
        import os
        env = {**os.environ, **self.config.env}
        cmd = [_resolve_executable(self.config.command)] + self.config.args
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        for target, args, suffix in (
            (self._pump, (self._proc.stdout, self._lines), "out"),
            (self._drain, (self._proc.stderr,), "err"),
        ):
            threading.Thread(
                target=target, args=args, name=f"mcp-{self.config.name}-{suffix}", daemon=True
            ).start()
        # MCP 握手：initialize → notifications/initialized
        self._call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "gangge-code", "version": "1.0.0"},
        })
        self._notify("notifications/initialized", {})
        logger.info(f"[MCP] stdio 连接成功: {self.config.name}")

    def disconnect(self):
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def list_tools(self) -> list[dict]:
        resp = self._call("tools/list", {})
        return resp.get("result", {}).get("tools", [])

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        resp = self._call("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })
        result = resp.get("result", {})
        # MCP 标准返回格式：content 列表
        contents = result.get("content", [])
        parts = []
        for item in contents:
            if item.get("type") == "text":
                parts.append(item["text"])
            elif item.get("type") == "image":
                parts.append(f"[图像: {item.get('mimeType', 'image')}]")
        text = "\n".join(parts) if parts else json.dumps(result, ensure_ascii=False)
        # 工具自身执行失败（非零退出、参数错误等）由 isError 表达，别当成功喂给模型。
        if result.get("isError"):
            return f"{_ERROR_PREFIX} {text}"
        return text

    # ── 内部 ──────────────────────────────

    @staticmethod
    def _pump(stream, sink: queue.Queue) -> None:
        """逐行读 stdout 直到 EOF；结束时投一个空串作哨兵。"""
        try:
            for line in iter(stream.readline, b""):
                sink.put(line)
        except Exception:
            pass
        finally:
            sink.put(b"")

    @staticmethod
    def _drain(stream) -> None:
        try:
            for line in iter(stream.readline, b""):
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    logger.debug(f"[MCP] stderr: {text}")
        except Exception:
            pass

    def _read(self) -> bytes:
        try:
            raw = self._lines.get(timeout=self.config.timeout)
        except queue.Empty:
            raise TimeoutError(
                f"[MCP] {self.config.name} 在 {self.config.timeout}s 内无响应"
            ) from None
        if not raw:
            raise RuntimeError(f"[MCP] {self.config.name} 进程已退出")
        return raw

    def _write(self, payload: bytes) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError(f"[MCP] {self.config.name} 未连接")
        try:
            self._proc.stdin.write(payload)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise RuntimeError(f"[MCP] {self.config.name} 写入失败: {e}") from e

    def _notify(self, method: str, params: dict) -> None:
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        self._write((json.dumps(msg, ensure_ascii=False) + "\n").encode())

    def _call(self, method: str, params: dict) -> dict:
        self._req_id += 1
        req_id = self._req_id
        self._write(_rpc_request(method, params, req_id))
        # Server 可能插播 notifications/*，只接受 id 与本次请求匹配的那条响应。
        while True:
            raw = self._read()
            try:
                msg = _parse_response(raw)
            except json.JSONDecodeError:
                logger.debug(f"[MCP] 忽略非 JSON 输出: {raw[:200]!r}")
                continue
            if msg.get("id") != req_id:
                logger.debug(f"[MCP] 忽略通知: {msg.get('method')}")
                continue
            if msg.get("error"):
                raise RuntimeError(f"{method} 失败: {msg['error']}")
            return msg


# ─────────────────────────────────────────
# SSE 传输（远程 HTTP MCP Server）
# ─────────────────────────────────────────

class SSEMCPConnection:
    """
    通过 HTTP + Server-Sent Events 与远程 MCP Server 通信。
    需要 httpx 库：pip install httpx
    """

    def __init__(self, config: MCPServerConfig):
        self.config = config
        self._session_url: str = ""

    def connect(self):
        import httpx
        # 第一步：建立 SSE 会话，获取 session endpoint
        with httpx.Client(timeout=self.config.timeout) as client:
            resp = client.post(f"{self.config.url}/initialize", json={
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "gangge-code", "version": "1.0.0"},
            })
            resp.raise_for_status()
            self._session_url = resp.json().get("sessionUrl", self.config.url)
        logger.info(f"[MCP] SSE 连接成功: {self.config.name} → {self._session_url}")

    def disconnect(self):
        pass  # HTTP 无状态，无需显式断开

    def list_tools(self) -> list[dict]:
        import httpx
        with httpx.Client(timeout=self.config.timeout) as client:
            resp = client.post(f"{self._session_url}/tools/list", json={})
            resp.raise_for_status()
            return resp.json().get("tools", [])

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        import httpx
        with httpx.Client(timeout=self.config.timeout) as client:
            resp = client.post(f"{self._session_url}/tools/call", json={
                "name": tool_name,
                "arguments": arguments,
            })
            resp.raise_for_status()
            result = resp.json()
        contents = result.get("content", [])
        text = "\n".join(
            item["text"] for item in contents if item.get("type") == "text"
        ) or json.dumps(result, ensure_ascii=False)
        if result.get("isError"):
            return f"{_ERROR_PREFIX} {text}"
        return text


# ─────────────────────────────────────────
# MCP 客户端管理器（核心类）
# ─────────────────────────────────────────

#: 会话级选择：``all`` / ``none`` / ``name1,name2``。空表示跟随配置里的 ``enabled``。
SELECTION_ENV = "GANGGE_MCP_SERVERS"

#: 估算 token 用的字符/ token 比。工具 schema 以英文标识符为主，实测这个比值够准。
CHARS_PER_TOKEN = 3.8

#: 控制 MCP 工具是否延迟揭示：auto（默认）/ all / none。
DEFER_ENV = "GANGGE_MCP_DEFER"

#: ``auto`` 的阈值：MCP 工具定义估算低于这个 token 数就直接给，超过就走 tool_search。
DIRECT_INJECT_TOKENS = 300


def estimate_chars(defs: list[ToolDefinition]) -> int:
    """工具定义的总字符数（name + description + schema 的 JSON 长度）。"""
    total = 0
    for d in defs:
        total += len(d.name) + len(d.description)
        total += len(json.dumps(d.input_schema, ensure_ascii=False))
    return total


def parse_selection(raw: str | None, available: list[str]) -> set[str] | None:
    """解析选择表达式。

    - ``None`` / 空串 → ``None``，表示"跟随配置里的 enabled"
    - ``all``          → 全部
    - ``none``/``off`` → 空集（一个都不连，零 token）
    - ``a,b``          → 精确匹配的名字集合；未知名字只告警，不报错
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered in ("all", "*"):
        return set(available)
    if lowered in ("none", "off", "-"):
        return set()

    wanted = {p.strip() for p in re.split(r"[,\s]+", text) if p.strip()}
    known = {n for n in available if n in wanted}
    unknown = sorted(wanted - known)
    if unknown:
        logger.warning(
            "[MCP] 选择里有未知 server，已忽略: %s（可选: %s）",
            ", ".join(unknown),
            ", ".join(available) or "无",
        )
    return known


# ─────────────────────────────────────────
# 分层配置：用户级 → 项目级 → 临时层
# ─────────────────────────────────────────

#: 用户级配置：配一次，所有工作目录都能用（Blender / GitHub 这种全局能力）。
GLOBAL_CONFIG_PATH = Path.home() / ".gangge" / "mcp_servers.json"

#: 项目级配置：跟着工作目录走，用于项目专属 server 或覆盖用户级同名条目。
PROJECT_CONFIG_REL = Path(".gangge") / "mcp_servers.json"

#: 额外一层（实验 / 一次性）：指向一个 json 文件。
CONFIG_ENV = "GANGGE_MCP_CONFIG"


def _read_document(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("[MCP] %s 读不出来，忽略这一层: %s", path, e)
        return {}
    if isinstance(data, list):
        return {"servers": data}
    return data if isinstance(data, dict) else {}


def merge_documents(
    layers: list[tuple[dict[str, Any], Path]],
) -> tuple[dict[str, Any], dict[str, Path]]:
    """按 ``name`` 合并多层配置：后一层**整体覆盖**前一层同名条目。

    故意不做字段级合并——``command`` 来自用户级、``args`` 来自项目级拼出来的
    server 没人能预期，宁可干脆地整条覆盖。

    返回 ``(合并后的文档, {server 名: 提供最终定义的配置文件})``。
    """
    merged: dict[str, dict[str, Any]] = {}
    sources: dict[str, Path] = {}
    for document, path in layers:
        servers = document.get("servers")
        if not isinstance(servers, list):
            continue
        for entry in servers:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            name = str(entry["name"])
            merged[name] = entry
            sources[name] = path
    return {"servers": list(merged.values())}, sources


def workspace_config_paths(workspace_dir: str | Path = ".", extra: str | None = None) -> list[Path]:
    ws = Path(workspace_dir) if workspace_dir else Path(".")
    paths = [GLOBAL_CONFIG_PATH, ws / PROJECT_CONFIG_REL]
    extra_path = extra if extra is not None else os.environ.get(CONFIG_ENV)
    if extra_path:
        paths.append(Path(extra_path))
    return paths


def load_merged_config(
    workspace_dir: str | Path = ".",
    extra: str | None = None,
) -> tuple[dict[str, Any], dict[str, Path]]:
    """读并合并三层配置。GUI 与 CLI 共用这一份，避免各写一套规则。"""
    layers = [
        (_read_document(path), path) for path in workspace_config_paths(workspace_dir, extra)
    ]
    return merge_documents(layers)


def source_label(path: Path, workspace_dir: str | Path = ".") -> str:
    ws = Path(workspace_dir) if workspace_dir else Path(".")
    if path == GLOBAL_CONFIG_PATH:
        return "用户级"
    if path == ws / PROJECT_CONFIG_REL:
        return "本项目"
    return "临时层"


def save_selection(workspace_dir: str | Path, states: dict[str, bool]) -> Path:
    """把勾选结果写进**项目级**配置，返回被写入的路径。

    两条规则，保证"在 A 项目关掉 Blender 不会影响 B 项目"：

    - 项目级已有的条目 → 直接改它的 ``enabled``；
    - 只存在于用户级的条目 → 在项目级写一条最小覆盖 ``{name, enabled}``，
      绝不改动用户级文件。勾选状态与用户级一致时就删掉这条覆盖。
    """
    ws = Path(workspace_dir) if workspace_dir else Path(".")
    project_path = ws / PROJECT_CONFIG_REL
    project_doc = _read_document(project_path)
    project_servers = [
        s for s in (project_doc.get("servers") or [])
        if isinstance(s, dict) and s.get("name")
    ]
    project_doc["servers"] = project_servers

    global_doc = _read_document(GLOBAL_CONFIG_PATH)
    global_servers = {
        str(s.get("name")): s
        for s in (global_doc.get("servers") or [])
        if isinstance(s, dict) and s.get("name")
    }
    project_names = {str(s["name"]) for s in project_servers}

    def _is_override(entry: dict[str, Any]) -> bool:
        """只写了 enabled 的条目 = 对用户级定义的最小覆盖，没有自己的 command/url。"""
        return not entry.get("command") and not entry.get("url")

    for entry in project_servers:
        name = str(entry["name"])
        if name in states:
            entry["enabled"] = bool(states[name])

    for name, desired in states.items():
        global_entry = global_servers.get(name)
        if global_entry is None:
            continue          # 未知名字，忽略
        existing = next((s for s in project_servers if str(s["name"]) == name), None)
        same_as_global = bool(global_entry.get("enabled", True)) == bool(desired)
        if existing is not None:
            # 覆盖项与用户级一致就删掉，别留一条无意义的 enabled: true
            if _is_override(existing) and same_as_global:
                project_servers.remove(existing)
            continue
        if same_as_global:
            continue          # 用户级默认值本身就是想要的状态
        project_servers.append({"name": name, "enabled": bool(desired)})

    project_path.parent.mkdir(parents=True, exist_ok=True)
    project_path.write_text(
        json.dumps(project_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    logger.info("[MCP] 选择已写入项目级配置: %s", project_path)
    return project_path


def upsert_server(entry: dict[str, Any], workspace_dir: str | Path = ".", scope: str = "project") -> Path:
    """把一条 server 定义写进项目级或用户级配置，同名条目按字段覆盖。

    与 :func:`save_selection` 的区别：那个只写 ``enabled``（最小覆盖），这个写完整
    定义——用于"新增一个 server"。
    """
    path = (
        GLOBAL_CONFIG_PATH
        if scope == "global"
        else (Path(workspace_dir) if workspace_dir else Path(".")) / PROJECT_CONFIG_REL
    )
    document = _read_document(path)
    servers = [
        s for s in (document.get("servers") or [])
        if isinstance(s, dict) and s.get("name")
    ]
    name = str(entry["name"])
    for i, existing in enumerate(servers):
        if str(existing["name"]) == name:
            servers[i] = {**existing, **entry}
            break
    else:
        servers.append(entry)
    document["servers"] = servers
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger.info("[MCP] 已写入 %s 配置: %s", "用户级" if scope == "global" else "项目级", path)
    return path


class MCPClientManager:
    """
    管理所有 MCP Server 的连接，对外提供统一接口。

    使用方式（AgenticLoop / PlanExecutor 已内置）：
        manager = MCPClientManager.from_workspace(workspace_dir)   # 含选择与环境变量
        defs = manager.build_tool_definitions()   # 追加进 LLM 的 tools 列表
        manager.call_tool("autocad__draw_circle", {...})

    工具命名约定 ``{server}__{tool}``。调用方不要靠 ``"__" in name`` 猜是不是
    MCP 工具——本地工具名同样可能含双下划线；用 :meth:`has_tool` 精确判定。

    **选择语义**：``selection`` 为 ``None`` 时跟随配置 ``enabled``；一旦显式选择，
    没被选中的 server 连进程都不启动——不连就不出定义，不占一个 token。
    """

    def __init__(self, configs: list[MCPServerConfig], selection: set[str] | None = None):
        self.configs = configs
        self.selection = selection
        #: server 名 → 提供最终定义的配置文件（用户级 / 项目级 / 临时层）
        self.sources: dict[str, Path] = {}
        self._connections: dict[str, StdioMCPConnection | SSEMCPConnection] = {}
        self._tools: list[MCPTool] = []

    # ── 选择 ──────────────────────────────

    @property
    def available_names(self) -> list[str]:
        return [c.name for c in self.configs]

    def is_selected(self, name: str) -> bool:
        if self.selection is not None:
            return name in self.selection
        for cfg in self.configs:
            if cfg.name == name:
                return cfg.enabled
        return False

    def select(self, selection: set[str] | None) -> None:
        """改选择。调用方负责重新 ``connect_all()``。"""
        self.selection = selection

    def estimate_tokens(self) -> int:
        return int(estimate_chars(self.build_tool_definitions()) / CHARS_PER_TOKEN)

    def describe(self) -> str:
        names = [c.name for c in self.configs if self.is_selected(c.name)]
        if not names:
            return "[MCP] 本次未启用任何 server（0 token）"
        return (
            f"[MCP] 已启用 {len(names)} 个 server: {', '.join(names)}；"
            f"{len(self._tools)} 个工具 ≈ {self.estimate_tokens()} tokens/请求"
        )

    # ── 延迟揭示 ──────────────────────────

    def should_defer(self) -> bool:
        """MCP 工具是否延迟揭示（默认 ``auto``）。

        - ``auto``：估算超过 :data:`DIRECT_INJECT_TOKENS` 就延迟。小 server 直接给，
          省掉一次搜索往返；大 server（GitHub 那种 26 工具）常驻成本归零。
        - ``all`` / ``none``：显式强制，通过 ``GANGGE_MCP_DEFER`` 设置。
        """
        raw = (os.environ.get(DEFER_ENV) or "auto").strip().lower()
        if raw in ("none", "0", "false", "off", "no"):
            return False
        if raw in ("all", "1", "true", "on", "yes"):
            return True
        return self.estimate_tokens() > DIRECT_INJECT_TOKENS

    def prompt_hint(self) -> str:
        """系统提示里的一行索引：告诉模型有哪些外部工具、该怎么取。

        比逐个发 stub 便宜一个数量级——Blender 9 个工具约 1.2k tokens，
        这行约 60 tokens。
        """
        tools = self.get_all_tools()
        if not tools:
            return ""
        by_server: dict[str, int] = {}
        for t in tools:
            by_server[t.server_name] = by_server.get(t.server_name, 0) + 1
        parts = "、".join(f"{name}({count} 个工具)" for name, count in by_server.items())
        return (
            f"已挂载 MCP 外部工具：{parts}。"
            "它们的完整定义没有随请求发送（为了省 token），"
            "需要时先用 tool_search 搜索（例如 tool_search(\"3D 建模\")），"
            "搜到后可直接调用。"
        )

    def status_lines(self) -> list[str]:
        """每个 server 一行的状态表，供 ``--list-mcp`` 和 ``/mcp`` 使用。"""
        # 按最终发给模型的定义统计，而不是按 MCPTool：定义里还有 "[MCP:server]" 前缀，
        # 两边算法不一致会让单行数字和合计对不上。
        counts: dict[str, int] = {}
        chars: dict[str, int] = {}
        for d in self.build_tool_definitions():
            server = d.name.split("__", 1)[0] if "__" in d.name else ""
            counts[server] = counts.get(server, 0) + 1
            size = len(d.name) + len(d.description)
            size += len(json.dumps(d.input_schema, ensure_ascii=False))
            chars[server] = chars.get(server, 0) + size

        lines = [f" {'选':<2} {'server':<24} {'传输':<7} 说明"]
        for cfg in self.configs:
            selected = self.is_selected(cfg.name)
            count = counts.get(cfg.name, 0)
            if not selected:
                detail = "未选中 → 不连接，0 token"
            elif count:
                tokens = int(chars.get(cfg.name, 0) / CHARS_PER_TOKEN)
                detail = f"{count} 个工具 ≈ {tokens} tokens/请求"
            else:
                detail = "已选中但没连上（看 [MCP] 日志）"
            lines.append(
                f" {'●' if selected else '○':<2} {cfg.name:<24} {cfg.transport:<7} {detail}"
            )
        lines.append(
            f"合计 {len([c for c in self.configs if self.is_selected(c.name)])} 个已选中，"
            f"{len(self._tools)} 个工具 ≈ {self.estimate_tokens()} tokens/请求"
        )
        return lines

    # ── 工厂方法 ──────────────────────────

    @classmethod
    def from_document(
        cls,
        document: dict[str, Any],
        sources: dict[str, Path] | None = None,
    ) -> "MCPClientManager":
        configs = []
        for s in document.get("servers") or []:
            if not isinstance(s, dict) or not s.get("name"):
                continue
            configs.append(MCPServerConfig(
                name=str(s["name"]),
                transport=s.get("transport", "stdio"),
                command=s.get("command", ""),
                args=s.get("args", []),
                env=s.get("env", {}),
                url=s.get("url", ""),
                enabled=s.get("enabled", True),
                timeout=s.get("timeout", 30),
            ))
        manager = cls(configs)
        manager.sources = dict(sources or {})
        return manager

    @classmethod
    def from_config_file(cls, config_path: str) -> "MCPClientManager":
        """从单个 json 文件加载配置（测试与一次性脚本用）。"""
        path = Path(config_path)
        if not path.exists():
            logger.info(f"[MCP] 配置文件不存在 {config_path}，跳过 MCP 初始化")
            return cls([])
        document = _read_document(path)
        return cls.from_document(document, {str(s.get("name")): path for s in document.get("servers") or []})

    @classmethod
    def from_workspace(
        cls,
        workspace_dir: str | Path = ".",
        selection: str | None = None,
        *,
        connect: bool = True,
    ) -> "MCPClientManager":
        """按 ``用户级 → 项目级 → 临时层`` 合并配置，并按选择连接。

        选择来源优先级：参数 ``selection`` > 环境变量 ``GANGGE_MCP_SERVERS`` >
        配置里的 ``enabled``。没被选中的 server 不启动进程、不产生工具定义。

        分层的意义：Blender / GitHub 这种全局能力配在 ``~/.gangge/mcp_servers.json``
        就到处可用；项目专属 server 放工作目录，换项目不会互相污染。
        """
        document, sources = load_merged_config(workspace_dir)
        manager = cls.from_document(document, sources)
        raw = selection if selection is not None else os.environ.get(SELECTION_ENV)
        manager.select(parse_selection(raw, manager.available_names))
        if connect:
            manager.connect_all()
        return manager

    # ── 连接管理 ──────────────────────────

    def add_server(self, cfg: MCPServerConfig) -> list[str]:
        """运行时新增一个 server：连上并返回新工具名。

        同名旧定义会被替换（先断开旧连接、清掉旧工具），避免出现两份同名工具。
        """
        old_conn = self._connections.pop(cfg.name, None)
        if old_conn is not None:
            try:
                old_conn.disconnect()
            except Exception:
                pass
        self._tools = [t for t in self._tools if t.server_name != cfg.name]
        self.configs = [c for c in self.configs if c.name != cfg.name] + [cfg]
        if self.selection is not None:
            self.selection.add(cfg.name)

        conn = self._make_connection(cfg)
        conn.connect()
        self._connections[cfg.name] = conn
        new_names: list[str] = []
        for t in conn.list_tools():
            self._tools.append(MCPTool(
                server_name=cfg.name,
                name=t["name"],
                full_name=f"{cfg.name}__{t['name']}",
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {"type": "object", "properties": {}}),
            ))
            new_names.append(f"{cfg.name}__{t['name']}")
        logger.info("[MCP] 运行时新增 %s: %d 个工具", cfg.name, len(new_names))
        return new_names

    def connect_all(self):
        """只连接被选中的 MCP Server，拉取工具列表。

        选择为 ``None`` 时跟随配置的 ``enabled``；显式选择则以此为准。未选中的
        server 连进程都不启动——不连就没有定义，也就不占 token。
        """
        # 先清空：重复调用 connect_all 否则会把同一批 tool 再 append 一遍，
        # full_name 冲突后 _find_tool 只会命中旧的那条。
        self.disconnect_all()
        for cfg in self.configs:
            if not self.is_selected(cfg.name):
                logger.debug("[MCP] %s 未被选中，跳过", cfg.name)
                continue
            try:
                conn = self._make_connection(cfg)
                conn.connect()
                self._connections[cfg.name] = conn
                # 拉取工具列表并注册
                raw_tools = conn.list_tools()
                for t in raw_tools:
                    self._tools.append(MCPTool(
                        server_name=cfg.name,
                        name=t["name"],
                        full_name=f"{cfg.name}__{t['name']}",
                        description=t.get("description", ""),
                        input_schema=t.get("inputSchema", {"type": "object", "properties": {}}),
                    ))
                logger.info(f"[MCP] {cfg.name}: 注册 {len(raw_tools)} 个工具")
            except Exception as e:
                logger.warning(f"[MCP] 连接 {cfg.name} 失败: {e}，跳过")
        if self._tools:
            logger.info(self.describe())

    def disconnect_all(self):
        for conn in self._connections.values():
            try:
                conn.disconnect()
            except Exception:
                pass
        self._connections.clear()
        self._tools.clear()

    # ── 工具访问 ──────────────────────────

    def get_all_tools(self) -> list[MCPTool]:
        return list(self._tools)

    def has_tool(self, full_name: str) -> bool:
        """精确判断某个名字是否是本管理器注册的 MCP 工具。

        调用方（loop.py / pipeline.py）用它替代 ``"__" in name``：后者会把
        名字里恰好含双下划线的本地工具错误地路由到 MCP。
        """
        return self._find_tool(full_name) is not None

    def call_tool(self, full_name: str, arguments: dict) -> str:
        """
        根据 full_name（如 "autocad__draw_circle"）找到对应 Server 并调用。
        返回字符串结果，供 LLM 作为 tool_result 消费。
        失败时返回值以 ``[错误]`` 开头，上层据此置 is_error。
        """
        tool = self._find_tool(full_name)
        if tool is None:
            return f"{_ERROR_PREFIX} 未找到 MCP 工具: {full_name}"
        conn = self._connections.get(tool.server_name)
        if conn is None:
            return f"{_ERROR_PREFIX} MCP Server '{tool.server_name}' 未连接"
        try:
            result = conn.call_tool(tool.name, arguments)
            logger.debug(f"[MCP] {full_name}({arguments}) → {str(result)[:200]}")
            return result
        except Exception as e:
            return f"{_ERROR_PREFIX} MCP 调用 {full_name} 失败: {e}"

    def build_tool_definitions(self) -> list[ToolDefinition]:
        """
        把 MCP 工具包装成与本地工具同类型的 ``ToolDefinition``。

        必须是 ``ToolDefinition`` 而不是 dict：``layer5_llm`` 的两个 provider
        都调用 ``t.to_openai_schema()`` / ``t.to_anthropic_schema()``，传 dict
        会在序列化阶段直接 AttributeError，MCP 工具根本到不了模型。
        """
        defs: list[ToolDefinition] = []
        for t in self._tools:
            defs.append(ToolDefinition(
                name=t.full_name,
                description=f"[MCP:{t.server_name}] {t.description}",
                input_schema=t.input_schema,
            ))
        return defs

    # ── 内部 ──────────────────────────────

    def _make_connection(self, cfg: MCPServerConfig):
        if cfg.transport == "stdio":
            return StdioMCPConnection(cfg)
        elif cfg.transport == "sse":
            return SSEMCPConnection(cfg)
        else:
            raise ValueError(f"不支持的传输类型: {cfg.transport}")

    def _find_tool(self, full_name: str) -> MCPTool | None:
        for t in self._tools:
            if t.full_name == full_name:
                return t
        return None
