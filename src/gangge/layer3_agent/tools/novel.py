"""
小说写作工具组 — 将 Dramatica-Flow 叙事引擎封装为 Gangge Code 工具。

工具列表：
  - novel_init       创建一本新书（自动初始化模板角色/世界/事件）
  - novel_setup      配置角色/势力/地点/世界规则
  - novel_outline    AI 生成三幕结构大纲（自动补全缺失的 setup）
  - novel_chapter_outlines  将序列展开为章纲
  - novel_write_chapter     执行五层写作管线写一章
  - novel_audit      审计指定章节
  - novel_revise     修订指定章节
  - novel_status     查看书籍当前状态
  - novel_export     导出全书为 Markdown
  - novel_list_books 列出所有书籍
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from gangge.layer3_agent.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

_DRAMATICA_AVAILABLE = False
try:
    from gangge.dramatica.types.narrative import (
        Character, CharacterNeed, CharacterWorldview, Obstacle, ObstacleType,
        StoryEvent, DramaticFunction,
    )
    from gangge.dramatica.types.state import (
        BookConfig, WorldState, TruthFileKey,
    )
    from gangge.dramatica.state import StateManager
    from gangge.dramatica.narrative import NarrativeEngine
    from gangge.dramatica.agents import (
        ArchitectAgent, WriterAgent, AuditorAgent, ReviserAgent, SummaryAgent,
    )
    from gangge.dramatica.pipeline import WritingPipeline
    from gangge.dramatica.validators import PostWriteValidator
    from gangge.dramatica.setup import SetupLoader
    _DRAMATICA_AVAILABLE = True
except ImportError as e:
    logger.debug("Dramatica-Flow 模块不可用: %s", e)


def _check_dramatica() -> str | None:
    if not _DRAMATICA_AVAILABLE:
        return "叙事引擎模块未加载，请检查 gangge.dramatica 包是否完整安装"
    return None


def _find_chapter_file(chapter_dir: Path, ch_num: int) -> Path | None:
    """查找章节文件，兼容多种命名格式。优先级：final > draft > chapter_N > 第N章"""
    if not chapter_dir.exists():
        return None
    candidates = [
        chapter_dir / f"ch{ch_num:04d}_final.md",
        chapter_dir / f"ch{ch_num:04d}_draft.md",
        chapter_dir / f"chapter_{ch_num:03d}.md",
        chapter_dir / f"chapter_{ch_num}.md",
    ]
    for p in candidates:
        if p.exists():
            return p
    import re
    for f in chapter_dir.glob("*.md"):
        m = re.match(r"第(\d+)章", f.stem)
        if m and int(m.group(1)) == ch_num:
            return f
    return None


def _get_books_dir(workspace: str) -> Path:
    return Path(workspace) / "books"


def _has_arcs(outline_path: Path) -> bool:
    if not outline_path.exists():
        return False
    try:
        data = json.loads(outline_path.read_text(encoding="utf-8"))
        arcs = data.get("arcs", [])
        return len(arcs) > 0
    except Exception:
        return False


def _ensure_default_arc(sm, title: str = ""):
    outline_path = sm.state_dir / "outline.json"
    outline_data = {}
    if outline_path.exists():
        try:
            outline_data = json.loads(outline_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    arcs = outline_data.get("arcs", [])
    if arcs:
        return

    arc_name = f"{title}·第一篇" if title else "第一篇"
    default_arc = {
        "name": arc_name,
        "order": 1,
        "goal": "",
        "summary": "",
        "sequences": [],
        "status": "pending",
    }
    arcs.append(default_arc)
    outline_data["arcs"] = arcs
    if "title" not in outline_data:
        outline_data["title"] = title or ""
        outline_data["logline"] = ""
        outline_data["total_goal"] = ""
    outline_path.parent.mkdir(parents=True, exist_ok=True)
    outline_path.write_text(
        json.dumps(outline_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _ensure_setup(workspace: str, book_id: str, genre: str = "", title: str = "") -> SetupLoader | None:
    """确保 setup_state.json 存在。如果不存在，自动用模板初始化。"""
    sm = StateManager(workspace, book_id)
    setup_state_path = sm.state_dir / "setup_state.json"
    if setup_state_path.exists():
        try:
            return SetupLoader.restore(workspace, book_id)
        except Exception:
            pass

    loader = SetupLoader(workspace, book_id)
    loader.init_templates()

    setup_dir = sm.book_dir / "setup"
    chars_path = setup_dir / "characters.json"
    if not chars_path.exists() or chars_path.stat().st_size < 10:
        default_chars = {
            "characters": [
                {
                    "id": "protagonist",
                    "name": "主角",
                    "need": {"external": "实现目标", "internal": "获得认可"},
                    "obstacles": [{"type": "antagonist", "description": "反派阻挠", "mechanism": "直接对抗"}],
                    "worldview": {"power": "seeks", "trust": "selective", "coping": "fight"},
                    "arc": "positive",
                    "profile": "勇敢坚韧，不轻言放弃",
                    "behavior_lock": ["不会背叛朋友", "不会伤害无辜"],
                    "personality": ["坚毅", "善良", "聪明"],
                    "backstory": "",
                    "current_goal": "",
                    "hidden_agenda": "",
                    "faction": "",
                    "is_main_cast": true,
                }
            ]
        }
        chars_path.write_text(json.dumps(default_chars, ensure_ascii=False, indent=2), encoding="utf-8")

    world_path = setup_dir / "world.json"
    if not world_path.exists() or world_path.stat().st_size < 10:
        default_world = {
            "locations": [
                {"id": "loc_start", "name": "起点", "description": "故事开始的地方", "connections": [], "dramatic_potential": "平凡中的不平凡"},
                {"id": "loc_conflict", "name": "冲突之地", "description": "矛盾激化的场所", "connections": ["loc_start"], "dramatic_potential": "危机与转折"},
            ],
            "factions": [
                {"id": "faction_ally", "name": "盟友势力", "description": "支持主角的力量", "relations": {}, "core_interest": "维护秩序"},
            ],
            "world_rules": [
                {"name": "核心规则", "description": f"{genre or '故事'}世界的根本法则", "consequence": "违反将遭受反噬", "is_hard": True},
            ],
        }
        world_path.write_text(json.dumps(default_world, ensure_ascii=False, indent=2), encoding="utf-8")

    events_path = setup_dir / "events.json"
    if not events_path.exists() or events_path.stat().st_size < 10:
        default_events = {
            "events": [
                {
                    "id": "seed_001",
                    "name": "激励事件",
                    "description": f"主角的日常生活被打破，命运开始转折",
                    "preconditions": [],
                    "effects": ["打破日常平衡", "迫使主角行动"],
                    "triggers": [],
                    "suggested_act": 1,
                    "suggested_function": "inciting",
                }
            ],
            "seed_event": "seed_001",
        }
        events_path.write_text(json.dumps(default_events, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        project_state = loader.load_all()
        return loader
    except Exception as e:
        logger.warning("自动 setup 失败: %s", e)
        return None


def _read_truth_bundle(workspace: str, book_id: str, keys: list) -> str:
    sm = StateManager(workspace, book_id)
    return sm.read_truth_bundle(keys)


class NovelInitTool(BaseTool):
    name = "novel_init"
    description = (
        "创建一本新小说，初始化书籍骨架。"
        "输出：仪表盘 Tab 显示新书信息。"
        "下一步：调用 novel_setup 配置角色和世界观。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "书名"},
            "genre": {"type": "string", "description": "题材（如：玄幻、都市、科幻、悬疑）"},
            "target_chapters": {"type": "integer", "description": "目标总章数", "default": 100},
            "words_per_chapter": {"type": "integer", "description": "每章目标字数", "default": 4000},
        },
        "required": ["title", "genre"],
    }

    def __init__(self, workspace: str = ""):
        self.workspace = workspace

    async def execute(self, **kwargs) -> ToolResult:
        err = _check_dramatica()
        if err:
            return ToolResult(output=err, is_error=True)

        title = kwargs.get("title", "")
        genre = kwargs.get("genre", "")
        target_chapters = kwargs.get("target_chapters", 100)
        words_per_chapter = kwargs.get("words_per_chapter", 4000)

        if not title:
            return ToolResult(output="请提供书名", is_error=True)

        from datetime import datetime, timezone
        book_id = f"book_{int(datetime.now(timezone.utc).timestamp())}"
        config = BookConfig(
            id=book_id,
            title=title,
            genre=genre,
            target_words_per_chapter=words_per_chapter,
            target_chapters=target_chapters,
            protagonist_id="",
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        sm = StateManager(self.workspace, book_id)
        sm.init(config)

        return ToolResult(
            output=(
                f"小说《{title}》已创建！\n"
                f"  书籍 ID：{book_id}\n"
                f"  题材：{genre}\n"
                f"  目标章数：{target_chapters}\n"
                f"  每章字数：{words_per_chapter}\n\n"
                f"📌 目前仅创建了书籍骨架，尚未消耗 Token。\n\n"
                f"下一步（任选其一）：\n"
                f"  1. 💬 在聊天窗口描述你的故事构思\n"
                f"     → 例如：「我想写一个程序员穿越到异世界用代码拯救世界的故事」\n"
                f"     → AI 会根据你的想法自动配置角色、世界观和大纲\n"
                f"  2. 🎨 在右侧面板手动配置角色和世界观\n"
                f"  3. 🚀 直接 novel_outline 生成大纲（将使用默认模板）"
            ),
            metadata={"book_id": book_id},
        )


class NovelSetupTool(BaseTool):
    name = "novel_setup"
    description = (
        "配置小说的世界观：角色、势力、地点、世界规则、种子事件。"
        "输出：角色 Tab + 世界观 Tab + 篇章 Tab（自动创建第一个篇章）。"
        "⚠️ 配置完成后必须停下来，等用户在篇章Tab中操作（生成大纲/展开章纲）。"
        "不要继续调用 novel_outline 或 novel_chapter_outlines。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "book_id": {"type": "string", "description": "书籍 ID"},
            "characters": {
                "type": "array",
                "description": "角色列表（JSON 数组）",
                "items": {"type": "object"},
            },
            "locations": {
                "type": "array",
                "description": "地点列表（JSON 数组）",
                "items": {"type": "object"},
            },
            "factions": {
                "type": "array",
                "description": "势力列表（JSON 数组）",
                "items": {"type": "object"},
            },
            "world_rules": {
                "type": "array",
                "description": "世界规则列表（JSON 数组）",
                "items": {"type": "object"},
            },
            "seed_events": {
                "type": "array",
                "description": "种子事件列表（JSON 数组）",
                "items": {"type": "object"},
            },
        },
        "required": ["book_id"],
    }

    def __init__(self, workspace: str = ""):
        self.workspace = workspace

    async def execute(self, **kwargs) -> ToolResult:
        err = _check_dramatica()
        if err:
            return ToolResult(output=err, is_error=True)

        book_id = kwargs.get("book_id", "")
        if not book_id:
            return ToolResult(output="请提供 book_id", is_error=True)

        sm = StateManager(self.workspace, book_id)
        config_data = sm.read_config()
        if not config_data:
            return ToolResult(output=f"找不到书籍 {book_id}，请先使用 novel_init 创建", is_error=True)

        setup_dir = sm.book_dir / "setup"
        setup_dir.mkdir(parents=True, exist_ok=True)

        chars_data = kwargs.get("characters", [])
        if chars_data:
            path = setup_dir / "characters.json"
            path.write_text(
                json.dumps({"characters": chars_data}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        world_data = {}
        if kwargs.get("locations"):
            world_data["locations"] = kwargs["locations"]
        if kwargs.get("factions"):
            world_data["factions"] = kwargs["factions"]
        if kwargs.get("world_rules"):
            world_data["world_rules"] = kwargs["world_rules"]
        if world_data:
            path = setup_dir / "world.json"
            path.write_text(
                json.dumps(world_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        events_data = kwargs.get("seed_events", [])
        if events_data:
            path = setup_dir / "events.json"
            path.write_text(
                json.dumps({"events": events_data, "seed_event": events_data[0].get("id", "")}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        try:
            loader = SetupLoader(self.workspace, book_id)
            project_state = loader.load_all()
            char_names = "、".join(c.name for c in project_state.characters.values())
            loc_count = len(project_state.locations)
            faction_count = len(project_state.factions)

            outline_path = sm.state_dir / "outline.json"
            if not outline_path.exists() or not _has_arcs(outline_path):
                title = config_data.get("title", "我的小说")
                _ensure_default_arc(sm, title)

            return ToolResult(
                output=(
                    f"世界观配置完成！\n"
                    f"  角色：{char_names}\n"
                    f"  地点：{loc_count} 个\n"
                    f"  势力：{faction_count} 个\n"
                    f"  世界规则：{len(project_state.world_rules)} 条\n"
                    f"  种子事件：{len(project_state.seed_events)} 个\n\n"
                    f"📌 已自动创建第一个篇章，请前往「篇章」Tab 查看。\n"
                    f"选择篇章后点击「生成大纲」为该篇章生成3幕结构大纲。"
                )
            )
        except FileNotFoundError as e:
            return ToolResult(
                output=(
                    f"配置文件不完整：{e}\n"
                    f"请提供 characters、locations、factions、seed_events 等配置数据。"
                ),
                is_error=True,
            )
        except Exception as e:
            return ToolResult(output=f"配置加载失败：{e}", is_error=True)


class NovelOutlineTool(BaseTool):
    name = "novel_outline"
    description = (
        "AI 基于 Dramatica 叙事理论自动生成三幕结构故事大纲。"
        "输出：大纲 Tab + 篇章 Tab（状态变为 outlined）。"
        "如果提供 arc_name，则为指定篇章生成大纲；否则为第一个 pending 篇章生成。"
        "⚠️ 生成大纲后停下来，等用户确认后再调用 novel_chapter_outlines。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "book_id": {"type": "string", "description": "书籍 ID"},
            "arc_name": {"type": "string", "description": "篇章名称（可选），为指定篇章生成大纲"},
        },
        "required": ["book_id"],
    }

    def __init__(self, workspace: str = "", llm=None):
        self.workspace = workspace
        self._llm = llm

    async def execute(self, **kwargs) -> ToolResult:
        err = _check_dramatica()
        if err:
            return ToolResult(output=err, is_error=True)

        book_id = kwargs.get("book_id", "")
        arc_name = kwargs.get("arc_name", "")
        if not book_id:
            return ToolResult(output="请提供 book_id", is_error=True)

        if not self._llm:
            return ToolResult(output="LLM 未初始化，无法生成大纲。请在设置中配置 API Key 和模型。", is_error=True)

        sm = StateManager(self.workspace, book_id)
        config_data = sm.read_config()
        if not config_data:
            return ToolResult(output=f"找不到书籍 {book_id}，请先使用 novel_init 创建", is_error=True)

        genre = config_data.get("genre", "")
        title = config_data.get("title", "")

        try:
            loader = SetupLoader.restore(self.workspace, book_id)
        except FileNotFoundError:
            loader = _ensure_setup(self.workspace, book_id, genre=genre, title=title)
            if not loader:
                return ToolResult(output="自动初始化失败，请手动使用 novel_setup 配置", is_error=True)
        except Exception as e:
            loader = _ensure_setup(self.workspace, book_id, genre=genre, title=title)
            if not loader:
                return ToolResult(output=f"加载配置失败：{e}", is_error=True)

        from gangge.layer3_agent.tools.dramatica_adapter import DramaticaLLMAdapter
        df_llm = DramaticaLLMAdapter(self._llm)
        engine = NarrativeEngine(df_llm)

        protagonist = next(
            (c for c in loader.characters.values() if c.id == loader.config.protagonist_id),
            next(iter(loader.characters.values())),
        )
        seed_event = loader.seed_events[0] if loader.seed_events else StoryEvent(
            id="seed_001", name="激励事件",
            description=f"{protagonist.name}的命运发生了转折",
            effects=["打破日常平衡"],
        )

        world_context = _read_truth_bundle(self.workspace, book_id, [
            TruthFileKey.STORY_BIBLE,
        ])

        outline = engine.generate_outline(
            seed_event=seed_event,
            protagonist=protagonist,
            world_context=world_context,
            target_chapters=loader.config.target_chapters,
            genre=loader.config.genre,
        )

        outline_path = sm.state_dir / "outline.json"
        outline_data = outline.model_dump() if hasattr(outline, 'model_dump') else {}

        if arc_name:
            outline_data_existing = {}
            if outline_path.exists():
                try:
                    outline_data_existing = json.loads(outline_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            arcs = outline_data_existing.get("arcs", [])
            found = False
            for arc in arcs:
                if arc.get("name") == arc_name:
                    arc["sequences"] = [s if isinstance(s, dict) else s.model_dump() for s in outline.sequences]
                    arc["status"] = "outlined"
                    found = True
                    break
            if not found:
                arcs.append({
                    "name": arc_name,
                    "order": len(arcs) + 1,
                    "sequences": [s if isinstance(s, dict) else s.model_dump() for s in outline.sequences],
                    "status": "outlined",
                })
            outline_data_existing["arcs"] = arcs
            if "title" not in outline_data_existing:
                outline_data_existing["title"] = outline.title
                outline_data_existing["logline"] = outline.logline
                outline_data_existing["total_goal"] = ""
            outline_path.write_text(
                json.dumps(outline_data_existing, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        else:
            outline_data_existing = {}
            if outline_path.exists():
                try:
                    outline_data_existing = json.loads(outline_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            arcs = outline_data_existing.get("arcs", [])
            if arcs:
                target_arc = None
                for arc in arcs:
                    if arc.get("status") in ("pending", ""):
                        target_arc = arc
                        break
                if not target_arc:
                    target_arc = arcs[0]
                target_arc["sequences"] = [s if isinstance(s, dict) else s.model_dump() for s in outline.sequences]
                target_arc["status"] = "outlined"
                arc_name = target_arc.get("name", "")
            else:
                arc_name = f"{title}·第一篇" if title else "第一篇"
                arcs.append({
                    "name": arc_name,
                    "order": 1,
                    "sequences": [s if isinstance(s, dict) else s.model_dump() for s in outline.sequences],
                    "status": "outlined",
                })
            outline_data_existing["arcs"] = arcs
            outline_data_existing["title"] = outline.title
            outline_data_existing["logline"] = outline.logline
            outline_data_existing["total_goal"] = ""
            outline_path.write_text(
                json.dumps(outline_data_existing, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        seq_summary = "\n".join(
            f"  {i+1}. 序列{s.number}（第{s.act}幕）：{s.summary}\n"
            f"     戏剧功能：{s.dramatic_function.value}，章数：{s.estimated_scenes}\n"
            f"     结尾钩子：{s.end_hook}"
            for i, s in enumerate(outline.sequences)
        )

        return ToolResult(
            output=(
                f"{'篇章「' + arc_name + '」的' if arc_name else '故事'}大纲已生成！\n"
                f"  书名：{outline.title}\n"
                f"  Logline：{outline.logline}\n"
                f"  序列数：{len(outline.sequences)}\n\n"
                f"序列概览：\n{seq_summary}\n\n"
                f"📌 请前往「大纲」Tab 查看大纲内容。确认合理后，在「篇章」Tab 点击「展开章纲」生成逐章章节大纲。"
            ),
            metadata={"book_id": book_id, "outline_id": outline.id, "arc_name": arc_name},
        )


class NovelChapterOutlinesTool(BaseTool):
    name = "novel_chapter_outlines"
    description = (
        "将故事大纲的序列展开为逐章章纲。"
        "输出：大纲 Tab（显示章节条目）+ 章节 Tab（显示章纲列表）。"
        "如果提供 arc_name，则只展开指定篇章的序列；否则展开第一个 outlined 篇章。"
        "⚠️ 展开章纲后停下来，等用户确认后再调用 novel_write_chapter。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "book_id": {"type": "string", "description": "书籍 ID"},
            "sequence_index": {
                "type": "integer",
                "description": "要展开的序列索引（从0开始），不填则展开全部",
            },
            "arc_name": {
                "type": "string",
                "description": "篇章名称（可选），只展开指定篇章的序列",
            },
        },
        "required": ["book_id"],
    }

    def __init__(self, workspace: str = "", llm=None):
        self.workspace = workspace
        self._llm = llm

    async def execute(self, **kwargs) -> ToolResult:
        err = _check_dramatica()
        if err:
            return ToolResult(output=err, is_error=True)

        book_id = kwargs.get("book_id", "")
        seq_idx = kwargs.get("sequence_index", None)
        arc_name = kwargs.get("arc_name", "")
        if not book_id:
            return ToolResult(output="请提供 book_id", is_error=True)

        if not self._llm:
            return ToolResult(output="LLM 未初始化", is_error=True)

        sm = StateManager(self.workspace, book_id)
        outline_path = sm.state_dir / "outline.json"
        if not outline_path.exists():
            return ToolResult(output="大纲不存在，请先使用 novel_outline 生成", is_error=True)

        try:
            loader = SetupLoader.restore(self.workspace, book_id)
        except FileNotFoundError:
            loader = _ensure_setup(self.workspace, book_id)
            if not loader:
                return ToolResult(output="配置未加载，请先使用 novel_setup", is_error=True)

        from gangge.layer3_agent.tools.dramatica_adapter import DramaticaLLMAdapter
        df_llm = DramaticaLLMAdapter(self._llm)
        engine = NarrativeEngine(df_llm)

        outline_data = json.loads(outline_path.read_text(encoding="utf-8"))
        from gangge.dramatica.narrative import StoryOutlineSchema, SequenceSchema

        if arc_name:
            arcs = outline_data.get("arcs", [])
            target_arc = None
            for arc in arcs:
                if arc.get("name") == arc_name:
                    target_arc = arc
                    break
            if not target_arc:
                return ToolResult(output=f"未找到篇章：{arc_name}", is_error=True)
            arc_seqs = target_arc.get("sequences", [])
            if not arc_seqs:
                return ToolResult(output=f"篇章「{arc_name}」尚无序列，请先生成大纲", is_error=True)
            sequences = []
            for s in arc_seqs:
                try:
                    sequences.append(SequenceSchema.model_validate(s))
                except Exception:
                    pass
        else:
            arcs = outline_data.get("arcs", [])
            if arcs:
                target_arc = None
                for arc in arcs:
                    if arc.get("status") == "outlined" and arc.get("sequences"):
                        target_arc = arc
                        break
                if not target_arc:
                    for arc in arcs:
                        if arc.get("sequences"):
                            target_arc = arc
                            break
                if target_arc:
                    arc_name = target_arc.get("name", "")
                    arc_seqs = target_arc.get("sequences", [])
                    sequences = []
                    for s in arc_seqs:
                        try:
                            sequences.append(SequenceSchema.model_validate(s))
                        except Exception:
                            pass
                else:
                    return ToolResult(output="所有篇章尚无序列，请先生成大纲", is_error=True)
            else:
                outline = StoryOutlineSchema.model_validate(outline_data)
                sequences = outline.sequences

        protagonist = next(
            (c for c in loader.characters.values() if c.id == loader.config.protagonist_id),
            next(iter(loader.characters.values())),
        )

        world_context = _read_truth_bundle(self.workspace, book_id, [
            TruthFileKey.CURRENT_STATE,
            TruthFileKey.STORY_BIBLE,
        ])

        if seq_idx is not None:
            if 0 <= seq_idx < len(sequences):
                sequences = [sequences[seq_idx]]
            else:
                return ToolResult(output=f"序列索引 {seq_idx} 超出范围（0-{len(sequences)-1}）", is_error=True)

        all_outlines = []
        chapter_start = 1
        for seq in sequences:
            chapter_outlines = engine.generate_chapter_outlines(
                sequence=seq,
                protagonist=protagonist,
                world_context=world_context,
                chapter_start=chapter_start,
                words_per_chapter=loader.config.target_words_per_chapter,
            )
            all_outlines.extend(chapter_outlines)
            chapter_start += len(chapter_outlines)

        existing_path = sm.state_dir / "chapter_outlines.json"
        existing = []
        if existing_path.exists():
            try:
                existing = json.loads(existing_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        existing.extend([co.model_dump() for co in all_outlines])
        existing_path.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        ch_summary = "\n".join(
            f"  第{co.chapter_number}章《{co.title}》：{co.summary[:50]}"
            for co in all_outlines
        )

        return ToolResult(
            output=(
                f"{'篇章「' + arc_name + '」的' if arc_name else ''}章纲已生成！共 {len(all_outlines)} 章\n\n"
                f"{ch_summary}\n\n"
                f"📌 请前往「章节」Tab 查看章纲。确认后使用 novel_write_chapter 逐章写作。"
            )
        )


class NovelNewArcTool(BaseTool):
    name = "novel_new_arc"
    description = (
        "为长篇小说创建新的篇章（Arc）。每个篇章是一个独立的3幕结构，"
        "服务于小说的总目标。总目标未完成时，可以不断追加新篇章。"
        "类似海贼王的东海篇、阿拉巴斯坦篇、空岛篇等。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "book_id": {"type": "string", "description": "书籍 ID"},
            "arc_name": {"type": "string", "description": "篇章名称，如「东海篇」"},
            "arc_summary": {"type": "string", "description": "篇章概要，描述这个篇章要讲什么故事"},
            "arc_goal": {"type": "string", "description": "这个篇章的小目标，服务于总目标"},
        },
        "required": ["book_id"],
    }

    def __init__(self, workspace: str = "", llm=None):
        self.workspace = workspace
        self._llm = llm

    async def execute(self, **kwargs) -> ToolResult:
        err = _check_dramatica()
        if err:
            return ToolResult(output=err, is_error=True)

        book_id = kwargs.get("book_id", "")
        arc_name = kwargs.get("arc_name", "")
        arc_summary = kwargs.get("arc_summary", "")
        arc_goal = kwargs.get("arc_goal", "")

        if not book_id:
            return ToolResult(output="请提供 book_id", is_error=True)

        if not self._llm:
            return ToolResult(output="LLM 未初始化", is_error=True)

        sm = StateManager(self.workspace, book_id)
        outline_path = sm.state_dir / "outline.json"
        if not outline_path.exists():
            return ToolResult(output="大纲不存在，请先使用 novel_outline 生成", is_error=True)

        try:
            from gangge.layer3_agent.tools.dramatica_adapter import DramaticaLLMAdapter
            df_llm = DramaticaLLMAdapter(self._llm)
            engine = NarrativeEngine(df_llm)
        except Exception as e:
            return ToolResult(output=f"引擎初始化失败: {e}", is_error=True)

        outline_data = json.loads(outline_path.read_text(encoding="utf-8"))
        arcs = outline_data.get("arcs", [])
        total_goal = outline_data.get("total_goal", "")

        if not arcs:
            existing_sequences = outline_data.get("sequences", [])
            if existing_sequences:
                arcs = [{
                    "id": "arc_1",
                    "name": arc_name or "第一篇",
                    "summary": arc_summary or "初始篇章",
                    "goal": arc_goal or total_goal or "完成故事启程",
                    "order": 1,
                    "sequences": [{
                        "number": s.get("number", ""),
                        "act": s.get("act", ""),
                        "summary": s.get("summary", ""),
                        "dramatic_function": s.get("dramatic_function", ""),
                        "end_hook": s.get("end_hook", ""),
                        "key_events": s.get("key_events", []),
                        "estimated_scenes": s.get("estimated_scenes", 3),
                    } for s in existing_sequences],
                }]
                if not total_goal:
                    outline_data["total_goal"] = arc_goal or arc_summary or "完成故事"

        arc_order = len(arcs) + 1
        arc_id = f"arc_{arc_order}"
        new_arc_summary = arc_summary or f"第{arc_order}个篇章"
        new_arc_goal = arc_goal or f"服务于总目标：{total_goal}"

        try:
            new_arc = engine.generate_arc(
                arc_name=arc_name or f"第{arc_order}篇",
                arc_summary=new_arc_summary,
                arc_goal=new_arc_goal,
                total_goal=total_goal,
                previous_arcs=[a.get("summary", "") for a in arcs],
                genre=outline_data.get("logline", ""),
            )
        except AttributeError:
            new_arc = {
                "id": arc_id,
                "name": arc_name or f"第{arc_order}篇",
                "summary": new_arc_summary,
                "goal": new_arc_goal,
                "order": arc_order,
                "sequences": [],
            }

        if isinstance(new_arc, dict):
            new_arc["id"] = arc_id
            new_arc["order"] = arc_order
        else:
            new_arc = {
                "id": arc_id,
                "name": arc_name or f"第{arc_order}篇",
                "summary": new_arc_summary,
                "goal": new_arc_goal,
                "order": arc_order,
                "sequences": [],
            }

        arcs.append(new_arc)
        outline_data["arcs"] = arcs
        outline_data.pop("sequences", None)

        outline_path.write_text(
            json.dumps(outline_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        return ToolResult(
            output=(
                f"✅ 新篇章已创建！\n"
                f"  篇章名称：{arc_name or f'第{arc_order}篇'}\n"
                f"  篇章概要：{new_arc_summary}\n"
                f"  篇章目标：{new_arc_goal}\n"
                f"  总目标：{total_goal}\n\n"
                f"📌 请前往「篇章」Tab 查看新篇章，选择后点击「生成大纲」为该篇章生成三幕结构大纲。"
            ),
            metadata={"book_id": book_id, "arc_id": arc_id},
        )


class NovelWriteChapterTool(BaseTool):
    name = "novel_write_chapter"
    description = (
        "使用五层写作管线写一章小说：建筑师规划→写手写作→写后验证→审计→修订闭环。"
        "输出：章节 Tab（显示新章节）+ 仪表盘（更新进度和字数）。"
        "写完后自动提取因果链、生成摘要、更新世界状态。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "book_id": {"type": "string", "description": "书籍 ID"},
            "chapter_number": {"type": "integer", "description": "要写的章节号"},
            "fast_mode": {"type": "boolean", "description": "快速模式：跳过建筑师规划和审计修订，直接写作（速度提升约50%）", "default": True},
        },
        "required": ["book_id", "chapter_number"],
    }

    def __init__(self, workspace: str = "", llm=None):
        self.workspace = workspace
        self._llm = llm

    async def execute(self, **kwargs) -> ToolResult:
        err = _check_dramatica()
        if err:
            return ToolResult(output=err, is_error=True)

        book_id = kwargs.get("book_id", "")
        chapter_number = kwargs.get("chapter_number", 0)
        if not book_id or not chapter_number:
            return ToolResult(output="请提供 book_id 和 chapter_number", is_error=True)

        if not self._llm:
            return ToolResult(output="LLM 未初始化", is_error=True)

        sm = StateManager(self.workspace, book_id)
        co_path = sm.state_dir / "chapter_outlines.json"
        if not co_path.exists():
            return ToolResult(output="章纲不存在，请先使用 novel_chapter_outlines 生成", is_error=True)

        try:
            loader = SetupLoader.restore(self.workspace, book_id)
        except FileNotFoundError:
            loader = _ensure_setup(self.workspace, book_id)
            if not loader:
                return ToolResult(output="配置未加载", is_error=True)

        from gangge.layer3_agent.tools.dramatica_adapter import DramaticaLLMAdapter
        df_llm = DramaticaLLMAdapter(self._llm)

        co_data = json.loads(co_path.read_text(encoding="utf-8"))
        from gangge.dramatica.narrative import ChapterOutlineSchema
        target_co = None
        for item in co_data:
            if item.get("chapter_number") == chapter_number:
                target_co = ChapterOutlineSchema.model_validate(item)
                break
        if not target_co:
            return ToolResult(
                output=f"找不到第 {chapter_number} 章的章纲，请先使用 novel_chapter_outlines 生成",
                is_error=True,
            )

        protagonist = next(
            (c for c in loader.characters.values() if c.id == loader.config.protagonist_id),
            next(iter(loader.characters.values())),
        )
        all_characters = list(loader.characters.values())

        pipeline = WritingPipeline(
            state_manager=sm,
            architect=ArchitectAgent(df_llm),
            writer=WriterAgent(df_llm, style_guide=loader.config.style_guide, genre=loader.config.genre),
            auditor=AuditorAgent(df_llm),
            reviser=ReviserAgent(df_llm),
            narrative_engine=NarrativeEngine(df_llm),
            summary_agent=SummaryAgent(df_llm),
            validator=PostWriteValidator(loader.config.custom_forbidden_words),
            protagonist=protagonist,
            all_characters=all_characters,
        )

        fast_mode = kwargs.get("fast_mode", True)

        result = pipeline.run(target_co, verbose=True, fast_mode=fast_mode)

        audit_status = "通过" if result.audit_report.passed else "未通过"
        dormancy = ""
        if result.dormancy_warnings:
            dormancy = "\n掉线预警：\n" + "\n".join(f"  - {w}" for w in result.dormancy_warnings)

        graph_info = ""
        if result.graph_indexed:
            graph_info = f"\n  图谱索引：已建立"
            if result.consistency_issues:
                critical = [i for i in result.consistency_issues if i["severity"] == "critical"]
                warnings = [i for i in result.consistency_issues if i["severity"] == "warning"]
                graph_info += f"（一致性检查：{len(critical)} 严重 / {len(warnings)} 警告）"
                for ci in critical[:3]:
                    graph_info += f"\n    ⚠ {ci['description']}"
            else:
                graph_info += "（一致性检查：通过）"
        else:
            graph_info = "\n  图谱索引：未启用"

        return ToolResult(
            output=(
                f"第 {result.chapter_number} 章写作完成！\n"
                f"  字数：{result.word_count}\n"
                f"  审计：{audit_status}\n"
                f"  修订轮数：{result.revision_rounds}\n"
                f"  因果链：{result.causal_links} 条\n"
                f"  线程：{result.thread_id or '主线'}{dormancy}{graph_info}\n\n"
                f"下一步：使用 novel_write_chapter 写下一章，或使用 novel_audit 审计。"
            ),
            metadata={
                "book_id": book_id,
                "chapter_number": result.chapter_number,
                "word_count": result.word_count,
                "audit_passed": result.audit_report.passed,
            },
        )


class NovelAuditTool(BaseTool):
    name = "novel_audit"
    description = "审计指定章节的叙事质量，检查 OOC、信息边界、因果一致性、情感弧线、伏笔管理等 12 个维度。"
    input_schema = {
        "type": "object",
        "properties": {
            "book_id": {"type": "string", "description": "书籍 ID"},
            "chapter_number": {"type": "integer", "description": "要审计的章节号"},
        },
        "required": ["book_id", "chapter_number"],
    }

    def __init__(self, workspace: str = "", llm=None):
        self.workspace = workspace
        self._llm = llm

    async def execute(self, **kwargs) -> ToolResult:
        err = _check_dramatica()
        if err:
            return ToolResult(output=err, is_error=True)

        book_id = kwargs.get("book_id", "")
        chapter_number = kwargs.get("chapter_number", 0)
        if not book_id or not chapter_number:
            return ToolResult(output="请提供 book_id 和 chapter_number", is_error=True)

        sm = StateManager(self.workspace, book_id)
        final_path = _find_chapter_file(sm.chapter_dir, chapter_number)
        if not final_path:
            return ToolResult(output=f"第 {chapter_number} 章不存在", is_error=True)

        content = final_path.read_text(encoding="utf-8")

        validator = PostWriteValidator()
        val_result = validator.validate(content, target_words=4000)

        issues_text = ""
        if val_result.issues:
            for issue in val_result.issues:
                icon = "[X]" if issue.severity == "error" else "[!]"
                issues_text += f"  {icon} [{issue.rule}] {issue.description}\n"
        else:
            issues_text = "  无问题"

        return ToolResult(
            output=(
                f"第 {chapter_number} 章审计结果\n"
                f"  字数：{val_result.word_count}\n"
                f"  通过：{'是' if val_result.passed else '否'}\n"
                f"  错误：{val_result.error_count}，警告：{val_result.warning_count}\n\n"
                f"问题列表：\n{issues_text}"
            )
        )


class NovelReviseTool(BaseTool):
    name = "novel_revise"
    description = "修订指定章节，根据审计问题进行 spot-fix 修订。"
    input_schema = {
        "type": "object",
        "properties": {
            "book_id": {"type": "string", "description": "书籍 ID"},
            "chapter_number": {"type": "integer", "description": "要修订的章节号"},
            "issues": {
                "type": "array",
                "description": "要修复的问题列表（字符串数组）",
                "items": {"type": "string"},
            },
        },
        "required": ["book_id", "chapter_number"],
    }

    def __init__(self, workspace: str = "", llm=None):
        self.workspace = workspace
        self._llm = llm

    async def execute(self, **kwargs) -> ToolResult:
        err = _check_dramatica()
        if err:
            return ToolResult(output=err, is_error=True)

        book_id = kwargs.get("book_id", "")
        chapter_number = kwargs.get("chapter_number", 0)
        issues_list = kwargs.get("issues", [])

        if not book_id or not chapter_number:
            return ToolResult(output="请提供 book_id 和 chapter_number", is_error=True)

        if not self._llm:
            return ToolResult(output="LLM 未初始化", is_error=True)

        sm = StateManager(self.workspace, book_id)
        final_path = _find_chapter_file(sm.chapter_dir, chapter_number)
        if not final_path:
            return ToolResult(output=f"第 {chapter_number} 章不存在", is_error=True)

        content = final_path.read_text(encoding="utf-8")

        from gangge.layer3_agent.tools.dramatica_adapter import DramaticaLLMAdapter
        from gangge.dramatica.agents import ReviserAgent, AuditIssue
        df_llm = DramaticaLLMAdapter(self._llm)
        reviser = ReviserAgent(df_llm)

        audit_issues = [
            AuditIssue(dimension="用户指定", severity="critical", description=desc)
            for desc in issues_list
        ]
        if not audit_issues:
            return ToolResult(output="没有指定要修复的问题", is_error=True)

        result = reviser.revise(content, audit_issues, mode="spot-fix")
        final_path.write_text(result.content, encoding="utf-8")

        return ToolResult(
            output=(
                f"第 {chapter_number} 章已修订！\n"
                f"  修复了 {len(audit_issues)} 个问题\n"
                f"  修订后字数：{len(result.content)}"
            )
        )


class NovelStatusTool(BaseTool):
    name = "novel_status"
    description = "查看小说的当前状态：进度、角色位置、情感状态、伏笔、因果链等。"
    input_schema = {
        "type": "object",
        "properties": {
            "book_id": {"type": "string", "description": "书籍 ID"},
            "detail": {
                "type": "string",
                "description": "查看详情：basic（基本）、hooks（伏笔）、causal（因果链）、emotions（情感）、all（全部）",
                "enum": ["basic", "hooks", "causal", "emotions", "all"],
                "default": "basic",
            },
        },
        "required": ["book_id"],
    }

    def __init__(self, workspace: str = ""):
        self.workspace = workspace

    async def execute(self, **kwargs) -> ToolResult:
        err = _check_dramatica()
        if err:
            return ToolResult(output=err, is_error=True)

        book_id = kwargs.get("book_id", "")
        detail = kwargs.get("detail", "basic")
        if not book_id:
            return ToolResult(output="请提供 book_id", is_error=True)

        sm = StateManager(self.workspace, book_id)
        config_data = sm.read_config()
        if not config_data:
            return ToolResult(output=f"找不到书籍 {book_id}", is_error=True)

        ws = sm.read_world_state()
        lines = [
            f"《{config_data.get('title', '')}》状态",
            f"  题材：{config_data.get('genre', '')}",
            f"  当前进度：第 {ws.current_chapter} 章 / 共 {config_data.get('target_chapters', '?')} 章",
            f"  状态：{config_data.get('status', 'planning')}",
        ]

        if detail in ("hooks", "all"):
            open_hooks = ws.open_hooks()
            overdue = ws.overdue_hooks(ws.current_chapter)
            lines.append(f"\n伏笔状态：")
            lines.append(f"  未闭合：{len(open_hooks)} 个")
            if overdue:
                lines.append(f"  超期未回收：{len(overdue)} 个")
                for h in overdue:
                    lines.append(f"    - {h.id}（{h.type.value}）：{h.description}")

        if detail in ("causal", "all"):
            lines.append(f"\n因果链：{len(ws.causal_chain)} 条")
            for cl in ws.causal_chain[-5:]:
                lines.append(f"  Ch.{cl.chapter}：{cl.cause} -> {cl.event} -> {cl.consequence}")

        if detail in ("emotions", "all"):
            lines.append(f"\n情感状态：")
            latest = {}
            for snap in ws.emotional_snapshots:
                if snap.character_id not in latest or snap.chapter > latest[snap.character_id].chapter:
                    latest[snap.character_id] = snap
            for cid, snap in latest.items():
                lines.append(f"  {cid}：{snap.emotion}（{snap.intensity}/10）")

        if detail in ("all",):
            lines.append(f"\n叙事线程：{len(ws.threads)} 条")
            for t in ws.threads:
                lines.append(f"  {t.name}（{t.id}）：{t.status}，权重 {t.weight}")

        return ToolResult(output="\n".join(lines))


class NovelImportTool(BaseTool):
    name = "novel_import"
    description = (
        "导入一本已有的 TXT 小说文件（支持几十万字大文件），自动分块采样分析提取文风、角色、叙事结构。"
        "分析结果保存为 style_guide 和参考素材，供仿写使用。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "book_id": {"type": "string", "description": "目标书籍 ID"},
            "file_path": {"type": "string", "description": "TXT 小说文件的完整路径"},
            "sample_chapters": {"type": "integer", "description": "每个采样区段取的章节数（默认3）", "default": 3},
            "extract_characters": {"type": "boolean", "description": "是否提取角色信息（默认 true）", "default": True},
            "deep_analysis": {"type": "boolean", "description": "是否进行深度结构分析（剧情弧线/转折点，默认 true）", "default": True},
        },
        "required": ["book_id", "file_path"],
    }

    def __init__(self, workspace: str = "", llm=None):
        self.workspace = workspace
        self.llm = llm

    async def execute(self, **kwargs) -> ToolResult:
        err = _check_dramatica()
        if err:
            return ToolResult(output=err, is_error=True)

        book_id = kwargs.get("book_id", "")
        file_path = kwargs.get("file_path", "")
        sample_ch = kwargs.get("sample_chapters", 3)
        extract_chars = kwargs.get("extract_characters", True)
        deep_analysis = kwargs.get("deep_analysis", True)

        if not book_id:
            return ToolResult(output="请提供 book_id", is_error=True)
        if not file_path:
            return ToolResult(output="请提供 TXT 文件路径", is_error=True)

        txt_path = Path(file_path)
        if not txt_path.exists():
            return ToolResult(output=f"文件不存在：{file_path}", is_error=True)
        if txt_path.suffix.lower() not in (".txt", ".md"):
            return ToolResult(output="仅支持 .txt 或 .md 文件", is_error=True)

        try:
            raw_text = txt_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                raw_text = txt_path.read_text(encoding="gbk")
            except Exception:
                return ToolResult(output="文件编码无法识别，请使用 UTF-8 或 GBK 编码", is_error=True)

        if not raw_text.strip():
            return ToolResult(output="文件内容为空", is_error=True)

        sm = StateManager(self.workspace, book_id)
        if not (sm.state_dir / "config.json").exists():
            return ToolResult(output=f"书籍 {book_id} 不存在，请先使用 novel_init 创建", is_error=True)

        chapters = self._split_chapters(raw_text)
        if not chapters:
            chapters = [("全文", raw_text)]

        total_ch = len(chapters)
        total_words = len(raw_text)

        ref_dir = sm.state_dir / "reference"
        ref_dir.mkdir(parents=True, exist_ok=True)

        chapter_index = []
        for i, (title, content) in enumerate(chapters, 1):
            chapter_index.append({"number": i, "title": title, "word_count": len(content)})
        (ref_dir / "chapter_index.json").write_text(
            json.dumps(chapter_index, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        samples = self._multi_region_sample(chapters, sample_ch)
        samples_meta = []
        for region, chs in samples.items():
            text = "\n\n".join(f"【{t}】\n{c}" for t, c in chs)
            path = ref_dir / f"sample_{region}.md"
            path.write_text(text, encoding="utf-8")
            samples_meta.append({"region": region, "chapters": [t for t, _ in chs], "chars": len(text)})

        style_guide = ""
        if self.llm:
            style_guide = await self._analyze_style_multiregion(samples, total_ch, total_words)
        else:
            all_sample = "\n\n".join(
                "\n\n".join(f"【{t}】\n{c}" for t, c in chs) for chs in samples.values()
            )
            style_guide = self._rule_based_style_analysis(all_sample)

        (ref_dir / "style_guide.md").write_text(style_guide, encoding="utf-8")

        config_data = sm._read_json("config.json")
        config_data["style_guide"] = style_guide
        config_data["reference_total_chapters"] = total_ch
        config_data["reference_total_words"] = total_words
        sm._write_json("config.json", config_data)

        for region, chs in samples.items():
            text = "\n\n".join(f"【{t}】\n{c}" for t, c in chs)
            (ref_dir / f"sample_{region}.md").write_text(text, encoding="utf-8")

        characters_extracted = []
        if extract_chars and self.llm:
            characters_extracted = await self._extract_characters_full(chapters)
            if characters_extracted:
                (ref_dir / "extracted_characters.json").write_text(
                    json.dumps(characters_extracted, ensure_ascii=False, indent=2), encoding="utf-8"
                )

        structure_info = {}
        if deep_analysis and self.llm:
            structure_info = await self._analyze_structure(chapters, samples)
            if structure_info:
                (ref_dir / "structure_analysis.json").write_text(
                    json.dumps(structure_info, ensure_ascii=False, indent=2), encoding="utf-8"
                )

        if total_words > 500000:
            chunk_size = 100000
            for i in range(0, len(raw_text), chunk_size):
                chunk = raw_text[i:i + chunk_size]
                chunk_idx = i // chunk_size
                (ref_dir / f"source_part_{chunk_idx:03d}.md").write_text(chunk, encoding="utf-8")
            (ref_dir / "source_meta.json").write_text(json.dumps({
                "total_words": total_words,
                "chunk_size": chunk_size,
                "chunks": (total_words + chunk_size - 1) // chunk_size,
                "split": True,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            (ref_dir / "source_text.md").write_text(raw_text, encoding="utf-8")
            (ref_dir / "source_meta.json").write_text(json.dumps({
                "total_words": total_words,
                "split": False,
            }, ensure_ascii=False, indent=2), encoding="utf-8")

        result_lines = [
            f"✅ 小说导入完成！",
            f"  文件：{txt_path.name}",
            f"  总章数：{total_ch}",
            f"  总字数：{total_words:,}",
            f"  采样区段：{', '.join(samples.keys())}",
            f"  提取角色：{len(characters_extracted)} 个",
            f"  结构分析：{'已完成' if structure_info else '跳过'}",
            f"  大文件分片：{'是（{0}片）'.format((total_words + 99999) // 100000) if total_words > 500000 else '否'}",
            f"",
            f"文风指南已保存，仿写时将自动应用。",
        ]
        return ToolResult(output="\n".join(result_lines), metadata={"book_id": book_id})

    def _split_chapters(self, text: str) -> list[tuple[str, str]]:
        import re
        patterns = [
            r'(?:^|\n)(第[零一二三四五六七八九十百千万\d]+章[^\n]*)',
            r'(?:^|\n)(Chapter\s+\d+[^\n]*)',
            r'(?:^|\n)(\d{1,4}[、.．][^\n]*)',
        ]
        for pat in patterns:
            splits = re.split(pat, text)
            if len(splits) > 3:
                chapters = []
                for i in range(1, len(splits) - 1, 2):
                    title = splits[i].strip()
                    content = splits[i + 1].strip() if i + 1 < len(splits) else ""
                    if content:
                        chapters.append((title, content))
                if chapters:
                    return chapters

        chunk_size = 4000
        chapters = []
        for i in range(0, len(text), chunk_size):
            chunk = text[i:i + chunk_size]
            chapters.append((f"段落 {i // chunk_size + 1}", chunk))
        return chapters

    def _multi_region_sample(
        self, chapters: list[tuple[str, str]], sample_ch: int
    ) -> dict[str, list[tuple[str, str]]]:
        total = len(chapters)
        samples: dict[str, list[tuple[str, str]]] = {}

        samples["beginning"] = chapters[:sample_ch]

        if total > sample_ch * 3:
            mid = total // 2
            samples["middle"] = chapters[max(0, mid - sample_ch // 2):mid + sample_ch // 2 + 1]

        if total > sample_ch * 5:
            q1 = total // 4
            samples["q1"] = chapters[max(0, q1 - sample_ch // 2):q1 + sample_ch // 2 + 1]
            q3 = total * 3 // 4
            samples["q3"] = chapters[max(0, q3 - sample_ch // 2):q3 + sample_ch // 2 + 1]

        if total > sample_ch * 2:
            samples["ending"] = chapters[-sample_ch:]

        return samples

    async def _analyze_style_multiregion(
        self, samples: dict[str, list[tuple[str, str]]], total_ch: int, total_words: int
    ) -> str:
        from gangge.layer3_agent.tools.dramatica_adapter import DramaticaLLMAdapter
        df_llm = DramaticaLLMAdapter(self.llm)

        region_texts = {}
        for region, chs in samples.items():
            text = "\n\n".join(f"【{t}】\n{c}" for t, c in chs)
            if len(text) > 8000:
                text = text[:8000] + "\n\n...(截断)"
            region_texts[region] = text

        first_region = list(samples.keys())[0]
        first_text = region_texts[first_region]

        prompt = f"""请分析以下小说片段的写作风格，提取出一份详细的「文风指南」。
