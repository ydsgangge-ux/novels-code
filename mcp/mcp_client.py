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
import subprocess
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


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


# ─────────────────────────────────────────
# stdio 传输（最常见：本地 Node.js / Python MCP Server）
# ─────────────────────────────────────────

class StdioMCPConnection:
    """
    通过 stdin/stdout 与本地 MCP Server 进程通信。
    协议：每行一条 JSON-RPC 2.0 消息。
    """

    def __init__(self, config: MCPServerConfig):
        self.config = config
        self._proc: subprocess.Popen | None = None
        self._req_id = 0

    def connect(self):
        import os
        env = {**os.environ, **self.config.env}
        cmd = [self.config.command] + self.config.args
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        # MCP 握手：initialize
        self._call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "gangge-code", "version": "1.0.0"},
        })
        logger.info(f"[MCP] stdio 连接成功: {self.config.name}")

    def disconnect(self):
        if self._proc:
            self._proc.terminate()
            self._proc = None

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
        return "\n".join(parts) if parts else json.dumps(result)

    # ── 内部 ──────────────────────────────

    def _call(self, method: str, params: dict) -> dict:
        self._req_id += 1
        payload = _rpc_request(method, params, self._req_id)
        self._proc.stdin.write(payload)
        self._proc.stdin.flush()
        raw = self._proc.stdout.readline()
        if not raw:
            raise RuntimeError(f"[MCP] {self.config.name} 进程无响应")
        return _parse_response(raw)


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
        return "\n".join(
            item["text"] for item in contents if item.get("type") == "text"
        ) or json.dumps(result)


# ─────────────────────────────────────────
# MCP 客户端管理器（核心类）
# ─────────────────────────────────────────

class MCPClientManager:
    """
    管理所有 MCP Server 的连接，对外提供统一接口。

    使用方式（在 AgenticLoop 初始化时）：
        manager = MCPClientManager.from_config_file(".gangge/mcp_servers.json")
        manager.connect_all()
        # 获取所有 MCP 工具，注入进 ToolRegistry
        for tool in manager.get_all_tools():
            registry.register_mcp_tool(tool, manager)
    """

    def __init__(self, configs: list[MCPServerConfig]):
        self.configs = configs
        self._connections: dict[str, StdioMCPConnection | SSEMCPConnection] = {}
        self._tools: list[MCPTool] = []

    # ── 工厂方法 ──────────────────────────

    @classmethod
    def from_config_file(cls, config_path: str) -> "MCPClientManager":
        """从 .gangge/mcp_servers.json 加载配置"""
        import pathlib
        path = pathlib.Path(config_path)
        if not path.exists():
            logger.info(f"[MCP] 配置文件不存在 {config_path}，跳过 MCP 初始化")
            return cls([])
        data = json.loads(path.read_text(encoding="utf-8"))
        configs = []
        for s in data.get("servers", []):
            configs.append(MCPServerConfig(
                name=s["name"],
                transport=s.get("transport", "stdio"),
                command=s.get("command", ""),
                args=s.get("args", []),
                env=s.get("env", {}),
                url=s.get("url", ""),
                enabled=s.get("enabled", True),
                timeout=s.get("timeout", 30),
            ))
        return cls(configs)

    # ── 连接管理 ──────────────────────────

    def connect_all(self):
        """连接所有已启用的 MCP Server，拉取工具列表"""
        for cfg in self.configs:
            if not cfg.enabled:
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

    def call_tool(self, full_name: str, arguments: dict) -> str:
        """
        根据 full_name（如 "autocad__draw_circle"）找到对应 Server 并调用。
        返回字符串结果，供 LLM 作为 tool_result 消费。
        """
        tool = self._find_tool(full_name)
        if tool is None:
            return f"[错误] 未找到 MCP 工具: {full_name}"
        conn = self._connections.get(tool.server_name)
        if conn is None:
            return f"[错误] MCP Server '{tool.server_name}' 未连接"
        try:
            result = conn.call_tool(tool.name, arguments)
            logger.debug(f"[MCP] {full_name}({arguments}) → {result[:200]}")
            return result
        except Exception as e:
            return f"[MCP 调用错误] {full_name}: {e}"

    def build_tool_definitions(self) -> list[dict]:
        """
        生成 OpenAI/Anthropic 格式的 tool definitions，直接追加进 LLM 请求的 tools 列表。
        每个工具的描述里会说明它来自哪个 MCP Server。
        """
        defs = []
        for t in self._tools:
            defs.append({
                "name": t.full_name,
                "description": f"[MCP:{t.server_name}] {t.description}",
                "input_schema": t.input_schema,
            })
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
