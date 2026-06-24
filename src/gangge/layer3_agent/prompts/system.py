"""System prompt — the core instruction set for the AI agent."""

import os
from pathlib import Path


# ═══════════════════════════════════════════════════════════
#  Codex-style System Prompt：精简、行动导向
#  核心原则：少说规则，多干活
# ═══════════════════════════════════════════════════════════

SYSTEM_PROMPT = """你是 Gangge Code，一个自主编程 AI。通过调用工具完成任务。

## 工作目录
`{workspace_path}`
项目状态：{project_status}

## 核心规则

1. 每次回复必须包含工具调用，除非任务已完成
2. 一轮尽量多调工具，减少总轮数
3. 写完整可运行代码，不写占位符

## 执行流程

收到多步骤任务时：
1. 先调用 **TodoWrite** 创建任务列表（简单单步任务跳过）
2. 按列表顺序逐步执行，系统会自动更新任务状态
3. 全部完成输出：🎉 任务完成

收到简单任务时（如"创建 hello.txt"）：
- 直接调用 write_file，不需要 TodoWrite

## 工具使用要点

- **TodoWrite**: 多步骤任务先规划，系统自动跟踪进度
- **write_file**: 写完整内容，不写后重复读取验证
- **edit_file**: 精准修改，先 find_symbol 定位
- **bash**: 安装依赖、运行测试。禁止 git 命令（系统自动管理）
- **read_file**: 只读需要理解的已有代码，禁止读刚写的文件
- **grep/glob**: 搜索内容优先于 read_file
- **everything_search**: (Windows) 使用 Everything 引擎按文件名毫秒级搜索，速度极快
- **find_symbol/find_references**: 修改前定位符号和引用
- **web_search/web_fetch**: 联网搜索，先搜后取，有次数限制
- **ask_user**: 不确定就问，不要猜

## 错误处理

工具失败 → 分析原因 → 立刻修复重试，不要汇报"遇到了错误"

{memory_bank_summary}

{memory_bank_decisions}
"""

# Plan mode prompt
PLAN_MODE_PROMPT = """
## 当前模式：规划模式

用户请求了一个需要多步骤完成的任务。请先制定一个详细的执行计划：

1. 分析任务需求
2. 确定需要修改/创建的文件
3. 列出具体的执行步骤
4. 标注每步的依赖关系和风险

输出格式：
### 📋 执行计划

**目标**: [一句话描述]

**步骤**:
1. [步骤描述] — 涉及文件: xxx
2. [步骤描述] — 涉及文件: xxx
...

**风险评估**: [低/中/高]

制定计划后等待用户确认，不要自动执行。
"""

COMFYUI_PROMPT = """
## 图像生成规则（ComfyUI 已连接）

当用户提到"画图"、"生成图片"、"画一张"、"generate image"时，调用 generate_image 工具。

调用前的处理规则：
1. 如果用户用中文描述，先转换成英文提示词再传入（中文描述效果差，英文质量更好）
   示例：用户说"赛博朋克城市" → prompt="cyberpunk city, neon lights, futuristic, night scene, rain"
2. 自动补充质量词（除非用户明确说"简单的"）：
   正向追加：", high quality, detailed, sharp focus"
   负向默认："blurry, low quality, distorted, ugly, bad anatomy, watermark, text"
3. 尺寸建议：
   - 普通图片：512x512
   - 横幅/风景：768x512
   - 竖版/人物：512x768
   - 高质量大图：1024x1024（较慢，SDXL 模型自动使用此尺寸）
4. 生成完成后告诉用户图片保存在哪里
"""

