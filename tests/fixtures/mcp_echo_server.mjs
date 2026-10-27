#!/usr/bin/env node
// 阶段 0 spike：最小 MCP stdio Server（Node sidecar 原型）
//
// 目的只有一个——证明「Node 侧只用 MCP 说话」这条路在现有 Python 客户端上是通的：
//   node tests/fixtures/mcp_echo_server.mjs
// 然后用 .gangge/mcp_servers.json 指过来，模型就能看到 mock__echo 之类的工具。
//
// 协议：stdin/stdout 每行一条 JSON-RPC 2.0 消息；日志一律走 stderr
// （stdout 只跑协议，混入任何非 JSON 行都会让客户端解析失败）。

import readline from "node:readline";

const SERVER_INFO = { name: "gangge-echo-spike", version: "0.0.1" };

const TOOLS = [
  {
    name: "echo",
    description: "原样返回 message，用来验证 stdio 往返链路。",
    inputSchema: {
      type: "object",
      properties: { message: { type: "string", description: "要回显的文本" } },
      required: ["message"],
    },
  },
  {
    name: "add",
    description: "返回 a + b，验证参数透传与数值结果。",
    inputSchema: {
      type: "object",
      properties: { a: { type: "number" }, b: { type: "number" } },
      required: ["a", "b"],
    },
  },
  {
    name: "explode",
    description: "总是失败，用来验证 isError 会被标记成工具错误。",
    inputSchema: { type: "object", properties: {} },
  },
];

function send(msg) {
  process.stdout.write(JSON.stringify(msg) + "\n");
}

function textResult(text, isError = false) {
  const result = { content: [{ type: "text", text }] };
  if (isError) result.isError = true;
  return result;
}

function handleToolCall(name, args) {
  switch (name) {
    case "echo":
      return textResult(`echo: ${args.message ?? ""}`);
    case "add":
      return textResult(String(Number(args.a) + Number(args.b)));
    case "explode":
      return textResult("故意失败：模拟工具执行错误", true);
    default:
      return null;
  }
}

const rl = readline.createInterface({ input: process.stdin, terminal: false });

rl.on("line", (line) => {
  const raw = line.trim();
  if (!raw) return;

  let msg;
  try {
    msg = JSON.parse(raw);
  } catch {
    process.stderr.write(`[echo-server] 非 JSON 输入，已忽略: ${raw}\n`);
    return;
  }

  const { id, method, params } = msg;

  // 通知没有 id，按 MCP 规范不回包。
  if (id === undefined) {
    if (method === "notifications/initialized") {
      process.stderr.write("[echo-server] 握手完成\n");
    }
    return;
  }

  switch (method) {
    case "initialize":
      send({
        jsonrpc: "2.0",
        id,
        result: {
          protocolVersion: "2024-11-05",
          capabilities: { tools: { listChanged: false } },
          serverInfo: SERVER_INFO,
        },
      });
      break;

    case "ping":
      send({ jsonrpc: "2.0", id, result: {} });
      break;

    case "tools/list":
      send({ jsonrpc: "2.0", id, result: { tools: TOOLS } });
      break;

    case "tools/call": {
      const result = handleToolCall(params?.name, params?.arguments ?? {});
      if (result) {
        send({ jsonrpc: "2.0", id, result });
      } else {
        send({
          jsonrpc: "2.0",
          id,
          error: { code: -32601, message: `unknown tool: ${params?.name}` },
        });
      }
      break;
    }

    default:
      send({
        jsonrpc: "2.0",
        id,
        error: { code: -32601, message: `unknown method: ${method}` },
      });
  }
});

process.stderr.write("[echo-server] 已就绪，等待 JSON-RPC 请求\n");
