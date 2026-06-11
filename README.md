<div align="center">

# ⚡ Gangge Code

**AI 编程助手 · 通用 Agent 框架 — 写代码、写小说、做研究，一个引擎全搞定**

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-blue?logo=python" />
  <img src="https://img.shields.io/badge/license-MIT-green" />
  <img src="https://img.shields.io/badge/LLM-DeepSeek%20%7C%20Claude%20%7C%20OpenAI%20%7C%20Ollama-orange" />
</p>

**💻 AI 编程助手 · 📖 小说创作 · 🔍 联网研究 · 🧩 可扩展**

[English](./README_EN.md) | 中文

</div>

---

## ✨ 特色对比

| | Gangge Code | Claude Code | ChatGPT / Copilot |
|--|--|--|--|
| **Agentic Loop + 工具调用** | ✅ 自动规划→执行→验证 | ✅ | ❌ 仅输出代码 |
| **多 LLM 支持** | ✅ DeepSeek / Claude / OpenAI / Ollama | ❌ 仅 Claude | ❌ 仅 OpenAI |
| **本地运行** | ✅ 全本地部署 | ❌ 需订阅 | ❌ 云端依赖 |
| **桌面 GUI** | ✅ PyQt6 | ❌ CLI only | ❌ |
| **Shadow Git 回滚** | ✅ | ✅ | ❌ |
| **Memory Bank 跨会话** | ✅ | ✅ | ❌ |
| **LSP 语法检查** | ✅ | ❌ | ❌ |
| **AI 自建工具** | ✅ 运行时创建新工具 | ❌ | ❌ |
| **MCP 协议接入** | ✅ 标准的 MCP 客户端 | ✅ | ❌ |
| **Agent Profile 场景化** | ✅ 编程/小说/研究自动切换 | ❌ 全部工具暴露 | ❌ |
| **小说创作引擎** | ✅ Dramatica-Flow 五层流水线 | ❌ | ❌ |
| **完全开源可改** | ✅ | ❌ | ❌ |
| **性价比（DeepSeek）** | ✅ 成本低 10x | ❌ | ❌ |

---

## 🚀 快速开始

### 1. 安装

```bash
git clone https://github.com/ydsgangge-ux/gangge-code.git
cd gangge-code
pip install -e ".[gui]"   # 带桌面 GUI（推荐）
# pip install -e .         # 最小安装（CLI only）
```

### 2. 配置

```bash
cp .env.example .env
# 编辑 .env，填入 API Key
```

最小配置（DeepSeek）：
```ini
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-xxx
```

### 3. 使用

```bash
# 桌面 GUI（推荐）
python desktop/app.py

# 单次任务
gangge "创建一个带用户认证的 FastAPI 项目"

# 交互式 REPL
gangge
```

---

## 💻 AI 编程助手

### 核心能力

在聊天窗口用自然语言描述需求，AI 自动理解意图、规划步骤、调用工具完成。

```
你：帮我创建一个 FastAPI 项目，带用户注册登录功能

AI：好的，我来规划一下：
     📋 任务清单 (0/4)
     1. [ ] 创建项目结构
     2. [ ] 实现用户模型 (User)
     3. [ ] 实现注册/登录 API
     4. [ ] 添加 JWT 认证中间件

     ▶ 开始执行...
     ✓ 已创建 app/models/user.py (42 行)
     ✓ 已创建 app/routes/auth.py (83 行)
     ✓ pytest 全部通过
     ✅ 所有任务完成
```

### 内置工具（按场景动态暴露）

系统会根据任务类型自动切换 Agent Profile，只暴露相关工具：

| Profile | 工具数 | 触发场景 |
|---------|--------|----------|
| **Coding** | 13 个 | 创建/写/修改/实现/开发/fix/build |
| **Novel** | 25 个 | 小说/写作/章节/角色/大纲 |
| **Research** | 10 个 | 搜索/查找/调研/联网 |

通用工具（全部 Profile 可用）：`read_file` · `write_file` · `edit_file` · `bash` · `grep` · `glob` · `list_dir` · `web_fetch` · `ask_user` · `lint_check` · `find_symbol` · `find_references` · `TodoWrite`

### 关键机制

