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

### 运行时组合（`gangge/runtime/`）

五个 layer 提供**实现**，`runtime` 决定**装哪些**。启动不再是"从头 new 一遍对象"，而是
把一棵插件树按 profile 组装起来：

```
profile（具名组装）
  └─ bundles（有序 patch 文档）  →  profile patch  →  home patch  →  --patch
```

- **一切皆插件**：`llm` / `tools` / `permission` / `session` / `telemetry` / `memory-bank`
  都是插件，各自占一个 `ctx.<key>`。没有需要打补丁的特权内核。
- **能力 seam**：接口声明（`runtime/seams.py`）→ 实现（`runtime/plugins/*`）→ 消费方。
  换实现不改消费方。
- **`inject` 声明依赖**：依赖未就绪的插件自动挂起，服务一出现就激活 —— 加载顺序由依赖
  表达，不靠手写启动序列。
- **注册是可逆副作用**：`ctx.effect()` / `ctx.on()` / `ctx.provide()` 返回 disposer，
  卸载时逆序撤销。
- **事件是扩展点**：`turn/start`、`step/start`、`tools/pre-execute`、`permission/check`
  等 22 个事件，各有唯一分发模式（`emit` / `waterfall` / `serial` / `parallel` / `bail`）。

查看当前实际组装出的树（静态，不挂载、不需要 API Key）：

```bash
gangge --list-profiles          # 可用 profile
gangge --dump-config            # 每一条行的来源层与最终配置
gangge --show-events            # 事件及其分发模式
gangge --show-seams             # 能力 seam 与内置实现
gangge -P minimal               # 用极简 profile 启动
gangge --set tools.profile=novel --dump-config   # 临时覆盖某一行
```

三个内置 profile：

| profile | 说明 |
|---|---|
| `standard` | base + full-tools + memory-bank，全能力 |
| `minimal` | 只要 base，不装树外插件、不开浏览器、不落遥测 |
| `novel` | 工具面收敛到小说 profile，关掉浏览器 |

自己加一个：在 `.gangge/profiles/demo.yml` 写 `bundles: [base]`，再用
`.gangge/cordis.patch.yml` 覆盖任意行即可，**不需要改一行 Python**。

### 弱模型护栏（`guardrails` bundle）

免费/小模型（GLM-4.7-Flash、Qwen 7B、本地 8B）失败的原因基本不是"想不明白"，而是
**手脚不稳**：参数名拼错、类型写成字符串、把文件内容包进代码围栏、同一个调用反复发、
被 ANSI 颜色码带偏。这些都发生在工具边界上，所以修在工具边界：

| 插件 | 挂钩 | 作用 |
|---|---|---|
| `schema-guard` | `tools/pre-execute` | 先**修**再**说**。`file_path`→`path`、`"5"`→`5`、去 ``` 围栏、丢弃多余字段；工具名拼错（`read` / `readfile`）时用 difflib 提示最近的真实工具名并给出可抄示例；修不动的返回「必填字段 + 全部参数 + 你实际传的 + 正确示例」 |
| `result-pruner` | `tools/post-execute` | 去 ANSI/CR 进度条、压缩空行、头尾截断（保留结尾，报错在结尾）。**只改模型看到的文本，`metadata['value']` 保留完整真值** |
| `repeat-guard` | `tools/pre+post` | 同一 `(工具, 参数)` 第 3 次起，在结果尾部追加「换参数 / 换工具 / 直接用已有结果」 |
| `next-step` | `tools/post-execute` | 每种工具**每回合只提示一次**、一行以内：read 完该 edit、改完该 verify、报错该怎么改 |
| `stop-guard` | `agent/turn-stopping` | 拒绝**提前收工**：只读不写、改了没验证，都不让结束回合（每条件最多拦 2 次） |

效果：
- 参数传错**不再浪费一轮**，多数直接被改对；改不动的拿到的是一段能照着抄的说明，而不是 `TypeError: unexpected keyword argument`。
- 工具名幻觉不再死循环，而是「你是不是想用 `read_file`？示例：`read_file({"path": "src/main.py"})`」。
- 模型"说完了"但一个文件都没改 → 被推回去动手；改了没跑 → 被推去验证。

`stop-guard` 挂在合成事件 `agent/turn-stopping` 上，`AgenticLoop` 在准备接受停止时咨询它：

```python
directive = await ctx.serial("agent/turn-stopping", state)   # loop 内部
# state = {round, text, user_task, has_modified_files,
#          verified_after_write, tool_history, files_touched, todo_pending}
```

返回 `TurnDirective.keep_going(msg)` 就再跑一轮；返回 `None` 交回 loop 原有规则。
`turn/start`、`turn/end`、`turn/aborted` 现在由 `run()` 统一括起来，所有退出路径
（正常结束 / 达到上限 / 抛异常）都恰好发一次。

用强模型想关掉：

```bash
gangge --set repeat-guard.limit=99                 # 关掉重复熔断
gangge --set stop-guard.require_verification=false # 允许改完不验证就收工
gangge --set next-step.on_error_only=true          # 只在报错时给提示
gangge --set schema-guard.coerce=false             # 只校验不修复
```

不 boot profile 的宿主（PyQt 桌面端）可以只挂流水线：

```python
from gangge.runtime.pipeline import tool_pipeline