这本小说共 {total_ch} 章、约 {total_words:,} 字。以下是{first_region}部分：

{first_text}

请从以下7个维度分析：
1. 语言风格：句式长短、修辞偏好、叙述视角
2. 对话风格：对话密度、对话格式、口头禅/语气词
3. 描写偏好：场景描写/心理描写/动作描写的比例和手法
4. 节奏特征：场景切换频率、张弛节奏
5. 用词习惯：高频词汇、禁忌词汇、特定表达
6. 叙事技巧：倒叙/插叙使用、伏笔手法、悬念设置
7. 情感表达：含蓄/直白、情感浓度

输出格式：
## 语言风格
...
## 对话风格
...
## 描写偏好
...
## 节奏特征
...
## 用词习惯
...
## 叙事技巧
...
## 情感表达
...
## 仿写要点（最重要的3条规则）
1. ...
2. ...
3. ..."""

        try:
            base_guide = df_llm.complete([
                {"role": "system", "content": "你是一位专业的文学风格分析师，擅长从文本中提取写作特征。"},
                {"role": "user", "content": prompt},
            ])
            if not isinstance(base_guide, str):
                base_guide = str(base_guide)
        except Exception:
            all_text = "\n\n".join(region_texts.values())
            return self._rule_based_style_analysis(all_text)

        if len(samples) <= 1:
            return base_guide

        supplement_parts = []
        for region in list(samples.keys())[1:]:
            region_text = region_texts[region]
            region_label = {"middle": "中段", "q1": "前1/4", "q3": "后3/4", "ending": "结尾"}.get(region, region)

            sup_prompt = f"""基于已有的文风指南，请分析这段{region_label}的文本，补充或修正文风指南中可能遗漏的特征。
