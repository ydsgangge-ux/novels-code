<div align="center">

# ⚡ Gangge Code

**AI Novel Writing Engine · Local Coding Assistant — Write Novels, Write Code, One Tool Does It All**

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-blue?logo=python" />
  <img src="https://img.shields.io/badge/license-MIT-green" />
  <img src="https://img.shields.io/badge/LLM-DeepSeek%20%7C%20Claude%20%7C%20OpenAI%20%7C%20Ollama-orange" />
</p>

**📖 Novel Writing (Dramatica-Flow) · 💻 AI Coding Assistant · Desktop GUI**

English | [中文](./README.md)

> Use natural language in the main chat window to write novels: say "add a character", "modify outline", "write next chapter", and AI automatically calls the right tools. Full AI coding capabilities preserved.

</div>

---

## 📖 Core Feature: AI Novel Writing Engine

Gangge Code integrates **Dramatica-Flow**, a complete novel creation system supporting end-to-end workflow from concept to manuscript:

### 🎯 Chat-Based Creation
Talk directly in the main chat window, AI understands intent and acts:
- "Add a childhood friend for the protagonist" → Auto-modifies character config
- "Chapter 3 is boring, add some suspense" → Adjusts chapter outline
- "Show me current progress" → Displays detailed status
- "Write next chapter" → Executes writing pipeline
- **Auto context injection on book select**: Characters, outline, foreshadowing, world state all loaded into system prompt for precise understanding

### 🏗️ Five-Layer Writing Pipeline
1. **Architect** — Analyzes outline, world state, prior summaries → generates chapter blueprint
2. **Writer** — Drafts content based on blueprint, maintains style consistency
3. **Post-write Validation** — Extracts causal chains, generates summaries, updates world state
4. **Auditor** — Checks logic gaps, foreshadowing consistency, character behavior plausibility
5. **Reviser** — Revisions based on audit feedback, loops until passing

### 🔧 16 Dedicated Tools
| Tool | Function |
|------|----------|
| `novel_init` | Create new book project |
| `novel_setup` | Configure characters/factions/locations/world rules |
| `novel_outline` | Generate/regenerate story outline |
| `novel_chapter_outlines` | Expand detailed chapter beats |
| `novel_write_chapter` | Write one chapter (fast mode supported) |
| `novel_audit` | Audit written chapters |
| `novel_revise` | Revise chapters with feedback |
| `novel_status` | View detailed progress & status |
| `novel_edit` | Modify any element (characters/relationships/hooks/outlines/chapters) |
| `novel_export` | Export full book (TXT/EPUB) |
| `novel_list_books` | List all books |
| `novel_navigate` | Quick file navigation (chapters/characters/outlines/status) |
| `novel_graph_query` | Query narrative knowledge graph |
| `novel_consistency_check` | Full-book consistency check |
| `novel_graph_rebuild` | Rebuild narrative graph |
| `novel_import` | Import TXT novel for imitation analysis |

### 📚 Import & Imitation Writing
- Supports importing TXT novels of hundreds of thousands of words
- Multi-region sampling analyzes writing style (beginning, middle, ending)
- Full-text scan extracts character information
- Structural analysis of plot arcs, turning points, emotional trajectories
- Dynamic style anchoring: matches reference text based on current writing progress
- Large files auto-chunked (>500K words split into 100K-word chunks)

### ⚡ Performance Optimizations
- **Fast Mode**: Skips Architect planning & Audit-Revision loop, 3x+ speedup
- **Parallel LLM Calls**: Causal chain extraction + summary generation run concurrently
- **Direct Execution**: Bypasses Agentic Loop overhead, calls tools directly
- **Context Compression**: Long novels auto-compress historical summaries, keeps context window manageable
- **Periodic Deep Review**: Triggers full consistency check every N chapters

