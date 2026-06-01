"""
叙事知识图谱 — 一致性检查

基于图谱数据检测叙事矛盾和不一致：
  - 位置冲突：同一章中角色同时出现在两个不同地点
  - 关系矛盾：A→B 敌对但 B→A 同盟
  - 伏笔超期：伏笔超过预期回收章节仍未闭合
  - 角色失踪：重要角色长时间未出现
  - 因果断裂：事件缺少上游原因
  - 信息越界：角色知道了不该知道的信息
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .schema import NarrativeGraphDB
from .queries import NarrativeQueries
from .traversal import NarrativeTraversal

logger = logging.getLogger(__name__)


class IssueSeverity(str, Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class IssueCategory(str, Enum):
    LOCATION_CONFLICT = "location_conflict"
    RELATIONSHIP_CONTRADICTION = "relationship_contradiction"
    HOOK_OVERDUE = "hook_overdue"
    CHARACTER_MISSING = "character_missing"
    CAUSAL_GAP = "causal_gap"
    INFO_LEAK = "info_leak"
    THREAD_DORMANT = "thread_dormant"


@dataclass
class ConsistencyIssue:
    severity: IssueSeverity
    category: IssueCategory
    description: str
    chapter: int = 0
    entities: list[str] = field(default_factory=list)
    suggestion: str = ""


class ConsistencyChecker:
    """叙事一致性检查器"""

    def __init__(self, db: NarrativeGraphDB):
        self.db = db
        self.queries = NarrativeQueries(db)
        self.traversal = NarrativeTraversal(db)

    def check_all(self, current_chapter: int = 0) -> list[ConsistencyIssue]:
        """运行所有一致性检查"""
        issues = []
        issues.extend(self.check_location_conflicts())
        issues.extend(self.check_relationship_contradictions())
        issues.extend(self.check_overdue_hooks(current_chapter))
        issues.extend(self.check_missing_characters(current_chapter))
        issues.extend(self.check_causal_gaps())
        issues.extend(self.check_dormant_threads(current_chapter))
        return issues

    def check_location_conflicts(self) -> list[ConsistencyIssue]:
        """
        检查位置冲突：同一章中角色同时出现在两个不同地点。

        借鉴 CodeGraph 的依赖冲突检测思路：
        CodeGraph 检测循环依赖，我们检测位置矛盾。
        """
        issues = []
        char_locations: dict[str, dict[int, list[str]]] = {}

        for edge in self.db.get_edges_by_kind("located_at"):
            source = edge["source"]
            target = edge["target"]
            chapter = edge["chapter"]
            if source not in char_locations:
                char_locations[source] = {}
            if chapter not in char_locations[source]:
                char_locations[source][chapter] = []
            if target not in char_locations[source][chapter]:
                char_locations[source][chapter].append(target)

        for char_id, chapters in char_locations.items():
            for chapter, locations in chapters.items():
                if len(locations) > 1:
                    char_node = self.db.get_node(char_id)
                    char_name = char_node["name"] if char_node else char_id
                    loc_names = []
                    for loc_id in locations:
                        loc_node = self.db.get_node(loc_id)
                        loc_names.append(loc_node["name"] if loc_node else loc_id)
                    issues.append(ConsistencyIssue(
                        severity=IssueSeverity.CRITICAL,
                        category=IssueCategory.LOCATION_CONFLICT,
                        description=f"{char_name}在第{chapter}章同时出现在：{'、'.join(loc_names)}",
                        chapter=chapter,
                        entities=[char_id] + locations,
                        suggestion=f"修改第{chapter}章，确保{char_name}只有一个位置，或添加移动说明",
                    ))

        return issues

    def check_relationship_contradictions(self) -> list[ConsistencyIssue]:
        """
        检查关系矛盾：A→B 的关系与 B→A 的关系不一致。
        """
        issues = []
        checked_pairs = set()

        for edge in self.db.get_edges_by_kind("relationship"):
            source = edge["source"]
            target = edge["target"]
            pair_key = ":".join(sorted([source, target]))
            if pair_key in checked_pairs:
                continue
            checked_pairs.add(pair_key)

            forward = self.db.get_edges_between(source, target, "relationship")
            backward = self.db.get_edges_between(target, source, "relationship")

            if forward and backward:
                f_latest = max(forward, key=lambda e: e["chapter"])
                b_latest = max(backward, key=lambda e: e["chapter"])

                f_type = f_latest["metadata"].get("type", "neutral")
                b_type = b_latest["metadata"].get("type", "neutral")
                f_strength = f_latest["metadata"].get("strength", 0)
                b_strength = b_latest["metadata"].get("strength", 0)

                if (f_type == "ally" and b_type == "enemy") or \
                   (f_type == "enemy" and b_type == "ally"):
                    src_node = self.db.get_node(source)
                    tgt_node = self.db.get_node(target)
                    issues.append(ConsistencyIssue(
                        severity=IssueSeverity.WARNING,
                        category=IssueCategory.RELATIONSHIP_CONTRADICTION,
                        description=(
                            f"{src_node['name'] if src_node else source}→"
                            f"{tgt_node['name'] if tgt_node else target} 是{f_type}，"
                            f"但反向是{b_type}"
                        ),
                        chapter=max(f_latest["chapter"], b_latest["chapter"]),
                        entities=[source, target],
                        suggestion="检查关系是否需要双向一致，或添加说明（如表面同盟实则敌对）",
                    ))

                elif abs(f_strength - b_strength) > 50:
                    src_node = self.db.get_node(source)
                    tgt_node = self.db.get_node(target)
                    issues.append(ConsistencyIssue(
                        severity=IssueSeverity.INFO,
                        category=IssueCategory.RELATIONSHIP_CONTRADICTION,
                        description=(
                            f"{src_node['name'] if src_node else source}→"
                            f"{tgt_node['name'] if tgt_node else target} "
                            f"关系强度不对称：{f_strength:+d} vs {b_strength:+d}"
                        ),
                        chapter=max(f_latest["chapter"], b_latest["chapter"]),
                        entities=[source, target],
                        suggestion="可能是单方面情感，也可能是遗漏更新",
                    ))

        return issues

    def check_overdue_hooks(self, current_chapter: int) -> list[ConsistencyIssue]:
        """检查超期未回收的伏笔"""
        issues = []
        for hook in self.queries.get_open_hooks():
            expected_range = hook.get("expected_range", [0, 0])
            if len(expected_range) >= 2 and expected_range[1] > 0:
                if current_chapter > expected_range[1]:
                    gap = current_chapter - expected_range[1]
                    issues.append(ConsistencyIssue(
                        severity=IssueSeverity.WARNING if gap <= 5 else IssueSeverity.CRITICAL,
                        category=IssueCategory.HOOK_OVERDUE,
                        description=f"伏笔「{hook['description']}」已超期{gap}章未回收（预期第{expected_range[0]}-{expected_range[1]}章）",
                        chapter=hook.get("planted_in_chapter", 0),
                        entities=[hook["id"]],
                        suggestion=f"在第{current_chapter + 1}章内回收此伏笔，或调整预期范围",
                    ))
        return issues

    def check_missing_characters(self, current_chapter: int, threshold: int = 10) -> list[ConsistencyIssue]:
        """检查重要角色长时间未出现"""
        issues = []
        for node in self.db.get_nodes_by_kind("character"):
            meta = node.get("metadata", {})
            arc = meta.get("arc", "")
            if arc in ("flat", ""):
                continue

            last_seen = node["chapter"]
            if current_chapter > 0 and last_seen > 0 and (current_chapter - last_seen) > threshold:
                gap = current_chapter - last_seen
                issues.append(ConsistencyIssue(
                    severity=IssueSeverity.WARNING if gap <= threshold * 2 else IssueSeverity.CRITICAL,
                    category=IssueCategory.CHARACTER_MISSING,
                    description=f"角色「{node['name']}」已{gap}章未出现（最后出现在第{last_seen}章）",
                    chapter=last_seen,
                    entities=[node["id"]],
                    suggestion=f"考虑在近期章节中安排{node['name']}出场，或解释其缺席原因",
                ))
        return issues

    def check_causal_gaps(self) -> list[ConsistencyIssue]:
        """
        检查因果断裂：事件缺少上游原因。

        借鉴 CodeGraph 的未解析引用检测：
        CodeGraph 检测 unresolved_refs，我们检测缺少 causes 入边的事件。
        """
        issues = []
        events = self.db.get_nodes_by_kind("event")

        for event_node in events:
            if event_node["chapter"] <= 1:
                continue

            incoming_causes = self.db.get_incoming_edges(event_node["id"], ["causes", "triggers"])
            if not incoming_causes:
                outgoing = self.db.get_outgoing_edges(event_node["id"], ["causes"])
                if outgoing:
                    issues.append(ConsistencyIssue(
                        severity=IssueSeverity.INFO,
                        category=IssueCategory.CAUSAL_GAP,
                        description=f"事件「{event_node['name']}」缺少上游原因（第{event_node['chapter']}章）",
                        chapter=event_node["chapter"],
                        entities=[event_node["id"]],
                        suggestion="添加因果链说明此事件的触发原因",
                    ))

        return issues

    def check_dormant_threads(self, current_chapter: int, threshold: int = 8) -> list[ConsistencyIssue]:
        """检查长时间未活跃的叙事线程"""
        issues = []
        for thread in self.queries.get_thread_overview():
            if thread["status"] == "active" and thread["type"] != "main":
                last_active = thread.get("last_active_chapter", 0)
                if current_chapter > 0 and last_active > 0:
                    gap = current_chapter - last_active
                    if gap > threshold:
                        issues.append(ConsistencyIssue(
                            severity=IssueSeverity.WARNING,
                            category=IssueCategory.THREAD_DORMANT,
                            description=f"叙事线程「{thread['name']}」已{gap}章未活跃（状态仍为 active）",
                            chapter=last_active,
                            entities=[thread["id"]],
                            suggestion=f"安排线程{thread['name']}在近期章节推进，或将其状态改为 dormant",
                        ))
        return issues