特别关注：风格是否有变化？节奏是否不同？情感浓度是否转变？

{region_text}

请输出补充要点（如果与已有指南一致则输出"无显著差异"）："""

            try:
                sup = df_llm.complete([
                    {"role": "system", "content": "你是文学风格分析师，负责补充和修正文风指南。"},
                    {"role": "user", "content": sup_prompt},
                ])
                if isinstance(sup, str) and "无显著差异" not in sup:
                    supplement_parts.append(f"### {region_label}补充\n{sup}")
            except Exception:
                pass

        if supplement_parts:
            return base_guide + "\n\n## 跨区段风格变化\n\n" + "\n\n".join(supplement_parts)
        return base_guide

    def _rule_based_style_analysis(self, sample_text: str) -> str:
        import re
        total_chars = len(sample_text)
        dialogues = re.findall(r'[「"「](.+?)[」"」]', sample_text)
        dialog_chars = sum(len(d) for d in dialogues)
        dialog_ratio = dialog_chars / max(total_chars, 1)

        sentences = re.split(r'[。！？…]', sample_text)
        sentences = [s for s in sentences if s.strip()]
        avg_sent_len = sum(len(s) for s in sentences) / max(len(sentences), 1)

        style_parts = [
            "## 语言风格",
            f"- 句式：{'短句为主，节奏明快' if avg_sent_len < 20 else '长短句结合，节奏舒缓' if avg_sent_len < 40 else '长句为主，描写细腻'}",
            f"- 平均句长：{avg_sent_len:.1f}字",
            "",
            "## 对话风格",
            f"- 对话密度：{'高（对话驱动型）' if dialog_ratio > 0.4 else '中等' if dialog_ratio > 0.2 else '低（叙述驱动型）'}",
            f"- 对话占比：{dialog_ratio:.1%}",
            "",
            "## 描写偏好",
            "- 基于统计推断，具体偏好需更多样本",
            "",
            "## 节奏特征",
            "- 基于文本结构推断",
            "",
            "## 仿写要点",
            f"1. 保持{'对话密集、节奏明快' if dialog_ratio > 0.3 else '叙述为主、描写细腻'}的风格",
            f"2. 句式长度控制在 {avg_sent_len:.0f} 字左右",
            f"3. 对话占比约 {dialog_ratio:.0%}",
        ]
        return "\n".join(style_parts)

    async def _extract_characters_full(self, chapters: list[tuple[str, str]]) -> list[dict]:
        from gangge.layer3_agent.tools.dramatica_adapter import DramaticaLLMAdapter
        df_llm = DramaticaLLMAdapter(self.llm)

        total = len(chapters)
        scan_points = [0]
        if total > 10:
            scan_points.append(total // 3)
        if total > 20:
            scan_points.append(total // 2)
        if total > 30:
            scan_points.append(2 * total // 3)
        if total > 5:
            scan_points.append(total - 3)

        all_characters: dict[str, dict] = {}

        for idx in scan_points:
            start = max(0, idx)
            end = min(total, idx + 3)
            chunk_text = "\n\n".join(f"【{chapters[i][0]}】\n{chapters[i][1][:3000]}" for i in range(start, end))
            if len(chunk_text) > 10000:
                chunk_text = chunk_text[:10000]

            prompt = f"""从以下小说片段中提取角色信息，每个角色包含：