### 🎨 Professional UI Panels
- **Dashboard** — Overview, statistics, quick actions
- **Characters** — Visual character management, edit attributes, view arcs
- **Outline** — Tree view of story arcs, drag-to-reorder
- **Chapters** — List all chapters, one-click write/audit/revise
- **Worldview** — Locations, factions, world rules overview
- **Tracking** — Foreshadowing management, causal chain visualization, emotional curves
- **Word Bank** — Custom vocabulary, style references

### 🧠 Narrative Knowledge Graph
- Narrative modeling system inspired by CodeGraph architecture
- Characters, events, relationships stored as graph nodes & edges
- SQLite persistence with complex query support
- Auto-tracks causal chains and foreshadowing closure status

---

## ✨ Feature Comparison

| | Gangge Code | ChatGPT / Copilot | Claude Code | Specialized Writers |
|--|--|--|--|--|
| **AI Novel Writing** | ✅ Dramatica-Flow | ❌ | ❌ | ❌ No Agent capability |
| **Chat-based novels** | ✅ Natural language driven | ❌ | ❌ | ❌ Fixed workflow |
| **5-layer pipeline** | ✅ Plan→Write→Audit→Revise | ❌ | ❌ | ❌ Single layer output |
| **Import & imitate** | ✅ Huge TXT support | ❌ | ❌ | ❌ Small file limit |
| **Narrative graph** | ✅ Causal+hook tracking | ❌ | ❌ | ❌ |
| Autonomous tool use | ✅ | ❌ Code only | ✅ | ❌ |
| Runs 100% locally | ✅ | ❌ | ❌ Subscription | ❌ Cloud dependent |
| DeepSeek native | ✅ 10x cheaper | ❌ | ❌ | ❌ |
| Desktop GUI | ✅ PyQt6 | ❌ | ❌ | Partial |
| Shadow Git rollback | ✅ | ❌ | ✅ | ❌ |
| Memory Bank | ✅ | ❌ | ✅ | ❌ |
| LSP syntax check | ✅ | ❌ | ❌ | ❌ |
| Batch task queue | ✅ | ❌ | ❌ | ❌ |
| MCP integration | ✅ | ❌ | ✅ | ❌ |
| Fully open source | ✅ | ❌ | ❌ | ❌ |

---

## 🚀 Quick Start

### 1. Clone

```bash
git clone https://github.com/ydsgangge-ux/gangge-code.git
cd gangge-code
```

### 2. Install (pick one)

```bash
# Option A: Minimal (CLI + TUI + Novel Writing)
pip install -e .

# Option B: With Desktop GUI (Recommended)
pip install -e ".[gui]"

# Option C: Everything (GUI + dev tools)
pip install -e ".[all]"
```

### 3. Configure API Key

```bash
cp .env.example .env
# Edit .env with your API key
```

Minimal config (DeepSeek):
```ini
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-xxx
```

### 4. Run

```bash
# Desktop GUI (Recommended, full experience)
python desktop/app.py
# Or on Windows: double-click desktop/run.bat

# One-shot task (coding mode)
gangge "Create a FastAPI project with user authentication"

# Interactive REPL
gangge
```

---

## 📖 Novel Writing Workflow

### Step 1: Create Book
1. Launch desktop GUI
2. Click "New Book" in left sidebar
3. Fill in title, genre, target chapters, words per chapter

### Step 2: Configure World
1. Switch to "Characters" tab, add main characters (name, role, personality, arc)
2. Switch to "Worldview" tab, add locations and factions
3. Or let AI auto-generate: "Set up a xianxia novel's characters and world for me"

### Step 3: Generate Outline
1. Switch to "Outline" tab
2. Click "Generate Outline"
3. Architect creates complete story arcs based on characters & world
4. Edit manually or ask AI: "Add a twist to the outline"

### Step 4: Expand Chapter Outlines
1. Click "Expand Chapter Outlines"
2. Each chapter gets detailed beat sheet (scenes, conflicts, emotional direction)

### Step 5: Start Writing
1. Switch to "Chapters" tab
2. Select chapter, click "Write Chapter" (enable fast mode for speed)
3. Writer drafts based on chapter outline and prior text
4. Auditor auto-checks quality, revises if needed

