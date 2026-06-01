"""
叙事知识图谱 — 查询引擎

提供高级查询接口：
  - 因果链追踪：从某个事件出发，追踪所有下游后果
  - 角色关系网络：两个角色间的所有关系路径
  - 伏笔状态：未闭合伏笔及其传播路径
  - 角色知识图谱：某个角色知道什么、不知道什么
  - 位置历史：角色在哪些章节出现在哪些地点
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .schema import NarrativeGraphDB

logger = logging.getLogger(__name__)


class NarrativeQueries:
    """叙事图谱高级查询"""

    def __init__(self, db: NarrativeGraphDB):
        self.db = db

    def get_character_profile(self, char_id: str) -> dict[str, Any]:
        """
        获取角色完整档案：基本信息 + 关系 + 位置历史 + 参与事件 + 伏笔
        """
        node = self.db.get_node(f"char_{char_id}")
        if not node:
            return {"error": f"角色 {char_id} 不存在"}

        result: dict[str, Any] = {
            "id": char_id,
            "name": node["name"],
            "description": node["description"],
            "metadata": node["metadata"],
        }

        relationships = []
        for edge in self.db.get_outgoing_edges(f"char_{char_id}", ["relationship"]):
            target_node = self.db.get_node(edge["target"])
            relationships.append({
                "with": target_node["name"] if target_node else edge["target"],
                "with_id": edge["target"],
                "type": edge["metadata"].get("type", "unknown"),
                "strength": edge["metadata"].get("strength", 0),
                "chapter": edge["chapter"],
            })
        for edge in self.db.get_incoming_edges(f"char_{char_id}", ["relationship"]):
            source_node = self.db.get_node(edge["source"])
            relationships.append({
                "with": source_node["name"] if source_node else edge["source"],
                "with_id": edge["source"],
                "type": edge["metadata"].get("type", "unknown"),
                "strength": edge["metadata"].get("strength", 0),
                "chapter": edge["chapter"],
            })
        result["relationships"] = relationships

        locations = []
        for edge in self.db.get_outgoing_edges(f"char_{char_id}", ["located_at"]):
            target_node = self.db.get_node(edge["target"])
            locations.append({
                "location": target_node["name"] if target_node else edge["target"],
                "location_id": edge["target"],
                "chapter": edge["chapter"],
                "description": edge["description"],
            })
        result["location_history"] = sorted(locations, key=lambda x: x["chapter"])

        events = []
        for edge in self.db.get_outgoing_edges(f"char_{char_id}", ["participates"]):
            target_node = self.db.get_node(edge["target"])
            if target_node and target_node["kind"] == "event":
                events.append({
                    "event": target_node["name"],
                    "event_id": edge["target"],
                    "chapter": edge["chapter"],
                    "role": edge["description"],
                })
        result["participated_events"] = sorted(events, key=lambda x: x["chapter"])

        return result

    def get_causal_chain(self, event_id: str, direction: str = "downstream") -> list[dict]:
        """
        追踪因果链：从某个事件出发，追踪所有上游原因或下游后果。

        direction: "upstream"（追溯原因）或 "downstream"（追踪后果）
        """
        results = []
        visited = set()
        queue = [event_id]

        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)

            node = self.db.get_node(current)
            if not node:
                continue

            if direction == "downstream":
                edges = self.db.get_outgoing_edges(current, ["causes", "triggers"])
            else:
                edges = self.db.get_incoming_edges(current, ["causes", "triggers"])

            entry = {
                "id": current,
                "name": node["name"],
                "kind": node["kind"],
                "chapter": node["chapter"],
                "description": node["description"],
                "connections": [],
            }

            for edge in edges:
                target_id = edge["target"] if direction == "downstream" else edge["source"]
                target_node = self.db.get_node(target_id)
                entry["connections"].append({
                    "to": target_id,
                    "to_name": target_node["name"] if target_node else target_id,
                    "kind": edge["kind"],
                    "chapter": edge["chapter"],
                    "description": edge["description"],
                })
                if target_id not in visited:
                    queue.append(target_id)

            results.append(entry)

        return results

    def get_open_hooks(self) -> list[dict]:
        """获取所有未闭合伏笔及其关联信息"""
        hooks = []
        for node in self.db.get_nodes_by_kind("hook"):
            meta = node.get("metadata", {})
            if meta.get("status") == "open":
                hook_info = {
                    "id": node["id"],
                    "name": node["name"],
                    "description": node["description"],
                    "planted_in_chapter": meta.get("planted_in_chapter", 0),
                    "expected_range": meta.get("expected_range", [0, 0]),
                    "type": meta.get("type", "foreshadow"),
                    "related_characters": [],
                }
                for edge in self.db.get_incoming_edges(node["id"], ["foreshadows"]):
                    src = self.db.get_node(edge["source"])
                    if src:
                        hook_info["related_characters"].append({
                            "id": src["id"],
                            "name": src["name"],
                        })
                hooks.append(hook_info)
        return hooks

    def get_relationship_network(self, char_id: str, max_depth: int = 2) -> dict:
        """
        获取以某个角色为中心的关系网络。

        返回该角色直接和间接关联的所有角色及其关系。
        """
        nodes = {}
        edges = []
        visited = set()
        queue = [(f"char_{char_id}", 0)]

        while queue:
            current_id, depth = queue.pop(0)
            if current_id in visited or depth > max_depth:
                continue
            visited.add(current_id)

            node = self.db.get_node(current_id)
            if not node or node["kind"] != "character":
                continue
            nodes[current_id] = {"id": current_id, "name": node["name"], "depth": depth}

            for edge in self.db.get_outgoing_edges(current_id, ["relationship"]):
                target_node = self.db.get_node(edge["target"])
                if target_node and target_node["kind"] == "character":
                    edges.append({
                        "source": current_id,
                        "source_name": node["name"],
                        "target": edge["target"],
                        "target_name": target_node["name"],
                        "type": edge["metadata"].get("type", "unknown"),
                        "strength": edge["metadata"].get("strength", 0),
                        "chapter": edge["chapter"],
                    })
                    if edge["target"] not in visited:
                        queue.append((edge["target"], depth + 1))

            for edge in self.db.get_incoming_edges(current_id, ["relationship"]):
                source_node = self.db.get_node(edge["source"])
                if source_node and source_node["kind"] == "character":
                    edges.append({
                        "source": edge["source"],
                        "source_name": source_node["name"],
                        "target": current_id,
                        "target_name": node["name"],
                        "type": edge["metadata"].get("type", "unknown"),
                        "strength": edge["metadata"].get("strength", 0),
                        "chapter": edge["chapter"],
                    })
                    if edge["source"] not in visited:
                        queue.append((edge["source"], depth + 1))

        return {"center": char_id, "nodes": list(nodes.values()), "edges": edges}

    def get_location_timeline(self, location_id: str) -> list[dict]:
        """获取某个地点的时间线：哪些角色在哪些章节出现在这里"""
        timeline = []
        for edge in self.db.get_incoming_edges(f"loc_{location_id}", ["located_at"]):
            source_node = self.db.get_node(edge["source"])
            timeline.append({
                "character": source_node["name"] if source_node else edge["source"],
                "character_id": edge["source"],
                "chapter": edge["chapter"],
                "description": edge["description"],
            })
        return sorted(timeline, key=lambda x: x["chapter"])

    def get_thread_overview(self) -> list[dict]:
        """获取所有叙事线程及其参与角色"""
        threads = []
        for node in self.db.get_nodes_by_kind("thread"):
            meta = node.get("metadata", {})
            participants = []
            for edge in self.db.get_incoming_edges(node["id"], ["participates"]):
                src = self.db.get_node(edge["source"])
                if src and src["kind"] == "character":
                    participants.append({"id": src["id"], "name": src["name"]})
            threads.append({
                "id": node["id"],
                "name": node["name"],
                "status": meta.get("status", "unknown"),
                "weight": meta.get("weight", 1.0),
                "type": meta.get("type", "main"),
                "last_active_chapter": node["chapter"],
                "participants": participants,
            })
        return threads

    def search_narrative(self, query: str, limit: int = 20) -> list[dict]:
        """全文搜索叙事内容"""
        return self.db.search(query, limit)

    def get_graph_summary(self) -> dict[str, Any]:
        """获取图谱概览统计"""
        stats = self.db.stats()
        open_hooks = self.get_open_hooks()
        threads = self.get_thread_overview()
        stats["open_hooks"] = len(open_hooks)
        stats["active_threads"] = sum(1 for t in threads if t["status"] == "active")
        stats["dormant_threads"] = sum(1 for t in threads if t["status"] == "dormant")
        return stats