- name: 姓名
- role: 角色类型（protagonist/antagonist/mentor/ally/trickster）
- personality: 性格描述（一句话）
- goal: 主要目标（一句话）
- speech_style: 说话风格（一句话）

只提取有明确出场和对话的角色，最多8个。输出 JSON 数组格式。

小说片段：
{chunk_text}"""

            try:
                from gangge.dramatica.llm import parse_llm_json
                resp = df_llm.complete([
                    {"role": "system", "content": "你是角色分析专家，从文本中提取角色信息。只输出 JSON 数组。"},
                    {"role": "user", "content": prompt},
                ])
                result = parse_llm_json(resp)
                if isinstance(result, list):
                    for char in result:
                        if isinstance(char, dict) and char.get("name"):
                            name = char["name"]
                            if name in all_characters:
                                existing = all_characters[name]
                                if char.get("personality") and not existing.get("personality"):
                                    existing["personality"] = char["personality"]
                                if char.get("goal") and not existing.get("goal"):
                                    existing["goal"] = char["goal"]
                                if char.get("speech_style") and not existing.get("speech_style"):
                                    existing["speech_style"] = char["speech_style"]
                            else:
                                all_characters[name] = char
            except Exception:
                pass

        return list(all_characters.values())

    async def _analyze_structure(
        self, chapters: list[tuple[str, str]], samples: dict[str, list[tuple[str, str]]]
    ) -> dict:
        from gangge.layer3_agent.tools.dramatica_adapter import DramaticaLLMAdapter
        df_llm = DramaticaLLMAdapter(self.llm)

        total = len(chapters)
        beginning_text = "\n\n".join(f"【{t}】\n{c[:1500]}" for t, c in chapters[:5])
        ending_text = "\n\n".join(f"【{t}】\n{c[:1500]}" for t, c in chapters[-3:])

        if len(beginning_text) > 8000:
            beginning_text = beginning_text[:8000]
        if len(ending_text) > 6000:
            ending_text = ending_text[:6000]

        prompt = f"""分析这本小说（共{total}章）的叙事结构，提取以下信息：