### Step 6: Chat-Based Adjustment (Always Available)
Type directly in the main chat window:
- "The protagonist's reaction in chapter 5 isn't strong enough, fix it"
- "Add a foreshadowing about the protagonist's origin"
- "Show me overall progress"
- "Export first 10 chapters"

### Advanced: Imitation Mode
1. Click "Import Novel" to upload TXT file
2. System auto-analyzes original style, characters, structure
3. Select "Imitation Mode" when creating new book
4. Writing automatically mimics target style

---

## 🎬 Demo

<p align="center">
  <img src="docs/screenshots/1.png" alt="Gangge Code Desktop GUI" width="90%" />
  <br/>
  <em>Desktop GUI — Novel writing panels + AI coding assistant</em>
</p>

```
📋 Task Analysis
Tech stack: FastAPI + SQLAlchemy + SQLite

✅ Task List (0/6)
1. [ ] Project structure — app/, routes/, models/
2. [ ] Data models     — models/user.py
3. [ ] Auth module     — routes/auth.py
...

▶ Executing step 1
  ▶ bash(mkdir -p app/routes app/models)
  ✓ write_file: wrote app/models/user.py (42 lines)
✅ 1/6 done
```

---

## 🛠️ Coding Assistant Features

### Agent Engine
- **30-round Plan & Execute loop** — Analyze → Call tools → Review → Continue
- **Plan on first round** — Module list + task steps + file structure
- **ask_user pause** — AI pauses to ask questions, resumes after your answer
- **Test verification** — Auto-runs pytest after file changes, auto-fixes failures
- **Context management** — Sliding window + tool result truncation + lazy file index
- **Progress emitter** — Auto-pushes progress after every step, real-time visibility

### 13+ Built-in Tools
`bash` · `read_file` · `write_file` · `edit_file` · `grep` · `glob` · `list_dir` · `web_fetch` · `ask_user` · `lint_check` · `find_symbol` · `find_references` · `create_tool`

### Symbol Navigation
- **`find_symbol`** — Locate classes, functions, methods by name with file path, line number, and type
- **`find_references`** — Find all references to a symbol across files
- **Repo Index** — AST-based multi-language symbol scanner (Python/JS/TS/JSX/TSX), incrementally cached to `.gangge/repo_index.json`

### Safety & Rollback
- **Shadow Git Checkpoint** — Auto-creates Git checkpoint before AI modifications, one-click rollback
- **LSP Syntax Check** — Auto-runs pyright/ruff after code changes, fixes errors immediately
- **Permission Control** — Rule engine + danger detection, system directory writes blocked
- **Critic Self-Check** — Built-in self-check in System Prompt (syntax / logic / deps / style)

### AI-Created Tools
- **`create_tool`** — AI builds new tools at runtime with 4-gate safety check (dangerous code → interface compliance → duplicate detection → dynamic load validation)
- **Plugin System** — Self-built tools auto-save to `.gangge/plugins/`, automatically restored on next startup

### Project Management
- **Memory Bank** — `.gangge/` directory stores project progress + decision log across sessions
- **Decision Log** — Records "why", not just "what", preventing AI from repeating mistakes
- **Session Persistence** — SQLite storage, resume any past session
- **File Change Diff** — Auto-generates unified diff for every modification

### Four Usage Modes

| Mode | Command | Best For |
|------|---------|----------|
| **One-shot** | `gangge "task"` | Quick tasks, CI/CD |
| **Pipe** | `cat error.log \| gangge "analyze"` | Log analysis, shell scripts |
| **REPL** | `gangge` | Multi-turn, ongoing development |
| **Desktop GUI** | `python desktop/app.py` | Novel writing + full project dev |