- **Runtime 自动推进 Todo**：系统自动跟踪任务进度，无需模型手动更新
- **连续只读自动退出**：模型连续 3 轮只读不写 → 自动结束，避免无谓消耗
- **上下文 Token 压缩**：使用率 > 92% 时自动压缩历史，保持上下文窗口可控
- **429 限流重试**：指数退避 + 随机抖动，API 限流时自动重试
- **AI 自建工具**：`create_tool` 在运行时创建新工具，4 重安全门检测
- **Memory Bank**：跨会话记录项目进度 + 决策日志，防止重复犯错
- **Shadow Git Checkpoint**：AI 修改前自动创建 Git 检查点，一键回滚
- **LSP 语法检查**：写完代码自动运行 pyright/ruff，错误即时修复
- **`.ganggerules` 项目规则**：项目根目录定义编码规范、测试要求、架构约定

---

## 🔌 可扩展性

### MCP 协议支持
完整的 MCP 客户端管理器，支持 stdio 和 SSE 两种传输方式：

```json
// .gangge/mcp_servers.json
{
  "mcpServers": {
    "database": { "command": "node", "args": ["db-mcp-server.js"] }
  }
}
```

### 插件系统
- AI 自建工具自动保存到 `.gangge/plugins/`，下次启动自动加载
- `.ganggerules` 定义项目级编码规范、测试要求、架构约定

---

## 📖 小说创作引擎

系统内置 Dramatica-Flow 完整小说创作系统（可选功能，不影响编程）：

- **聊天式创作**：说「加个角色」「写下一章」，AI 自动调用对应工具
- **五层写作流水线**：Architect 规划 → Writer 撰写 → Audit 检查 → Revise 修订
- **16 个专用小说工具**：角色管理、大纲生成、章节写作、审计修订、导出等
- **叙事知识图谱**：因果链追踪、伏笔管理、情感弧线
- **仿写模式**：导入 TXT 小说，自动分析风格并仿写
- **专业 UI 面板**：仪表盘、角色、大纲、章节、世界观、追踪、词库

---

## 📐 架构

```
用户界面层 (Layer 1)
  CLI / REPL / TUI / PyQt6 桌面 GUI
        │
  Agentic Loop 核心引擎 (Layer 3)
        │
  ├─ 工具执行层 (Layer 3 tools)
  │    Coding 工具 · Novel 工具 · MCP 工具 · 插件工具
  │    └── Agent Profile 动态暴露（按场景自动切换）
  │
  ├─ 会话管理层 (Layer 2)
  │    持久化 · Memory Bank · 上下文压缩 · Todo 状态管理
  │
  ├─ 权限安全层 (Layer 4)
  │    规则引擎 · 危险检测 · Shadow Git 回滚
  │
  └─ LLM 适配层 (Layer 5)
       DeepSeek · OpenAI · Claude · Ollama
```

---

## ⚙️ 配置

```ini
LLM_PROVIDER=deepseek          # deepseek / openai / anthropic / ollama
DEEPSEEK_API_KEY=sk-xxx
MAX_ROUNDS=30                  # 最大工具调用轮数
LLM_TIMEOUT=300                # LLM 响应超时（秒）
```

---

## 🗺️ Roadmap

- [x] Agentic Loop + TodoWrite 自动状态管理
- [x] Agent Profile 场景化工具动态暴露
- [x] Runtime 上下文压缩（Token 阈值触发）
- [x] 429 限流指数退避重试
- [x] Memory Bank + Decision Log
- [x] Shadow Git Checkpoint / 回滚
- [x] LSP 语法检查（pyright/ruff/pylint）
- [x] AI 自建工具 + 插件系统
- [x] MCP 协议支持（stdio + SSE）
- [x] Dramatica-Flow 小说创作引擎
- [x] PyQt6 桌面 GUI
- [x] 多种 LLM 支持（DeepSeek / Claude / OpenAI / Ollama）
- [ ] Web UI（远程访问）
- [ ] 向量索引 (RAG) 局部上下文
- [ ] 国际化 (i18n) 英语语言包
- [ ] 协作模式（多人共享会话）

---

## 📜 License

MIT © [ydsgangge-ux](https://github.com/ydsgangge-ux)