1. 整体结构：三幕式/四幕式/多线交织/其他
2. 主要转折点：发生在第几章，什么事件
3. 节奏模式：哪些章节是高潮，哪些是过渡
4. 伏笔模式：伏笔通常埋设后多少章回收
5. 情感弧线：整体情感走向

开头部分：
{beginning_text}

结尾部分：
{ending_text}

输出 JSON 格式：
{{
  "structure_type": "三幕式/四幕式/多线交织",
  "turning_points": [{{"chapter": 数字, "event": "描述"}}],
  "pacing_pattern": "描述节奏模式",
  "hook_cycle": "伏笔回收周期描述",
  "emotional_arc": "整体情感走向描述"
}}"""

        try:
            from gangge.dramatica.llm import parse_llm_json
            resp = df_llm.complete([
                {"role": "system", "content": "你是叙事结构分析师。只输出 JSON。"},
                {"role": "user", "content": prompt},
            ])
            result = parse_llm_json(resp)
            if isinstance(result, dict):
                return result
            return {}
        except Exception:
            return {}


class NovelImitateWriteTool(BaseTool):
    name = "novel_imitate_write"
    description = (
        "仿写模式：基于导入参考小说的文风，写新章节。"
        "需要先使用 novel_import 导入参考小说。"
        "会自动加载 style_guide 并根据当前章节进度选取对应位置的参考片段作为风格锚点。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "book_id": {"type": "string", "description": "书籍 ID"},
            "chapter_number": {"type": "integer", "description": "要写的章节号"},
            "fast_mode": {"type": "boolean", "description": "快速模式（默认 true）", "default": True},
            "reference_style_boost": {
                "type": "boolean",
                "description": "是否强化风格仿写（注入对应进度的参考原文片段）",
                "default": True,
            },
        },
        "required": ["book_id", "chapter_number"],
    }

    def __init__(self, workspace: str = "", llm=None):
        self.workspace = workspace
        self.llm = llm

    async def execute(self, **kwargs) -> ToolResult:
        err = _check_dramatica()
        if err:
            return ToolResult(output=err, is_error=True)

        book_id = kwargs.get("book_id", "")
        ch = kwargs.get("chapter_number", 0)
        fast_mode = kwargs.get("fast_mode", True)
        style_boost = kwargs.get("reference_style_boost", True)

        if not book_id or not ch:
            return ToolResult(output="请提供 book_id 和 chapter_number", is_error=True)

        sm = StateManager(self.workspace, book_id)
        config_data = sm._read_json("config.json")
        style_guide = config_data.get("style_guide", "")
        ref_total_ch = config_data.get("reference_total_chapters", 0)

        ref_dir = sm.state_dir / "reference"
        if not ref_dir.exists():
            return ToolResult(
                output="尚未导入参考小说。请先使用 novel_import 导入一本 TXT 小说作为风格参考。",
                is_error=True,
            )

        if not style_guide.strip():
            sg_path = ref_dir / "style_guide.md"
            if sg_path.exists():
                style_guide = sg_path.read_text(encoding="utf-8")
                config_data["style_guide"] = style_guide
                sm._write_json("config.json", config_data)

        style_anchor = ""
        if style_boost:
            style_anchor = self._get_dynamic_anchor(ref_dir, ch, ref_total_ch)

        enhanced_style = style_guide + style_anchor

        from gangge.layer3_agent.tools.dramatica_adapter import DramaticaLLMAdapter
        df_llm = DramaticaLLMAdapter(self.llm)

        loader = SetupLoader(self.workspace, book_id)
        all_characters = list(loader.characters.values())
        protagonist = next((c for c in all_characters if c.role == "protagonist"), all_characters[0] if all_characters else None)

        if not protagonist:
            return ToolResult(output="未找到主角，请先配置角色", is_error=True)

        writer = WriterAgent(df_llm, style_guide=enhanced_style, genre=loader.config.genre)

        pipeline = WritingPipeline(
            architect=ArchitectAgent(df_llm, genre=loader.config.genre),
            writer=writer,
            auditor=AuditorAgent(df_llm),
            reviser=ReviserAgent(df_llm),
            summarizer=SummaryAgent(df_llm),
            workspace=self.workspace,
            book_id=book_id,
        )
        pipeline.all_characters = all_characters

        ne = NarrativeEngine(self.workspace, book_id)
        co_list = ne.load_chapter_outlines()
        target_co = next((co for co in co_list if co.chapter_number == ch), None)
        if not target_co:
            return ToolResult(output=f"第 {ch} 章的章纲不存在，请先生成章纲", is_error=True)

        result = pipeline.run(target_co, verbose=True, fast_mode=fast_mode)

        output_lines = [
            f"✅ 仿写完成：第 {ch} 章",
            f"  字数：{result.word_count}",
            f"  审计：{'通过' if result.audit_report.passed else '未通过'}",
            f"  修订轮次：{result.revision_rounds}",
            f"  风格指南：{'已加载' if enhanced_style else '未加载'}",
            f"  风格锚点：{'已注入（动态定位）' if style_anchor else '未注入'}",
        ]
        return ToolResult(output="\n".join(output_lines), metadata={"book_id": book_id})

    def _get_dynamic_anchor(self, ref_dir: Path, current_ch: int, ref_total_ch: int) -> str:
        anchor_text = ""

        if ref_total_ch > 0:
            progress = min(current_ch / max(ref_total_ch, 1), 1.0)
            region_map = [
                (0.15, "beginning"),
                (0.35, "q1"),
                (0.55, "middle"),
                (0.75, "q3"),
                (1.01, "ending"),
            ]
            target_region = "beginning"
            for threshold, region in region_map:
                if progress < threshold:
                    target_region = region
                    break

            sample_path = ref_dir / f"sample_{target_region}.md"
            if sample_path.exists():
                sample = sample_path.read_text(encoding="utf-8")
                anchor_text = sample[:2000]
                return f"\n\n## 参考原文风格锚点（取自{target_region}区段，进度{progress:.0%}）\n{anchor_text}"

        source_meta_path = ref_dir / "source_meta.json"
        if source_meta_path.exists():
            meta = json.loads(source_meta_path.read_text(encoding="utf-8"))
            if meta.get("split"):
                chunk_idx = min(
                    int(progress * meta.get("chunks", 1)) if ref_total_ch > 0 else 0,
                    meta.get("chunks", 1) - 1,
                )
                part_path = ref_dir / f"source_part_{chunk_idx:03d}.md"
                if part_path.exists():
                    part_text = part_path.read_text(encoding="utf-8")
                    anchor_text = part_text[:2000]
                    return f"\n\n## 参考原文风格锚点（取自原文分片 {chunk_idx}）\n{anchor_text}"
            else:
                source_path = ref_dir / "source_text.md"
                if source_path.exists():
                    source = source_path.read_text(encoding="utf-8")
                    offset = int(progress * len(source)) if ref_total_ch > 0 else 0
                    anchor_text = source[offset:offset + 2000]
                    return f"\n\n## 参考原文风格锚点（进度{progress:.0%}）\n{anchor_text}"

        source_path = ref_dir / "source_text.md"
        if source_path.exists():
            source = source_path.read_text(encoding="utf-8")
            anchor_text = source[:2000]
            return f"\n\n## 参考原文风格锚点\n{anchor_text}"

        return ""


class NovelNavigateTool(BaseTool):
    name = "novel_navigate"
    description = (
        "快速定位和读取小说项目文件。可以查看章节内容、大纲、角色配置、世界状态等。"
        "target 参数指定要查看的内容：config/setup/outline/chapter_outlines/world_state/"
        "chapter_N（第N章正文）/summaries/hooks/current_state/characters/locations/factions/list_chapters"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "book_id": {"type": "string", "description": "书籍 ID"},
            "target": {
                "type": "string",
                "description": (
                    "要查看的内容目标："
                    "config=书籍配置, setup=角色/势力/地点, outline=大纲, "
                    "chapter_outlines=章纲, world_state=世界状态, "
                    "chapter_N=第N章正文(如chapter_3), "
                    "summaries=章节摘要, hooks=伏笔, current_state=当前状态, "
                    "characters=角色列表, locations=地点列表, factions=势力列表, "
                    "list_chapters=列出所有章节"
                ),
            },
            "max_length": {"type": "integer", "description": "返回内容最大字符数（默认3000）", "default": 3000},
        },
        "required": ["book_id", "target"],
    }

    def __init__(self, workspace: str = ""):
        self.workspace = workspace

    async def execute(self, **kwargs) -> ToolResult:
        err = _check_dramatica()
        if err:
            return ToolResult(output=err, is_error=True)

        book_id = kwargs.get("book_id", "")
        target = kwargs.get("target", "").strip().lower()
        max_len = kwargs.get("max_length", 3000)

        if not book_id:
            return ToolResult(output="请提供 book_id", is_error=True)
        if not target:
            return ToolResult(output="请提供 target 参数", is_error=True)

        sm = StateManager(self.workspace, book_id)
        if not (sm.state_dir / "config.json").exists():
            return ToolResult(output=f"书籍 {book_id} 不存在", is_error=True)

        import re
        ch_match = re.match(r"chapter[_\s]*(\d+)", target)

        file_map = {
            "config": sm.state_dir / "config.json",
            "setup": sm.state_dir / "setup_state.json",
            "outline": sm.state_dir / "outline.json",
            "chapter_outlines": sm.state_dir / "chapter_outlines.json",
            "world_state": sm.state_dir / "world_state.json",
            "summaries": sm.state_dir / "truth" / "chapter_summaries.md",
            "hooks": sm.state_dir / "truth" / "pending_hooks.md",
            "current_state": sm.state_dir / "truth" / "current_state.md",
        }

        if ch_match:
            ch_num = int(ch_match.group(1))
            ch_dir = Path(self.workspace) / "books" / book_id / "chapters"
            ch_path = _find_chapter_file(ch_dir, ch_num)
            if not ch_path:
                return ToolResult(output=f"第 {ch_num} 章文件不存在", is_error=True)
            try:
                content = ch_path.read_text(encoding="utf-8")
                return ToolResult(output=f"## 第 {ch_num} 章正文\n\n{content[:max_len]}")
            except Exception as e:
                return ToolResult(output=f"读取失败: {e}", is_error=True)

        if target == "characters":
            setup_path = sm.state_dir / "setup_state.json"
            if not setup_path.exists():
                return ToolResult(output="角色配置尚未创建")
            try:
                setup = json.loads(setup_path.read_text(encoding="utf-8"))
                chars = setup.get("characters", [])
                if not chars:
                    return ToolResult(output="暂无角色")
                lines = []
                for c in chars:
                    line = f"- {c.get('name', '?')}（{c.get('role', '?')}）：{c.get('personality', '')}"
                    if c.get('arc'):
                        line += f" | 弧线: {c['arc']}"
                    lines.append(line)
                return ToolResult(output="\n".join(lines)[:max_len])
            except Exception as e:
                return ToolResult(output=f"读取失败: {e}", is_error=True)

        if target == "locations":
            setup_path = sm.state_dir / "setup_state.json"
            if not setup_path.exists():
                return ToolResult(output="地点配置尚未创建")
            try:
                setup = json.loads(setup_path.read_text(encoding="utf-8"))
                locs = setup.get("locations", [])
                if not locs:
                    return ToolResult(output="暂无地点")
                lines = [f"- {l.get('name', '?')}：{l.get('description', '')}" for l in locs]
                return ToolResult(output="\n".join(lines)[:max_len])
            except Exception as e:
                return ToolResult(output=f"读取失败: {e}", is_error=True)

        if target == "factions":
            setup_path = sm.state_dir / "setup_state.json"
            if not setup_path.exists():
                return ToolResult(output="势力配置尚未创建")
            try:
                setup = json.loads(setup_path.read_text(encoding="utf-8"))
                factions = setup.get("factions", [])
                if not factions:
                    return ToolResult(output="暂无势力")
                lines = [f"- {f.get('name', '?')}：{f.get('goal', '')}" for f in factions]
                return ToolResult(output="\n".join(lines)[:max_len])
            except Exception as e:
                return ToolResult(output=f"读取失败: {e}", is_error=True)

        if target == "list_chapters":
            ch_dir = Path(self.workspace) / "books" / book_id / "chapters"
            if not ch_dir.exists():
                return ToolResult(output="暂无章节文件")
            chapters = sorted(ch_dir.glob("*.md"))
            if not chapters:
                return ToolResult(output="暂无章节文件")
            lines = []
            for ch_path in chapters:
                try:
                    content = ch_path.read_text(encoding="utf-8")
                    word_count = len(content)
                    first_line = content.split("\n")[0][:60] if content else ""
                    lines.append(f"- {ch_path.name}（{word_count}字）：{first_line}")
                except Exception:
                    lines.append(f"- {ch_path.name}（读取失败）")
            return ToolResult(output="\n".join(lines)[:max_len])

        if target in file_map:
            fpath = file_map[target]
            if not fpath.exists():
                return ToolResult(output=f"{target} 文件尚未创建")
            try:
                content = fpath.read_text(encoding="utf-8")
                if fpath.suffix == ".json":
                    try:
                        data = json.loads(content)
                        content = json.dumps(data, ensure_ascii=False, indent=2)
                    except Exception:
                        pass
                return ToolResult(output=f"## {target}\n\n{content[:max_len]}")
            except Exception as e:
                return ToolResult(output=f"读取失败: {e}", is_error=True)

        valid_targets = list(file_map.keys()) + ["characters", "locations", "factions", "list_chapters", "chapter_N"]
        return ToolResult(
            output=f"未知目标 '{target}'。可用目标: {', '.join(valid_targets)}",
            is_error=True,
        )


class NovelChatTool(BaseTool):
    name = "novel_chat"
    description = (
        "聊天式小说创作助手：用自然语言描述你的想法，系统自动识别意图并执行对应操作。"
        "支持：修改大纲、增加人物、调整剧情、查看状态、写章节等。"
        "会自动加载当前小说的完整上下文（角色、大纲、世界状态）供 LLM 理解。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "book_id": {"type": "string", "description": "书籍 ID"},
            "message": {"type": "string", "description": "用户的自然语言输入"},
        },
        "required": ["book_id", "message"],
    }

    def __init__(self, workspace: str = "", llm=None):
        self.workspace = workspace
        self.llm = llm

    async def execute(self, **kwargs) -> ToolResult:
        err = _check_dramatica()
        if err:
            return ToolResult(output=err, is_error=True)

        book_id = kwargs.get("book_id", "")
        message = kwargs.get("message", "")

        if not book_id:
            return ToolResult(output="请提供 book_id", is_error=True)
        if not message:
            return ToolResult(output="请输入你的想法", is_error=True)

        sm = StateManager(self.workspace, book_id)
        if not (sm.state_dir / "config.json").exists():
            return ToolResult(output=f"书籍 {book_id} 不存在", is_error=True)

        config_data = sm._read_json("config.json")
        title = config_data.get("title", "")
        genre = config_data.get("genre", "")
        current_ch = 0

        ws_data = {}
        ws_path = sm.state_dir / "world_state.json"
        if ws_path.exists():
            try:
                ws_data = json.loads(ws_path.read_text(encoding="utf-8"))
                current_ch = ws_data.get("current_chapter", 0)
            except Exception:
                pass

        outline_text = ""
        outline_path = sm.state_dir / "outline.json"
        if outline_path.exists():
            try:
                outline_data = json.loads(outline_path.read_text(encoding="utf-8"))
                outline_text = json.dumps(outline_data, ensure_ascii=False, indent=2)[:3000]
            except Exception:
                pass

        characters_text = ""
        setup_path = sm.state_dir / "setup_state.json"
        if setup_path.exists():
            try:
                setup_data = json.loads(setup_path.read_text(encoding="utf-8"))
                chars = setup_data.get("characters", [])
                if chars:
                    characters_text = "\n".join(
                        f"- {c.get('name', '?')}（{c.get('role', '?')}）：{c.get('personality', '')}"
                        for c in chars[:15]
                    )
            except Exception:
                pass

        hooks_text = ""
        try:
            hooks_text = sm.read_truth(TruthFileKey.PENDING_HOOKS)[:1500]
        except Exception:
            pass

        summaries_text = ""
        try:
            full_summaries = sm.read_truth(TruthFileKey.CHAPTER_SUMMARIES)
            if full_summaries:
                lines = full_summaries.strip().split("\n")
                summaries_text = "\n".join(lines[-30:])[:2000]
        except Exception:
            pass

        world_text = ""
        try:
            world_text = sm.read_truth(TruthFileKey.CURRENT_STATE)[:1500]
        except Exception:
            pass

        context_block = f"""## 当前小说状态