### Desktop GUI Features
- **VSCode-style 3-panel layout** — Left (sessions+files) | Center (chat+input) | Right (preview+tools)
- **Standalone file preview** — Click files to preview in right panel, keeps chat clean
- **Stop button** — Red stop button appears during execution, cancel anytime
- **Batch task queue** — Multi-line input, executes sequentially
- **Plan confirmation** — Review AI's plan before it executes
- **Diff rollback** — View diffs and roll back to pre-modification state
- **Internationalization** — Built-in Chinese/English language packs, auto-switches by system language

### Extensibility
- **`.ganggerules`** — Define coding standards, test requirements, architecture conventions per project
- **MCP Protocol** — Full MCP client manager supporting stdio and SSE transports
- **MCP Server** — Configure via `.gangge/mcp_servers.json`, auto-connect and register tools (AutoCAD, FreeCAD, databases, browsers...)
- **ComfyUI Integration** — Auto-detects local ComfyUI on startup, activates image generation tool when available

---

## 📐 Architecture

```
UI Layer (Layer 1)
  CLI gangge "task"   ──┐
  Pipe cat x | gangge ──┤
  REPL gangge          ─┤──► AgenticLoop (Core Engine, Layer 3)
  TUI terminal.py      ─┤       │
  GUI desktop/app.py   ─┘       ├─ Tool Layer (Layer 3 tools)
                                │    bash · file_ops · grep · glob · list_dir
                                │    web_fetch · ask_user · lint_check
                                │    find_symbol · find_references · create_tool
                                │    └── Plugin tools (.gangge/plugins/)
                                │    └── Novel Tools (16) ← Dramatica-Flow
                                │         novel_* series
                                ├─ MCP Tool Layer (Layer 4)
                                │    MCP Client (stdio/SSE) → External Servers
                                │    └── ComfyUI image generation
                                ├─ Session Layer (Layer 2)
                                │    Persistence · Memory Bank · Compression
                                ├─ Permission Layer (Layer 4)
                                │    Rules · Danger Detection · Shadow Git
                                └─ LLM Layer (Layer 5)
                                     DeepSeek · OpenAI · Claude · Ollama
```

---

## ⚙️ Configuration

**`.env` Environment Variables**

```ini
LLM_PROVIDER=deepseek          # deepseek / openai / anthropic / ollama
DEEPSEEK_API_KEY=sk-xxx
DEEPSEEK_MODEL=deepseek-chat

MAX_ROUNDS=30                  # Max tool-call rounds
MAX_TOKENS=8192
TEMPERATURE=0.0
```

**`.ganggerules` Project Rules** (place in project root)

```markdown
# Coding Standards
- All comments in English
- Every new function must have a pytest test
- Database operations only in repositories/ directory
```

---

## 📁 Project Structure

