# 架构与扩展点

> README 回答「能做什么」，这份文档回答「怎么搭起来的、想改哪块该动哪个文件」。
> 内容面向维护者，不影响安装和使用。

## 分层

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