- 书名：《{title}》
- 题材：{genre}
- 当前进度：第 {current_ch} 章
- 目标章数：{config_data.get('target_chapters', '?')}

## 角色列表
{characters_text or '（尚未配置角色）'}

## 大纲概要
{outline_text or '（尚未生成大纲）'}

## 当前世界状态
{world_text or '（尚未更新）'}

## 未闭合伏笔
{hooks_text or '（无）'}

## 近期章节摘要
{summaries_text or '（无）'}"""

        system_prompt = f"""你是一位专业的小说创作助手，正在帮助作者创作《{title}》。
你可以通过调用以下工具来执行操作：

- novel_edit: 修改小说元素（角色/关系/伏笔/大纲/地点/势力/章节）
- novel_setup: 配置角色/势力/地点/世界规则
- novel_outline: 生成或重新生成大纲
- novel_chapter_outlines: 展开章纲
- novel_write_chapter: 写一章
- novel_audit: 审计章节
- novel_revise: 修订章节
- novel_status: 查看详细状态
- novel_imitate_write: 仿写模式写一章

{context_block}

请根据用户的自然语言输入，理解其意图，然后调用合适的工具。
如果用户只是聊天或提问，直接回答即可，不需要调用工具。
如果用户要求修改，请确认修改内容后再调用工具。
回复使用中文。"""

        if not self.llm:
            return ToolResult(
                output="聊天模式需要 LLM 支持，请先配置 API Key。",
                is_error=True,
            )

        available_tools = self._get_tool_schemas()

        try:
            from gangge.layer5_llm.base import Message, Role
            messages = [
                Message(role=Role.SYSTEM, content=system_prompt),
                Message(role=Role.USER, content=message),
            ]
            resp = await self.llm.chat(messages, tools=available_tools)

            if resp.tool_calls:
                results = []
                tool_result_msg = Message(role=Role.TOOL)
                for tc in resp.tool_calls:
                    tool_result = await self._execute_tool_call(tc.name, tc.input, book_id)
                    results.append(f"🔧 执行: {tc.name}({json.dumps(tc.input, ensure_ascii=False)})\n📋 结果: {tool_result}")
                    tool_result_msg.add_tool_result(tc.id, tool_result)

                follow_up_msgs = [
                    Message(role=Role.SYSTEM, content=system_prompt),
                    Message(role=Role.USER, content=message),
                ]
                assistant_msg = Message(role=Role.ASSISTANT)
                assistant_msg.content = resp.content
                follow_up_msgs.append(assistant_msg)
                follow_up_msgs.append(tool_result_msg)

                follow_up = await self.llm.chat(follow_up_msgs, tools=available_tools)

                final_text = follow_up.get_text() or "\n\n".join(results)
                return ToolResult(output=final_text)
            else:
                return ToolResult(output=resp.get_text() or "（无回复）")

        except Exception as e:
            return ToolResult(output=f"聊天处理出错: {e}", is_error=True)

    def _get_tool_schemas(self) -> list:
        from gangge.layer5_llm.base import ToolDefinition
        return [
            ToolDefinition(
                name="novel_edit",
                description="修改小说元素：角色/关系/伏笔/大纲/地点/势力/章节",
                input_schema={
                    "type": "object",
                    "properties": {
                        "book_id": {"type": "string"},
                        "target_type": {"type": "string", "enum": ["character", "relationship", "hook", "outline", "location", "faction", "chapter"]},
                        "action": {"type": "string", "enum": ["add", "modify", "remove"]},
                        "data": {"type": "object"},
                    },
                    "required": ["book_id", "target_type", "action", "data"],
                },
            ),
            ToolDefinition(
                name="novel_setup",
                description="配置角色/势力/地点/世界规则",
                input_schema={
                    "type": "object",
                    "properties": {
                        "book_id": {"type": "string"},
                        "characters": {"type": "array", "items": {"type": "object"}},
                        "locations": {"type": "array", "items": {"type": "object"}},
                        "factions": {"type": "array", "items": {"type": "object"}},
                    },
                },
            ),
            ToolDefinition(
                name="novel_outline",
                description="生成或重新生成小说大纲",
                input_schema={
                    "type": "object",
                    "properties": {
                        "book_id": {"type": "string"},
                    },
                    "required": ["book_id"],
                },
            ),
            ToolDefinition(
                name="novel_chapter_outlines",
                description="将大纲展开为每章的详细节拍",
                input_schema={
                    "type": "object",
                    "properties": {
                        "book_id": {"type": "string"},
                    },
                    "required": ["book_id"],
                },
            ),
            ToolDefinition(
                name="novel_write_chapter",
                description="写一章小说",
                input_schema={
                    "type": "object",
                    "properties": {
                        "book_id": {"type": "string"},
                        "chapter_number": {"type": "integer"},
                        "fast_mode": {"type": "boolean", "default": True},
                    },
                    "required": ["book_id", "chapter_number"],
                },
            ),
            ToolDefinition(
                name="novel_status",
                description="查看小说详细状态",
                input_schema={
                    "type": "object",
                    "properties": {
                        "book_id": {"type": "string"},
                        "detail": {"type": "string", "default": "all"},
                    },
                },
            ),
            ToolDefinition(
                name="novel_audit",
                description="审计指定章节",
                input_schema={
                    "type": "object",
                    "properties": {
                        "book_id": {"type": "string"},
                        "chapter_number": {"type": "integer"},
                    },
                    "required": ["book_id", "chapter_number"],
                },
            ),
        ]

    async def _execute_tool_call(self, tool_name: str, args: dict, book_id: str) -> str:
        if tool_name == "novel_chat":
            return "聊天模式不支持嵌套调用"
        from gangge.layer3_agent.tools.registry import create_tool_registry
        if "book_id" not in args:
            args["book_id"] = book_id

        registry = create_tool_registry(workspace=self.workspace, llm=self.llm)
        tool = next((t for t in registry._tools.values() if t.name == tool_name), None)
        if not tool:
            return f"工具 {tool_name} 未找到"

        try:
            result = await tool.execute(**args)
            return result.output if hasattr(result, 'output') else str(result)
        except Exception as e:
            return f"执行出错: {e}"


class NovelEditTool(BaseTool):
    name = "novel_edit"
    description = (
        "编辑小说的各种元素：修改角色属性、调整关系强度、标记伏笔回收、"
        "修改大纲/章纲、编辑世界观、重写指定章节。支持精细化的手动编辑操作。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "book_id": {"type": "string", "description": "书籍 ID"},
            "edit_type": {
                "type": "string",
                "enum": [
                    "update_character", "delete_character",
                    "update_relationship", "delete_relationship",
                    "resolve_hook", "update_hook_range",
                    "update_outline", "update_chapter_outline",
                    "update_location", "delete_location",
                    "update_faction", "delete_faction",
                    "update_arc", "delete_arc",
                    "rewrite_chapter",
                ],
                "description": "编辑操作类型",
            },
            "data": {
                "type": "object",
                "description": "编辑数据，结构取决于 edit_type",
            },
        },
        "required": ["book_id", "edit_type", "data"],
    }

    def __init__(self, workspace: str = ""):
        self.workspace = workspace

    async def execute(self, **kwargs) -> ToolResult:
        err = _check_dramatica()
        if err:
            return ToolResult(output=err, is_error=True)

        book_id = kwargs.get("book_id", "")
        edit_type = kwargs.get("edit_type", "")
        data = kwargs.get("data", {})
        if not book_id or not edit_type:
            return ToolResult(output="请提供 book_id 和 edit_type", is_error=True)

        sm = StateManager(self.workspace, book_id)

        try:
            if edit_type == "update_character":
                return self._update_character(sm, data)
            elif edit_type == "delete_character":
                return self._delete_character(sm, data)
            elif edit_type == "update_relationship":
                return self._update_relationship(sm, data)
            elif edit_type == "delete_relationship":
                return self._delete_relationship(sm, data)
            elif edit_type == "resolve_hook":
                return self._resolve_hook(sm, data)
            elif edit_type == "update_hook_range":
                return self._update_hook_range(sm, data)
            elif edit_type == "update_outline":
                return self._update_outline(sm, data)
            elif edit_type == "update_chapter_outline":
                return self._update_chapter_outline(sm, data)
            elif edit_type == "update_location":
                return self._update_location(sm, data)
            elif edit_type == "delete_location":
                return self._delete_location(sm, data)
            elif edit_type == "update_faction":
                return self._update_faction(sm, data)
            elif edit_type == "delete_faction":
                return self._delete_faction(sm, data)
            elif edit_type == "update_arc":
                return self._update_arc(sm, data)
            elif edit_type == "delete_arc":
                return self._delete_arc(sm, data)
            elif edit_type == "rewrite_chapter":
                return self._rewrite_chapter(sm, data)
            else:
                return ToolResult(output=f"未知编辑类型：{edit_type}", is_error=True)
        except Exception as e:
            return ToolResult(output=f"编辑失败：{e}", is_error=True)

    def _update_character(self, sm: StateManager, data: dict) -> ToolResult:
        char_id = data.get("character_id", "")
        if not char_id:
            return ToolResult(output="请提供 character_id", is_error=True)

        setup_path = sm.state_dir / "setup_state.json"
        if not setup_path.exists():
            return ToolResult(output="配置文件不存在", is_error=True)

        setup = json.loads(setup_path.read_text(encoding="utf-8"))
        chars = setup.get("characters", {})
        if char_id not in chars:
            return ToolResult(output=f"角色 {char_id} 不存在", is_error=True)

        char = chars[char_id]
        updated_fields = []
        for key in ("name", "arc", "profile", "backstory", "personality", "behavior_lock", "faction"):
            if key in data:
                char[key] = data[key]
                updated_fields.append(key)

        if "is_main_cast" in data:
            char["is_main_cast"] = bool(data["is_main_cast"])
            updated_fields.append("is_main_cast")

        need = char.get("need", {})
        if not isinstance(need, dict):
            need = {"external": "", "internal": ""}
        if "need_external" in data:
            need["external"] = data["need_external"]
            updated_fields.append("need_external")
        if "need_internal" in data:
            need["internal"] = data["need_internal"]
            updated_fields.append("need_internal")
        char["need"] = need

        setup_path.write_text(json.dumps(setup, ensure_ascii=False, indent=2), encoding="utf-8")
        return ToolResult(output=f"角色 {char_id} 已更新：{', '.join(updated_fields)}")

    def _delete_character(self, sm: StateManager, data: dict) -> ToolResult:
        char_id = data.get("character_id", "")
        if not char_id:
            return ToolResult(output="请提供 character_id", is_error=True)

        setup_path = sm.state_dir / "setup_state.json"
        if not setup_path.exists():
            return ToolResult(output="配置文件不存在", is_error=True)

        setup = json.loads(setup_path.read_text(encoding="utf-8"))
        chars = setup.get("characters", {})
        if char_id not in chars:
            return ToolResult(output=f"角色 {char_id} 不存在", is_error=True)

        del chars[char_id]
        setup_path.write_text(json.dumps(setup, ensure_ascii=False, indent=2), encoding="utf-8")
        return ToolResult(output=f"角色 {char_id} 已删除")

    def _update_relationship(self, sm: StateManager, data: dict) -> ToolResult:
        char_a = data.get("character_a", "")
        char_b = data.get("character_b", "")
        delta = data.get("delta", 0)
        reason = data.get("reason", "手动调整")
        chapter = data.get("chapter", 0)

        if not char_a or not char_b:
            return ToolResult(output="请提供 character_a 和 character_b", is_error=True)

        ws = sm.read_world_state()
        ch = chapter or ws.current_chapter
        sm.update_relationship(char_a, char_b, delta, ch, reason)
        return ToolResult(output=f"关系已更新：{char_a}-{char_b} delta={delta:+d}（{reason}）")

    def _delete_relationship(self, sm: StateManager, data: dict) -> ToolResult:
        char_a = data.get("character_a", "")
        char_b = data.get("character_b", "")
        if not char_a or not char_b:
            return ToolResult(output="请提供 character_a 和 character_b", is_error=True)

        ws = sm.read_world_state()
        key = ":".join(sorted([char_a, char_b]))
        before = len(ws.relationships)
        ws.relationships = [r for r in ws.relationships if r.key != key]
        sm.write_world_state(ws)
        removed = before - len(ws.relationships)
        return ToolResult(output=f"已删除 {removed} 条关系记录：{char_a}-{char_b}")

    def _resolve_hook(self, sm: StateManager, data: dict) -> ToolResult:
        hook_id = data.get("hook_id", "")
        chapter = data.get("chapter", 0)
        if not hook_id:
            return ToolResult(output="请提供 hook_id", is_error=True)

        ws = sm.read_world_state()
        ch = chapter or ws.current_chapter
        sm.resolve_hook(hook_id, ch)
        return ToolResult(output=f"伏笔 {hook_id} 已在第 {ch} 章回收")

    def _update_hook_range(self, sm: StateManager, data: dict) -> ToolResult:
        hook_id = data.get("hook_id", "")
        new_range = data.get("expected_range", [])
        if not hook_id or len(new_range) != 2:
            return ToolResult(output="请提供 hook_id 和 expected_range [earliest, latest]", is_error=True)

        ws = sm.read_world_state()
        hook = next((h for h in ws.pending_hooks if h.id == hook_id), None)
        if not hook:
            return ToolResult(output=f"伏笔 {hook_id} 不存在", is_error=True)

        hook.expected_resolution_range = tuple(new_range)
        sm.write_world_state(ws)
        return ToolResult(output=f"伏笔 {hook_id} 回收范围已更新为第 {new_range[0]}-{new_range[1]} 章")

    def _update_outline(self, sm: StateManager, data: dict) -> ToolResult:
        outline_path = sm.state_dir / "outline.json"
        if not outline_path.exists():
            return ToolResult(output="大纲不存在，请先使用 novel_outline 生成", is_error=True)

        outline = json.loads(outline_path.read_text(encoding="utf-8"))
        sequence_idx = data.get("sequence_index")
        if sequence_idx is None:
            return ToolResult(output="请提供 sequence_index", is_error=True)

        sequences = outline if isinstance(outline, list) else outline.get("sequences", outline.get("acts", []))
        if sequence_idx >= len(sequences):
            return ToolResult(output=f"序列索引 {sequence_idx} 超出范围", is_error=True)

        seq = sequences[sequence_idx]
        for key in ("title", "summary", "dramatic_function"):
            if key in data:
                seq[key] = data[key]

        outline_path.write_text(json.dumps(outline, ensure_ascii=False, indent=2), encoding="utf-8")
        return ToolResult(output=f"大纲序列 {sequence_idx} 已更新")

    def _update_chapter_outline(self, sm: StateManager, data: dict) -> ToolResult:
        co_path = sm.state_dir / "chapter_outlines.json"
        if not co_path.exists():
            return ToolResult(output="章纲不存在，请先使用 novel_chapter_outlines 生成", is_error=True)

        co_data = json.loads(co_path.read_text(encoding="utf-8"))
        ch_num = data.get("chapter_number", 0)
        if not ch_num:
            return ToolResult(output="请提供 chapter_number", is_error=True)

        target = None
        for item in co_data:
            if item.get("chapter_number") == ch_num:
                target = item
                break
        if not target:
            return ToolResult(output=f"第 {ch_num} 章章纲不存在", is_error=True)

        for key in ("title", "summary", "pov", "thread_id", "writing_notes"):
            if key in data:
                target[key] = data[key]
        if "target_words" in data:
            target["target_words"] = int(data["target_words"])
        if "beats" in data:
            target["beats"] = data["beats"]

        co_path.write_text(json.dumps(co_data, ensure_ascii=False, indent=2), encoding="utf-8")
        return ToolResult(output=f"第 {ch_num} 章章纲已更新")

    def _update_location(self, sm: StateManager, data: dict) -> ToolResult:
        loc_id = data.get("location_id", "")
        if not loc_id:
            return ToolResult(output="请提供 location_id", is_error=True)

        setup_path = sm.state_dir / "setup_state.json"
        if not setup_path.exists():
            return ToolResult(output="配置文件不存在", is_error=True)

        setup = json.loads(setup_path.read_text(encoding="utf-8"))
        world = setup.get("world", {})
        locations = world.get("locations", [])

        target = None
        for loc in locations:
            if loc.get("id") == loc_id:
                target = loc
                break
        if not target:
            return ToolResult(output=f"地点 {loc_id} 不存在", is_error=True)

        for key in ("name", "description", "dramatic_potential"):
            if key in data:
                target[key] = data[key]
        if "connections" in data:
            target["connections"] = data["connections"]

        setup_path.write_text(json.dumps(setup, ensure_ascii=False, indent=2), encoding="utf-8")
        return ToolResult(output=f"地点 {loc_id} 已更新")

    def _delete_location(self, sm: StateManager, data: dict) -> ToolResult:
        loc_id = data.get("location_id", "")
        if not loc_id:
            return ToolResult(output="请提供 location_id", is_error=True)

        setup_path = sm.state_dir / "setup_state.json"
        if not setup_path.exists():
            return ToolResult(output="配置文件不存在", is_error=True)

        setup = json.loads(setup_path.read_text(encoding="utf-8"))
        world = setup.get("world", {})
        locations = world.get("locations", [])
        before = len(locations)
        world["locations"] = [l for l in locations if l.get("id") != loc_id]
        setup_path.write_text(json.dumps(setup, ensure_ascii=False, indent=2), encoding="utf-8")
        removed = before - len(world["locations"])
        return ToolResult(output=f"已删除 {removed} 个地点：{loc_id}")

    def _update_faction(self, sm: StateManager, data: dict) -> ToolResult:
        faction_id = data.get("faction_id", "")
        if not faction_id:
            return ToolResult(output="请提供 faction_id", is_error=True)

        setup_path = sm.state_dir / "setup_state.json"
        if not setup_path.exists():
            return ToolResult(output="配置文件不存在", is_error=True)

        setup = json.loads(setup_path.read_text(encoding="utf-8"))
        world = setup.get("world", {})
        factions = world.get("factions", [])

        target = None
        for f in factions:
            if f.get("id") == faction_id:
                target = f
                break
        if not target:
            return ToolResult(output=f"势力 {faction_id} 不存在", is_error=True)

        for key in ("name", "description", "core_interest"):
            if key in data:
                target[key] = data[key]
        if "relations" in data:
            target["relations"] = data["relations"]

        setup_path.write_text(json.dumps(setup, ensure_ascii=False, indent=2), encoding="utf-8")
        return ToolResult(output=f"势力 {faction_id} 已更新")

    def _delete_faction(self, sm: StateManager, data: dict) -> ToolResult:
        faction_id = data.get("faction_id", "")
        if not faction_id:
            return ToolResult(output="请提供 faction_id", is_error=True)

        setup_path = sm.state_dir / "setup_state.json"
        if not setup_path.exists():
            return ToolResult(output="配置文件不存在", is_error=True)

        setup = json.loads(setup_path.read_text(encoding="utf-8"))
        world = setup.get("world", {})
        factions = world.get("factions", [])
        before = len(factions)
        world["factions"] = [f for f in factions if f.get("id") != faction_id]
        setup_path.write_text(json.dumps(setup, ensure_ascii=False, indent=2), encoding="utf-8")
        removed = before - len(world["factions"])
        return ToolResult(output=f"已删除 {removed} 个势力：{faction_id}")

    def _update_arc(self, sm: StateManager, data: dict) -> ToolResult:
        arc_name = data.get("arc_name", "")
        new_name = data.get("new_name", "")
        if not arc_name:
            return ToolResult(output="请提供 arc_name（要编辑的篇章名）", is_error=True)

        outline_path = sm.state_dir / "outline_state.json"
        if not outline_path.exists():
            return ToolResult(output="大纲文件不存在", is_error=True)

        outline = json.loads(outline_path.read_text(encoding="utf-8"))
        arcs = outline.get("arcs", [])
        updated = False
        for arc in arcs:
            if arc.get("name") == arc_name:
                if new_name and new_name != arc_name:
                    arc["name"] = new_name
                if "arc_goal" in data:
                    arc["goal"] = data["arc_goal"]
                if "arc_summary" in data:
                    arc["summary"] = data["arc_summary"]
                updated = True
                break

        if not updated:
            return ToolResult(output=f"未找到篇章：{arc_name}", is_error=True)

        outline_path.write_text(json.dumps(outline, ensure_ascii=False, indent=2), encoding="utf-8")
        return ToolResult(output=f"篇章「{arc_name}」已更新")

    def _delete_arc(self, sm: StateManager, data: dict) -> ToolResult:
        arc_name = data.get("arc_name", "")
        if not arc_name:
            return ToolResult(output="请提供 arc_name", is_error=True)

        outline_path = sm.state_dir / "outline_state.json"
        if not outline_path.exists():
            return ToolResult(output="大纲文件不存在", is_error=True)

        outline = json.loads(outline_path.read_text(encoding="utf-8"))
        arcs = outline.get("arcs", [])
        before = len(arcs)
        outline["arcs"] = [a for a in arcs if a.get("name") != arc_name]
        removed = before - len(outline["arcs"])
        if removed == 0:
            return ToolResult(output=f"未找到篇章：{arc_name}", is_error=True)

        outline_path.write_text(json.dumps(outline, ensure_ascii=False, indent=2), encoding="utf-8")
        return ToolResult(output=f"已删除篇章「{arc_name}」")

    def _rewrite_chapter(self, sm: StateManager, data: dict) -> ToolResult:
        ch_num = data.get("chapter_number", 0)
        new_content = data.get("content", "")
        if not ch_num:
            return ToolResult(output="请提供 chapter_number", is_error=True)
        if not new_content:
            return ToolResult(output="请提供 content（新章节内容）", is_error=True)

        sm.save_final(ch_num, new_content)
        return ToolResult(output=f"第 {ch_num} 章已重写（{len(new_content)} 字）")


class NovelGraphQueryTool(BaseTool):
    name = "novel_graph_query"
    description = (
        "查询叙事知识图谱：角色档案、因果链追踪、关系网络、伏笔状态、叙事线程。"
        "基于 CodeGraph 架构思想构建的叙事知识图谱，支持 BFS/DFS 遍历和一致性检查。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "book_id": {"type": "string", "description": "书籍 ID"},
            "query_type": {
                "type": "string",
                "enum": ["character_profile", "causal_chain", "relationship_network", "open_hooks", "thread_overview", "search", "graph_summary"],
                "description": "查询类型",
            },
            "character_id": {"type": "string", "description": "角色 ID（character_profile/relationship_network 需要）"},
            "event_id": {"type": "string", "description": "事件 ID（causal_chain 需要）"},
            "search_query": {"type": "string", "description": "搜索关键词（search 需要）"},
            "max_depth": {"type": "integer", "description": "遍历深度（默认 3）", "default": 3},
        },
        "required": ["book_id", "query_type"],
    }

    def __init__(self, workspace: str = ""):
        self.workspace = workspace

    async def execute(self, **kwargs) -> ToolResult:
        err = _check_dramatica()
        if err:
            return ToolResult(output=err, is_error=True)

        book_id = kwargs.get("book_id", "")
        query_type = kwargs.get("query_type", "")
        if not book_id or not query_type:
            return ToolResult(output="请提供 book_id 和 query_type", is_error=True)

        try:
            from gangge.dramatica.narrative_graph import (
                NarrativeGraphDB, NarrativeQueries, NarrativeTraversal,
            )
        except ImportError:
            return ToolResult(output="叙事图谱模块未加载", is_error=True)

        db_path = _get_books_dir(self.workspace) / book_id / "state" / "narrative_graph.db"
        if not db_path.exists():
            return ToolResult(output="图谱数据库不存在，请先写章节以自动建立图谱", is_error=True)

        db = NarrativeGraphDB(db_path)
        queries = NarrativeQueries(db)
        traversal = NarrativeTraversal(db)

        try:
            if query_type == "character_profile":
                char_id = kwargs.get("character_id", "")
                if not char_id:
                    chars = db.get_nodes_by_kind("character")
                    lines = ["角色列表：\n"]
                    for c in chars:
                        lines.append(f"  {c['id']}: {c['name']}")
                    return ToolResult(output="\n".join(lines))
                profile = queries.get_character_profile(char_id)
                if "error" in profile:
                    return ToolResult(output=profile["error"], is_error=True)
                lines = [
                    f"角色档案：{profile['name']}",
                    f"描述：{profile['description']}",
                    f"\n关系（{len(profile['relationships'])}）：",
                ]
                for r in profile["relationships"]:
                    lines.append(f"  {r['with']}：{r['type']}（强度 {r['strength']:+d}）Ch.{r['chapter']}")
                lines.append(f"\n位置历史（{len(profile['location_history'])}）：")
                for loc in profile["location_history"]:
                    lines.append(f"  Ch.{loc['chapter']}：{loc['location']}")
                lines.append(f"\n参与事件（{len(profile['participated_events'])}）：")
                for evt in profile["participated_events"]:
                    lines.append(f"  Ch.{evt['chapter']}：{evt['event']}")
                return ToolResult(output="\n".join(lines))

            elif query_type == "causal_chain":
                event_id = kwargs.get("event_id", "")
                if not event_id:
                    events = db.get_nodes_by_kind("event")
                    lines = ["事件列表：\n"]
                    for e in events[-10:]:
                        lines.append(f"  {e['id']}: {e['name']} (Ch.{e['chapter']})")
                    return ToolResult(output="\n".join(lines))
                max_depth = kwargs.get("max_depth", 3)
                downstream = queries.get_causal_chain(event_id, "downstream")
                lines = [f"因果链追踪（从 {event_id}）：\n"]
                for entry in downstream:
                    indent = "  " * min(entry.get("depth", 0), 3)
                    lines.append(f"{indent}→ {entry['name']} (Ch.{entry['chapter']})")
                    for conn in entry.get("connections", []):
                        lines.append(f"{indent}  [{conn['kind']}] → {conn['to_name']}")
                return ToolResult(output="\n".join(lines))

            elif query_type == "relationship_network":
                char_id = kwargs.get("character_id", "")
                if not char_id:
                    return ToolResult(output="请提供 character_id", is_error=True)
                max_depth = kwargs.get("max_depth", 2)
                network = queries.get_relationship_network(char_id, max_depth)
                lines = [f"关系网络（中心：{char_id}，深度：{max_depth}）：\n"]
                lines.append(f"涉及角色（{len(network['nodes'])}）：")
                for n in network["nodes"]:
                    lines.append(f"  {n['name']}（深度 {n['depth']}）")
                lines.append(f"\n关系边（{len(network['edges'])}）：")
                for e in network["edges"]:
                    lines.append(f"  {e['source_name']} → {e['target_name']}：{e['type']}（{e['strength']:+d}）")
                return ToolResult(output="\n".join(lines))

            elif query_type == "open_hooks":
                hooks = queries.get_open_hooks()
                if not hooks:
                    return ToolResult(output="没有未闭合的伏笔")
                lines = [f"未闭合伏笔（{len(hooks)}）：\n"]
                for h in hooks:
                    lines.append(
                        f"  {h['id']}: {h['description']}\n"
                        f"    类型：{h['type']}，埋设：Ch.{h['planted_in_chapter']}，"
                        f"预期回收：Ch.{h['expected_range'][0]}-{h['expected_range'][1]}"
                    )
                return ToolResult(output="\n".join(lines))

            elif query_type == "thread_overview":
                threads = queries.get_thread_overview()
                if not threads:
                    return ToolResult(output="没有叙事线程")
                lines = [f"叙事线程（{len(threads)}）：\n"]
                for t in threads:
                    participants = "、".join(p["name"] for p in t["participants"])
                    lines.append(
                        f"  {t['name']}（{t['id']}）\n"
                        f"    状态：{t['status']}，权重：{t['weight']}，"
                        f"最后活跃：Ch.{t['last_active_chapter']}\n"
                        f"    参与者：{participants or '无'}"
                    )
                return ToolResult(output="\n".join(lines))

            elif query_type == "search":
                search_query = kwargs.get("search_query", "")
                if not search_query:
                    return ToolResult(output="请提供 search_query", is_error=True)
                results = queries.search_narrative(search_query)
                if not results:
                    return ToolResult(output=f"未找到匹配「{search_query}」的内容")
                lines = [f"搜索「{search_query}」结果（{len(results)}）：\n"]
                for r in results:
                    lines.append(f"  [{r['kind']}] {r['name']}: {r['description'][:60]}")
                return ToolResult(output="\n".join(lines))

            elif query_type == "graph_summary":
                summary = queries.get_graph_summary()
                lines = [
                    "图谱概览：",
                    f"  节点：{summary['nodes']}（角色 {summary['node_kinds'].get('character', 0)}，"
                    f"事件 {summary['node_kinds'].get('event', 0)}，"
                    f"地点 {summary['node_kinds'].get('location', 0)}，"
                    f"伏笔 {summary['node_kinds'].get('hook', 0)}）",
                    f"  边：{summary['edges']}（因果 {summary['edge_kinds'].get('causes', 0)}，"
                    f"关系 {summary['edge_kinds'].get('relationship', 0)}，"
                    f"参与 {summary['edge_kinds'].get('participates', 0)}）",
                    f"  已索引章节：{summary['chapters_indexed']}",
                    f"  未闭合伏笔：{summary['open_hooks']}",
                    f"  活跃线程：{summary['active_threads']}，休眠：{summary['dormant_threads']}",
                ]
                return ToolResult(output="\n".join(lines))

            else:
                return ToolResult(output=f"未知查询类型：{query_type}", is_error=True)

        finally:
            db.close()


class NovelConsistencyCheckTool(BaseTool):
    name = "novel_consistency_check"
    description = (
        "基于叙事知识图谱进行一致性检查：位置冲突、关系矛盾、伏笔超期、"
        "角色失踪、因果断裂、线程休眠。借鉴 CodeGraph 的依赖冲突检测思路。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "book_id": {"type": "string", "description": "书籍 ID"},
        },
        "required": ["book_id"],
    }

    def __init__(self, workspace: str = ""):
        self.workspace = workspace

    async def execute(self, **kwargs) -> ToolResult:
        err = _check_dramatica()
        if err:
            return ToolResult(output=err, is_error=True)

        book_id = kwargs.get("book_id", "")
        if not book_id:
            return ToolResult(output="请提供 book_id", is_error=True)

        try:
            from gangge.dramatica.narrative_graph import (
                NarrativeGraphDB, ConsistencyChecker,
            )
        except ImportError:
            return ToolResult(output="叙事图谱模块未加载", is_error=True)

        db_path = _get_books_dir(self.workspace) / book_id / "state" / "narrative_graph.db"
        if not db_path.exists():
            return ToolResult(output="图谱数据库不存在，请先写章节以自动建立图谱", is_error=True)

        db = NarrativeGraphDB(db_path)
        checker = ConsistencyChecker(db)

        try:
            ws_path = _get_books_dir(self.workspace) / book_id / "state" / "world_state.json"
            current_chapter = 0
            if ws_path.exists():
                ws_data = json.loads(ws_path.read_text(encoding="utf-8"))
                current_chapter = ws_data.get("current_chapter", 0)

            issues = checker.check_all(current_chapter)

            if not issues:
                return ToolResult(output="✅ 一致性检查通过，未发现叙事矛盾")

            critical = [i for i in issues if i.severity.value == "critical"]
            warnings = [i for i in issues if i.severity.value == "warning"]
            infos = [i for i in issues if i.severity.value == "info"]

            lines = [
                f"一致性检查结果：{len(critical)} 严重 / {len(warnings)} 警告 / {len(infos)} 提示\n",
            ]

            if critical:
                lines.append("🔴 严重问题：")
                for i in critical:
                    lines.append(f"  [{i.category.value}] {i.description}")
                    if i.suggestion:
                        lines.append(f"    建议：{i.suggestion}")

            if warnings:
                lines.append("\n🟡 警告：")
                for i in warnings:
                    lines.append(f"  [{i.category.value}] {i.description}")
                    if i.suggestion:
                        lines.append(f"    建议：{i.suggestion}")

            if infos:
                lines.append("\n🔵 提示：")
                for i in infos:
                    lines.append(f"  [{i.category.value}] {i.description}")

            return ToolResult(output="\n".join(lines))

        finally:
            db.close()


class NovelGraphRebuildTool(BaseTool):
    name = "novel_graph_rebuild"
    description = "重建叙事知识图谱。从书籍目录全量重新索引所有角色、地点、事件、关系。"
    input_schema = {
        "type": "object",
        "properties": {
            "book_id": {"type": "string", "description": "书籍 ID"},
        },
        "required": ["book_id"],
    }

    def __init__(self, workspace: str = ""):
        self.workspace = workspace

    async def execute(self, **kwargs) -> ToolResult:
        err = _check_dramatica()
        if err:
            return ToolResult(output=err, is_error=True)

        book_id = kwargs.get("book_id", "")
        if not book_id:
            return ToolResult(output="请提供 book_id", is_error=True)

        try:
            from gangge.dramatica.narrative_graph import (
                NarrativeGraphDB, NarrativeIndexer,
            )
        except ImportError:
            return ToolResult(output="叙事图谱模块未加载", is_error=True)

        book_dir = _get_books_dir(self.workspace) / book_id
        if not book_dir.exists():
            return ToolResult(output=f"书籍 {book_id} 不存在", is_error=True)

        db_path = book_dir / "state" / "narrative_graph.db"
        if db_path.exists():
            db_path.unlink()

        db = NarrativeGraphDB(db_path)
        db.initialize()
        indexer = NarrativeIndexer(db)

        try:
            stats = indexer.rebuild_from_book(book_dir)
            return ToolResult(
                output=(
                    f"叙事图谱重建完成！\n"
                    f"  节点：{stats['nodes']}\n"
                    f"  边：{stats['edges']}\n"
                    f"  已索引章节：{stats['chapters']}"
                )
            )
        finally:
            db.close()


class NovelExportTool(BaseTool):
    name = "novel_export"
    description = "导出整本小说为 Markdown 文件，包含所有章节正文。"
    input_schema = {
        "type": "object",
        "properties": {
            "book_id": {"type": "string", "description": "书籍 ID"},
            "output_path": {"type": "string", "description": "导出文件路径（不填则自动命名）"},
        },
        "required": ["book_id"],
    }

    def __init__(self, workspace: str = ""):
        self.workspace = workspace

    async def execute(self, **kwargs) -> ToolResult:
        err = _check_dramatica()
        if err:
            return ToolResult(output=err, is_error=True)

        book_id = kwargs.get("book_id", "")
        output_path = kwargs.get("output_path", "")
        if not book_id:
            return ToolResult(output="请提供 book_id", is_error=True)

        sm = StateManager(self.workspace, book_id)
        config_data = sm.read_config()
        if not config_data:
            return ToolResult(output=f"找不到书籍 {book_id}", is_error=True)

        title = config_data.get("title", "小说")
        parts = [f"# {title}\n\n"]

        chapter_dir = sm.chapter_dir
        if not chapter_dir.exists():
            return ToolResult(output="还没有写任何章节", is_error=True)

        chapters = sorted(chapter_dir.glob("*.md"))
        # 去重：同一章节号只取优先级最高的文件
        seen_nums = set()
        deduped = []
        for ch_path in chapters:
            ch_num = None
            name = ch_path.stem
            if name.startswith("ch") and ("_final" in name or "_draft" in name):
                num_str = name.replace("ch", "").replace("_final", "").replace("_draft", "")
                try:
                    ch_num = int(num_str)
                except ValueError:
                    pass
            elif name.startswith("chapter_"):
                try:
                    ch_num = int(name.replace("chapter_", ""))
                except ValueError:
                    pass
            elif name.startswith("第"):
                import re
                m = re.match(r"第(\d+)章", name)
                if m:
                    ch_num = int(m.group(1))
            if ch_num is not None:
                if ch_num not in seen_nums:
                    seen_nums.add(ch_num)
                    deduped.append((ch_num, ch_path))
            else:
                deduped.append((9999, ch_path))
        deduped.sort(key=lambda x: x[0])
        for ch_num, ch_path in deduped:
            content = ch_path.read_text(encoding="utf-8")
            parts.append(content + "\n\n---\n\n")

        full_text = "".join(parts)

        if not output_path:
            output_path = str(Path(self.workspace) / f"{title}_全文.md")

        Path(output_path).write_text(full_text, encoding="utf-8")

        return ToolResult(
            output=f"小说已导出到：{output_path}\n  总字数：{len(full_text)}\n  章节数：{len(chapters)}"
        )


class NovelListBooksTool(BaseTool):
    name = "novel_list_books"
    description = "列出工作目录中所有小说书籍及其状态。"
    input_schema = {
        "type": "object",
        "properties": {},
    }

    def __init__(self, workspace: str = ""):
        self.workspace = workspace

    async def execute(self, **kwargs) -> ToolResult:
        err = _check_dramatica()
        if err:
            return ToolResult(output=err, is_error=True)

        books_dir = _get_books_dir(self.workspace)
        if not books_dir.exists():
            return ToolResult(output="还没有创建任何小说，使用 novel_init 开始创作！")

        books = []
        for book_path in sorted(books_dir.iterdir()):
            if book_path.is_dir():
                config_path = book_path / "state" / "config.json"
                if config_path.exists():
                    try:
                        cfg = json.loads(config_path.read_text(encoding="utf-8"))
                        books.append(cfg)
                    except Exception:
                        pass

        if not books:
            return ToolResult(output="还没有创建任何小说，使用 novel_init 开始创作！")

        lines = ["小说列表：\n"]
        for cfg in books:
            lines.append(
                f"  《{cfg.get('title', '?')}》（{cfg.get('id', '?')}）\n"
                f"     题材：{cfg.get('genre', '?')}，"
                f"目标：{cfg.get('target_chapters', '?')}章 x {cfg.get('target_words_per_chapter', '?')}字，"
                f"状态：{cfg.get('status', '?')}"
            )

        return ToolResult(output="\n".join(lines))