```
gangge-code/
├── desktop/
│   ├── app.py              # PyQt6 desktop application (with novel panels)
│   ├── run.bat             # Windows one-click launcher
│   └── run.ps1             # PowerShell launcher
├── src/gangge/
│   ├── cli.py              # CLI entry point
│   ├── cli_repl.py         # Interactive REPL
│   ├── pricing.py          # Token usage stats
│   ├── layer1_ui/          # TUI interface (Textual)
│   ├── layer2_session/     # Session management (SQLite + Memory Bank)
│   │   ├── manager.py      #   Session manager
│   │   ├── context.py      #   Context compression
│   │   ├── state.py        #   Session state
│   │   └── storage.py      #   SQLite persistence
│   ├── layer3_agent/       # Core engine
│   │   ├── loop.py         #   Agentic Loop
│   │   ├── planner.py      #   Plan & Execute planner
│   │   ├── progress_emitter.py  # Progress push
│   │   ├── prompts/
│   │   │   └── system.py   #   System Prompt
│   │   └── tools/
│   │       ├── base.py     #   Tool base class + ToolResult
│   │       ├── registry.py #   Tool registry (33+ tools unified)
│   │       ├── bash.py     #   Shell command execution
│   │       ├── file_ops.py #   File read/write/edit
│   │       ├── search.py   #   grep/glob/list_dir
│   │       ├── web.py      #   Web content fetch
│   │       ├── ask_user.py #   User questions
│   │       ├── lint_check.py   #   LSP syntax check
│   │       ├── symbol.py   #   Symbol lookup + reference tracking
│   │       ├── create_tool.py  #   AI self-built tools
│   │       ├── comfyui_tool.py # ComfyUI image generation
│   │       └── novel.py    #   ★ Novel toolset (16 tools)
│   ├── dramatica/          # ★ Dramatica-Flow novel engine
│   │   ├── pipeline.py     #   Five-layer writing pipeline
│   │   ├── agents/         #   Architect/Writer/Auditor/Reviser
│   │   ├── state/          #   StateManager world state management
│   │   ├── narrative/      #   Narrative modules (causal chains/hooks/emotional arcs)
│   │   └── validators/     #   Consistency validators
│   ├── layer4_permission/  # Permission & safety
│   │   ├── guard.py        #   Security guard
│   │   ├── rules.py        #   Rule engine
│   │   └── danger.py       #   Dangerous operation detection
│   ├── layer4_tools/       # Infrastructure tools
│   │   ├── shadow_git.py   #   Git checkpoint / rollback
│   │   ├── mcp_client.py   #   MCP client (stdio + SSE)
│   │   ├── repo_index.py   #   Repo symbol index
│   │   └── plugin_loader.py    # Plugin loader
│   └── layer5_llm/         # LLM adapters (4 providers)
│       ├── base.py         #   Base class + ToolDefinition
│       ├── registry.py     #   Provider registry
│       ├── deepseek.py     #   DeepSeek adapter
│       ├── anthropic.py    #   Claude adapter
│       └── openai_compat.py    # OpenAI / Ollama adapter
├── tests/
│   ├── test_core.py        # Core module tests
│   ├── test_architecture.py    # Architecture layer compliance tests
│   ├── test_mcp_integration.py # MCP integration tests
│   └── test_items.py       # Desktop GUI route tests
├── .env.example            # Environment variable template
├── pyproject.toml          # Project configuration
├── requirements.txt        # Dependency list
└── LICENSE                 # MIT license
```

---

## 🧪 Testing

```bash
# Run all tests
pytest tests/ -v

# Run core module tests
pytest tests/test_core.py -v

# Only AgenticLoop tests
pytest tests/test_core.py -v -k "test_loop"

# Architecture layer compliance tests
pytest tests/test_architecture.py -v

# MCP integration tests
pytest tests/test_mcp_integration.py -v

# With coverage
pytest tests/ --cov=src/gangge --cov-report=term-missing
```

---

## 🗺️ Roadmap

- [x] ★ Dramatica-Flow novel writing engine integrated
- [x] ★ 16 dedicated novel tools
- [x] ★ Chat-based natural language creation
- [x] ★ Import & imitation (large TXT file support)
- [x] ★ Narrative knowledge graph
- [x] ★ Five-layer writing pipeline
- [x] ★ Novel-specific UI panels (7 tabs)
- [x] CLI / REPL / TUI / PyQt6 Desktop
- [x] Memory Bank cross-session context + Decision Log
- [x] Shadow Git checkpoints (rollback at any step)
- [x] LSP syntax check (pyright/ruff/pylint)
- [x] ask_user pause for user input
- [x] VSCode-style desktop GUI (3-panel layout + file preview + stop button)
- [x] Symbol lookup + reference tracking (find_symbol / find_references)
- [x] AI self-built tools (create_tool + plugin system)
- [x] Full MCP protocol support (stdio + SSE transports)
- [x] ComfyUI image generation (auto-detect activation)
- [ ] Internationalization (i18n) English language pack
- [ ] Web UI (remote access)
- [ ] Vector index (RAG) for local context
- [ ] EPUB export format support
- [ ] Collaborative writing
- [ ] Cloud sync for novels

---

## 🤝 Contributing

PRs and Issues are welcome!

If you find this useful, please give it a ⭐ — it helps a lot.

---

## 📜 License

MIT © [ydsgangge-ux](https://github.com/ydsgangge-ux)