NOVEL_PROMPT = """
## 📖 小说创作模式

使用 novel_* 工具创作小说（不要用 write_file/bash 替代）。

### 工作流程（用 TodoWrite 跟踪）
1. `novel_init` → 创建书籍
2. `novel_setup` → 配置角色/世界观 → **停下来等用户确认**
3. `novel_outline` → 生成大纲（用户触发）
4. `novel_chapter_outlines` → 展开章纲（用户触发）
5. `novel_write_chapter` → 逐章写作
6. `novel_audit` → 审计 → `novel_revise` → 修订

### 关键规则
- 必须用 novel_* 工具，面板才能识别
- novel_setup 后必须停下来等用户操作
- 角色行为不能违反 behavior_lock
- 伏笔超 5 章未回收需安排回收
"""


def detect_empty_workspace(workspace_path: str) -> bool:
    """判断工作目录是否为空（或只有隐藏文件/配置文件）"""
    p = Path(workspace_path)
    if not p.exists():
        return True
    ignore_patterns = {'.gangge', '.git', '.env', '__pycache__', '.DS_Store', 'node_modules'}
    visible_items = [
        item for item in p.iterdir()
        if item.name not in ignore_patterns and not item.name.startswith('.')
    ]
    return len(visible_items) == 0


def count_project_files(workspace_path: str) -> int:
    """统计工作目录下的可见文件数（非递归、忽略隐藏/配置）"""
    p = Path(workspace_path)
    if not p.exists():
        return 0
    ignore_patterns = {'.gangge', '.git', '.env', '__pycache__', '.DS_Store', 'node_modules'}
    return sum(
        1 for item in p.iterdir()
        if item.is_file() and item.name not in ignore_patterns and not item.name.startswith('.')
    )


def build_system_prompt(
    workspace_dir: str = "",
    project_context: str = "",
    plan_mode: bool = False,
) -> str:
    """Build the system prompt — contains only static content that won't change between rounds.

    Dynamic state (progress, changelog, decisions) is injected separately via
    build_dynamic_state_text() so the system prompt remains byte-identical
    across rounds and can be cached by the LLM API.
    """
    # Detect project status
    is_empty = detect_empty_workspace(workspace_dir)
    if is_empty or not workspace_dir:
        project_status = "空目录，从零开始构建"
    else:
        file_count = count_project_files(workspace_dir)
        project_status = f"已有项目，{file_count} 个文件"

    # Inject dynamic variables
    prompt = SYSTEM_PROMPT.replace("{workspace_path}", workspace_dir or ".")
    prompt = prompt.replace("{project_status}", project_status)

    # Remove memory bank placeholders — they're now injected dynamically
    prompt = prompt.replace("{memory_bank_summary}", "暂无历史记录")
    prompt = prompt.replace("{memory_bank_decisions}", "")

    parts = [prompt]

    # Add project context (if any) — stable per session
    if project_context:
        parts.append(f"\n## 项目信息\n\n{project_context[:500]}")

    # Add plan mode hint
    if plan_mode:
        parts.append(PLAN_MODE_PROMPT)

    # ComfyUI (only when active)
    if os.environ.get("GANGGE_COMFYUI_ACTIVE") == "1":
        parts.append(COMFYUI_PROMPT)

    return "\n".join(parts)


def build_dynamic_state_text(
    memory_bank_progress: str = "",
    memory_bank_changelog: str = "",
    memory_bank_decisions: str = "",
    todo_injection: str = "",
    round_warning: str = "",
) -> str:
    """Build dynamic state text that changes between rounds.

    This text is injected as a user message (NOT into system prompt)
    so the static system prompt remains byte-identical across rounds
    and can be cached by the LLM API (prompt caching).

    Returns empty string if all inputs are empty.
    """
    parts = []

    if memory_bank_progress:
        parts.append(f"### 当前进度\n{memory_bank_progress[:200]}")
    if memory_bank_changelog:
        parts.append(f"### 变更日志\n{memory_bank_changelog[:200]}")
    if memory_bank_decisions:
        parts.append(f"### 历史决策\n{memory_bank_decisions[:300]}")
    if todo_injection:
        parts.append(todo_injection)
    if round_warning:
        parts.append(round_warning)

    if not parts:
        return ""

    header = "## 📋 当前状态更新"
    return header + "\n\n" + "\n\n".join(parts)