service, ctx = tool_pipeline(registry, workspace=ws, repeat_limit=3)
loop = AgenticLoop(..., tool_service=service, context=ctx)
```

### 请求级钩子（`llm/request` · `llm/response`）

`AgenticLoop` 的每一次模型调用（主循环、压缩摘要、问答短路）都经过 `runtime.llm.chat()`，
在真正发出去之前给你一次改写机会：

```python
def cheaper_for_summaries(request, next):
    last = request["messages"][-1].get_text()
    if "压缩以下对话" in last:
        request["model"] = "glm-4.7-flash"      # 摘要不值得用贵模型
    return next(request)

ctx.on("llm/request", cheaper_for_summaries)
```

请求 dict 的字段：`messages` / `tools` / `system` / `model`。可以改的：

| 想做的事 | 写法 |
|---|---|
| 按任务分层用模型 | `request["model"] = "..."`（**只影响本次调用**，事后自动还原） |
| 出网前打码密钥 | 遍历 `request["messages"]` 改 `block.text` |
| 临时关闭工具 | `request["tools"] = []` |
| 请求前裁上下文 | `request["messages"] = request["messages"][-10:]` |

`llm/response` 带两个参数 `(response, meta)`，`meta` 含 `latency_ms` / `model` / `adapters`，
适合记账。`runtime.llm.stats` 也有累计计数。

> 调用方传入的 adapter 会被**钉住**（`adapter=self.llm`），工厂默认实例绝不会悄悄替换它；
> `model` 覆盖是 try/finally 作用域内的临时修改，失败也会还原。

### step 生命周期

主循环每轮发一次 `step/start`，请求返回后发 `step/end`：

```python
ctx.on("step/end", lambda s: print(s["round"], s["latency_ms"], s["stop_reason"],
                                   s["tool_calls"], s["error"]))
```

本项目的 **step = 一次模型请求**；随后的工具执行由 `tool/call` → `tool/result` 单独计时。
超时和异常路径也会发 `step/end`（`error="timeout"` 等），保证 start/end 配对。

### 中断消息链修复

工具执行到一半被取消（Ctrl+C / 停止按钮 / `task.cancel()`）时，历史里会留下
"assistant 请求了 N 个工具，却只有 M 个结果"。下一次请求会直接被供应商拒绝
（`tool_use ids were found without tool_result blocks`）。

`run()` 在异常和正常退出两条路径上都会调用 `_repair_message_chain()`：给未配对的
`tool_use` 补一条 `is_error=True` 的合成结果

```
[中断] 工具 `bash` 的调用被取消，结果未知。
如果它是只读或幂等操作，可以重试；否则请先确认是否已经生效，避免重复执行造成副作用。
```

已配对的结果原样保留；重复调用是幂等的（已完整时直接返回 0，不发事件）。

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