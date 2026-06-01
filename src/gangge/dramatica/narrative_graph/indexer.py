"""
叙事知识图谱 — 索引器

从两个数据源提取实体和关系写入图谱：
  1. 世界状态（world_state.json）→ 角色、位置、关系、伏笔、因果链
  2. 章节内容（ch*_final.md）→ 事件、位置变化、情感变化

借鉴 CodeGraph 的 indexer 思路：
  - CodeGraph 用 Tree-sitter 从源码提取符号和调用关系
  - 我们用结构化数据（world_state.json）+ LLM 从章节提取叙事实体
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from .schema import NarrativeGraphDB

logger = logging.getLogger(__name__)


class NarrativeIndexer:
    """
    叙事图谱索引器。

    职责：
      1. 从 setup_state.json 初始化角色/地点/势力节点
      2. 从 world_state.json 同步关系/伏笔/因果链边
      3. 从章节内容提取事件节点和参与关系
    """

    def __init__(self, db: NarrativeGraphDB):
        self.db = db

    # ── 1. 从 setup 初始化基础节点 ──────────────────────────────────────────

    def index_setup(self, setup_data: dict) -> None:
        """
        从 setup_state.json 初始化角色、地点、势力节点。
        只在首次索引或 setup 变更时调用。
        """
        for char_data in setup_data.get("characters", {}).values():
            if isinstance(char_data, dict):
                char_id = char_data.get("id", "")
                char_name = char_data.get("name", "")
                if not char_id:
                    continue
                need = char_data.get("need", {})
                need_ext = need.get("external", "") if isinstance(need, dict) else ""
                need_int = need.get("internal", "") if isinstance(need, dict) else ""
                description = f"{char_data.get('profile', '')} 弧线:{char_data.get('arc', '')} 外部:{need_ext} 内在:{need_int}"
                self.db.upsert_node(
                    node_id=f"char_{char_id}",
                    kind="character",
                    name=char_name or char_id,
                    description=description,
                    chapter=0,
                    metadata={
                        "arc": char_data.get("arc", ""),
                        "behavior_lock": char_data.get("behavior_lock", []),
                        "personality": char_data.get("personality", []),
                        "backstory": char_data.get("backstory", ""),
                    },
                )

        world_data = setup_data.get("world", {})
        for loc in world_data.get("locations", []):
            loc_id = loc.get("id", "")
            if not loc_id:
                continue
            self.db.upsert_node(
                node_id=f"loc_{loc_id}",
                kind="location",
                name=loc.get("name", loc_id),
                description=loc.get("description", ""),
                chapter=0,
                metadata={
                    "connections": loc.get("connections", []),
                    "dramatic_potential": loc.get("dramatic_potential", ""),
                },
            )

        for faction in world_data.get("factions", []):
            f_id = faction.get("id", "")
            if not f_id:
                continue
            self.db.upsert_node(
                node_id=f"faction_{f_id}",
                kind="faction",
                name=faction.get("name", f_id),
                description=faction.get("description", ""),
                chapter=0,
                metadata={
                    "core_interest": faction.get("core_interest", ""),
                    "relations": faction.get("relations", {}),
                },
            )

        for evt in setup_data.get("events", {}).get("events", []):
            evt_id = evt.get("id", "")
            if not evt_id:
                continue
            self.db.upsert_node(
                node_id=f"event_{evt_id}",
                kind="event",
                name=evt.get("name", evt_id),
                description=evt.get("description", ""),
                chapter=0,
                metadata={
                    "effects": evt.get("effects", []),
                    "suggested_act": evt.get("suggested_act", 0),
                    "suggested_function": evt.get("suggested_function", ""),
                },
            )

        logger.info("[NarrativeIndexer] setup indexed: %d chars, %d locs, %d factions",
                     len(setup_data.get("characters", {})),
                     len(world_data.get("locations", [])),
                     len(world_data.get("factions", [])))

    # ── 2. 从世界状态同步边 ──────────────────────────────────────────────────

    def index_world_state(self, ws_data: dict) -> None:
        """
        从 world_state.json 同步关系边、伏笔边、因果链边。
        每章写完后调用，保持图谱与状态同步。
        """
        chapter = ws_data.get("current_chapter", 0)

        for pos in ws_data.get("character_positions", {}).items():
            char_id, loc_id = pos
            self.db.add_edge(
                source=f"char_{char_id}",
                target=f"loc_{loc_id}",
                kind="located_at",
                chapter=chapter,
                weight=1.0,
                description=f"{char_id}在{loc_id}",
            )

        for rel in ws_data.get("relationships", []):
            a = rel.get("character_a", "")
            b = rel.get("character_b", "")
            if not a or not b:
                continue
            rel_type = rel.get("type", "neutral")
            strength = rel.get("strength", 0)
            self.db.add_edge(
                source=f"char_{a}",
                target=f"char_{b}",
                kind="relationship",
                chapter=chapter,
                weight=abs(strength) / 100.0,
                description=f"{rel_type} 强度:{strength:+d}",
                metadata={
                    "type": rel_type,
                    "strength": strength,
                },
            )

        for hook in ws_data.get("pending_hooks", []):
            hook_id = hook.get("id", "")
            if not hook_id:
                continue
            hook_type = hook.get("type", "foreshadow")
            planted_ch = hook.get("planted_in_chapter", 0)
            status = hook.get("status", "open")
            self.db.upsert_node(
                node_id=f"hook_{hook_id}",
                kind="hook",
                name=f"{hook_type}:{hook_id}",
                description=hook.get("description", ""),
                chapter=planted_ch,
                metadata={
                    "type": hook_type,
                    "status": status,
                    "planted_in_chapter": planted_ch,
                    "expected_range": list(hook.get("expected_resolution_range", [0, 0])),
                    "resolved_in_chapter": hook.get("resolved_in_chapter"),
                },
            )
            if status == "resolved" and hook.get("resolved_in_chapter"):
                self.db.add_edge(
                    source=f"hook_{hook_id}",
                    target=f"hook_{hook_id}",
                    kind="resolves",
                    chapter=hook["resolved_in_chapter"],
                    description=f"伏笔{hook_id}在第{hook['resolved_in_chapter']}章回收",
                )

        for cl in ws_data.get("causal_chain", []):
            cl_id = cl.get("id", "")
            cl_ch = cl.get("chapter", 0)
            cause = cl.get("cause", "")
            event = cl.get("event", "")
            consequence = cl.get("consequence", "")
            self.db.upsert_node(
                node_id=f"causal_{cl_id}",
                kind="event",
                name=event[:50] if event else cl_id,
                description=f"因:{cause} → 事件:{event} → 果:{consequence}",
                chapter=cl_ch,
                metadata={
                    "cause": cause,
                    "consequence": consequence,
                    "thread_id": cl.get("thread_id", "thread_main"),
                    "triggered_events": cl.get("triggered_events", []),
                },
            )
            for ad in cl.get("affected_decisions", []):
                char_id = ad.get("character_id", "")
                if char_id:
                    self.db.add_edge(
                        source=f"char_{char_id}",
                        target=f"causal_{cl_id}",
                        kind="participates",
                        chapter=cl_ch,
                        description=f"{char_id}决定:{ad.get('decision', '')}",
                    )

        for thread in ws_data.get("threads", []):
            t_id = thread.get("id", "")
            if not t_id:
                continue
            self.db.upsert_node(
                node_id=f"thread_{t_id}",
                kind="thread",
                name=thread.get("name", t_id),
                description=f"目标:{thread.get('goal', '')} 弧线:{thread.get('growth_arc', '')}",
                chapter=thread.get("last_active_chapter", 0),
                metadata={
                    "type": thread.get("type", "main"),
                    "status": thread.get("status", "active"),
                    "weight": thread.get("weight", 1.0),
                    "pov_character_id": thread.get("pov_character_id", ""),
                    "character_ids": thread.get("character_ids", []),
                },
            )
            for char_id in thread.get("character_ids", []):
                self.db.add_edge(
                    source=f"char_{char_id}",
                    target=f"thread_{t_id}",
                    kind="participates",
                    chapter=thread.get("last_active_chapter", 0),
                    description=f"{char_id}参与线程{thread.get('name', t_id)}",
                )

        for te in ws_data.get("timeline", []):
            te_id = te.get("id", "")
            te_ch = te.get("chapter", 0)
            char_id = te.get("character_id", "")
            loc_id = te.get("location_id", "")
            action = te.get("action", "")
            if char_id and loc_id:
                self.db.add_edge(
                    source=f"char_{char_id}",
                    target=f"loc_{loc_id}",
                    kind="located_at",
                    chapter=te_ch,
                    description=f"Ch.{te_ch}: {action[:40]}",
                )

        logger.info("[NarrativeIndexer] world_state indexed at chapter %d", chapter)

    # ── 3. 从章节内容提取事件节点 ──────────────────────────────────────────

    def index_chapter(
        self,
        chapter_number: int,
        title: str,
        content: str,
        known_characters: dict[str, str] | None = None,
    ) -> None:
        """
        从章节正文提取事件节点和参与关系。
        使用轻量级文本分析（不依赖 LLM），基于已知角色名匹配。
        """
        content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()

        if self.db.is_chapter_indexed(chapter_number, content_hash):
            logger.debug("[NarrativeIndexer] chapter %d already indexed (hash match)", chapter_number)
            return

        self.db.upsert_chapter(
            number=chapter_number,
            title=title,
            word_count=len(content),
            content_hash=content_hash,
        )

        char_map = known_characters or {}
        for char_id, char_name in char_map.items():
            if char_name and char_name in content:
                self.db.upsert_node(
                    node_id=f"char_{char_id}",
                    kind="character",
                    name=char_name,
                    description=f"出现在第{chapter_number}章",
                    chapter=chapter_number,
                )
                self.db.add_edge(
                    source=f"char_{char_id}",
                    target=f"event_ch{chapter_number:04d}",
                    kind="participates",
                    chapter=chapter_number,
                    description=f"{char_name}出现在第{chapter_number}章",
                )

        self.db.upsert_node(
            node_id=f"event_ch{chapter_number:04d}",
            kind="event",
            name=f"第{chapter_number}章: {title}",
            description=content[:200],
            chapter=chapter_number,
            metadata={"word_count": len(content)},
        )

        if chapter_number > 1:
            self.db.add_edge(
                source=f"event_ch{chapter_number - 1:04d}",
                target=f"event_ch{chapter_number:04d}",
                kind="causes",
                chapter=chapter_number,
                weight=0.5,
                description=f"第{chapter_number - 1}章→第{chapter_number}章（叙事连续）",
            )

        logger.info("[NarrativeIndexer] chapter %d indexed (%d chars)", chapter_number, len(content))

    # ── 4. 全量重建 ──────────────────────────────────────────────────────────

    def rebuild_from_book(self, book_dir: Path) -> dict[str, Any]:
        """
        从书籍目录全量重建图谱。用于首次索引或数据修复。
        """
        setup_path = book_dir / "state" / "setup_state.json"
        ws_path = book_dir / "state" / "world_state.json"
        chapter_dir = book_dir / "chapters"

        stats = {"nodes": 0, "edges": 0, "chapters": 0}

        if setup_path.exists():
            setup_data = json.loads(setup_path.read_text(encoding="utf-8"))
            self.index_setup(setup_data)

        if ws_path.exists():
            ws_data = json.loads(ws_path.read_text(encoding="utf-8"))
            self.index_world_state(ws_data)

        known_chars = {}
        if setup_path.exists():
            setup_data = json.loads(setup_path.read_text(encoding="utf-8"))
            for cid, cdata in setup_data.get("characters", {}).items():
                if isinstance(cdata, dict) and cdata.get("name"):
                    known_chars[cid] = cdata["name"]

        if chapter_dir.exists():
            for ch_path in sorted(chapter_dir.glob("ch*_final.md")):
                name = ch_path.stem
                ch_num_str = name.replace("ch", "").replace("_final", "")
                try:
                    ch_num = int(ch_num_str)
                except ValueError:
                    continue
                content = ch_path.read_text(encoding="utf-8")
                title = ""
                for line in content.split("\n")[:3]:
                    if line.strip().startswith("#"):
                        title = line.strip().lstrip("#").strip()
                        break
                self.index_chapter(ch_num, title, content, known_chars)
                stats["chapters"] += 1

        db_stats = self.db.stats()
        stats["nodes"] = db_stats["nodes"]
        stats["edges"] = db_stats["edges"]

        logger.info("[NarrativeIndexer] rebuild complete: %s", stats)
        return stats
